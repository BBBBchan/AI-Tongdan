"""Candidate quotas use synthetic evidence and a stub; no model or send API."""
import json

import pytest

from app.danmaku_candidates import generate_candidates


class Model:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def complete(self, messages, **options):
        self.calls.append((messages, options))
        return json.dumps({"candidates": self.items}, ensure_ascii=False)


def context(**changes):
    return {"visual": [{"description": "画面人物举起蓝色杯子"}], **changes}


@pytest.mark.asyncio
async def test_default_count_keeps_five_and_same_request_as_explicit_five():
    items = [f"接话{index}" for index in range(12)]
    implicit, explicit = Model(items), Model(items)
    assert await generate_candidates(implicit, context()) == items[:5]
    assert await generate_candidates(explicit, context(), count=5) == items[:5]
    assert implicit.calls == explicit.calls
    assert implicit.calls[0][1] == {"max_tokens": 360, "json_mode": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["builtin", "local"])
async def test_ten_count_reaches_both_prompts_json_example_and_output_budget(mode):
    items = [f"接话{index}" for index in range(10)]
    model = Model(items)
    evidence = context(style_mode=mode, style_references=["以前房间里的话"])
    assert await generate_candidates(model, evidence, count=10) == items
    prompt = model.calls[0][0][0]["content"]
    assert "拟写10条" in prompt
    assert "目标为10条" in prompt and "依据不足时可以少于10条" in prompt
    assert "五条" not in prompt and "五个话题" not in prompt and "5条" not in prompt
    example = json.loads(prompt[prompt.index('{"candidates"'):prompt.rindex("}") + 1])
    assert len(example["candidates"]) == 10
    assert model.calls[0][1] == {"max_tokens": 720, "json_mode": True}


@pytest.mark.asyncio
async def test_ten_sanitizes_and_scans_past_eight_without_reordering_or_padding():
    valid = [f"接话{index}" for index in range(11)]
    dirty = [None, "长" * 21, "有\n换行", "\ud800", "接话0", "接话0", *valid[1:]]
    model = Model(dirty)
    assert await generate_candidates(model, context(), count=10) == valid[:10]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 2, 5, 10])
async def test_supported_counts_bound_model_and_actual_audience_additions(count):
    items = [f"接话{index}" for index in range(12)]
    evidence = context(audience_expressions=[{"text": "哈哈"}, {"text": "还真是"}])
    result = await generate_candidates(Model(items), evidence, count=count)
    assert len(result) == count
    assert result == (["哈哈", "还真是"][:count] + items)[:count]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, -1, 11, 1000, True, False, 5.0, "10", None])
async def test_invalid_counts_fail_before_any_model_call_even_without_evidence(count):
    model = Model(["好"])
    with pytest.raises(ValueError, match="candidate count"):
        await generate_candidates(model, {}, count=count)
    assert not model.calls


@pytest.mark.asyncio
async def test_ten_keeps_a_short_grounded_response_and_does_not_retry_or_pad():
    items = ["杯子好看", "举杯了", "蓝色的"]
    model = Model(items)
    assert await generate_candidates(model, context(), count=10) == items
    assert len(model.calls) == 1
    absent = Model(items)
    assert await generate_candidates(absent, {}, count=10) == []
    assert not absent.calls


@pytest.mark.asyncio
async def test_ten_fallback_only_uses_present_audience_replies_after_filtering():
    live = ["好听好听", "爱听", "再来"]
    evidence = context(visual=[{"description": "主播在唱歌"}],
                       audience_expressions=[{"text": text} for text in live])
    result = await generate_candidates(Model(["哈哈", "还真是", "爱听"]), evidence, count=10)
    assert set(result) == set(live)
    assert len(result) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 5, 10])
@pytest.mark.parametrize("already_in_model", [False, True])
async def test_chorus_occupies_one_slot_and_is_not_appended_above_the_quota(count, already_in_model):
    items = [f"接话{index}" for index in range(10)]
    if already_in_model:
        items = ["好耶", *items]
    evidence = context(audience_chorus=[{"text": "好耶", "count": 4}])
    result = await generate_candidates(Model(items), evidence, count=count)
    assert result == ["好耶", *[text for text in items if text != "好耶"][:count - 1]]
    assert len(result) == count and result.count("好耶") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("chorus", ["[表情]", "uid:123456", "我昨晚看过", "已发的话"])
async def test_chorus_cannot_bypass_safety_or_own_sent_filters_to_fill_quota(chorus):
    evidence = context(audience_chorus=[{"text": chorus, "count": 4}],
                       recently_sent=[{"text": "已发的话"}])
    assert await generate_candidates(Model(["杯子好看"]), evidence, count=10) == ["杯子好看"]
