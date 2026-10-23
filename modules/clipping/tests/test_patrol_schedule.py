"""Room residence and ended-stream handling without a live DB or network."""

import asyncio

import pytest

from clipper.config import Settings
from clipper.patrol import FollowingPatrol, PatrolConfig


def make_patrol(tmp_path, monkeypatch, now, config=None):
    monkeypatch.setattr("clipper.patrol.time.time", lambda: now[0])
    patrol = FollowingPatrol(Settings(data_dir=tmp_path), tmp_path / "patrol", config)
    patrol.capacity = lambda snapshot: None
    patrol.refreshed_at = now[0]
    return patrol


def live_snapshot(status="live", duration=50):
    return {"sessions": [{"id": "mine", "mode": "live", "status": status,
                          "created_at": 1000, "duration": duration,
                          "auto_render": True, "auto_publish": False}],
            "jobs": [], "anchors": []}


def test_expired_cookie_is_visible_and_changed_login_retries_without_waiting(tmp_path, monkeypatch):
    async def scenario():
        now = [1050]
        patrol = make_patrol(tmp_path, monkeypatch, now)
        patrol.refreshed_at = 0
        calls = []
        async def request(*args, **kwargs):
            return {'sessions': [], 'jobs': [], 'anchors': []}
        async def refresh():
            calls.append(now[0])
            raise RuntimeError('upload_cookie_not_logged_in')
        patrol.request, patrol.follows = request, refresh
        await patrol.tick()
        assert patrol.state['status'] == 'waiting_for_login'
        assert patrol.state['error'] == 'upload_cookie_not_logged_in'
        assert patrol.state['following_retry_at'] == 1350
        now[0] += 10; await patrol.tick(); assert len(calls) == 1
        cookie = tmp_path / 'private/bilibili-upload.cookie'
        cookie.parent.mkdir(); cookie.write_text('new-user-supplied-login-fixture')
        now[0] += 10; await patrol.tick(); assert len(calls) == 2
        await patrol.client.aclose()
    asyncio.run(scenario())


def current_visit():
    return {"room": {"room_id": 7, "name": "test"}, "session_id": "mine",
            "started_at": 1000, "scheduled_end_at": 3075, "finish_after": 3100,
            "planned_duration_seconds": 2100, "last_duration": 50,
            "last_media_at": 1050}


@pytest.mark.parametrize("selected", [2700, 3211.25, 3720])
def test_default_residence_is_sampled_once_and_deadline_survives_restart(
        tmp_path, monkeypatch, selected):
    async def scenario():
        now = [1050]
        patrol = make_patrol(tmp_path, monkeypatch, now)
        sampled = []

        def sample(low, high):
            sampled.append((low, high))
            return selected

        monkeypatch.setattr("clipper.patrol.random.uniform", sample)

        async def request(method, path, payload=None):
            assert (method, path) == ("PATCH", "/api/sessions/mine")
            return {}

        patrol.request = request
        await patrol.adopt(live_snapshot()["sessions"][0], {"room_id": 7, "name": "test"})
        visit = patrol.state["current_visit"].copy()
        assert sampled == [(2700, 3720)]
        assert visit["planned_duration_seconds"] == selected
        assert visit["finish_after"] == 1000 + selected
        assert visit["scheduled_end_at"] == visit["finish_after"] - 25
        await patrol.client.aclose()

        # Restarting and reading the same session must not extend its residence.
        resumed = make_patrol(tmp_path, monkeypatch, now)
        snapshot = live_snapshot()

        async def state_request(method, path, payload=None):
            assert (method, path) == ("GET", "/api/state")
            return snapshot

        async def still_live(room_id):
            return 1

        resumed.request = state_request
        resumed.read_room_live_status = still_live
        await resumed.tick()
        await resumed.tick()
        assert sampled == [(2700, 3720)]
        assert resumed.state["current_visit"]["finish_after"] == visit["finish_after"]
        assert resumed.state["current_visit"]["scheduled_end_at"] == visit["scheduled_end_at"]
        await resumed.client.aclose()

    asyncio.run(scenario())


def test_explicit_fixed_residence_keeps_existing_tail_semantics(tmp_path, monkeypatch):
    async def scenario():
        patrol = make_patrol(tmp_path, monkeypatch, [1050], PatrolConfig(dwell_seconds=900))

        def no_random(*args):
            raise AssertionError("An explicit fixed residence must not be randomized")

        monkeypatch.setattr("clipper.patrol.random.uniform", no_random)

        async def request(*args, **kwargs):
            return {}

        patrol.request = request
        await patrol.adopt(live_snapshot()["sessions"][0], {"room_id": 7, "name": "test"})
        visit = patrol.state["current_visit"]
        assert visit["planned_duration_seconds"] == 925
        assert visit["scheduled_end_at"] == 1900
        assert visit["finish_after"] == 1925
        await patrol.client.aclose()

    asyncio.run(scenario())


def test_scheduled_rotation_uses_saved_total_without_adding_another_tail(tmp_path, monkeypatch):
    async def scenario():
        now = [3099]
        patrol = make_patrol(tmp_path, monkeypatch, now)
        patrol.state["current_visit"] = current_visit()
        snapshot = live_snapshot(duration=2099)
        calls = []

        async def request(method, path, payload=None):
            calls.append((method, path))
            if (method, path) == ("GET", "/api/state"):
                return snapshot
            assert (method, path) == ("POST", "/api/sessions/mine/stop")
            return {}

        async def still_live(room_id):
            return 1

        patrol.request, patrol.read_room_live_status = request, still_live
        await patrol.tick()
        assert patrol.state["current_visit"]["finish_after"] == 3100
        assert all(method != "POST" for method, _ in calls)
        now[0] = 3100
        snapshot["sessions"][0]["duration"] = 2100
        await patrol.tick()
        assert patrol.state["current_visit"] is None
        assert patrol.state["visits"][-1]["finish_reason"] == "scheduled_rotation"
        assert calls.count(("POST", "/api/sessions/mine/stop")) == 1
        await patrol.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"dwell_min_seconds": 179},
    {"dwell_min_seconds": 2701, "dwell_max_seconds": 2700},
    {"dwell_max_seconds": -1},
    {"dwell_seconds": 179},
    {"dwell_min_seconds": float("nan")},
    {"dwell_max_seconds": float("inf")},
    {"dwell_seconds": float("nan")},
    {"room_status_seconds": 0},
    {"room_status_seconds": float("inf")},
])
def test_invalid_residence_and_probe_intervals_are_rejected(changes):
    with pytest.raises(ValueError):
        PatrolConfig(**changes).validate()


@pytest.mark.parametrize("status", [0, 2])
def test_ended_room_waits_for_stop_before_advancing_and_cools_down(
        tmp_path, monkeypatch, status):
    async def scenario():
        now = [1050]
        patrol = make_patrol(tmp_path, monkeypatch, now)
        patrol.state["current_visit"] = current_visit()
        patrol.rooms = [{"room_id": 7, "name": "ended"}, {"room_id": 8, "name": "next"}]
        snapshot = live_snapshot()
        stop_entered, stop_release = asyncio.Event(), asyncio.Event()
        started, calls = [], []

        async def request(method, path, payload=None):
            calls.append((method, path))
            if (method, path) == ("GET", "/api/state"):
                return snapshot
            assert (method, path) == ("POST", "/api/sessions/mine/stop")
            stop_entered.set()
            await stop_release.wait()
            snapshot["sessions"][0].update(status="stopped", stop_phase="complete")
            return {}

        async def ended(room_id):
            assert room_id == 7
            return status

        async def refresh():
            # The following list may briefly lag behind the room's status API.
            patrol.rooms = [{"room_id": 7, "name": "stale"}, {"room_id": 8, "name": "next"}]
            patrol.refreshed_at = now[0]
            return patrol.rooms

        async def start(room, snapshot):
            started.append(room["room_id"])

        patrol.request, patrol.read_room_live_status = request, ended
        patrol.follows, patrol.start_visit = refresh, start
        task = asyncio.create_task(patrol.tick())
        try:
            await asyncio.wait_for(stop_entered.wait(), 1)
            assert not task.done()
            assert patrol.state["current_visit"]["session_id"] == "mine"
            assert not started
            stop_release.set()
            await task
            assert patrol.state["current_visit"] is None
            assert patrol.state["visits"][-1]["finish_reason"] == "stream_ended"
            assert patrol.state["retry_after"]["7"] >= now[0] + 60
            assert all(room["room_id"] != 7 for room in patrol.rooms)
            assert patrol.refreshed_at == 0
            assert not started
            await patrol.tick()
            assert started == [8]
            assert calls.count(("POST", "/api/sessions/mine/stop")) == 1
        finally:
            stop_release.set()
            await task
            await patrol.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("probe_result", [None, 1, RuntimeError("unavailable")])
def test_unknown_status_or_api_failure_does_not_end_reconnecting_capture(
        tmp_path, monkeypatch, probe_result):
    async def scenario():
        now = [1050]
        patrol = make_patrol(tmp_path, monkeypatch, now)
        patrol.state["current_visit"] = current_visit()
        calls = []

        async def request(method, path, payload=None):
            calls.append((method, path))
            assert (method, path) == ("GET", "/api/state")
            return live_snapshot(status="reconnecting")

        async def read_status(room_id):
            if isinstance(probe_result, Exception):
                raise probe_result
            return probe_result

        patrol.request, patrol.read_room_live_status = request, read_status
        await patrol.tick()
        assert patrol.state["current_visit"]["session_id"] == "mine"
        assert not patrol.state["current_visit"].get("stop_requested_at")
        assert patrol.state["visits"] == []
        assert calls == [("GET", "/api/state")]
        await patrol.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("first_result", [1, None, RuntimeError("unavailable")])
def test_room_status_probe_is_throttled_after_success_unknown_and_failure(
        tmp_path, monkeypatch, first_result):
    async def scenario():
        now = [1050]
        patrol = make_patrol(tmp_path, monkeypatch, now)
        visit = current_visit()
        calls = []

        async def read_status(room_id):
            calls.append(room_id)
            if len(calls) > 1:
                return 0
            if isinstance(first_result, Exception):
                raise first_result
            return first_result

        patrol.read_room_live_status = read_status
        assert await patrol.room_ended(visit) is False
        now[0] += 29
        assert await patrol.room_ended(visit) is False
        assert calls == [7]
        now[0] += 1
        assert await patrol.room_ended(visit) is True
        assert calls == [7, 7]
        await patrol.client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("raw, expected", [
    (0, 0), (1, 1), (2, 2), ("0", 0), ("1", 1), ("2", 2),
    (None, None), (False, None), (True, None), (1.0, None),
    (3, None), ("live", None), ([], None), ({}, None),
])
def test_room_api_status_parsing_rejects_ambiguous_values(tmp_path, monkeypatch, raw, expected):
    async def scenario():
        patrol = make_patrol(tmp_path, monkeypatch, [1050])

        async def json_response(client, path, params=None, **kwargs):
            assert path.endswith("/room/v1/Room/get_info")
            assert params == {"room_id": 7}
            return {"live_status": raw}

        monkeypatch.setattr("clipper.patrol.BilibiliClient._json", json_response)
        assert await patrol.read_room_live_status(7) == expected
        await patrol.client.aclose()

    asyncio.run(scenario())


def test_missing_live_status_is_unknown_instead_of_offline(tmp_path, monkeypatch):
    async def scenario():
        patrol = make_patrol(tmp_path, monkeypatch, [1050])

        async def json_response(*args, **kwargs):
            return {"room_id": 7}

        monkeypatch.setattr("clipper.patrol.BilibiliClient._json", json_response)
        assert await patrol.read_room_live_status(7) is None
        assert await patrol.room_ended(current_visit()) is False
        await patrol.client.aclose()

    asyncio.run(scenario())


def test_held_stream_ignores_deadline_and_releases_only_on_offline(tmp_path, monkeypatch):
    async def scenario():
        now = [1050]
        config = PatrolConfig(until_offline_room=7)
        patrol = make_patrol(tmp_path, monkeypatch, now, config)
        snapshot = live_snapshot()
        stopped = []
        status = [1]
        async def request(method, path, payload=None):
            if method == "GET": return snapshot
            if method == "POST": stopped.append(path)
            return {}
        async def live_status(room): return status[0]
        patrol.request, patrol.read_room_live_status = request, live_status
        await patrol.adopt(snapshot["sessions"][0], {"room_id":7, "name":"held"})
        assert patrol.state["current_visit"]["finish_after"] is None
        await patrol.client.aclose()
        now[0] = 10000
        # A service restart retains the unbounded first visit.
        resumed = make_patrol(tmp_path, monkeypatch, now, config)
        resumed.request, resumed.read_room_live_status = request, live_status
        snapshot["sessions"][0]["duration"] = 9000
        await resumed.tick()
        assert not stopped and resumed.state["until_offline"]["status"] == "active"
        now[0] += 31; status[0] = 0
        await resumed.tick()
        assert stopped == ["/api/sessions/mine/stop"]
        assert resumed.state["until_offline"]["status"] == "complete"
        await resumed.client.aclose()
        after = make_patrol(tmp_path, monkeypatch, now, config)
        assert after.state["until_offline"]["status"] == "complete"
        await after.client.aclose()
    asyncio.run(scenario())


def test_extra_task_does_not_block_patrol_or_allow_duplicate_room(tmp_path, monkeypatch):
    async def scenario():
        patrol = make_patrol(tmp_path, monkeypatch, [1050])
        patrol.rooms = [{"room_id":7,"name":"snow"}, {"room_id":8,"name":"other"}]
        snapshot = {"sessions":[{"id":"extra", "mode":"live", "status":"live",
                                  "room":{"room_id":7}, "model_profile":"extra-gpu", "capture_lane":"extra"}],
                    "jobs":[], "anchors":[]}
        started=[]
        async def request(*args, **kwargs): return snapshot
        async def start(room, snapshot): started.append(room["room_id"])
        patrol.request, patrol.start_visit = request, start
        await patrol.tick()
        assert started == [8]
        await patrol.client.aclose()
    asyncio.run(scenario())


def test_failed_held_capture_reconnects_same_room_instead_of_rotating(tmp_path, monkeypatch):
    async def scenario():
        now=[1050]
        patrol=make_patrol(tmp_path, monkeypatch, now, PatrolConfig(until_offline_room=7))
        patrol.rooms=[{"room_id":7,"name":"held"},{"room_id":8,"name":"other"}]
        patrol.state["current_visit"]=current_visit()
        patrol.complete_visit("capture_failed")
        assert patrol.state["until_offline"]["status"] == "active"
        snapshot={"sessions":[], "jobs":[], "anchors":[]};started=[]
        async def request(*args,**kwargs): return snapshot
        async def start(room,snapshot): started.append(room["room_id"])
        patrol.request,patrol.start_visit=request,start
        await patrol.tick();assert not started
        now[0]+=31
        await patrol.tick();assert started==[7]
        await patrol.client.aclose()
    asyncio.run(scenario())


def test_held_room_reconnect_does_not_depend_on_following_list(tmp_path, monkeypatch):
    async def scenario():
        now=[1050]
        patrol=make_patrol(tmp_path, monkeypatch, now, PatrolConfig(until_offline_room=7))
        patrol.state['until_offline']['room']={"room_id":7,"name":"held"}
        patrol.state['following_error']='read_timeout'
        started=[];checked=[]
        async def request(*args,**kwargs): return {"sessions":[], "jobs":[], "anchors":[]}
        async def status(room): checked.append(room);return 1
        async def start(room,snapshot): started.append(room['room_id'])
        patrol.request,patrol.read_room_live_status,patrol.start_visit=request,status,start
        await patrol.tick()
        assert started==checked==[7]
        await patrol.client.aclose()
    asyncio.run(scenario())


def test_completed_hold_does_not_reapply_next_room_on_restart(tmp_path, monkeypatch):
    async def scenario():
        patrol=make_patrol(tmp_path, monkeypatch, [1050],
                           PatrolConfig(until_offline_room=7,next_room=7))
        patrol.state['until_offline']['status']='complete'
        async def tick(): patrol.stopping.set()
        patrol.tick=tick
        await patrol.run()
        assert not patrol.state.get('requested_next_room')
    asyncio.run(scenario())
