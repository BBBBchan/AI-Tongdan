import asyncio
from dataclasses import replace
import json
from pathlib import Path

import httpx
import pytest

from app.bilibili import RoomInfo
from app.config import Settings
from app.events import EventBus
from app.journal import SessionJournal
from app.server import create_app
from app.danmaku_send import DanmakuSendError


class FakeBilibili:
    def __init__(self):
        self.closed = False

    async def resolve_room(self, value):
        if value == "bad":
            raise ValueError("bad room")
        return RoomInfo(room_id=80397, short_id=510, title="测试房间", owner_uid=1,
                        live_status=0, cover="")

    async def events(self, room_id):
        yield {"type": "connection", "status": "connected"}
        yield {"type": "danmaku", "text": "测试弹幕", "user_name": "观众甲"}
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture
def clean_model_env(monkeypatch):
    for name in ("MODEL_BASE_URL", "MODEL_ENDPOINT_FILE", "MODEL_API_KEY", "MODEL_NAME",
                 "AUDIO_BASE_URL", "AUDIO_ENDPOINT_FILE", "AUDIO_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_danmaku_credentials_private_and_send_bound_to_active_room(tmp_path, clean_model_env):
    app = create_app(Settings(tmp_path, "ffmpeg"))
    workspace = app.state.workspace
    await workspace.client.aclose()
    workspace.client = FakeBilibili()
    calls = []
    async def send(room, text):
        calls.append((room, text))
        return {"sent": True, "message": "Bilibili 已受理发送请求"}
    workspace.danmaku_sender.send = send
    secret = "SESSDATA=private-session-test; bili_jct=private-csrf-test"
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            await workspace.start("510")
            await asyncio.sleep(.02)
            sid = workspace.session["id"]
            saved = await client.post("/api/danmaku/credentials", json={"cookie": secret})
            assert saved.status_code == 200 and saved.json()["configured"]
            assert not saved.json()["can_send"]  # offline room
            assert workspace.danmaku_credentials.path.stat().st_mode & 0o777 == 0o600
            for body in ([], {"cookie": {"token": secret}}, {"cookie": secret + "\n"}):
                invalid = await client.post("/api/danmaku/credentials", json=body)
                assert invalid.status_code == 400 and secret not in invalid.text
            assert workspace.danmaku_credentials.status()["configured"]
            public = (await client.get("/api/state")).text
            assert "private-session-test" not in public and "private-csrf-test" not in public
            wrong = await client.post("/api/danmaku/send", json={"session_id": "old", "text": "测试草稿"})
            assert wrong.status_code == 409 and not calls
            offline = await client.post("/api/danmaku/send", json={"session_id": sid, "text": "测试草稿"})
            assert offline.status_code == 409 and not calls
            workspace.room = replace(workspace.room, live_status=1)
            response = await client.post("/api/danmaku/send", json={"session_id": sid, "text": "测试草稿"})
            assert response.status_code == 200 and response.json()["sent"]
            assert calls == [(80397, "测试草稿")]
            duplicate = await client.post("/api/danmaku/send", json={"session_id": sid, "text": "测试草稿"})
            assert duplicate.status_code == 429 and len(calls) == 1
            workspace.last_danmaku_sent_at = None
            async def rejected(*_):
                raise DanmakuSendError("请更新本人登录状态。", 401, "login_required")
            workspace.danmaku_sender.send = rejected
            expired = await client.post("/api/danmaku/send", json={"session_id": sid, "text": "测试草稿"})
            assert expired.status_code == 401 and workspace.danmaku_send_error
            removed = await client.post("/api/danmaku/credentials", json={"cookie": ""})
            assert not removed.json()["configured"]
            assert not workspace.danmaku_credentials.path.exists()


@pytest.mark.asyncio
async def test_room_lifecycle_invalid_input_and_evidence(tmp_path, clean_model_env):
    app = create_app(Settings(tmp_path, "ffmpeg", room_poll_seconds=.02))
    workspace = app.state.workspace
    await workspace.client.aclose()
    workspace.client = FakeBilibili()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.post("/api/session", json={"room": "510"})
            assert response.status_code == 200
            sid = response.json()["session"]["id"]
            await asyncio.sleep(.05)
            state = (await client.get("/api/state")).json()
            assert state["session"]["status"] == "waiting"
            assert state["danmaku"][0]["text"] == "测试弹幕"
            assert state["danmaku_status"]["status"] == "connected"
            assert state["observations"] == []
            assert not state["playback"]["ready"]
            bad = await client.post("/api/session", json={"room": "bad"})
            assert bad.status_code == 400
            assert workspace.session["id"] == sid
            assert workspace.agent._running
            wrong_sid = await client.post("/api/questions", json={"session_id": "old", "text": "现在是什么？"})
            assert wrong_sid.status_code == 409
            no_model = await client.post("/api/questions", json={"session_id": sid, "text": "现在是什么？"})
            assert no_model.status_code == 503
            frame_id = "a" * 32
            assert (await client.get(f"/api/sessions/{sid}/frames/{frame_id}.jpg")).status_code == 410
            path = tmp_path / "sessions" / sid / "agent" / "frames" / f"{frame_id}.jpg"
            path.write_bytes(b"evidence")
            assert (await client.get(f"/api/sessions/{sid}/frames/{frame_id}.jpg")).content == b"evidence"
            assert (await client.get(f"/api/sessions/old/frames/{frame_id}.jpg")).status_code == 404
            assert (await client.get(f"/api/sessions/{sid}/frames/invalid.jpg")).status_code == 404
            stopped = await client.post("/api/session/stop", json={})
            assert stopped.json()["session"]["status"] == "stopped"
            assert not workspace.agent._running
            assert not workspace.tasks
            assert (await client.post("/api/questions", json={"session_id": sid, "text": "继续"})).status_code == 409
    assert workspace.client.closed


@pytest.mark.asyncio
async def test_endpoint_discovery_preserves_agent_and_cookie_privacy(tmp_path, clean_model_env):
    app = create_app(Settings(tmp_path, "ffmpeg", bilibili_cookie="private-cookie"))
    workspace = app.state.workspace
    await workspace.client.aclose()
    workspace.client = FakeBilibili()
    async with app.router.lifespan_context(app):
        await workspace.start("510")
        agent = workspace.agent
        previous = workspace.model
        key_file = tmp_path / "key"
        key_file.write_text("private-key")
        (tmp_path / "model_endpoint.json").write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1",
            "model": "Qwen3.8-27B-FP8", "api_key_file": str(key_file)}))
        await workspace.ensure_model()
        assert workspace.agent is agent
        assert workspace.model is not previous
        assert agent.model is workspace.model
        assert workspace.model.configured
        public = json.dumps(workspace.snapshot())
        assert "private-key" not in public and "private-cookie" not in public
        assert workspace.safe_error(ValueError("private-key private-cookie")) == "[private] [private]"


@pytest.mark.asyncio
async def test_origin_guard_and_event_history_are_bounded(tmp_path, clean_model_env):
    app = create_app(Settings(tmp_path, "ffmpeg"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            assert (await client.post("/api/session/stop", headers={"Origin": "https://other"})).status_code == 403
            assert (await client.post("/api/session/stop", headers={"Origin": "http://local"})).status_code == 200
    bus = EventBus(history_size=3, queue_size=2)
    queue = bus.subscribe()
    data = {"status": "first"}
    await bus.emit("state", data)
    data["status"] = "changed"
    assert bus.history[0]["data"]["status"] == "first"
    for i in range(5):
        await bus.emit("observation", {"i": i})
    assert len(bus.history) == 3 and queue.qsize() == 2
    assert (await queue.get())["data"]["i"] == 3
    bus.unsubscribe(queue)
    assert not bus.subscribers


def test_observation_journal_rotates_without_unbounded_growth(tmp_path):
    journal = SessionJournal(tmp_path, max_bytes=100, backups=2)
    for number in range(12):
        journal.append("observation", {"number": number, "text": "持续观察"})
    paths = list(tmp_path.glob("events.jsonl*"))
    assert len(paths) == 3
    assert json.loads(journal.path.read_text())["data"]["number"] == 11


def test_broken_endpoint_file_does_not_break_local_state(tmp_path, clean_model_env):
    settings = Settings(tmp_path, "ffmpeg")
    endpoint = tmp_path / "model_endpoint.json"
    endpoint.write_text('{"base_url":')
    assert settings.model_config()["base_url"] == ""
    assert settings.model_error
    endpoint.write_text(json.dumps({"base_url": "http://localhost:8020/v1", "api_key_file": "missing"}))
    assert settings.model_config()["base_url"] == ""
    assert settings.model_error


@pytest.mark.asyncio
async def test_shutdown_closes_resources_when_lifespan_is_interrupted(tmp_path, clean_model_env):
    app = create_app(Settings(tmp_path, "ffmpeg"))
    workspace = app.state.workspace
    await workspace.client.aclose()
    workspace.client = FakeBilibili()
    with pytest.raises(RuntimeError, match="interrupted"):
        async with app.router.lifespan_context(app):
            await workspace.start("732")
            raise RuntimeError("interrupted")
    assert workspace.client.closed
    assert not workspace.tasks
    assert not workspace.agent._running


@pytest.mark.asyncio
async def test_audio_labels_and_evidence_are_session_scoped(tmp_path, clean_model_env):
    from io import BytesIO
    import wave
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 48000)
    app = create_app(Settings(tmp_path, "ffmpeg"))
    workspace = app.state.workspace
    await workspace.client.aclose()
    workspace.client = FakeBilibili()
    async with app.router.lifespan_context(app):
        await workspace.start("732")
        sid = workspace.session["id"]
        await workspace.audio._accept({"wav_bytes": output.getvalue(), "start": 2, "end": 5},
            {"segments": [{"start": 0, "end": 3, "text": "测试替身音频", "embedding": [1] + [0] * 15}]}, 3)
        await workspace.audio._accept({"wav_bytes": output.getvalue(), "start": 5, "end": 8},
            {"segments": [{"start": 0, "end": 3, "text": "第二段确认相同声音", "embedding": [1] + [0] * 15}]}, 3)
        segment = workspace.audio.snapshot()["segments"][0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            assert (await client.get(segment["audio_url"])).content.startswith(b"RIFF")
            route = "/api/audio/segments/" + segment["id"] + "/label"
            assert (await client.post(route, json={"session_id": "old", "role": "host"})).status_code == 409
            assert (await client.post(route, json={"session_id": sid, "role": "host"})).json()["role_basis"] == "manual_segment"
            speaker_route = "/api/audio/speakers/" + segment["speaker_id"] + "/label"
            labelled = await client.post(speaker_route, json={"session_id": sid, "role": "background"})
            assert labelled.status_code == 200
            assert labelled.json()["segments"][0]["role"] == "host"
            memory = workspace.snapshot()["memory"]
            assert memory["speakers"][0]["role"] == "background"
            assert next(s for s in memory["audio"] if s["id"] == segment["id"])["role"] == "host"
            events = [event for event in workspace.bus.history if event["type"] == "memory"]
            assert events[-1]["data"]["speakers"][0]["role"] == "background"
            assert (await client.get(f"/api/sessions/old/audio/{segment['id']}.wav")).status_code == 404
            assert (await client.get(f"/api/sessions/{sid}/audio/{'f' * 32}.wav")).status_code == 410
            assert (await client.post(route, json={"session_id": sid, "role": "invented"})).status_code == 422
            await workspace.stop()
            assert (await client.post(route, json={"session_id": sid, "role": "host"})).status_code == 409
