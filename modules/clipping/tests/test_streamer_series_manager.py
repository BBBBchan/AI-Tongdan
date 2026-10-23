import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from clipper.bili_publish import PublishError
from clipper.bili_series import (
    BiliSeriesClient, SERIES_APPEND_PATH, SERIES_CREATE_PATH, StreamerSeriesManager,
)


def session(uid=123, name="主播甲"):
    return {"mode": "live", "streamer_uid": uid, "streamer": name,
            "room": {"owner_uid": uid, "room_id": 456}}


def publication(aid=100):
    return {"id": f"publication-{aid}", "status": "published", "aid": aid, "account_uid": 42}


class FakeSeries:
    def __init__(self):
        self.uid = 42
        self.rows = {}
        self.create_calls = []
        self.append_calls = []
        self.read_calls = 0
        self.next_id = 500
        self.create_error = None
        self.append_error = None
        self.commit_before_error = False
        self.cancel_create = False

    def __call__(self, cookie):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def account(self):
        self.read_calls += 1
        return {"uid": self.uid, "logged_in": True}

    async def video_series(self, account_uid, page=1, size=20):
        self.read_calls += 1
        return {"series_list": [{"meta": r["meta"]} for r in self.rows.values()],
                "seasons_list": [], "page": {"total": len(self.rows)}}

    async def series_info(self, series_id):
        self.read_calls += 1
        return {"meta": self.rows[series_id]["meta"]}

    async def series_archives(self, account_uid, series_id, page=1, size=20):
        self.read_calls += 1
        aids = self.rows[series_id]["aids"]
        return {"aids": aids[(page - 1) * size:page * size], "page": {"total": len(aids)}}

    async def create_series(self, account_uid, name, aids, description="", keywords=()):
        self.create_calls.append((account_uid, name, aids, description))
        if self.cancel_create:
            raise asyncio.CancelledError()
        if self.create_error and not self.commit_before_error:
            raise self.create_error
        sid = self.next_id
        self.next_id += 1
        self.rows[sid] = {"meta": {"mid": account_uid, "series_id": sid, "name": name,
                                  "description": description}, "aids": list(aids)}
        if self.create_error:
            raise self.create_error
        return {"unverified_response_schema": sid}

    async def append_series(self, account_uid, series_id, aids):
        self.append_calls.append((account_uid, series_id, aids))
        if self.append_error and not self.commit_before_error:
            raise self.append_error
        self.rows[series_id]["aids"] += aids
        if self.append_error:
            raise self.append_error
        return {}


@pytest.fixture
def setup(tmp_path):
    fake = FakeSeries()
    clock = [1000]
    settings = SimpleNamespace(data_dir=tmp_path, upload_cookie=lambda: "test-cookie-only")
    manager = StreamerSeriesManager(settings, client_factory=fake, clock=lambda: clock[0])
    return manager, fake, settings, clock


async def test_create_first_clip_append_second_and_repeated_calls_never_duplicate(setup):
    manager, fake, _, _ = setup
    p = publication()
    original = deepcopy(p)
    first = await manager.archive(p, session())
    assert first["status"] == "added" and first["series_id"] == 500
    assert await manager.archive(p, session()) == first
    assert (await manager.archive(publication(101), session()))["status"] == "added"
    assert len(fake.create_calls) == 1 and len(fake.append_calls) == 1
    assert fake.rows[500]["aids"] == [100, 101]
    assert p == original


async def test_restart_and_rename_reuse_uid_mapping(setup):
    manager, fake, settings, clock = setup
    await manager.archive(publication(), session())
    reopened = StreamerSeriesManager(settings, fake, clock=lambda: clock[0])
    result = await reopened.archive(publication(101), session(name="主播改名了"))
    assert result["series_id"] == 500
    assert len(fake.create_calls) == 1


async def test_equal_names_different_uids_create_distinct_groups(setup):
    manager, fake, _, _ = setup
    a = await manager.archive(publication(), session(uid=123))
    b = await manager.archive(publication(101), session(uid=1234))
    assert a["series_id"] != b["series_id"]
    assert len(fake.create_calls) == 2


async def test_committed_create_with_lost_response_recovers_by_reads_only(setup):
    manager, fake, settings, clock = setup
    fake.create_error = TimeoutError("SESSDATA=must-not-leak")
    fake.commit_before_error = True
    result = await manager.archive(publication(), session())
    assert result["status"] == "uncertain"
    assert "must-not-leak" not in json.dumps(result)
    reads = fake.read_calls
    assert (await manager.archive(publication(), session()))["status"] == "uncertain"
    assert fake.read_calls == reads
    clock[0] += 61
    reopened = StreamerSeriesManager(settings, fake, clock=lambda: clock[0])
    assert (await reopened.archive(publication(), session()))["status"] == "added"
    assert len(fake.create_calls) == 1 and not fake.append_calls


async def test_unconfirmed_create_never_repeats_and_get_rechecks_are_bounded_across_aids(setup):
    manager, fake, _, clock = setup
    fake.create_error = TimeoutError()
    result = await manager.archive(publication(), session())
    for index in range(6):
        clock[0] += 61
        result = await manager.archive(publication(101 + index), session())
    assert result["status"] == "uncertain" and result["retryable"] is False
    reads = fake.read_calls
    clock[0] += 10000
    await manager.archive(publication(999), session())
    assert fake.read_calls == reads and len(fake.create_calls) == 1


async def test_lost_append_response_reconciles_membership_without_second_append(setup):
    manager, fake, _, clock = setup
    await manager.archive(publication(), session())
    fake.append_error = TimeoutError()
    fake.commit_before_error = True
    assert (await manager.archive(publication(101), session()))["status"] == "uncertain"
    clock[0] += 61
    assert (await manager.archive(publication(101), session()))["status"] == "added"
    assert len(fake.append_calls) == 1


async def test_definite_permission_failure_does_not_retry_or_change_publication(setup):
    manager, fake, _, clock = setup
    fake.create_error = PublishError("No permission", code=-403)
    p = publication()
    assert (await manager.archive(p, session()))["status"] == "failed"
    clock[0] += 1000
    assert (await manager.archive(p, session()))["status"] == "failed"
    assert len(fake.create_calls) == 1 and p["status"] == "published"


async def test_cancelled_create_leaves_durable_checkpoint_and_no_retry(setup):
    manager, fake, settings, clock = setup
    fake.cancel_create = True
    p = publication()
    with pytest.raises(asyncio.CancelledError):
        await manager.archive(p, session())
    data = json.loads(manager.path.read_text())
    assert data["groups"]["42:123"]["creation_state"] == "creating"
    fake.cancel_create = False
    reopened = StreamerSeriesManager(settings, fake, clock=lambda: clock[0])
    assert (await reopened.archive(p, session()))["status"] == "uncertain"
    assert len(fake.create_calls) == 1 and p["status"] == "published"


async def test_account_mismatch_and_file_introduction_are_never_written(setup):
    manager, fake, _, _ = setup
    fake.uid = 43
    assert (await manager.archive(publication(), session()))["status"] == "blocked"
    assert (await manager.archive(publication(), {"mode": "file"}))["status"] == "skipped"
    assert (await manager.archive({**publication(), "status": "submitted"}, session()))["status"] == "waiting_for_publication"
    assert not fake.create_calls and not fake.append_calls


async def test_existing_id_owned_by_someone_else_is_not_appended(setup):
    manager, fake, _, _ = setup
    await manager.archive(publication(), session())
    fake.rows[500]["meta"]["mid"] = 99
    assert (await manager.archive(publication(101), session()))["status"] == "blocked"
    assert not fake.append_calls


async def test_existing_collaboration_or_other_named_list_is_not_adopted(setup):
    manager, fake, _, _ = setup
    fake.rows[400] = {"meta": {"mid": 42, "series_id": 400, "name": "主播甲的联动",
                              "description": "https://space.bilibili.com/123"}, "aids": []}
    fake.rows[401] = {"meta": {"mid": 42, "series_id": 401, "name": "主播甲直播切片",
                              "description": "https://space.bilibili.com/123\nhttps://space.bilibili.com/456"},
                     "aids": []}
    result = await manager.archive(publication(), session())
    assert result["series_id"] == 500 and len(fake.create_calls) == 1
    assert fake.rows[400]["aids"] == fake.rows[401]["aids"] == []


async def test_mapped_series_repurposed_by_user_is_not_appended(setup):
    manager, fake, _, _ = setup
    await manager.archive(publication(), session())
    fake.rows[500]["meta"]["description"] = "https://space.bilibili.com/1234"
    assert (await manager.archive(publication(101), session()))["status"] == "blocked"
    assert not fake.append_calls


async def test_client_mutations_use_verified_endpoints_and_csrf_form_without_automatic_retries():
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.host == "api.bilibili.com"
        fields = parse_qs(request.content.decode())
        assert fields["csrf"] == fields["csrf_token"] == ["test-csrf"]
        assert fields["mid"] == ["42"]
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        if request.url.path == SERIES_CREATE_PATH:
            assert fields["aids"] == ["100"] and fields["name"] == ["主播甲直播切片"]
        else:
            assert request.url.path == SERIES_APPEND_PATH and fields["series_id"] == ["500"]
        return httpx.Response(200, json={"code": 0, "data": {}})

    async with BiliSeriesClient("SESSDATA=test-session; bili_jct=test-csrf",
                                transport=httpx.MockTransport(handler)) as client:
        await client.create_series(42, "主播甲直播切片", [100])
        await client.append_series(42, 500, [101])
    assert len(calls) == 2
