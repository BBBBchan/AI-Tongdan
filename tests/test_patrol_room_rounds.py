"""Offline per-room simulation schedule checks; no live session or sender."""
import copy
import json
from types import SimpleNamespace

import pytest

import scripts.patrol_following as patrol_module


@pytest.fixture
def scheduled_patrol(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1_800_000_000.0)
    monkeypatch.setattr(patrol_module.time, "time", lambda: clock.now)
    # These methods need only scheduling state, so do not construct an HTTP
    # client or load any environment credentials in this test fixture.
    patrol = patrol_module.Patrol.__new__(patrol_module.Patrol)
    patrol.root = tmp_path
    patrol.args = SimpleNamespace(dwell=900, send=False, candidate_count=5)
    patrol.owned_sid = "current-owned-session"
    patrol.state = {"config": {"simulation_rounds_per_visit": 10},
                    "deadline": clock.now + 86400}
    patrol.visit = {
        "visit_id": "010-1713546334", "room": {"room_id": 1713546334},
        "started_at": clock.now - 3, "session_started_at": clock.now,
        "scheduled_end_at": clock.now + 1800,
        "simulation_rounds_per_visit": 15, "sent_count": 0,
    }
    patrol.visit_dir = tmp_path / "visits" / patrol.visit["visit_id"]
    patrol.visit_dir.mkdir(parents=True)
    patrol.seen = set()
    patrol.log = lambda *a, **k: None

    async def forbid_request(*a, **k):
        raise AssertionError("Simulation rounds must not invoke an HTTP API")

    patrol.request = forbid_request
    return patrol, clock


def snapshot(patrol, clock, number=1):
    return {
        "session": {"id": patrol.owned_sid},
        "observer": {"last_timestamp": clock.now - patrol.visit["session_started_at"]},
        "danmaku_candidates": {
            "session_id": patrol.owned_sid, "status": "ready", "generated_at": clock.now,
            "items": [{"text": f"第{number}轮候选{i}"} for i in range(5)],
        },
        "danmaku_chorus": {
            "session_id": patrol.owned_sid, "status": "ready",
            "items": [{"text": f"现场跟刷{i}", "count": i + 2} for i in range(12)],
        },
    }


def rounds(patrol):
    path = patrol.visit_dir / "simulation_rounds.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def plan(patrol, value):
    data = {"rooms": [{"plan_id": "requested-1713546334-30min-15rounds",
                       "room_id": 1713546334, "dwell_seconds": 1800,
                       "simulation_rounds_per_visit": value}]}
    patrol_module.write_json(patrol.root / "plan.json", data)
    return data


def test_thirty_minutes_fifteen_rounds_have_two_minute_midpoints(scheduled_patrol):
    patrol, _ = scheduled_patrol
    start = patrol.visit["session_started_at"]
    targets = patrol.simulation_targets()
    assert patrol.simulation_round_count() == 15
    assert [target - start for target in targets] == list(range(60, 1741, 120))
    assert len(targets) == 15
    assert all(b - a == 120 for a, b in zip(targets, targets[1:]))


def test_fifteen_unique_rounds_capture_five_candidates_and_all_chorus_without_sending(scheduled_patrol):
    patrol, clock = scheduled_patrol
    for number, target in enumerate(patrol.simulation_targets(), 1):
        clock.now = target
        snap = snapshot(patrol, clock, number)
        patrol.sample_simulation(snap)
        patrol.sample_simulation(snap)  # SSE/poll repetition cannot duplicate a slot.
    rows = rounds(patrol)
    assert [row["round"] for row in rows] == list(range(1, 16))
    assert all(row["data"]["status"] == "complete" for row in rows)
    assert all(row["data"]["sent"] is False for row in rows)
    assert all(len(row["data"]["candidates"]["items"]) == 5 for row in rows)
    assert all(len(row["data"]["chorus"]["items"]) == 12 for row in rows)
    assert len({row["data"]["candidates"]["generated_at"] for row in rows}) == 15
    assert patrol.visit["simulation_completed"] == 15
    assert patrol.visit["simulation_rounds"] == list(range(1, 16))
    assert patrol.visit["sent_count"] == 0
    clock.now = patrol.visit["scheduled_end_at"]
    patrol.sample_simulation(snapshot(patrol, clock), final=True)
    assert rounds(patrol) == rows


def test_freshness_prevents_reusing_one_batch_in_later_slot(scheduled_patrol):
    patrol, clock = scheduled_patrol
    targets = patrol.simulation_targets()
    clock.now = targets[0]
    old = snapshot(patrol, clock)
    patrol.sample_simulation(old)
    clock.now = targets[1]
    patrol.sample_simulation(old)
    assert len(rounds(patrol)) == 1
    clock.now += 41
    patrol.sample_simulation(old)
    rows = rounds(patrol)
    assert [r["data"]["status"] for r in rows] == ["complete", "skipped"]
    assert "candidates" not in rows[-1]["data"]


def test_count_precedence_visit_then_room_then_config_then_ten(scheduled_patrol):
    patrol, _ = scheduled_patrol
    patrol.visit["room"]["simulation_rounds_per_visit"] = 12
    patrol.state["config"]["simulation_rounds_per_visit"] = 8
    assert patrol.simulation_round_count() == 15
    patrol.visit.pop("simulation_rounds_per_visit")
    assert patrol.simulation_round_count() == 12
    patrol.visit["room"].pop("simulation_rounds_per_visit")
    assert patrol.simulation_round_count() == 8
    patrol.state["config"].pop("simulation_rounds_per_visit")
    assert patrol.simulation_round_count() == 10


def test_next_room_fifteen_round_plan_does_not_change_current_ten_round_visit(scheduled_patrol):
    patrol, _ = scheduled_patrol
    patrol.visit["simulation_rounds_per_visit"] = 10
    patrol.visit["room"] = {"room_id": 22886883, "short_id": 82568}
    before = copy.deepcopy(patrol.visit)
    deadline = patrol.state["deadline"]
    targets = patrol.simulation_targets()
    plan(patrol, 15)
    patrol.configure_plan()
    assert patrol.simulation_round_count() == 10
    assert patrol.simulation_targets() == targets
    assert patrol.visit == before
    assert patrol.state["deadline"] == deadline
    assert patrol.state["room_plan"][0]["simulation_rounds_per_visit"] == 15


def test_legacy_visit_without_count_remains_ten_when_next_room_plan_is_fifteen(scheduled_patrol):
    patrol, _ = scheduled_patrol
    patrol.visit.pop("simulation_rounds_per_visit")
    plan(patrol, 15)
    patrol.configure_plan()
    assert patrol.simulation_round_count() == 10
    assert len(patrol.simulation_targets()) == 10


def test_visit_count_alone_does_not_enable_disabled_simulation(scheduled_patrol):
    patrol, clock = scheduled_patrol
    patrol.state["config"]["simulation_rounds_per_visit"] = 0
    clock.now = patrol.simulation_targets()[0]
    patrol.sample_simulation(snapshot(patrol, clock))
    assert not rounds(patrol)


def test_grace_window_uses_actual_room_round_count(scheduled_patrol):
    patrol, clock = scheduled_patrol
    patrol.visit["simulation_rounds_per_visit"] = 60
    # A 30 s slot has a 12 s grace window, not the old 10-round 40 s window.
    clock.now = patrol.simulation_targets()[0] + 13
    patrol.sample_simulation(snapshot(patrol, clock))
    assert rounds(patrol)[0]["data"]["status"] == "skipped"


@pytest.mark.parametrize("count", [1, 10, 15, 60])
def test_plan_accepts_bounded_integer_round_count(scheduled_patrol, count):
    patrol, _ = scheduled_patrol
    expected = plan(patrol, count)
    assert patrol.read_plan() == expected


@pytest.mark.parametrize("count", [True, False, 15.0, 1.5, 0, -1, 61, "15", None])
def test_plan_rejects_noninteger_or_out_of_bounds_round_count(scheduled_patrol, count):
    patrol, _ = scheduled_patrol
    plan(patrol, count)
    with pytest.raises(ValueError):
        patrol.read_plan()


def test_legacy_plan_without_count_is_still_valid(scheduled_patrol):
    patrol, _ = scheduled_patrol
    data = plan(patrol, 15)
    data["rooms"][0].pop("simulation_rounds_per_visit")
    patrol_module.write_json(patrol.root / "plan.json", data)
    assert patrol.read_plan() == data
