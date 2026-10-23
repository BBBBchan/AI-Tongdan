"""Recover stopped live capture's remaining recorded audio on its source clock.

The caller supplies its existing Store and profile-scoped Settings. Checkpoints
are durable before event insertion; source keys make interrupted insertion safe.
This recovers the tail, not an assertion that all earlier live ASR was correct.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx

from .render import command
from .subtitle_review import (ALIGNER, MODEL, ReviewError, _read_wav,
                              caption_events, identity_context, request_review,
                              validate_response)

CHUNK_SECONDS = 20.0
LOOKBACK_SECONDS = 900.0
ACTIVE = {"connecting", "live", "waiting", "reconnecting", "recording"}


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as handle:
        os.chmod(temp, 0o600)
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def recovery_plan(session, segments):
    rows = []
    for segment in segments:
        start, end = _number(segment.get("start")), _number(segment.get("end"))
        if start is None or end is None or not 0 <= start < end:
            raise ReviewError("invalid_recording_timeline")
        rows.append({"start": start, "end": end, "path": str(segment["path"])})
    rows.sort(key=lambda row: row["start"])
    if not rows:
        # A failed connection with no recorded audio has no model work to drain.
        return {"start": 0., "end": 0., "scope": "no_recorded_media", "chunks": [], "gaps": []}
    end = rows[-1]["end"]
    watermark = _number(session.get("asr_drained_until"))
    if watermark is None:
        watermark = _number((session.get("asr") or {}).get("processed_until"))
    scope = "after_live_asr_watermark" if watermark is not None else "last_900_recorded_seconds_no_watermark"
    if watermark is not None and not 0 <= watermark <= end + 30:
        raise ReviewError("invalid_asr_watermark")
    start = max(rows[0]["start"], min(watermark, end) if watermark is not None else max(0., end-LOOKBACK_SECONDS))
    chunks, gaps, groups = [], [], []
    for row in rows:
        if row["end"] <= start:
            continue
        if not Path(row["path"]).is_file():
            raise ReviewError("recorded_tail_file_missing")
        if groups and row["start"] < groups[-1][-1]["end"] - .025:
            raise ReviewError("overlapping_recording_timeline")
        if not groups or row["start"] > groups[-1][-1]["end"] + .025:
            if groups:
                gaps.append({"start": groups[-1][-1]["end"], "end": row["start"]})
            groups.append([])
        groups[-1].append(row)
    for group in groups:
        cursor = max(start, group[0]["start"])
        while cursor < group[-1]["end"] - .001:
            chunk_end = min(cursor+CHUNK_SECONDS, group[-1]["end"])
            sources = [row for row in group if row["end"] > cursor and row["start"] < chunk_end]
            chunk = {"start": round(cursor, 6), "end": round(chunk_end, 6), "sources": sources}
            chunk["key"] = hashlib.sha256(json.dumps(chunk, sort_keys=True).encode()).hexdigest()[:24]
            chunks.append(chunk)
            cursor = chunk_end
    return {"start": start, "end": end, "scope": scope, "chunks": chunks, "gaps": gaps}


async def _extract(settings, directory, chunk):
    directory.mkdir(parents=True, exist_ok=True)
    lines = ["ffconcat version 1.0"]
    for index, row in enumerate(chunk["sources"]):
        name = f"segment-{index:05}.ts"
        link = directory / name
        link.unlink(missing_ok=True)
        link.symlink_to(Path(row["path"]).resolve())
        lines.extend([f"file '{name}'", f"duration {row['end']-row['start']:.6f}"])
    concat = directory / "source.ffconcat"
    concat.write_text("\n".join(lines) + "\n")
    wav = directory / "audio.wav"
    duration = chunk["end"]-chunk["start"]
    offset = chunk["start"]-chunk["sources"][0]["start"]
    await command([settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", concat, "-vn",
                   "-af", f"atrim=start={offset:.6f}:duration={duration:.6f},asetpts=PTS-STARTPTS",
                   "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav], timeout=180)
    actual, silent = _read_wav(wav, duration)
    return wav, actual, silent


async def recover_live_tail(settings, store, sid):
    session = store.session(sid)
    if not session or session.get("mode") != "live" or session.get("status") in ACTIVE:
        raise ReviewError("live_tail_requires_stopped_capture")
    profile = session.get("model_profile", "default")
    if getattr(settings, "model_profile", "default") != profile:
        raise ReviewError("live_tail_model_profile_mismatch")
    old = session.get("tail_recovery") or {}
    report = {"version": 1, "status": "running", "model_profile": profile,
              "model": MODEL, "aligner": ALIGNER, "timing_basis": "forced_alignment",
              "source_clock": "recording_seconds", "started_at": time.time(),
              "speaker_attribution": "unknown", "complete_chunks": []}
    try:
        plan = recovery_plan(session, store.segments(sid))
        fingerprint = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        if old.get("status") == "complete" and old.get("plan_sha256") == fingerprint:
            return old
        report.update(plan_sha256=fingerprint, coverage_start=plan["start"], coverage_end=plan["end"],
                      scope=plan["scope"], recording_gaps=plan["gaps"], total_chunks=len(plan["chunks"]))
        store.update_session(sid, tail_recovery=report)
        directory = Path(settings.data_dir) / "sessions" / sid / "tail-recovery"
        config = settings.audio_config()
        if plan["chunks"]:
            parsed = urlsplit(config.get("base_url", ""))
            if (config.get("error") or parsed.scheme not in {"https", "http"} or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment or not config.get("api_key")):
                raise ReviewError("tail_audio_service_unavailable")
        async with httpx.AsyncClient(trust_env=False) as client:
            for chunk in plan["chunks"]:
                folder = directory / chunk["key"]
                checkpoint = folder / "result.json"
                if checkpoint.is_file():
                    saved = json.loads(checkpoint.read_text())
                    if saved.get("key") != chunk["key"]:
                        raise ReviewError("tail_checkpoint_mismatch")
                    result = validate_response(saved["result"], chunk["end"]-chunk["start"])
                else:
                    wav, duration, silent = await _extract(settings, folder, chunk)
                    result = ({"text": "", "words": [], "duration": duration, "model": MODEL,
                               "aligner": ALIGNER, "timing_basis": "forced_alignment"} if silent else
                              await request_review(client, config, wav.read_bytes(), identity_context(session), duration))
                    _atomic(checkpoint, {"version": 1, "key": chunk["key"], "result": result,
                                         "zero_pcm": silent, "model_profile": profile})
                    wav.unlink(missing_ok=True)
                existing = store.events(sid, chunk["start"], chunk["end"], limit=10000)
                for index, event in enumerate(caption_events(result, chunk["start"], 22)):
                    # With no persisted watermark, do not duplicate known live
                    # transcript intervals while recovering the available tail.
                    if plan["scope"].endswith("no_watermark") and any(
                            row.get("kind") in {"audio_segment", "transcript"}
                            and not (row.get("data") or {}).get("tail_recovery")
                            and min(row["end"], event["end"]) > max(row["start"], event["start"])
                            for row in existing):
                        continue
                    store.add_event(sid, "transcript", event["start"], event["end"], event["text"],
                                    data={**event["data"], "tail_recovery": True, "model_profile": profile,
                                          "recovery_chunk": chunk["key"]},
                                    source_key=f"tail-v1:{chunk['key']}:{index}")
                report["complete_chunks"].append({"key": chunk["key"], "start": chunk["start"],
                                                   "end": chunk["end"], "words": len(result["words"])})
                store.update_session(sid, tail_recovery=report)
        report.update(status="complete", completed_at=time.time())
        store.update_session(sid, tail_recovery=report)
        return report
    except BaseException as exc:
        reason = str(exc) if isinstance(exc, ReviewError) else type(exc).__name__
        report.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                      error=reason, updated_at=time.time())
        store.update_session(sid, tail_recovery=report)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ReviewError("live_tail_recovery_incomplete") from None
