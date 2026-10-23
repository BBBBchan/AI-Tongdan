"""Reference-style source credits stay concise without losing user additions."""
import asyncio

import pytest

from clipper.config import Settings
from clipper.patrol import FollowingPatrol
from clipper.publishing import metadata_for, public_description_from_fields


@pytest.mark.parametrize(("fields", "expected"), [
    ({"creator_name": "当前主播", "homepage": "https://space.bilibili.com/123"},
     "当前主播：https://space.bilibili.com/123"),
    ({"creator_name": "当前主播"}, "当前主播"),
    ({"homepage": "https://space.bilibili.com/123"}, "https://space.bilibili.com/123"),
    ({"live_room": "https://live.bilibili.com/456"}, "直播间：https://live.bilibili.com/456"),
    ({"source": "https://www.bilibili.com/video/BV1JGnE6zEYU"},
     "素材来源：https://www.bilibili.com/video/BV1JGnE6zEYU"),
    ({}, ""),
])
def test_known_sources_only(fields, expected):
    assert public_description_from_fields(fields) == expected


def test_shared_formatter_never_copies_unlisted_prose_fields():
    assert public_description_from_fields({
        "creator_name": "当前主播", "description": "private-user-note", "cookie": "private-cookie",
    }) == "当前主播"


@pytest.mark.parametrize("description", [
    "这段字幕已人工校对。\n剪辑：测试作者",
    "欢迎关注主播，更多内容请前往原直播间。",
])
def test_local_custom_description_is_preserved_even_if_it_matches_an_old_default(description):
    result = metadata_for({"anchor": {"title": "测试", "start": 0, "end": 10}}, {
        "mode": "file", "streamer": "当前主播", "streamer_uid": 123,
        "publish_profile": {"description": description},
    })
    assert result["description"] == "当前主播：https://space.bilibili.com/123\n\n" + description


@pytest.mark.parametrize("profile", [
    None,
    {"description": "手工补充的致谢", "follow_creator": False},
    {"description": "欢迎关注主播，更多内容请前往原直播间。"},
])
def test_patrol_defaults_use_creator_follow_credit_and_preserve_existing_user_settings(tmp_path, profile):
    async def scenario():
        patrol = FollowingPatrol(Settings(data_dir=tmp_path), tmp_path / "patrol")
        requests = []

        async def request(method, path, payload=None):
            requests.append((method, path, payload))
            return {}

        patrol.request = request
        try:
            await patrol.adopt({"id": "owned", "created_at": 100, "publish_profile": profile},
                               {"room_id": 456, "name": "当前主播"})
            assert len(requests) == 1
            payload = requests[0][2]
            assert payload["auto_publish"] is False
            assert payload["publish_profile"]["description"] == (profile or {}).get("description", "")
            assert payload["publish_profile"]["follow_creator"] == (profile or {}).get("follow_creator", True)
        finally:
            await patrol.client.aclose()

    asyncio.run(scenario())
