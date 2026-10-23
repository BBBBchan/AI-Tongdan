#!/usr/bin/env python3
"""Re-listen to existing clips and replace their artifacts without changing URLs.

The rendering adapter opens the production journal read-only; it never constructs
Store and cannot enqueue publications. Only the final, validated subtitle fields
are written to the existing job row. A directory exchange and an on-disk receipt
allow interrupted replacements to finish without losing the previous export.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import ctypes
from dataclasses import replace
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid
import wave
import zipfile

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipper.config import Settings
from clipper.render import Renderer, probe, validate_range
from clipper.subtitle_review import MODEL, ALIGNER, identity_context, validate_response
from clipper.library_manifest import atomic_json as _atomic_json

JOB_ID = re.compile(r"^[0-9a-f]{16}$")
MARKER = "bulk-subtitle-replacement.json"
OPTIONS = {"aspect": "landscape", "resolution": 720, "captions": True,
           "emphasis": True, "cover": True, "cover_mode": "designed", "focus_region": None}
PRESERVED_METADATA = ("publication_metadata", "publication_metadata_evidence", "evidence_snapshot",
                      "title_variants", "source_title", "source_url")


class ReprocessError(RuntimeError):
    """Fixed, non-sensitive errors suitable for the progress receipt."""


def atomic_json(path, value):
    _atomic_json(path, value)
    fsync_directory(Path(path).parent)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def readonly_db(data_dir):
    con = sqlite3.connect(f"file:{Path(data_dir).resolve() / 'clipper.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def snapshot(data_dir):
    con = readonly_db(data_dir)
    try:
        return {table: [json.loads(row[0]) for row in con.execute(f"SELECT payload FROM {table} ORDER BY created DESC")]
                for table in ("jobs", "sessions", "anchors", "publications")}
    finally:
        con.close()


def options_for(job):
    return {**OPTIONS, **{k: v for k, v in job.get("options", {}).items() if k in OPTIONS}, "captions": True}


def generated_demo(source):
    """These project-owned authored videos are not livestream clip recordings."""
    if source.get("mode") != "file":
        return False
    parts = Path(source.get("media_path", "")).parts
    return any(p.startswith("xiaodan-intro") for p in parts) or "publish-demo.mp4" in parts


def font_is_current(folder, job):
    options = options_for(job)
    expected = (44 if options["aspect"] == "vertical" else 48) * options["resolution"] / 720
    try:
        line = next(x for x in (folder / "subtitles.ass").read_text().splitlines()
                    if x.startswith("Style: Caption,"))
        return abs(float(line.split(",")[2]) - expected) < .1
    except (OSError, ValueError, IndexError, StopIteration):
        return False


def valid_review(folder, job):
    report = read_json(folder / "subtitle-review.json", {})
    if not report.get("applied") or not font_is_current(folder, job):
        return False
    if report.get("status") == "silent_audio" and report.get("timing_basis") == "digital_silence":
        return True
    if (report.get("status"), report.get("model"), report.get("aligner"), report.get("timing_basis")) != (
            "complete", MODEL, ALIGNER, "forced_alignment"):
        return False
    try:
        # PCM extraction can finish a few milliseconds past the requested
        # trim because source audio is packetized. Match the renderer's actual
        # WAV clock, while independently bounding drift from the clip range.
        duration = report.get("duration")
        expected = job["anchor"]["end"] - job["anchor"]["start"]
        if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not math.isfinite(duration) or abs(duration - expected) > .25):
            return False
        with wave.open(str(folder / "review.wav"), "rb") as wav:
            if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
                return False
            measured_duration = wav.getnframes() / wav.getframerate()
        if abs(measured_duration - duration) > 1 / 16000:
            return False
        validate_response(report, measured_duration)
    except (ValueError, KeyError, TypeError, OSError, wave.Error, EOFError):
        return False
    return bool(report.get("words"))


class ReadOnlyRenderStore:
    """Only the in-memory staged job is mutable; all source rows are read-only."""
    def __init__(self, data_dir, job):
        self.db = readonly_db(data_dir)
        self.current_job = deepcopy(job)
        self.current_job["options"] = options_for(job)

    def job(self, job_id):
        if job_id != self.current_job["id"]:
            raise ReprocessError("unexpected_render_job")
        return deepcopy(self.current_job)

    def update_job(self, job_id, **fields):
        self.job(job_id)
        self.current_job.update(fields)
        return self.job(job_id)

    def update_anchor(self, anchor_id, **fields):
        # Renderer marks an anchor exported; it was already exported. Never
        # modify the live anchor or its editorial choices from this process.
        if anchor_id != self.current_job["anchor_id"]:
            raise ReprocessError("unexpected_render_anchor")
        return deepcopy(self.current_job["anchor"])

    def session(self, session_id):
        row = self.db.execute("SELECT payload FROM sessions WHERE id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def segments(self, session_id, start, end):
        return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in self.db.execute(
            "SELECT * FROM segments WHERE session_id=? AND end>? AND start<? ORDER BY start",
            (session_id, start, end))]

    def events(self, session_id, start=0, end=1e12, limit=2000):
        return [{**dict(row), "data": json.loads(row["data"])} for row in self.db.execute(
            "SELECT * FROM events WHERE session_id=? AND end>=? AND start<=? ORDER BY start LIMIT ?",
            (session_id, start, end, limit))]


def original_focus_report(folder, job):
    metadata = read_json(folder / "metadata.json", {})
    report_path = folder / "chat-focus.json"
    report = (read_json(report_path) if report_path.exists()
              else metadata.get("chat_focus", job.get("chat_focus")))
    if report is None and not report_path.exists():
        return {"version": 1, "status": "no_recorded_original_focus", "plans": [], "omissions": []}
    if not isinstance(report, dict) or not isinstance(report.get("plans"), list) or not isinstance(report.get("omissions"), list):
        raise ReprocessError("invalid_original_focus_report")
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    duration = job["anchor"]["end"] - job["anchor"]["start"]
    if len(report["plans"]) > 4:
        raise ReprocessError("invalid_original_focus_report")
    for plan in report["plans"]:
        start, end, box = plan.get("start"), plan.get("end"), plan.get("bbox")
        width, height = report.get("source_width"), report.get("source_height")
        zoom = plan.get("magnification", 1.3)
        if (report.get("timing_basis") != "decoded_source_frame_pts"
                or plan.get("timing_basis") != "decoded_source_frame_pts"
                or not all(number(x) for x in (start, end, width, height, zoom))
                or not 0 <= start < end <= duration + .05 or end-start > 1.55
                or not 1.25 <= zoom <= 1.4 or width <= 0 or height <= 0
                or not isinstance(box, list) or len(box) != 4 or not all(number(x) for x in box)
                or not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height)):
            raise ReprocessError("invalid_original_focus_report")
    return report


def render_signature(job, source, segments, focus_report=None):
    # Cover images and copy are restored separately. These are all anchor
    # properties used by caption rendering and pixel-region emphasis.
    fields = ("session_id", "start", "end", "style", "focus_event_ids", "focus_region", "evidence_ids")
    payload = {"anchor": {key: job["anchor"].get(key) for key in fields},
               "options": options_for(job), "identity": identity_context(source),
               "original_focus_sha256": hashlib.sha256(json.dumps(focus_report, sort_keys=True).encode()).hexdigest(),
               "segments": [{"start": s["start"], "end": s["end"],
                             "path": str(Path(s["path"]).resolve()),
                             "size": Path(s["path"]).stat().st_size,
                             "mtime_ns": Path(s["path"]).stat().st_mtime_ns} for s in segments]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def inventory(data_dir):
    data = snapshot(data_dir)
    sources = {s["id"]: s for s in data["sessions"]}
    entries = []
    for job in data["jobs"]:
        if job.get("status") != "complete" or not JOB_ID.fullmatch(job["id"]):
            continue
        folder = data_dir / "exports" / job["id"]
        item = {"id": job["id"], "session_id": job["session_id"],
                "superseded": bool(job.get("superseded_by")),
                "duration": job.get("duration", 0), "status": "pending"}
        source = sources.get(job["session_id"], {})
        if generated_demo(source):
            item.update(status="excluded", reason="authored_intro_or_synthetic_demo")
        elif folder.is_symlink() or not (folder / "clip.mp4").is_file():
            item.update(status="blocked", reason="missing_or_unsafe_export")
        elif valid_review(folder, job):
            item.update(status="already_reviewed")
        else:
            adapter = ReadOnlyRenderStore(data_dir, job)
            try:
                segments = adapter.segments(job["session_id"], job["anchor"]["start"], job["anchor"]["end"])
                validate_range(job["anchor"], segments)
                if any(not Path(s["path"]).is_file() for s in segments):
                    raise ReprocessError("missing_source")
                if source.get("auto_publish"):
                    raise ReprocessError("source_auto_publish_enabled")
                item["signature"] = render_signature(job, source, segments, original_focus_report(folder, job))
            except (ValueError, OSError, TypeError, KeyError, ReprocessError) as exc:
                item.update(status="blocked", reason=str(exc) if isinstance(exc, ReprocessError) else "invalid_source_range")
            finally:
                adapter.db.close()
        entries.append(item)
    return {"version": 1, "created_at": time.time(), "scope": "all_completed_exports",
            "model": MODEL, "aligner": ALIGNER, "entries": entries}


def exchange_backup(first):
    first = Path(first)
    return first.with_name(".bulk-exchange-" + first.name)


def recover_directory_exchange(first, second):
    """Finish an interrupted three-rename exchange before inspecting its marker."""
    first, second = Path(first), Path(second)
    backup = exchange_backup(first)
    if not backup.exists():
        return
    if not first.exists() and second.exists():
        os.rename(second, first)
        fsync_directory(first.parent)
        fsync_directory(second.parent)
    if first.exists() and not second.exists():
        os.rename(backup, second)
        fsync_directory(backup.parent)
        fsync_directory(second.parent)
        return
    raise ReprocessError("inconsistent_directory_exchange")


def exchange_via_renames(first, second):
    """Recoverable fallback for mounts without RENAME_EXCHANGE support.

    There is a brief directory handover between the first two renames. Never
    delete the old directory in this step; retain it until the DB commits.
    """
    first, second = Path(first), Path(second)
    backup = exchange_backup(first)
    if backup.exists() or first.stat().st_dev != second.stat().st_dev:
        raise ReprocessError("unsafe_directory_exchange")
    os.rename(first, backup)
    fsync_directory(first.parent)
    try:
        os.rename(second, first)
    except Exception:
        os.rename(backup, first)
        fsync_directory(first.parent)
        raise
    fsync_directory(first.parent)
    fsync_directory(second.parent)
    os.rename(backup, second)
    fsync_directory(backup.parent)
    fsync_directory(second.parent)


def exchange_directories(first, second):
    """Prefer one atomic swap; use recoverable renames on unsupported mounts."""
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        return exchange_via_renames(first, second)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(first), -100, os.fsencode(second), 2):
        if ctypes.get_errno() in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP}:
            return exchange_via_renames(first, second)
        raise ReprocessError("atomic_directory_exchange_failed")
    for parent in {Path(first).parent, Path(second).parent}:
        fsync_directory(parent)


def prepare_metadata(original, staged, job, rendered):
    metadata = read_json(staged / "metadata.json", {})
    old_metadata = read_json(original / "metadata.json", {})
    for key in PRESERVED_METADATA:
        if key in old_metadata:
            metadata[key] = old_metadata[key]
        elif key in job:
            metadata[key] = job[key]
    metadata["anchor"] = deepcopy(job["anchor"])
    metadata["options"] = options_for(job)
    metadata["subtitle_reprocessed_at"] = time.time()
    (staged / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    # Subtitle repair must not redesign approved covers or rewrite copy.
    for name in ("cover.jpg", "frame.jpg", "titles.txt"):
        if (original / name).is_file():
            shutil.copy2(original / name, staged / name)
    report = read_json(staged / "subtitle-review.json", {})
    summary = {key: report[key] for key in ("version", "status", "applied", "model", "aligner",
        "timing_basis", "cache_hit", "duration", "reason", "warning", "speaker_attribution") if key in report}
    summary.update(word_count=len(report.get("words", [])), caption_count=len(report.get("reviewed_subtitles", [])))
    fields = {"subtitle_review": summary, "subtitle_reprocessed_at": metadata["subtitle_reprocessed_at"],
              "warning": metadata.get("warning"), "duration": metadata["duration"],
              "options": {**job.get("options", {}), "captions": True},
              "chat_focus": metadata.get("chat_focus"), "render_stage": "完成", "progress": 100, "error": None}
    names = ["clip.mp4", "subtitles.srt", "metadata.json", "titles.txt", "chat-focus.json", "subtitle-review.json"]
    if (staged / "cover.jpg").is_file():
        names.append("cover.jpg")
    with zipfile.ZipFile(staged / "package.zip", "w", zipfile.ZIP_STORED) as package:
        for name in names:
            package.write(staged / name, name)
    return fields


@contextmanager
def preserve_visual_edits(original, staged, job, source_size=None):
    """Reuse the original pixel-verified magnifier while replacing only captions.

    This adapter is scoped to the standalone batch process. The production
    workbench keeps its normal OCR behavior for newly selected clips.
    """
    import clipper.chat_focus as focus
    original_function = focus.align_chat_focus
    metadata = read_json(original / "metadata.json", {})
    report = original_focus_report(original, job)
    original_anchor = metadata.get("anchor", {})
    matched = (all(original_anchor.get(key) == job["anchor"].get(key)
                       for key in ("session_id", "start", "end", "focus_event_ids", "focus_region")))
    if not matched:
        raise ReprocessError("original_visual_edit_source_changed")
    if report["plans"] and source_size != (report.get("source_width"), report.get("source_height")):
        raise ReprocessError("original_visual_edit_dimensions_changed")
    async def reuse_or_align(ffmpeg, concat, offset, duration, events, anchor, folder):
        for name in ("chat-focus-ocr.json", "chat-focus-frames"):
            source, target = original / name, Path(folder) / name
            if source.is_file():
                shutil.copy2(source, target)
            elif source.is_dir():
                shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        return deepcopy(report)
    focus.align_chat_focus = reuse_or_align
    try:
        yield matched
    finally:
        focus.align_chat_focus = original_function


def write_job_fields(data_dir, job_id, fields, expected_anchor, expected_options=None):
    con = sqlite3.connect(data_dir / "clipper.sqlite", timeout=30)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        current = json.loads(row[0]) if row else None
        if not current or current.get("status") != "complete" or current.get("anchor") != expected_anchor:
            raise ReprocessError("original_job_changed")
        if expected_options is not None and current.get("options", {}) not in (expected_options, fields.get("options")):
            raise ReprocessError("original_job_options_changed")
        # Merge into the latest row, keeping URL, lineage, publication IDs,
        # metadata and all unrelated fields intact.
        current.update(fields)
        con.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(current, ensure_ascii=False), job_id))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def commit_replacement(data_dir, run_dir, entry, job, fields=None):
    original = data_dir / "exports" / job["id"]
    staged = run_dir / "staging" / "exports" / job["id"]
    receipt_path = run_dir / "receipts" / (job["id"] + ".json")
    receipt = read_json(receipt_path, {})
    if fields is not None:
        receipt = {"id": job["id"], "operation_id": uuid.uuid4().hex, "status": "prepared",
                   "fields": fields, "anchor": job["anchor"], "expected_options": job.get("options", {}),
                   "prepared_at": time.time()}
        atomic_json(staged / MARKER, {"operation_id": receipt["operation_id"], "id": job["id"]})
        atomic_json(receipt_path, receipt)
    if not receipt.get("operation_id"):
        raise ReprocessError("replacement_receipt_missing")
    recover_directory_exchange(original, staged)
    swapped = read_json(original / MARKER, {}).get("operation_id") == receipt["operation_id"]
    if receipt.get("status") == "committed" and swapped:
        if staged.exists():
            shutil.rmtree(staged)
        entry.update(status="complete", completed_at=receipt["committed_at"])
        return
    if not swapped:
        if read_json(staged / MARKER, {}).get("operation_id") != receipt["operation_id"]:
            raise ReprocessError("replacement_marker_mismatch")
        exchange_directories(original, staged)
    try:
        write_job_fields(data_dir, job["id"], receipt["fields"], receipt["anchor"], receipt.get("expected_options"))
    except Exception:
        exchange_directories(original, staged)
        receipt["status"] = "rolled_back"
        atomic_json(receipt_path, receipt)
        raise
    receipt.update(status="committed", committed_at=time.time())
    atomic_json(receipt_path, receipt)
    # The exchanged staging directory now holds only the previous artifacts.
    # Remove it after the replacement and journal row have both committed.
    if staged.exists():
        shutil.rmtree(staged)
    entry.update(status="complete", completed_at=receipt["committed_at"])


def status_summary(plan):
    counts = dict(Counter(item["status"] for item in plan["entries"]))
    return {"counts": counts, "total": len(plan["entries"]), "updated_at": time.time(),
            "current": plan.get("current"), "model": MODEL}


async def wait_audio_idle(settings, timeout=900):
    """Avoid starting a historical review while an automatic export owns ASR."""
    config = settings.audio_config()
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(config["base_url"].rstrip("/") + "/health",
                    headers={"Authorization": "Bearer " + config["api_key"]})
                if response.status_code == 200:
                    health = response.json()
                    if health.get("offline_model") == MODEL and not health.get("offline_busy"):
                        return
            except (httpx.HTTPError, ValueError, KeyError):
                pass
            await asyncio.sleep(2)
    raise ReprocessError("audio_service_not_ready")


async def run_batch(settings, run_dir, plan, *, only=None, retry_failed=False):
    data_dir = settings.data_dir
    staging_data = run_dir / "staging"
    index = {}
    data = snapshot(data_dir)
    jobs = {job["id"]: job for job in data["jobs"]}
    sources = {source["id"]: source for source in data["sessions"]}
    for job in data["jobs"]:
        folder = data_dir / "exports" / job["id"]
        if not valid_review(folder, job):
            continue
        adapter = ReadOnlyRenderStore(data_dir, job)
        try:
            segments = adapter.segments(job["session_id"], job["anchor"]["start"], job["anchor"]["end"])
            signature = render_signature(job, sources[job["session_id"]], segments, original_focus_report(folder, job))
            index[signature] = job["id"]
        except (OSError, KeyError, TypeError, ReprocessError):
            pass
        finally:
            adapter.db.close()
    def persist():
        atomic_json(run_dir / "plan.json", plan)
        atomic_json(run_dir / "status.json", status_summary(plan))
    for entry in plan["entries"]:
        if only and entry["id"] not in only:
            continue
        if entry["status"] in {"complete", "already_reviewed", "excluded", "blocked"}:
            continue
        if entry["status"] == "failed" and not retry_failed:
            continue
        job = jobs[entry["id"]]
        plan["current"] = job["id"]
        staged = staging_data / "exports" / job["id"]
        original = data_dir / "exports" / job["id"]
        receipt = read_json(run_dir / "receipts" / (job["id"] + ".json"), {})
        try:
            if receipt.get("status") in {"prepared", "committed"}:
                commit_replacement(data_dir, run_dir, entry, job)
                persist()
                continue
            if sources[job["session_id"]].get("auto_publish"):
                raise ReprocessError("source_auto_publish_enabled")
            adapter = ReadOnlyRenderStore(data_dir, job)
            try:
                segments = adapter.segments(job["session_id"], job["anchor"]["start"], job["anchor"]["end"])
                validate_range(job["anchor"], segments)
                current_source = adapter.session(job["session_id"])
                focus_report = original_focus_report(original, job)
                entry["signature"] = render_signature(job, current_source, segments, focus_report)
            finally:
                adapter.db.close()
            if staged.exists():
                # A valid per-audio hash inside this cache is checked again by
                # review_subtitles; interrupted encoding need not call ASR twice.
                cache = staged / "subtitle-review-cache.json"
                resume_cache = run_dir / "resume-cache" / (job["id"] + ".json")
                if cache.is_file():
                    resume_cache.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cache, resume_cache)
                shutil.rmtree(staged)
            entry.update(status="rendering", started_at=time.time())
            entry.pop("reason", None)
            persist()
            donor = index.get(entry["signature"])
            if donor:
                await asyncio.to_thread(shutil.copytree, data_dir / "exports" / donor, staged, symlinks=True)
                rendered = jobs[donor]
                entry["reused_job"] = donor
            else:
                await wait_audio_idle(settings)
                adapter = ReadOnlyRenderStore(data_dir, job)
                try:
                    resume_cache = run_dir / "resume-cache" / (job["id"] + ".json")
                    if resume_cache.is_file():
                        staged.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(resume_cache, staged / "subtitle-review-cache.json")
                    # Keep the approved cover/frame; avoid regenerating them
                    # only to replace them again in prepare_metadata.
                    adapter.current_job["options"]["cover"] = False
                    source_size = None
                    if focus_report["plans"]:
                        info = await probe(settings, segments[0]["path"])
                        source_size = (info["width"], info["height"])
                    with preserve_visual_edits(original, staged, job, source_size) as preserved:
                        entry["preserved_chat_focus"] = preserved
                        await Renderer(replace(settings, data_dir=staging_data), adapter).run(job["id"])
                    rendered = adapter.job(job["id"])
                finally:
                    adapter.db.close()
                if rendered.get("status") != "complete":
                    raise ReprocessError("render_failed")
            if not valid_review(staged, job):
                report = read_json(staged / "subtitle-review.json", {})
                entry["review_status"] = report.get("status", "missing")
                entry["review_reason"] = report.get("reason")
                raise ReprocessError("strong_review_not_applied")
            info = await probe(settings, staged / "clip.mp4")
            if abs(info["duration"] - job["duration"]) > max(.2, job["duration"] * .002):
                raise ReprocessError("replacement_duration_mismatch")
            fields = prepare_metadata(original, staged, job, rendered)
            commit_replacement(data_dir, run_dir, entry, job, fields)
            (run_dir / "resume-cache" / (job["id"] + ".json")).unlink(missing_ok=True)
            index[entry["signature"]] = job["id"]
            jobs[job["id"]].update(fields)
            print(json.dumps({"id": job["id"], "status": "complete", "reused": bool(donor),
                              "counts": status_summary(plan)["counts"]}), flush=True)
        except Exception as exc:
            entry.update(status="failed", reason=str(exc) if isinstance(exc, ReprocessError) else "replacement_failed")
            print(json.dumps({"id": job["id"], "status": "failed", "reason": entry["reason"]}), flush=True)
        persist()
    plan["current"] = None
    persist()
    return status_summary(plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inventory", "run", "status"])
    parser.add_argument("--run-dir", type=Path, default=ROOT / "data/bulk-subtitle-review")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    if args.only and any(not JOB_ID.fullmatch(value) for value in args.only):
        parser.error("invalid job ID")
    args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == "status":
        print(json.dumps(read_json(args.run_dir / "status.json", {})))
        return 0
    with (args.run_dir / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"error": "batch_already_running"}))
            return 1
        plan_path = args.run_dir / "plan.json"
        plan = read_json(plan_path)
        if args.command == "inventory":
            if plan:
                print(json.dumps(status_summary(plan)))
                return 0
            plan = inventory(args.data_dir)
            atomic_json(plan_path, plan)
            atomic_json(args.run_dir / "status.json", status_summary(plan))
            print(json.dumps(status_summary(plan)))
            return 0
        if not plan:
            raise ReprocessError("inventory_required")
        settings = replace(Settings.from_env(), data_dir=args.data_dir.resolve())
        result = asyncio.run(run_batch(settings, args.run_dir, plan,
                                      only=set(args.only) if args.only else None, retry_failed=args.retry_failed))
        print(json.dumps(result))
        return int(bool(result["counts"].get("failed") or result["counts"].get("blocked")))


if __name__ == "__main__":
    raise SystemExit(main())
