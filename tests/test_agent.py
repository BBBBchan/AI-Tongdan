import asyncio
import json
import base64
import re
from pathlib import Path

import httpx
import pytest

from app.agent import LiveAgent, LiveMemory, LiveMemoryGraph
from app.model import ModelError, VisionModel


# A real, tiny JPEG is unnecessary for the fake endpoint: the tests assert that
# the exact original frame bytes reach the model, instead of synthetic evidence.
FRAME_BYTES = b"\xff\xd8\xfftest-frame\xff\xd9"


async def until(predicate, timeout=3):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


class Model:
    configured = True

    def __init__(self):
        self.calls = []
        self.answer_started = asyncio.Event()
        self.release_answer = asyncio.Event()
        self.release_answer.set()
        self.observed = 0

    async def complete(self, messages, max_tokens=512, json_mode=False):
        self.calls.append(messages)
        system = messages[0]["content"]
        if "持续直播视觉观察器" in system:
            self.observed += 1
            return json.dumps({"description": f"主播拿着红色水杯，观察{self.observed}",
                               "delta": "主播举起水杯", "changed": True,
                               "entities": ["主播", "水杯"], "key_fact": "桌上有红色水杯"}, ensure_ascii=False)
        if "规划器" in system:
            data = json.loads(messages[-1]["content"])
            return json.dumps({"plan": "回看水杯证据", "queries": ["红色水杯"],
                               "replay_times": data["available_replay_times"][:1]}, ensure_ascii=False)
        self.answer_started.set()
        await self.release_answer.wait()
        return "主播拿着红色水杯。[t=1]"


def baseline_agent(*args, **kwargs):
    return LiveAgent(*args, observation_frames=2, observation_stride=1,
                     observation_concurrency=1, **kwargs)


async def start_baseline(agent, frame):
    await agent.start()
    await agent.offer_frame(frame, 0)


@pytest.fixture
def frame(tmp_path):
    path = tmp_path / "source.jpg"
    path.write_bytes(FRAME_BYTES)
    return path


@pytest.mark.asyncio
async def test_observation_continues_during_and_after_multiple_questions(tmp_path, frame):
    model = Model()
    events = []
    async def emit(kind, data):
        events.append((kind, data))
    agent = baseline_agent("live", tmp_path / "session", model, emit)
    await start_baseline(agent, frame)
    try:
        await agent.offer_frame(frame, 1)
        await until(lambda: len(agent.observations) == 1)
        assert agent.observations[0]["delta"] == "主播举起水杯"
        assert agent.observations[0]["changed"] is True
        assert agent.observations[0]["frame_count"] == 2
        agent.add_danmaku({"text": "这是红色水杯", "user": "观众甲"})
        model.release_answer.clear()
        first = agent.ask("主播手里是什么？")
        await model.answer_started.wait()
        await agent.offer_frame(frame, 2)
        await until(lambda: len(agent.observations) == 2)
        assert agent._assistant_message(first)["status"] == "answering"
        assert agent.observations[-1]["delta"] == "主播举起水杯"
        model.release_answer.set()
        await until(lambda: agent._assistant_message(first)["status"] == "complete")
        second = agent.ask("那是什么颜色的？")
        await until(lambda: agent._assistant_message(second)["status"] == "complete")
        await agent.offer_frame(frame, 3)
        await until(lambda: len(agent.observations) == 3)
        assert agent._running
        answer_calls = [c for c in model.calls if "你是直播助手" in c[0]["content"]]
        assert len(answer_calls) == 2
        assert any(m["role"] == "assistant" and "红色水杯" in m["content"] for m in answer_calls[1][:-1])
        images = [p for p in answer_calls[0][-1]["content"] if p["type"] == "image_url"]
        assert images and images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        answer_data = json.loads(answer_calls[0][-1]["content"][0]["text"])
        assert answer_data["snapshot_time"] == 1
        assert len(answer_data["recent_observations"]) == 1  # immutable despite concurrent observation
        assert answer_data["audience_chat_untrusted"]["recent"][0]["text"] == "这是红色水杯"
        assert sum(kind == "answer_done" for kind, _ in events) == 2
        assert all(event.kind == "observation" for event in agent.memory.events)
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_cancel_and_overload_do_not_stop_observer(tmp_path, frame):
    model = Model()
    model.release_answer.clear()
    events = []
    async def emit(kind, data):
        events.append((kind, data))
    agent = baseline_agent("live", tmp_path / "session", model, emit)
    await start_baseline(agent, frame)
    try:
        await agent.offer_frame(frame, 1)
        await until(lambda: len(agent.observations) == 1)
        question = agent.ask("现在发生什么？")
        await model.answer_started.wait()
        queued = [agent.ask("继续描述") for _ in range(3)]
        with pytest.raises(ValueError, match="4 个问题"):
            agent.ask("更多问题")
        await agent.cancel_question(question)
        await agent.cancel_question()
        assert all(agent._assistant_message(q)["status"] == "cancelled" for q in [question, *queued])
        await agent.offer_frame(frame, 2)
        await until(lambda: len(agent.observations) == 2)
        assert agent._running
        assert not agent._leases
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_frame_leases_and_all_stores_stay_bounded(tmp_path, frame):
    model = Model()
    async def emit(*args):
        pass
    agent = baseline_agent("live", tmp_path / "session", model, emit, max_frames=2, max_observations=3)
    await start_baseline(agent, frame)
    try:
        for t in range(1, 3):
            await agent.offer_frame(frame, t)
            await until(lambda: agent._observed == t)
        first_frame = agent.frames[0].path
        model.release_answer.clear()
        question = agent.ask("之前的水杯是什么样？")
        await model.answer_started.wait()
        for t in range(3, 10):
            await agent.offer_frame(frame, t)
            await until(lambda: agent._observed == t)
        assert first_frame.exists()  # visual evidence is protected while answering
        assert len(agent.frames) == 2
        assert len(list(agent.frame_dir.iterdir())) <= 4
        assert len(agent.memory.events) == 3
        assert len(agent.observations) == 3
        for n in range(400):
            agent.add_danmaku({"text": str(n)})
        assert len(agent.danmaku) == 300
        model.release_answer.set()
        await until(lambda: agent._assistant_message(question)["status"] == "complete")
        await until(lambda: not agent._questions)
        assert not first_frame.exists()
        assert len(list(agent.frame_dir.iterdir())) == 2
        assert frame.exists()  # source belongs to the capture producer
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_latest_frame_backpressure_and_missing_model_are_honest(tmp_path, frame):
    model = Model()
    model.configured = False
    events = []
    async def emit(kind, data):
        events.append((kind, data))
    agent = baseline_agent("live", tmp_path / "session", model, emit, max_frames=2)
    await agent.start()
    try:
        for t in range(20):
            await agent.offer_frame(frame, t)
        assert agent._pending.qsize() == 1
        assert agent._dropped == 18
        assert agent._replaced_windows == 18
        await until(lambda: agent._pending.empty())
        assert not agent.observations
        assert not model.calls
        question = agent.ask("画面是什么？")
        await until(lambda: agent._assistant_message(question)["status"] == "error")
        assert not agent._assistant_message(question)["content"]
        assert not any(kind == "answer_delta" for kind, _ in events)
    finally:
        await agent.stop()


def test_chinese_retrieval_matches_inside_sentences_across_layers():
    memory = LiveMemory("test")
    relevant = memory.write_event(1, "observation", "主播拿起红色水杯喝水", delta="拿起水杯", is_change=True)
    memory.write_event(2, "observation", "窗外有人骑自行车经过")
    episode = memory.write_episode(1, 1, "主播使用红色水杯", ev_ids=[relevant.eid])
    graph = LiveAgent._build_graph(memory)
    assert isinstance(graph, LiveMemoryGraph)
    assert memory.search("水杯是什么颜色？")[0].eid == relevant.eid
    paths = graph.retrieve_backtrack("红色水杯在哪里？")
    assert any(any(n.layer == "working" and n.node_id == relevant.eid for n in p.nodes) for p in paths)
    assert any(any(n.layer == "episodic" and n.node_id == episode["id"] for n in p.nodes) for p in paths)


@pytest.mark.asyncio
async def test_model_payload_and_upstream_errors():
    seen = []
    async def endpoint(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "真实模型输出"}}]})
    model = VisionModel("http://127.0.0.1:8000/v1", "test-model")
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    try:
        assert await model.complete([{"role": "user", "content": "观察"}], json_mode=True) == "真实模型输出"
        assert seen[0]["chat_template_kwargs"]["enable_thinking"] is False
        assert seen[0]["response_format"]["type"] == "json_object"
    finally:
        await model.close()
    async def bad_endpoint(request):
        return httpx.Response(401, json={"error": {"message": "secret-do-not-expose"}})
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(bad_endpoint))
    try:
        with pytest.raises(ModelError, match="HTTP 401") as error:
            await model.complete([])
        assert "secret" not in str(error.value)
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_queued_question_freezes_when_submitted_not_when_started(tmp_path, frame):
    model = Model()
    model.release_answer.clear()
    async def emit(*args):
        pass
    agent = baseline_agent("live", tmp_path / "session", model, emit, max_frames=2)
    await start_baseline(agent, frame)
    try:
        await agent.offer_frame(frame, 1)
        await until(lambda: agent._observed == 1)
        first = agent.ask("之前是什么？")
        await model.answer_started.wait()
        await agent.offer_frame(frame, 2)
        await until(lambda: agent._observed == 2)
        second = agent.ask("这个东西是什么颜色？")
        assert agent._question_contexts[second]["frames"][-1].timestamp == 2
        await agent.offer_frame(frame, 3)
        await until(lambda: agent._observed == 3)
        model.release_answer.set()
        await until(lambda: agent._assistant_message(second)["status"] == "complete")
        answers = [c for c in model.calls if "你是直播助手" in c[0]["content"]]
        second_data = json.loads(answers[1][-1]["content"][0]["text"])
        assert second_data["snapshot_time"] == 2
        assert max(e["t"] for e in second_data["recent_observations"]) == 2
        assert any(m["role"] == "assistant" and "红色水杯" in m["content"] for m in answers[1][1:-1])
        assert agent._assistant_message(first)["status"] == "complete"
    finally:
        await agent.stop()


def test_monotonic_ids_and_chinese_entity_index_survive_retention():
    memory = LiveMemory("live")
    episodes, pins = [], []
    for t in range(12):
        event = memory.write_event(t, "observation", "主播捡起红色水杯", entities=["红色水杯"])
        episode = memory.write_episode(t, t, "捡起物体", ev_ids=[event.eid])
        pin = memory.pin_fact("桌上有水杯", t=t, ep_id=episode["id"])
        episodes.append(episode["id"])
        pins.append(pin["id"])
        memory.events = memory.events[-2:]
        memory.episodes = memory.episodes[-2:]
        memory.pinned_facts = memory.pinned_facts[-2:]
    assert episodes == list(range(1, 13))
    assert pins == list(range(1, 13))
    graph = LiveAgent._build_graph(memory)
    assert graph.entity_index.entity_count == 1
    timeline = graph.entity_index.get_timeline("红色水杯")
    assert timeline and timeline.event_ids == [11, 12]
    assert timeline.episode_ids == [11, 12]
    assert "红色水杯" in graph.entity_index.render_digest()


@pytest.mark.asyncio
async def test_structured_visual_fields_are_retained_and_future_replay_is_rejected(tmp_path, frame):
    class StructuredModel(Model):
        async def complete(self, messages, max_tokens=512, json_mode=False):
            if "持续直播视觉观察器" in messages[0]["content"]:
                assert max_tokens == 900
                self.observed += 1
                return json.dumps({"description": "主播展示水杯", "delta": "首次观察", "changed": False,
                                   "actors": ["主播"], "objects": ["红色水杯"], "actions": "展示",
                                   "ocr": "欢迎", "scene": "室内", "entities": ["红色水杯"]}, ensure_ascii=False)
            if "规划器" in messages[0]["content"]:
                return json.dumps({"plan": "回看", "replay_times": [999999]})
            return await super().complete(messages, max_tokens, json_mode)
    model = StructuredModel()
    events = []
    async def emit(kind, data):
        events.append((kind, data))
    agent = baseline_agent("live", tmp_path / "session", model, emit, observation_max_tokens=900)
    await start_baseline(agent, frame)
    try:
        agent.add_danmaku({"text": "你好", "user_name": "观众乙"})
        assert agent.danmaku[-1]["user"] == "观众乙"
        await agent.offer_frame(frame, 1)
        await until(lambda: agent._observed == 1)
        record = agent.observations[0]
        assert record["ocr"] == "欢迎"
        assert record["objects"] == "红色水杯"
        assert "ocr: 欢迎" in agent.memory.events[0].text
        assert agent.snapshot()["observer"]["last_timestamp"] == 1
        assert agent.snapshot()["observer"]["model_ready"] is True
        question = agent.ask("现在发生什么？")
        await until(lambda: agent._assistant_message(question)["status"] == "complete")
        plan = next(data for kind, data in events if kind == "plan")
        assert all(t <= 1 for t in plan["replay_times"])
        assert 999999 not in plan["replay_times"]
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_truncated_model_output_is_not_treated_as_success():
    async def endpoint(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "部分回答"}}]})
    model = VisionModel("http://127.0.0.1:8000/v1", "test-model")
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    try:
        with pytest.raises(ModelError, match="截断"):
            await model.complete([])
    finally:
        await model.close()


@pytest.mark.asyncio
async def test_continuous_session_across_many_questions_and_cancellations(tmp_path, frame):
    class LongModel(Model):
        async def complete(self, messages, max_tokens=768, json_mode=False):
            result = await super().complete(messages, max_tokens, json_mode)
            if "持续直播视觉观察器" in messages[0]["content"]:
                record = json.loads(result)
                record["key_fact"] = f"第{self.observed}次观察的独立可见事实"
                return json.dumps(record, ensure_ascii=False)
            return result
    async def emit(*args):
        pass
    agent = baseline_agent("long", tmp_path / "session", LongModel(), emit, max_frames=5, max_observations=12)
    await start_baseline(agent, frame)
    completed, cancelled = [], []
    try:
        for tick in range(1, 121):
            await agent.offer_frame(frame, tick)
            await until(lambda: agent._observed == tick)
            if tick % 10 == 0:
                question = agent.ask("水杯是什么颜色？")
                if tick % 20 == 0:
                    await agent.cancel_question(question)
                    cancelled.append(question)
                else:
                    await until(lambda: agent._assistant_message(question)["status"] == "complete")
                    completed.append(question)
        assert agent._running and agent._observed == 120
        assert len(completed) == len(cancelled) == 6
        assert all(agent._assistant_message(q)["status"] == "complete" for q in completed)
        assert all(agent._assistant_message(q)["status"] == "cancelled" for q in cancelled)
        assert len(agent.memory.events) == len(agent.observations) == 12
        assert len(agent.memory.pinned_facts) == 40
        assert len(agent.memory.episodes) <= 12
        assert len(list(agent.frame_dir.iterdir())) == 5
        assert len({pin["id"] for pin in agent.memory.pinned_facts}) == 40
    finally:
        await agent.stop()


class WindowModel(Model):
    """Independently controlled requests expose real scheduling/retention races."""
    def __init__(self, failures=()):
        super().__init__()
        self.windows = {}
        self.failures = set(failures)
        self.active_observers = 0
        self.peak_observers = 0

    async def complete(self, messages, max_tokens=768, json_mode=False):
        if "持续直播视觉观察器" not in messages[0]["content"]:
            return await super().complete(messages, max_tokens, json_mode)
        content = messages[-1]["content"]
        labels = [part["text"] for part in content if part["type"] == "text" and re.match(r"第\d+/\d+张", part["text"])]
        times = [float(re.search(r"采样时间 ([\d.]+) 秒", label).group(1)) for label in labels]
        end = times[-1]
        gate = asyncio.Event()
        self.windows[end] = {"times": times, "gate": gate, "messages": messages,
                             "images": [p["image_url"]["url"] for p in content if p["type"] == "image_url"]}
        self.active_observers += 1
        self.peak_observers = max(self.peak_observers, self.active_observers)
        try:
            await gate.wait()
            if end in self.failures:
                raise ModelError("controlled observer failure")
            return json.dumps({"description": f"窗口到{end:g}秒，主播拿着红色水杯", "delta": "中间帧举杯后放回",
                               "changed": True, "entities": ["主播", "水杯"]}, ensure_ascii=False)
        finally:
            self.active_observers -= 1

    def release(self, end):
        self.windows[end]["gate"].set()


@pytest.mark.asyncio
async def test_exact_four_frame_windows_overlap_and_commit_without_waiting_for_older(tmp_path):
    model = WindowModel()
    events = []
    async def emit(kind, data):
        events.append((kind, data))
    agent = LiveAgent("windows", tmp_path / "session", model, emit, max_frames=4)
    await agent.start()
    try:
        sources = []
        for tick in range(1, 8):
            source = tmp_path / f"frame_{tick}.jpg"
            source.write_bytes(FRAME_BYTES + bytes([tick]))
            sources.append(source)
        for tick in range(1, 4):
            await agent.offer_frame(sources[tick - 1], tick)
        await asyncio.sleep(0)
        assert not model.windows  # the warm-up never submits a 1/2/3-frame batch
        await agent.offer_frame(sources[3], 4)
        await until(lambda: 4 in model.windows)
        assert model.windows[4]["times"] == [1, 2, 3, 4]
        assert len(model.windows[4]["images"]) == 4  # no hidden fifth anchor
        assert [base64.b64decode(url.split(",", 1)[1]) for url in model.windows[4]["images"]] == [p.read_bytes() for p in sources[:4]]
        assert "中间出现又消失" in model.windows[4]["messages"][-1]["content"][0]["text"]
        for tick in range(5, 8):
            await agent.offer_frame(sources[tick - 1], tick)
        await until(lambda: 7 in model.windows)
        assert model.windows[7]["times"] == [4, 5, 6, 7]
        assert model.peak_observers == 2
        assert agent.snapshot()["observer"]["inflight_observations"] == 2
        model.release(7)
        await until(lambda: agent._observed == 1)
        record = agent.observations[0]
        assert record["window_start"] == 4 and record["window_end"] == 7
        assert record["capture_timestamps"] == [4, 5, 6, 7]
        assert record["frame_count"] == 4
        assert record["changed"] is True  # first multi-frame window can contain motion
        assert record["completion_lag"] >= record["inference_seconds"] >= 0
        model.release(4)
        await until(lambda: agent._stale_results == 1)
        assert [o["window_end_sequence"] for o in agent.observations] == [7]
        assert [event.t for event in agent.memory.events] == [7]
        stats = agent.snapshot()["observer"]
        assert stats["received_frames"] == stats["submitted_frames"] == 7
        assert stats["observed_frames"] == 4
        assert stats["dropped_frames"] == stats["failed_windows"] == 0
        assert any(kind == "observation_skipped" for kind, _ in events)
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_overlapping_window_backpressure_counts_unique_frames_and_preserves_all_leases(tmp_path, frame):
    model = WindowModel()
    async def emit(*args):
        pass
    agent = LiveAgent("bounded", tmp_path / "session", model, emit,
                      max_frames=4, observation_stride=2)
    await agent.start()
    try:
        for tick in range(1, 5):
            await agent.offer_frame(frame, tick)
        await until(lambda: 4 in model.windows)
        initial = {f.sequence: f.path for f in agent._inflight[4].frames}
        for tick in (5, 6):
            await agent.offer_frame(frame, tick)
        await until(lambda: 6 in model.windows)
        initial.update({f.sequence: f.path for f in agent._inflight[6].frames})
        for tick in range(7, 201):
            await agent.offer_frame(frame, tick)
            assert len(agent._frame_usage) <= 16
            assert agent._pending.qsize() <= 1
        assert all(path.exists() for path in initial.values())  # both complete inflight batches
        assert len(list(agent.frame_dir.glob("*.jpg"))) == 10  # 6 inflight unique + latest 4
        assert agent._queued_window.frames[0].timestamp == 197
        assert agent._queued_window.frames[-1].timestamp == 200
        stats = agent.snapshot()["observer"]
        assert stats["received_frames"] == 200
        assert stats["submitted_frames"] == 6
        assert stats["observed_frames"] == 0
        assert stats["dropped_frames"] == 190  # not 96 replaced windows * 4
        assert stats["replaced_windows"] == 96
        model.release(4)
        await until(lambda: 200 in model.windows)
        model.release(6)
        await until(lambda: agent._observed == 2)
        model.release(200)
        await until(lambda: agent._observed == 3)
        await until(lambda: not agent._inflight)
        stats = agent.snapshot()["observer"]
        assert stats["submitted_frames"] == stats["observed_frames"] == 10
        assert stats["received_frames"] == stats["submitted_frames"] + stats["dropped_frames"]
        assert len(agent._frame_usage) == 4
        assert len(list(agent.frame_dir.glob("*.jpg"))) == 4
        assert model.peak_observers == 2
    finally:
        await agent.stop()
    assert not agent._frame_usage and not agent._leases


@pytest.mark.asyncio
async def test_four_frame_questions_freeze_before_concurrent_observation_commits(tmp_path, frame):
    model = WindowModel()
    model.release_answer.clear()
    async def emit(*args):
        pass
    agent = LiveAgent("questions", tmp_path / "session", model, emit, max_frames=4)
    await agent.start()
    try:
        for tick in range(1, 5):
            await agent.offer_frame(frame, tick)
        await until(lambda: 4 in model.windows)
        model.release(4)
        await until(lambda: agent._observed == 1)
        first = agent.ask("主播拿着什么？")
        await model.answer_started.wait()
        second = agent.ask("它是什么颜色？")
        protected = [f.path for f in agent._question_contexts[second]["frames"]]
        for end in (7, 10):
            for tick in range(end - 2, end + 1):
                await agent.offer_frame(frame, tick)
            await until(lambda: end in model.windows)
            model.release(end)
            await until(lambda: agent.observations[-1]["window_end"] == end)
        assert agent._assistant_message(first)["status"] == "answering"
        assert all(path.exists() for path in protected)
        model.release_answer.set()
        await until(lambda: agent._assistant_message(second)["status"] == "complete")
        answers = [c for c in model.calls if "你是直播助手" in c[0]["content"]]
        assert len(answers) == 2
        for messages in answers:
            payload = json.loads(messages[-1]["content"][0]["text"])
            assert payload["snapshot_time"] == 4
            assert max(item["t"] for item in payload["recent_observations"]) == 4
        assert agent._observed == 3
        assert any(m["role"] == "assistant" for m in answers[1][1:-1])
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_stop_cancels_both_observers_and_releases_pending_accounting(tmp_path, frame):
    model = WindowModel()
    async def emit(*args):
        pass
    agent = LiveAgent("stop", tmp_path / "session", model, emit, max_frames=4)
    await agent.start()
    for tick in range(1, 5):
        await agent.offer_frame(frame, tick)
    await until(lambda: 4 in model.windows)
    for tick in range(5, 8):
        await agent.offer_frame(frame, tick)
    await until(lambda: 7 in model.windows)
    for tick in range(8, 13):
        await agent.offer_frame(frame, tick)
    assert agent._pending.qsize() == 1
    await agent.stop()
    stats = agent.snapshot()["observer"]
    assert model.active_observers == 0
    assert stats["inflight_observations"] == stats["pending_windows"] == 0
    assert stats["received_frames"] == 12
    assert stats["submitted_frames"] == 7
    assert stats["observed_frames"] == 0
    assert stats["dropped_frames"] == 5
    assert stats["cancelled_windows"] == 2
    assert stats["failed_windows"] == 0
    assert not agent._leases and not agent._frame_usage
    assert not agent._observer_tasks


@pytest.mark.asyncio
async def test_failed_older_window_does_not_claim_successful_coverage_or_override_new_success(tmp_path, frame):
    model = WindowModel(failures={4})
    async def emit(*args):
        pass
    agent = LiveAgent("failed", tmp_path / "session", model, emit)
    await agent.start()
    try:
        for tick in range(1, 5):
            await agent.offer_frame(frame, tick)
        await until(lambda: 4 in model.windows)
        for tick in range(5, 8):
            await agent.offer_frame(frame, tick)
        await until(lambda: 7 in model.windows)
        model.release(7)
        await until(lambda: agent._observed == 1)
        model.release(4)
        await until(lambda: agent._failed_windows == 1)
        stats = agent.snapshot()["observer"]
        assert stats["submitted_frames"] == 7
        assert stats["observed_frames"] == 4
        assert stats["failed_windows"] == 1 and stats["stale_results"] == 0
        assert stats["model_ready"] is True and stats["error"] is None
        assert [e.t for e in agent.memory.events] == [7]
    finally:
        await agent.stop()
