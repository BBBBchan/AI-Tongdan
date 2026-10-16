"""Local-helper API and cancellation tests; all platform I/O is mocked."""
import asyncio
from contextlib import suppress
import json
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.bilibili import RoomInfo
from app.config import Settings
from app.danmaku_send import DanmakuCredentials, DanmakuSender
from app.server import create_app


@pytest_asyncio.fixture
async def demo(tmp_path, monkeypatch):
    for key in ("MODEL_BASE_URL", "MODEL_ENDPOINT_FILE", "MODEL_API_KEY", "AUDIO_BASE_URL", "AUDIO_ENDPOINT_FILE"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(Settings(tmp_path, "ffmpeg", audio_enabled=False))
    ws = app.state.workspace
    ws.session = {"id": "b" * 32, "status": "live", "started_at": time.time()}
    ws.room = RoomInfo(732, 0, "模拟直播", 1, 1, "")
    async def never_send(*args, **kwargs):
        pytest.fail("Local mode must never use the server sender")
    monkeypatch.setattr(ws.danmaku_sender, "send", never_send)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        try:
            yield ws, client
        finally:
            ws.local_danmaku.disconnect()
            if ws.danmaku_auto.task:
                ws.danmaku_auto.task.cancel()
                with suppress(asyncio.CancelledError):
                    await ws.danmaku_auto.task
            await ws.client.aclose()
            await ws.danmaku_sender.close()


async def pair(client):
    assert (await client.post("/api/danmaku/transport", json={"mode": "local"})).status_code == 200
    response = await client.post("/api/danmaku/local/pairing", json={})
    assert response.status_code == 200
    code = response.json()["code"]
    connected = await client.post("/api/danmaku/local/connect", json={"code": code})
    assert connected.status_code == 200
    token = connected.json()["token"]
    return {"Authorization": "Bearer " + token}, code, token


async def claim(client, headers):
    for _ in range(100):
        response = await client.post("/api/danmaku/local/poll", headers=headers, json={})
        assert response.status_code == 200
        if response.json()["job"]:
            return response.json()["job"]
        await asyncio.sleep(.005)
    raise AssertionError("job not created")


@pytest.mark.asyncio
async def test_local_unpaired_never_falls_back_or_accepts_cookie_upload(demo):
    ws, client = demo
    assert (await client.post("/api/danmaku/local/pairing", json={})).status_code == 409
    switched = await client.post("/api/danmaku/transport", json={"mode": "local"})
    assert switched.json()["pairing_required"] and not switched.json()["can_send"]
    response = await client.post("/api/danmaku/send", json={"session_id": ws.session["id"], "text": "你好"})
    assert response.status_code == 409 and response.json()["code"] == "local_disconnected"
    auto = await client.post("/api/danmaku/auto/start", json={"session_id": ws.session["id"]})
    assert auto.status_code == 409 and ws.danmaku_auto.task is None
    cookie = await client.post("/api/danmaku/credentials", json={"cookie": "DO-NOT-UPLOAD"})
    assert cookie.status_code == 409 and "DO-NOT-UPLOAD" not in cookie.text
    assert not ws.danmaku_credentials.path.exists()


@pytest.mark.asyncio
async def test_pairing_auth_and_public_state_do_not_leak_secrets(demo):
    ws, client = demo
    headers, code, token = await pair(client)
    replay = await client.post("/api/danmaku/local/connect", json={"code": code})
    assert replay.status_code == 401
    assert (await client.post("/api/danmaku/local/poll", json={})).status_code == 401
    status = (await client.get("/api/state")).json()["danmaku_send"]
    assert status["transport"] == "local" and status["local_connected"] and status["local_paired"]
    public = json.dumps(ws.snapshot()) + json.dumps(list(ws.bus.history))
    assert token not in public and code not in public
    assert json.loads((ws.settings.data_dir / "danmaku_transport.json").read_text()) == {"mode": "local"}
    for file in ws.settings.data_dir.rglob("*"):
        if file.is_file():
            assert token not in file.read_text() and code not in file.read_text()
    revoked = await client.post("/api/danmaku/local/disconnect", json={})
    assert not revoked.json()["local_paired"]
    assert (await client.post("/api/danmaku/local/poll", headers=headers, json={})).status_code == 401


@pytest.mark.asyncio
async def test_manual_local_success_uses_shared_history_once_and_does_not_hold_workspace_lock(demo):
    ws, client = demo
    headers, _, _ = await pair(client)
    pending = asyncio.create_task(client.post("/api/danmaku/send", json={"session_id": ws.session["id"], "text": "好听"}))
    job = await claim(client, headers)
    await asyncio.wait_for(ws.lock.acquire(), .1)
    ws.lock.release()
    assert (await client.get("/api/state")).status_code == 200
    assert (await client.post("/api/danmaku/local/poll", headers=headers, json={})).json() == {"job": None}
    authorized = await client.post("/api/danmaku/local/authorize", headers=headers, json={"job_id": job["id"]})
    assert authorized.json() == {"authorized": True, "valid_for_seconds": 3}
    report = {"job_id": job["id"], "outcome": "accepted", "code": "success", "receipt": {"phase": "send"}}
    for _ in range(2):
        assert (await client.post("/api/danmaku/local/result", headers=headers, json=report)).json() == {"ok": True}
    assert (await asyncio.wait_for(pending, 1)).json()["sent"]
    assert len(ws.sent_danmaku) == 1 and ws.sent_danmaku[0]["source"] == "manual"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["accepted", "unknown"])
async def test_auto_local_without_server_cookie_tracks_receipt_and_quota(demo, monkeypatch, outcome):
    ws, client = demo
    headers, _, _ = await pair(client)
    async def room(_):
        return ws.room
    monkeypatch.setattr(ws.client, "resolve_room", room)
    ws.agent = SimpleNamespace(_observer_snapshot=lambda: {"understanding_age_seconds": 1}, snapshot=lambda: {})
    response = await client.post("/api/danmaku/auto/start", json={"session_id": ws.session["id"], "max_messages": 1})
    assert response.status_code == 200 and not ws.danmaku_credentials.status()["configured"]
    ws.danmaku_candidates = {"session_id": ws.session["id"], "generated_at": time.time(), "style_revision": 0,
                             "sources": ["audience_comments"], "items": [{"text": "好听"}]}
    ws.danmaku_auto.next_due = 0
    # The runner may already have entered its initial warm-up sleep.
    ws.danmaku_auto.task.cancel()
    with suppress(asyncio.CancelledError):
        await ws.danmaku_auto.task
    ws.danmaku_auto.task = asyncio.create_task(ws.danmaku_auto.run())
    job = await claim(client, headers)
    assert (await client.post("/api/danmaku/local/authorize", headers=headers, json={"job_id": job["id"]})).status_code == 200
    report = {"job_id": job["id"], "outcome": outcome,
              "code": "success" if outcome == "accepted" else "send_unknown", "receipt": {}}
    assert (await client.post("/api/danmaku/local/result", headers=headers, json=report)).status_code == 200
    await asyncio.wait_for(ws.danmaku_auto.task, 1)
    assert ws.danmaku_auto.data["sent_count"] == int(outcome == "accepted")
    assert ws.danmaku_auto.data["status"] == ("completed" if outcome == "accepted" else "error")
    assert len(ws.sent_danmaku) == int(outcome == "accepted")
    if ws.sent_danmaku:
        assert ws.sent_danmaku[0]["source"] == "auto"
    assert (await client.post("/api/danmaku/local/poll", headers=headers, json={})).json() == {"job": None}


@pytest.mark.asyncio
async def test_queued_second_send_cannot_escape_room_switch_fence(demo, monkeypatch):
    ws, client = demo
    headers, _, _ = await pair(client)
    sid = ws.session["id"]
    first = asyncio.create_task(client.post("/api/danmaku/send", json={"session_id": sid, "text": "第一条"}))
    old_job = await claim(client, headers)
    second = asyncio.create_task(client.post("/api/danmaku/send", json={"session_id": sid, "text": "第二条"}))
    resolving, release = asyncio.Event(), asyncio.Event()
    async def room(_):
        resolving.set()
        await release.wait()
        raise ValueError("mock invalid room")
    monkeypatch.setattr(ws.client, "resolve_room", room)
    switching = asyncio.create_task(client.post("/api/session", json={"room": "123"}))
    await asyncio.wait_for(resolving.wait(), 1)
    try:
        assert (await asyncio.wait_for(first, 1)).status_code == 409
        assert (await asyncio.wait_for(second, 1)).status_code == 409
        assert ws.pending_session_changes == 1
        assert (await client.post("/api/danmaku/local/poll", headers=headers, json={})).json() == {"job": None}
        assert (await client.post("/api/danmaku/local/authorize", headers=headers, json={"job_id": old_job["id"]})).status_code == 409
    finally:
        release.set()
        await switching
    assert ws.pending_session_changes == 0 and not ws.sent_danmaku


@pytest.mark.asyncio
async def test_overlapping_room_switches_keep_fence_until_last_request_finishes(demo, monkeypatch):
    ws, client = demo
    await pair(client)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = 0
    async def room(_):
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await release[index].wait()
        raise ValueError("mock invalid room")
    monkeypatch.setattr(ws.client, "resolve_room", room)
    first = asyncio.create_task(client.post("/api/session", json={"room": "123"}))
    await entered[0].wait()
    second = asyncio.create_task(client.post("/api/session", json={"room": "456"}))
    for _ in range(20):
        if ws.pending_session_changes == 2:
            break
        await asyncio.sleep(.005)
    assert ws.pending_session_changes == 2
    release[0].set()
    await first
    await entered[1].wait()
    try:
        assert ws.pending_session_changes == 1
        response = await client.post("/api/danmaku/send", json={"session_id": ws.session["id"], "text": "不要发送"})
        assert response.status_code == 409 and response.json()["code"] == "trial_stopped"
    finally:
        release[1].set()
        await second
    assert ws.pending_session_changes == 0


@pytest.mark.asyncio
async def test_stop_waiting_for_global_lock_blocks_new_local_jobs(demo):
    ws, client = demo
    await pair(client)
    await ws.lock.acquire()
    stopping = asyncio.create_task(client.post("/api/session/stop", json={}))
    for _ in range(20):
        if ws.pending_session_changes:
            break
        await asyncio.sleep(.005)
    try:
        response = await client.post("/api/danmaku/send", json={"session_id": ws.session["id"], "text": "不要发送"})
        assert response.status_code == 409 and response.json()["code"] == "trial_stopped"
    finally:
        ws.lock.release()
        await stopping


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_auto_stop_retires_local_job_with_honest_history(demo, monkeypatch, authorized):
    ws, client = demo
    headers, _, _ = await pair(client)
    async def room(_):
        return ws.room
    monkeypatch.setattr(ws.client, "resolve_room", room)
    ws.agent = SimpleNamespace(_observer_snapshot=lambda: {"understanding_age_seconds": 1}, snapshot=lambda: {})
    # Seed after start so the candidate belongs to this explicit activation.
    await ws.danmaku_auto.start(ws.session["id"], max_messages=1)
    ws.danmaku_candidates = {"session_id": ws.session["id"], "generated_at": time.time(), "style_revision": 0,
                             "sources": ["audience_comments"], "items": [{"text": "好听"}]}
    job = await claim(client, headers)
    if authorized:
        assert (await client.post("/api/danmaku/local/authorize", headers=headers, json={"job_id": job["id"]})).status_code == 200
    response = await client.post("/api/danmaku/auto/stop", json={"session_id": ws.session["id"]})
    assert response.status_code == 200
    assert ws.danmaku_auto.data["history"][-1]["status"] == ("unknown" if authorized else "not_sent")
    assert not ws.sent_danmaku
    assert (await client.post("/api/danmaku/local/authorize", headers=headers, json={"job_id": job["id"]})).status_code == 409


@pytest.mark.asyncio
async def test_server_nav_in_progress_is_fenced_when_local_mode_is_selected(demo, tmp_path, monkeypatch):
    ws, client = demo
    nav_started, release = asyncio.Event(), asyncio.Event()
    posts = []
    async def platform(request):
        if request.method == "GET":
            nav_started.set()
            await release.wait()
            return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "wbi_img": {
                "img_url": "https://example.test/" + "a" * 32 + ".png",
                "sub_url": "https://example.test/" + "b" * 32 + ".png"}}})
        posts.append(request)
        return httpx.Response(200, json={"code": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(platform)) as platform_client:
        await ws.danmaku_sender.close()
        ws.danmaku_sender = DanmakuSender(DanmakuCredentials(tmp_path, "SESSDATA=mock; bili_jct=mock"), platform_client)
        pending = asyncio.create_task(client.post("/api/danmaku/send", json={"session_id": ws.session["id"], "text": "好听"}))
        await asyncio.wait_for(nav_started.wait(), 1)
        switching = asyncio.create_task(client.post("/api/danmaku/transport", json={"mode": "local"}))
        for _ in range(20):
            if ws.pending_danmaku_transport_updates:
                break
            await asyncio.sleep(.005)
        assert ws.pending_danmaku_transport_updates == 1
        release.set()
        assert (await asyncio.wait_for(pending, 1)).json()["code"] == "trial_stopped"
        assert (await switching).status_code == 200
        assert not posts and not ws.sent_danmaku


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, '{"mode":"server"}', '{"mode":"local"}', "broken", "{}", '{"mode":[]}', "unreadable"])
async def test_mode_persists_but_damaged_saved_choice_never_falls_back(tmp_path, saved):
    path = tmp_path / "danmaku_transport.json"
    if saved == "unreadable":
        path.mkdir()
    elif saved is not None:
        path.write_text(saved)
    ws = create_app(Settings(tmp_path, "ffmpeg", audio_enabled=False)).state.workspace
    try:
        assert ws.danmaku_transport == ("server" if saved in (None, '{"mode":"server"}') else "local")
        assert not ws.local_danmaku.connected and not ws.local_danmaku.paired
    finally:
        await ws.client.aclose()
        await ws.danmaku_sender.close()
