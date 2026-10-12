import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import app.server as server_module
from app.config import Settings
from app.model import VisionModel
from app.server import Workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("question_active", [False, True], ids=["busy-observations", "question-priority"])
async def test_deferred_candidates_eventually_refresh_without_interrupting_observations(
        tmp_path, monkeypatch, question_active):
    """Exercise the real candidate loop, prompt builder and model admission path.

    Only this server module's clock/sleep are controlled. The two observations
    and optional question remain real pending asyncio tasks on a mock endpoint.
    """
    workspace = Workspace(Settings(tmp_path, "ffmpeg"))
    clock = SimpleNamespace(value=100.0)
    sleeping = asyncio.Queue()
    entered = asyncio.Queue()
    release_question = asyncio.Event()
    requests = []
    current_question = {"active": question_active}

    async def controlled_sleep(seconds):
        gate = asyncio.get_running_loop().create_future()
        await sleeping.put((seconds, gate))
        await gate

    async def next_sleep():
        return await asyncio.wait_for(sleeping.get(), 1)

    async def advance(gate, now):
        clock.value = now
        gate.set_result(None)
        return await next_sleep()

    async def endpoint(request):
        payload = json.loads(request.content)
        first = payload["messages"][0]["content"]
        name = first if first in {"observe-one", "observe-two", "question"} else "draft"
        requests.append(name)
        if name.startswith("observe"):
            await entered.put(name)
            await asyncio.Event().wait()
        if name == "question":
            await entered.put(name)
            await release_question.wait()
        if name == "draft":
            assert payload["max_tokens"] == 360
        content = (json.dumps({"candidates": ["一起看直播", "刚才好有趣", "期待下一幕", "挥手了哈哈", "招手好可爱"]}, ensure_ascii=False)
                   if name == "draft" else "问题已回答")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    model = VisionModel("http://model.invalid/v1", "test", timeout=10)
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    workspace.model = model
    workspace.clock_start = 100.0
    workspace.session = {"id": "session", "status": "live"}
    workspace.room = SimpleNamespace(live_status=1)
    workspace.agent = SimpleNamespace(_observer_snapshot=lambda: {
        "understanding_age_seconds": 16, "active_questions": int(current_question["active"]),
        "inflight_observations": 2})
    old_items = [{"id": "existing", "text": "已有草稿"}]
    workspace.danmaku_candidates = {"items": old_items, "status": "ready", "generated_at": 1}

    def snapshot():
        source_time = clock.value - workspace.clock_start
        return {"observer": workspace.agent._observer_snapshot(),
                "room": {"title": "测试直播"},
                "observations": [{"kind": "observation", "timestamp": source_time - 16,
                                  "description": "画面里的角色正在挥手"}],
                "danmaku": [{"timestamp": source_time - 1, "text": "一起看"}],
                "audio": {"segments": []}}

    emitted = []

    async def emit(kind, data):
        assert kind == "danmaku_candidates"
        emitted.append(data["status"])

    send = AsyncMock(side_effect=AssertionError("Candidate generation must never send a Bilibili message"))
    monkeypatch.setattr(workspace, "snapshot", snapshot)
    monkeypatch.setattr(workspace, "emit", emit)
    monkeypatch.setattr(workspace.danmaku_sender, "send", send)
    monkeypatch.setattr(server_module, "time", SimpleNamespace(monotonic=lambda: clock.value, time=time.time))
    monkeypatch.setattr(server_module, "asyncio", SimpleNamespace(
        sleep=controlled_sleep, wait_for=asyncio.wait_for, CancelledError=asyncio.CancelledError,
        create_task=asyncio.create_task, wait=asyncio.wait, FIRST_COMPLETED=asyncio.FIRST_COMPLETED,
        gather=asyncio.gather))

    observations = [asyncio.create_task(model.complete_observation([
        {"role": "user", "content": name}])) for name in ("observe-one", "observe-two")]
    question = None
    candidate = None
    try:
        for _ in observations:
            await asyncio.wait_for(entered.get(), 1)
        if question_active:
            question = asyncio.create_task(model.complete([{"role": "user", "content": "question"}]))
            assert await asyncio.wait_for(entered.get(), 1) == "question"
        assert model._observation_requests == 2
        assert not model.background_available

        candidate = asyncio.create_task(workspace._candidate_loop("session"))
        delay, gate = await next_sleep()
        assert delay == 2
        assert workspace.danmaku_candidates["status"] == "deferred"
        assert workspace.danmaku_candidates["items"] == old_items
        assert workspace.danmaku_candidates["generated_at"] == 1
        assert "draft" not in requests

        delay, gate = await advance(gate, 129.0)
        assert delay == 2
        assert "draft" not in requests
        delay, gate = await advance(gate, 130.0)

        if question_active:
            # Even twice the fairness interval cannot bypass a live question.
            assert delay == 2
            assert "draft" not in requests
            delay, gate = await advance(gate, 160.0)
            assert delay == 2
            assert "draft" not in requests
            assert workspace.danmaku_candidates["items"] == old_items
            release_question.set()
            await asyncio.wait_for(question, 1)
            current_question["active"] = False
            delay, gate = await advance(gate, 161.0)

        # The fallback traversed generate_candidates -> complete_background
        # while both observation requests were still admitted and unfinished.
        assert delay == 12
        assert requests.count("draft") == 1
        assert workspace.danmaku_candidates["status"] == "ready"
        assert [item["text"] for item in workspace.danmaku_candidates["items"]] == [
            "一起看直播", "刚才好有趣", "期待下一幕", "挥手了哈哈", "招手好可爱"]
        assert model._observation_requests == 2
        assert all(not task.done() for task in observations)
        assert emitted.count("generating") == 1
        assert emitted.count("ready") == 1
        send.assert_not_awaited()

        fresh_items = workspace.danmaku_candidates["items"]
        delay, gate = await advance(gate, clock.value + 12)
        assert delay == 2
        assert requests.count("draft") == 1
        assert workspace.danmaku_candidates["status"] == "deferred"
        assert workspace.danmaku_candidates["items"] == fresh_items
        assert all(not task.done() for task in observations)
        send.assert_not_awaited()
    finally:
        release_question.set()
        tasks = [*observations, *([question] if question else []), *([candidate] if candidate else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await model.close()
        await workspace.client.aclose()
        await workspace.danmaku_sender.close()
