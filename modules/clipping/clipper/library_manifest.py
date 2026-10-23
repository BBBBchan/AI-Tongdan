"""Safe local-gallery manifest projection and filesystem helpers.

Only explicitly selected public fields are exposed; no remote site credentials,
transport or synchronization state belong in this module.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from .titles import format_clip_title
from .publishing import public_description_from_fields, description_style_for

JOB_ID = re.compile(r"^[0-9a-f]{16}$")


FILES = ("clip.mp4", "cover.jpg", "subtitles.srt", "package.zip")


DEFAULT_WARNING = "自动生成预览，字幕、语音归属、标题及弹幕位置需校对。"


PATROL_STATES = {
    "starting", "watching", "finishing_visit", "between_rooms", "stopped",
    "stopping", "retrying", "waiting_for_workbench", "waiting_for_following_api",
    "waiting_for_live_following", "capacity_reached", "owned_session_missing",
    "reconciling_start", "start_uncertain",
}


class ManifestError(RuntimeError):
    """Contains only a fixed, log-safe error code."""


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def text(value, limit=160):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c >= " " or c == "\n")[:limit]


def positive_int(value):
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) and result >= 0 else default


def timestamp(value):
    try:
        return datetime.fromtimestamp(number(value), timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return "1970-01-01T00:00:00Z"


def eligible_session(session):
    # Manual capture and imported recordings use the same completed-export
    # pipeline as patrol. Capture ownership controls scheduling, not visibility.
    return session.get("mode") in ("live", "file")


def streamer_info(session):
    room = session.get("room") if isinstance(session.get("room"), dict) else {}
    uid = positive_int(session.get("streamer_uid")) or positive_int(room.get("owner_uid"))
    room_id = positive_int(room.get("room_id"))
    name = text(session.get("streamer") or room.get("uname"), 80)
    fallback = f"主播 {uid}" if uid else "导入素材" if session.get("mode") == "file" else "手动直播"
    # Imports need no Bilibili identity. Never substitute a local filename,
    # signed source URL or capture-owner string for missing public information.
    return {"uid": str(uid) if uid else "", "name": name or fallback,
            "room_id": str(room_id) if room_id else "",
            "profile_url": f"https://space.bilibili.com/{uid}" if uid else "",
            "live_url": f"https://live.bilibili.com/{room_id}" if room_id else ""}


def build_manifest(snapshot, patrol, now=None):
    """Build a public allowlist: never copy raw session/job dictionaries."""
    sessions = {s["id"]: s for s in snapshot.get("sessions", [])
                if isinstance(s, dict) and eligible_session(s) and isinstance(s.get("id"), str)}
    publications = {}
    for item in snapshot.get("publications", []):
        if isinstance(item, dict) and item.get("status") == "published":
            bvid = item.get("bvid", "")
            if isinstance(bvid, str) and re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
                publications[item.get("job_id")] = {
                    "status": "published", "bvid": bvid,
                    "url": f"https://www.bilibili.com/video/{bvid}/"}
    clips = []
    for job in snapshot.get("jobs", []):
        if not isinstance(job, dict) or job.get("status") != "complete":
            continue
        # Preserve superseded exports locally for audit, but expose only the
        # replacement in the local gallery. Older jobs have no such field.
        if job.get("superseded_by"):
            continue
        job_id = job.get("id", "")
        session = sessions.get(job.get("session_id"))
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id) or not session:
            continue
        streamer = streamer_info(session)
        if not streamer:
            continue
        anchor = job.get("anchor") if isinstance(job.get("anchor"), dict) else {}
        metadata = job.get("publication_metadata") if isinstance(job.get("publication_metadata"), dict) else {}
        review = job.get("quality_review") if isinstance(job.get("quality_review"), dict) else {}
        review_required = review.get("publish_ready") is not True
        created_at = number(job.get("created_at"))
        live = session.get("mode") == "live"
        captured_at = number(session.get("created_at")) + number(anchor.get("start")) if live else None
        date = datetime.fromisoformat(timestamp(captured_at if live else created_at).replace("Z", "+00:00")).astimezone(
            timezone(timedelta(hours=8))).date().isoformat()
        raw_tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
        tags = [text(tag, 30) for tag in raw_tags if isinstance(tag, str)][:10]
        room = session.get("room") if isinstance(session.get("room"), dict) else {}
        description_fields = {"creator_name": text(session.get("streamer") or room.get("uname"), 80),
                              "homepage": streamer["profile_url"], "live_room": streamer["live_url"]}
        try:
            # Import time says nothing about the source recording's live date.
            if not live:
                raise ValueError("capture date unavailable for imported media")
            created, start = float(session["created_at"]), float(anchor["start"])
            end = float(anchor["end"]) if "end" in anchor else start + float(job.get("duration", 0))
            if not all(math.isfinite(x) for x in (created, start, end)) or created <= 0 or not 0 <= start <= end:
                raise ValueError("invalid capture clock")
            days = [datetime.fromtimestamp(created + offset, timezone(timedelta(hours=8))).date()
                    for offset in (start, end)]
            labels = [f"{day.year}年{day.month}月{day.day}日" for day in days]
            description_fields["capture_date"] = labels[0] if days[0] == days[1] else "—".join(labels)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            pass
        # Never pass arbitrary metadata/profile descriptions through this
        # public allowlist, even though the local editor preserves them.
        profile = session.get("publish_profile") if isinstance(session.get("publish_profile"), dict) else {}
        description = public_description_from_fields(description_fields,
            follow_creator=profile.get("follow_creator", True) is True,
            style=description_style_for(streamer["name"], profile.get("description_style")))
        clips.append({
            "id": job_id,
            "title": format_clip_title(text(metadata.get("title") or anchor.get("title")) or ("直播切片" if live else "素材切片"),
                                       session),
            "streamer": streamer, "date": date, "created_at": timestamp(created_at),
            "captured_at": timestamp(captured_at) if live else "", "duration": round(number(job.get("duration")), 3),
            "score": min(100, round(number(anchor.get("score")))),
            "warning": DEFAULT_WARNING if review_required or job.get("warning") else "",
            "review_required": review_required,
            "status": "review_required" if review_required else "ready",
            "tags": tags or [streamer["name"], "直播切片" if live else "素材切片"], "description": description,
            "source_url": streamer["live_url"],
            "publication": publications.get(job_id),
            "files": {},
        })
    clips.sort(key=lambda c: (c["created_at"], c["id"]), reverse=True)
    visit = patrol.get("current_visit") if isinstance(patrol.get("current_visit"), dict) else {}
    active = sessions.get(visit.get("session_id"), {})
    state = patrol.get("status")
    return {
        "version": 1, "generated_at": timestamp(time.time() if now is None else now),
        "clips": clips,
        "patrol": {
            "status": state if state in PATROL_STATES else "unknown",
            "current_streamer": streamer_info(active) if active else None,
            "live_following_count": max(0, int(number(patrol.get("live_following_count")))),
            "auto_render": active.get("auto_render") is True,
            "auto_publish": active.get("auto_publish") is True,
            "send_danmaku": False,
            "updated_at": timestamp(patrol.get("updated_at")),
        },
        "counts": counts(clips),
    }


def counts(clips):
    return {"clips": len(clips), "streamers": len({c["streamer"]["uid"] or c["streamer"]["name"] for c in clips}),
            "review_required": sum(c["review_required"] for c in clips),
            "ready": sum(not c["review_required"] for c in clips),
            "published": sum(c["publication"] is not None for c in clips)}


def safe_file(export_dir, job_id, name):
    if not JOB_ID.fullmatch(job_id) or name not in FILES:
        raise ManifestError("invalid_export_path")
    root = export_dir.resolve()
    folder = root / job_id
    path = folder / name
    if folder.is_symlink() or path.is_symlink() or path.resolve() != path:
        raise ManifestError("unsafe_export_path")
    try:
        mode = path.stat().st_mode
    except OSError:
        raise ManifestError("export_missing") from None
    if not stat.S_ISREG(mode):
        raise ManifestError("unsafe_export_path")
    return path
