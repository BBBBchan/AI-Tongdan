"""Author-provided guidance reaches both modes without copying placeholders."""
from datetime import datetime, timezone
import json

import pytest

from app.danmaku_candidates import candidate_context, generate_candidates


class Model:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        return json.dumps({"candidates": self.replies}, ensure_ascii=False)


def context(mode="builtin", value=30):
    value = candidate_context({
        "observer": {"last_timestamp": 30},
        "observations": [{"window_end": 30, "description": "大家讨论刚才的游戏失误"}],
        "danmaku_style": {"value": value, "mode": mode},
    }, source_time=30)
    if mode == "local":
        value["style_references"] = ["别急慢慢打"]
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["builtin", "local"])
async def test_author_common_sense_reaches_both_prompt_modes(mode):
    model = Model(["主包", "咋这样", "收到"])
    assert await generate_candidates(model, context(mode)) == ["主包", "咋这样", "收到"]
    system = model.calls[0][0]["content"]
    assert "主包" in system and "宝宝" in system
    assert "xx考子来了" in system and "大大方方" in system
    assert "晚" in system and "问候" in system
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_placeholder_examples_do_not_leak_into_finished_candidates():
    model = Model(["xx说是", "xx考子来了", "老大我们这样真能xx吗", "大大方方睡/摆烂/看/玩", "老大我们这样真能赢吗", "这下看懂了"])
    result = await generate_candidates(model, context(value=100), count=10)
    assert result == ["老大我们这样真能赢吗", "这下看懂了"]


@pytest.mark.asyncio
async def test_provided_short_reactions_remain_available_in_local_mode():
    data = context("local")
    data["style_references"] = ["宝宝", "太惨了", "流汗了", "给我吃口"]
    replies = ["宝宝", "太惨了", "流汗了", "给我吃口"]
    assert await generate_candidates(Model(replies), data) == replies


def test_content_clock_uses_session_timeline_in_china_time():
    started = datetime(2026, 10, 13, 3, 59, 50, tzinfo=timezone.utc).timestamp()
    data = candidate_context({"session": {"started_at": started}}, source_time=20)
    assert data["content_clock"] == {"timezone": "Asia/Shanghai", "local_time": "2026-10-13 12:00", "hour": 12}
    assert data["visual"] == [] and data["audio_transcripts"] == []


@pytest.mark.parametrize("stamp", [None, True, "1791890137", 0, -1, float("inf"), float("nan"), 1e100])
def test_missing_or_invalid_clock_does_not_invent_a_greeting_time(stamp):
    assert candidate_context({"session": {"started_at": stamp}}, source_time=10)["content_clock"] is None
