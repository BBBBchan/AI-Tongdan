import asyncio
import json
import re
import time

import pytest

import app.agent as agent_module
from app.adaptive import AdaptiveObservation
from app.agent import LiveAgent


async def emit(*args):
    pass


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.002)
    await asyncio.wait_for(poll(), 2)


class Clock:
    value = 100.0

    def monotonic(self):
        return self.value

    def time(self):
        return 1700000000.0 + self.value

    def advance(self, seconds):
        self.value += seconds


class ControlledModel:
    configured = True

    def __init__(self, automatic=False):
        self.calls = []
        self.automatic = automatic

    async def complete(self, *args, **kwargs):
        raise AssertionError("Observations must use their dedicated admission method")

    async def complete_observation(self, messages, **kwargs):
        gate = asyncio.Event()
        if self.automatic:
            gate.set()
        text = [part["text"] for part in messages[1]["content"] if part["type"] == "text"]
        times = [float(re.search(r"采样时间 ([\d.]+) 秒", line).group(1))
                 for line in text if re.match(r"第\d+/\d+张", line)]
        context = json.loads(next(line.split("：", 1)[1] for line in text if line.startswith("分源上下文")))
        self.calls.append({"times": times, "context": context, "gate": gate})
        await gate.wait()
        return json.dumps({"description": "主播举起水杯", "visual_delta": "举杯", "changed": True})


def make_agent(tmp_path, model=None, **kwargs):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"\xff\xd8\xfftest-frame\xff\xd9")
    return LiveAgent("adaptive", tmp_path / "agent", model or ControlledModel(), emit,
                     adaptive_observation=True, **kwargs), source


def test_controller_changes_frames_and_submission_cadence_with_hysteresis():
    controller = AdaptiveObservation()
    initial_interval = controller.submit_interval
    for _ in range(3):
        controller.record(4, 2, 2, 2, 4)
    assert controller.frame_count == 4
    controller.record(4, 2, 2, 2, 4)
    assert controller.frame_count == 6
    assert controller.submit_interval > initial_interval
    # A fresh individual response cannot hide a long period without updates.
    controller.record(6, 2, 2, 14, 16)
    assert controller.frame_count == 4
    assert controller.reason == "reducing_latency"
    controller.record(4, 12, 12, 6, 18)
    assert controller.frame_count == 2
    assert controller.submit_interval == 6  # 12-second service / two workers
    for _ in range(100):
        controller.record(2, 12, 12, 6, 18)
    assert len(controller.samples[2]) == 20


@pytest.mark.asyncio
async def test_age_keeps_increasing_between_updates_and_ignores_future_media_pts(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(agent_module, "time", clock)
    agent, source = make_agent(tmp_path, ControlledModel(automatic=True), observation_concurrency=1)
    await agent.start()
    try:
        for tick in range(4):
            await agent.offer_frame(source, 10000 + tick)
        await until(lambda: agent._observed == 1)
        first = agent.snapshot()["observer"]
        assert first["understanding_age_seconds"] == 0
        assert first["source_received_at"] == clock.time()
        clock.advance(16)
        late = agent.snapshot()["observer"]
        assert late["understanding_age_seconds"] == 16
        assert late["freshness_status"] == "overdue"
        assert late["source_received_at"] == first["source_received_at"]
        await agent.offer_frame(source, 10004)
        await until(lambda: agent._observed == 2)
        record = agent.observations[-1]
        assert record["update_interval_seconds"] == 16
        assert record["peak_age_seconds"] == 16
        assert agent.snapshot()["observer"]["understanding_age_seconds"] == 0
        assert agent._adaptive.frame_count == 2
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_late_result_is_published_then_next_request_uses_latest_frames(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(agent_module, "time", clock)
    agent, source = make_agent(tmp_path, observation_concurrency=1, max_frames=8)
    await agent.start()
    try:
        for tick in range(4):
            await agent.offer_frame(source, tick)
        await until(lambda: len(agent.model.calls) == 1)
        for tick in range(4, 12):
            clock.advance(1)
            await agent.offer_frame(source, tick)
        clock.advance(20)
        agent.model.calls[0]["gate"].set()
        await until(lambda: len(agent.model.calls) == 2)
        assert agent._observed == 1
        first = agent.observations[-1]
        assert first["completion_lag"] == 28
        assert first["freshness_status"] == "overdue"
        assert agent.model.calls[1]["times"] == [10, 11]
        assert agent._pending.qsize() <= 1
        assert len(agent._inflight) == 1
        assert agent._cancelled_windows == 0
        agent.model.calls[1]["gate"].set()
        await until(lambda: agent._observed == 2)
        assert not agent._leases
        assert len(list(agent.frame_dir.iterdir())) == 8
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_refresh_after_audio_wait_rebuilds_context_and_leases_without_duplicate_submit(tmp_path):
    agent, source = make_agent(tmp_path, ControlledModel(automatic=True),
                               observation_concurrency=2, audio_alignment_wait_seconds=.06)
    await agent.start()
    try:
        for tick in range(1, 5):
            await agent.offer_frame(source, tick)
        await until(lambda: bool(agent._inflight))
        assert not agent.model.calls
        # Force a pending window during the wait, sharing the refreshed end.
        agent._last_scheduled_monotonic = None
        await agent.offer_frame(source, 5)
        await agent.offer_frame(source, 6)
        await agent.offer_frame(source, 7)
        agent.add_audio_segment({"id": "old", "start": 1, "end": 2, "text": "旧声音"})
        agent.add_audio_segment({"id": "matching", "start": 6, "end": 7, "text": "当前声音"})
        await until(lambda: agent._observed >= 1)
        await until(lambda: not agent._inflight and agent._pending.empty())
        newest = max(agent.model.calls, key=lambda call: call["times"][-1])
        assert newest["times"] == [4, 5, 6, 7]
        assert [item["id"] for item in newest["context"]["audio_untrusted"]] == ["matching"]
        assert newest["context"]["audio_alignment"]["window_end"] == 7
        ends = [call["times"][-1] for call in agent.model.calls]
        assert len(ends) == len(set(ends))
        assert not agent._leases
        assert all(frame.path.exists() for frame in agent.frames)
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_old_audio_wait_setting_is_capped_without_changing_its_configuration(tmp_path):
    agent, source = make_agent(tmp_path, ControlledModel(automatic=True), audio_alignment_wait_seconds=4)
    await agent.start()
    try:
        start = time.monotonic()
        for tick in range(4):
            await agent.offer_frame(source, tick)
        await until(lambda: agent._observed == 1)
        assert time.monotonic() - start < 1
        assert .45 <= agent.observations[-1]["audio_wait_seconds"] < .8
        assert agent.audio_alignment_wait_seconds == 4
        assert not agent.observations[-1]["audio_evidence"]
    finally:
        await agent.stop()


def test_restored_observations_do_not_claim_freshness_from_saved_wall_clock(tmp_path):
    agent, _ = make_agent(tmp_path)
    agent.restore_from_state({"observations": [{"id": 1, "kind": "observation", "timestamp": 500,
        "source_end": 500, "source_received_at": time.time(), "description": "历史画面", "delta": "历史"}],
        "observer": {"last_timestamp": 500}})
    observer = agent.snapshot()["observer"]
    assert observer["freshness_status"] == "unknown"
    assert observer["understanding_age_seconds"] is None
    assert observer["source_received_at"] is None
