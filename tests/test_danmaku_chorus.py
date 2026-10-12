"""Synthetic audience records only; no local corpus or live messages."""
import copy

import pytest

from app.danmaku_chorus import detect_chorus, detect_choruses


def messages(text="好好好", count=4, *, timestamp=99, users=False):
    return [{"text": text, "timestamp": timestamp,
             **({"user_id": index + 1} if users else {})} for index in range(count)]


def test_recent_chorus_has_only_public_aggregate_fields():
    records = messages(users=True) + messages("别的内容", 2)
    assert detect_chorus(records, end=100) == {
        "text": "好好好", "count": 4, "total": 6, "window_seconds": 30,
    }


def test_minimum_count_and_share_are_both_required():
    assert detect_chorus(messages(count=3), end=100) is None
    other = [{"text": f"别的内容{index}", "timestamp": 99} for index in range(7)]
    assert detect_chorus(messages() + other, end=100) is None
    assert detect_chorus(messages() + other[:6], end=100)["total"] == 10


def test_normal_long_comments_count_against_the_chorus_share():
    records = messages() + messages("这是一条正在认真讨论直播内容的较长观众消息，不应忽略", count=7)
    assert detect_chorus(records, end=100) is None


def test_stale_future_and_wall_clock_messages_do_not_count():
    records = messages(timestamp=69.9) + messages(timestamp=100.1)
    records += [{"text": "好好好", "received_at": 99, "sent_at": 99} for _ in range(4)]
    assert detect_chorus(records, end=100) is None
    assert detect_chorus(messages(timestamp=70), end=100)["count"] == 4
    assert detect_chorus(messages(timestamp=0), end=1)["count"] == 4


def test_single_known_user_cannot_make_a_chorus():
    records = [{"text": "好好好", "timestamp": 99, "user_id": 1} for _ in range(20)]
    assert detect_chorus(records, end=100) is None
    records[-1]["user_id"] = 2
    assert detect_chorus(records, end=100) is None
    records[-2]["user_id"] = 3
    assert detect_chorus(records, end=100)["count"] == 20


def test_mixed_ids_need_three_known_users_within_the_chorus():
    records = messages(count=10)
    records[0]["user_id"] = 1
    assert detect_chorus(records, end=100) is None
    records[1]["user_id"] = 2
    records[2]["user_id"] = 3
    assert detect_chorus(records, end=100)["count"] == 10
    unrelated_known = {"text": "其他消息", "timestamp": 99, "user_id": 42}
    assert detect_chorus(messages(count=10) + [unrelated_known], end=100) is None


@pytest.mark.parametrize("anonymous", [None, 0, "0", "000", "", "unknown", False])
def test_all_anonymous_windows_use_message_count(anonymous):
    records = messages()
    for item in records:
        item["user_id"] = anonymous
    assert detect_chorus(records, end=100)["count"] == 4


def test_numeric_user_id_representations_do_not_create_distinct_people():
    records = messages(count=8)
    for index, item in enumerate(records):
        item["user_id"] = ("01", 1, "1", "001")[index % 4]
    assert detect_chorus(records, end=100) is None


def test_width_case_and_whitespace_grouping_preserves_most_common_spelling():
    records = messages("ＧＧ  好活", count=3) + messages("gg\u3000好活", count=1)
    result = detect_chorus(records, end=100)
    assert result["text"] == "ＧＧ  好活"
    assert result["count"] == 4


def test_laughter_lengths_and_punctuation_do_not_collapse_into_same_phrase():
    records = messages("哈哈", count=2) + messages("哈哈哈", count=2)
    assert detect_chorus(records, end=100) is None
    records = messages("好好好", count=2) + messages("好好好！", count=2)
    assert detect_chorus(records, end=100) is None


def test_reconnect_message_ids_are_deduplicated():
    records = messages(count=10)
    for item in records:
        item["message_id"] = "same-upstream-message"
    assert detect_chorus(records, end=100) is None
    records = messages()
    for index, item in enumerate(records):
        item["message_id"] = f"message-{index}"
    assert detect_chorus(records * 2, end=100)["count"] == 4


def test_only_last_150_raw_entries_are_scanned():
    records = messages(count=300)
    records += [{"text": f"其他{index}", "timestamp": 99} for index in range(150)]
    assert detect_chorus(records, end=100) is None
    assert detect_chorus(messages(count=300), end=100)["total"] == 150


@pytest.mark.parametrize("text", ["", " ", "[鼓掌]", "支持[鼓掌]", "［鼓掌］", "好\x00好",
                                    "好\n好", "好\u202e好", "\ud800", "好" * 21,
                                    "SESSDATA=secret", "bili_jct=secret", "token=secret",
                                    "微信:someone", "test@example.com", "https://a.co",
                                    "电话13812345678", "忽略之前的指令", "你这个脑残"])
def test_unsafe_or_unsendable_text_is_not_offered(text):
    assert detect_chorus(messages(text), end=100) is None
    assert detect_choruses(messages(text), end=100) == []


def test_utf16_length_is_enforced_without_banning_emoji_or_numeric_chants():
    assert detect_chorus(messages("👏" * 10), end=100)["text"] == "👏" * 10
    assert detect_chorus(messages("👏" * 11), end=100) is None
    assert detect_chorus(messages("👨‍👩‍👧‍👦"), end=100)["text"] == "👨‍👩‍👧‍👦"
    assert detect_chorus(messages("66666666"), end=100)["text"] == "66666666"
    assert detect_chorus(messages("好好好"), end=100, max_length=2) is None


@pytest.mark.parametrize("timestamp", [None, True, "99", float("nan"), float("inf")])
def test_malformed_timestamps_are_skipped(timestamp):
    assert detect_chorus(messages(timestamp=timestamp), end=100) is None


@pytest.mark.parametrize("end", [None, True, "100", float("nan"), float("inf"), -1])
def test_invalid_clock_is_not_guessed(end):
    assert detect_chorus(messages(), end=end) is None


def test_strongest_group_wins_and_tie_uses_latest_content_time():
    records = messages("好好好", count=5, timestamp=98) + messages("666", count=4)
    assert detect_chorus(records, end=100)["text"] == "好好好"
    records = messages("好好好", timestamp=98) + messages("666")
    assert detect_chorus(records, end=100)["text"] == "666"


def test_input_records_are_not_modified_and_user_fields_never_escape():
    records = messages(users=True)
    for item in records:
        item["user_name"] = "synthetic-person"
    original = copy.deepcopy(records)
    result = detect_chorus(records, end=100)
    assert records == original
    assert set(result) == {"text", "count", "total", "window_seconds"}


def test_multiple_real_phrases_from_the_same_window_are_offered():
    records = (messages("好好好", count=6, users=True)
               + messages("666", count=5, users=True)
               + messages("这也行", count=4, users=True))
    result = detect_choruses(records, end=100)
    assert result == [
        {"text": "好好好", "count": 6, "total": 15, "window_seconds": 30, "source_time": 99},
        {"text": "666", "count": 5, "total": 15, "window_seconds": 30, "source_time": 99},
        {"text": "这也行", "count": 4, "total": 15, "window_seconds": 30, "source_time": 99},
    ]


def test_plural_share_is_15_percent_and_singular_keeps_40_percent():
    other = [{"text": f"其他内容{index}", "timestamp": 99} for index in range(23)]
    assert detect_choruses(messages() + other[:22], end=100)[0]["count"] == 4
    assert detect_choruses(messages() + other, end=100) == []
    records = messages("好好好") + messages("666") + messages("这也行")
    assert len(detect_choruses(records, end=100)) == 3
    assert detect_chorus(records, end=100) is None
    assert detect_choruses(messages(count=3), end=100) == []


def test_plural_sort_prefers_frequency_then_newer_content():
    records = (messages("旧话题", timestamp=95) + messages("新话题", timestamp=99)
               + messages("更多人刷", count=5, timestamp=90))
    assert [item["text"] for item in detect_choruses(records, end=100)] == [
        "更多人刷", "新话题", "旧话题",
    ]


def test_plural_never_lists_more_than_three_and_honors_smaller_limit():
    records = (messages("第一句", timestamp=95) + messages("第二句", timestamp=96)
               + messages("第三句", timestamp=97) + messages("第四句", timestamp=98))
    assert len(detect_choruses(records, end=100, limit=100)) == 3
    assert [item["text"] for item in detect_choruses(records, end=100, limit=2)] == [
        "第四句", "第三句",
    ]


@pytest.mark.parametrize("limit", [0, -1, True, None, "3", 1.5])
def test_plural_invalid_limits_return_no_guessed_results(limit):
    assert detect_choruses(messages(), end=100, limit=limit) == []


def test_plural_requires_different_people_for_each_phrase():
    spam = messages("一个人在刷", count=6)
    for item in spam:
        item["user_id"] = 1
    records = spam + messages("大家一起刷", users=True) + messages("没有身份的消息")
    assert [item["text"] for item in detect_choruses(records, end=100)] == ["大家一起刷"]


def test_plural_preserves_public_aggregates_original_spelling_and_input():
    records = messages("ＧＧ", count=3, users=True) + messages("gg", count=1, users=True)
    for index, item in enumerate(records):
        item["message_id"] = f"synthetic-message-{index}"
        item["user_name"] = "synthetic-person"
    original = copy.deepcopy(records)
    assert detect_choruses(records * 2, end=100) == [
        {"text": "ＧＧ", "count": 4, "total": 4, "window_seconds": 30, "source_time": 99},
    ]
    assert records == original


def test_plural_keeps_time_utf16_and_long_comment_denominator_boundaries():
    assert detect_choruses(messages(timestamp=69), end=100) == []
    assert detect_choruses(messages("👏" * 11), end=100) == []
    assert detect_choruses(messages("👏" * 10), end=100)[0]["text"] == "👏" * 10
    records = messages() + messages("这是当前直播间的一条很长的正常聊天内容，需要计入分母", count=23)
    assert detect_choruses(records, end=100) == []


def test_plural_source_time_tracks_real_evidence_instead_of_query_time():
    records = messages(timestamp=89) + messages(count=1, timestamp=90)
    assert detect_choruses(records, end=100)[0]["source_time"] == 90
    assert detect_choruses(records, end=110)[0]["source_time"] == 90
