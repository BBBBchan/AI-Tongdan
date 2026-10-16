"""Rolling-frame eviction races use real temporary files, never live sessions."""
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.patrol_following as patrol_module


@pytest.fixture
def archive(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(patrol_module.time, "time", lambda: now)
    # archive_media needs no HTTP client, environment, Cookie or active process.
    patrol = object.__new__(patrol_module.Patrol)
    patrol.settings = SimpleNamespace(data_dir=tmp_path / "data")
    patrol.owned_sid = "synthetic-session"
    patrol.visit_dir = tmp_path / "visit"
    patrol.visit_dir.mkdir()
    patrol.visit = {"last_frame_saved_at": now - 100}
    session = patrol.settings.data_dir / "sessions" / patrol.owned_sid
    frames = session / "agent/frames"
    frames.mkdir(parents=True)
    older, newest = frames / ("a" * 32 + ".jpg"), frames / ("b" * 32 + ".jpg")
    for path, timestamp, content in ((older, 100, b"older-frame-bytes"),
                                     (newest, 200, b"newest-frame-bytes")):
        path.write_bytes(content)
        os.utime(path, (timestamp, timestamp))
    return SimpleNamespace(patrol=patrol, now=now, before=now - 100,
        session=session, older=older, newest=newest, snapshot={"audio": {"segments": []}, "messages": []})


def manifest(env):
    path = env.patrol.visit_dir / "frames.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def assert_archived(env, source, expected):
    target = env.patrol.visit_dir / "evidence/frames" / source.name
    assert target.read_bytes() == expected
    assert env.patrol.visit["last_frame_saved_at"] == env.now
    assert manifest(env) == [{"time": env.now, "file": str(target.relative_to(env.patrol.visit_dir))}]


def assert_no_success(env):
    assert env.patrol.visit["last_frame_saved_at"] == env.before
    assert manifest(env) == []


def test_frame_evicted_after_glob_before_stat_falls_back_to_surviving_frame(archive, monkeypatch):
    original_stat = Path.stat

    def evict_newest(path, *args, **kwargs):
        if path == archive.newest:
            path.unlink(missing_ok=True)
            raise FileNotFoundError(errno.ENOENT, "rolling eviction", str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", evict_newest)
    archive.patrol.archive_media(archive.snapshot)
    assert_archived(archive, archive.older, b"older-frame-bytes")


def test_newest_frame_evicted_before_copy_retries_older_existing_frame(archive, monkeypatch):
    original_copy = patrol_module.shutil.copyfile
    attempted = []

    def evict_on_copy(source, target, *args, **kwargs):
        attempted.append(Path(source))
        if Path(source) == archive.newest:
            Path(source).unlink()
            raise FileNotFoundError(errno.ENOENT, "rolling eviction", str(source))
        return original_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(patrol_module.shutil, "copyfile", evict_on_copy)
    archive.patrol.archive_media(archive.snapshot)
    assert attempted == [archive.newest, archive.older]
    assert_archived(archive, archive.older, b"older-frame-bytes")


@pytest.mark.parametrize("phase", ["stat", "copy"])
def test_all_frames_evicted_leaves_no_success_manifest_or_saved_time(archive, monkeypatch, phase):
    sources = {archive.older, archive.newest}
    if phase == "stat":
        original_stat = Path.stat

        def evict_stat(path, *args, **kwargs):
            if path in sources:
                path.unlink(missing_ok=True)
                raise FileNotFoundError(errno.ENOENT, "rolling eviction", str(path))
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", evict_stat)
    else:
        def evict_copy(source, target, *args, **kwargs):
            Path(source).unlink(missing_ok=True)
            raise FileNotFoundError(errno.ENOENT, "rolling eviction", str(source))

        monkeypatch.setattr(patrol_module.shutil, "copyfile", evict_copy)
    archive.patrol.archive_media(archive.snapshot)
    assert_no_success(archive)


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ENOSPC])
def test_other_copy_errors_never_claim_a_success_even_with_partial_target(archive, monkeypatch, error_number):
    def failed_copy(source, target, *args, **kwargs):
        Path(target).write_bytes(b"partial-copy")
        raise OSError(error_number, "synthetic filesystem failure")

    monkeypatch.setattr(patrol_module.shutil, "copyfile", failed_copy)
    # A genuine I/O error may propagate or be recorded by the caller; neither
    # policy permits recording a successful frame or advancing the throttle.
    try:
        archive.patrol.archive_media(archive.snapshot)
    except OSError as exc:
        assert exc.errno == error_number
    assert_no_success(archive)


def test_other_stat_errors_do_not_create_a_success_manifest(archive, monkeypatch):
    original_stat = Path.stat

    def forbidden_stat(path, *args, **kwargs):
        if path in {archive.older, archive.newest}:
            raise PermissionError(errno.EACCES, "synthetic permission failure", str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", forbidden_stat)
    try:
        archive.patrol.archive_media(archive.snapshot)
    except PermissionError:
        pass
    assert_no_success(archive)


def test_completed_qa_frame_and_audio_copies_still_work_inside_periodic_throttle(archive, monkeypatch):
    audio_id = "c" * 32
    audio = archive.session / "audio" / f"{audio_id}.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"answer-audio-bytes")
    archive.patrol.visit["last_frame_saved_at"] = archive.now - 5
    snapshot = {"audio": {"segments": []}, "messages": [{"role": "assistant", "status": "complete",
        "evidence": [{"frame_id": archive.older.stem}], "audio_evidence": [{"id": audio_id}]}]}
    archive.patrol.archive_media(snapshot)
    assert (archive.patrol.visit_dir / "evidence/frames" / archive.older.name).read_bytes() == b"older-frame-bytes"
    assert (archive.patrol.visit_dir / "evidence/audio" / audio.name).read_bytes() == b"answer-audio-bytes"
    assert archive.patrol.visit["last_frame_saved_at"] == archive.now - 5
    assert manifest(archive) == []

    def unexpected_copy(*args, **kwargs):
        raise AssertionError("Already archived QA evidence should not be recopied")

    monkeypatch.setattr(patrol_module.shutil, "copyfile", unexpected_copy)
    archive.patrol.archive_media(snapshot)


def test_normal_periodic_copy_records_only_the_newest_completed_file(archive):
    archive.patrol.archive_media(archive.snapshot)
    assert_archived(archive, archive.newest, b"newest-frame-bytes")
