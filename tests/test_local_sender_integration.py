"""Full desktop-helper / demo protocol with an entirely mocked Bilibili network."""
import asyncio
from contextlib import asynccontextmanager
import json
import time

import httpx
import pytest

from app.bilibili import RoomInfo
from app.config import Settings
from app.danmaku_send import DanmakuSender
from app.server import create_app
from local_sender import LocalCredentials, LocalSender


COOKIE = "SESSDATA=desktop-only-session; bili_jct=desktop-only-csrf"
SID = "b" * 32
NAV = {"code": 0, "data": {"isLogin": True, "wbi_img": {
    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"}}}


@asynccontextmanager
async def connected_pair(tmp_path, bili_handler):
    app = create_app(Settings(tmp_path / "server", "ffmpeg", audio_enabled=False))
    ws = app.state.workspace
    ws.session = {"id": SID, "status": "live", "started_at": time.time()}
    ws.room = RoomInfo(732, 0, "模拟直播", 1, 1, "")
    async def forbid_server_send(*args, **kwargs):
        raise AssertionError("A local send must never call the server sender")
    ws.danmaku_sender.send = forbid_server_send
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7864") as demo:
        async with httpx.AsyncClient(transport=httpx.MockTransport(bili_handler)) as bili:
            sender = DanmakuSender(LocalCredentials(tmp_path / "desktop", env_cookie=COOKIE), bili)
            helper = LocalSender("http://127.0.0.1:7864", sender, tmp_path / "desktop", demo_client=demo, output=lambda _: None)
            switched = await demo.post("/api/danmaku/transport", json={"mode": "local"})
            assert switched.status_code == 200
            pairing = await demo.post("/api/danmaku/local/pairing", json={})
            assert pairing.status_code == 200
            await helper.connect(pairing.json()["code"])
            try:
                yield ws, demo, helper
            finally:
                ws.local_danmaku.disconnect()
                await helper.close()
                await ws.client.aclose()
                await ws.danmaku_sender.close()


async def next_job(helper):
    for _ in range(100):
        payload = await helper._request("/api/danmaku/local/poll", {})
        if payload.get("job"):
            return payload["job"]
        await asyncio.sleep(.005)
    raise AssertionError("Local send was not delivered to helper")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status,accepted", [
    ({"code": 0, "message": "0"}, 200, True),
    ({"code": 0, "message": "f", "msg": "f"}, 409, False),
    ({"code": 0, "message": "f:您已被禁言"}, 409, False),
])
async def test_desktop_send_roundtrip_and_private_boundary(tmp_path, payload, status, accepted):
    posts = []
    def bili(request):
        assert request.headers["cookie"] == COOKIE
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        posts.append(request)
        return httpx.Response(200, json=payload)
    async with connected_pair(tmp_path, bili) as (ws, demo, helper):
        pending = asyncio.create_task(demo.post("/api/danmaku/send", json={"session_id": SID, "text": "好听好听"}))
        job = await next_job(helper)
        # The desktop waiting path does not own the global workspace lock.
        await asyncio.wait_for(ws.lock.acquire(), timeout=.3)
        ws.lock.release()
        await helper.process_job(job)
        response = await asyncio.wait_for(pending, timeout=2)
        assert response.status_code == status
        assert len(posts) == 1
        assert bool(ws.sent_danmaku) == accepted
        public = (await demo.get("/api/state")).text
        assert "desktop-only" not in public and helper.token not in public
        for path in (tmp_path / "server").rglob("*"):
            if path.is_file():
                saved = path.read_text()
                assert "desktop-only" not in saved and helper.token not in saved
        # Even a replay after a lost acknowledgement never repeats a Bilibili POST.
        await helper.process_job(job)
        assert len(posts) == 1
        assert len(ws.sent_danmaku) == int(accepted)


@pytest.mark.asyncio
async def test_switch_transport_during_desktop_nav_prevents_external_post(tmp_path):
    nav_started, finish_nav = asyncio.Event(), asyncio.Event()
    posts = []
    async def bili(request):
        if request.method == "GET":
            nav_started.set()
            await finish_nav.wait()
            return httpx.Response(200, json=NAV)
        posts.append(request)
        return httpx.Response(200, json={"code": 0})
    async with connected_pair(tmp_path, bili) as (ws, demo, helper):
        pending = asyncio.create_task(demo.post("/api/danmaku/send", json={"session_id": SID, "text": "好听"}))
        job = await next_job(helper)
        processing = asyncio.create_task(helper.process_job(job))
        await asyncio.wait_for(nav_started.wait(), timeout=2)
        switched = await asyncio.wait_for(demo.post("/api/danmaku/transport", json={"mode": "server"}), timeout=2)
        assert switched.status_code == 200
        finish_nav.set()
        # Token revocation may terminate the helper or return a cancelled job.
        result = await asyncio.gather(processing, return_exceptions=True)
        assert len(result) == 1
        response = await asyncio.wait_for(pending, timeout=2)
        assert response.status_code == 409
        assert not posts and not ws.sent_danmaku


@pytest.mark.asyncio
async def test_desktop_post_timeout_stays_unknown_without_resending(tmp_path):
    posts = []
    def bili(request):
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        posts.append(request)
        raise httpx.ReadTimeout("mock timeout", request=request)
    async with connected_pair(tmp_path, bili) as (ws, demo, helper):
        pending = asyncio.create_task(demo.post("/api/danmaku/send", json={"session_id": SID, "text": "好听"}))
        job = await next_job(helper)
        await helper.process_job(job)
        response = await asyncio.wait_for(pending, timeout=2)
        assert response.status_code == 409 and response.json()["code"] == "send_unknown"
        assert ws.local_danmaku.last_receipt["outcome"] == "unknown"
        assert not ws.sent_danmaku
        assert len(posts) == 1
