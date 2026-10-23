"""Half-open source ranges prevent repeated footage across all posting paths."""
import math


RESERVED = {"queued", "uploading", "submitting", "submitted", "published", "private", "rejected", "uncertain"}


def capture_span(store, job):
    session = store.session(job.get("session_id"))
    if not session or session.get("mode") not in {"live", "file"}:
        raise ValueError("切片来源记录不完整，暂不投稿")
    if session.get("mode") == "file":
        return None
    room = session.get("room") or {}
    uid = room.get("owner_uid") or session.get("streamer_uid")
    if (not uid or str(uid).isdigit() is False or int(uid) <= 0
            or (room.get("owner_uid") and session.get("streamer_uid")
                and str(room["owner_uid"]) != str(session["streamer_uid"]))):
        raise ValueError("无法确认主播身份，暂不投稿")
    anchor = job.get("anchor") or {}
    try:
        start, end = float(anchor["start"]), float(anchor["end"])
        created = float(session.get("capture_clock_started_at") or session["created_at"])
        if not all(math.isfinite(x) for x in (start, end, created)) or not 0 <= start < end or created <= 0:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("无法确认切片源时间范围，暂不投稿") from None
    # Cross-session clocks describe receipt rather than broadcaster frame time.
    # Reserve an extra minute at either edge for client start/cache uncertainty.
    # Same-session ranges retain exact half-open media-timeline semantics.
    return {"streamer_uid": str(uid), "session_id": job["session_id"], "start": start,
            "end": end, "absolute_start": created + start, "absolute_end": created + end,
            "clock_basis": "capture_receipt" if session.get("capture_clock_started_at") else "legacy_session_creation",
            "cross_session_margin_seconds": 60.0}


def overlaps(left, right):
    if left["streamer_uid"] != right["streamer_uid"]:
        return False
    if left["session_id"] == right["session_id"]:
        return max(left["start"], right["start"]) < min(left["end"], right["end"])
    left_margin = max(60.0, float(left.get("cross_session_margin_seconds", 60.0)))
    right_margin = max(60.0, float(right.get("cross_session_margin_seconds", 60.0)))
    return (max(left["absolute_start"] - left_margin, right["absolute_start"] - right_margin)
            < min(left["absolute_end"] + left_margin, right["absolute_end"] + right_margin))


def validate_no_overlap(store, job, *, publication_id=None, video_sha256=None):
    span = capture_span(store, job)
    for publication in store.publications():
        if publication.get("id") == publication_id or publication.get("status") not in RESERVED:
            continue
        if video_sha256 and publication.get("video_sha256") == video_sha256:
            raise ValueError("相同成片已有投稿记录，已阻止重复投稿")
        if not span:
            continue
        other = publication.get("capture_span")
        if not other:
            old_job = store.job(publication.get("job_id"))
            if not old_job:
                raise ValueError("历史投稿的来源记录缺失，需核实后再投稿")
            other = capture_span(store, old_job)
        if other and overlaps(span, other):
            raise ValueError("切片与已投稿或待投稿片段存在时间重叠，请保留不重叠的完整段落")
    return span
