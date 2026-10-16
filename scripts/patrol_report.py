#!/usr/bin/env python3
"""Build an offline, escaped review of a local Bilibili patrol run.

No network, account credentials, or application imports are used.  The daemon may
call ``render_report(run_dir)`` while it is running.  Event logs may be stored
under each visit or once at the run root (partitioned by session_id).
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import math
import os
import statistics
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
CONTEXT_SECONDS = 45
TYPE_LABELS = {
    "observation": "观察与变化", "audio_segment": "音频转写", "danmaku": "现场弹幕",
    "danmaku_candidates": "候选弹幕", "sent_danmaku": "已发送弹幕", "danmaku_auto": "自动发送",
}
STATUS_LABELS = {
    "running": "运行中", "completed": "已完成", "complete": "已完成", "finished": "已结束",
    "stopped": "已停止", "interrupted": "已中断", "failed": "失败", "error": "异常",
    "skipped": "已跳过", "pending": "等待中", "sent": "已发送", "starting": "正在连接",
    "recording": "采集中", "observing": "观察中", "idle": "空闲", "cancelled": "已取消",
    "accepted": "平台已受理", "not_sent": "未发送", "unknown": "结果未确认",
    "queued": "排队中", "planning": "规划中", "answering": "回答中", "requested": "已请求",
    "waiting": "等待中", "ready": "已就绪", "recorded": "已记录",
    "submission_uncertain": "提问提交结果未确认",
}
QUESTION_ORIGIN_LABELS = {"planned": "规划提问", "manual": "手动提问", "manual_or_historical": "手动/历史提问"}


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _epoch(value: Any) -> float | None:
    number = _number(value)
    if number is not None:
        return number / 1000 if number > 100_000_000_000 else number
    if isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=UTC)
            return instant.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


def _iso(value: Any) -> str:
    epoch = _epoch(value)
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


def _date(value: Any, *, short: bool = False) -> str:
    epoch = _epoch(value)
    if epoch is None:
        return "时间未记录"
    try:
        instant = datetime.fromtimestamp(epoch, CST)
        return instant.strftime("%H:%M:%S" if short else "%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "时间无效"


def _offset(value: Any) -> str:
    seconds = _number(value)
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "；".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("description") or value.get("content") or "")
    return str(value)


def _load_json(path: Path, warnings: list[str]) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        warnings.append(f"暂时无法读取 {path.name}，下次刷新会重试。")
        return {}


def _read_events(path: Path, warnings: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    bad_lines = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and isinstance(event.get("data"), (dict, list, str)):
                        events.append(event)
                except ValueError:
                    bad_lines += 1
    except FileNotFoundError:
        return []
    except OSError:
        warnings.append(f"暂时无法读取 {path.name}。")
    if bad_lines:
        warnings.append(f"{path.name} 有 {bad_lines} 行尚未完整写入或格式不正确；已略过。")
    return events


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _href(root: Path, path: Path) -> str:
    if not _inside(root, path):
        return ""
    return quote(path.resolve().relative_to(root.resolve()).as_posix(), safe="/")


def _event_content(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Project selected public content, omitting speaker identities and raw metadata."""
    kind = str(event.get("type", ""))
    data = event.get("data", {})
    data = data if isinstance(data, dict) else {"text": _text(data)}
    items = data.get("items") if kind == "danmaku" else None
    values = items if isinstance(items, list) else [data]
    output = []
    for item in values:
        if not isinstance(item, dict):
            item = {"text": _text(item)}
        content = _text(item)
        if kind == "observation":
            details = []
            for name, label in (("visual_delta", "画面变化"), ("audio_delta", "声音变化"),
                                ("danmaku_delta", "弹幕变化")):
                detail = _text(item.get(name))
                if detail:
                    details.append(f"{label}：{detail}")
            if not details and item.get("delta"):
                details.append(f"变化：{_text(item['delta'])}")
            content = "\n".join(filter(None, [content, *details]))
        if not content:
            continue
        offset = _number(item.get("timestamp", item.get("start", item.get("t"))))
        # UNIX timestamps must not be presented as hours since joining the room.
        if offset is not None and offset > 1_000_000_000:
            offset = None
        output.append({"type": kind, "text": content, "time": _epoch(event.get("time")),
                       "offset": offset, "end_offset": _number(item.get("end"))})
    return output


def _candidate_event(event: dict[str, Any]) -> dict[str, Any] | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    items = data.get("items", [])
    if not isinstance(items, list):
        return None
    texts = [_text(item) for item in items]
    texts = [text for text in texts if text]
    if not texts:
        return None
    return {"time": _epoch(event.get("time")), "items": texts, "status": str(data.get("status", "")),
            "generated_at": _epoch(data.get("generated_at"))}


def _content_seconds(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and 0 <= number < 1_000_000_000 else None


def _collect_questions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join archived lifecycle events by ID, without truncating actual answers."""
    questions: dict[str, dict[str, Any]] = {}
    seen: set[tuple] = set()
    stages = {"question_requested": 0, "question_submission_uncertain": 0,
              "question_started": 1, "plan": 2, "answer_done": 3, "error": 3}
    for event in events:
        kind, data = event.get("type"), event.get("data")
        if kind not in stages or not isinstance(data, dict):
            continue
        qid = str(data.get("question_id") or "")
        if kind == "error" and (data.get("source") != "question" or not qid):
            continue
        when = _epoch(event.get("time"))
        key = qid or f"unlinked:{kind}:{when}:{_text(data.get('text'))}"
        fingerprint = (key, kind, json.dumps(data, sort_keys=True, ensure_ascii=False))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        question = questions.setdefault(key, {
            "question_id": qid, "question": "", "answer": "", "status": "unknown", "kind": "",
            "origin": "manual_or_historical",
            "requested_at": None, "started_at": None, "answered_at": None,
            "snapshot_time": None, "plan": "", "replay_times": [], "evidence_times": [],
            "error": "", "events": [], "_stage": -1,
        })
        # The controller acknowledgement can arrive after started/answer events.
        # An acknowledged planned ID always wins over provisional SSE inference.
        if kind == "question_requested" or data.get("origin") == "planned":
            question["origin"] = "planned"
        elif question["origin"] != "planned" and (kind == "question_started" or data.get("origin") == "manual"):
            question["origin"] = "manual"
        if data.get("kind") or data.get("type"):
            question["kind"] = _text(data.get("kind") or data.get("type"))
        snapshot = _content_seconds(data.get("snapshot_time"))
        if snapshot is not None:
            question["snapshot_time"] = snapshot
        if kind in {"question_requested", "question_started", "question_submission_uncertain"}:
            question["question"] = _text(data.get("question") or data.get("text")) or question["question"]
            time_key = "started_at" if kind == "question_started" else "requested_at"
            question[time_key] = question[time_key] if question[time_key] is not None else when
        elif kind == "plan":
            question["plan"] = _text(data.get("plan"))
            values = data.get("replay_times", [])
            if isinstance(values, list):
                question["replay_times"] = [value for item in values if (value := _content_seconds(item)) is not None]
        elif kind == "answer_done":
            question["answer"] = _text(data.get("content") or data.get("text"))
            question["answered_at"] = question["answered_at"] if question["answered_at"] is not None else when
        if kind in {"plan", "answer_done"}:
            evidence = data.get("evidence", [])
            if isinstance(evidence, list):
                for item in evidence:
                    timestamp = _content_seconds(item.get("timestamp")) if isinstance(item, dict) else None
                    if timestamp is not None and timestamp not in question["evidence_times"]:
                        question["evidence_times"].append(timestamp)
        if kind == "error" or data.get("error") or data.get("error_type"):
            question["error"] = _text(data.get("error") or data.get("message") or data.get("error_type"))
        default = {"question_requested": "requested", "question_started": "queued", "plan": "planning",
                   "question_submission_uncertain": "submission_uncertain",
                   "answer_done": "complete" if question["answer"] else "unknown", "error": "error"}[kind]
        status = str(data.get("status") or default)
        if kind in {"question_requested", "question_started"} and status in {"complete", "completed"}:
            # A persisted user message can be marked complete before any answer.
            status = default
        if stages[kind] >= question["_stage"]:
            question["status"], question["_stage"] = status, stages[kind]
        question["events"].append({"type": kind, "time": when, "status": status})
    for question in questions.values():
        question.pop("_stage")
    return list(questions.values())


def _chorus_payload(data: Any) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    items = data.get("items", [])
    projected = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not _text(item):
            continue
        projected.append({"text": _text(item), "count": _number(item.get("count")),
                          "total": _number(item.get("total")),
                          "window_seconds": _number(item.get("window_seconds")),
                          "source_time": _content_seconds(item.get("source_time")),
                          "kind": _text(item.get("kind"))})
    return {"updated_at": _epoch(data.get("updated_at")), "items": projected,
            "recorded": isinstance(data.get("items"), list)}


def _collect_choruses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows, seen = [], set()
    for event in events:
        if event.get("type") != "danmaku_chorus" or not isinstance(event.get("data"), dict):
            continue
        window = _chorus_payload(event["data"])
        # Retain distinct windows (including empty ones); merge duplicate archives.
        key = (window["updated_at"] if window["updated_at"] is not None else _epoch(event.get("time")),
               json.dumps(window["items"], sort_keys=True, ensure_ascii=False))
        if key in seen:
            continue
        seen.add(key)
        windows.append({"time": _epoch(event.get("time")), **window})
    return windows


def _collect_simulations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rounds: dict[tuple, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "simulation_round" or not isinstance(event.get("data"), dict):
            continue
        if isinstance(event["data"].get("data"), dict) and "round" in event["data"]:
            event = event["data"]  # emit() can wrap the dedicated archive envelope.
        data = event["data"]
        candidate_data = data.get("candidates", {})
        candidate_data = candidate_data if isinstance(candidate_data, dict) else {}
        candidate = _candidate_event({"time": event.get("time"), "data": candidate_data})
        number = _number(event.get("round", data.get("round")))
        when = _epoch(event.get("time"))
        key = (str(event.get("session_id", "")), number if number is not None else when)
        rounds[key] = {"round": number, "time": when, "target_at": _epoch(event.get("target_at", data.get("target_at"))),
                       "status": str(data.get("status") or ("recorded" if candidate else "waiting")),
                       "reason": _text(data.get("reason")),
                       "snapshot_time": _content_seconds(data.get("snapshot_time", data.get("source_time"))),
                       "candidates": candidate["items"] if candidate else [],
                       "candidate_status": str(candidate_data.get("status", "")),
                       "candidate_generated_at": _epoch(candidate_data.get("generated_at")),
                       "source_candidate_count": _number(data.get("source_candidate_count")),
                       "candidate_selection": _text(data.get("candidate_selection")),
                       "chorus_capture_note": _text(data.get("chorus_capture_note")),
                       "chorus": _chorus_payload(data.get("chorus"))}
    return list(rounds.values())


def _collect_visit(visit: dict[str, Any], events: list[dict[str, Any]], *,
                   default_simulation_rounds: float | None = None) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    seen_content: set[tuple] = set()
    sends: dict[tuple, dict[str, Any]] = {}
    attempts: dict[tuple, dict[str, Any]] = {}
    accepted_fallbacks: list[tuple[dict[str, Any], Any]] = []
    candidates: list[dict[str, Any]] = []
    candidate_keys: set[tuple] = set()
    lag_values = [_number(value) for value in visit.get("lag_samples", []) if _number(value) is not None]

    def collect_sent(item: dict[str, Any], event_time: Any) -> None:
        text = _text(item.get("text"))
        when = _epoch(item.get("sent_at"))
        if not text or when is None:
            return
        key = (round(when, 3), text)
        previous = sends.get(key, {})
        offset = _number(item.get("timestamp"))
        sends[key] = {"text": text, "sent_at": when,
                      "offset": offset if offset is not None else previous.get("offset"),
                      "source": str(item.get("source", item.get("candidate_source", previous.get("source", "")))),
                      "recorded_at": _epoch(event_time)}

    for event in events:
        kind = event.get("type")
        data = event.get("data", {})
        if kind in {"observation", "audio_segment", "danmaku"}:
            for content in _event_content(event):
                key = (kind, content["offset"] if content["offset"] is not None else content["time"], content["text"])
                if key not in seen_content:
                    seen_content.add(key)
                    contents.append(content)
            if not visit.get("lag_samples") and kind == "observation" and isinstance(data, dict):
                value = _number(data.get("completion_lag"))
                if value is not None and value >= 0:
                    lag_values.append(value)
        elif kind == "sent_danmaku" and isinstance(data, dict):
            for item in data.get("items", []):
                if isinstance(item, dict):
                    collect_sent(item, event.get("time"))
        elif kind == "danmaku_auto" and isinstance(data, dict):
            for item in data.get("history", []):
                if not isinstance(item, dict):
                    continue
                when = _epoch(item.get("attempted_at", item.get("sent_at")))
                text = _text(item.get("text"))
                if when is None:
                    continue
                key = (round(when, 3), text)
                attempts[key] = {"text": text, "time": when, "status": str(item.get("status", "")),
                                 "code": str(item.get("code", "")), "source": str(item.get("candidate_source", ""))}
                if item.get("status") in {"sent", "success", "delivered", "accepted"}:
                    accepted_fallbacks.append((item, event.get("time")))
        elif kind == "danmaku_candidates":
            candidate = _candidate_event(event)
            if candidate:
                key = (candidate["generated_at"], tuple(candidate["items"]))
                if key not in candidate_keys:
                    candidates.append(candidate)
                    candidate_keys.add(key)

    # A send context can preserve a successful send missed by an SSE reconnect.
    for context in visit.get("send_contexts", []):
        if not isinstance(context, dict):
            continue
        item = context.get("message", context.get("sent", context))
        if isinstance(item, dict):
            collect_sent(item, context.get("time"))

    # The application's auto-history timestamp is taken just after the sender's
    # own record.  Recover missing sends from it without counting the same POST
    # twice or replacing the sender's more precise content timestamp.
    for item, event_time in accepted_fallbacks:
        when = _epoch(item.get("sent_at"))
        text = _text(item.get("text"))
        if when is not None and not any(record["text"] == text and abs(record["sent_at"] - when) <= 2
                                        for record in sends.values()):
            collect_sent(item, event_time)

    ordered_sends = sorted(sends.values(), key=lambda item: item["sent_at"])
    for sent in ordered_sends:
        matched = []
        for item in contents:
            if sent["offset"] is not None and item["offset"] is not None:
                difference = item["offset"] - sent["offset"]
                basis = "session_content_time"
            elif sent["sent_at"] is not None and item["time"] is not None:
                difference = item["time"] - sent["sent_at"]
                basis = "event_received_time"
            else:
                continue
            if abs(difference) <= CONTEXT_SECONDS:
                matched.append({**item, "relative_seconds": round(difference, 2), "alignment": basis})
        sent["context"] = sorted(matched, key=lambda item: item["relative_seconds"])
    lags = [value for value in lag_values if value is not None and value >= 0]
    ordered_lags = sorted(lags)
    counts = Counter(item["type"] for item in contents)
    all_attempts = sorted(attempts.values(), key=lambda item: item["time"])
    failed = sum(item["status"] in {"failed", "error", "rejected"} for item in all_attempts)
    skipped = sum(item["status"] in {"skipped", "cancelled", "not_sent"} for item in all_attempts)
    room = visit.get("room") or {}
    saved_candidate_batches = int(_number(visit.get("candidate_batches")) or 0)
    last_candidate_count = len(candidates[-1]["items"]) if candidates else 0
    if saved_candidate_batches > len(candidates) or not candidates:
        last_candidate_count = int(_number(visit.get("last_candidate_count")) or last_candidate_count)
    questions = _collect_questions(events)
    target_rounds = _number(visit.get("simulation_rounds_per_visit"))
    if target_rounds is None or target_rounds <= 0:
        target_rounds = default_simulation_rounds or (10 if visit.get("simulation_rounds") else None)
    session_started_at = _epoch(visit.get("session_started_at"))
    scheduled_end_at = _epoch(visit.get("scheduled_end_at", visit.get("scheduled_end")))
    simulation_interval = ((scheduled_end_at - session_started_at) / target_rounds
                           if target_rounds and session_started_at is not None and scheduled_end_at is not None
                           and scheduled_end_at >= session_started_at else None)
    return {
        "visit_id": str(visit.get("visit_id", "")), "session_id": str(visit.get("session_id", "")),
        "room": {key: room.get(key, "") for key in ("room_id", "title", "uname", "area")},
        "started_at": _epoch(visit.get("started_at")), "ended_at": _epoch(visit.get("ended_at")),
        "session_started_at": session_started_at, "scheduled_end_at": scheduled_end_at,
        "simulation_rounds_per_visit": target_rounds, "simulation_interval_seconds": simulation_interval,
        "status": str(visit.get("status", "")), "error": _text(visit.get("error")),
        "auto_status": str(visit.get("auto_status", "")),
        # A current run policy must never retroactively authorize older visits.
        "allow_manual_send": visit.get("allow_manual_send") is True,
        "allow_manual_send_since": _epoch(visit.get("allow_manual_send_since")),
        "sent_count": max(len(ordered_sends), int(_number(visit.get("sent_count")) or 0)),
        "manual_sent_count": sum(item["source"] == "manual" for item in ordered_sends),
        "recorded_send_count": len(ordered_sends), "failed_count": failed,
        "unconfirmed_count": sum(item["status"] == "unknown" for item in all_attempts),
        "skipped_count": max(skipped, int(_number(visit.get("skipped_count")) or 0)),
        "observations": counts["observation"], "audio_segments": counts["audio_segment"],
        "chat_messages": counts["danmaku"],
        "candidate_batches": max(len(candidates), saved_candidate_batches),
        "candidate_total": max(sum(len(batch["items"]) for batch in candidates),
                               int(_number(visit.get("candidate_total")) or 0)),
        "recorded_candidate_batches": len(candidates),
        "last_candidate_count": last_candidate_count,
        "lag": {"samples": len(lags), "median": round(statistics.median(lags), 2) if lags else None,
                "p95": round(ordered_lags[max(0, math.ceil(len(lags) * .95) - 1)], 2) if lags else None,
                "max": round(max(lags), 2) if lags else None},
        "sends": ordered_sends, "attempts": all_attempts, "contents": contents, "candidates": candidates,
        "questions": questions,
        "planned_questions": sum(question["origin"] == "planned" for question in questions),
        "manual_questions": sum(question["origin"] == "manual" for question in questions),
        "historical_questions": sum(question["origin"] == "manual_or_historical" for question in questions),
        "chorus_windows": _collect_choruses(events),
        "simulation_rounds": _collect_simulations(events),
        "recordings": visit.get("recordings", []),
    }


def _atomic_write(path: Path, text: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _csv_safe(value: Any) -> str:
    text = str(value if value is not None else "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _render_csv(visits: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["visit_id", "room_id", "title", "streamer", "area", "started_at_utc", "ended_at_utc",
                     "status", "sent", "failed", "skipped", "observations", "audio_segments", "chat_messages",
                     "lag_median_seconds", "lag_p95_seconds", "lag_max_seconds", "error",
                     "candidate_batches", "candidate_total", "last_candidate_count",
                     "questions", "answers_complete", "chorus_windows", "simulation_rounds", "simulation_complete", "simulation_skipped",
                     "planned_questions", "manual_questions", "historical_questions",
                     "simulation_rounds_per_visit", "simulation_interval_seconds",
                     "manual_sent", "allow_manual_send", "allow_manual_send_since_utc", "sending_policy_mismatch"])
    for visit in visits:
        room, lag = visit["room"], visit["lag"]
        values = [visit["visit_id"], room["room_id"], room["title"], room["uname"], room["area"],
                  _iso(visit["started_at"]), _iso(visit["ended_at"]), visit["status"], visit["sent_count"],
                  visit["failed_count"], visit["skipped_count"], visit["observations"], visit["audio_segments"],
                  visit["chat_messages"], lag["median"], lag["p95"], lag["max"], visit["error"],
                  visit["candidate_batches"], visit["candidate_total"], visit["last_candidate_count"],
                  len(visit["questions"]), sum(q["status"] == "complete" and bool(q["answer"]) for q in visit["questions"]),
                  len(visit["chorus_windows"]), len(visit["simulation_rounds"]),
                  sum(r["status"] in {"complete", "completed", "recorded"} for r in visit["simulation_rounds"]),
                  sum(r["status"] == "skipped" for r in visit["simulation_rounds"]),
                  visit["planned_questions"], visit["manual_questions"], visit["historical_questions"],
                  visit["simulation_rounds_per_visit"], visit["simulation_interval_seconds"],
                  visit["manual_sent_count"], visit["allow_manual_send"],
                  _iso(visit["allow_manual_send_since"]), visit.get("sending_policy_mismatch", False)]
        writer.writerow([_csv_safe(value) for value in values])
    return output.getvalue()


CSS = """
:root{color-scheme:dark;--bg:#10141f;--card:#192132;--line:#354156;--muted:#acb8cc;--accent:#8ce4d2}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f0f4fa;font:16px/1.6 system-ui,-apple-system,"Microsoft YaHei",sans-serif}
main{max-width:1180px;margin:auto;padding:28px 24px 80px}h1{font-size:32px;margin:0}h2{font-size:22px;margin:0 0 6px}h3{font-size:17px;margin:0 0 8px}
a{color:var(--accent);overflow-wrap:anywhere}p{margin:10px 0}.muted,small{color:var(--muted)}.lead{font-size:18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:12px;margin:20px 0}
.metric{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px}.metric b{display:block;font-size:27px;color:var(--accent)}
section{margin:22px 0;padding:22px;border:1px solid var(--line);border-radius:14px;background:var(--card)}.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.chip{padding:4px 11px;background:#273249;border-radius:18px;font-size:13px}
details{border-top:1px solid var(--line);padding:12px 0}summary{cursor:pointer;font-weight:600;color:#e4edf9}details details{margin-left:12px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;vertical-align:top}th{white-space:nowrap;color:var(--muted)}td.content{white-space:pre-wrap;overflow-wrap:anywhere;min-width:240px}.nowrap{white-space:nowrap}.send{background:#111a29;border-left:3px solid var(--accent);padding:14px 18px;margin:14px 0;border-radius:6px}.send-text{font-size:21px;white-space:pre-wrap;overflow-wrap:anywhere;margin:7px 0}.warn{color:#ffcc8c;background:#31291e;padding:10px 14px;border-radius:8px}.source{font-size:13px;color:var(--muted)}ul{padding-left:22px}li{margin:6px 0}.notes{background:#202939;padding:12px 16px;border-radius:8px}nav{display:flex;gap:16px;flex-wrap:wrap;margin:18px 0}.candidate{margin:10px 0;padding:10px 14px;background:#111a29;border-radius:8px}.candidate p{margin:3px 0}footer{margin-top:28px;color:var(--muted);font-size:13px}button{background:#26384b;color:var(--accent);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer}
@media(max-width:600px){main{padding:18px 12px}section{padding:15px}h1{font-size:26px}.send-text{font-size:18px}th,td{padding:7px 5px}}
"""


def _content_table(items: list[dict[str, Any]], *, relative: bool = False) -> str:
    if not items:
        return '<p class="muted">此时间段暂无已记录内容。</p>'
    rows = []
    for item in items:
        label = TYPE_LABELS.get(item["type"], item["type"])
        time_label = _date(item.get("time"), short=True)
        if item.get("offset") is not None:
            time_label += f" / 会话 {_offset(item['offset'])}"
        if relative:
            difference = item["relative_seconds"]
            time_label = f"{difference:+.0f}s · " + time_label
            if item.get("alignment") == "event_received_time":
                time_label += "（按收到时间）"
        rows.append(f'<tr><td class="nowrap">{_escape(label)}<br><small>{_escape(time_label)}</small></td>'
                    f'<td class="content">{_escape(item["text"])}</td></tr>')
    return '<div class="table-wrap"><table><tbody>' + "".join(rows) + "</tbody></table></div>"


def _recordings_html(root: Path, visit_dir: Path, visit: dict[str, Any]) -> str:
    links = []
    for recording in visit["recordings"]:
        if not isinstance(recording, dict) or not recording.get("file"):
            continue
        value = str(recording["file"])
        source = Path(value)
        choices = [source] if source.is_absolute() else [visit_dir / source, root / source]
        path = next((candidate for candidate in choices if _inside(root, candidate) and candidate.is_file()), None)
        if path is None:
            links.append(f'<li>{_escape(source.name)} · 文件尚未生成或当前不可用</li>')
            continue
        href = _href(root, path)
        try:
            megabytes = path.stat().st_size / 1024 / 1024
        except OSError:
            megabytes = 0
        dates = f"{_date(recording.get('started_at'))} 至 {_date(recording.get('ended_at'))}"
        links.append(f'<li><a href="{_escape(href)}" download>{_escape(path.name)}</a>'
                     f' <small>· {megabytes:.1f} MB · {_escape(dates)}</small></li>')
    if not links:
        return '<p class="muted">该次访问暂无可下载的录像。</p>'
    return '<ul>' + "".join(links) + '</ul><p class="source">录像保留直播原音。MKV 可下载后用 VLC 等播放器打开；正在写入的分段可能尚未完整。</p>'


def _recording_link(root: Path, visit_dir: Path, visit: dict[str, Any],
                    offset: float | None, wall_time: float | None = None) -> str:
    """Link a content timestamp, explicitly distinguishing exact and clock estimates."""
    recordings = [item for item in visit["recordings"] if isinstance(item, dict) and item.get("file")]
    recordings.sort(key=lambda item: _epoch(item.get("started_at")) or 0)
    for index, recording in enumerate(recordings):
        content_start = _content_seconds(recording.get("content_start"))
        content_end = _content_seconds(recording.get("content_end"))
        if content_end is None and index + 1 < len(recordings):
            content_end = _content_seconds(recordings[index + 1].get("content_start"))
        estimated = True
        if offset is not None and content_start is not None:
            if offset < content_start or (content_end is not None and offset >= content_end):
                continue
            position, estimated = offset - content_start, False
        else:
            start = _epoch(recording.get("started_at"))
            end = _epoch(recording.get("ended_at"))
            if end is None and index + 1 < len(recordings):
                end = _epoch(recordings[index + 1].get("started_at"))
            if end is None:
                end = visit["ended_at"]
            session_start = visit.get("session_started_at")
            target = session_start + offset if session_start is not None and offset is not None else wall_time
            if start is None or target is None or target < start or (end is not None and target >= end):
                continue
            position = target - start
        source = Path(str(recording["file"]))
        choices = [source] if source.is_absolute() else [visit_dir / source, root / source]
        path = next((item for item in choices if _inside(root, item) and item.is_file()), None)
        if path is None:
            continue
        basis = "按录制起点估算" if estimated else "按内容时间"
        if offset is None or (visit.get("session_started_at") is None and content_start is None):
            basis = "按事件收到时刻估算"
        href = _href(root, path) + f"#t={position:.2f}"
        return (f'<a href="{_escape(href)}">{_escape(path.name)} · {_escape(_offset(position))}</a>'
                f' <small>（{basis}，播放器不支持跳转时请下载定位）</small>')
    return '<span class="muted">该时刻暂无可定位的录像分段。</span>'


def _questions_html(root: Path, visit_dir: Path, visit: dict[str, Any]) -> str:
    questions = visit["questions"]
    parts = [f'<details><summary>真实问答 · {len(questions)} 次</summary>',
             f'<p>规划提问 {visit["planned_questions"]} 次 · 手动提问 {visit["manual_questions"]} 次'
             f' · 手动/历史提问 {visit["historical_questions"]} 次</p>',
             '<p class="source">手动提问作为额外信息保存，不计入规划提问数。以下保留实际问题与完整回答；内容快照时间与回答完成时刻分开显示。回答可能有误，请结合原声、画面核对。</p>']
    for number, question in enumerate(questions, 1):
        state = STATUS_LABELS.get(question["status"], question["status"])
        parts.append(f'<div class="candidate"><h3>{number}. {_escape(question["question"] or "问题原文未记录")}</h3>'
                     f'<p><small>{_escape(QUESTION_ORIGIN_LABELS[question["origin"]])} · {_escape(state)}'
                     f' · {_escape(question["kind"] or "类型未记录")} · '
                     f'请求 {_escape(_date(question["requested_at"] or question["started_at"]))}'
                     f' · 完成 {_escape(_date(question["answered_at"]) if question["answered_at"] else "尚无完成记录")}</small></p>')
        snapshot = question["snapshot_time"]
        if snapshot is not None:
            parts.append(f'<p>内容快照：会话 {_escape(_offset(snapshot))}（{snapshot:g} 秒）</p>')
        else:
            parts.append('<p class="muted">内容快照时间未记录。</p>')
        parts.append('<p>' + _recording_link(root, visit_dir, visit, snapshot,
                                            question["requested_at"] or question["started_at"]) + '</p>')
        if question["answer"]:
            parts.append(f'<div style="white-space:pre-wrap;overflow-wrap:anywhere">{_escape(question["answer"])}</div>')
        else:
            parts.append('<p class="muted">没有已记录的回答正文。</p>')
        if question["error"]:
            parts.append(f'<p class="warn">{_escape(question["error"])}</p>')
        if question["plan"] or question["replay_times"] or question["evidence_times"]:
            parts.append('<details><summary>检索计划与内容定位</summary>')
            if question["plan"]:
                parts.append(f'<p style="white-space:pre-wrap">{_escape(question["plan"])}</p>')
            for label, values in (("画面回看", question["replay_times"]), ("引用内容", question["evidence_times"])):
                for value in values:
                    parts.append(f'<p>{label} · 会话 {_escape(_offset(value))}：'
                                 + _recording_link(root, visit_dir, visit, value) + '</p>')
            parts.append('</details>')
        parts.append('</div>')
    if not questions:
        parts.append('<p class="muted">暂无已记录的问答；本报告不会补写问题或回答。</p>')
    parts.append('</details>')
    return "".join(parts)


def _chorus_items_html(chorus: dict[str, Any]) -> str:
    if not chorus["recorded"]:
        return '<p class="muted">本轮未记录复读窗口；不能据此判断当时是否有人复读。</p>'
    if not chorus["items"]:
        return '<p class="muted">该窗口没有达到阈值的复读内容。</p>'
    parts = ['<div class="table-wrap"><table><thead><tr><th>现场原句</th><th>次数 / 窗口内总条数</th><th>窗口 / 最近内容时间</th></tr></thead><tbody>']
    for item in chorus["items"]:
        def numeric(key: str) -> str:
            return f'{item[key]:g}' if item[key] is not None else "未记录"
        parts.append(f'<tr><td class="content">{_escape(item["text"])}</td>'
                     f'<td>{numeric("count")} / {numeric("total")}</td>'
                     f'<td>{numeric("window_seconds")} 秒 / {_escape(_offset(item["source_time"]) or "未记录")}</td></tr>')
    return "".join(parts) + '</tbody></table></div>'


def _choruses_html(visit: dict[str, Any]) -> str:
    windows = visit["chorus_windows"]
    parts = [f'<details><summary>大家在刷 · {len(windows)} 个历史窗口</summary>',
             '<p class="source">现场重复弹幕的检测记录，保留每个不同窗口及空窗口；不是同担发送记录。</p>']
    for number, window in enumerate(windows, 1):
        parts.append(f'<div class="candidate"><small>窗口 {number} · {_escape(_date(window["updated_at"] or window["time"]))}</small>'
                     + _chorus_items_html(window) + '</div>')
    if not windows:
        parts.append('<p class="muted">暂无复读窗口档案；未记录不等于当时没有复读。</p>')
    return "".join(parts) + '</details>'


def _simulations_html(root: Path, visit_dir: Path, visit: dict[str, Any]) -> str:
    rounds = visit["simulation_rounds"]
    parts = [f'<details open><summary>模拟轮次 · {len(rounds)} 次记录（不发送）</summary>']
    for record in rounds:
        number = f'{record["round"]:g}' if record["round"] is not None else "未记录"
        state = STATUS_LABELS.get(record["status"], record["status"])
        parts.append(f'<div class="candidate"><h3>第 {number} 轮 · {_escape(state)}</h3>'
                     f'<p><small>计划 {_escape(_date(record["target_at"]))} · 记录 {_escape(_date(record["time"]))}</small></p>')
        if record["reason"]:
            parts.append(f'<p>{_escape(record["reason"])}</p>')
        if record["candidate_selection"] == "first_5_of_legacy_10" or (
                record["source_candidate_count"] is not None and record["source_candidate_count"] > len(record["candidates"])):
            source_count = record["source_candidate_count"] or 10
            parts.append(f'<p class="source">过渡记录：从旧会话原生 {source_count:g} 条候选中取前 {len(record["candidates"])} 条；完整原始批次仍留档。</p>')
        parts.append(f'<h3>候选 · {len(record["candidates"])} 条</h3>')
        parts.extend(f'<p>{index}. {_escape(text)}</p>' for index, text in enumerate(record["candidates"], 1))
        if not record["candidates"]:
            parts.append('<p class="muted">本轮没有记录候选，不以历史批次补齐。</p>')
        parts.append('<h3>同一轮的大家在刷</h3>' + _chorus_items_html(record["chorus"]))
        if record["chorus_capture_note"]:
            parts.append(f'<p class="source">{_escape(record["chorus_capture_note"])}</p>')
        parts.append('<p>' + _recording_link(root, visit_dir, visit, record["snapshot_time"], record["time"]) + '</p></div>')
    if not rounds:
        parts.append('<p class="muted">暂无模拟轮次记录。</p>')
    return "".join(parts) + '</details>'


def _visit_html(root: Path, visit_dir: Path, visit: dict[str, Any], index: int,
                *, candidate_only: bool = False, simulation_mode: bool = False) -> str:
    room, lag = visit["room"], visit["lag"]
    room_id = str(room.get("room_id", ""))
    label = STATUS_LABELS.get(visit["status"], visit["status"] or "状态未记录")
    title = room.get("title") or f"直播间 {room_id}"
    parts = [f'<section id="visit-{index}"><h2>{index}. {_escape(title)}</h2>',
             f'<p class="muted">{_escape(room.get("uname"))} · 房间 {_escape(room_id)} · {_escape(room.get("area"))}</p>',
             f'<p>{_escape(_date(visit["started_at"]))} — {_escape(_date(visit["ended_at"]) if visit["ended_at"] else "尚未结束")} · {_escape(label)}</p>']
    if room_id.isdigit():
        parts.append(f'<a href="https://live.bilibili.com/{room_id}" target="_blank" rel="noopener noreferrer">打开直播间</a>')
    if visit["scheduled_end_at"] is not None and visit["session_started_at"] is not None:
        dwell = max(0, visit["scheduled_end_at"] - visit["session_started_at"])
        parts.append(f'<p>本次计划停留 {dwell / 60:.1f} 分钟 · 计划结束 {_escape(_date(visit["scheduled_end_at"]))}</p>')
    target_rounds = visit["simulation_rounds_per_visit"]
    if target_rounds:
        interval = visit["simulation_interval_seconds"]
        interval_label = f"约 {interval / 60:.3g} 分钟一次" if interval is not None else "间隔按此次停留时长均分"
        parts.append(f'<p class="source">本次模拟目标 {target_rounds:g} 轮 · {interval_label}；以各轮计划时刻为准，模拟本身不发送弹幕。</p>')
    if candidate_only:
        policy = ("候选模拟不自动发送；允许用户手动发送，实际发送单独记录。"
                  if visit["allow_manual_send"] else "此次访问未授权手动发送，候选仅作模拟记录。")
        if visit["allow_manual_send"] and visit["allow_manual_send_since"] is not None:
            policy += f'手动发送权限自 {_date(visit["allow_manual_send_since"])} 起生效。'
        parts.append(f'<p class="source">{_escape(policy)}</p>')
    chips = ([f'候选 {visit["candidate_batches"]} 轮 / {visit["candidate_total"]} 条',
              f'最近一轮 {visit["last_candidate_count"]} 条'] if candidate_only else
             [f'本次成功发送 {visit["sent_count"]}', f'失败 {visit["failed_count"]}', f'跳过 {visit["skipped_count"]}'])
    chips.extend([f'观察 {visit["observations"]}', f'转写片段 {visit["audio_segments"]}',
                  f'现场弹幕 {visit["chat_messages"]}'])
    if candidate_only and (visit["allow_manual_send"] or visit["sent_count"]):
        chips.extend([f'实际发送 {visit["sent_count"]}', f'手动发送 {visit["manual_sent_count"]}'])
    if lag["samples"]:
        chips.extend([f'理解延迟中位 {lag["median"]}s', f'P95 {lag["p95"]}s', f'最长 {lag["max"]}s'])
    if visit["unconfirmed_count"]:
        chips.append(f'发送结果未确认 {visit["unconfirmed_count"]}')
    parts.append('<div class="chips">' + "".join(f'<span class="chip">{_escape(chip)}</span>' for chip in chips) + '</div>')
    if visit["error"]:
        parts.append(f'<p class="warn">{_escape(visit["error"])}</p>')
    if simulation_mode or visit["simulation_rounds"]:
        parts.append(_simulations_html(root, visit_dir, visit))
    parts.append(_questions_html(root, visit_dir, visit))
    parts.append(_choruses_html(visit))
    show_sends = not candidate_only or visit["sent_count"] > 0
    if show_sends:
        parts.append('<details open><summary>已发送弹幕与前后文</summary>')
        if not visit["sends"]:
            parts.append('<p class="muted">暂无成功发送的正文记录。</p>')
        if visit["sent_count"] > visit["recorded_send_count"]:
            parts.append('<p class="warn">发送计数大于正文记录数；部分发送详情未被事件日志捕获。</p>')
    for sent in visit["sends"]:
        parts.append(f'<div class="send"><small>{_escape(_date(sent["sent_at"]))}'
                     f' · {_escape(sent["source"] or "来源未记录")}'
                     f'{" · 会话 " + _escape(_offset(sent["offset"])) if sent["offset"] is not None else ""}</small>'
                     f'<p class="send-text">{_escape(sent["text"])}</p>'
                     f'<details><summary>发送前后各 {CONTEXT_SECONDS} 秒 · {len(sent["context"])} 条内容</summary>')
        for phase, predicate in (("发送前", lambda item: item["relative_seconds"] < 0),
                                 ("发送后", lambda item: item["relative_seconds"] >= 0)):
            selected = [item for item in sent["context"] if predicate(item)]
            parts.append(f'<h3>{phase}</h3>' + _content_table(selected, relative=True))
        parts.append('</details></div>')
    if show_sends:
        parts.append('</details>')
    parts.append('<details><summary>原始录像与事件记录</summary>')
    parts.append(_recordings_html(root, visit_dir, visit))
    event_file = visit_dir / "events.jsonl"
    if event_file.is_file() and _inside(root, event_file):
        parts.append(f'<p><a href="{_escape(_href(root, event_file))}" download>下载本房间 events.jsonl</a></p>')
    candidate_file = visit_dir / "candidates.jsonl"
    if candidate_file.is_file() and _inside(root, candidate_file):
        parts.append(f'<p><a href="{_escape(_href(root, candidate_file))}" download>下载本房间全部候选 candidates.jsonl</a></p>')
    for filename, label in (("questions.jsonl", "完整问答事件"), ("chorus.jsonl", "全部复读窗口"),
                            ("simulation_rounds.jsonl", "模拟轮次原始记录")):
        path = visit_dir / filename
        if path.is_file() and _inside(root, path):
            parts.append(f'<p><a href="{_escape(_href(root, path))}" download>下载{label} {filename}</a></p>')
    parts.append('</details>')
    speech_and_observations = [item for item in visit["contents"] if item["type"] != "danmaku"]
    parts.append(f'<details><summary>全部观察与音频转写 · {len(speech_and_observations)} 条</summary>'
                 + _content_table(speech_and_observations) + '</details>')
    parts.append(f'<details><summary>候选弹幕批次 · {len(visit["candidates"])} 组</summary>')
    if visit["candidate_batches"] > visit["recorded_candidate_batches"]:
        parts.append('<p class="warn">批次计数大于已记录的候选正文组数；部分候选详情尚未写入或未被日志捕获。</p>')
    for batch in visit["candidates"]:
        parts.append(f'<div class="candidate"><small>{_escape(_date(batch["time"]))}</small>'
                     + "".join(f'<p>{number}. {_escape(text)}</p>' for number, text in enumerate(batch["items"], 1)) + '</div>')
    if not visit["candidates"]:
        parts.append('<p class="muted">暂无候选弹幕记录。</p>')
    parts.append('</details>')
    unsuccessful = [item for item in visit["attempts"] if item["status"] not in {"sent", "success", "delivered", "accepted"}]
    if unsuccessful:
        parts.append(f'<details><summary>失败、跳过或未完成的发送尝试 · {len(unsuccessful)} 条</summary><div class="table-wrap"><table>')
        for item in unsuccessful:
            state = STATUS_LABELS.get(item["status"], item["status"])
            parts.append(f'<tr><td class="nowrap">{_escape(_date(item["time"], short=True))}<br>{_escape(state)}</td>'
                         f'<td class="content">{_escape(item["text"])}<br><small>{_escape(item["code"])}</small></td></tr>')
        parts.append('</table></div></details>')
    parts.append('</section>')
    return "".join(parts)


def _room_selection_label(config: dict[str, Any]) -> str:
    if config.get("virtual_fallback"):
        return "关注列表优先：只要有关注的直播间开播，就只去关注；仅确认关注列表无人开播时，才从虚拟主播分区选择正在直播的房间。"
    return "仅在关注列表中选择正在直播的房间。"


def _room_plan(status: dict[str, Any]) -> list[dict[str, Any]]:
    plan = status.get("room_plan", [])
    return [{"room_id": str(item.get("room_id", "")), "dwell_seconds": _number(item.get("dwell_seconds")),
             "simulation_rounds_per_visit": _number(item.get("simulation_rounds_per_visit"))}
            for item in plan if isinstance(item, dict)] if isinstance(plan, list) else []


def _room_plan_label(status: dict[str, Any]) -> str:
    labels = []
    for item in _room_plan(status):
        duration = item["dwell_seconds"]
        dwell = f"{duration / 60:g} 分钟" if duration is not None else "停留时长未记录"
        rounds = item["simulation_rounds_per_visit"]
        if rounds and rounds > 0:
            dwell += f"，目标 {rounds:g} 轮"
            if duration is not None:
                dwell += f"，约 {duration / rounds / 60:.3g} 分钟一次"
        labels.append(f'{item["room_id"]}（{dwell}）')
    return "指定房间计划：" + " → ".join(labels) + "；这些房间按指定时长，其余使用默认停留时长。" if labels else ""


def _sending_policy_mismatch(visit: dict[str, Any]) -> bool:
    """Check this visit's explicit policy, including a mid-visit opt-in time."""
    allowed = visit.get("allow_manual_send") is True
    since = _epoch(visit.get("allow_manual_send_since"))
    sends = visit.get("sends", [])
    if visit.get("sent_count", 0) > len(sends):
        return True  # Missing send details cannot establish manual authorization.
    for record in [*sends, *visit.get("attempts", [])]:
        when = _epoch(record.get("sent_at", record.get("time")))
        if (not allowed or record.get("source") != "manual"
                or (since is not None and (when is None or when < since))):
            return True
    return False


def _candidate_sending_rule(config: dict[str, Any]) -> str:
    return ("候选模拟不自动发送；允许用户手动发送，实际发送单独记录。"
            if config.get("allow_manual_send") is True else "全程不发送弹幕。")


def _candidate_rule(config: dict[str, Any], *, room_overrides: bool = False) -> str:
    rounds = _number(config.get("simulation_rounds_per_visit"))
    if rounds is not None and rounds > 0:
        count = config.get("simulation_candidate_count", "未记录")
        prefix = "默认" if room_overrides else ""
        override_note = "指定房间的目标轮数见下方计划，历史访问沿用各自记录。" if room_overrides else ""
        return (f"{prefix}每房均匀安排 {rounds:g} 次模拟记录，每次保存 {count} 条候选及完整的“大家在刷”。"
                + override_note +
                _candidate_sending_rule(config) + "错过或候选不可用的轮次标为跳过，原始生成批次另行完整留档。")
    count = config.get("candidate_count", "未记录")
    scope = config.get("candidate_scope")
    if scope in {None, "per_generation_batch"}:
        return f"只生成候选，每轮目标 {count} 条；每轮指一次生成批次。{_candidate_sending_rule(config)}实际生成数量以记录为准。"
    return f"只生成候选，目标数量 {count} 条；范围：{scope}。{_candidate_sending_rule(config)}实际生成数量以记录为准。"


def render_report(run_dir: str | Path) -> dict[str, Any]:
    """Atomically create index.html, summary.csv and report.json, returning totals.

    All input times without an explicit offset are treated as UTC.  The page
    shows wall clocks in Asia/Shanghai (UTC+08:00); CSV/JSON retain UTC.  A partial
    JSONL final line is skipped and reported, allowing calls during recording.
    Does not modify inputs, contact Bilibili, or issue any application command.
    """
    root = Path(run_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Patrol run directory does not exist: {root}")
    warnings: list[str] = []
    status = _load_json(root / "status.json", warnings)
    global_events = _read_events(root / "events.jsonl", warnings)
    by_session: dict[str, list[dict[str, Any]]] = {}
    for event in global_events:
        by_session.setdefault(str(event.get("session_id", "")), []).append(event)
    visit_files = sorted((root / "visits").glob("*/visit.json"))
    parsed: list[tuple[Path, dict[str, Any]]] = []
    for path in visit_files:
        if not _inside(root, path):
            continue
        visit = _load_json(path, warnings)
        if not visit:
            continue
        visit.setdefault("visit_id", path.parent.name)
        local_events = path.parent / "events.jsonl"
        events = _read_events(local_events, warnings) if local_events.is_file() else by_session.get(str(visit.get("session_id", "")), [])
        candidate_file = path.parent / "candidates.jsonl"
        if _inside(root, candidate_file):
            events = [*events, *({**event, "type": "danmaku_candidates"}
                                for event in _read_events(candidate_file, warnings))]
        for filename, forced_type in (("questions.jsonl", None), ("chorus.jsonl", "danmaku_chorus"),
                                      ("simulation_rounds.jsonl", "simulation_round")):
            archive = path.parent / filename
            if _inside(root, archive):
                archived = _read_events(archive, warnings)
                events.extend(({**event, "type": forced_type} if forced_type else event) for event in archived)
        sid = str(visit.get("session_id") or "")
        if sid:
            events = [event for event in events if not event.get("session_id") or str(event["session_id"]) == sid]
        events = sorted(events, key=lambda event: _epoch(event.get("time")) or 0)
        parsed.append((path.parent, _collect_visit(visit, events, default_simulation_rounds=
                                                  _number((status.get("config") or {}).get("simulation_rounds_per_visit")))))
    parsed.sort(key=lambda pair: (pair[1]["started_at"] or 0, pair[1]["visit_id"]))
    visits = [visit for _, visit in parsed]
    current_time = datetime.now(UTC).timestamp()
    totals = {"visits": len(visits), "rooms": len({str(item["room"]["room_id"]) for item in visits}),
              "sent": sum(item["sent_count"] for item in visits),
              "manual_sent": sum(item["manual_sent_count"] for item in visits),
              "failed": sum(item["failed_count"] for item in visits),
              "skipped": sum(item["skipped_count"] for item in visits),
              "observations": sum(item["observations"] for item in visits),
              "audio_segments": sum(item["audio_segments"] for item in visits),
              "chat_messages": sum(item["chat_messages"] for item in visits),
              "candidate_batches": sum(item["candidate_batches"] for item in visits),
              "candidate_total": sum(item["candidate_total"] for item in visits),
              "questions": sum(len(item["questions"]) for item in visits),
              "planned_questions": sum(item["planned_questions"] for item in visits),
              "manual_questions": sum(item["manual_questions"] for item in visits),
              "historical_questions": sum(item["historical_questions"] for item in visits),
              "answers_complete": sum(question["status"] == "complete" and bool(question["answer"])
                                      for item in visits for question in item["questions"]),
              "chorus_windows": sum(len(item["chorus_windows"]) for item in visits),
              "simulation_rounds": sum(len(item["simulation_rounds"]) for item in visits),
              "simulation_complete": sum(record["status"] in {"complete", "completed", "recorded"}
                                         for item in visits for record in item["simulation_rounds"]),
              "simulation_skipped": sum(record["status"] == "skipped" for item in visits for record in item["simulation_rounds"])}
    config = status.get("config") or {}
    candidate_only = config.get("candidate_only") is True
    for visit in visits:
        visit["sending_policy_mismatch"] = candidate_only and _sending_policy_mismatch(visit)
    totals["sending_policy_mismatches"] = sum(visit["sending_policy_mismatch"] for visit in visits)
    simulation_mode = bool(_number(config.get("simulation_rounds_per_visit")))
    quota_scope = None if candidate_only else config.get("quota_scope")
    first_room = str(config.get("first_room") or "")
    room_quotas: dict[str, dict[str, Any]] = {}
    for visit in ([] if candidate_only else visits):
        room_id = str(visit["room"]["room_id"])
        cap_key = "first_max_messages" if room_id == first_room else "max_messages"
        cap = int(_number(config.get(cap_key, config.get("max_messages"))) or 0)
        quota = room_quotas.setdefault(room_id, {"room_id": room_id, "uname": visit["room"]["uname"],
                                               "sent": 0, "cap": cap, "visits": 0})
        quota["sent"] += visit["sent_count"]
        quota["visits"] += 1
        quota["remaining"] = max(0, quota["cap"] - quota["sent"])
    report = {"schema_version": 1, "run_id": str(status.get("run_id", root.name)),
              "generated_at": _iso(current_time), "display_timezone": "Asia/Shanghai (UTC+08:00)",
              "started_at": _iso(status.get("started_at")), "deadline": _iso(status.get("deadline")),
              "status": str(status.get("status", "unknown")), "totals": totals,
              "context_seconds_each_side": CONTEXT_SECONDS,
              "sending_disabled_reason": _text(status.get("sending_disabled_reason")),
              "warnings": warnings, "visits": visits,
              "quota_scope": quota_scope, "room_quotas": list(room_quotas.values()),
              "room_plan": _room_plan(status),
              "config": {key: config.get(key) for key in ("dwell_seconds", "duration_hours", "interval_seconds",
                         "max_messages", "jitter_ratio", "first_room", "first_dwell_seconds", "first_max_messages",
                         "candidate_only", "allow_manual_send", "send_authorized", "candidate_count", "candidate_scope", "virtual_fallback", "room_selection",
                         "simulation_rounds_per_visit", "simulation_candidate_count", "simulation_scope",
                         "questions_enabled", "question_interval_seconds")}}
    dwell = _number(config.get("dwell_seconds"))
    interval = _number(config.get("interval_seconds"))
    state = STATUS_LABELS.get(report["status"], report["status"])
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'self\' data:; media-src \'self\'; base-uri \'none\'; form-action \'none\'">',
             '<title>AI 同担 · 轮巡复盘</title><style>', CSS, '</style></head><body><main>',
             '<h1>AI 同担 · 轮巡复盘</h1>',
             f'<p class="lead">{_escape(report["run_id"])} · {_escape(state)}</p>',
             f'<p class="muted">开始 {_escape(_date(status.get("started_at")))} · 计划结束 {_escape(_date(status.get("deadline")))}<br>',
             f'页面生成 {_escape(_date(current_time))} · 页面时区：北京时间 UTC+08:00；CSV 时间为 UTC。</p>',
             '<nav><a href="summary.csv" download>下载汇总 CSV</a><a href="report.json" download>下载结构化复盘 JSON</a></nav>',
             '<div class="grid">']
    metrics = [("访问次数", totals["visits"]), ("不同房间", totals["rooms"])]
    metrics.extend([("候选批次", totals["candidate_batches"]), ("候选总条数", totals["candidate_total"])]
                   if candidate_only else [("成功发送", totals["sent"]),
                                           ("失败 / 跳过", f'{totals["failed"]} / {totals["skipped"]}')])
    metrics.append(("观察条数", totals["observations"]))
    if candidate_only and (config.get("allow_manual_send") is True or totals["sent"]):
        metrics.extend([("实际发送", totals["sent"]), ("手动发送", totals["manual_sent"])])
    if simulation_mode:
        metrics.extend([("模拟完成 / 跳过", f'{totals["simulation_complete"]} / {totals["simulation_skipped"]}'),
                        ("问答完成 / 记录", f'{totals["answers_complete"]} / {totals["questions"]}')])
    if totals["questions"]:
        metrics.append(("规划 / 手动提问", f'{totals["planned_questions"]} / {totals["manual_questions"]}'))
        if totals["historical_questions"]:
            metrics.append(("手动/历史提问", totals["historical_questions"]))
    for label, count in metrics:
        parts.append(f'<div class="metric"><b>{_escape(count)}</b>{label}</div>')
    parts.append('</div>')
    if candidate_only:
        parts.append(f'<p class="lead">{_escape(_candidate_rule(config, room_overrides=any(item["simulation_rounds_per_visit"] for item in _room_plan(status))))}</p>')
        dwell_label = f"{dwell / 60:g} 分钟" if dwell else "未记录"
        parts.append(f'<p>普通房间默认每次停留 {_escape(dwell_label)} · 总时长 {_escape(config.get("duration_hours", "未记录"))} 小时</p>')
        if first_room:
            first_dwell = _number(config.get("first_dwell_seconds"))
            first_dwell_label = f"{first_dwell / 60:g} 分钟" if first_dwell else "未记录"
            parts.append(f'<p>首站：房间 {_escape(first_room)}，首次停留 {_escape(first_dwell_label)}；之后默认每次停留 {_escape(dwell_label)}，指定房间计划另计。</p>')
        room_plan_label = _room_plan_label(status)
        if room_plan_label:
            parts.append(f'<p>{_escape(room_plan_label)}</p>')
        parts.append(f'<p>{_escape(_room_selection_label(config))}</p>')
        rounds = _number(config.get("simulation_rounds_per_visit"))
        if rounds and dwell:
            first_dwell = _number(config.get("first_dwell_seconds"))
            first_interval = f"首站约 {first_dwell / rounds / 60:g} 分钟一次；" if first_room and first_dwell else ""
            parts.append(f'<p>{first_interval}普通房间约 {dwell / rounds / 60:g} 分钟一次，指定房间按各自停留时长均分，均为本地模拟记录。</p>')
        if config.get("questions_enabled"):
            interval = _number(config.get("question_interval_seconds"))
            interval_text = f"目标约 {interval / 60:g} 分钟一次；" if interval else ""
            parts.append(f'<p>自动提问：{interval_text}实际请求、排队、完成与失败均按问答档案复盘。</p>')
        if totals["sending_policy_mismatches"]:
            parts.append('<p class="warn">记录中出现发送或发送尝试，与“只生成候选”的计划不符或无法确认符合当次手动发送权限；相关原始记录保留在下方，请核查。当前权限不追溯授权历史访问。</p>')
    elif config:
        dwell_label = f"{dwell / 60:g} 分钟" if dwell else "未记录"
        jitter = _number(config.get("jitter_ratio"))
        jitter = .3 if jitter is None else jitter
        interval_label = f"{interval / 60:g} 分钟 ±{jitter * 100:g}%" if interval else "未记录"
        quota_label = "本轮每房累计最多" if quota_scope == "per_room_across_entire_run" else "最多"
        parts.append(f'<p>普通房间每次停留 {_escape(dwell_label)} · {quota_label} {_escape(config.get("max_messages", "未记录"))} 条 · 基准间隔 {_escape(interval_label)} · 总时长 {_escape(config.get("duration_hours", "未记录"))} 小时</p>')
        if first_room:
            first_dwell = _number(config.get("first_dwell_seconds"))
            first_dwell_label = f"{first_dwell / 60:g} 分钟" if first_dwell else "未记录"
            first_cap = config.get("first_max_messages", config.get("max_messages", "未记录"))
            parts.append(f'<p>首站：房间 {_escape(first_room)}，首次停留 {_escape(first_dwell_label)}；该房间本轮累计最多 {_escape(first_cap)} 条。</p>')
        if quota_scope == "per_room_across_entire_run":
            parts.append('<p class="muted">发送配额按整个轮巡任务累计；回访同一直播间不会重置配额。</p>')
            if room_quotas:
                parts.append('<details><summary>各直播间本轮累计配额</summary><div class="table-wrap"><table>'
                             '<thead><tr><th>房间</th><th>访问次数</th><th>已发送 / 总配额</th><th>剩余</th></tr></thead><tbody>')
                for quota in room_quotas.values():
                    name = f'{quota["uname"]} · {quota["room_id"]}' if quota["uname"] else quota["room_id"]
                    parts.append(f'<tr><td>{_escape(name)}</td><td>{quota["visits"]}</td>'
                                 f'<td>{quota["sent"]} / {quota["cap"]}</td><td>{quota["remaining"]}</td></tr>')
                parts.append('</tbody></table></div></details>')
    record_note = ('候选仅供本地复盘，不代表已经发送或主播看见。' if candidate_only else
                   '发送记录来自应用确认结果。前后文用于人工复盘，不能据此断定主播念到弹幕或识别了 AI。')
    parts.append('<div class="notes">' + record_note +
                 '优先按会话内容时间对齐；缺少内容时间的条目标注为“按收到时间”。音频转写可能有误，请结合原录像。'
                 '运行中请刷新页面获取最新报告。</div>')
    if report["sending_disabled_reason"]:
        parts.append(f'<p class="warn">发送已暂停：{_escape(report["sending_disabled_reason"])}</p>')
    for warning in warnings:
        parts.append(f'<p class="warn">{_escape(warning)}</p>')
    if visits:
        parts.append('<details><summary>跳转到某次访问</summary><ol>')
        for index, visit in enumerate(visits, 1):
            room = visit["room"]
            parts.append(f'<li><a href="#visit-{index}">{_escape(_date(visit["started_at"], short=True))} · '
                         f'{_escape(room["uname"] or room["room_id"])} · {_escape(room["title"])}</a></li>')
        parts.append('</ol></details>')
    else:
        parts.append('<section><p>轮巡尚未产生访问记录。运行开始后刷新此页即可查看。</p></section>')
    for index, (directory, visit) in enumerate(parsed, 1):
        parts.append(_visit_html(root, directory, visit, index, candidate_only=candidate_only,
                                 simulation_mode=simulation_mode))
    parts.append('<footer>本页只读取本地轮巡记录，不会发送弹幕，也不会加载外部脚本。所有内容均为记录，未自动评判主播反应。</footer></main></body></html>')
    _atomic_write(root / "summary.csv", _render_csv(visits))
    _atomic_write(root / "report.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _atomic_write(root / "index.html", "".join(parts))
    return {"run_id": report["run_id"], "index": str(root / "index.html"),
            "csv": str(root / "summary.csv"), "json": str(root / "report.json"),
            "totals": totals, "warnings": warnings}


def render_plan(run_dir: str | Path) -> str:
    """Write an independent startup-plan page without altering live reports."""
    root = Path(run_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Patrol run directory does not exist: {root}")
    status = _load_json(root / "status.json", [])
    config = status.get("config") or {}
    candidate_only = config.get("candidate_only") is True
    dwell = _number(config.get("dwell_seconds"))
    first_dwell = _number(config.get("first_dwell_seconds"))
    interval = _number(config.get("interval_seconds"))
    jitter = _number(config.get("jitter_ratio"))
    jitter = .3 if jitter is None else jitter
    rows = [
        ("运行时长", f'{config.get("duration_hours", "未记录")} 小时'),
        ("开始时间", _date(status.get("started_at"))),
        ("计划结束", _date(status.get("deadline"))),
        ("普通房间", f'每次停留 {dwell / 60:g} 分钟；本轮累计每房最多 {config.get("max_messages", "未记录")} 条' if dwell else "未记录"),
        ("首站直播间", str(config.get("first_room") or "未指定")),
        ("首站规则", f'首次停留 {first_dwell / 60:g} 分钟；该房间本轮累计最多 {config.get("first_max_messages", "未记录")} 条' if first_dwell else "与普通房间一致"),
        ("发送间隔", f'基准 {interval / 60:g} 分钟，每次随机上下浮动 {jitter * 100:g}%' if interval else "未记录"),
        ("回访规则", "回访同一房间不会重置发送配额；首站后续回访使用普通停留时长"),
    ]
    if candidate_only:
        rows = [
            ("运行模式", _candidate_rule(config, room_overrides=any(item["simulation_rounds_per_visit"] for item in _room_plan(status)))),
            ("运行时长", f'{config.get("duration_hours", "未记录")} 小时'),
            ("开始时间", _date(status.get("started_at"))),
            ("计划结束", _date(status.get("deadline"))),
            ("普通房间", f'默认每次停留 {dwell / 60:g} 分钟；指定房间计划另计' if dwell else "未记录"),
            ("首站直播间", str(config.get("first_room") or "未指定")),
            ("首站规则", f'首次停留 {first_dwell / 60:g} 分钟' if first_dwell else "与普通房间一致"),
            ("回访规则", "首站后续回访使用普通停留时长；候选按生成批次记录"),
            ("选房规则", _room_selection_label(config)),
        ]
        rounds = _number(config.get("simulation_rounds_per_visit"))
        if rounds and dwell:
            first_interval = f"首站约 {first_dwell / rounds / 60:g} 分钟一次；" if config.get("first_room") and first_dwell else ""
            rows.append(("模拟记录节奏", first_interval + f"普通房间约 {dwell / rounds / 60:g} 分钟一次，按各次停留时长均匀分配"))
        room_plan_label = _room_plan_label(status)
        if room_plan_label:
            rows.append(("指定房间队列", room_plan_label))
        if config.get("questions_enabled"):
            question_interval = _number(config.get("question_interval_seconds"))
            rows.append(("自动提问", f"目标约 {question_interval / 60:g} 分钟一次，以实际问答记录为准"
                         if question_interval else "已启用，以实际问答记录为准"))
    page = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            '<title>AI 同担 · 本轮执行计划</title><style>', CSS, '</style></head><body><main>',
            '<h1>AI 同担 · 本轮执行计划</h1>',
            f'<p class="muted">{_escape(status.get("run_id", root.name))} · 所有页面时间为北京时间 UTC+08:00</p>',
            '<section><div class="table-wrap"><table><tbody>']
    page.extend(f'<tr><th>{_escape(label)}</th><td>{_escape(value)}</td></tr>' for label, value in rows)
    record_label = ("全部候选及手动发送记录" if config.get("allow_manual_send") is True else "全部候选记录") if candidate_only else "发送记录"
    plan_note = (_candidate_sending_rule(config) + "实际生成批次和条数以复盘报告为准。" if candidate_only else
                 "实际成功发送数量以复盘报告为准；候选不足或依据过期时会跳过发送。")
    page.extend(['</tbody></table></div></section>',
                 f'<p><a href="index.html">查看持续更新的复盘报告、{record_label}与录像</a></p>',
                 f'<p class="muted">本页保存本次启动计划，不自动更新。{plan_note}</p>',
                 '</main></body></html>'])
    path = root / "轮巡计划.html"
    _atomic_write(path, "".join(page))
    return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AI 同担轮巡本地复盘页面")
    parser.add_argument("run_dir", type=Path, help="data/patrol/<run> 目录")
    args = parser.parse_args()
    print(json.dumps(render_report(args.run_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
