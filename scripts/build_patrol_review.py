#!/usr/bin/env python3
"""Build a small, private static review snapshot; no network or app imports.

Only a display-field allowlist is exported. Raw reports, JSONL logs, account
identifiers and full observation streams are never copied into the output.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "scripts/patrol_review_assets"
LOGO_SOURCE = ROOT / "static/brand/ai-tongdan-logo.png"
DEFAULT_OUTPUT = ROOT / "static/patrol-review"
TOTAL_KEYS = ("visits", "rooms", "sent", "manual_sent", "sending_policy_mismatches", "candidate_total", "candidate_batches", "questions",
              "answers_complete", "planned_questions", "manual_questions", "chorus_windows",
              "simulation_complete", "simulation_skipped", "simulated_candidates", "recording_bytes")
_PRIVATE_VALUE = re.compile(
    r"(?i)(?:SESSDATA|bili_jct|cookie|authorization|api[_ -]?key|access[_ -]?token|token|password|passwd)"
    r"\s*[:：=]\s*[^\s，。；;]+|\bgh[pousr]_[A-Za-z0-9]+|\bsk-[A-Za-z0-9_-]{12,}"
    r"|(?:uid|uname|username|user[_ -]?id|session[_ -]?id)\s*[:：=]\s*[^\s，。；;]+"
)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _text(value, limit=None):
    if not isinstance(value, str):
        return ""
    # Keep complete prose, while not republishing an accidentally quoted secret.
    text = _PRIVATE_VALUE.sub("[已隐去敏感字段]", value)
    text = text.encode("utf-8", "replace").decode("utf-8")
    return text[:limit] if limit is not None else text


def _number(value):
    if type(value) in (int, float) and math.isfinite(value):
        return value
    return None


def _count(value):
    number = _number(value)
    return max(0, int(number)) if number is not None else 0


def _room_id(value):
    return str(value) if type(value) in (str, int) and re.fullmatch(r"[1-9]\d*", str(value)) else ""


def _epoch(value):
    number = _number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date.replace(tzinfo=timezone.utc).timestamp() if date.tzinfo is None else date.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def _iso(value):
    when = _epoch(value)
    if when is None:
        return ""
    try:
        return datetime.fromtimestamp(when, timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""


def _seconds(value):
    number = _number(value)
    return number if number is not None and 0 <= number < 1_000_000_000 else None


def _load(path, *, required=False):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if required:
            raise ValueError(f"Cannot read review input: {path.name}") from exc
        return {}
    if not isinstance(value, dict):
        if required:
            raise ValueError(f"Review input must be an object: {path.name}")
        return {}
    return value


def _safe_url(value, output):
    """Only existing media below this export, never URLs with credentials."""
    if not isinstance(value, str) or not value or re.search(r"[\\\x00-\x20]", value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        return None
    decoded = unquote(parts.path)
    path = PurePosixPath(decoded)
    if (not path.parts or path.parts[0] != "media" or path.is_absolute()
            or ".." in path.parts or "\\" in decoded or re.search(r"[\x00-\x1f]", decoded)):
        return None
    target = output.joinpath(*path.parts)
    try:
        target.resolve().relative_to(output.resolve())
    except (ValueError, OSError):
        return None
    return value if target.is_file() else None


def _name(value):
    return _text(str(value).replace("\\", "/").rsplit("/", 1)[-1], 140) if isinstance(value, str) else ""


def _chorus(value):
    value = _dict(value)
    items = []
    for row in _list(value.get("items")):
        row = _dict(row)
        text = _text(row.get("text"))
        if not text:
            continue
        item = {"text": text, "count": _count(row.get("count"))}
        for key in ("total", "window_seconds"):
            if _number(row.get(key)) is not None:
                item[key] = _count(row[key])
        items.append(item)
    return {"updated_at": _epoch(value.get("updated_at")), "items": items,
            "recorded": value.get("recorded") is True}


def _round(row):
    row = _dict(row)
    return {"round": _count(row.get("round")), "time": _epoch(row.get("time")),
            "target_at": _epoch(row.get("target_at")), "status": _text(row.get("status"), 40),
            "reason": _text(row.get("reason"), 400), "snapshot_time": _seconds(row.get("snapshot_time")),
            "candidates": [_text(text) for text in _list(row.get("candidates"))[:5] if isinstance(text, str)],
            "candidate_generated_at": _epoch(row.get("candidate_generated_at")),
            "source_candidate_count": _number(row.get("source_candidate_count")),
            "candidate_selection": _text(row.get("candidate_selection"), 100),
            "chorus_capture_note": _text(row.get("chorus_capture_note"), 400),
            "chorus": _chorus(row.get("chorus"))}


def _question(row, index):
    row = _dict(row)
    return {"id": f"q{index}", "question": _text(row.get("question")), "answer": _text(row.get("answer")),
            "status": _text(row.get("status"), 40), "kind": _text(row.get("kind"), 60),
            "origin": _text(row.get("origin"), 40), "requested_at": _epoch(row.get("requested_at")),
            "answered_at": _epoch(row.get("answered_at")), "snapshot_time": _seconds(row.get("snapshot_time")),
            "evidence_times": [n for value in _list(row.get("evidence_times")) if (n := _seconds(value)) is not None],
            "replay_times": [n for value in _list(row.get("replay_times")) if (n := _seconds(value)) is not None],
            "error": _text(row.get("error"), 400)}


def _observations(contents):
    rows = []
    for source in _list(contents):
        row = _dict(source)
        if row.get("type") != "observation":
            continue
        # Source-excerpt context rows contain audience labels, not visual
        # conclusions. Keep only the short first paragraph of visual records.
        text = _text(row.get("text")).split("\n", 1)[0]
        if not text or text.startswith("音频/弹幕来源更新"):
            continue
        rows.append({"time": _seconds(row.get("offset")), "text": text[:220], "kind": "observation"})
    if len(rows) > 5:
        rows = [rows[index * (len(rows) - 1) // 4] for index in range(5)]
    return rows


def _media(raw, visit, output):
    raw = _dict(raw)
    preview = _dict(raw.get("preview"))
    preview_url = _safe_url(preview.get("url"), output)
    selected_preview = ({"url": preview_url, "source_file": _name(preview.get("source_file")),
                         "start_seconds": _seconds(preview.get("start_seconds")),
                         "duration_seconds": _seconds(preview.get("duration_seconds")),
                         "timestamp_basis": _text(preview.get("timestamp_basis"), 180)} if preview_url else None)
    originals = {_name(row.get("file")): row for row in _list(visit.get("recordings")) if isinstance(row, dict)}
    exported = {_name(row.get("name", row.get("file"))): row for row in _list(raw.get("recordings")) if isinstance(row, dict)}
    recordings = []
    for name in dict.fromkeys([*originals, *exported]):
        if not name:
            continue
        original, record = _dict(originals.get(name)), _dict(exported.get(name))
        recordings.append({"name": name, "url": _safe_url(record.get("url"), output),
            "bytes": _count(record.get("bytes", original.get("bytes"))),
            "duration_seconds": _seconds(record.get("duration_seconds")),
            "started_at": _epoch(record.get("started_at", original.get("started_at"))),
            "ended_at": _epoch(record.get("ended_at", original.get("ended_at")))})
    return {"thumbnail": _safe_url(raw.get("thumbnail"), output),
            "preview": selected_preview, "recordings": recordings}


def _extras(visit):
    """Complete lazy text archives, with no identities or raw lifecycle data."""
    chorus = [{"time": _epoch(row.get("time")), **_chorus(row)}
              for row in _list(visit.get("chorus_windows")) if isinstance(row, dict)]
    candidates = [{"time": _epoch(row.get("time")), "generated_at": _epoch(row.get("generated_at")),
                   "items": [_text(text) for text in _list(row.get("items")) if isinstance(text, str)],
                   "status": _text(row.get("status"), 40)}
                  for row in _list(visit.get("candidates")) if isinstance(row, dict)]
    return {"chorus_windows": chorus, "candidate_batches": candidates}


def review_data(report, run_dir, output, media_manifest, *, built_at=None):
    """Project the frozen report; newer visit files only supply missing metadata."""
    raw_snapshot = report.get("generated_at")
    snapshot_at = raw_snapshot if isinstance(raw_snapshot, str) and _epoch(raw_snapshot) is not None else _iso(raw_snapshot)
    snapshot_epoch = _epoch(snapshot_at)
    config = {key: _dict(report.get("config")).get(key) is True
              for key in ("candidate_only", "allow_manual_send", "send_authorized")}
    visits = []
    for index, value in enumerate(_list(report.get("visits")), 1):
        source = _dict(value)
        vid = _text(source.get("visit_id"))
        if len(vid) > 128 or not re.fullmatch(r"\d+-\d+", vid):
            raise ValueError("Invalid review visit identifier")
        metadata = _load(run_dir / "visits" / vid / "visit.json")
        room = _dict(source.get("room"))
        # requested_room_id is set for every visit (including following and
        # area discovery); only its recorded selection source proves that this
        # particular visit was explicitly requested.
        selection_source = room.get("selection_source", _dict(metadata.get("room")).get("selection_source"))
        canonical = _room_id(room.get("room_id"))
        room_id = _room_id(metadata.get("requested_room_id")) or canonical
        start = _epoch(source.get("started_at", metadata.get("started_at")))
        session_start = _epoch(source.get("session_started_at", metadata.get("session_started_at"))) or start
        ended = _epoch(source.get("ended_at"))
        scheduled = _epoch(source.get("scheduled_end_at", metadata.get("scheduled_end_at")))
        end_for_duration = ended if ended is not None else snapshot_epoch
        duration = max(0, end_for_duration - session_start) if end_for_duration is not None and session_start is not None else None
        rounds = [_round(row) for row in _list(source.get("simulation_rounds")) if isinstance(row, dict)]
        questions = [_question(row, number) for number, row in enumerate(_list(source.get("questions")), 1)
                     if isinstance(row, dict)]
        # Permission belongs to the frozen visit. Never infer it from newer
        # metadata or today's run configuration when exporting older records.
        allow_manual_send = source.get("allow_manual_send") is True
        allow_manual_send_since = _epoch(source.get("allow_manual_send_since"))
        sends = [{"text": _text(row.get("text")), "sent_at": _epoch(row.get("sent_at")),
                  "offset": _seconds(row.get("offset")),
                  "source": row.get("source") if row.get("source") in ("manual", "auto") else "unknown"}
                 for row in _list(source.get("sends")) if isinstance(row, dict)]
        policy_mismatch = config["candidate_only"] and (
            source.get("sending_policy_mismatch") is True
            or _count(source.get("sent_count")) > len(sends)
            or any(not allow_manual_send or row["source"] != "manual"
                   or (allow_manual_send_since is not None
                       and (row["sent_at"] is None or row["sent_at"] < allow_manual_send_since)) for row in sends)
            or any(not allow_manual_send or row.get("source") != "manual"
                   or (allow_manual_send_since is not None
                       and (_epoch(row.get("time")) is None or _epoch(row.get("time")) < allow_manual_send_since))
                   for row in _list(source.get("attempts")) if isinstance(row, dict)))
        counts = {"simulations_complete": sum(row["status"] == "complete" for row in rounds),
            "simulations_skipped": sum(row["status"] == "skipped" for row in rounds),
            "questions": len(questions), "answers_complete": sum(row["status"] == "complete" for row in questions),
            "planned_questions": sum(row["origin"] == "planned" for row in questions),
            "manual_questions": sum(row["origin"] == "manual" for row in questions),
            "candidate_batches": _count(source.get("candidate_batches")),
            "candidate_total": _count(source.get("candidate_total")),
            "chorus_windows": len(_list(source.get("chorus_windows"))),
            "sent": max(len(sends), _count(source.get("sent_count"))),
            "manual_sent": sum(row["source"] == "manual" for row in sends)}
        lag = _dict(source.get("lag"))
        visit = {"id": vid, "room_id": room_id, "canonical_room_id": canonical,
            "is_requested": selection_source in {"requested_first", "requested_queue"},
            "title": _text(room.get("title"), 200), "area": _text(room.get("area"), 100),
            "started_at": start, "session_started_at": session_start, "ended_at": ended,
            "scheduled_end_at": scheduled, "duration_seconds": duration,
            "target_rounds": _count(source.get("simulation_rounds_per_visit", metadata.get("simulation_rounds_per_visit"))),
            "simulation_interval_seconds": _seconds(source.get("simulation_interval_seconds")),
            "status": _text(source.get("status"), 40), "error": _text(source.get("error"), 400),
            "allow_manual_send": allow_manual_send, "allow_manual_send_since": allow_manual_send_since,
            "sending_policy_mismatch": policy_mismatch, "sends": sends,
            "exit_reason": _text(source.get("exit_reason", metadata.get("exit_reason")), 400),
            "counts": counts, "lag": {key: _number(lag.get(key)) for key in ("median", "p95", "max")},
            **_media(_dict(_dict(media_manifest.get("visits")).get(vid)), source, output),
            "rounds": rounds, "questions": questions, "observations": _observations(source.get("contents")),
            "extra_url": f"details/{vid}.js"}
        visits.append(visit)
    source_totals = _dict(report.get("totals"))
    totals = {key: _count(source_totals.get(key)) for key in TOTAL_KEYS}
    # These totals describe the actual exported snapshot, never a newer state
    # file or the number of raw model drafts confused with simulated candidates.
    totals.update(visits=len(visits), rooms=len({v["canonical_room_id"] for v in visits}),
        sent=sum(v["counts"]["sent"] for v in visits),
        manual_sent=sum(v["counts"]["manual_sent"] for v in visits),
        sending_policy_mismatches=sum(v["sending_policy_mismatch"] for v in visits),
        questions=sum(v["counts"]["questions"] for v in visits),
        answers_complete=sum(v["counts"]["answers_complete"] for v in visits),
        planned_questions=sum(v["counts"]["planned_questions"] for v in visits),
        manual_questions=sum(v["counts"]["manual_questions"] for v in visits),
        simulation_complete=sum(v["counts"]["simulations_complete"] for v in visits),
        simulation_skipped=sum(v["counts"]["simulations_skipped"] for v in visits),
        simulated_candidates=sum(len(row["candidates"]) for v in visits for row in v["rounds"] if row["status"] == "complete"),
        recording_bytes=sum(_count(row.get("bytes")) for v in _list(report.get("visits"))
                            for row in _list(_dict(v).get("recordings")) if isinstance(row, dict)))
    return {"schema_version": 1, "run_id": _text(report.get("run_id"), 140),
            "built_at": _iso(built_at or datetime.now(timezone.utc).timestamp()),
            "snapshot_at": snapshot_at or "", "capture_started_at": _iso(report.get("started_at")),
            "deadline": _iso(report.get("deadline")), "status": _text(report.get("status"), 40),
            "timezone": "Asia/Shanghai", "demo_url": "/", "snapshot": True,
            "config": config, "totals": totals, "visits": visits}


def script_json(value):
    """JSON safe inside a script element, including hostile closing tags."""
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for character, escape in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
                              ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        data = data.replace(character, escape)
    return data


def _atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_review(run_dir, output=DEFAULT_OUTPUT, *, assets_dir=None, logo_source=None):
    run_dir, output = Path(run_dir).resolve(), Path(output).resolve()
    if output == run_dir:
        raise ValueError("Review output must differ from the source run directory")
    assets_dir = Path(assets_dir) if assets_dir is not None else ASSET_DIR
    logo_source = Path(logo_source) if logo_source is not None else LOGO_SOURCE
    report = _load(run_dir / "report.json", required=True)
    media = _load(output / "media-manifest.json")
    data = review_data(report, run_dir, output, media)
    template = (assets_dir / "index.html").read_text(encoding="utf-8")
    if template.count("{{REVIEW_DATA}}") != 1:
        raise ValueError("Review HTML must contain exactly one {{REVIEW_DATA}} placeholder")
    files = {"review.css": (assets_dir / "review.css").read_bytes(),
             "review.js": (assets_dir / "review.js").read_bytes(),
             "assets/logo.png": logo_source.read_bytes()}
    for source in _list(report.get("visits")):
        if not isinstance(source, dict):
            continue
        vid = source["visit_id"]  # Validated before any output is written.
        extra = ("window.PATROL_EXTRA=window.PATROL_EXTRA||{};window.PATROL_EXTRA["
                 + script_json(vid) + "]=" + script_json(_extras(source)) + ";\n")
        files[f"details/{vid}.js"] = extra.encode("utf-8")
    html = template.replace("{{REVIEW_DATA}}", script_json(data)).encode("utf-8")
    for name, content in files.items():
        _atomic_write(output / name, content)
    # Publish the new embedded data only after its required assets are ready.
    _atomic_write(output / "index.html", html)
    return {"output": str(output / "index.html"), "snapshot_at": data["snapshot_at"],
            "built_at": data["built_at"], "snapshot": True, "totals": data["totals"],
            "html_bytes": len(html), "media_visits": sum(bool(v["thumbnail"] or v["preview"]) for v in data["visits"])}


def main():
    parser = argparse.ArgumentParser(description="生成 AI 同担的私有静态巡回复盘页（离线快照）")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    try:
        metadata = build_review(args.run_dir, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"构建失败：{type(exc).__name__}；请检查输入和资源文件。\n")
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    main()
