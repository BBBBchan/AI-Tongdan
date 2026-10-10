import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.model import ModelBusy, ModelError, VisionModel
from app.server import Workspace


def response():
    return httpx.Response(200, json={"choices": [{"message": {"content": "有效结果"}}]})


@pytest.mark.asyncio
async def test_slow_question_does_not_block_observation_and_drafts_yield():
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def endpoint(request):
        name = json.loads(request.content)["messages"][0]["content"]
        requests.append(name)
        if name == "question":
            entered.set()
            await release.wait()
        return response()

    model = VisionModel("http://model.invalid/v1", "test")
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    question = asyncio.create_task(model.complete([{"role": "user", "content": "question"}]))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await asyncio.wait_for(model.complete_observation([
            {"role": "user", "content": "observation"}]), 1) == "有效结果"
        with pytest.raises(ModelBusy):
            await model.complete_background([{"role": "user", "content": "draft"}])
        assert requests == ["question", "observation"]
        assert not question.done()
        release.set()
        assert await question == "有效结果"
        assert await model.complete_background([{"role": "user", "content": "draft"}]) == "有效结果"
    finally:
        release.set()
        await question
        await model.close()


@pytest.mark.asyncio
async def test_two_active_observations_reserve_capacity_and_cancel_releases_it():
    releases = {name: asyncio.Event() for name in ("first", "second")}
    started = asyncio.Queue()

    async def endpoint(request):
        name = json.loads(request.content)["messages"][0]["content"]
        if name in releases:
            started.put_nowait(name)
            await releases[name].wait()
        return response()

    model = VisionModel("http://model.invalid/v1", "test")
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    tasks = [asyncio.create_task(model.complete_observation([
        {"role": "user", "content": name}])) for name in releases]
    try:
        await asyncio.wait_for(started.get(), 1)
        await asyncio.wait_for(started.get(), 1)
        with pytest.raises(ModelBusy):
            await model.complete_background([{"role": "user", "content": "draft"}])
        # After a bounded deferral the scheduler may allow one short draft;
        # neither ongoing observation is cancelled or forced to wait for it.
        assert await model.complete_background([{"role": "user", "content": "draft"}],
                                               allow_observations_busy=True) == "有效结果"
        assert not any(task.done() for task in tasks)
        tasks[0].cancel()
        with suppress(asyncio.CancelledError):
            await tasks[0]
        assert model.background_available
        assert await model.complete_background([{"role": "user", "content": "draft"}]) == "有效结果"
        releases["second"].set()
        await tasks[1]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await model.close()


@pytest.mark.asyncio
async def test_stuck_request_budget_frees_observation_and_background_slots():
    cancelled = []

    async def endpoint(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    model = VisionModel("http://model.invalid/v1", "test", timeout=0.02)
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    try:
        with pytest.raises(ModelError, match="长时间"):
            await model.complete_observation([])
        assert model._observation_requests == 0
        with pytest.raises(ModelBusy, match="暂缓"):
            await model.complete_background([])
        assert not model._interactive_slot.locked() and model.background_available
        assert len(cancelled) == 2
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_deferred_candidates_keep_existing_drafts_without_model_request(tmp_path, monkeypatch):
    settings = Settings(tmp_path, "ffmpeg")
    workspace = Workspace(settings)
    workspace.session = {"id": "session", "status": "live"}
    workspace.room = SimpleNamespace(live_status=1)
    workspace.agent = SimpleNamespace(_observer_snapshot=lambda: {
        "understanding_age_seconds": 16, "active_questions": 0})
    workspace.model = SimpleNamespace(configured=True, background_available=True)
    workspace.danmaku_candidates = {"items": [{"id": "existing", "text": "已有草稿"}],
                                    "status": "ready", "generated_at": 1}
    emitted = asyncio.Event()

    async def emit(kind, data):
        assert kind == "danmaku_candidates"
        assert data["status"] == "deferred"
        emitted.set()

    async def should_not_generate(*args, **kwargs):
        pytest.fail("Optional draft must not compete with an overdue observation")

    monkeypatch.setattr(workspace, "emit", emit)
    monkeypatch.setattr("app.server.generate_candidates", should_not_generate)
    task = asyncio.create_task(workspace._candidate_loop("session"))
    try:
        await asyncio.wait_for(emitted.wait(), 1)
        assert workspace.danmaku_candidates["items"][0]["text"] == "已有草稿"
        assert workspace.danmaku_candidates["generated_at"] == 1
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await workspace.client.aclose()
        await workspace.danmaku_sender.close()


def test_adaptive_settings_reject_unsupported_frame_limit_and_invalid_target(tmp_path):
    with pytest.raises(ValueError, match="MAX_FRAMES"):
        Settings(tmp_path, "ffmpeg", observation_max_frames=10)
    for value in (float("nan"), float("inf"), -1, 0):
        with pytest.raises(ValueError, match="TARGET_LAG"):
            Settings(tmp_path, "ffmpeg", observation_target_lag=value)
