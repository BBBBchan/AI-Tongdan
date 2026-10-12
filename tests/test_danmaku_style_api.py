"""Style API, generation invalidation and send-boundary tests without live I/O."""
import asyncio
from contextlib import suppress
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.bilibili import RoomInfo
from app.config import Settings
from app.danmaku_send import DanmakuSender
import app.server as server_module
from app.server import Workspace, create_app

COOKIE = "SESSDATA=style-test-session; bili_jct=style-test-csrf"
NAV = {"code": 0, "data": {"isLogin": True, "wbi_img": {
    "img_url": "https://example.invalid/0123456789abcdef0123456789abcdef.png",
    "sub_url": "https://example.invalid/abcdef0123456789abcdef0123456789.png"}}}


class FakeCorpus:
    """Synthetic in-memory corpus; no filesystem operation even in local mode."""

    def __init__(self, path):
        self.path = path
        self.refresh_calls = 0
        self.select_calls = []
        self.references = ["合成句式参考甲", "合成句式参考乙"]
        self.revision = 0

    def refresh(self):
        self.refresh_calls += 1
        if self.revision == 0:
            self.revision = 1
            return True
        return False

    def snapshot(self):
        return {"status": "ready" if self.revision else "not_loaded",
                "file_count": 1 if self.revision else 0,
                "indexed_count": len(self.references) if self.revision else 0,
                "skipped_files": 0, "updated_at": 1 if self.revision else None,
                "revision": self.revision}

    def select(self, context, style_value, limit=6):
        self.select_calls.append((dict(context), style_value, limit))
        return self.references[:limit] if self.revision else []


class FakeBilibili:
    async def resolve_room(self, _):
        return RoomInfo(room_id=123, short_id=0, title="离线风格测试", owner_uid=1,
                        live_status=1, cover="")

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def workspace_app(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "DanmakuCorpus", FakeCorpus, raising=False)
    for name in ("MODEL_BASE_URL", "MODEL_ENDPOINT_FILE", "MODEL_API_KEY", "MODEL_NAME",
                 "AUDIO_BASE_URL", "AUDIO_ENDPOINT_FILE", "AUDIO_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(Settings(tmp_path, "ffmpeg", adaptive_observation=False, bilibili_cookie=COOKIE))
    ws = app.state.workspace
    await ws.client.aclose()
    ws.client = FakeBilibili()
    ws.session = {"id": "current-session", "status": "live"}
    ws.room = await ws.client.resolve_room(123)
    ws.clock_start = time.monotonic() - 100
    ws.danmaku.append({"timestamp": 99, "text": "刚才这个动作好有趣"})
    ws.agent = SimpleNamespace(danmaku=list(ws.danmaku),
        snapshot=lambda: {"observer": {"last_timestamp": 99}, "observations": []},
        _observer_snapshot=lambda: {"understanding_age_seconds": 2, "active_questions": 0})

    async def never_send(*args, **kwargs):
        pytest.fail("Style changes and candidate generation must not send messages")

    monkeypatch.setattr(ws.danmaku_sender, "send", never_send)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            yield app, ws, client
    finally:
        corpus_task = getattr(ws, "_corpus_task", None)
        if corpus_task:
            with suppress(asyncio.CancelledError):
                await corpus_task
        if ws.danmaku_auto.task:
            ws.danmaku_auto.task.cancel()
            with suppress(asyncio.CancelledError):
                await ws.danmaku_auto.task
        await ws.client.aclose()
        await ws.danmaku_sender.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [True, False, 30.0, 30.5, -1, 101, "30", None])
async def test_style_defaults_to_thirty_and_rejects_invalid_values_without_side_effects(workspace_app, invalid):
    _, ws, client = workspace_app
    initial = (await client.get("/api/state")).json()
    assert initial["danmaku_style"] == {"value": 30, "revision": 0, "mode": "builtin"}
    response = await client.post("/api/danmaku/style", json={
        "session_id": ws.session["id"], "style_value": invalid})
    assert response.status_code == 422
    assert ws.danmaku_style == {"value": 30, "revision": 0, "mode": "builtin"}
    assert ws.pending_danmaku_style_updates == 0
    assert not ws._candidate_refresh.is_set()
    assert not ws.bus.history and not ws.sent_danmaku
    assert not (ws.settings.data_dir / "danmaku_preferences.json").exists()


@pytest.mark.asyncio
async def test_old_session_cannot_change_style_or_invalidate_current_candidates(workspace_app):
    _, ws, client = workspace_app
    candidates = {"items": [{"text": "当前房间的候选"}], "status": "ready", "style_revision": 0}
    ws.danmaku_candidates = candidates
    response = await client.post("/api/danmaku/style", json={"session_id": "old-session", "style_value": 90})
    assert response.status_code == 409
    assert ws.danmaku_style == {"value": 30, "revision": 0, "mode": "builtin"}
    assert ws.danmaku_candidates is candidates
    assert ws.pending_danmaku_style_updates == 0
    assert not ws.bus.history and not ws.sent_danmaku
    assert not (ws.settings.data_dir / "danmaku_preferences.json").exists()


@pytest.mark.asyncio
async def test_changed_style_invalidates_drafts_emits_events_and_persists_but_same_value_is_idempotent(workspace_app):
    _, ws, client = workspace_app
    sid = ws.session["id"]
    candidates = {"items": [{"text": "旧风格候选"}], "status": "ready", "style_revision": 0}
    ws.danmaku_candidates = candidates
    unchanged = await client.post("/api/danmaku/style", json={"session_id": sid, "style_value": 30})
    assert unchanged.status_code == 200 and ws.danmaku_candidates is candidates
    assert not ws.bus.history and not ws._candidate_refresh.is_set()

    changed = await client.post("/api/danmaku/style", json={"session_id": sid, "style_value": 85})
    assert changed.status_code == 200
    assert changed.json()["danmaku_style"] == {"value": 85, "revision": 1, "mode": "builtin"}
    assert changed.json()["danmaku_candidates"] == {"items": [], "status": "waiting", "generated_at": None,
                                                   "session_id": sid, "style_revision": 1}
    assert ws._candidate_refresh.is_set()
    events = list(ws.bus.history)
    assert [entry["type"] for entry in events] == ["danmaku_style", "danmaku_candidates"]
    assert all(entry["session_id"] == sid for entry in events)
    assert events[0]["data"] == {"value": 85, "revision": 1, "mode": "builtin"}
    assert events[1]["data"]["items"] == []
    assert ws.pending_danmaku_style_updates == 0 and not ws.sent_danmaku

    ws._candidate_refresh.clear()
    again = await client.post("/api/danmaku/style", json={"session_id": sid, "style_value": 85})
    assert again.status_code == 200 and again.json() == changed.json()
    assert list(ws.bus.history) == events and not ws._candidate_refresh.is_set()
    assert json.loads((ws.settings.data_dir / "danmaku_preferences.json").read_text()) == {"style_value": 85, "mode": "builtin"}
    rebuilt = Workspace(ws.settings)
    try:
        assert rebuilt.danmaku_style == {"value": 85, "revision": 0, "mode": "builtin"}
        assert rebuilt.danmaku_auto.snapshot()["status"] == "idle"
    finally:
        await rebuilt.client.aclose()
        await rebuilt.danmaku_sender.close()


@pytest.mark.asyncio
async def test_inflight_old_style_is_discarded_and_new_style_wakes_real_candidate_loop(workspace_app):
    _, ws, client = workspace_app
    first_started, release_first = asyncio.Event(), asyncio.Event()
    style_calls = []

    class Model:
        configured = True

        async def complete(self, messages, **kwargs):
            style = json.loads(messages[-1]["content"])["style_value"]
            style_calls.append(style)
            if style == 30:
                first_started.set()
                await release_first.wait()
            return json.dumps({"candidates": [f"风格{style}候选{i}" for i in range(5)]}, ensure_ascii=False)

        complete_background = complete

    ws.model = Model()
    queue = ws.bus.subscribe()

    async def next_ready():
        while True:
            event = await queue.get()
            if event["type"] == "danmaku_candidates" and event["data"]["status"] == "ready":
                return event["data"]

    task = asyncio.create_task(ws._candidate_loop(ws.session["id"]))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        response = await client.post("/api/danmaku/style", json={"session_id": ws.session["id"], "style_value": 90})
        assert response.status_code == 200 and response.json()["danmaku_candidates"]["items"] == []
        release_first.set()
        ready = await asyncio.wait_for(next_ready(), 1)
        assert style_calls == [30, 90]
        assert ready["style_revision"] == 1 and ready["style_value"] == 90
        assert len(ready["items"]) == 5 and all("风格90" in item["text"] for item in ready["items"])
        assert not any(event["type"] == "danmaku_candidates" and event["data"].get("status") == "ready"
                       and event["data"].get("style_value") == 30 for event in ws.bus.history)

        # The loop is now in its normal ~12 s pause. A new style must wake it
        # within this short deadline, without replacing that real wait helper.
        response = await client.post("/api/danmaku/style", json={"session_id": ws.session["id"], "style_value": 20})
        assert response.status_code == 200
        next_drafts = await asyncio.wait_for(next_ready(), 1)
        assert style_calls == [30, 90, 20]
        assert next_drafts["style_revision"] == 2 and next_drafts["style_value"] == 20
        assert not ws.sent_danmaku
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        ws.bus.unsubscribe(queue)


@pytest.mark.asyncio
async def test_style_request_waiting_for_lock_blocks_old_style_at_real_sender_post_boundary(workspace_app, monkeypatch):
    _, ws, client = workspace_app
    auth_started, release_auth = asyncio.Event(), asyncio.Event()
    upstream_calls = []

    async def upstream(request):
        upstream_calls.append(request.method)
        assert request.method == "GET", "A pending style update must prevent the old candidate POST"
        auth_started.set()
        await release_auth.wait()
        return httpx.Response(200, json=NAV)

    async def parked_run():
        await asyncio.Event().wait()

    monkeypatch.setattr(ws.danmaku_auto, "run", parked_run)
    await ws.danmaku_sender.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as platform_client:
        ws.danmaku_sender = DanmakuSender(ws.danmaku_credentials, platform_client)
        started = await client.post("/api/danmaku/auto/start", json={"session_id": ws.session["id"]})
        assert started.status_code == 200
        ws.danmaku_candidates = {"session_id": ws.session["id"], "generated_at": time.time(),
            "style_revision": 0, "status": "ready", "sources": ["visual"], "items": [{"text": "旧风格待发送"}]}

        async def tick_under_lock():
            async with ws.lock:
                await ws.danmaku_auto.tick()

        async def wait_for_pending():
            while ws.pending_danmaku_style_updates != 1:
                await asyncio.sleep(0)

        tick = asyncio.create_task(tick_under_lock())
        style_request = None
        try:
            await asyncio.wait_for(auth_started.wait(), 1)
            style_request = asyncio.create_task(client.post("/api/danmaku/style", json={
                "session_id": ws.session["id"], "style_value": 90}))
            await asyncio.wait_for(wait_for_pending(), 1)
            assert ws.lock.locked() and not style_request.done()
            assert ws.danmaku_style["revision"] == 0
            assert ws.danmaku_auto.choose() is None
            release_auth.set()
            await asyncio.wait_for(tick, 1)
            response = await asyncio.wait_for(style_request, 1)
            assert response.status_code == 200
            assert response.json()["danmaku_style"] == {"value": 90, "revision": 1, "mode": "builtin"}
            assert ws.pending_danmaku_style_updates == 0
            assert upstream_calls == ["GET"] and not ws.sent_danmaku
            status = ws.danmaku_auto.snapshot()
            assert status["status"] == "active" and status["sent_count"] == 0
            assert status["skipped_count"] == 1
            assert status["history"][-1]["status"] == "not_sent"
            assert status["history"][-1]["code"] == "trial_stale"
        finally:
            release_auth.set()
            for task in (tick, style_request):
                if task and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["", "remote", "LOCAL", 1, True, [], {}])
async def test_invalid_mode_is_rejected_without_updating_style_or_drafts(workspace_app, mode):
    _, ws, client = workspace_app
    original = dict(ws.danmaku_style)
    candidates = {"items": [{"text": "仍然有效的候选"}], "status": "ready", "style_revision": 0}
    ws.danmaku_candidates = candidates
    response = await client.post("/api/danmaku/style", json={
        "session_id": ws.session["id"], "style_value": 30, "mode": mode})
    assert response.status_code == 422
    assert ws.danmaku_style == original and ws.danmaku_candidates is candidates
    assert ws.pending_danmaku_style_updates == 0
    assert not ws._candidate_refresh.is_set() and not ws.bus.history
    assert not (ws.settings.data_dir / "danmaku_preferences.json").exists()
    assert not ws.sent_danmaku


@pytest.mark.asyncio
async def test_mode_only_change_invalidates_drafts_and_old_clients_keep_selected_mode(workspace_app):
    _, ws, client = workspace_app
    sid = ws.session["id"]
    ws.danmaku_candidates = {"items": [{"text": "内置旧候选"}], "status": "ready", "style_revision": 0}
    response = await client.post("/api/danmaku/style", json={
        "session_id": sid, "style_value": 30, "mode": "local"})
    assert response.status_code == 200
    assert response.json()["danmaku_style"] == {"value": 30, "revision": 1, "mode": "local"}
    assert response.json()["danmaku_candidates"]["items"] == []
    assert ws._candidate_refresh.is_set()
    if ws._corpus_task:
        await ws._corpus_task
    events = list(ws.bus.history)
    assert [event["type"] for event in events if event["type"] != "danmaku_corpus"] == [
        "danmaku_style", "danmaku_candidates"]
    for event in events:
        if event["type"] == "danmaku_corpus":
            serialized = json.dumps(event["data"], ensure_ascii=False)
            assert not any(text in serialized for text in ws.danmaku_corpus.references)
    assert events[0]["data"]["mode"] == "local"
    assert json.loads((ws.settings.data_dir / "danmaku_preferences.json").read_text()) == {
        "style_value": 30, "mode": "local"}

    ws._candidate_refresh.clear()
    same = await client.post("/api/danmaku/style", json={
        "session_id": sid, "style_value": 30, "mode": "local"})
    assert same.status_code == 200
    assert same.json()["danmaku_style"] == response.json()["danmaku_style"]
    assert same.json()["danmaku_candidates"] == response.json()["danmaku_candidates"]
    assert not ws._candidate_refresh.is_set() and list(ws.bus.history) == events

    # An old UI does not send `mode`; moving its slider must preserve the mode.
    legacy = await client.post("/api/danmaku/style", json={"session_id": sid, "style_value": 70})
    assert legacy.status_code == 200
    assert legacy.json()["danmaku_style"] == {"value": 70, "revision": 2, "mode": "local"}
    assert json.loads((ws.settings.data_dir / "danmaku_preferences.json").read_text()) == {
        "style_value": 70, "mode": "local"}
    rebuilt = Workspace(ws.settings)
    try:
        assert rebuilt.danmaku_style == {"value": 70, "revision": 0, "mode": "local"}
        assert rebuilt.danmaku_auto.snapshot()["status"] == "idle"
    finally:
        await rebuilt.client.aclose()
        await rebuilt.danmaku_sender.close()

    switched_back = await client.post("/api/danmaku/style", json={
        "session_id": sid, "style_value": 70, "mode": "builtin"})
    assert switched_back.status_code == 200
    assert switched_back.json()["danmaku_style"] == {"value": 70, "revision": 3, "mode": "builtin"}
    assert not ws.sent_danmaku and ws.pending_danmaku_style_updates == 0


@pytest.mark.asyncio
async def test_wrong_session_cannot_change_mode(workspace_app):
    _, ws, client = workspace_app
    response = await client.post("/api/danmaku/style", json={
        "session_id": "outdated-room", "style_value": 30, "mode": "local"})
    assert response.status_code == 409
    assert ws.danmaku_style == {"value": 30, "revision": 0, "mode": "builtin"}
    assert not ws._candidate_refresh.is_set() and not ws.bus.history
    assert ws.pending_danmaku_style_updates == 0 and not ws.sent_danmaku


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [
    {"style_value": 45},
    {"style_value": 45, "mode": "invalid"},
    {"style_value": 45, "mode": None},
])
async def test_legacy_or_invalid_saved_mode_uses_portable_default(workspace_app, saved):
    _, ws, _ = workspace_app
    ws.settings.data_dir.mkdir(parents=True, exist_ok=True)
    (ws.settings.data_dir / "danmaku_preferences.json").write_text(json.dumps(saved))
    rebuilt = Workspace(ws.settings)
    try:
        assert rebuilt.danmaku_style == {"value": 45, "revision": 0, "mode": "builtin"}
    finally:
        await rebuilt.client.aclose()
        await rebuilt.danmaku_sender.close()


@pytest.mark.asyncio
async def test_builtin_generation_never_reads_or_selects_local_corpus(workspace_app, monkeypatch):
    _, ws, _ = workspace_app
    ready = asyncio.Event()

    def forbidden(*args, **kwargs):
        pytest.fail("Builtin mode must not read or select any local corpus")

    monkeypatch.setattr(ws.danmaku_corpus, "refresh", forbidden)
    monkeypatch.setattr(ws.danmaku_corpus, "select", forbidden)

    class Model:
        configured = True

        async def complete(self, messages, **options):
            assert json.loads(messages[-1]["content"])["style_mode"] == "builtin"
            return json.dumps({"candidates": ["内置合成候选"]}, ensure_ascii=False)

    original_emit = ws.emit

    async def observe_emit(kind, data):
        await original_emit(kind, data)
        if kind == "danmaku_candidates" and data.get("status") == "ready":
            ready.set()

    monkeypatch.setattr(ws, "emit", observe_emit)
    ws.model = Model()
    task = asyncio.create_task(ws._candidate_loop(ws.session["id"]))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert [item["text"] for item in ws.danmaku_candidates["items"]] == ["内置合成候选"]
        assert not ws.sent_danmaku
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_inflight_mode_switch_discards_old_drafts_and_updates_next_request(workspace_app):
    _, ws, client = workspace_app
    first_started, release_first = asyncio.Event(), asyncio.Event()
    calls = []
    # Populate the in-memory fake before starting the loop. No JSON is accessed.
    ws.danmaku_corpus.refresh()

    class Model:
        configured = True

        async def complete(self, messages, **kwargs):
            context = json.loads(messages[-1]["content"])
            mode = context["style_mode"]
            calls.append(context)
            if mode == "builtin" and len(calls) == 1:
                first_started.set()
                await release_first.wait()
            return json.dumps({"candidates": [f"{mode}模式新候选"]}, ensure_ascii=False)

    ws.model = Model()
    queue = ws.bus.subscribe()

    async def next_ready():
        while True:
            event = await queue.get()
            if event["type"] == "danmaku_candidates" and event["data"].get("status") == "ready":
                return event["data"]

    task = asyncio.create_task(ws._candidate_loop(ws.session["id"]))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        response = await client.post("/api/danmaku/style", json={
            "session_id": ws.session["id"], "style_value": 30, "mode": "local"})
        assert response.status_code == 200
        assert response.json()["danmaku_candidates"]["items"] == []
        release_first.set()
        ready = await asyncio.wait_for(next_ready(), 1)
        assert [call["style_mode"] for call in calls] == ["builtin", "local"]
        assert not calls[0].get("style_references")
        assert calls[1]["style_references"] == ws.danmaku_corpus.references
        assert ready["style_revision"] == 1
        assert ready["items"][0]["text"] == "local模式新候选"
        assert not any(event["type"] == "danmaku_candidates" and event["data"].get("status") == "ready"
                       and event["data"].get("style_revision") == 0 for event in ws.bus.history)

        response = await client.post("/api/danmaku/style", json={
            "session_id": ws.session["id"], "style_value": 30, "mode": "builtin"})
        assert response.status_code == 200
        new_ready = await asyncio.wait_for(next_ready(), 1)
        assert new_ready["style_revision"] == 2
        assert calls[-1]["style_mode"] == "builtin" and not calls[-1].get("style_references")
        assert not ws.sent_danmaku
        public = (await client.get("/api/state")).json()
        corpus_status = public["danmaku_corpus"]
        assert isinstance(corpus_status, dict) and "status" in corpus_status
        assert "references" not in corpus_status
        assert not any(text in json.dumps(public, ensure_ascii=False) for text in ws.danmaku_corpus.references)
    finally:
        release_first.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        ws.bus.unsubscribe(queue)


@pytest.mark.asyncio
async def test_slow_local_indexing_keeps_api_responsive_and_can_switch_back(workspace_app, monkeypatch):
    _, ws, client = workspace_app
    started, release = threading.Event(), threading.Event()
    original_refresh = ws.danmaku_corpus.refresh

    def slow_refresh():
        started.set()
        assert release.wait(3), "Index worker was not released"
        return original_refresh()

    monkeypatch.setattr(ws.danmaku_corpus, "refresh", slow_refresh)
    try:
        response = await asyncio.wait_for(client.post("/api/danmaku/style", json={
            "session_id": ws.session["id"], "style_value": 30, "mode": "local"}), 1)
        assert response.status_code == 200
        assert await asyncio.to_thread(started.wait, 1)
        assert not release.is_set()
        state = (await asyncio.wait_for(client.get("/api/state"), 0.5)).json()
        assert state["danmaku_corpus"]["status"] == "loading"
        switched = await asyncio.wait_for(client.post("/api/danmaku/style", json={
            "session_id": ws.session["id"], "style_value": 30, "mode": "builtin"}), 0.5)
        assert switched.status_code == 200
        assert switched.json()["danmaku_style"]["mode"] == "builtin"
        ws._candidate_refresh.clear()
        assert not ws.sent_danmaku
    finally:
        release.set()
        if ws._corpus_task:
            await asyncio.wait_for(ws._corpus_task, 1)
    assert not ws._candidate_refresh.is_set(), "Old local indexing must not wake builtin generation"
    assert ws.danmaku_corpus.refresh_calls == 1


@pytest.mark.asyncio
async def test_inflight_old_index_revision_is_discarded_after_corpus_refresh(workspace_app):
    _, ws, _ = workspace_app
    ws.danmaku_style["mode"] = "local"
    ws.danmaku_corpus.refresh()
    # Simulate a recently checked in-memory index, isolating the publish race.
    ws._corpus_last_check = time.monotonic()
    first_started, release_first = asyncio.Event(), asyncio.Event()
    calls = []
    old_references = list(ws.danmaku_corpus.references)

    class Model:
        configured = True

        async def complete(self, messages, **options):
            references = json.loads(messages[-1]["content"])["style_references"]
            calls.append(references)
            if len(calls) == 1:
                first_started.set()
                await release_first.wait()
            return json.dumps({"candidates": ["旧索引候选" if len(calls) == 1 else "新索引候选"]}, ensure_ascii=False)

    ws.model = Model()
    queue = ws.bus.subscribe()

    async def next_ready():
        while True:
            event = await queue.get()
            if event["type"] == "danmaku_candidates" and event["data"].get("status") == "ready":
                return event["data"]

    task = asyncio.create_task(ws._candidate_loop(ws.session["id"]))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        ws.danmaku_corpus.references = ["另一组纯合成句式"]
        ws.danmaku_corpus.revision += 1
        ws._candidate_refresh.set()
        release_first.set()
        ready = await asyncio.wait_for(next_ready(), 1)
        assert calls == [old_references, ws.danmaku_corpus.references]
        assert [item["text"] for item in ready["items"]] == ["新索引候选"]
        assert not any(event["type"] == "danmaku_candidates" and event["data"].get("status") == "ready"
                       and any(item["text"] == "旧索引候选" for item in event["data"]["items"])
                       for event in ws.bus.history)
        assert not ws.sent_danmaku
    finally:
        release_first.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        ws.bus.unsubscribe(queue)
