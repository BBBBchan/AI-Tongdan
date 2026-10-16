"""Session cancellation races, bounded cleanup and stale-event isolation."""
import asyncio
from contextlib import suppress
import time

import httpx
import pytest
import pytest_asyncio

from app.bilibili import RoomInfo, _cancellable_wait
from app.config import Settings
from app.server import create_app


class OfflineClient:
    async def resolve_room(self, room):
        return RoomInfo(room_id=int(room), short_id=0, title="离线房", owner_uid=1,
                        live_status=0, cover="")

    async def events(self, room):
        await asyncio.Event().wait()
        yield {}

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def workspace(tmp_path, monkeypatch):
    for key in ("MODEL_BASE_URL", "MODEL_ENDPOINT_FILE", "AUDIO_BASE_URL", "AUDIO_ENDPOINT_FILE"):
        monkeypatch.delenv(key, raising=False)
    app = create_app(Settings(tmp_path, "unused-ffmpeg", audio_enabled=False))
    ws = app.state.workspace
    await ws.client.aclose()
    ws.client = OfflineClient()
    ws.shutdown_timeout = .025
    ws.shutdown_retry_timeout = .025
    yield app, ws
    await ws.close()


@pytest.mark.asyncio
async def test_completed_receive_does_not_swallow_simultaneous_cancellation():
    """The Python 3.10 wait_for implementation returned recv.result here."""
    incoming = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(_cancellable_wait(incoming, 30))
    await asyncio.sleep(0)
    incoming.set_result(b"a frame arriving at the cancellation boundary")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_receive_timeout_cancels_inner_receive():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def receive():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(asyncio.TimeoutError):
        await _cancellable_wait(receive(), .01)
    assert entered.is_set() and cancelled.is_set()


@pytest.mark.asyncio
async def test_late_danmaku_after_lost_cancellation_is_not_published(workspace):
    _, ws = workspace
    receiving = asyncio.Event()

    async def legacy_events(room):
        receiving.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Model the old wait_for race returning a just-completed recv.
            yield {"type": "danmaku", "text": "old room must not leak"}

    ws.client.events = legacy_events
    await ws.start("101")
    await receiving.wait()
    await ws.stop()
    assert ws.session["status"] == "stopped"
    assert not ws.danmaku
    assert not any(event["type"] == "danmaku" for event in ws.bus.history)
    assert not ws.playback["ready"]


@pytest.mark.asyncio
async def test_uncooperative_task_fails_boundedly_and_retry_can_finish(workspace):
    _, ws = workspace
    await ws.start("101")
    sid, old_agent = ws.session["id"], ws.agent
    entered, release = asyncio.Event(), asyncio.Event()

    async def stubborn():
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(stubborn())
    ws.tasks.append(task)
    await entered.wait()
    try:
        began = time.monotonic()
        with pytest.raises(RuntimeError, match="尚未完全停止"):
            await ws.stop()
        assert time.monotonic() - began < .5
        assert ws.session["status"] == "stopping_failed"
        assert not ws.playback["ready"]
        assert not ws.danmaku_send_status()["can_send"]
        assert task in ws._shutdown_pending
        before = len(ws.bus.history)
        await old_agent.emit("observation", {"id": "stale-observation", "description": "old"})
        assert len(ws.bus.history) == before
        with pytest.raises(RuntimeError, match="尚未完全停止"):
            await ws.start("102")
        assert ws.session["id"] == sid and ws.room.room_id == 101
    finally:
        release.set()
        await task
        await ws.stop()
    assert not ws._shutdown_pending
    await ws.start("102")
    assert ws.session["id"] != sid and ws.room.room_id == 102
    before = len(ws.bus.history)
    await old_agent.emit("observation", {"id": "old-result-after-switch"})
    assert len(ws.bus.history) == before


@pytest.mark.asyncio
async def test_second_cancellation_recovers_task_that_lost_first(workspace):
    _, ws = workspace
    await ws.start("101")
    entered, first_cancel_lost = asyncio.Event(), asyncio.Event()

    async def loses_one_cancel():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancel_lost.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(loses_one_cancel())
    ws.tasks.append(task)
    await entered.wait()
    await ws.stop()
    assert first_cancel_lost.is_set()
    assert task.cancelled()
    assert ws.session["status"] == "stopped"
    assert not ws._shutdown_pending


@pytest.mark.asyncio
async def test_retry_remembers_component_failure_alongside_pending_task(workspace):
    _, ws = workspace
    await ws.start("101")
    entered, release = asyncio.Event(), asyncio.Event()
    closes = []

    async def fails_once():
        closes.append(True)
        if len(closes) == 1:
            raise RuntimeError("simulated cleanup failure")

    async def stubborn():
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    ws.model.close = fails_once
    task = asyncio.create_task(stubborn())
    ws.tasks.append(task)
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="尚未完全停止"):
            await ws.stop()
        assert ws._shutdown_failed and task in ws._shutdown_pending
    finally:
        release.set()
        await task
        await ws.stop()
    assert len(closes) == 2
    assert ws.session["status"] == "stopped"
    assert not ws._shutdown_failed


@pytest.mark.asyncio
async def test_stopping_state_is_published_before_cleanup_waits(workspace):
    _, ws = workspace
    await ws.start("101")
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        entered.set()
        await release.wait()

    ws.model.close = slow_close
    stopping = asyncio.create_task(ws.stop())
    await entered.wait()
    states = [event["data"] for event in ws.bus.history if event["type"] == "state"]
    assert states[-1]["session"]["status"] == "stopping"
    assert not states[-1]["playback"]["ready"]
    release.set()
    await stopping


@pytest.mark.asyncio
async def test_cancelled_stop_request_retains_cleanup_for_retry(workspace):
    _, ws = workspace
    await ws.start("101")
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_close():
        entered.set()
        await release.wait()

    ws.model.close = slow_close
    stopping = asyncio.create_task(ws.stop())
    await entered.wait()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert ws.session["status"] == "stopping_failed"
    assert ws._shutdown_pending
    release.set()
    await ws.stop()
    assert ws.session["status"] == "stopped"


@pytest.mark.asyncio
async def test_failed_stop_route_returns_actionable_error_without_hanging(workspace):
    app, ws = workspace
    await ws.start("101")
    release = asyncio.Event()

    async def slow_close():
        await release.wait()

    ws.model.close = slow_close
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as http:
        try:
            response = await http.post("/api/session/stop")
            assert response.status_code == 503
            assert "再次停止" in response.json()["detail"]
            assert (await http.get("/api/state")).json()["session"]["status"] == "stopping_failed"
        finally:
            release.set()
            await ws.stop()


@pytest.mark.asyncio
async def test_empty_hls_playlist_is_not_reported_as_success(workspace):
    app, ws = workspace
    await ws.start("101")
    sid = ws.session["id"]
    media = ws.settings.data_dir / "sessions" / sid / "media/g1"
    media.mkdir(parents=True)
    playlist = media / "index.m3u8"
    playlist.write_bytes(b"")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as http:
        response = await http.get(f"/api/media/{sid}/g1/index.m3u8")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "2"
        playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nsegment_000001.ts\n")
        assert (await http.get(f"/api/media/{sid}/g1/index.m3u8")).status_code == 200
