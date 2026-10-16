"""Common-expression recognition is deliberately narrower than chat generation."""
import pytest

from app.danmaku_corpus import _reaction_family, is_common_reaction


@pytest.mark.parametrize("text", [
    "宝宝", "卧槽", "太惨了", "好崩溃..", "咋这样", "我去", "？", "神人", "这谁不怕", "给我吃口",
    "晚上好", "下午好", "早上好", "早早早", "好听...", "收到", "流汗了", "支持",
    "爆了", "爆爆爆", "懂你意思", "这下看懂了", "乐", "大大方方睡", "大大方方摆烂", "大大方方看",
    "大大方方玩", "开考", "好崩溃……", "太惨了！", "懂你意思。",
])
def test_provided_short_expressions_are_reusable(text):
    assert is_common_reaction(text)


@pytest.mark.parametrize("text", [
    "甲说是", "甲考子来了", "老大我们这样真能赢吗", "大大方方泄露密码", "大大方方王小明",
    "宝宝王小明", "卧槽甲君", "开考王小明", "爆了他家地址", "支持甲君违法", "收到密码123",
    "太惨了王小明", "晚上好我是甲", "这下看懂了甲君", "开考\n", "\u202e宝宝",
])
def test_known_words_do_not_admit_named_templates_or_arbitrary_suffixes(text):
    assert not is_common_reaction(text)


def test_different_reactions_and_different_actions_keep_distinct_families():
    texts = ["宝宝", "卧槽", "太惨了", "好崩溃..", "咋这样", "神人", "这谁不怕", "给我吃口",
             "晚上好", "下午好", "早上好", "早早早", "好听...", "收到", "流汗了", "支持",
             "爆了", "懂你意思", "这下看懂了", "乐", "大大方方睡", "大大方方摆烂", "大大方方看",
             "大大方方玩", "开考"]
    assert len({_reaction_family(text) for text in texts}) == len(texts)


def test_laughter_punctuation_and_repetition_variants_still_share_families():
    assert {_reaction_family(text) for text in ["哈", "哈哈哈哈", "嘿嘿", "笑死我了", "乐", "乐了"]} == {"laughter"}
    assert {_reaction_family(text) for text in ["？", "？？？", "?!", "！"]} == {"punctuation"}
    assert _reaction_family("好听...") == _reaction_family("好听好听")
    assert _reaction_family("爆爆爆") == _reaction_family("爆爆爆！")
    assert _reaction_family("大大方方玩") == _reaction_family("大大方方玩！")
