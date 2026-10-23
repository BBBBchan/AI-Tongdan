from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from clipper.review_repair import (
    MAX_REVIEW_BYTES, MAX_SUBTITLE_BYTES, bounded_validation_retry,
)


@pytest.fixture
def case(tmp_path):
    jid = "0123456789abcdef"
    data = tmp_path / "data"
    folder = data / "exports" / jid
    folder.mkdir(parents=True)
    reason = "editorial_evidence_invalid"
    job = {"id": jid, "status": "complete", "live_publication_review": {
        "status": "held", "reason": reason,
    }}
    report = {"status": "held", "reason": reason, "candidate": {
        "title_body": "被说208千克", "cover_text": "怎么是208千克",
        "caption_ids": [0, 1], "evidence_quote": "二百零八千克，这是怎么回事？",
    }}
    subtitles = {"reviewed_subtitles": [
        {"start": 0., "end": 1., "text": "二百零八千克，"},
        {"start": 1., "end": 2., "text": "这是怎么回事？"},
        {"start": 3., "end": 4., "text": "没有引用的999不能作为数字证据"},
    ]}
    def save():
        (folder / "publication-review.json").write_text(json.dumps(report, ensure_ascii=False))
        (folder / "subtitle-review.json").write_text(json.dumps(subtitles, ensure_ascii=False))
    save()
    calls = []
    def quote_check(ids, quote, captions):
        calls.append(("quote", deepcopy(ids), quote, deepcopy(captions)))
        assert quote == "".join(captions[i]["text"] for i in ids)
    def numeric_check(copy, cited):
        calls.append(("numeric", copy, cited))
        assert "208" in copy and "二百零八" in cited and "999" not in cited
    def retry(version=2):
        return bounded_validation_retry(data, job, version, quote_check, numeric_check)
    return SimpleNamespace(data=data, folder=folder, job=job, report=report, subtitles=subtitles,
                           save=save, calls=calls, quote=quote_check, numeric=numeric_check, retry=retry)


def test_old_quote_failure_is_only_selected_for_full_review_and_changes_nothing(case):
    before_job = deepcopy(case.job)
    paths = list(case.folder.iterdir())
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    assert case.retry()
    assert [call[0] for call in case.calls] == ["quote", "numeric"]
    assert case.job == before_job
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths} == before
    assert set(case.folder.iterdir()) == set(paths)
    assert case.job["live_publication_review"]["status"] == "held"


def test_old_numeric_failure_also_requires_both_checks(case):
    case.job["live_publication_review"]["reason"] = "editorial_numeric_claim_without_evidence"
    case.report["reason"] = "editorial_numeric_claim_without_evidence"
    case.save()
    assert case.retry()
    assert [call[0] for call in case.calls] == ["quote", "numeric"]


@pytest.mark.parametrize("version", [0, -1, None, True, "2", 2., []])
def test_invalid_current_version_does_not_admit(case, version):
    assert not case.retry(version)
    assert not case.calls


@pytest.mark.parametrize("location", ["job", "report"])
@pytest.mark.parametrize("version", [2, 3, 0, True, "1", None])
def test_current_newer_or_malformed_old_versions_are_never_retried(case, location, version):
    target = case.job["live_publication_review"] if location == "job" else case.report
    target["validation_version"] = version
    case.save()
    assert not case.retry()
    assert not case.calls


def test_version_two_can_retry_only_after_new_validator_version(case):
    case.job["live_publication_review"]["validation_version"] = 2
    case.report["validation_version"] = 2
    case.save()
    assert not case.retry(2)
    assert case.retry(3)


@pytest.mark.parametrize("reason", ["source_overlap", "editorial_review_requires_changes", "ValueError", None])
def test_other_failures_never_enter_recovery(case, reason):
    case.job["live_publication_review"]["reason"] = reason
    case.report["reason"] = reason
    case.save()
    assert not case.retry()


@pytest.mark.parametrize("location", ["job", "report"])
@pytest.mark.parametrize("status", ["submitted", "failed", "reviewing", "ready", None])
def test_only_held_records_are_recoverable(case, location, status):
    target = case.job["live_publication_review"] if location == "job" else case.report
    target["status"] = status
    case.save()
    assert not case.retry()


def test_stale_report_from_another_failure_is_not_used(case):
    case.report["reason"] = "editorial_numeric_claim_without_evidence"
    case.save()
    assert not case.retry()


@pytest.mark.parametrize("key", ["title_body", "cover_text", "caption_ids", "evidence_quote"])
def test_incomplete_saved_candidate_cannot_be_assumed_valid(case, key):
    del case.report["candidate"][key]
    case.save()
    assert not case.retry()


@pytest.mark.parametrize("ids", [[], [True], [-1], [999], [0, 0], ["0"], {"id": 0}])
def test_bad_caption_references_are_rejected_before_checker(case, ids):
    case.report["candidate"]["caption_ids"] = ids
    case.save()
    assert not case.retry()
    assert not case.calls


@pytest.mark.parametrize("key,value", [("text", None), ("text", ""), ("start", -1),
                                       ("start", True), ("end", "2"), ("end", float("nan"))])
def test_invalid_subtitles_are_not_treated_as_evidence(case, key, value):
    case.subtitles["reviewed_subtitles"][0][key] = value
    case.save()
    assert not case.retry()


def test_subtitle_ids_are_from_array_positions_not_untrusted_fields(case):
    case.subtitles["reviewed_subtitles"][0]["id"] = 999
    case.save()
    assert case.retry()
    assert case.calls[0][3][0]["id"] == 0


@pytest.mark.parametrize("checker", ["quote", "numeric"])
@pytest.mark.parametrize("failure", [False, ValueError("rejected"), RuntimeError("unavailable")])
def test_both_checker_rejections_and_errors_fail_closed(case, checker, failure):
    def reject(*_):
        if isinstance(failure, Exception):
            raise failure
        return failure
    quote = reject if checker == "quote" else case.quote
    numeric = reject if checker == "numeric" else case.numeric
    assert not bounded_validation_retry(case.data, case.job, 2, quote, numeric)


@pytest.mark.parametrize("jid", ["../0123456789abcdef", "/tmp/0123456789abcdef", "ABCDEF0123456789",
                                 "0123456789abcdeg", "short", None, 42])
def test_untrusted_job_id_cannot_redirect_file_reads(case, jid):
    case.job["id"] = jid
    assert not case.retry()


@pytest.mark.parametrize("filename", ["publication-review.json", "subtitle-review.json"])
@pytest.mark.parametrize("problem", ["missing", "invalid_json", "wrong_top_level", "oversize", "symlink", "fifo"])
def test_missing_malformed_or_unsafe_files_fail_closed(case, tmp_path, filename, problem):
    path = case.folder / filename
    content = path.read_bytes()
    path.unlink()
    if problem == "invalid_json":
        path.write_bytes(b"not-json")
    elif problem == "wrong_top_level":
        path.write_text("[]")
    elif problem == "oversize":
        size = MAX_REVIEW_BYTES if filename == "publication-review.json" else MAX_SUBTITLE_BYTES
        with path.open("wb") as handle:
            handle.seek(size)
            handle.write(b" ")
    elif problem == "symlink":
        outside = tmp_path / filename
        outside.write_bytes(content)
        path.symlink_to(outside)
    elif problem == "fifo":
        os.mkfifo(path)
    assert not case.retry()
    assert not case.calls


@pytest.mark.parametrize("component", ["data", "exports", "job"])
def test_symlinked_directories_are_not_followed(case, tmp_path, component):
    path = {"data": case.data, "exports": case.data / "exports", "job": case.folder}[component]
    outside = tmp_path / ("outside-" + component)
    path.rename(outside)
    path.symlink_to(outside, target_is_directory=True)
    assert not case.retry()


def test_absent_or_non_mapping_job_fails_closed(case):
    for job in [None, [], {}, {"id": "0123456789abcdef"}, {"id": "0123456789abcdef", "live_publication_review": []}]:
        assert not bounded_validation_retry(case.data, job, 2, case.quote, case.numeric)
