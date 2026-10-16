"""Offline checks for continuing one bounded, candidate-only patrol run."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

import scripts.patrol_following as patrol_module


class OfflineHTTP:
    async def request(self, *args, **kwargs):
        raise AssertionError("Resume tests must not contact a live service")

    async def aclose(self):
        pass


@pytest.fixture
def continuation(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(patrol_module.time, "time", lambda: clock.now)
    settings = SimpleNamespace(data_dir=tmp_path / "data", ffmpeg_bin="unused", bilibili_cookie="")
    settings.data_dir.mkdir()
    monkeypatch.setattr(patrol_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(patrol_module.httpx, "AsyncClient", lambda *a, **k: OfflineHTTP())
    args = SimpleNamespace(
        run_dir=tmp_path / "run", base_url="http://127.0.0.1:1", hours=24,
        dwell=900, interval=180, max_messages=5, warmup=45,
        max_recording_gib=30, send=False, candidate_count=10, virtual_fallback=True,
        first_room=213, first_dwell=1800, first_max_messages=10,
        action="resume", resume_visit="001-213",
    )
    patrol = patrol_module.Patrol(args)
    patrol.log = lambda *a, **k: None
    started = clock.now - 1200
    visit = {
        "visit_id": "001-213", "room": {"room_id": 47867, "short_id": 213},
        "requested_room_id": 213, "session_id": "owned-session",
        "started_at": started - 3, "session_started_at": started,
        "scheduled_end_at": started + 1800, "status": "finished", "ended_at": clock.now,
        "sent_count": 0, "auto_started": False, "candidate_batches": 100,
        "candidate_total": 1000, "recordings": [{"file": "recordings/part-001.mkv", "bytes": 900}],
        "lag_samples": [],
    }
    saved = copy.deepcopy(patrol.state)
    saved.update(started_at=started - 3, deadline=started - 3 + 86400, status="stopped",
                 ended_at=clock.now, visits=[visit["visit_id"]], current_visit=None,
                 last_owned_session=visit["session_id"], recording_bytes=900)
    patrol_module.write_json(patrol.root / "status.json", saved)
    patrol_module.write_json(patrol.root / "visits" / visit["visit_id"] / "visit.json", visit)
    return patrol, clock, saved, visit


def save_plan(patrol):
    plan = {
        "questions_enabled": True, "question_interval_seconds": 165,
        "rooms": [
            {"plan_id": "requested-song", "room_id": 510, "dwell_seconds": 1800},
            {"plan_id": "requested-next", "room_id": 21452505, "dwell_seconds": 900},
        ],
    }
    patrol_module.write_json(patrol.root / "plan.json", plan)
    return plan


def attach_visit(patrol, visit):
    patrol.visit = copy.deepcopy(visit)
    patrol.visit_dir = patrol.root / "visits" / visit["visit_id"]
    patrol.owned_sid = visit["session_id"]
    patrol.state["config"]["simulation_rounds_per_visit"] = 10
    return patrol.visit


def sample_snapshot(patrol, clock, *, count=5):
    return {
        "session": {"id": patrol.owned_sid},
        "observer": {"last_timestamp": 123.5},
        "danmaku_candidates": {
            "session_id": patrol.owned_sid, "status": "ready", "generated_at": clock.now,
            "items": [{"text": f"候选{i}", "reason": "当前内容"} for i in range(count)],
        },
        "danmaku_chorus": {
            "status": "ready", "items": [{"text": f"跟刷{i}", "count": i + 2} for i in range(11)],
            "window_seconds": 30,
        },
    }


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def test_restore_preserves_run_and_visit_deadlines_and_owned_session(continuation):
    patrol, clock, saved, visit = continuation
    patrol.restore()
    assert patrol.state["started_at"] == saved["started_at"]
    assert patrol.state["deadline"] == saved["deadline"]
    assert patrol.state["deadline"] != clock.now + 86400
    assert patrol.state["last_owned_session"] == visit["session_id"]
    assert patrol.state["recording_bytes"] == 900
    assert patrol.resume_visit["scheduled_end_at"] == visit["scheduled_end_at"]
    assert patrol.resume_visit["session_started_at"] == visit["session_started_at"]
    assert patrol.resume_visit["candidate_batches"] == 100
    assert patrol.resume_visit["recordings"] == visit["recordings"]
    assert patrol.state["visits"] == [visit["visit_id"]]


@pytest.mark.parametrize("reason", ["send_requested", "old_send_run", "user_takeover"])
def test_restore_rejects_unsafe_resume(continuation, reason):
    patrol, _, saved, _ = continuation
    if reason == "send_requested":
        patrol.args.send = True
    elif reason == "old_send_run":
        saved["config"]["candidate_only"] = False
    else:
        saved["status"] = "user_took_over"
    patrol_module.write_json(patrol.root / "status.json", saved)
    with pytest.raises(ValueError):
        patrol.restore()


def test_expired_station_is_not_reopened_or_extended(continuation):
    patrol, clock, saved, visit = continuation
    clock.now = visit["scheduled_end_at"] + 1
    patrol.restore()
    assert patrol.resume_visit is None
    assert patrol.state["deadline"] == saved["deadline"]
    assert patrol.visits[-1]["scheduled_end_at"] == visit["scheduled_end_at"]


@pytest.mark.parametrize("allowed", [False, True])
def test_manual_permission_restores_without_enabling_automatic_sending(continuation, allowed):
    patrol, _, saved, visit = continuation
    saved["config"]["allow_manual_send"] = allowed
    patrol_module.write_json(patrol.root / "status.json", saved)
    patrol.restore()
    assert patrol.args.allow_manual_send is allowed
    assert patrol.args.send is False
    assert patrol.state["deadline"] == saved["deadline"]
    assert patrol.resume_visit["scheduled_end_at"] == visit["scheduled_end_at"]


def test_missing_manual_permission_cannot_be_granted_by_resume_cli(continuation):
    patrol, _, saved, _ = continuation
    saved["config"].pop("allow_manual_send", None)
    patrol.args.allow_manual_send = True
    patrol_module.write_json(patrol.root / "status.json", saved)
    patrol.restore()
    assert patrol.args.allow_manual_send is False


@pytest.mark.parametrize("value", ["true", 1, None])
def test_bad_saved_manual_permission_fails_closed(continuation, value):
    patrol, _, saved, _ = continuation
    saved["config"]["allow_manual_send"] = value
    patrol_module.write_json(patrol.root / "status.json", saved)
    with pytest.raises(ValueError, match="manual send authorization"):
        patrol.restore()


def test_expired_global_deadline_remains_inactive_after_restore(continuation):
    patrol, clock, saved, _ = continuation
    clock.now = saved["deadline"] + 1
    patrol.restore()
    assert not patrol.active()
    assert patrol.state["deadline"] == saved["deadline"]


def test_explicit_stop_file_is_not_removed_by_resume(continuation):
    patrol, _, _, _ = continuation
    (patrol.root / "STOP").touch()
    patrol.restore()
    assert (patrol.root / "STOP").exists()
    assert not patrol.active()


def test_restore_only_allows_last_healthy_visit(continuation):
    patrol, _, saved, visit = continuation
    next_visit = {**visit, "visit_id": "002-510", "room": {"room_id": 80397, "short_id": 510}}
    saved["visits"].append(next_visit["visit_id"])
    patrol_module.write_json(patrol.root / "status.json", saved)
    patrol_module.write_json(patrol.root / "visits" / next_visit["visit_id"] / "visit.json", next_visit)
    with pytest.raises(ValueError):
        patrol.restore()


def test_restore_rejects_failed_visit(continuation):
    patrol, _, _, visit = continuation
    visit["error"] = "service_unavailable"
    patrol_module.write_json(patrol.root / "visits" / visit["visit_id"] / "visit.json", visit)
    with pytest.raises(ValueError):
        patrol.restore()


def test_plan_changes_future_generation_to_five_without_rewriting_old_records(continuation):
    patrol, _, saved, visit = continuation
    save_plan(patrol)
    patrol.restore()
    assert patrol.args.candidate_count == 5
    assert patrol.state["config"]["candidate_count"] == 5
    assert patrol.state["config"]["simulation_rounds_per_visit"] == 10
    assert patrol.state["config"]["simulation_scope"] == "per_visit_evenly_spaced"
    assert patrol.resume_visit["candidate_total"] == 1000
    assert patrol.resume_visit["session_id"] == visit["session_id"]
    assert patrol.state["deadline"] == saved["deadline"]


@pytest.mark.asyncio
async def test_requested_queue_runs_once_before_following_rooms(continuation):
    patrol, _, _, _ = continuation
    save_plan(patrol)
    patrol.restore()
    calls = []

    async def following():
        calls.append("following")
        return [{"room_id": 555, "title": "关注开播房"}]

    async def virtual():
        raise AssertionError("Live followed rooms must take precedence over virtual discovery")

    patrol.following, patrol.virtual_rooms = following, virtual
    first = await patrol.next_room()
    assert first["room_id"] == 510
    assert first["dwell_seconds"] == 1800
    assert not calls
    patrol.visits.append({"room": {**first, "room_id": 80397}, "started_at": 20})
    second = await patrol.next_room()
    assert second["room_id"] == 21452505
    assert second["dwell_seconds"] == 900
    assert not calls
    patrol.visits.append({"room": second, "started_at": 30})
    third = await patrol.next_room()
    assert third["room_id"] == 555
    assert third["selection_source"] == "following"
    assert calls == ["following"]


@pytest.mark.parametrize("duration", [900, 1800])
def test_simulation_has_ten_evenly_spaced_midpoints(continuation, duration):
    patrol, _, _, visit = continuation
    visit["scheduled_end_at"] = visit["session_started_at"] + duration
    attach_visit(patrol, visit)
    times = patrol.simulation_targets()
    assert len(times) == 10
    assert times[0] == visit["session_started_at"] + duration / 20
    assert times[-1] == visit["scheduled_end_at"] - duration / 20
    assert all(b - a == duration / 10 for a, b in zip(times, times[1:]))


@pytest.mark.asyncio
async def test_hot_room_insert_preserves_current_visit_and_carries_focus_once(continuation):
    patrol, _, saved, visit = continuation
    plan = save_plan(patrol)
    patrol.restore()
    assert (await patrol.next_room())["room_id"] == 510
    requested = {"plan_id": "requested-game-room", "room_id": 82568,
                 "dwell_seconds": 1800, "question_focus": "game_and_speech"}
    plan["rooms"].insert(0, requested)
    patrol_module.write_json(patrol.root / "plan.json", plan)
    room = await patrol.next_room()
    assert room["room_id"] == 82568
    assert room["dwell_seconds"] == 1800
    assert room["question_focus"] == "game_and_speech"
    assert patrol.state["deadline"] == saved["deadline"]
    assert patrol.resume_visit["scheduled_end_at"] == visit["scheduled_end_at"]
    assert patrol.resume_visit["session_id"] == visit["session_id"]
    patrol.visits.append({"room": {**room, "room_id": 22886883}, "started_at": 30})
    assert (await patrol.next_room())["room_id"] == 510


def test_each_simulation_preserves_five_candidates_and_all_chorus_without_sending(continuation):
    patrol, clock, _, visit = continuation
    attach_visit(patrol, visit)
    for target in patrol.simulation_targets():
        clock.now = target
        patrol.sample_simulation(sample_snapshot(patrol, clock))
    rows = jsonl(patrol.visit_dir / "simulation_rounds.jsonl")
    assert [row["round"] for row in rows] == list(range(1, 11))
    assert all(row["data"]["sent"] is False for row in rows)
    assert all(row["data"]["status"] == "complete" for row in rows)
    assert all(len(row["data"]["candidates"]["items"]) == 5 for row in rows)
    assert all(len(row["data"]["chorus"]["items"]) == 11 for row in rows)
    assert patrol.visit["simulation_completed"] == 10
    assert patrol.visit["sent_count"] == 0
    patrol.sample_simulation(sample_snapshot(patrol, clock), final=True)
    assert jsonl(patrol.visit_dir / "simulation_rounds.jsonl") == rows


def test_legacy_ten_candidate_source_kept_alongside_selected_five(continuation):
    patrol, clock, _, visit = continuation
    attach_visit(patrol, visit)
    clock.now = patrol.simulation_targets()[0]
    snapshot = sample_snapshot(patrol, clock, count=10)
    original = copy.deepcopy(snapshot)
    patrol.sample_simulation(snapshot)
    data = jsonl(patrol.visit_dir / "simulation_rounds.jsonl")[0]["data"]
    assert len(data["candidates"]["items"]) == 5
    assert data["source_candidate_count"] == 10
    assert data["candidate_selection"] == "first_5_of_legacy_10"
    assert data["raw_candidates"] == original["danmaku_candidates"]
    assert data["chorus"] == original["danmaku_chorus"]
    assert snapshot == original


@pytest.mark.parametrize("invalid", ["old_session", "stale", "future", "not_ready", "too_few"])
def test_simulation_waits_for_fresh_complete_owned_candidates(continuation, invalid):
    patrol, clock, _, visit = continuation
    attach_visit(patrol, visit)
    clock.now = patrol.simulation_targets()[0]
    snap = sample_snapshot(patrol, clock)
    candidate = snap["danmaku_candidates"]
    if invalid == "old_session":
        candidate["session_id"] = "other-session"
    elif invalid == "stale":
        candidate["generated_at"] -= 36
    elif invalid == "future":
        candidate["generated_at"] += 1
    elif invalid == "not_ready":
        candidate["status"] = "generating"
    else:
        candidate["items"] = candidate["items"][:4]
    patrol.sample_simulation(snap)
    assert not jsonl(patrol.visit_dir / "simulation_rounds.jsonl")


def test_missed_slots_are_not_backfilled_from_one_new_snapshot(continuation):
    patrol, clock, _, visit = continuation
    attach_visit(patrol, visit)
    clock.now = patrol.simulation_targets()[4]
    patrol.sample_simulation(sample_snapshot(patrol, clock))
    rows = jsonl(patrol.visit_dir / "simulation_rounds.jsonl")
    assert [row["round"] for row in rows] == [1, 2, 3, 4, 5]
    assert [row["data"]["status"] for row in rows] == ["skipped"] * 4 + ["complete"]
    assert all("candidates" not in row["data"] for row in rows[:4])
    assert all(row["data"]["chorus_capture_note"] for row in rows[:4])
    assert patrol.visit["simulation_completed"] == 1


def test_snapshot_captures_qa_and_full_chorus_without_duplicate_poll_rows(continuation):
    patrol, clock, _, visit = continuation
    attach_visit(patrol, visit)
    snap = sample_snapshot(patrol, clock)
    snap["messages"] = [
        {"role": "user", "question_id": "q1", "content": "现在在唱什么歌", "status": "complete"},
        {"role": "assistant", "question_id": "q1", "content": "当前线索不足以确定歌名", "status": "complete"},
    ]
    patrol.snapshot_events(snap)
    patrol.snapshot_events(snap)
    questions = jsonl(patrol.visit_dir / "questions.jsonl")
    chorus = jsonl(patrol.visit_dir / "chorus.jsonl")
    assert [row["type"] for row in questions] == ["question_started", "answer_done"]
    assert len(chorus) == 1
    assert len(chorus[0]["data"]["items"]) == 11


def prepare_resumed_visit(patrol, clock, visit):
    """Keep the real visit controller while replacing all external effects."""
    patrol.restore()
    snap = sample_snapshot(patrol, clock)
    snap["session"].update(id=visit["session_id"], status="live", candidate_only=True,
                           candidate_count=10, started_at=visit["session_started_at"])
    snap["room"] = {**visit["room"], "live_status": 1}
    snap["observer"].update(model_ready=True, understanding_age_seconds=2, observed=3)
    snap["danmaku_candidates"]["session_id"] = visit["session_id"]
    snap["danmaku_auto"] = {"status": "idle", "sent_count": 0}
    snap["sent_danmaku"] = []
    calls = []

    async def request(method, path, payload=None):
        calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == "/api/state":
            return copy.deepcopy(snap)
        if method == "POST" and path == "/api/danmaku/auto/stop":
            assert payload["session_id"] == visit["session_id"]
            return {"status": "stopped"}
        raise AssertionError(f"Unexpected operation while adopting an owned session: {method} {path}")

    async def delay(seconds):
        clock.now += max(seconds, 300)
        await asyncio.sleep(0)

    async def noop(*a, **k):
        pass

    patrol.request, patrol.delay = request, delay
    patrol.archive_media = lambda *a: None
    patrol.ensure_recording = patrol.stop_recording = noop
    patrol.stream_events = patrol.report = noop
    return snap, calls


@pytest.mark.asyncio
async def test_resume_adopts_same_session_without_posting_a_new_session(continuation):
    patrol, clock, saved, visit = continuation
    snap, calls = prepare_resumed_visit(patrol, clock, visit)
    await patrol.visit_room(patrol.resume_visit["room"], resume_visit=patrol.resume_visit)
    assert not [c for c in calls if c[0] == "POST" and c[1] != "/api/danmaku/auto/stop"]
    assert len(patrol.visits) == 1
    assert patrol.state["visits"] == [visit["visit_id"]]
    assert patrol.state["last_owned_session"] == visit["session_id"]
    assert patrol.visits[0]["session_id"] == visit["session_id"]
    assert patrol.visits[0]["scheduled_end_at"] == visit["scheduled_end_at"]
    assert patrol.visits[0]["session_started_at"] == visit["session_started_at"]
    assert patrol.state["deadline"] == saved["deadline"]
    assert patrol.visits[0]["sent_count"] == 0
    assert snap["session"]["candidate_count"] == 10  # Existing session is not silently recreated.


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["session", "room", "stopped"])
async def test_resume_mismatch_stops_without_mutating_any_session(continuation, mismatch):
    patrol, clock, _, visit = continuation
    snap, calls = prepare_resumed_visit(patrol, clock, visit)
    if mismatch == "session":
        snap["session"]["id"] = "user-selected-session"
    elif mismatch == "room":
        snap["room"]["room_id"] = 999
    else:
        snap["session"]["status"] = "stopped"
    await patrol.visit_room(patrol.resume_visit["room"], resume_visit=patrol.resume_visit)
    assert patrol.stop.is_set()
    assert patrol.state["status"] in {"user_took_over", "resume_session_mismatch"}
    assert not [c for c in calls if c[0] == "POST"]


@pytest.mark.asyncio
async def test_resume_rejects_session_that_lost_candidate_only_guard(continuation):
    patrol, clock, _, visit = continuation
    snap, calls = prepare_resumed_visit(patrol, clock, visit)
    snap["session"]["candidate_only"] = False
    await patrol.visit_room(patrol.resume_visit["room"], resume_visit=patrol.resume_visit)
    assert patrol.stop.is_set()
    assert patrol.state["status"] == "candidate_guard_unavailable"
    assert not [c for c in calls if c[0] == "POST" and c[1] != "/api/danmaku/auto/stop"]


@pytest.mark.asyncio
async def test_resume_rebuilds_candidate_dedup_from_previous_events(continuation):
    patrol, clock, _, visit = continuation
    snap, _ = prepare_resumed_visit(patrol, clock, visit)
    visit_dir = patrol.root / "visits" / visit["visit_id"]
    previous = {"time": clock.now, "session_id": visit["session_id"],
                "type": "danmaku_candidates", "data": snap["danmaku_candidates"]}
    patrol_module.append(visit_dir / "events.jsonl", previous)
    await patrol.visit_room(patrol.resume_visit["room"], resume_visit=patrol.resume_visit)
    assert patrol.visits[0]["candidate_batches"] == 100
    assert patrol.visits[0]["candidate_total"] == 1000
    assert not jsonl(visit_dir / "candidates.jsonl")
    candidate_events = [row for row in jsonl(visit_dir / "events.jsonl") if row["type"] == "danmaku_candidates"]
    assert candidate_events == [previous]


def test_finalization_does_not_fill_simulation_slots_in_the_future(continuation):
    patrol, clock, _, visit = continuation
    attach_visit(patrol, visit)
    clock.now = patrol.simulation_targets()[3] + 1
    patrol.sample_simulation(sample_snapshot(patrol, clock), final=True)
    rows = jsonl(patrol.visit_dir / "simulation_rounds.jsonl")
    assert [row["round"] for row in rows] == [1, 2, 3, 4]
    assert all(row["target_at"] <= clock.now for row in rows)
    assert all(row["data"]["status"] == "skipped" for row in rows)


@pytest.mark.asyncio
async def test_song_question_intent_persisted_before_post_and_timeout_not_retried(continuation):
    patrol, clock, _, visit = continuation
    visit.update(requested_room_id=510, room={"room_id": 80397, "short_id": 510})
    attach_visit(patrol, visit)
    patrol.state["config"].update(questions_enabled=True, question_interval_seconds=165)
    snap = sample_snapshot(patrol, clock)
    snap["session"].update(status="live")
    snap["observer"].update(model_ready=True, understanding_age_seconds=2)
    snap["room"] = {"room_id": 80397, "short_id": 510, "live_status": 1}
    clock.now = visit["session_started_at"] + 1200
    attempted = []

    async def uncertain_post(method, path, payload=None):
        assert (method, path) == ("POST", "/api/questions")
        recorded = json.loads((patrol.visit_dir / "visit.json").read_text())
        intent = recorded["question_history"][-1]
        assert intent["status"] == "submitting"
        assert intent["type"] == "scheduled_song"
        assert intent["scheduled_for"] == 1200
        assert intent["session_id"] == patrol.owned_sid
        attempted.append(payload)
        raise httpx.ReadTimeout("Response lost after the server may have accepted the question")

    patrol.request = uncertain_post
    await patrol.maybe_question(snap)
    recorded = json.loads((patrol.visit_dir / "visit.json").read_text())
    assert recorded["question_history"][-1]["status"] == "submission_uncertain"
    clock.now += 166
    patrol.question_history = recorded["question_history"]
    await patrol.maybe_question(snap)
    assert len(attempted) == 1
