import asyncio
import json
from pathlib import Path
import time

from clipper.config import Settings
from clipper.patrol import FollowingPatrol, PatrolConfig, choose_room, health


def make_patrol(tmp_path):
    return FollowingPatrol(Settings(data_dir=tmp_path), tmp_path / "patrol")


def test_room_rotation_and_failed_room_cooldown():
    rooms = [{"room_id": 1}, {"room_id": 2}, {"room_id": 3}]
    visits = [{"room": {"room_id": 1}, "started_at": 10},
              {"room": {"room_id": 2}, "started_at": 20}]
    assert choose_room(rooms, visits, {}, 30)["room_id"] == 3
    assert choose_room(rooms, visits, {"3": 40}, 30)["room_id"] == 1
    assert choose_room(rooms, visits, {"1": 40, "2": 40, "3": 40}, 30) is None


def test_lost_create_response_adopts_exact_new_session(tmp_path):
    async def scenario():
        p = make_patrol(tmp_path)
        p.state["requested_next_room"] = 123
        p.state["pending_start"] = {"room": {"room_id": 123, "name": "test"},
                                    "requested_at": 100, "before_ids": ["old"]}
        calls = []
        async def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {}
        p.request = request
        await p.reconcile_start({"sessions": [
            {"id": "old", "mode": "live", "room": "123", "created_at": 100},
            {"id": "new", "mode": "live", "room": {"room_id": 123}, "created_at": 101,
             "capture_owner": p.state["capture_owner"]}
        ]})
        assert p.state["current_visit"]["session_id"] == "new"
        assert p.state["pending_start"] is None
        assert p.state["requested_next_room"] is None
        assert len(calls) == 1
        assert calls[0][:2] == ("PATCH", "/api/sessions/new")
        payload = calls[0][2]
        assert payload["auto_render"] is True
        assert payload["auto_publish"] is False
        assert payload["minimum_score"] == 88
        assert payload["publish_profile"]["creator_name"] == "test"
        assert payload["publish_profile"]["source"] == "https://live.bilibili.com/123"
        assert payload["publish_profile"]["copyright"] == 2
        await p.client.aclose()
    asyncio.run(scenario())


def test_lost_create_never_adopts_manual_session_same_room(tmp_path, monkeypatch):
    async def scenario():
        p = make_patrol(tmp_path)
        monkeypatch.setattr("clipper.patrol.time.time", lambda: 110)
        p.state["pending_start"] = {"room": {"room_id": 123, "name": "test"},
                                    "requested_at": 100, "before_ids": []}
        async def forbidden_request(*args, **kwargs):
            raise AssertionError("manual session must never be mutated")
        p.request = forbidden_request
        await p.reconcile_start({"sessions": [
            {"id": "manual", "mode": "live", "room": "123", "created_at": 101}
        ]})
        assert p.state["current_visit"] is None
        assert p.state["pending_start"] is not None
        await p.client.aclose()
    asyncio.run(scenario())


def test_inflight_stop_is_reconciled_without_repeating_stop(tmp_path, monkeypatch):
    async def scenario():
        p = make_patrol(tmp_path)
        monkeypatch.setattr("clipper.patrol.time.time", lambda: 110)
        p.state["current_visit"] = {"session_id": "mine", "started_at": 1,
                                   "room": {"room_id": 7}, "stop_requested_at": 100}
        calls = []
        async def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {"session": {"status": "stopped", "stop_phase": "analyzing"}}
        p.request = request
        await p.finish_visit("scheduled_rotation")
        assert calls == [("GET", "/api/sessions/mine", None)]
        assert p.state["current_visit"]["session_id"] == "mine"
        await p.client.aclose()
    asyncio.run(scenario())


def test_finish_only_stops_owned_session_and_uses_servers_final_analysis(tmp_path):
    async def scenario():
        p = make_patrol(tmp_path)
        p.state["current_visit"] = {"session_id": "mine", "started_at": 1,
                                   "room": {"room_id": 7}}
        calls = []
        async def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {}
        p.request = request
        await p.finish_visit("scheduled_rotation")
        assert p.state["current_visit"] is None
        assert p.state["visits"][0]["session_id"] == "mine"
        assert calls == [("POST", "/api/sessions/mine/stop", None)]
        await p.client.aclose()
    asyncio.run(scenario())


def test_other_active_capture_is_never_stopped_or_replaced(tmp_path):
    async def scenario():
        p = make_patrol(tmp_path)
        calls = []
        async def request(method, path, payload=None):
            calls.append((method, path, payload))
            return {"sessions": [{"id": "manual", "mode": "live", "status": "live"}],
                    "jobs": [], "anchors": []}
        p.request = request
        await p.tick()
        assert p.state["status"] == "waiting_for_workbench"
        assert calls == [("GET", "/api/state", None)]
        await p.client.aclose()
    asyncio.run(scenario())


def test_capacity_counts_owned_media_and_preserves_it(tmp_path):
    async def scenario():
        p = make_patrol(tmp_path)
        p.config.max_recording_gib = 0.000000001
        p.state["visits"] = [{"session_id": "mine", "room": {"room_id": 1}}]
        media = tmp_path / "sessions/mine/segment.ts"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"existing user media")
        assert p.capacity({"jobs": []}) == "recording_budget_reached"
        assert media.read_bytes() == b"existing user media"
        await p.client.aclose()
    asyncio.run(scenario())


def test_completed_exports_do_not_consume_raw_recording_budget(tmp_path):
    async def scenario():
        p = make_patrol(tmp_path)
        p.config.max_recording_gib = 0.000000001
        p.state['visits'] = [{'session_id': 'mine', 'room': {'room_id': 1}}]
        clip = tmp_path / 'exports/job/clip.mp4'
        clip.parent.mkdir(parents=True); clip.write_bytes(b'keep this finished clip')
        assert p.capacity({'jobs': [{'id': 'job', 'session_id': 'mine'}]}) is None
        assert p.state['recording_bytes'] == 0 and clip.exists()
        await p.client.aclose()
    asyncio.run(scenario())


def test_health_is_read_only_and_does_not_open_store(tmp_path):
    folder = tmp_path / "patrol"
    folder.mkdir()
    path = folder / "status.json"
    path.write_text(json.dumps({"status": "watching", "pid": 999999999}))
    before = path.read_bytes()
    assert health(folder)["process_running"] is False
    assert path.read_bytes() == before
    assert not (tmp_path / "clipper.sqlite").exists()


def test_first_room_is_following_only_and_does_not_exclude_other_rooms(tmp_path):
    async def scenario():
        p = make_patrol(tmp_path)
        p.config.first_room = 80397
        p.rooms = [{"room_id": 47867, "name": "one"},
                   {"room_id": 80397, "name": "two"}]
        p.refreshed_at = time.time()
        snapshot = {"sessions": [], "anchors": [], "jobs": []}
        selected = []
        async def request(*args, **kwargs):
            return snapshot
        async def start(room, snapshot):
            selected.append(room["room_id"])
        p.request, p.start_visit = request, start
        await p.tick()
        p.state["visits"] = [{"session_id": "first", "room": {"room_id": 80397},
                              "started_at": 1}]
        await p.tick()
        assert selected == [80397, 47867]
        p.state["requested_next_room"] = 80397
        await p.tick()
        assert selected[-1] == 80397
        p.state["requested_next_room"] = None
        p.state["visits"] = []
        p.config.first_room = 999999  # Not currently followed/live.
        await p.tick()
        assert selected[-1] == 47867
        await p.client.aclose()
    asyncio.run(scenario())
