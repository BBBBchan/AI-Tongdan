import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from clipper.bili_collections import (
    BiliCollectionClient, StreamerCollectionManager, published_collection_groups,
)


def publications(count=3, uid=42, start=100):
    return [{"id": f"p-{aid}", "session_id": "s", "status": "published", "aid": aid,
             "account_uid": uid, "uploaded_video": {"cid": aid + 1000}, "title": f"片段{aid}",
             "uploaded_cover": "https://archive.biliimg.com/bfs/archive/cover.jpg",
             "series": {"status": "added", "series_id": 999}} for aid in range(start, start + count)]


SESSIONS = [{"id": "s", "mode": "live", "streamer_uid": 123, "streamer": "主播甲",
             "room": {"owner_uid": 123}}]


class FakeCollections:
    def __init__(self):
        self.allowed = False
        self.remaining = 10
        self.uid = 42
        self.seasons = {}
        self.sections = {}
        self.calls = []
        self.cap_calls = 0
        self.create_error = None
        self.append_error = None
        self.commit_error = False
        self.section_default = False
        self.rejected_aids = set()

    def __call__(self, _cookie):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def account(self):
        return {"logged_in": True, "uid": self.uid}

    async def collection_capability(self):
        self.cap_calls += 1
        return {"allowed": self.allowed, "status": "available" if self.allowed else "blocked_permission",
                "season": self.allowed, "new_season": self.allowed, "remaining": self.remaining if self.allowed else 0,
                "can_create": self.allowed and self.remaining > 0, "can_manage_existing": self.allowed,
                "level": 2 if self.allowed else 1}

    async def ugc_seasons(self, page=1, size=30):
        rows = [{"season": v} for v in self.seasons.values()]
        return {"total": len(rows), "seasons": rows[(page - 1) * size:page * size]}

    async def ugc_season(self, sid):
        sections = [v["section"] for v in self.sections.values() if v["section"]["seasonId"] == sid]
        return {"season": self.seasons[sid], "sections": {"sections": sections, "total": len(sections)}}

    async def ugc_section(self, sid):
        return self.sections[sid]

    async def create_collection(self, title, description, cover):
        self.calls.append(("create", title))
        if self.create_error and not self.commit_error:
            raise self.create_error
        sid = 500 + len(self.seasons)
        self.seasons[sid] = {"id": sid, "mid": self.uid, "title": title, "desc": description}
        if self.section_default:
            self.sections[800] = {"section": {"id": 800, "seasonId": sid, "title": "默认"}, "episodes": []}
        if self.create_error:
            raise self.create_error
        return sid

    async def create_section(self, sid):
        self.calls.append(("section", sid))
        section = 800 + len(self.sections)
        self.sections[section] = {"section": {"id": section, "seasonId": sid, "title": "直播切片"}, "episodes": []}
        return section

    async def verify_episode(self, aid):
        if aid in self.rejected_aids:
            from clipper.bili_publish import PublishError
            raise PublishError("already belongs to another collection", code=20001)

    async def append_episodes(self, sid, episodes):
        self.calls.append(("append", [e["aid"] for e in episodes]))
        if self.append_error and not self.commit_error:
            raise self.append_error
        self.sections[sid]["episodes"].extend(deepcopy(episodes))
        if self.append_error:
            raise self.append_error


@pytest.fixture
def setup(tmp_path):
    fake = FakeCollections()
    clock = [1000]
    settings = SimpleNamespace(data_dir=tmp_path, upload_cookie=lambda: "test-cookie")
    return StreamerCollectionManager(settings, fake, clock=lambda: clock[0]), fake, settings, clock


async def test_blocked_permission_persists_all_history_without_writes_and_throttles(setup):
    manager, fake, settings, clock = setup
    snapshot = await manager.reconcile(publications(), SESSIONS)
    group = snapshot["groups"]["42:123"]
    assert group["status"] == "blocked_permission"
    assert group["desired_aids"] == [100, 101, 102]
    assert group["permission"]["level"] == 1 and group["permission"]["season"] is False
    assert group["season_id"] is None and not fake.calls
    await manager.reconcile(publications(4), SESSIONS)
    assert fake.cap_calls == 1
    assert manager.snapshot()["groups"]["42:123"]["desired_aids"] == [100, 101, 102, 103]
    clock[0] += 601
    reopened = StreamerCollectionManager(settings, fake, clock=lambda: clock[0])
    await reopened.reconcile(publications(4), SESSIONS)
    assert fake.cap_calls == 2 and not fake.calls


async def test_unlock_integrates_all_history_then_only_appends_new_archive(setup):
    manager, fake, _, clock = setup
    await manager.reconcile(publications(), SESSIONS)
    fake.allowed = True
    clock[0] += 601
    snapshot = await manager.reconcile(publications(), SESSIONS)
    group = snapshot["groups"]["42:123"]
    assert group["status"] == "integrated" and group["season_id"] == 500
    assert fake.calls == [("create", "主播甲直播切片"), ("section", 500), ("append", [100, 101, 102])]
    await manager.reconcile(publications(), SESSIONS)
    await manager.reconcile(publications(4), SESSIONS)
    assert fake.calls[-1] == ("append", [103])
    assert len(fake.calls) == 4


async def test_zero_creation_quota_still_appends_existing_owned_collection(setup):
    manager, fake, _, clock = setup
    fake.allowed = True
    await manager.reconcile(publications(), SESSIONS)
    fake.remaining = 0
    clock[0] += 601
    result = await manager.reconcile(publications(4), SESSIONS)
    assert result["groups"]["42:123"]["status"] == "integrated"
    assert fake.calls[-1] == ("append", [103])
    assert [c[0] for c in fake.calls].count("create") == 1


async def test_zero_creation_quota_discovers_existing_but_never_creates_new(setup):
    manager, fake, _, clock = setup
    fake.allowed = True
    fake.remaining = 0
    result = await manager.reconcile(publications(), SESSIONS)
    assert result["groups"]["42:123"]["status"] == "blocked_creation_quota"
    assert not fake.calls
    fake.seasons[500] = {"id": 500, "mid": 42, "title": "甲的合集", "desc": "主播主页：https://space.bilibili.com/123"}
    fake.sections[800] = {"section": {"id": 800, "seasonId": 500, "title": "默认"}, "episodes": []}
    clock[0] += 601
    result = await manager.reconcile(publications(), SESSIONS)
    assert result["groups"]["42:123"]["status"] == "integrated"
    assert fake.calls == [("append", [100, 101, 102])]


async def test_threshold_counts_unique_public_aids_and_excludes_imported_intro(setup):
    manager, fake, _, _ = setup
    fake.allowed = True
    pubs = publications(2)
    snapshot = await manager.reconcile(pubs + [pubs[0], {**publications()[2], "status": "submitted"}], SESSIONS)
    assert snapshot["groups"]["42:123"]["status"] == "waiting_for_three_publications"
    assert not fake.calls and fake.cap_calls == 0
    groups, _ = published_collection_groups(publications(), [{**SESSIONS[0], "mode": "file"}])
    assert groups == {}


async def test_lost_create_response_without_commit_never_reposts_after_restart(setup):
    manager, fake, settings, clock = setup
    fake.allowed = True
    fake.create_error = TimeoutError("SESSDATA=secret-never-print")
    snapshot = await manager.reconcile(publications(), SESSIONS)
    assert snapshot["groups"]["42:123"]["status"] == "uncertain"
    assert "secret-never-print" not in json.dumps(snapshot)
    for _ in range(3):
        clock[0] += 601
        manager = StreamerCollectionManager(settings, fake, clock=lambda: clock[0])
        await manager.reconcile(publications(4), SESSIONS)
    assert len(fake.calls) == 1


async def test_committed_lost_create_recovers_by_get_then_adds_history(setup):
    manager, fake, _, _ = setup
    fake.allowed = True
    fake.create_error = TimeoutError()
    fake.commit_error = True
    result = await manager.reconcile(publications(), SESSIONS)
    assert result["groups"]["42:123"]["status"] == "integrated"
    assert [c[0] for c in fake.calls].count("create") == 1


async def test_cancelled_write_intent_survives_restart_and_never_reposts(setup):
    manager, fake, settings, clock = setup
    fake.allowed = True
    fake.create_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await manager.reconcile(publications(), SESSIONS)
    assert manager.snapshot()["groups"]["42:123"]["operations"]["create"]["phase"] == "intent"
    fake.create_error = None
    clock[0] += 601
    reopened = StreamerCollectionManager(settings, fake, clock=lambda: clock[0])
    result = await reopened.reconcile(publications(), SESSIONS)
    assert result["groups"]["42:123"]["status"] == "uncertain"
    assert len(fake.calls) == 1


def test_conflicting_live_owner_cannot_be_assigned_by_display_name():
    groups, errors = published_collection_groups(publications(), [{**SESSIONS[0], "room": {"owner_uid": 456}}])
    assert not groups and len(errors) == 3


async def test_lost_append_response_never_retries_unconfirmed_batch(setup):
    manager, fake, settings, clock = setup
    fake.allowed = True
    fake.append_error = TimeoutError()
    await manager.reconcile(publications(), SESSIONS)
    clock[0] += 601
    reopened = StreamerCollectionManager(settings, fake, clock=lambda: clock[0])
    await reopened.reconcile(publications(4), SESSIONS)
    assert [c[0] for c in fake.calls].count("append") == 1
    # The platform later exposes the original accepted batch. A GET discovers
    # it; only the unrelated fourth archive is then appended.
    fake.sections[800]["episodes"] = [{"aid": p["aid"], "cid": p["uploaded_video"]["cid"]} for p in publications()]
    fake.append_error = None
    clock[0] += 601
    result = await reopened.reconcile(publications(4), SESSIONS)
    assert result["groups"]["42:123"]["status"] == "integrated"
    assert fake.calls[-1] == ("append", [103])


async def test_account_mismatch_never_writes_and_ordinary_series_never_counts(setup):
    manager, fake, _, _ = setup
    fake.allowed = True
    fake.uid = 43
    result = await manager.reconcile(publications(), SESSIONS)
    assert result["groups"]["42:123"]["status"] == "blocked_account_mismatch"
    assert not fake.calls


async def test_default_section_reused_and_existing_other_collection_not_moved(setup):
    manager, fake, _, _ = setup
    fake.allowed = True
    fake.section_default = True
    fake.rejected_aids.add(101)
    result = await manager.reconcile(publications(), SESSIONS)
    assert not any(c[0] == "section" for c in fake.calls)
    assert fake.calls[-1] == ("append", [100, 102])
    assert result["groups"]["42:123"]["members"]["101"]["phase"] == "blocked_episode"


async def test_read_only_run_records_plan_but_never_posts(setup):
    manager, fake, _, _ = setup
    fake.allowed = True
    result = await manager.reconcile(publications(), SESSIONS, allow_writes=False)
    assert result["groups"]["42:123"]["status"] == "pending_write_disabled"
    assert not fake.calls


async def test_current_official_json_write_schema_and_no_transport_retry():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": 500})
    async with BiliCollectionClient("SESSDATA=fake; bili_jct=fake-csrf", httpx.MockTransport(handler)) as client:
        await client.create_collection("主播甲直播切片", "主播主页：https://space.bilibili.com/123", "https://archive.biliimg.com/bfs/archive/cover.jpg")
        await client.create_section(500)
        await client.append_episodes(800, [{"aid": 100, "cid": 1100, "title": "片段"}])
    assert [r.url.path for r in requests] == ["/x2/creative/web/season/add", "/x2/creative/web/season/section/add", "/x2/creative/web/season/section/episodes/add"]
    assert all(r.url.params["csrf"] == "fake-csrf" for r in requests)
    assert json.loads(requests[1].content) == {"type": 0, "seasonId": 500, "title": "直播切片"}
    assert json.loads(requests[2].content) == {"sectionId": 800, "episodes": [{"title": "片段", "aid": 100, "cid": 1100, "charging_pay": 0, "member_first": 0, "limited_free": False}]}
