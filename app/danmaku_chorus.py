"""Bounded live-chat repetition detection, without retaining audience identity."""
from __future__ import annotations

from collections import Counter
import math
import re
import unicodedata

from .danmaku_corpus import _ABUSE, _INJECTION


WINDOW_SECONDS = 30
MAX_MESSAGES = 150
MIN_COUNT = 4
MIN_SHARE = 0.4
MIN_CHORUS_SHARE = 0.15
MAX_CHORUSES = 3
MIN_KNOWN_USERS = 3

_EMOTE = re.compile(r"\[[^\]]*\]")
_PRIVATE = re.compile(
    r"https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}"
    r"|(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"
    r"|(?:uid|uname|user[_ ]?id|用户名|用户号|微信|微\s*信|手机号|身份证|qq号)\s*[:：=]"
    r"|(?:加我|联系我|私信我|我的微信|我的QQ)|@[^\s，。！？!?]{2,}"
    r"|(?:sessdata|bili_jct|cookie|token|password|passwd|api[_ -]?key|密码|密钥)\s*[:：=]"
    r"|gh[pousr]_[a-z0-9]+|sk-[a-z0-9]{8,}", re.I,
)


def _safe_text(value, max_length):
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        return None
    # A joiner and emoji tag characters can be part of visible emoji. Other
    # invisible format/control characters must not be silently concealed.
    if any(unicodedata.category(char) in {"Cc", "Cs"}
           or (unicodedata.category(char) == "Cf" and char != "\u200d"
               and not "\U000e0020" <= char <= "\U000e007f") for char in value):
        return None
    text = value.strip()
    if not text or len(text.encode("utf-16-le")) // 2 > max_length:
        return None
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).casefold()
    if not normalized or all(unicodedata.category(char).startswith("C") for char in normalized):
        return None
    if any(pattern.search(normalized) for pattern in (_EMOTE, _PRIVATE, _INJECTION, _ABUSE)):
        return None
    return text, normalized


def _known_user(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return None
    value = value.strip()
    if not value or value.casefold() in {"0", "none", "null", "anonymous", "unknown"}:
        return None
    if value.isascii() and value.isdecimal():
        return str(int(value)) if int(value) > 0 else None
    return value


def _detect_choruses(records, *, end, max_length, limit, min_share):
    """Return bounded recent repetitions using a caller-selected share threshold.

    ``timestamp`` is session content time, as in ``snapshot.danmaku``; wall
    clock ``sent_at`` / ``received_at`` must not be substituted. Only the last
    150 raw entries are examined, and only valid messages within 30 seconds
    participate. Normal long comments remain in the denominator even when too
    long to offer as a candidate. If any valid message exposes a user ID, a group
    needs three known distinct users; count-only fallback is reserved for
    all-anonymous windows. User IDs and message IDs never appear in the result.

    Comparison folds Unicode width, case and whitespace, but preserves exact
    laughter lengths, punctuation and wording. The selected spelling is the
    most frequent real original, with latest appearance breaking ties.
    """
    if (not isinstance(records, (list, tuple))
            or isinstance(end, bool) or not isinstance(end, (int, float))
            or not math.isfinite(end) or end < 0
            or isinstance(max_length, bool) or not isinstance(max_length, int)
            or not 1 <= max_length <= 500
            or isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        return []
    limit = min(limit, MAX_CHORUSES)
    cutoff = max(0, end - WINDOW_SECONDS)
    groups, seen_ids = {}, set()
    total, any_known_user = 0, False
    for index, item in enumerate(records[-MAX_MESSAGES:]):
        if not isinstance(item, dict):
            continue
        timestamp = item.get("timestamp")
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp) or not cutoff <= timestamp <= end):
            continue
        cleaned = _safe_text(item.get("text"), 500)
        if cleaned is None:
            continue
        message_id = item.get("message_id")
        if isinstance(message_id, (str, int)) and not isinstance(message_id, bool):
            message_id = str(message_id)
            if message_id and len(message_id) <= 128 and message_id != "0":
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
        text, key = cleaned
        user = _known_user(item.get("user_id"))
        any_known_user |= user is not None
        total += 1
        # Long real discussion still competes with short repetitions. Omitting
        # it would turn four chants among 100 long comments into a false 100%.
        if len(text.encode("utf-16-le")) // 2 > max_length:
            continue
        group = groups.setdefault(key, {"spellings": Counter(), "last_seen": {},
                                        "count": 0, "users": set(), "latest": timestamp})
        group["count"] += 1
        group["spellings"][text] += 1
        group["last_seen"][text] = (timestamp, index)
        group["latest"] = max(group["latest"], timestamp)
        if user is not None:
            group["users"].add(user)

    eligible = [group for group in groups.values()
                if group["count"] >= MIN_COUNT and group["count"] / total >= min_share
                and (not any_known_user or len(group["users"]) >= MIN_KNOWN_USERS)]
    result = []
    for group in sorted(eligible, key=lambda value: (value["count"], value["latest"]),
                        reverse=True)[:limit]:
        text = max(group["spellings"], key=lambda value: (group["spellings"][value],
                                                        group["last_seen"][value]))
        result.append({"text": text, "count": group["count"], "total": total,
                       "window_seconds": WINDOW_SECONDS, "source_time": group["latest"]})
    return result


def detect_choruses(records, *, end, max_length=20, limit=3):
    """Find up to three real phrases repeated by the current audience.

    Each phrase needs at least four occurrences and 15% of valid messages in
    the last 30 seconds / 150 records, plus three known users when IDs exist.
    Results sort by frequency, then most recent content time. Invalid input
    returns an empty list; ``limit`` is always capped at three. ``source_time``
    is the latest real message timestamp in the group, never a refreshed clock.
    """
    return _detect_choruses(records, end=end, max_length=max_length, limit=limit,
                            min_share=MIN_CHORUS_SHARE)


def detect_chorus(records, *, end, max_length=20):
    """Compatibility: one dominant phrase at the original 40% share threshold."""
    result = _detect_choruses(records, end=end, max_length=max_length, limit=1,
                             min_share=MIN_SHARE)
    return {key: value for key, value in result[0].items() if key != "source_time"} if result else None
