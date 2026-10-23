import json
from pathlib import Path
import sqlite3
import time

import pytest

from clipper.recording_retention import connect, plan, policy, purge_session
from clipper.store import Store


@pytest.fixture
def source(tmp_path):
    store = Store(tmp_path)
    session = store.create_session(mode="live", status="stopped", stop_phase="complete", duration=100,
                                   final_analysis_status="complete", analysis_status="complete")
    directory = tmp_path / "sessions" / session["id"]
    media = directory / "media/g1/segment_000001.ts"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"source")
    audio = directory / "audio/piece.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    frame = directory / "frames/frame.jpg"
    frame.parent.mkdir()
    frame.write_bytes(b"frame")
    audit = directory / "audio/checkpoint.json"
    audit.write_text("{}")
    store.add_segment(session["id"], 0, 100, media, {"generation": "g1"})
    anchor = store.add_anchor(session["id"], start=0, end=100, title="test")
    job = store.create_job(anchor["id"], session["id"], anchor=anchor)
    store.update_job(job["id"], status="complete")
    export = tmp_path / "exports" / job["id"]
    export.mkdir(parents=True)
    for name in ("clip.mp4", "cover.jpg", "subtitles.srt", "metadata.json", "package.zip"):
        (export / name).write_bytes(b"keep")
    store.create_publication(job["id"], status="published", aid=1234)
    p = tmp_path / "recording-retention/policy.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"version": 1, "enabled": True, "mode": "clips_only"}))
    yield tmp_path, store, session, media, job, export, audit
    store.db.close()


def test_plan_readonly_apply_preserves_clips_journal_and_imports(source):
    data, store, session, media, job, export, audit = source
    imported = store.create_session(mode="file", status="ready")
    report = plan(data, grace_seconds=0)
    assert report["files"] == 3 and report["bytes"] == 16
    assert media.exists()
    assert report["held_reasons"] == {"imported_source": 1}
    assert len(store.segments(session["id"])) == 1
    before = (store.jobs(), store.anchors(), store.publications())
    result = purge_session(data, session["id"], grace_seconds=0)
    assert result["status"] == "removed" and result["files"] == 3
    assert not media.exists() and audit.exists()
    assert len(list(export.iterdir())) == 5
    assert (store.jobs(), store.anchors(), store.publications()) == before
    assert not store.segments(session["id"])
    assert store.db.execute("SELECT count(*) FROM segments").fetchone()[0] == 1
    assert store.session(session["id"])["source_retention"]["status"] == "removed"
    assert purge_session(data, session["id"], grace_seconds=0)["reason"] == "already_removed"


@pytest.mark.parametrize("changes,reason", [
    ({"status": "live"}, "capture_active_or_unknown"),
    ({"status": "waiting"}, "capture_active_or_unknown"),
    ({"stop_phase": "analyzing"}, "stop_not_finalized"),
    ({"analysis_status": "pending"}, "analysis_active"),
    ({"analysis_source_reserved": True}, "analysis_active"),
    ({"tail_recovery": {"status": "queued"}}, "tail_recovery_active"),
    ({"final_analysis_status": "timeout"}, "final_analysis_incomplete"),
    ({"analysis_status": "分析已停止"}, "final_analysis_incomplete"),
])
def test_protects_work_in_progress_and_retryable_analysis(source, changes, reason):
    data, store, session, media, *_ = source
    store.update_session(session["id"], **changes)
    result = purge_session(data, session["id"], grace_seconds=0)
    assert result["reason"] == reason and media.exists()


@pytest.mark.parametrize("status,reason", [
    ("queued", "job_pending_or_unknown"), ("rendering", "job_pending_or_unknown"),
    ("failed", "failed_job_needs_source"), ("mystery", "job_pending_or_unknown"),
])
def test_rechecks_new_job_after_plan(source, status, reason):
    data, store, session, media, job, *_ = source
    assert plan(data, grace_seconds=0)["eligible"]
    store.update_job(job["id"], status=status)
    assert purge_session(data, session["id"], grace_seconds=0)["reason"] == reason
    assert media.exists()


def test_missing_export_blocks_cleanup(source):
    data, _, session, media, _, export, _ = source
    (export / "clip.mp4").unlink()
    assert purge_session(data, session["id"], grace_seconds=0)["reason"] == "completed_clip_missing"
    assert media.exists()


def _successful_retry(source, *, changes=None, anchor_changes=None):
    data, store, session, _, original, _, _ = source
    store.update_job(original["id"], status="failed", options={
        "resolution": 1080, "aspect": "landscape", "captions": True, "emphasis": True, "cover": True})
    # A manual API retry adds these two defaults to an older automatic job.
    options = {"resolution": 1080, "aspect": "landscape", "captions": True, "emphasis": True,
               "cover": True, "cover_mode": "designed", "focus_region": None, **(changes or {})}
    retry = store.create_job(original["anchor_id"], session["id"],
                             anchor={**original["anchor"], **(anchor_changes or {})}, options=options)
    store.update_job(retry["id"], status="complete")
    export = data / "exports" / retry["id"]
    export.mkdir()
    (export / "clip.mp4").write_bytes(b"successful retry")
    return retry


def test_successful_manual_retry_releases_failed_automatic_job_with_default_options(source):
    data, _, session, media, *_ = source
    retry = _successful_retry(source)
    assert purge_session(data, session["id"], grace_seconds=0)["status"] == "removed"
    assert not media.exists()
    assert (data / "exports" / retry["id"] / "clip.mp4").read_bytes() == b"successful retry"


@pytest.mark.parametrize("changes", [
    {"resolution": 720}, {"captions": False}, {"emphasis": False}, {"cover": False},
    {"aspect": "vertical"}, {"cover_mode": "frame"},
    {"focus_region": {"x": .1, "y": .1, "w": .2, "h": .2, "start": 1, "end": 2}},
    {"future_editor_option": True},
])
def test_retry_with_materially_different_options_keeps_failed_source(source, changes):
    data, _, session, media, *_ = source
    _successful_retry(source, changes=changes)
    assert purge_session(data, session["id"], grace_seconds=0)["reason"] == "failed_job_needs_source"
    assert media.exists()


def test_post_render_publication_copy_review_does_not_keep_failed_source(source):
    data, store, session, media, original, *_ = source
    retry = _successful_retry(source, anchor_changes={
        "title": "审核优化后的标题", "title_variants": ["审核优化后的标题"], "cover_text": "改好的封面文案"})
    before = (store.job(original["id"]), store.job(retry["id"]))
    assert purge_session(data, session["id"], grace_seconds=0)["status"] == "removed"
    assert not media.exists()
    assert (store.job(original["id"]), store.job(retry["id"])) == before


@pytest.mark.parametrize("changes", [
    {"start": 1}, {"end": 99}, {"status": "exported"}, {"style": "clean"},
    {"timestamp": 10}, {"focus_event_ids": ["another-message"]},
    {"evidence_ids": ["different-evidence"]}, {"reason": "不同的选段依据"},
    {"future_editor_choice": "new"},
])
def test_public_copy_exception_keeps_every_other_anchor_field_strict(source, changes):
    data, _, session, media, *_ = source
    _successful_retry(source, anchor_changes=changes)
    assert purge_session(data, session["id"], grace_seconds=0)["reason"] == "failed_job_needs_source"
    assert media.exists()


def test_policy_and_settling_are_required(source):
    data, _, session, media, *_ = source
    assert purge_session(data, session["id"])["reason"] == "settling"
    (data / "recording-retention/policy.json").unlink()
    assert not policy(data)["enabled"]
    assert purge_session(data, session["id"], grace_seconds=0)["status"] == "disabled"
    assert media.exists()


def test_source_claim_blocks_new_render_and_analysis(source):
    data, store, session, _, job, *_ = source
    # A durable claim covers the whole filesystem phase and survives a crash.
    store.update_session(session["id"], source_retention={"status": "purging"})
    with pytest.raises(ValueError, match="原录播缓冲已清理"):
        store.create_job(job["anchor_id"], session["id"], anchor=job["anchor"])
    with pytest.raises(ValueError, match="原录播缓冲已清理"):
        store.reserve_analysis(session["id"])
    assert purge_session(data, session["id"], grace_seconds=0)["status"] == "removed"


def test_pending_analysis_admission_prevents_cleanup(source):
    data, store, session, media, *_ = source
    store.reserve_analysis(session["id"])
    # Detector can publish complete before the calling task has queued exports.
    store.update_session(session["id"], analysis_status="complete")
    assert purge_session(data, session["id"], grace_seconds=0)["reason"] == "analysis_active"
    assert media.exists()


def test_explicit_terminal_analysis_discard_is_auditable(source):
    data, store, session, media, *_ = source
    store.update_session(session["id"], final_analysis_status="timeout", analysis_status="分析已停止")
    assert not plan(data, grace_seconds=0)["eligible"]
    assert plan(data, grace_seconds=0, discard_terminal_analysis_failures=True)["eligible"][0]["discarded_without_final_analysis"]
    p = data / "recording-retention/policy.json"
    p.write_text(json.dumps({"version": 1, "mode": "clips_only", "enabled": True,
                             "discard_terminal_analysis_failures": True}))
    assert purge_session(data, session["id"], grace_seconds=0)["status"] == "removed"
    state = store.session(session["id"])["source_retention"]
    assert state["discarded_without_final_analysis"] is True
    assert state["final_analysis_status_at_discard"] == "timeout"
    assert not media.exists()


@pytest.mark.parametrize("changes", [
    {"analysis_status": "pending"}, {"analysis_status": "analyzing"},
    {"analysis_source_reserved": True}, {"final_analysis_status": "running"},
    {"tail_recovery": {"status": "queued"}},
])
def test_discard_policy_cannot_override_active_work(source, changes):
    data, store, session, media, *_ = source
    p = data / "recording-retention/policy.json"
    p.write_text(json.dumps({"version": 1, "mode": "clips_only", "enabled": True,
                             "discard_terminal_analysis_failures": True}))
    store.update_session(session["id"], **changes)
    assert purge_session(data, session["id"], grace_seconds=0)["status"] == "held"
    assert media.exists()


def test_symlinks_never_delete_export_or_external_target(source):
    data, store, session, media, _, export, _ = source
    link = media.parent / "segment_000002.ts"
    link.symlink_to(export / "clip.mp4")
    assert purge_session(data, session["id"], grace_seconds=0)["status"] == "removed"
    assert link.is_symlink() and (export / "clip.mp4").read_bytes() == b"keep"


def test_readonly_connect_does_not_run_store_startup(source):
    data, store, session, _, job, *_ = source
    store.update_session(session["id"], status="live")
    store.update_job(job["id"], status="rendering")
    plan(data, grace_seconds=0)
    assert store.session(session["id"])["status"] == "live"
    assert store.job(job["id"])["status"] == "rendering"
