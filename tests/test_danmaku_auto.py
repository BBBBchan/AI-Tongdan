import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace

import pytest
import pytest_asyncio

import app.danmaku_auto as auto_module
from app.danmaku_auto import AutoDanmaku
from app.danmaku_send import DanmakuSendError


class Clock:
    def __init__(self):
        self.wall = 1_700_000_000.0
        self.mono = 100.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class Workspace:
    """All room resolution and external sends stay in memory."""

    def __init__(self, path, clock):
        self.settings = SimpleNamespace(data_dir=path)
        self.lock = asyncio.Lock()
        self.session = {"id": "current-session", "status": "live"}
        self.room = SimpleNamespace(room_id=123, live_status=1)
        self.configured = True
        self.danmaku_credentials = SimpleNamespace(status=lambda: {"configured": self.configured})
        self.age = 2.0
        self.agent = SimpleNamespace(_observer_snapshot=lambda: {"understanding_age_seconds": self.age})
        self.danmaku = []
        self.danmaku_candidates = {}
        self.last_danmaku_sent_at = None
        self.clock = clock
        self.events = []
        self.attempts = []
        self.resolve_calls = []
        self.resolved_room = self.room
        self.resolve_error = self.send_error = None
        self.resolve_delay = self.send_delay = 0
        self.client = SimpleNamespace(resolve_room=self.resolve)

    async def emit(self, kind, data):
        self.events.append((kind, data))

    async def resolve(self, room_id):
        self.resolve_calls.append(room_id)
        self.clock.advance(self.resolve_delay)
        if self.resolve_error:
            raise self.resolve_error
        return self.resolved_room

    async def _send_danmaku_locked(self, sid, text, *, before_post=None):
        assert self.lock.locked(), "External sends must share the room-switch lock"
        assert sid == self.session["id"]
        self.clock.advance(self.send_delay)
        if before_post is not None:
            before_post()
        self.attempts.append({"sid": sid, "text": text, "mono": self.clock.monotonic()})
        if self.send_error:
            raise self.send_error
        self.last_danmaku_sent_at = self.clock.monotonic()

    def seed(self, text="杯子抢镜了", **overrides):
        self.danmaku_candidates = {
            "session_id": self.session["id"], "generated_at": self.clock.time(),
            "sources": {"visual": True}, "status": "ready", "items": [{"text": text}],
            **overrides,
        }


@pytest_asyncio.fixture
async def trial(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(auto_module, "time", clock)
    ws = Workspace(tmp_path, clock)
    auto = AutoDanmaku(ws)

    async def parked_run():
        await asyncio.Event().wait()

    # Most cases exercise tick explicitly, under its documented caller lock.
    monkeypatch.setattr(auto, "run", parked_run)
    try:
        yield auto, ws, clock
    finally:
        if auto.task:
            auto.task.cancel()
            with suppress(asyncio.CancelledError):
                await auto.task


async def tick(auto):
    async with auto.workspace.lock:
        await auto.tick()


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [("stop", "stopped"), ("deadline", "completed"), ("stale", "active")])
async def test_rechecks_trial_at_post_boundary_after_authentication(trial, change, expected):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    ws.seed()
    if change == "deadline":
        clock.advance(1799)
        ws.seed()

    async def authentication_then_guard(sid, text, *, before_post):
        if change == "stop":
            auto.stop_requested = True
        clock.advance(31 if change == "stale" else 2)
        before_post()
        pytest.fail("Stopped, expired or stale trial must not POST")

    ws._send_danmaku_locked = authentication_then_guard
    await tick(auto)
    assert auto.data["status"] == expected
    assert auto.data["sent_count"] == 0
    assert auto.data["history"][0]["status"] == "not_sent"
    assert not ws.attempts


@pytest.mark.asyncio
async def test_storage_failure_does_not_leave_a_trial_active(trial, monkeypatch):
    auto, ws, _ = trial

    def fail_storage():
        raise OSError("disk full")

    monkeypatch.setattr(auto, "_persist", fail_storage)
    with pytest.raises(DanmakuSendError) as error:
        await auto.start(ws.session["id"])
    assert error.value.code == "storage_unavailable"
    assert auto.data["status"] == "error" and auto.task is None
    assert not ws.attempts


@pytest.mark.asyncio
async def test_starts_disabled_and_never_restores_an_active_trial(tmp_path):
    clock = Clock()
    ws = Workspace(tmp_path, clock)
    folder = tmp_path / "sessions" / ws.session["id"]
    folder.mkdir(parents=True)
    (folder / "danmaku_auto.private.json").write_text(json.dumps({
        "status": "active", "sent_count": 2, "session_id": ws.session["id"],
    }))
    auto = AutoDanmaku(ws)
    await tick(auto)
    assert auto.snapshot()["status"] == "idle"
    assert auto.task is None and ws.attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"interval_seconds": 299}, {"duration_seconds": 1801}, {"max_messages": 7},
])
async def test_only_authorized_trial_limits_are_accepted(trial, kwargs):
    auto, ws, _ = trial
    with pytest.raises(DanmakuSendError):
        await auto.start(ws.session["id"], **kwargs)
    assert auto.task is None and auto.data["status"] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["session", "offline", "disconnected", "credentials"])
async def test_start_requires_current_live_session_and_login(trial, invalid):
    auto, ws, _ = trial
    sid = ws.session["id"]
    if invalid == "session":
        sid = "old-session"
    elif invalid == "offline":
        ws.room.live_status = 0
    elif invalid == "disconnected":
        ws.session["status"] = "waiting"
    else:
        ws.configured = False
    with pytest.raises(DanmakuSendError):
        await auto.start(sid)
    assert auto.task is None and ws.attempts == []


@pytest.mark.asyncio
async def test_repeated_start_preserves_quota_deadline_and_single_task(trial):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    ws.seed()
    await tick(auto)
    task, deadline, next_due = auto.task, auto.deadline, auto.next_due
    clock.advance(15)
    again = await auto.start(ws.session["id"])
    assert again["sent_count"] == 1 and len(again["history"]) == 1
    assert auto.task is task and auto.deadline == deadline and auto.next_due == next_due
    assert auto.sent_texts == {"杯子抢镜了"}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "expired", "future", "before-start", "no-sources", "duplicate", "copied", "old-session",
    "late-understanding", "missing-understanding", "nan-understanding", "long", "control", "surrogate",
])
async def test_stale_repeated_copied_or_invalid_candidates_are_skipped(trial, invalid):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    clock.advance(40)
    ws.seed()
    if invalid == "expired":
        ws.danmaku_candidates["generated_at"] = clock.time() - 31
    elif invalid == "future":
        ws.danmaku_candidates["generated_at"] = clock.time() + 1
    elif invalid == "before-start":
        ws.danmaku_candidates["generated_at"] = auto.data["started_at"] - 1
    elif invalid == "no-sources":
        ws.danmaku_candidates["sources"] = {}
    elif invalid == "duplicate":
        auto.sent_texts.add("杯子抢镜了")
    elif invalid == "copied":
        ws.danmaku = [{"text": "  杯子抢镜了  "}]
    elif invalid == "old-session":
        ws.danmaku_candidates["session_id"] = "previous-session"
    elif invalid == "late-understanding":
        ws.age = 15.1
    elif invalid == "missing-understanding":
        ws.age = None
    elif invalid == "nan-understanding":
        ws.age = float("nan")
    else:
        ws.seed({"long": "😀" * 11, "control": "有\n换行", "surrogate": "\ud800"}[invalid])
    await tick(auto)
    assert ws.attempts == [] and ws.resolve_calls == []
    assert auto.data["skipped_count"] == 1
    assert auto.next_due == clock.monotonic() + 300


@pytest.mark.asyncio
async def test_uses_latest_candidate_and_spaces_successes_from_completion(trial):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    ws.seed("旧候选")
    ws.seed("刚刚举杯了")
    ws.send_delay = 5
    await tick(auto)
    assert ws.attempts[0]["text"] == "刚刚举杯了"
    first_completed = clock.monotonic()
    clock.advance(299)
    ws.seed("杯子又回来了")
    await tick(auto)
    assert len(ws.attempts) == 1
    clock.advance(1)
    await tick(auto)
    assert len(ws.attempts) == 2
    assert ws.attempts[1]["mono"] - first_completed >= 300


@pytest.mark.asyncio
async def test_missed_clock_slots_never_create_a_catch_up_burst(trial):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    ws.seed()
    await tick(auto)
    clock.advance(950)
    ws.seed("这次换红杯子了")
    await tick(auto)
    for _ in range(5):
        await tick(auto)
    assert len(ws.attempts) == 2
    assert auto.next_due == clock.monotonic() + 300


@pytest.mark.asyncio
async def test_stops_at_six_messages_without_a_seventh(trial):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    for index in range(6):
        if index:
            clock.advance(300)
        ws.seed(f"候选{index}")
        await tick(auto)
    assert auto.data["status"] == "completed" and auto.data["sent_count"] == 6
    assert auto.data["next_send_at"] is None
    clock.advance(300)
    ws.seed("不应发送第七条")
    await tick(auto)
    assert len(ws.attempts) == 6


@pytest.mark.asyncio
async def test_expired_trial_stops_without_sending_even_with_fresh_candidate(trial):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    clock.advance(1800)
    ws.seed()
    await tick(auto)
    assert auto.data["status"] == "completed" and ws.attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["session", "room", "offline", "disconnected", "missing-room"])
async def test_room_or_session_change_stops_before_external_calls(trial, change):
    auto, ws, _ = trial
    await auto.start(ws.session["id"])
    ws.seed()
    if change == "session":
        ws.session = {"id": "new-session", "status": "live"}
    elif change == "room":
        ws.room = SimpleNamespace(room_id=456, live_status=1)
    elif change == "offline":
        ws.room.live_status = 0
    elif change == "disconnected":
        ws.session["status"] = "waiting"
    else:
        ws.room = None
    await tick(auto)
    assert auto.data["status"] == "stopped"
    assert ws.attempts == ws.resolve_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["offline", "changed-room", "network-error", "timeout"])
async def test_room_recheck_failure_stops_without_sending(trial, result):
    auto, ws, _ = trial
    await auto.start(ws.session["id"])
    ws.seed()
    if result == "offline":
        ws.resolved_room = SimpleNamespace(room_id=123, live_status=0)
    elif result == "changed-room":
        ws.resolved_room = SimpleNamespace(room_id=456, live_status=1)
    else:
        ws.resolve_error = RuntimeError("offline") if result == "network-error" else asyncio.TimeoutError()
    await tick(auto)
    assert auto.data["status"] in {"stopped", "error"}
    assert ws.attempts == [] and len(ws.resolve_calls) == 1


@pytest.mark.asyncio
async def test_candidate_expiring_during_room_lookup_is_not_sent(trial):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    ws.seed()
    clock.advance(25)
    ws.resolve_delay = 6
    await tick(auto)
    assert ws.attempts == [] and auto.data["skipped_count"] == 1


@pytest.mark.asyncio
async def test_deadline_reached_during_room_lookup_is_not_sent(trial):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    clock.advance(1795)
    ws.seed()
    ws.resolve_delay = 6
    await tick(auto)
    assert auto.data["status"] == "completed" and ws.attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code,history_status", [
    (504, "send_unknown", "unknown"), (401, "login_required", "failed"),
])
async def test_unknown_or_unauthorized_send_stops_without_retry(trial, status, code, history_status):
    auto, ws, clock = trial
    await auto.start(ws.session["id"])
    ws.seed()
    ws.send_error = DanmakuSendError("发送未确认", status, code)
    await tick(auto)
    assert auto.data["status"] == "error" and auto.data["sent_count"] == 0
    assert auto.data["history"][0]["status"] == history_status
    assert auto.data["history"][0]["code"] == code
    ws.send_error = None
    clock.advance(300)
    ws.seed("不应自动重试")
    await tick(auto)
    assert len(ws.attempts) == 1


@pytest.mark.asyncio
async def test_stop_under_workspace_lock_cancels_waiting_runner_without_deadlock(trial):
    auto, ws, _ = trial
    await auto.start(ws.session["id"])
    auto.task.cancel()
    with suppress(asyncio.CancelledError):
        await auto.task
    ws.seed()
    async with ws.lock:
        # The production runner queues for the lock currently held by stop.
        runner = auto.task = asyncio.create_task(AutoDanmaku.run(auto))
        await asyncio.sleep(0)
        assert not runner.done()
        await asyncio.wait_for(auto.stop(), timeout=1)
    assert runner.cancelled() and auto.task is None
    await tick(auto)
    assert auto.data["status"] == "stopped" and ws.attempts == []


@pytest.mark.asyncio
async def test_stop_requested_during_room_lookup_prevents_post_before_lock_is_free(trial):
    auto, ws, _ = trial
    await auto.start(ws.session["id"])
    ws.seed()
    resolving, release = asyncio.Event(), asyncio.Event()

    async def slow_resolve(room_id):
        resolving.set()
        await release.wait()
        return ws.room

    async def stop_route():
        # The API marks intent before waiting for the shared workspace lock.
        auto.stop_requested = True
        async with ws.lock:
            await auto.stop()

    ws.client.resolve_room = slow_resolve
    checking = asyncio.create_task(tick(auto))
    stopping = None
    try:
        await asyncio.wait_for(resolving.wait(), timeout=1)
        stopping = asyncio.create_task(stop_route())
        await asyncio.sleep(0)
        assert not stopping.done() and ws.lock.locked()
        release.set()
        await asyncio.wait_for(asyncio.gather(checking, stopping), timeout=1)
        assert auto.data["status"] == "stopped" and ws.attempts == []
    finally:
        for task in (checking, stopping):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
async def test_first_candidate_warmup_does_not_send_a_preexisting_draft(trial):
    auto, ws, clock = trial
    ws.seed("启动前的候选", generated_at=clock.time() - 1)
    await auto.start(ws.session["id"])
    await tick(auto)
    assert ws.attempts == [] and auto.data["skipped_count"] == 0
    assert auto.next_due == clock.monotonic() + 2
    clock.advance(2)
    ws.seed("现在的新候选")
    await tick(auto)
    assert [item["text"] for item in ws.attempts] == ["现在的新候选"]


@pytest.mark.asyncio
async def test_concurrent_room_switch_wins_lock_and_prevents_queued_send(trial):
    auto, ws, _ = trial
    await auto.start(ws.session["id"])
    ws.seed()
    async with ws.lock:
        queued_tick = asyncio.create_task(tick(auto))
        await asyncio.sleep(0)
        ws.session = {"id": "next-session", "status": "live"}
        ws.room = SimpleNamespace(room_id=456, live_status=1)
    await asyncio.wait_for(queued_tick, timeout=1)
    assert auto.data["status"] == "stopped" and ws.attempts == []
