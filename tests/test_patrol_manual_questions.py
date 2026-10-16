"""Manual/planned QA integration with an offline API stub and synthetic state."""
import copy
import json
from types import SimpleNamespace

import pytest

import scripts.patrol_following as patrol_module


class NoNetwork:
    async def request(self, *args, **kwargs):
        raise AssertionError("The offline QA test attempted an HTTP request")

    async def aclose(self):
        pass


@pytest.fixture
def qa_patrol(tmp_path, monkeypatch):
    clock = SimpleNamespace(start=1_800_000_000.0, elapsed=165.0)
    monkeypatch.setattr(patrol_module.time, "time", lambda: clock.start + clock.elapsed)
    settings = SimpleNamespace(data_dir=tmp_path / "data", ffmpeg_bin="unused", bilibili_cookie="")
    monkeypatch.setattr(patrol_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(patrol_module.httpx, "AsyncClient", lambda *args, **kwargs: NoNetwork())
    args = SimpleNamespace(run_dir=tmp_path / "run", base_url="http://127.0.0.1:1", hours=24,
        dwell=1800, interval=180, max_messages=5, warmup=0, max_recording_gib=30,
        send=False, first_room=213, first_dwell=1800, first_max_messages=10)
    patrol = patrol_module.Patrol(args)
    patrol.owned_sid = "owned-test-session"
    patrol.visit_dir = tmp_path / "run/visits/001-213"
    patrol.visit_dir.mkdir(parents=True)
    patrol.visit = {"visit_id": "001-213", "room": {"room_id": 213},
        "requested_room_id": 213, "started_at": clock.start,
        "session_started_at": clock.start, "scheduled_end_at": clock.start + 1800,
        "question_history": [], "sent_count": 0}
    patrol.question_history = patrol.visit["question_history"]
    patrol.state["config"].update(questions_enabled=True, question_interval_seconds=165)
    patrol.log = lambda *args: None
    calls = []

    async def request(method, path, payload=None):
        assert method == "POST" and path == "/api/questions"
        assert payload["session_id"] == patrol.owned_sid
        calls.append(copy.deepcopy(payload))
        return {"question_id": f"planned-{len(calls)}"}

    patrol.request = request

    def snapshot(manual=0, planned=(), active=None, room_id=213, age=2):
        messages = []
        for index in range(manual):
            qid = f"manual-{index}"
            messages.extend([
                {"role": "user", "question_id": qid, "status": "complete", "text": f"用户额外问的问题{index}"},
                {"role": "assistant", "question_id": qid, "status": "answering", "text": ""},
            ])
        for qid in planned:
            messages.extend([
                {"role": "user", "question_id": qid, "status": "complete", "text": "已提交的计划问题"},
                {"role": "assistant", "question_id": qid, "status": "queued", "text": ""},
            ])
        return {"session": {"id": patrol.owned_sid, "status": "live"},
            "room": {"room_id": room_id, "short_id": 0, "live_status": 1},
            "observer": {"model_ready": True, "understanding_age_seconds": age,
                "last_timestamp": clock.elapsed,
                "active_questions": manual + len(planned) if active is None else active},
            "observations": [{"kind": "observation", "window_end": clock.elapsed,
                "description": f"游戏角色正在完成第{int(clock.elapsed)}处的任务"}],
            "audio": {"segments": []}, "messages": messages,
            "danmaku_chorus": {"session_id": patrol.owned_sid, "items": []},
            "danmaku": [], "sent_danmaku": []}

    return SimpleNamespace(patrol=patrol, clock=clock, calls=calls, snapshot=snapshot)


def question_records(patrol):
    path = patrol.visit_dir / "questions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_manual_qa_is_archived_without_consuming_or_resetting_planned_cadence(qa_patrol):
    env = qa_patrol
    await env.patrol.maybe_question(env.snapshot())
    first_history = copy.deepcopy(env.patrol.question_history)
    retry_at = env.patrol.visit["question_retry_at"]
    env.clock.elapsed = 329
    snap = env.snapshot()
    snap["messages"] = [
        {"role": "user", "question_id": "manual-extra", "status": "complete", "text": "刚才门后有什么？"},
        {"role": "assistant", "question_id": "manual-extra", "status": "complete", "text": "记录里门后是走廊。"},
    ]
    env.patrol.snapshot_events(snap)
    assert env.patrol.question_history == first_history
    assert env.patrol.visit["question_retry_at"] == retry_at
    records = question_records(env.patrol)
    manual = [row for row in records if row["data"].get("question_id") == "manual-extra"]
    assert {row["type"] for row in manual} == {"question_started", "answer_done"}
    assert all(row["type"] != "question_requested" for row in manual)
    requested = [row for row in records if row["type"] == "question_requested"]
    assert requested[0]["data"]["origin"] == "planned"
    await env.patrol.maybe_question(snap)
    assert len(env.calls) == 1
    env.clock.elapsed = 330
    await env.patrol.maybe_question(env.snapshot())
    assert len(env.calls) == 2
    assert [row["elapsed_seconds"] for row in env.patrol.question_history] == [165, 330]


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_count", [1, 2])
async def test_manual_busy_allows_one_planned_question_in_agent_queue_without_mutating_snapshot(qa_patrol, manual_count):
    env = qa_patrol
    snap = env.snapshot(manual=manual_count)
    before = copy.deepcopy(snap)
    await env.patrol.maybe_question(snap)
    assert len(env.calls) == 1 and len(env.patrol.question_history) == 1
    assert env.patrol.question_history[0]["question_id"] == "planned-1"
    assert snap == before
    assert all(row["question_id"] != "manual-0" for row in env.patrol.question_history)
    # A later due slot must not pile another plan behind the one already queued.
    env.clock.elapsed = 330
    await env.patrol.maybe_question(env.snapshot(manual=manual_count, planned=["planned-1"]))
    assert len(env.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("manual,active", [(0, 1), (1, 2), (2, 1), (1, 0), (3, 3), (4, 4)])
async def test_unknown_activity_or_full_manual_queue_is_not_treated_as_safe_to_enqueue(qa_patrol, manual, active):
    env = qa_patrol
    await env.patrol.maybe_question(env.snapshot(manual=manual, active=active))
    assert not env.calls and not env.patrol.question_history


@pytest.mark.asyncio
async def test_manual_queue_exception_does_not_bypass_observation_freshness(qa_patrol):
    env = qa_patrol
    await env.patrol.maybe_question(env.snapshot(manual=1, age=20.1))
    assert not env.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["user", "qid"])
async def test_pending_assistant_without_matching_user_is_not_assumed_manual(qa_patrol, missing):
    env = qa_patrol
    snap = env.snapshot(manual=1)
    if missing == "user":
        snap["messages"] = [row for row in snap["messages"] if row["role"] != "user"]
    else:
        snap["messages"][1].pop("question_id")
    await env.patrol.maybe_question(snap)
    assert not env.calls


@pytest.mark.asyncio
async def test_uncertain_planned_submission_matches_user_text_even_when_assistant_text_is_empty(qa_patrol):
    env = qa_patrol
    text = "尚未收到POST结果的计划问题"
    env.patrol.question_history.append({"text": text, "type": "game", "status": "submission_uncertain",
        "elapsed_seconds": 165, "room_id": 213, "session_id": env.patrol.owned_sid,
        "fingerprint": "uncertain-fixture"})
    env.clock.elapsed = 330
    snap = env.snapshot(manual=1)
    snap["messages"][0]["text"] = text
    snap["messages"][1].update(status="queued", text="")
    await env.patrol.maybe_question(snap)
    assert not env.calls and len(env.patrol.question_history) == 1


@pytest.mark.asyncio
async def test_manual_question_before_twenty_minutes_does_not_shift_510_song_slot(qa_patrol):
    env = qa_patrol
    env.patrol.visit.update(room={"room_id": 510}, requested_room_id=510)
    env.patrol.question_history.append({"question_id": "earlier-plan", "text": "先前的计划问题",
        "type": "game", "elapsed_seconds": 1035, "room_id": 510,
        "session_id": env.patrol.owned_sid, "fingerprint": "earlier-fixture"})
    env.patrol.visit["question_retry_at"] = env.clock.start + 1200
    env.clock.elapsed = 1190
    snap = env.snapshot(manual=1, room_id=510)
    env.patrol.snapshot_events(snap)
    assert len(env.patrol.question_history) == 1
    assert env.patrol.visit["question_retry_at"] == env.clock.start + 1200
    env.clock.elapsed = 1199
    await env.patrol.maybe_question(env.snapshot(manual=1, room_id=510))
    assert not env.calls
    env.clock.elapsed = 1200
    await env.patrol.maybe_question(env.snapshot(manual=1, room_id=510))
    assert len(env.calls) == 1
    assert "刚才唱的是什么歌" in env.calls[0]["text"]
    song = env.patrol.question_history[-1]
    assert song["type"] == "scheduled_song"
    assert song["elapsed_seconds"] == song["scheduled_for"] == 1200
