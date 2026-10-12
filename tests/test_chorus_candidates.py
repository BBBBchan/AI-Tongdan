"""Live chorus candidates use synthetic chats and never call a send endpoint."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import app.server as server_module
from app.config import Settings
from app.model import ModelBusy
from app.server import Workspace


@pytest_asyncio.fixture
async def live(tmp_path, monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(server_module, "time", SimpleNamespace(
        monotonic=lambda: clock.value, time=lambda: 1000 + clock.value))
    workspace = Workspace(Settings(tmp_path, "ffmpeg"))
    workspace.clock_start = 0
    workspace.session = {"id": "current", "status": "live"}
    workspace.room = SimpleNamespace(live_status=1, room_id=123)
    observer = {"understanding_age_seconds": 1, "active_questions": 0}
    workspace.agent = SimpleNamespace(_observer_snapshot=lambda: observer,
                                      danmaku=workspace.danmaku)
    workspace.model = SimpleNamespace(configured=True, background_available=True)
    workspace.danmaku_candidates = {
        "items": [{"id": str(index), "text": f"已有草稿{index}"} for index in range(5)],
        "status": "ready", "generated_at": 800, "source_time": 80,
        "session_id": "current", "style_revision": 0}
    monkeypatch.setattr(workspace, "snapshot", lambda: {
        "observer": {"last_timestamp": clock.value}, "room": {"title": "合成测试"},
        "observations": [{"timestamp": clock.value, "description": "角色正在挥手"}],
        "audio": {"segments": []}, "danmaku": list(workspace.danmaku)})
    monkeypatch.setattr(workspace, "_request_corpus_refresh", lambda **kwargs: None)
    emissions = []

    async def emit(kind, data):
        emissions.append((kind, copy.deepcopy(data)))

    monkeypatch.setattr(workspace, "emit", emit)
    send = AsyncMock(side_effect=AssertionError("Chorus detection cannot send a message"))
    monkeypatch.setattr(workspace.danmaku_sender, "send", send)
    pauses, ticks = asyncio.Queue(), asyncio.Queue()

    async def wait_on(queue, seconds):
        gate = asyncio.get_running_loop().create_future()
        await queue.put((seconds, gate))
        await gate

    async def pause(seconds):
        await wait_on(pauses, seconds)

    async def sleep(seconds):
        await wait_on(ticks, seconds)

    monkeypatch.setattr(workspace, "_candidate_pause", pause)
    monkeypatch.setattr(server_module, "asyncio", SimpleNamespace(
        sleep=sleep, wait_for=asyncio.wait_for, CancelledError=asyncio.CancelledError))
    tasks = []

    def spawn(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    def seed(text="好耶", timestamp=None):
        workspace.danmaku.extend({"text": text, "timestamp": clock.value if timestamp is None else timestamp,
                                 "user_id": index + 1, "message_id": f"{text}-{index}"}
                                for index in range(4))

    try:
        yield SimpleNamespace(workspace=workspace, clock=clock, observer=observer,
            seed=seed, spawn=spawn, pauses=pauses, ticks=ticks, emissions=emissions, send=send)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        send.assert_not_awaited()
        await workspace.client.aclose()
        await workspace.danmaku_sender.close()


async def next_pause(queue):
    return await asyncio.wait_for(queue.get(), 1)


def assert_chorus(workspace, text="好耶"):
    state = workspace.danmaku_chorus
    assert state["items"][0]["text"] == text
    assert state["items"][0]["kind"] == "chorus"
    assert len(state["items"]) <= 3
    assert [item["text"] for item in state["items"]].count(text) == 1
    assert state["items"][0]["count"] == 4
    assert state["items"][0]["source_time"] >= 0
    assert state["session_id"] == "current"
    assert "user_id" not in json.dumps(state)
    assert "user_name" not in json.dumps(state)


@pytest.mark.asyncio
async def test_real_chat_ingestion_adds_chorus_without_model_or_send(live, monkeypatch):
    workspace = live.workspace
    workspace.model = None

    async def events(room_id):
        assert room_id == 123
        for index in range(4):
            yield {"type": "danmaku", "text": "好耶", "user_id": index + 1,
                   "message_id": str(index), "user_name": "合成用户"}

    monkeypatch.setattr(workspace.client, "events", events)
    await workspace._danmaku_loop("current")
    assert_chorus(workspace)
    assert len(workspace.danmaku_candidates["items"]) == 5
    assert workspace.danmaku_candidates["generated_at"] == 800
    assert workspace.danmaku_candidates["source_time"] == 80
    assert not any(kind == "danmaku_candidates" for kind, _ in live.emissions)
    assert any(kind == "danmaku_chorus" and data["items"] for kind, data in live.emissions)


@pytest.mark.asyncio
async def test_independent_ticker_expires_chorus_and_preserves_model_age(live):
    workspace = live.workspace
    live.seed()
    live.spawn(workspace._chorus_loop("current"))
    seconds, gate = await next_pause(live.ticks)
    assert seconds == 1
    assert_chorus(workspace)
    first_id = workspace.danmaku_chorus["items"][0]["id"]
    await workspace._refresh_chorus("current")
    assert workspace.danmaku_chorus["items"][0]["id"] == first_id
    assert len(live.emissions) == 1
    live.clock.value = 130.001
    gate.set_result(None)
    await next_pause(live.ticks)
    assert not workspace.danmaku_chorus["items"]
    assert len(workspace.danmaku_candidates["items"]) == 5
    assert workspace.danmaku_candidates["generated_at"] == 800
    assert workspace.danmaku_candidates["source_time"] == 80
    assert len(live.emissions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [False, True])
async def test_busy_model_and_questions_cannot_block_chorus_refresh(live, monkeypatch, question):
    workspace = live.workspace
    live.observer.update(understanding_age_seconds=40, active_questions=int(question))
    generate = AsyncMock(side_effect=AssertionError("Busy model must not be called"))
    monkeypatch.setattr(server_module, "generate_candidates", generate)
    live.seed()
    live.spawn(workspace._chorus_loop("current"))
    _, gate = await next_pause(live.ticks)
    live.spawn(workspace._candidate_loop("current"))
    seconds, _ = await next_pause(live.pauses)
    assert seconds == 2
    assert workspace.danmaku_candidates["status"] == "deferred"
    assert_chorus(workspace)
    live.clock.value = 131
    gate.set_result(None)
    await next_pause(live.ticks)
    assert not workspace.danmaku_chorus["items"]
    assert workspace.danmaku_candidates["generated_at"] == 800
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_model_still_publishes_chorus(live, monkeypatch):
    workspace = live.workspace
    workspace.model.configured = False
    generate = AsyncMock(side_effect=AssertionError("Unconfigured model must not be called"))
    monkeypatch.setattr(server_module, "generate_candidates", generate)
    live.seed()
    live.spawn(workspace._chorus_loop("current"))
    await next_pause(live.ticks)
    live.spawn(workspace._candidate_loop("current"))
    seconds, _ = await next_pause(live.pauses)
    assert seconds == 3
    assert_chorus(workspace)
    assert len(workspace.danmaku_candidates["items"]) == 0
    assert workspace.danmaku_candidates["generated_at"] is None
    generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error, status", [(ModelBusy(), "deferred"), (ValueError(), "error")])
async def test_model_errors_keep_independent_chorus(live, monkeypatch, error, status):
    workspace = live.workspace
    generate = AsyncMock(side_effect=error)
    monkeypatch.setattr(server_module, "generate_candidates", generate)
    live.seed()
    live.spawn(workspace._chorus_loop("current"))
    await next_pause(live.ticks)
    live.spawn(workspace._candidate_loop("current"))
    await next_pause(live.pauses)
    assert workspace.danmaku_candidates["status"] == status
    assert_chorus(workspace)
    assert workspace.danmaku_candidates["generated_at"] == 800
    assert generate.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [False, True])
async def test_slow_model_cannot_resurrect_expired_chorus(live, monkeypatch, replacement):
    workspace = live.workspace
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(model, context, **kwargs):
        assert context["audience_chorus"][0]["text"] == "好耶"
        entered.set()
        await release.wait()
        return ["好耶", "挥手了", "挥手真可爱", "跟着挥挥手", "大家晚上好"]

    monkeypatch.setattr(server_module, "generate_candidates", generate)
    live.seed()
    live.spawn(workspace._chorus_loop("current"))
    _, gate = await next_pause(live.ticks)
    live.spawn(workspace._candidate_loop("current"))
    await asyncio.wait_for(entered.wait(), 1)
    assert workspace.danmaku_candidates["status"] == "generating"
    assert_chorus(workspace)
    live.clock.value = 131
    if replacement:
        live.seed("打卡")
    gate.set_result(None)
    await next_pause(live.ticks)
    assert "好耶" not in [item["text"] for item in workspace.danmaku_chorus["items"]]
    release.set()
    await next_pause(live.pauses)
    assert workspace.danmaku_candidates["status"] == "ready"
    assert "好耶" not in [item["text"] for item in workspace.danmaku_chorus["items"]]
    # The AI output remains independent; it cannot populate the live chorus box.
    assert "好耶" in [item["text"] for item in workspace.danmaku_candidates["items"]]
    assert len(workspace.danmaku_candidates["items"]) == 5
    if replacement:
        assert_chorus(workspace, "打卡")
    else:
        assert not workspace.danmaku_chorus["items"]


@pytest.mark.asyncio
async def test_old_session_tasks_cannot_publish_or_expire_current_candidates(live):
    workspace = live.workspace
    live.seed()
    await workspace._refresh_chorus("current")
    expected = copy.deepcopy(workspace.danmaku_chorus)
    count = len(live.emissions)
    live.clock.value = 200
    await workspace._refresh_chorus("old-session")
    await workspace._chorus_loop("old-session")
    assert workspace.danmaku_chorus == expected
    assert len(live.emissions) == count


@pytest.mark.asyncio
async def test_style_change_keeps_current_chorus_and_invalidates_ai_drafts(live):
    workspace = live.workspace
    live.seed()
    await workspace._refresh_chorus("current")
    result = await workspace.set_danmaku_style("current", 100)
    assert_chorus(workspace)
    assert len(result["danmaku_candidates"]["items"]) == 0
    assert result["danmaku_candidates"]["style_revision"] == 1
    assert result["danmaku_candidates"]["generated_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["offline", "stopped", "short-limit"])
async def test_unavailable_room_or_length_limit_removes_chorus(live, monkeypatch, change):
    workspace = live.workspace
    live.seed()
    await workspace._refresh_chorus("current")
    assert_chorus(workspace)
    if change == "offline":
        workspace.room.live_status = 0
    elif change == "stopped":
        workspace.session["status"] = "stopped"
    else:
        monkeypatch.setattr(server_module, "MAX_DANMAKU_LENGTH", 1)
    await workspace._refresh_chorus("current")
    assert not workspace.danmaku_chorus["items"]
    assert len(workspace.danmaku_candidates["items"]) == 5


@pytest.mark.asyncio
async def test_multiple_choruses_leave_all_five_ai_candidates_untouched(live):
    workspace = live.workspace
    expected = copy.deepcopy(workspace.danmaku_candidates)
    for text in ("好耶", "打卡", "来了"):
        live.seed(text, timestamp=99)
    await workspace._refresh_chorus("current")
    assert {item["text"] for item in workspace.danmaku_chorus["items"]} == {"好耶", "打卡", "来了"}
    assert all(item["source_time"] == 99 for item in workspace.danmaku_chorus["items"])
    assert workspace.danmaku_candidates == expected
    original = copy.deepcopy(workspace.danmaku_chorus)
    live.clock.value = 103
    await workspace._refresh_chorus("current")
    assert workspace.danmaku_chorus == original
    assert len(live.emissions) == 1


@pytest.mark.asyncio
async def test_new_message_updates_real_source_time_but_keeps_same_chorus_id(live):
    workspace = live.workspace
    live.seed()
    await workspace._refresh_chorus("current")
    first = copy.deepcopy(workspace.danmaku_chorus["items"][0])
    live.clock.value = 110
    workspace.danmaku.append({"text": "好耶", "timestamp": 109, "user_id": 7, "message_id": "new"})
    await workspace._refresh_chorus("current")
    current = workspace.danmaku_chorus["items"][0]
    assert current["id"] == first["id"]
    assert current["source_time"] == 109
    assert current["updated_at"] > first["updated_at"]
    assert current["count"] == 5


@pytest.mark.asyncio
async def test_new_session_clears_chorus_without_reusing_old_items(live):
    workspace = live.workspace
    live.seed()
    await workspace._refresh_chorus("current")
    workspace.session = {"id": "next", "status": "live"}
    workspace.danmaku.clear()
    await workspace._refresh_chorus("next")
    assert workspace.danmaku_chorus["session_id"] == "next"
    assert not workspace.danmaku_chorus["items"]
