import httpx
import pytest

from clipper.bili_series import (
    BiliSeriesInspector, series_append_payload, series_create_payload,
    streamer_series_plan, ugc_append_payload, ugc_create_payload,
)


COOKIE = "SESSDATA=series-test-only; bili_jct=csrf-test-only; DedeUserID=42"


def session(name="主播甲", uid=123):
    return {"mode": "live", "streamer": name, "streamer_uid": uid,
            "room": {"owner_uid": uid, "room_id": 678}}


def test_uid_and_account_determine_stable_group_key_not_display_name():
    first = streamer_series_plan(session(), 42)
    renamed = streamer_series_plan(session("改名后的主播"), 42)
    same_name_other_person = streamer_series_plan(session(uid=124), 42)
    other_publisher = streamer_series_plan(session(), 43)
    assert first["key"] == renamed["key"] == "42:123"
    assert first["key"] != same_name_other_person["key"] != other_publisher["key"]
    assert first["title"] == "主播甲直播切片"
    assert first["platform_series_id"] is None
    assert first["status"] == "pending_platform_capability"


def test_introduction_and_imported_material_do_not_enter_a_streamer_series():
    assert streamer_series_plan({**session(), "mode": "file"}, 42) is None


@pytest.mark.parametrize("bad", [None, 0, "not-a-uid", True])
def test_verified_uid_is_required(bad):
    with pytest.raises(ValueError):
        streamer_series_plan({"mode": "live", "streamer": "名字", "streamer_uid": bad}, 42)


def test_conflicting_room_identity_is_rejected():
    data = session()
    data["room"]["owner_uid"] = 999
    with pytest.raises(ValueError, match="differs"):
        streamer_series_plan(data, 42)


async def test_verified_collection_endpoints_are_get_only_and_zero_is_not_series_denial():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.host == "member.bilibili.com"
        if request.url.path.endswith("/remain"):
            data = 0
        else:
            assert request.url.params["draft"] == "1"
            assert request.url.params["source"] == "0"
            data = {"seasons": None, "total": 0, "sort_mode": "default"}
        return httpx.Response(200, json={"code": 0, "data": data})

    async with BiliSeriesInspector(COOKIE, transport=httpx.MockTransport(handle)) as client:
        assert await client.ugc_remaining() == 0
        assert (await client.ugc_seasons())["seasons"] == []
    assert len(calls) == 2


def test_collection_request_builders_match_official_ui_schema_without_network():
    assert ugc_create_payload("主播甲直播切片", "简介", "https://archive.biliimg.com/a.jpg") == {
        "title": "主播甲直播切片", "desc": "简介",
        "cover": "https://archive.biliimg.com/a.jpg", "season_price": 0,
    }
    assert ugc_append_payload(1, 2, 3, "标题") == {"sectionId": 1, "episodes": [
        {"title": "标题", "aid": 2, "cid": 3, "charging_pay": 0,
         "member_first": 0, "limited_free": False}
    ]}
    with pytest.raises(ValueError):
        ugc_create_payload("title", "", "https://example.test/image.jpg")


async def test_ordinary_series_can_be_empty_independently_of_collection_quota():
    def handle(request):
        assert request.method == "GET"
        assert request.url.host == "api.bilibili.com"
        assert request.url.path == "/x/polymer/web-space/seasons_series_list"
        assert request.url.params["mid"] == "42"
        return httpx.Response(200, json={"code": 0, "data": {"items_lists": {
            "page": {"page_num": 1, "page_size": 20, "total": 0},
            "series_list": [], "seasons_list": [],
        }}})
    async with BiliSeriesInspector(COOKIE, transport=httpx.MockTransport(handle)) as client:
        assert (await client.video_series(42))["series_list"] == []


def test_series_create_and_append_fields_follow_published_sdk_and_avoid_empty_groups():
    assert series_create_payload(42, "主播甲直播切片", [123, 123], "说明", ["主播甲"]) == {
        "mid": 42, "name": "主播甲直播切片", "aids": "123", "description": "说明", "keywords": "主播甲",
    }
    assert series_append_payload(42, 789, [123, 124]) == {
        "mid": 42, "series_id": 789, "aids": "123,124",
    }
    with pytest.raises(ValueError, match="first published"):
        series_create_payload(42, "主播甲直播切片", [])
