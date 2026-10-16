"""Resolve portable recording tools without reading settings or launching ffmpeg."""
from types import SimpleNamespace

import pytest

from scripts import patrol_following as patrol_module


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    patrol = patrol_module.Patrol.__new__(patrol_module.Patrol)
    patrol.settings = SimpleNamespace(ffmpeg_bin="ffmpeg")
    patrol.args = SimpleNamespace(base_url="http://127.0.0.1:7862", max_recording_gib=1)
    patrol.owned_sid = "a" * 32
    patrol.visit_dir = tmp_path / "visit"
    patrol.visit_dir.mkdir()
    patrol.visit = {"recordings": []}
    patrol.state = {"recording_bytes": 0}
    patrol.recording = patrol.recording_log = None
    patrol.recording_retry_at = 0
    patrol.recording_url = None
    patrol.log = lambda *args: None
    return patrol, {"playback": {"ready": True,
                    "url": f"/api/media/{patrol.owned_sid}/g1/index.m3u8"}}


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return str(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["environment", "settings", "path"])
async def test_recording_resolves_configured_tool_before_path(recorder, tmp_path, monkeypatch, mode):
    patrol, snapshot = recorder
    configured = executable(tmp_path / "configured-ffmpeg")
    fallback = executable(tmp_path / "bin" / "ffmpeg")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    patrol.settings.ffmpeg_bin = str(tmp_path / "missing")
    if mode == "environment":
        monkeypatch.setenv("FFMPEG_BIN", configured)
    elif mode == "settings":
        patrol.settings.ffmpeg_bin = configured
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        assert not kwargs["stderr"].closed
        return SimpleNamespace(pid=123, returncode=None)

    monkeypatch.setattr(patrol_module.asyncio, "create_subprocess_exec", spawn)
    try:
        await patrol.ensure_recording(snapshot, patrol_module.time.time() + 60)
        assert calls[0][0] == (fallback if mode == "path" else configured)
        assert len(patrol.visit["recordings"]) == 1
    finally:
        if patrol.recording_log:
            patrol.recording_log.close()


@pytest.mark.asyncio
async def test_missing_ffmpeg_does_not_create_log_or_claim_recording(recorder, monkeypatch):
    patrol, snapshot = recorder

    async def forbidden(*args, **kwargs):
        pytest.fail("No process may start when ffmpeg is unavailable")

    monkeypatch.setattr(patrol_module.asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(FileNotFoundError, match="FFMPEG_BIN"):
        await patrol.ensure_recording(snapshot, patrol_module.time.time() + 60)
    assert "PATH" in patrol.visit["recording_error"]
    assert not patrol.visit["recordings"]
    assert patrol.recording_log is None
    assert not (patrol.visit_dir / "recordings").exists()


@pytest.mark.asyncio
async def test_spawn_failure_closes_log_without_claiming_a_recording(recorder, tmp_path, monkeypatch):
    patrol, snapshot = recorder
    monkeypatch.setenv("FFMPEG_BIN", executable(tmp_path / "ffmpeg"))
    opened = []

    async def spawn(*args, **kwargs):
        opened.append(kwargs["stderr"])
        raise OSError("simulated execution failure")

    monkeypatch.setattr(patrol_module.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(OSError):
        await patrol.ensure_recording(snapshot, patrol_module.time.time() + 60)
    assert opened[0].closed
    assert patrol.recording_log is None
    assert patrol.recording is None
    assert not patrol.visit["recordings"]
