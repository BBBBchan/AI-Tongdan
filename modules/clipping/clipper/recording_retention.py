"""Release stopped live source buffers without constructing the production Store.

The journal is retained. Only regular media/audio/frame files inside the known
live session directory are removed; exports and imported footage are untouched.
Source claims and new source-dependent work serialize on SQLite's writer lock.
"""
from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

VERSION = 1
UNAVAILABLE = {"purging", "partial", "removed"}
ACTIVE_ANALYSIS = {"pending", "running", "analyzing", "transcribing", "分析中", "正在提取画面与语音"}
TERMINAL = {"stopped", "interrupted", "error", "capacity_reached", "limit_reached"}
TERMINAL_ANALYSIS = {None, "complete", "timeout", "failed", "partial", "interrupted", "rules",
                     "waiting_for_events", "waiting_for_media", "分析已停止", "分析未完成", "分析已中断"}
RAW_SUFFIXES = {"media": {".ts", ".m3u8", ".jpg", ".wav", ".tmp"},
                "audio": {".wav"}, "frames": {".jpg", ".png"}}


def equivalent_render_options(left, right):
    """Normalize only defaults used by Renderer/RenderRequest.

    Explicit nondefault values and unknown future keys remain significant.
    In particular, this does not erase captions, focus-region, or resolution
    differences simply because both jobs produced a playable video.
    """
    from .render import DEFAULT_RESOLUTION
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    defaults = {"resolution": DEFAULT_RESOLUTION, "aspect": "landscape",
                "captions": True, "emphasis": True, "cover": True,
                "cover_mode": "designed", "focus_region": None}
    return {**defaults, **left} == {**defaults, **right}


def equivalent_source_anchor(left, right):
    """Ignore only copy rewritten after rendering by apply_review.

    LivePublicationController updates these three fields on the completed job
    while preserving its video edit. All timing, evidence, focus, style, identity
    and unknown fields must still match the failed job exactly.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    public_copy = {"title", "title_variants", "cover_text"}
    return ({key: value for key, value in left.items() if key not in public_copy}
            == {key: value for key, value in right.items() if key not in public_copy})


def source_available(session):
    return (session.get("source_retention") or {}).get("status") not in UNAVAILABLE


def policy(data_dir):
    try:
        value = json.loads((Path(data_dir) / "recording-retention/policy.json").read_text())
        if value.get("version") == VERSION and value.get("mode") == "clips_only":
            return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {"version": VERSION, "mode": "archive", "enabled": False}


def connect(data_dir, *, readonly=False):
    path = (Path(data_dir) / "clipper.sqlite").resolve()
    conn = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                           uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, table, sid=None):
    sql = f"SELECT payload FROM {table}"
    if sid is not None:
        sql += " WHERE session_id=?"
    return [json.loads(row[0]) for row in conn.execute(sql, (sid,) if sid is not None else ())]


def _eligibility(conn, data_dir, sid, *, now, grace_seconds, discard_terminal_analysis_failures=False):
    row = conn.execute("SELECT payload,updated FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None, "session_missing"
    session = json.loads(row["payload"])
    if session.get("mode") != "live":
        return session, "imported_source"
    if session.get("status") not in TERMINAL:
        return session, "capture_active_or_unknown"
    if session.get("stop_phase") != "complete":
        return session, "stop_not_finalized"
    if (session.get("analysis_status") in ACTIVE_ANALYSIS or session.get("analysis_source_reserved")
            or session.get("final_analysis_status") in ACTIVE_ANALYSIS):
        return session, "analysis_active"
    if (session.get("tail_recovery") or {}).get("status") in {"queued", "running"}:
        return session, "tail_recovery_active"
    retention = session.get("source_retention") or {}
    if retention.get("status") == "removed":
        return session, "already_removed"
    # Failed/timed-out tail analysis is retained by default. An explicit clips-
    # only discard policy may abandon that future retry, never active work.
    if session.get("duration", 0) >= 6 and (session.get("final_analysis_status") != "complete"
                                            or session.get("analysis_status") != "complete"):
        if not (discard_terminal_analysis_failures
                and session.get("final_analysis_status") in TERMINAL_ANALYSIS
                and session.get("analysis_status") in TERMINAL_ANALYSIS):
            return session, "final_analysis_incomplete"
    jobs = _rows(conn, "jobs", sid)
    completed = [j for j in jobs if j.get("status") == "complete"]
    for job in jobs:
        if job.get("status") == "complete":
            jid = str(job.get("id", ""))
            if not re.fullmatch(r"[0-9a-f]{16}", jid):
                return session, "invalid_export_id"
            clip = Path(data_dir) / "exports" / jid / "clip.mp4"
            if not clip.is_file() or clip.stat().st_size <= 0:
                return session, "completed_clip_missing"
        elif job.get("status") == "failed":
            # Only a successful later rerender of exactly the same edit can
            # release a failed job's dependency. A different edit cannot.
            superseded = any(
                c.get("anchor_id") == job.get("anchor_id")
                and c.get("created_at", 0) > job.get("created_at", 0)
                and equivalent_source_anchor(c.get("anchor"), job.get("anchor"))
                and equivalent_render_options(c.get("options", {}), job.get("options", {}))
                for c in completed)
            if not superseded:
                return session, "failed_job_needs_source"
        else:
            return session, "job_pending_or_unknown"
    if retention.get("status") not in {"purging", "partial"} and now - row["updated"] < grace_seconds:
        return session, "settling"
    return session, None


def raw_files(data_dir, sid):
    if not re.fullmatch(r"[0-9a-f]{16}", str(sid)):
        raise ValueError("invalid_session_id")
    directory = Path(data_dir).resolve() / "sessions" / sid
    if directory.is_symlink() or directory.resolve() != directory:
        raise ValueError("unsafe_session_directory")
    files = []
    for name, suffixes in RAW_SUFFIXES.items():
        root = directory / name
        if root.is_symlink():
            raise ValueError("unsafe_source_directory")
        if not root.is_dir():
            continue
        for parent, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(parent) / d).is_symlink()]
            for name in names:
                path = Path(parent) / name
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and path.suffix.lower() in suffixes:
                    files.append((path, info.st_size))
    return files


def plan(data_dir, *, now=None, grace_seconds=120, discard_terminal_analysis_failures=None):
    now = time.time() if now is None else now
    if discard_terminal_analysis_failures is None:
        discard_terminal_analysis_failures = policy(data_dir).get("discard_terminal_analysis_failures") is True
    result = {"version": VERSION, "planned_at": now, "eligible": [], "held": [],
              "files": 0, "bytes": 0}
    with connect(data_dir, readonly=True) as conn:
        for row in conn.execute("SELECT id FROM sessions"):
            sid = row["id"]
            session, reason = _eligibility(conn, data_dir, sid, now=now, grace_seconds=grace_seconds,
                discard_terminal_analysis_failures=discard_terminal_analysis_failures)
            entry = {"session_id": sid}
            if reason:
                result["held"].append({**entry, "reason": reason})
                continue
            try:
                files = raw_files(data_dir, sid)
            except (OSError, ValueError) as exc:
                result["held"].append({**entry, "reason": type(exc).__name__})
                continue
            size = sum(size for _, size in files)
            result["eligible"].append({**entry, "files": len(files), "bytes": size,
                "streamer_name": session.get("streamer_name") or session.get("streamer"),
                "discarded_without_final_analysis": session.get("duration", 0) >= 6 and (
                    session.get("analysis_status") != "complete" or session.get("final_analysis_status") != "complete")})
            result["files"] += len(files)
            result["bytes"] += size
    result["held_reasons"] = dict(Counter(row["reason"] for row in result["held"]))
    return result


def purge_session(data_dir, sid, *, now=None, grace_seconds=120):
    """Recheck under a writer lock; never trust a stale dry-run list.

    A durable purging claim precedes filesystem mutation. Interrupted deletion
    is retryable, and render/analysis admission refuses this source meanwhile.
    """
    config = policy(data_dir)
    if config.get("enabled") is not True:
        return {"session_id": sid, "status": "disabled", "bytes": 0, "files": 0}
    now = time.time() if now is None else now
    conn = connect(data_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        session, reason = _eligibility(conn, data_dir, sid, now=now, grace_seconds=grace_seconds,
            discard_terminal_analysis_failures=config.get("discard_terminal_analysis_failures") is True)
        if reason:
            conn.rollback()
            return {"session_id": sid, "status": "held", "reason": reason, "bytes": 0, "files": 0}
        files = raw_files(data_dir, sid)
        previous = session.get("source_retention") or {}
        session["source_retention"] = {"version": VERSION, "mode": "clips_only", "status": "purging",
            "claimed_at": previous.get("claimed_at", now), "updated_at": now,
            "removed_bytes": previous.get("removed_bytes", 0), "removed_files": previous.get("removed_files", 0)}
        if session.get("duration", 0) >= 6 and (session.get("analysis_status") != "complete"
                                               or session.get("final_analysis_status") != "complete"):
            session["source_retention"].update(discarded_without_final_analysis=True,
                final_analysis_status_at_discard=session.get("final_analysis_status"),
                analysis_status_at_discard=session.get("analysis_status"))
        conn.execute("UPDATE sessions SET payload=?,updated=? WHERE id=?",
                     (json.dumps(session, ensure_ascii=False), now, sid))
        conn.commit()
        removed_bytes = removed_files = 0
        error = None
        try:
            for path, _ in files:
                # No traversal of source symlinks, including any introduced
                # after planning. No shutil.rmtree on session directories.
                if path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("source_changed_during_cleanup")
                size = path.stat().st_size
                path.unlink()
                removed_bytes += size
                removed_files += 1
        except (OSError, ValueError) as exc:
            error = type(exc).__name__
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM sessions WHERE id=?", (sid,)).fetchone()
        session = json.loads(row[0])
        state = session["source_retention"]
        state.update(status="partial" if error else "removed", updated_at=time.time(), error=error,
                     removed_bytes=state["removed_bytes"] + removed_bytes,
                     removed_files=state["removed_files"] + removed_files)
        # Preserve journal rows and timestamps, but stop advertising missing
        # files as playable/exportable source to the workbench or detector.
        for row in conn.execute("SELECT id,path,metadata FROM segments WHERE session_id=?", (sid,)).fetchall():
            if not Path(row["path"]).is_file():
                metadata = json.loads(row["metadata"] or "{}")
                metadata.update(retention_removed=True, retention_removed_at=time.time())
                conn.execute("UPDATE segments SET metadata=? WHERE id=?", (json.dumps(metadata), row["id"]))
        if not error:
            session.update(recording_bytes=0, playlist=None, latest_frame=None)
        conn.execute("UPDATE sessions SET payload=?,updated=? WHERE id=?",
                     (json.dumps(session, ensure_ascii=False), time.time(), sid))
        conn.commit()
        return {"session_id": sid, "status": state["status"], "files": removed_files,
                "bytes": removed_bytes, "error": error}
    finally:
        conn.close()
