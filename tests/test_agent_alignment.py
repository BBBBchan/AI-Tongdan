import asyncio
import json
import time

import pytest

from app.agent import Frame, LiveAgent, ObservationWindow


async def emit(*args):
    pass


class Model:
    configured = True

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        self.started.set()
        await self.release.wait()
        return json.dumps({"description": "桌上有杯子", "visual_delta": "未确认明显变化",
                           "audio_delta": "这段声音说你好", "changed": False})


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(poll(), 2)


def make_agent(tmp_path, wait=0):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"\xff\xd8\xffframe\xff\xd9")
    agent = LiveAgent("alignment", tmp_path / "agent", Model(), emit,
                      observation_frames=2, observation_stride=1, observation_concurrency=1,
                      audio_alignment_wait_seconds=wait)
    return agent, source


def audio(key, start, end, text="你好"):
    return {"id": key, "start": start, "end": end, "text": text}


def window(path, start=10, end=13):
    return ObservationWindow((Frame("first", path, start, 1), Frame("last", path, end, 2)), time.monotonic())


def prompt_context(call):
    return json.loads(next(part["text"].split("：", 1)[1] for part in call[1]["content"]
                           if part.get("text", "").startswith("分源上下文")))


def test_context_matches_actual_source_overlap_and_rejects_old_or_future(tmp_path):
    agent, path = make_agent(tmp_path)
    for item in (audio("old", 6, 10), audio("future", 13, 16), audio("overlap", 9, 14)):
        agent.add_audio_segment(item)
    for timestamp in (9.99, 11.5, 13.01):
        agent.add_danmaku({"text": f"弹幕{timestamp}", "timestamp": timestamp})
    context = agent._observation_context(window(path))
    assert [item["id"] for item in context["audio_untrusted"]] == ["overlap"]
    assert [item["timeline_timestamp"] for item in context["danmaku_untrusted"]] == [11.5]
    assert context["audio_untrusted"][0]["start"] == 9
    assert context["audio_untrusted"][0]["end"] == 14
    assert (context["source_window_start"], context["source_window_end"]) == (10, 13)
    assert "没有逐词时间" in context["note"]


@pytest.mark.asyncio
async def test_delayed_asr_is_included_before_inference_and_later_revisions_are_frozen(tmp_path):
    agent, source = make_agent(tmp_path, wait=1)
    agent.model.release.clear()
    await agent.start()
    try:
        await agent.offer_frame(source, 10)
        await agent.offer_frame(source, 13)
        await until(lambda: bool(agent._inflight))
        await asyncio.sleep(0.02)
        assert not agent.model.calls
        agent.add_audio_segment(audio("matching", 11, 12, "推理前的转写"))
        agent.add_audio_progress({"start": 9, "end": 14, "status": "complete"})
        await agent.model.started.wait()
        context = prompt_context(agent.model.calls[0])
        assert context["audio_untrusted"][0]["text"] == "推理前的转写"
        assert context["audio_alignment"]["status"] == "complete"
        assert context["audio_alignment"]["wait_seconds"] >= 0.02
        agent.add_audio_segment(audio("matching", 11, 12, "推理后的修订"))
        agent.model.release.set()
        await until(lambda: agent._observed == 1)
        observation = [item for item in agent.observations if item["kind"] == "observation"][-1]
        assert observation["audio_evidence"][0]["text"] == "推理前的转写"
        assert observation["audio_alignment"]["status"] == "complete"
        assert observation["timestamp"] == observation["source_end"] == 13
        memory = agent.snapshot()["memory"]["working"][-1]
        assert memory["source_end"] == memory["t"] == 13
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_audio_timeout_continues_visual_and_late_audio_keeps_its_source_time(tmp_path):
    agent, source = make_agent(tmp_path, wait=0.03)
    await agent.start()
    try:
        await agent.offer_frame(source, 10)
        await agent.offer_frame(source, 13)
        await until(lambda: agent._observed == 1)
        observation = agent.observations[-1]
        assert observation["audio_alignment"]["status"] == "pending"
        assert observation["audio_alignment"]["timed_out"] is True
        assert observation["audio_wait_seconds"] >= 0.03
        assert observation["audio_evidence"] == [] and observation["audio_delta"] == ""
        agent.add_audio_segment(audio("late", 10.25, 12.75))
        late = agent._flush_context()
        assert late["timestamp"] == late["source_end"] == 12.75
        assert late["recorded_at"] >= 13
        assert agent.memory.events[-1].t == 12.75
        assert observation["audio_evidence"] == []  # no mutation of previous inference
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_silent_analyzed_coverage_wakes_observer_without_speech(tmp_path):
    agent, source = make_agent(tmp_path, wait=1)
    await agent.start()
    try:
        await agent.offer_frame(source, 10)
        await agent.offer_frame(source, 13)
        await until(lambda: bool(agent._inflight))
        agent.add_audio_progress({"start": 9, "end": 11, "status": "complete", "silent": True})
        assert agent._audio_alignment(window(source))["status"] == "partial"
        agent.add_audio_progress({"start": 11, "end": 14, "status": "complete", "silent": True})
        await until(lambda: agent._observed == 1)
        record = agent.observations[-1]
        assert record["audio_alignment"]["status"] == "complete"
        assert record["audio_alignment"]["covered_seconds"] == 3
        assert record["audio_alignment"]["wait_seconds"] < 0.5
        assert not record["audio_evidence"] and record["audio_delta"] == ""
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_disabled_alignment_never_waits_and_shutdown_cancels_wait(tmp_path):
    agent, source = make_agent(tmp_path)
    await agent.start()
    try:
        await agent.offer_frame(source, 10)
        await agent.offer_frame(source, 13)
        await until(lambda: agent._observed == 1)
        record = agent.observations[-1]
        assert record["audio_alignment"]["status"] == "unavailable"
        assert record["audio_wait_seconds"] < 0.01
        agent.audio_alignment_wait_seconds = 10
        await agent.offer_frame(source, 14)
        await until(lambda: bool(agent._inflight))
        await asyncio.wait_for(agent.stop(), 0.2)
        assert not agent._leases and not agent._inflight and not agent._observer_tasks
    finally:
        await agent.stop()


def test_completed_coverage_does_not_bridge_failed_gaps_and_stays_bounded(tmp_path):
    agent, source = make_agent(tmp_path, wait=1)
    agent.add_audio_progress({"start": 9, "end": 11, "status": "complete"})
    agent.add_audio_progress({"start": 11, "end": 12, "status": "failed"})
    agent.add_audio_progress({"start": 12, "end": 14, "status": "complete"})
    alignment = agent._audio_alignment(window(source))
    assert alignment["status"] == "partial" and alignment["covered_seconds"] == 2
    for number in range(1000):
        agent.add_audio_progress({"start": number * 4, "end": number * 4 + 1, "status": "complete"})
    assert len(agent._audio_completed_ranges) <= 200
    assert agent._audio_completed_ranges[-1][1] - agent._audio_completed_ranges[0][0] <= 900


def test_restored_legacy_context_and_memory_migrate_to_evidence_time(tmp_path):
    agent, _ = make_agent(tmp_path)
    state = {"observations": [{"id": 1, "kind": "context", "timestamp": 123,
              "received_at": 1700000123, "audio_evidence": [audio("past", 10.25, 12.75)],
              "danmaku_evidence": [], "description": "迟到声音", "delta": "你好"}],
             "stored_memory": {"working": [{"id": 1, "t": 123, "kind": "context", "text": "你好"}]}}
    agent.restore_from_state(state)
    record = agent.observations[-1]
    assert record["timestamp"] == record["source_end"] == 12.75
    assert record["recorded_at"] == 123 and record["generated_at"] == 1700000123
    assert agent.memory.events[-1].t == 12.75
    working = agent.snapshot()["memory"]["working"][-1]
    assert working["t"] == working["source_end"] == 12.75
    assert state["observations"][0]["timestamp"] == 123


def test_late_audio_is_not_timestamped_with_unrelated_new_chat(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.add_audio_segment(audio("late", 10.25, 12.75))
    agent.add_danmaku({"text": "新弹幕", "timestamp": 80})
    first = agent._flush_context()
    second = agent._flush_context()
    assert first["source_tags"] == ["audio"] and first["timestamp"] == 12.75
    assert second["source_tags"] == ["danmaku"] and second["timestamp"] == 80
    assert first["recorded_at"] >= 80 and second["recorded_at"] >= 80
    assert agent._flush_context() is None
    assert [event.t for event in agent.memory.events] == [12.75, 80]
