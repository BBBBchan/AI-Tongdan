"""Offline patrol boundary tests: no credentials, model calls or live APIs."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

import scripts.patrol_following as patrol_module


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now


class NoNetwork:
    async def request(self, *args, **kwargs):
        raise AssertionError("An offline patrol test attempted network access")

    async def aclose(self):
        pass


class LocalAPI:
    """An in-memory session service; records every attempted mutation."""
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.sequence = 0
        self.on_get = None
        self.start_error = None
        self.started_counts = []
        self.after_start_error = False
        self.state = self.snapshot("previous", 999)

    def snapshot(self, sid, room):
        return {
            "session": {"id": sid, "status": "live", "started_at": self.clock.time()},
            "room": {"room_id": room, "live_status": 1},
            "observer": {"model_ready": True, "understanding_age_seconds": 2, "observed": 1},
            "audio": {"segments": []}, "observations": [], "danmaku": [],
            "sent_danmaku": [], "danmaku_auto": {"status": "idle", "sent_count": 0},
        }

    async def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == "/api/state":
            if self.on_get:
                self.on_get(self)
            return copy.deepcopy(self.state)
        if method == "POST" and path == "/api/session":
            self.sequence += 1
            self.state = self.snapshot(f"owned-{self.sequence}", int(payload["room"]))
            self.state["session"].update(candidate_only=payload.get("candidate_only", False),
                                         candidate_count=payload.get("candidate_count", 5),
                                         allow_manual_send=payload.get("allow_manual_send", False))
            self.state["danmaku_send"] = {"can_auto_send": not payload.get("candidate_only", False)}
            return copy.deepcopy(self.state)
        if method == "POST" and path == "/api/danmaku/auto/start":
            if self.start_error:
                raise self.start_error
            count = self.started_counts.pop(0) if self.started_counts else 0
            self.state["danmaku_auto"] = {
                "status": "error" if self.after_start_error else "completed",
                "sent_count": count,
                "error": "send_unknown" if self.after_start_error else None,
            }
            return copy.deepcopy(self.state["danmaku_auto"])
        if method == "POST" and path == "/api/danmaku/auto/stop":
            # Match the real API's session fence: never affect a user's new room.
            if payload["session_id"] != self.state["session"]["id"]:
                raise RuntimeError("local_api_http_409")
            self.state["danmaku_auto"]["status"] = "stopped"
            return copy.deepcopy(self.state["danmaku_auto"])
        raise AssertionError(f"Unexpected mocked request: {method} {path}")

    def mutations(self, path):
        return [payload for method, route, payload in self.calls if method == "POST" and route == path]


@pytest.fixture
def offline_patrol(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(patrol_module.time, "time", clock.time)
    settings = SimpleNamespace(data_dir=tmp_path / "data", ffmpeg_bin="unused", bilibili_cookie="")
    settings.data_dir.mkdir()
    monkeypatch.setattr(patrol_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(patrol_module.httpx, "AsyncClient", lambda *a, **k: NoNetwork())
    args = SimpleNamespace(
        run_dir=tmp_path / "run", base_url="http://127.0.0.1:1", hours=12,
        dwell=180, interval=180, max_messages=5, warmup=0,
        max_recording_gib=30, send=True,
        first_room=None, first_dwell=1800, first_max_messages=10,
    )
    patrol = patrol_module.Patrol(args)
    api = LocalAPI(clock)
    patrol.request = api.request
    patrol.log = lambda *args: None
    patrol.archive_media = lambda snap: None

    async def delay(seconds):
        clock.now += seconds
        await asyncio.sleep(0)

    async def recording(*args):
        patrol.recording = SimpleNamespace(returncode=None)

    async def stop_recording():
        patrol.recording = None

    async def noop(*args):
        pass

    patrol.delay = delay
    patrol.ensure_recording = recording
    patrol.stop_recording = stop_recording
    patrol.stream_events = noop
    patrol.report = noop
    return patrol, api, clock


def room(room_id=101):
    return {"room_id": room_id, "title": "离线测试房", "area": "虚拟日常"}


def test_room_quota_is_cumulative_across_revisits():
    visits = [
        {"room": {"room_id": 101}, "sent_count": 2},
        {"room": {"room_id": 102}, "sent_count": 5},
        {"room": {"room_id": "101"}, "sent_count": 1},
    ]
    assert patrol_module.room_quota(101, visits, 5) == 2
    assert patrol_module.room_quota(102, visits, 5) == 0
    assert patrol_module.room_quota(103, visits, 5) == 5
    assert patrol_module.room_quota(101, visits, 2) == 0


def test_choosing_rooms_prefers_unvisited_then_least_recent(monkeypatch):
    monkeypatch.setattr(patrol_module.random, "shuffle", lambda values: None)
    rooms = [room(1), room(2), room(3)]
    visits = [{"room": room(1), "started_at": 10}, {"room": room(2), "started_at": 20}]
    assert patrol_module.choose_room(rooms, visits, current_room=2)["room_id"] == 3
    visits.append({"room": room(3), "started_at": 30})
    assert patrol_module.choose_room(rooms, visits, current_room=3)["room_id"] == 1
    assert patrol_module.choose_room([room(1), room(2)], [], current_room=1)["room_id"] == 2
    assert patrol_module.choose_room([], visits) is None


def test_recursive_clean_removes_identity_and_credentials():
    raw = {
        "text": "这波操作有点东西", "room_id": 123,
        "UID": 41, "Uname": "private-name", "userId": 42, "ownerUid": 43,
        "Authorization": "Bearer private-secret", "access_token": "private-secret",
        "nested": [{"user_name": "private-name", "embedding": [1, 2], "timestamp": 12}],
        "message": "SESSDATA=private-secret",
    }
    result = patrol_module.clean(raw)
    assert result["text"] == raw["text"]
    assert result["room_id"] == 123
    assert result["nested"] == [{"timestamp": 12}]
    encoded = json.dumps(result)
    assert "private-name" not in encoded
    assert "private-secret" not in encoded
    assert not any(key in result for key in ("UID", "Uname", "userId", "ownerUid", "Authorization", "access_token"))


@pytest.mark.asyncio
async def test_revisit_uses_only_remaining_quota_and_never_resets_cap(offline_patrol):
    patrol, api, _ = offline_patrol
    api.started_counts = [2, 3]
    await patrol.visit_room(room())
    await patrol.visit_room(room())
    await patrol.visit_room(room())
    starts = api.mutations("/api/danmaku/auto/start")
    assert [item["max_messages"] for item in starts] == [5, 3]
    assert sum(visit["sent_count"] for visit in patrol.visits) == 5


@pytest.mark.asyncio
async def test_send_failure_disables_sending_for_later_rooms(offline_patrol):
    patrol, api, _ = offline_patrol
    api.after_start_error = True
    await patrol.visit_room(room(101))
    assert patrol.state["sending_disabled_reason"]
    await patrol.visit_room(room(102))
    assert len(api.mutations("/api/session")) == 2  # Read-only observation still rotates.
    assert len(api.mutations("/api/danmaku/auto/start")) == 1


@pytest.mark.asyncio
async def test_uncertain_auto_start_is_stopped_and_never_retried(offline_patrol):
    patrol, api, _ = offline_patrol
    api.start_error = httpx.ReadTimeout("response was lost")
    await patrol.visit_room(room(101))
    await patrol.visit_room(room(102))
    assert len(api.mutations("/api/danmaku/auto/start")) == 1
    assert api.mutations("/api/danmaku/auto/stop")
    assert patrol.state["sending_disabled_reason"]


@pytest.mark.asyncio
async def test_expired_run_does_not_start_another_session(offline_patrol):
    patrol, api, clock = offline_patrol
    patrol.state["deadline"] = clock.time() - 1
    await patrol.visit_room(room())
    assert not api.mutations("/api/session")
    assert not api.mutations("/api/danmaku/auto/start")


@pytest.mark.asyncio
async def test_deadline_expiring_during_state_get_prevents_session_post(offline_patrol):
    patrol, api, clock = offline_patrol
    patrol.state["deadline"] = clock.time() + 1
    api.on_get = lambda service: setattr(clock, "now", clock.time() + 2)
    await patrol.visit_room(room())
    assert not api.mutations("/api/session")
    assert not api.mutations("/api/danmaku/auto/start")


@pytest.mark.asyncio
async def test_user_room_change_between_visits_prevents_further_posts(offline_patrol):
    patrol, api, _ = offline_patrol
    patrol.state["last_owned_session"] = "old-owned"
    api.state["session"]["id"] = "user-selected-session"
    await patrol.visit_room(room())
    assert patrol.stop.is_set()
    assert patrol.state["status"] == "user_took_over"
    assert not [call for call in api.calls if call[0] == "POST"]


@pytest.mark.asyncio
async def test_user_room_change_during_visit_never_starts_auto_in_new_room(offline_patrol):
    patrol, api, _ = offline_patrol

    def take_over(service):
        if service.sequence:
            service.state = service.snapshot("user-selected-session", 777)

    api.on_get = take_over
    await patrol.visit_room(room())
    assert patrol.stop.is_set()
    assert patrol.state["status"] == "user_took_over"
    assert not api.mutations("/api/danmaku/auto/start")
    assert all(p["session_id"] == "owned-1" for p in api.mutations("/api/danmaku/auto/stop"))
    assert api.state["session"]["id"] == "user-selected-session"


@pytest.mark.asyncio
async def test_user_stopping_owned_session_stops_patrol(offline_patrol):
    patrol, api, _ = offline_patrol

    def stop_session(service):
        if service.sequence:
            service.state["session"]["status"] = "stopped"

    api.on_get = stop_session
    await patrol.visit_room(room())
    assert patrol.stop.is_set()
    assert patrol.state["status"] == "user_took_over"
    assert not api.mutations("/api/danmaku/auto/start")


@pytest.mark.asyncio
async def test_watch_only_never_starts_auto_sender(offline_patrol):
    patrol, api, _ = offline_patrol
    patrol.args.send = False
    await patrol.visit_room(room())
    assert api.mutations("/api/session")
    assert not api.mutations("/api/danmaku/auto/start")
    assert api.mutations("/api/session")[0]["candidate_only"] is True


@pytest.mark.asyncio
async def test_manual_permission_follows_each_room_without_auto_start(offline_patrol):
    patrol, api, clock = offline_patrol
    patrol.args.send = False
    patrol.args.allow_manual_send = True
    started = clock.time()
    await patrol.visit_room(room(101))
    await patrol.visit_room(room(102))
    sessions = api.mutations("/api/session")
    assert len(sessions) == 2
    assert all(item["candidate_only"] and item["allow_manual_send"] for item in sessions)
    assert not api.mutations("/api/danmaku/auto/start")
    assert all(visit["allow_manual_send"] and not visit["auto_started"] for visit in patrol.visits)
    assert all(started <= visit["allow_manual_send_since"] <= clock.time() for visit in patrol.visits)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["allow_manual_send", "can_auto_send"])
async def test_manual_patrol_fails_closed_if_server_ignores_permission(offline_patrol, missing):
    patrol, api, _ = offline_patrol
    patrol.args.send = False
    patrol.args.allow_manual_send = True
    original = patrol.request

    async def old_server(method, path, payload=None):
        result = await original(method, path, payload)
        if method == "POST" and path == "/api/session":
            result["session" if missing == "allow_manual_send" else "danmaku_send"].pop(missing)
        return result

    patrol.request = old_server
    await patrol.visit_room(room())
    assert patrol.stop.is_set()
    assert patrol.state["status"] == "candidate_guard_unavailable"
    assert not api.mutations("/api/danmaku/auto/start")


@pytest.mark.asyncio
async def test_candidate_only_requests_ten_and_fails_closed_on_old_server(offline_patrol):
    patrol, api, _ = offline_patrol
    patrol.args.send = False
    patrol.args.candidate_count = 10
    original = patrol.request

    async def old_server(method, path, payload=None):
        result = await original(method, path, payload)
        if path == "/api/session":
            result["session"].pop("candidate_only", None)
        return result

    patrol.request = old_server
    await patrol.visit_room(room())
    assert api.mutations("/api/session")[0]["candidate_count"] == 10
    assert patrol.stop.is_set()
    assert patrol.state["status"] == "candidate_guard_unavailable"
    assert not api.mutations("/api/danmaku/auto/start")


@pytest.mark.asyncio
async def test_requested_first_room_does_not_depend_on_following_api(offline_patrol):
    patrol, _, _ = offline_patrol
    patrol.args.first_room = 213

    async def unavailable():
        raise AssertionError("first room must not depend on follows")

    patrol.following = unavailable
    first = await patrol.next_room()
    assert first["room_id"] == 213
    assert first["selection_source"] == "requested_first"


@pytest.mark.asyncio
async def test_following_always_wins_over_virtual_area(offline_patrol):
    patrol, _, _ = offline_patrol
    patrol.args.virtual_fallback = True
    patrol.visits = [{"room": room(213)}]

    async def follows():
        return [room(101)]

    async def virtual():
        raise AssertionError("Do not browse virtual when follows are live")

    patrol.following, patrol.virtual_rooms = follows, virtual
    selected = await patrol.next_room()
    assert selected["room_id"] == 101
    assert selected["selection_source"] == "following"


@pytest.mark.asyncio
async def test_virtual_fallback_only_after_confirmed_empty_following(offline_patrol):
    patrol, _, _ = offline_patrol
    patrol.args.virtual_fallback = True
    patrol.visits = [{"room": room(213)}]

    async def empty():
        return []

    async def virtual():
        return [room(202)]

    patrol.following, patrol.virtual_rooms = empty, virtual
    selected = await patrol.next_room()
    assert selected["room_id"] == 202
    assert selected["selection_source"] == "virtual_area"

    async def unavailable():
        raise RuntimeError("following unavailable")

    patrol.following = unavailable
    with pytest.raises(RuntimeError):
        await patrol.next_room()


@pytest.mark.asyncio
async def test_virtual_visit_returns_early_when_followed_streamer_goes_live(offline_patrol):
    patrol, api, _ = offline_patrol
    patrol.args.send = False

    async def follows():
        return [room(303)]

    patrol.following = follows
    await patrol.visit_room({**room(202), "selection_source": "virtual_area"})
    visit = patrol.visits[0]
    assert visit["ended_at"] - visit["started_at"] < patrol.args.dwell
    assert "关注列表已有开播" in visit["exit_reason"]
    assert not api.mutations("/api/danmaku/auto/start")


def test_candidate_batches_saved_once_with_all_ten_items(offline_patrol):
    patrol, _, _ = offline_patrol
    patrol.visit = {"sent_count": 0}
    patrol.visit_dir = patrol.root / "candidates-test"
    patrol.visit_dir.mkdir()
    patrol.owned_sid = "owned-offline"
    payload = {"items": [{"text": f"候选{i}"} for i in range(10)], "status": "ready", "generated_at": 123}
    patrol.emit("danmaku_candidates", payload)
    patrol.emit("danmaku_candidates", payload)
    patrol.emit("danmaku_candidates", {**payload, "status": "generating"})
    patrol.emit("danmaku_candidates", {**payload, "error": None})
    rows = (patrol.visit_dir / "candidates.jsonl").read_text().splitlines()
    assert len(rows) == 1
    assert len(json.loads(rows[0])["data"]["items"]) == 10
    assert patrol.visit["candidate_total"] == 10
    assert patrol.visit["candidate_batches"] == 1


@pytest.mark.asyncio
async def test_following_scans_past_a_full_page_without_live_rooms(offline_patrol, monkeypatch):
    patrol, _, _ = offline_patrol
    pages = []

    class FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeBili:
        def __init__(self, **kwargs):
            pass

        async def _json(self, path, params):
            pages.append(params["page"])
            if params["page"] == 1:
                return {"list": [{"room_id": i, "live_status": 0} for i in range(1, 11)]}
            return {"list": [{"room_id": 101, "live_status": 1, "title": "开播"}]}

    monkeypatch.setattr(patrol_module.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    monkeypatch.setattr(patrol_module, "BilibiliClient", FakeBili)
    monkeypatch.setattr(patrol_module, "DanmakuCredentials",
                        lambda *args: SimpleNamespace(_read=lambda: ("", None)))
    assert [r["room_id"] for r in await patrol.following()] == [101]
    assert pages == [1, 2]


@pytest.mark.asyncio
async def test_virtual_discovery_filters_other_areas_and_deduplicates(offline_patrol, monkeypatch):
    patrol, _, _ = offline_patrol

    class FakeHTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeBili:
        def __init__(self, **kwargs):
            assert not kwargs.get("cookie")

        async def _json(self, path, params):
            assert params["parent_area_id"] == 9
            return {"list": [{"roomid": 202, "area_v2_parent_id": 9},
                             {"roomid": 202, "area_v2_parent_id": "9"},
                             {"roomid": 303, "area_v2_parent_id": 2}]}

    monkeypatch.setattr(patrol_module.httpx, "AsyncClient", lambda **kwargs: FakeHTTP())
    monkeypatch.setattr(patrol_module, "BilibiliClient", FakeBili)
    assert [r["room_id"] for r in await patrol.virtual_rooms()] == [202]


def test_stop_marker_and_expiry_make_run_inactive(offline_patrol):
    patrol, _, clock = offline_patrol
    assert patrol.active()
    (patrol.root / "STOP").touch()
    assert not patrol.active()
    (patrol.root / "STOP").unlink()
    patrol.state["deadline"] = clock.time()
    assert not patrol.active()


def test_event_export_redacts_identity_and_deduplicates(offline_patrol):
    patrol, _, _ = offline_patrol
    patrol.visit = {"sent_count": 0}
    patrol.visit_dir = patrol.root / "visit-test"
    patrol.visit_dir.mkdir()
    patrol.owned_sid = "owned-offline"
    payload = {"text": "不错哦", "uid": 1234, "user_name": "private-name", "timestamp": 3.0}
    patrol.emit("danmaku", payload)
    patrol.emit("danmaku", payload)
    rows = [json.loads(line) for line in (patrol.visit_dir / "events.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["data"] == {"text": "不错哦", "timestamp": 3.0}
    assert "private-name" not in json.dumps(rows)


@pytest.mark.asyncio
async def test_first_room_gets_ten_total_and_thirty_minutes_only_first_visit(offline_patrol):
    patrol, api, _ = offline_patrol
    patrol.args.first_room = 851181
    patrol.args.first_dwell = 1800
    patrol.args.first_max_messages = 10
    api.started_counts = [6, 5, 4]
    await patrol.visit_room(room(851181))
    await patrol.visit_room(room(102))
    await patrol.visit_room(room(851181))
    starts = api.mutations("/api/danmaku/auto/start")
    assert [item["max_messages"] for item in starts] == [10, 5, 4]
    assert [visit["scheduled_end_at"] - visit["started_at"] for visit in patrol.visits] == [1800, 180, 180]
    assert patrol_module.room_quota(851181, patrol.visits, 10) == 0


@pytest.mark.asyncio
async def test_user_manual_auto_stop_ends_patrol(offline_patrol):
    patrol, api, _ = offline_patrol

    def manual_stop(service):
        if service.mutations("/api/danmaku/auto/start"):
            service.state["danmaku_auto"].update(status="stopped", reason="已手动停止")

    api.on_get = manual_stop
    await patrol.visit_room(room())
    assert patrol.stop.is_set()
    assert patrol.state["status"] == "user_took_over"
    assert len(api.mutations("/api/danmaku/auto/start")) == 1


@pytest.mark.asyncio
async def test_expiry_guard_stops_auto_while_main_poll_is_awaiting(offline_patrol, monkeypatch):
    patrol, api, clock = offline_patrol
    patrol.owned_sid = "owned-guard-test"
    api.state["session"]["id"] = patrol.owned_sid
    patrol.state["deadline"] = clock.time() + 0.01
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)
    stops_before_following_finished = []

    async def blocked_following():
        # The deadline task must be able to stop the sender while the main
        # coroutine has not returned from a read-only operation.
        await patrol.stop.wait()
        await asyncio.sleep(0)
        stops_before_following_finished.extend(api.mutations("/api/danmaku/auto/stop"))
        return []

    patrol.following = blocked_following
    await asyncio.wait_for(patrol.run(), timeout=1)
    assert stops_before_following_finished
    assert patrol.stop.is_set()
    assert not api.mutations("/api/session")
    assert not api.mutations("/api/danmaku/auto/start")
    patrol.lock.close()
