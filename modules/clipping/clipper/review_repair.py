"""Read-only admission for one full re-review after a validation rule changes.

Passing this check is not publication approval. The caller must still run its
normal complete media, editorial, identity, duplication and overlap checks.
No review files or job state are modified here.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import stat


REPAIRABLE_REASONS = frozenset({
    "editorial_evidence_invalid", "editorial_numeric_claim_without_evidence",
})
MAX_REVIEW_BYTES = 256 * 1024
MAX_SUBTITLE_BYTES = 8 * 1024 * 1024
MAX_CAPTIONS = 10000


def _version(value):
    return value if type(value) is int and value >= 1 else None


def _invalid_constant(_value):
    raise ValueError("non_finite_json")


def _read_object(directory_fd, filename, limit):
    # Opening relative to pinned directory descriptors also prevents symlink
    # replacement of exports/job directories between inspection and reading.
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                 dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError("review_file_invalid")
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise ValueError("review_file_too_large")
    result = json.loads(content.decode("utf-8"), parse_constant=_invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("review_object_required")
    return result


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def bounded_validation_retry(data_dir, job, validation_version, quote_checker, numeric_checker) -> bool:
    """Whether an old held candidate warrants a *new complete* editorial review.

    Only known validation failures from an older validator version qualify.
    Both current local checkers must accept the saved candidate. Missing or
    malformed evidence fails closed. Checkers use the validation convention:
    return None/True on success, raise an exception or return False on failure.
    The caller owns the retry bound and must stamp the current version on every
    full re-review attempt, including failed attempts.
    """
    try:
        if not isinstance(job, dict) or _version(validation_version) is None:
            return False
        jid = job.get("id")
        if not isinstance(jid, str) or not re.fullmatch(r"[a-f0-9]{16}", jid):
            return False
        current = job.get("live_publication_review")
        if not isinstance(current, dict) or current.get("status") != "held":
            return False
        reason = current.get("reason")
        previous_version = _version(current.get("validation_version", 1))
        if (not isinstance(reason, str) or reason not in REPAIRABLE_REASONS
                or previous_version is None or previous_version >= validation_version):
            return False

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as stack:
            parent_fd = os.open(Path(data_dir), flags)
            stack.callback(os.close, parent_fd)
            for component in ("exports", jid):
                parent_fd = os.open(component, flags, dir_fd=parent_fd)
                stack.callback(os.close, parent_fd)
            report = _read_object(parent_fd, "publication-review.json", MAX_REVIEW_BYTES)
            subtitle_report = _read_object(parent_fd, "subtitle-review.json", MAX_SUBTITLE_BYTES)

        report_version = _version(report.get("validation_version", 1))
        if (report.get("status") != "held" or report.get("reason") != reason
                or report_version is None or report_version >= validation_version):
            return False
        candidate = report.get("candidate")
        if not isinstance(candidate, dict):
            return False
        ids, quote = candidate.get("caption_ids"), candidate.get("evidence_quote")
        title, cover = candidate.get("title_body"), candidate.get("cover_text")
        if (not isinstance(title, str) or not title.strip() or len(title) > 1000
                or not isinstance(cover, str) or not cover.strip() or len(cover) > 1000
                or not isinstance(quote, str) or len(quote.strip()) < 2 or len(quote) > 16000
                or not isinstance(ids, list) or not ids or len(ids) > MAX_CAPTIONS
                or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids)):
            return False
        rows = subtitle_report.get("reviewed_subtitles")
        if not isinstance(rows, list) or not rows or len(rows) > MAX_CAPTIONS:
            return False
        captions = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                return False
            start, end, text = row.get("start"), row.get("end"), row.get("text")
            if (not _number(start) or not _number(end) or not 0 <= start <= end
                    or not isinstance(text, str) or not text.strip() or len(text) > 16000):
                return False
            captions.append({"id": index, "start": start, "end": end, "text": text})
        if any(i < 0 or i >= len(captions) for i in ids):
            return False
        cited_text = "\n".join(captions[i]["text"] for i in ids)
        if quote_checker(ids, quote, captions) is False:
            return False
        if numeric_checker(title + "\n" + cover, cited_text) is False:
            return False
        return True
    except Exception:
        # This is an optional recovery path: malformed files, checker errors or
        # unavailable filesystem features must never authorize a retry.
        return False
