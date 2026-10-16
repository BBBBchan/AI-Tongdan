"""Offline music-reply preferences across model drafts and audience additions.

Lyric source strings below are synthetic fixtures, not imported song/corpus data.
No model service, local corpus directory, or danmaku sender is accessed.
"""
import json

import pytest

import app.danmaku_candidates as candidates


class Model:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    async def complete(self, messages, **options):
        self.calls.append((messages, options))
        return json.dumps({"candidates": self.replies}, ensure_ascii=False)


def music_context(mode="builtin", style=30):
    return {
        "source_time": 100,
        "style_mode": mode,
        "style_value": style,
        "visual": [{"start": 94, "end": 98, "description": "虚拟主播对着麦克风唱歌，屏幕显示歌词",
                    "screen_text": "", "delta": "继续演唱"}],
        "audio_transcripts": [],
        "audience_comments": [],
        "audience_expressions": [],
        "audience_chorus": [],
        "recently_sent": [],
        # A genuine local-mode reference forces the reference prompt branch.
        "style_references": ["爱听", "再来"] if mode == "local" else [],
    }


UNWANTED = ["闭眼唱", "太阳呢", "太阳去哪儿了", "彩虹来了", "怎么解脱", "这句唱得好痛", "这句歌词刀我"]
WHOLE_SONG = ["好痛的歌", "这首不赖", "听emo了", "好听好听"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["builtin", "local"])
@pytest.mark.parametrize("style", [0, 100])
async def test_music_preferences_apply_in_both_modes_at_both_slider_extremes(mode, style):
    context = music_context(mode, style)
    model = Model(UNWANTED + ["好痛的歌", "这首不赖"])
    result = await candidates.generate_candidates(model, context, count=5)
    assert result == ["好痛的歌", "这首不赖"]
    assert len(model.calls) == 1  # Neither another inference nor invented padding.
    prompt = model.calls[0][0][0]["content"]
    assert all(term in prompt for term in ("闭眼唱", "歌词", "整首"))
    assert f"{style}/100" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["builtin", "local"])
@pytest.mark.parametrize("style", [0, 100])
async def test_whole_song_feelings_remain_available_without_needing_audience_repetition(mode, style):
    context = music_context(mode, style)
    model = Model(WHOLE_SONG)
    result = await candidates.generate_candidates(model, context)
    assert result == WHOLE_SONG
    assert len(model.calls) == 1
    assert all(not candidates.music_reply_disallowed(reply, context) for reply in WHOLE_SONG)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["audience_chorus", "audience_expressions"])
@pytest.mark.parametrize("phrase", ["闭眼唱", "太阳去哪儿了", "这句歌词刀我"])
async def test_live_repetition_and_local_examples_cannot_override_music_preference(channel, phrase):
    context = music_context("local", 100)
    context["style_references"] = [phrase, "爱听"]
    context["audience_comments"] = [{"time": 95 + index, "text": phrase} for index in range(4)]
    context[channel] = [{"text": phrase, "count": 4, "last_seen": 98, "source_time": 98,
                         "window_seconds": 30, "context": ["正在唱歌"]}]
    model = Model([phrase, "这首不赖"])
    result = await candidates.generate_candidates(model, context)
    assert result == ["这首不赖"]
    assert phrase not in result
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["asr", "screen_text"])
@pytest.mark.parametrize("lyric,reply", [
    ("风吹过没有名字的街", "没有名字的街"),
    ("把昨天折成一只纸船", "把昨天折成一只纸船"),
    ("落叶装满透明的背包", "透明的背包？"),
])
async def test_unlisted_lyric_fragments_from_current_music_evidence_are_not_copied(source, lyric, reply):
    context = music_context()
    if source == "asr":
        context["audio_transcripts"] = [{"start": 94, "end": 98, "text": lyric, "role": "unknown"}]
    else:
        context["visual"][0]["screen_text"] = f"歌词：{lyric}"
    assert candidates.music_reply_disallowed(reply, context)
    result = await candidates.generate_candidates(Model([reply, "好听好听"]), context)
    assert result == ["好听好听"]


@pytest.mark.asyncio
@pytest.mark.parametrize("description,reply", [
    ("正在打游戏，讨论队友刚才给的路线建议", "这句说得对"),
    ("杂谈，大家讨论一段关于作息的建议", "这句说得对"),
    ("窗外刚停雨，天空出现一道彩虹，大家正在聊天气", "彩虹来了"),
])
async def test_nonmusic_game_talk_and_weather_are_not_mistaken_for_lyrics(description, reply):
    context = music_context()
    context["visual"] = [{"start": 94, "end": 98, "description": description, "screen_text": "", "delta": ""}]
    context["audio_transcripts"] = [{"start": 94, "end": 98, "text": description, "role": "unknown"}]
    assert not candidates.music_reply_disallowed(reply, context)
    assert await candidates.generate_candidates(Model([reply]), context) == [reply]


@pytest.mark.asyncio
async def test_only_disallowed_replies_fail_closed_instead_of_creating_five_replacements():
    context = music_context("local", 100)
    context["audience_comments"] = [{"time": 98, "text": "太阳呢"}]
    context["audience_chorus"] = [{"text": "太阳呢", "count": 8, "source_time": 98}]
    context["audience_expressions"] = [{"text": "太阳呢", "count": 8, "last_seen": 98}]
    context["style_references"] = ["太阳呢", "闭眼唱"]
    model = Model(UNWANTED)
    with pytest.raises(ValueError, match="No usable candidates"):
        await candidates.generate_candidates(model, context, count=5)
    assert len(model.calls) == 1
