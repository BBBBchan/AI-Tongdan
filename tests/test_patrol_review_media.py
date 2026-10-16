"""Offline media export boundaries; no real recordings, network, or ffmpeg."""
import json
from pathlib import Path

import pytest

from scripts import patrol_review_media as media


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
def test_tool_configuration_wins_then_missing_or_nonexecutable_path_uses_path(tmp_path, monkeypatch, name):
    directory = tmp_path / "bin"
    directory.mkdir()
    fallback = directory / name
    fallback.write_text("#!/bin/sh\nexit 0\n")
    fallback.chmod(0o700)
    configured = tmp_path / ("custom-" + name)
    configured.write_text("#!/bin/sh\nexit 0\n")
    configured.chmod(0o700)
    monkeypatch.setenv("PATH", str(directory))
    monkeypatch.setenv(name.upper() + "_BIN", str(configured))
    assert media.tool_path(name) == str(configured)
    configured.chmod(0o600)
    assert media.tool_path(name) == str(fallback)
    configured.unlink()
    assert media.tool_path(name) == str(fallback)


@pytest.mark.parametrize("name", ["ffmpeg", "ffprobe"])
def test_missing_tool_has_actionable_error_without_using_a_personal_fallback(tmp_path, monkeypatch, name):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv(name.upper() + "_BIN", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError, match=name.upper() + "_BIN") as error:
        media.tool_path(name)
    assert "PATH" in str(error.value)


def recording(root, name="part-001.mkv", *, closed=True):
    path = root / "recordings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test-media")
    row = {"file": f"recordings/{name}", "started_at": 10}
    if closed:
        row.update(ended_at=40, exit_code=0)
    return row, path


def test_closed_media_whitelist_excludes_live_recordings_and_private_paths(tmp_path):
    closed, source = recording(tmp_path)
    live, _ = recording(tmp_path, "part-002.mkv", closed=False)
    (tmp_path / "session.json").write_text('{"cookie":"private"}')
    private = {**closed, "file": "session.json"}
    traversal = {**closed, "file": "recordings/../session.json"}
    assert media.closed_recordings(tmp_path, {"recordings": [closed, live, private, traversal]}) == [(closed, source)]


def test_source_file_rejects_symlink_file_and_directory(tmp_path):
    visit = tmp_path / "visit"
    visit.mkdir()
    secret = tmp_path / "outside.mkv"
    secret.write_bytes(b"outside")
    folder = visit / "recordings"
    folder.mkdir()
    link = folder / "part-001.mkv"
    link.symlink_to(secret)
    assert media.source_file(visit, "recordings/part-001.mkv", media.RECORDING) is None
    link.unlink()
    folder.rmdir()
    outside = tmp_path / "external-recordings"
    outside.mkdir()
    (outside / "part-001.mkv").write_bytes(b"outside")
    folder.symlink_to(outside, target_is_directory=True)
    assert media.source_file(visit, "recordings/part-001.mkv", media.RECORDING) is None


def test_originals_are_exact_hardlinks_and_can_be_reused_without_copy(tmp_path, monkeypatch):
    _, source = recording(tmp_path / "visit")
    target = tmp_path / "part-001.mkv"
    monkeypatch.setattr(media.shutil, "copyfile", lambda *a, **k: pytest.fail("Original copy forbidden"))
    media.hardlink_recording(source, target)
    assert source.samefile(target)
    assert source.stat().st_ino == target.stat().st_ino
    media.hardlink_recording(source, target)


def test_failed_hardlink_never_falls_back_to_copy(tmp_path, monkeypatch):
    _, source = recording(tmp_path / "visit")
    target = tmp_path / "part-001.mkv"
    monkeypatch.setattr(media.os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError("cross-device")))
    monkeypatch.setattr(media.shutil, "copyfile", lambda *a, **k: pytest.fail("Original copy forbidden"))
    with pytest.raises(OSError):
        media.hardlink_recording(source, target)
    assert not target.exists()


def test_export_current_visit_is_thumbnail_only_when_recording_is_open(tmp_path):
    run, output, vid = tmp_path / "run", tmp_path / "static", "067-23777594"
    visit = run / "visits" / vid
    live, _ = recording(visit, closed=False)
    (visit / "visit.json").write_text(json.dumps({"recordings": [live]}))
    frames = visit / "evidence/frames"
    frames.mkdir(parents=True)
    image = frames / ("a" * 32 + ".jpg")
    image.write_bytes(b"archived-frame")
    (visit / "frames.jsonl").write_text(json.dumps({"time": 15, "file": image.relative_to(visit).as_posix()}) + "\n")
    result, warnings = media.export_visit(run, output, vid, {}, "never-run", "never-run")
    assert result["thumbnail"] == f"media/{vid}/thumbnail.jpg"
    assert result["preview"] is None
    assert result["recordings"] == []
    assert not warnings
    assert {p.name for p in (output / "media" / vid).iterdir()} == {"thumbnail.jpg"}


def test_output_does_not_follow_symlink_directories(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "static"
    output.mkdir()
    (output / "media").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        media.target_directory(output, "001-213")
    assert not list(outside.iterdir())


def test_archive_index_cannot_select_nonframe_file(tmp_path):
    (tmp_path / "session.json").write_text("private")
    (tmp_path / "frames.jsonl").write_text(json.dumps({"file": "session.json"}) + "\n")
    assert media.archived_thumbnail(tmp_path) is None
