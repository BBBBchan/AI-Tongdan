"""Author-provided conventions are portable instructions, never live evidence."""
import builtins
from pathlib import Path

import pytest

from app.danmaku_voice import common_sense_instruction, voice_instruction


@pytest.mark.parametrize("style_value", [0, 20, 21, 40, 41, 60, 61, 80, 81, 100])
def test_room_address_has_priority_over_optional_default_at_every_style(style_value):
    rules = common_sense_instruction(style_value)
    assert "当前现场有明确对主播的惯称时优先沿用" in rules
    assert "第三人的名字不能当成主播称呼" in rules
    assert "不知道惯称时可叫‘主包’" in rules
    assert "也可省略称呼，不要句句主包" in rules
    assert "‘宝宝’只在现场亲昵语气合适时用" in rules
    assert "不是普适称呼，不当实名，也不推断真实关系" in rules


@pytest.mark.parametrize("style_value", [0, 100])
def test_ordinary_examples_remain_available_at_both_slider_extremes(style_value):
    rules = common_sense_instruction(style_value)
    examples = ["宝宝", "卧槽", "太惨了", "好崩溃..", "咋这样", "老大我们这样真能xx吗",
                "我去", "？", "神人", "这谁不怕", "给我吃口", "晚上好", "下午好", "早上好",
                "早早早", "好听...", "收到", "xx说是", "流汗了", "支持"]
    assert all(f"‘{text}’" in rules for text in examples)
    assert "普通短句在最高档仍然有效" in rules
    assert "没有合适反差时照常接话" in rules


@pytest.mark.parametrize("style_value,label", [
    (0, "纯良档"), (20, "纯良档"), (21, "轻松档"), (40, "轻松档"),
    (41, "玩梗档"), (60, "玩梗档"), (61, "吐槽档"), (80, "吐槽档"),
    (81, "开串档"), (100, "开串档"),
])
def test_example_tendency_follows_the_selected_slider_tier(style_value, label):
    assert label in common_sense_instruction(style_value)


def test_playful_examples_are_contextual_and_not_a_pure_style_default():
    pure = common_sense_instruction(0)
    light = common_sense_instruction(30)
    high = common_sense_instruction(100)
    assert "不强套反话" in pure and "偏串短话语感" not in pure
    assert "现场已经这样用且语境贴合时偶尔接" in light
    assert "更积极选有依据的偏串短话" in high
    assert "不要求句句反串" in high
    examples = ["爆了", "爆爆爆", "懂你意思", "这下看懂了", "乐", "大大方方睡",
                "大大方方摆烂", "大大方方看", "大大方方玩", "开考", "xx考子来了"]
    assert all(f"‘{text}’" in high for text in examples)


@pytest.mark.parametrize("style_value", [0, 30, 60, 100])
def test_templates_quotes_and_greetings_require_current_evidence(style_value):
    rules = common_sense_instruction(style_value)
    assert "只在当前画面、声音或弹幕有对应语境时选" in rules
    assert "没有合适依据就不用" in rules
    assert "不自行解释或发明固定含义" in rules
    assert "xx和并列示例用的斜杠只是模板说明，不能原样输出" in rules
    assert "否则弃用模板" in rules
    assert "‘xx说是’的引用对象及其说法、‘xx考子’所指都不能编造" in rules
    assert "content_clock提供的北京时间" in rules
    assert "仅在时间一致或现场问候成立时使用" in rules
    assert "不知道时间不能猜早晚" in rules


def test_shared_guidance_is_bounded_separate_from_builtin_rhythm_and_count_independent():
    for value in (0, 30, 60, 80, 100):
        shared, rhythm = common_sense_instruction(value), voice_instruction(value)
        assert len(shared) < 850
        assert shared.count("作者提供的普通短话语感") == 1
        assert "作者提供的普通短话语感" not in rhythm
        assert "表达节奏" in rhythm
        assert "五条" not in shared + rhythm and "5条" not in shared + rhythm


def test_guidance_needs_no_local_json_or_other_runtime_file_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Portable voice guidance must not open runtime files")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    for value in (0, 30, 60, 80, 100):
        assert common_sense_instruction(value)
        assert voice_instruction(value)


@pytest.mark.parametrize("style_value", [-1, 101, True, False, 30.0, "30", None])
def test_shared_rules_reject_invalid_slider_values(style_value):
    with pytest.raises(ValueError, match="style value"):
        common_sense_instruction(style_value)
