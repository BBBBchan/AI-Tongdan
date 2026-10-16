"""Offline checks for persistent, transport-independent candidate-only sessions."""
import asyncio
from contextlib import suppress
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from app.bilibili import RoomInfo
from app.config import Settings
from app.danmaku_send import DanmakuSendError
import app.server as server_module
from app.server import Workspace, create_app


REAL_CANDIDATE_LOOP = Workspace._candidate_loop


class OfflineRoom:
    async def resolve_room(self, room):
        return RoomInfo(room_id=213, short_id=0, title="离线候选测试", owner_uid=1,
                        live_status=1, cover="")

    async def aclose(self):
        pass


async def parked(*args, **kwargs):
    await asyncio.Event().wait()


@pytest_asyncio.fixture
async def application(tmp_path, monkeypatch):
    for key in ("MODEL_BASE_URL", "MODEL_ENDPOINT_FILE", "MODEL_API_KEY", "MODEL_NAME",
                "AUDIO_BASE_URL", "AUDIO_ENDPOINT_FILE", "AUDIO_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for name in ("_stream_loop", "_danmaku_loop", "_model_loop", "_candidate_loop", "_chorus_loop"):
        monkeypatch.setattr(Workspace, name, parked)
    monkeypatch.setattr(Workspace, "_request_corpus_refresh", lambda self: None)
    apps = []

    async def make():
        app = create_app(Settings(tmp_path, "unused", audio_enabled=False, adaptive_observation=False))
        ws = app.state.workspace
        await ws.client.aclose()
        ws.client = OfflineRoom()
        monkeypatch.setattr(ws.danmaku_sender, "send", AsyncMock(side_effect=AssertionError("Live send attempted")))
        apps.append(app)
        return app

    yield make
    for app in apps:
        await app.state.workspace.close()


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local")


@pytest.mark.asyncio
async def test_api_defaults_and_requested_candidate_policy_are_persisted(application):
    app = await application()
    ws = app.state.workspace
    async with client_for(app) as client:
        normal = await client.post("/api/session", json={"room": "213"})
        assert normal.status_code == 200
        assert normal.json()["session"]["candidate_count"] == 5
        assert normal.json()["session"]["candidate_only"] is False
        for count in (1, 10):
            response = await client.post("/api/session", json={"room": "213", "candidate_count": count, "candidate_only": True})
            assert response.status_code == 200
            state = response.json()
            assert state["session"]["candidate_count"] == count
            assert state["session"]["candidate_only"] is True
            assert state["danmaku_send"]["can_send"] is False
            assert state["danmaku_send"]["status"] == "candidate_only"
            saved = json.loads((ws.settings.data_dir / "sessions" / state["session"]["id"] / "session.json").read_text())
            assert saved["session"]["candidate_count"] == count
            assert saved["session"]["candidate_only"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("candidate_count", 0), ("candidate_count", 11), ("candidate_count", True),
    ("candidate_count", 5.0), ("candidate_count", "10"), ("candidate_count", None),
    ("candidate_only", "false"), ("candidate_only", 1), ("candidate_only", None),
])
async def test_invalid_policy_does_not_replace_current_session(application, field, value):
    app = await application()
    ws = app.state.workspace
    await ws.start("213", candidate_count=10, candidate_only=True)
    sid = ws.session["id"]
    async with client_for(app) as client:
        response = await client.post("/api/session", json={"room": "213", field: value})
    assert response.status_code == 422
    assert ws.session["id"] == sid and ws.candidate_only


@pytest.mark.asyncio
async def test_resume_preserves_policy_and_never_restores_auto_sender(application):
    first = await application()
    ws = first.state.workspace
    await ws.start("213", candidate_count=10, candidate_only=True)
    sid = ws.session["id"]
    await ws.close()
    second = await application()
    restored = second.state.workspace
    await restored.resume()
    assert restored.session["id"] == sid
    assert restored.session["candidate_count"] == 10 and restored.candidate_only
    assert restored.snapshot()["danmaku_send"]["can_send"] is False
    assert restored.danmaku_auto.task is None
    assert restored.danmaku_auto.snapshot()["status"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("candidate_only", "false"), ("candidate_count", 0)])
async def test_corrupt_saved_policy_fails_closed_on_resume(application, field, value):
    first = await application()
    ws = first.state.workspace
    await ws.start("213", candidate_count=10, candidate_only=True)
    path = ws.settings.data_dir / "sessions" / ws.session["id"] / "session.json"
    await ws.close()
    data = json.loads(path.read_text())
    data["session"][field] = value
    path.write_text(json.dumps(data))
    second = await application()
    restored = second.state.workspace
    await restored.resume()
    assert restored.session is None and restored.resume_error
    assert not restored.danmaku_send_status()["can_send"]
    assert restored.danmaku_auto.task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["server", "local"])
async def test_candidate_only_rejects_manual_auto_and_broker_entrypoints(application, transport):
    app = await application()
    ws = app.state.workspace
    await ws.start("213", candidate_count=10, candidate_only=True)
    ws.session["status"] = "live"
    ws.danmaku_transport = transport
    sid = ws.session["id"]
    async with client_for(app) as client:
        for path, body in (("/api/danmaku/send", {"session_id": sid, "text": "不得发送"}),
                           ("/api/danmaku/auto/start", {"session_id": sid})):
            response = await client.post(path, json=body)
            assert response.status_code == 409
            assert "只生成候选" in response.json()["detail"]
        if transport == "local":
            for path, body in (("/api/danmaku/local/poll", {}),
                               ("/api/danmaku/local/authorize", {"job_id": "old"})):
                response = await client.post(path, json=body)
                assert response.status_code == 409 and response.json()["code"] == "candidate_only"
    with pytest.raises(DanmakuSendError) as direct:
        await ws.danmaku_auto.start(sid)
    assert direct.value.code == "candidate_only"
    with pytest.raises(DanmakuSendError) as enqueue:
        await ws.local_danmaku.send(213, "不得入队", guard=lambda: None, source="manual")
    assert enqueue.value.code == "candidate_only"
    assert ws.local_danmaku._job is None
    assert ws.danmaku_auto.task is None and not ws.sent_danmaku
    ws.danmaku_sender.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_candidate_only_session_cancels_previous_auto_and_local_job(application, monkeypatch):
    app = await application()
    ws = app.state.workspace
    await ws.start("213")
    ws.session["status"] = "live"
    monkeypatch.setattr(ws.danmaku_credentials, "status", lambda: {"configured": True, "max_length": 20})
    previous_auto = ws.danmaku_auto
    monkeypatch.setattr(previous_auto, "run", parked)
    await previous_auto.start(ws.session["id"])
    previous_task = previous_auto.task
    pairing = ws.local_danmaku.pairing()
    token = ws.local_danmaku.connect(pairing["code"])["token"]
    pending = asyncio.create_task(ws.local_danmaku.send(213, "旧草稿", guard=lambda: None, source="manual"))
    await asyncio.sleep(0)
    assert ws.local_danmaku.poll(token)["job"]
    await ws.start("213", candidate_count=10, candidate_only=True)
    with pytest.raises(DanmakuSendError) as cancelled:
        await pending
    assert cancelled.value.code == "trial_stopped"
    assert previous_task.cancelled() and previous_auto.stop_requested
    assert ws.local_danmaku._job is None
    assert ws.danmaku_auto.task is None and ws.danmaku_auto.snapshot()["status"] == "idle"
    ws.danmaku_sender.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_is_rechecked_before_post_and_before_local_claim(application, monkeypatch):
    app = await application()
    ws = app.state.workspace
    await ws.start("213")
    ws.session["status"] = "live"
    posts = []

    async def delayed_auth(room, text, *, before_post):
        ws.session["candidate_only"] = True
        before_post()
        posts.append(text)

    monkeypatch.setattr(ws.danmaku_sender, "send", delayed_auth)
    with pytest.raises(DanmakuSendError) as blocked:
        await ws._send_danmaku_locked(ws.session["id"], "不得发送")
    assert blocked.value.code == "candidate_only" and not posts
    ws.session["candidate_only"] = False
    token = ws.local_danmaku.connect(ws.local_danmaku.pairing()["code"])["token"]
    pending = asyncio.create_task(ws.local_danmaku.send(213, "待领取", guard=lambda: None, source="manual"))
    await asyncio.sleep(0)
    job = ws.local_danmaku._job
    ws.session["candidate_only"] = True
    for call in (lambda: ws.local_danmaku.poll(token), lambda: ws.local_danmaku.authorize(token, job.id)):
        with pytest.raises(DanmakuSendError) as denied:
            call()
        assert denied.value.code == "candidate_only"
    assert not job.delivered and not job.authorized
    ws.local_danmaku.cancel()
    with pytest.raises(DanmakuSendError):
        await pending


@pytest.mark.asyncio
async def test_candidate_loop_passes_count_without_creating_send_jobs(application, monkeypatch):
    app = await application()
    ws = app.state.workspace
    await ws.start("213", candidate_count=10, candidate_only=True)
    ws.session["status"] = "live"
    ws.danmaku.append({"timestamp": 0, "text": "主播正在挥手"})
    await ws.model.close()
    ws.model = SimpleNamespace(configured=True, close=AsyncMock())
    generated = AsyncMock(return_value=[f"离线草稿{i}" for i in range(10)])
    monkeypatch.setattr(server_module, "generate_candidates", generated)
    ready = asyncio.Event()

    async def pause(seconds):
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ws, "_candidate_pause", pause)
    task = asyncio.create_task(REAL_CANDIDATE_LOOP(ws, ws.session["id"]))
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert generated.await_args.kwargs["count"] == 10
        assert ws.danmaku_candidates["requested_count"] == 10
        assert len(ws.danmaku_candidates["items"]) == 10
        assert ws.danmaku_candidates["status"] == "ready"
        assert ws.local_danmaku._job is None and ws.danmaku_auto.task is None
        ws.danmaku_sender.send.assert_not_awaited()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
