"""Reference-style clip copy must be grounded in the current recording."""
from datetime import datetime, timezone

import pytest

from clipper.publishing import (
    metadata_for, publication_metadata_evidence, tags_for, validate_metadata,
    public_description_from_fields,
)


def scene():
    session = {
        "mode": "live", "created_at": datetime(2026, 10, 20, 16, tzinfo=timezone.utc).timestamp(),
        "streamer": "当前主播", "streamer_uid": 12345,
        "room": {"room_id": 67890, "owner_uid": 12345, "title": "标题里有其他人的名字"},
        "source_url": "https://live.bilibili.com/67890",
        "publish_profile": {"tid": 21, "copyright": 2, "tags": ["当前主播", "直播切片"]},
    }
    job = {
        "anchor": {"title": "一次实际的方言互动", "start": 12, "end": 30, "evidence_ids": ["speech", "chat"]},
        "options": {"captions": True},
        "evidence_snapshot": [
            {"id": "speech", "kind": "transcript", "start": 13, "end": 17, "text": "今天聊聊四川话。"},
            {"id": "chat", "kind": "danmaku", "start": 20, "end": 20, "text": "这个方言好亲切"},
        ],
    }
    return job, session


def test_current_streamer_sources_capture_day_and_evidence_tags():
    job, session = scene()
    metadata = metadata_for(job, session)
    assert metadata["description"] == (
        "欢迎关注👉 @当前主播  https://space.bilibili.com/12345\n\n素材：2026年10月21日直播"
    )
    assert metadata["tags"] == ["当前主播", "直播切片", "方言"]
    assert validate_metadata(metadata) == metadata
    evidence = publication_metadata_evidence(job, session)
    assert evidence["source_fields"]["homepage"]["source"] == "session.room.owner_uid"
    assert evidence["source_fields"]["creator_name"]["source"] == "session.streamer"
    assert evidence["source_fields"]["capture_date"]["meaning"] == "本次直播采集日期，不是主播开播日期"
    assert evidence["tags"][-1]["evidence_ids"] == ["speech", "chat"]
    assert evidence["tags"][-1]["matched_text"] == ["四川话", "方言"]


def test_room_verified_name_wins_and_room_title_never_supplies_identity():
    job, session = scene()
    session["room"]["uname"] = "平台当前主播"
    session["publish_profile"]["tags"] = []
    session["publish_profile"]["creator_name"] = "旧配置名"
    metadata = metadata_for(job, session)
    assert metadata["tags"][0] == "平台当前主播"
    assert "旧配置名" not in metadata["description"]
    assert "标题里" not in metadata["description"]


def test_generated_title_reason_and_unbacked_quote_do_not_create_tags():
    job, session = scene()
    job.pop("evidence_snapshot")
    job["anchor"].update(title="七海打CS2，弹幕搞笑反转", reason="无畏契约和天猫双11",
                         quote="我今天打英雄联盟")
    metadata = metadata_for(job, session)
    assert metadata["tags"] == ["当前主播", "直播切片"]
    assert "七海" not in metadata["description"]
    assert "无畏契约" not in metadata["description"]


@pytest.mark.parametrize("patch", [
    {"id": "unrelated"},
    {"start": 2, "end": 3},
    {"start": 29, "end": 31},
    {"kind": "frame"},
    {"start": float("nan")},
])
def test_unreferenced_or_outside_or_nontext_events_are_not_tag_evidence(patch):
    job, session = scene()
    job["evidence_snapshot"] = [
        {"id": "speech", "kind": "transcript", "start": 13, "end": 14, "text": "打CS2", **patch}
    ]
    assert metadata_for(job, session)["tags"] == ["当前主播", "直播切片"]


def test_real_game_mention_is_a_topic_even_without_title_mention():
    job, session = scene()
    job["evidence_snapshot"] = [
        {"id": "speech", "kind": "transcript", "start": 13, "end": 17, "text": "刚才那把CS2真的好险。"}
    ]
    assert metadata_for(job, session)["tags"] == ["当前主播", "直播切片", "CS2"]
    assert publication_metadata_evidence(job, session)["tags"][-1]["matched_text"] == ["CS2"]


def test_import_creation_time_filename_and_generated_date_are_not_broadcast_dates():
    job, session = scene()
    session.update(mode="file", title="2024-01-02星瞳直播", media_path="/tmp/七海2024-01-01.mp4")
    metadata = metadata_for(job, session)
    assert "素材：" not in metadata["description"]
    assert "2024" not in metadata["description"]
    assert "capture_date" in publication_metadata_evidence(job, session)["omissions"]


def test_clip_crossing_beijing_midnight_has_capture_date_range():
    job, session = scene()
    session["created_at"] = datetime(2026, 10, 20, 15, 59, 40, tzinfo=timezone.utc).timestamp()
    assert "素材：2026年10月20日—2026年10月21日直播" in metadata_for(job, session)["description"]


@pytest.mark.parametrize("created", [None, "unknown", float("nan"), float("inf"), -1])
def test_unknown_or_invalid_capture_date_is_omitted(created):
    job, session = scene()
    session["created_at"] = created
    assert "素材：" not in metadata_for(job, session)["description"]


def test_missing_identity_does_not_borrow_reference_creators_or_guess_a_homepage():
    job, session = scene()
    session.pop("streamer")
    session.pop("streamer_uid")
    session["room"] = "67890"
    session["publish_profile"]["tags"] = []
    metadata = metadata_for(job, session)
    assert metadata["tags"] == ["直播切片", "方言"]
    assert "主页：" not in metadata["description"] and "主播：" not in metadata["description"]
    assert "阿梓" not in metadata["description"]


def test_user_confirmed_tags_deduplicate_without_padding_and_optional_follow_line():
    job, session = scene()
    session["publish_profile"].update(tags=[" CS2 ", "cs2", "直播切片"], follow_creator=True)
    metadata = metadata_for(job, session)
    assert metadata["tags"] == ["当前主播", "直播切片", "CS2", "方言"]
    assert metadata["description"].startswith("欢迎关注👉 @当前主播  https://space.bilibili.com/12345\n")
    assert tags_for(job["anchor"], {}, evidence_snapshot=job["evidence_snapshot"]) == ["直播切片", "方言"]


@pytest.mark.parametrize("style,expected", [
    ("profile", "当前主播：https://space.bilibili.com/12345"),
    ("source_links", "当前主播的个人主页：https://space.bilibili.com/12345\n当前主播的直播间：https://live.bilibili.com/67890"),
    ("credit_date", "欢迎关注👉 @当前主播  https://space.bilibili.com/12345\n\n素材：2026年10月21日直播"),
    ("date_links", "▷2026年10月21日直播\n▷当前主播的空间：https://space.bilibili.com/12345\n▷当前主播的直播间：https://live.bilibili.com/67890\n▷欢迎关注@当前主播"),
])
def test_reference_description_presets_use_current_verified_sources(style, expected):
    job, session = scene()
    session["publish_profile"]["description_style"] = style
    assert metadata_for(job, session)["description"] == expected


@pytest.mark.parametrize("creator,expected", [
    ("阿梓从小就很可爱", "阿梓从小就很可爱：https://space.bilibili.com/12345"),
    ("七海Nana7mi", "▷2026年10月21日直播\n▷七海Nana7mi的空间：https://space.bilibili.com/12345\n▷七海Nana7mi的直播间：https://live.bilibili.com/67890\n▷欢迎关注@七海Nana7mi"),
])
def test_streamer_selects_reference_shape_without_borrowing_reference_uid(creator, expected):
    job, session = scene()
    session["streamer"] = creator
    assert metadata_for(job, session)["description"] == expected


@pytest.mark.parametrize("style", ["profile", "source_links", "credit_date", "date_links"])
def test_presets_omit_unknown_dates_and_honor_explicit_no_follow(style):
    job, session = scene()
    session["created_at"] = None
    session["publish_profile"].update(description_style=style, follow_creator=False)
    description = metadata_for(job, session)["description"]
    assert "12345" in description
    for absent in ["欢迎关注", "素材：", "年", "None", "unknown"]:
        assert absent not in description


def test_import_keeps_editable_legacy_credit_and_explicit_source():
    job, session = scene()
    session["mode"] = "file"
    session["publish_profile"].update(
        description_style="profile", follow_creator=True,
        source="https://www.bilibili.com/video/BV1example123/",
        description="用户已经编辑的介绍。",
    )
    assert metadata_for(job, session)["description"] == (
        "欢迎关注 @当前主播：https://space.bilibili.com/12345\n"
        "直播间：https://live.bilibili.com/67890\n"
        "素材来源：https://www.bilibili.com/video/BV1example123/\n\n用户已经编辑的介绍。"
    )


def test_internal_config_and_analysis_never_enter_generated_description():
    job, session = scene()
    session.update(cookie="private-cookie", model_endpoint="private-model-endpoint")
    session["publish_profile"].update(api_key="private-api-key", notes="private-profile-note")
    job["anchor"].update(reason="private-review-note", quote="private-speech", title="private-title")
    description = metadata_for(job, session)["description"]
    assert "private" not in description
    fields = {"creator_name": "当前主播", "homepage": "https://space.bilibili.com/12345",
              "api_key": "private-key", "reason": "private-reason"}
    for style in ["profile", "source_links", "credit_date", "date_links"]:
        assert "private" not in public_description_from_fields(fields, style=style)
