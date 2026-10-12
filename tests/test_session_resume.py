from io import BytesIO
from dataclasses import replace
import json
import wave

import httpx
import pytest

from app.bilibili import RoomInfo
from app.config import Settings
from app.server import create_app


class OfflineRoom:
    async def resolve_room(self, _):
        return RoomInfo(room_id=23029447, short_id=0, title="测试房间", owner_uid=1,
                        live_status=0, cover="")

    async def events(self, _):
        import asyncio
        await asyncio.Event().wait()
        yield {}

    async def aclose(self):
        pass


async def application(directory, monkeypatch):
    for key in ("MODEL_BASE_URL", "MODEL_ENDPOINT_FILE", "AUDIO_BASE_URL", "AUDIO_ENDPOINT_FILE"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(Settings(directory, "ffmpeg"))
    await app.state.workspace.client.aclose()
    app.state.workspace.client = OfflineRoom()
    return app


async def add_voice(workspace, index, start=1):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 48000)
    for offset in (0, 4):
        await workspace.audio._accept({"wav_bytes": buffer.getvalue(), "start": start + offset,
            "end": start + offset + 3}, {"segments": [{"start": 0, "end": 3,
            "text": "测试用声音片段", "embedding": [int(i == index) for i in range(16)]}]}, 3)
    return workspace.audio.snapshot()["segments"][-1]


@pytest.mark.asyncio
async def test_restart_restores_same_session_bindings_and_explicit_stop_disables_resume(tmp_path, monkeypatch):
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        workspace = app.state.workspace
        await workspace.start("23029447")
        sid = workspace.session["id"]
        clip = await add_voice(workspace, 0)
        assert clip["speaker_id"]
        await workspace.audio.label_speaker(clip["speaker_id"], "host", "用户绑定的主播")
        before = workspace.snapshot()
        assert before["memory"]["speakers"][0]["role"] == "host"
        assert (tmp_path / "active_session.json").exists()
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        workspace = app.state.workspace
        restored = workspace.snapshot()
        assert restored["session"]["id"] == sid
        assert restored["session"]["started_at"] == before["session"]["started_at"]
        assert restored["audio"]["speakers"][0]["label"] == "用户绑定的主播"
        assert restored["memory"]["speakers"][0]["role"] == "host"
        assert any(s["role"] == "host" for s in restored["memory"]["audio"])
        assert (tmp_path / "sessions" / sid / "audio" / (clip["id"] + ".wav")).exists()
        assert not any(word in json.dumps(restored) for word in ('"vector"', '"embedding"'))
        await workspace.stop()
        assert not (tmp_path / "active_session.json").exists()
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        assert app.state.workspace.session is None


@pytest.mark.asyncio
async def test_merge_updates_current_memory_and_respects_manual_clip_source(tmp_path, monkeypatch):
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        workspace = app.state.workspace
        await workspace.start("23029447")
        sid = workspace.session["id"]
        first = await add_voice(workspace, 0)
        second = await add_voice(workspace, 1, 10)
        assert first["speaker_id"] != second["speaker_id"]
        await workspace.audio.label_speaker(first["speaker_id"], "host", "主播参考")
        await workspace.audio.label_speaker(second["speaker_id"], "host")
        await workspace.audio.label_segment(second["id"], "background")
        path = f"/api/audio/speakers/{second['speaker_id']}/merge"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            wrong = await client.post(path, json={"session_id": "wrong", "target_id": first["speaker_id"]})
            assert wrong.status_code == 409
            response = await client.post(path, json={"session_id": sid, "target_id": first["speaker_id"]})
            assert response.status_code == 200
        state = workspace.snapshot()
        assert len(state["memory"]["speakers"]) == 1
        assert state["memory"]["speakers"][0]["label"] == "主播参考"
        clip = next(s for s in state["memory"]["audio"] if s["id"] == second["id"])
        assert clip["speaker_id"] == first["speaker_id"]
        assert clip["role"] == "background" and clip["role_basis"] == "manual_segment"


@pytest.mark.asyncio
async def test_invalid_saved_session_does_not_prevent_app_start(tmp_path, monkeypatch):
    (tmp_path / "active_session.json").write_text('{"session_id":"../outside","room":"732"}')
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        assert app.state.workspace.session is None
        assert app.state.workspace.snapshot()["resume_error"]


@pytest.mark.asyncio
async def test_sent_danmaku_survives_restart_and_migrates_only_accepted_trial_records(tmp_path, monkeypatch):
    app = await application(tmp_path, monkeypatch)

    async def confirmed(room, text, **kwargs):
        return {"sent": True, "text": text}

    async with app.router.lifespan_context(app):
        workspace = app.state.workspace
        await workspace.start("23029447")
        sid = workspace.session["id"]
        workspace.room = replace(workspace.room, live_status=1)
        monkeypatch.setattr(workspace.danmaku_sender, "send", confirmed)
        async with workspace.lock:
            await workspace._send_danmaku_locked(sid, "手动发送测试")
            workspace.last_danmaku_sent_at = None
            await workspace._send_danmaku_locked(sid, "自动发送测试", source="auto")
        before = workspace.snapshot()["sent_danmaku"]
        directory = tmp_path / "sessions" / sid
        # Legacy trial history can also contain pending or uncertain deliveries.
        (directory / "danmaku_auto.private.json").write_text(json.dumps({
            "session_id": sid, "room_id": workspace.room.room_id, "status": "active", "history": [
                {"text": "自动发送测试", "status": "accepted", "sent_at": before[-1]["sent_at"] + .01},
                {"text": "迁移旧记录", "status": "accepted", "sent_at": before[-1]["sent_at"] + .02},
                *[{"text": "不要误记", "status": status, "sent_at": before[-1]["sent_at"]}
                  for status in ("pending", "unknown", "failed", "not_sent")],
            ]}))
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        workspace = app.state.workspace
        restored = workspace.snapshot()["sent_danmaku"]
        assert restored[:2] == before
        assert [item["text"] for item in restored] == ["手动发送测试", "自动发送测试", "迁移旧记录"]
        assert restored[-1]["source"] == "auto" and restored[-1]["timestamp"] >= 0
        assert json.loads((directory / "sent_danmaku.private.json").read_text())["items"] == restored
        assert workspace.danmaku_auto.snapshot()["status"] == "idle"
        assert workspace.danmaku_auto.task is None
        await workspace.start("23029447")
        assert workspace.session["id"] != sid
        assert workspace.snapshot()["sent_danmaku"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session_id", "room_id"])
async def test_sent_history_ignores_records_from_another_session_or_room(tmp_path, monkeypatch, mismatch):
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        workspace = app.state.workspace
        await workspace.start("23029447")
        sid, started = workspace.session["id"], workspace.session["started_at"]
        wrong = {"session_id": sid, "room_id": workspace.room.room_id}
        wrong[mismatch] = "other-session" if mismatch == "session_id" else 732
        directory = tmp_path / "sessions" / sid
        (directory / "sent_danmaku.private.json").write_text(json.dumps({**wrong, "items": [
            {"text": "其他房间手动记录", "source": "manual", "timestamp": 0, "sent_at": started}]}))
        (directory / "danmaku_auto.private.json").write_text(json.dumps({**wrong, "history": [
            {"text": "其他房间自动记录", "status": "accepted", "sent_at": started}]}))
    app = await application(tmp_path, monkeypatch)
    async with app.router.lifespan_context(app):
        assert app.state.workspace.snapshot()["sent_danmaku"] == []
