import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from clipper.detector import Detector, heatmap
from clipper.store import Store


@pytest.fixture
def scene(tmp_path):
    store = Store(tmp_path / "journal")
    session = store.create_session(title="B站测试直播", duration=120, minimum_score=72)
    sid = session["id"]
    store.add_segment(sid, 0, 120, tmp_path / "recording.mp4")
    setup = store.add_event(sid, "transcript", 20, 28, "我以为这把肯定能赢，结果我第一个倒下了。")
    speech = store.add_event(sid, "transcript", 40, 46, "没想到你们夸我的不是技术，是我嘴硬的速度。")
    chats = [store.add_event(sid, "danmaku", 45 + i, 45 + i,
                             ["哈哈哈", "笑死我了", "嘴是真的硬", "好家伙", "没绷住", "？？？"][i],
                             {"uid": 100 + i}) for i in range(6)]
    settings = SimpleNamespace(data_dir=tmp_path, minimum_score=72, import_roots=(),
                               model_config=lambda: {"base_url": "http://model.invalid/v1",
                                                     "model": "test-model", "api_key": ""})
    detector = Detector(settings, store)
    proposal = {"start": 15, "end": 65, "peak": 43, "score": 86,
                "story_complete": True,
                "reason": "铺垫失败与主播自嘲形成反差，观众接住笑点",
                "quote": "没想到你们夸我的不是技术，是我嘴硬的速度。",
                "evidence_ids": [setup, speech] + chats[:2],
                "focus_event_ids": chats[:2], "title_variants": ["本想被夸技术，结果夸到了嘴硬速度",
                                                               "主播刚自嘲完，弹幕就笑了"],
                "cover_text": "夸的是技术？\n不，是嘴硬速度"}
    return SimpleNamespace(store=store, sid=sid, settings=settings, detector=detector,
                           proposal=proposal, speech=speech, chats=chats, tmp=tmp_path)


def model_result(monkeypatch, detector, proposals):
    async def ask(*args, **kwargs):
        return {"anchors": deepcopy(proposals)}
    monkeypatch.setattr(detector, "_ask_model", ask)


async def test_grounded_proposal_is_persisted_and_repeated_analysis_deduplicates(scene, monkeypatch):
    model_result(monkeypatch, scene.detector, [{**scene.proposal, "end": 80}])
    anchors = await scene.detector.analyze(scene.sid)
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor["method"] == "model" and anchor["status"] == "ready"
    assert anchor["timestamp"] == anchor["peak"]
    assert anchor["focus_event_ids"] == scene.chats[:2]
    assert isinstance(anchor["cover_text"], str)
    assert await scene.detector.analyze(scene.sid) == []
    assert len(scene.store.anchors(scene.sid)) == 1
    assert scene.store.session(scene.sid)["analysis_status"] == "complete"


@pytest.mark.parametrize("patch", [
    {"quote": "主播根本没有说过这句话"},
    {"quote": "没想到你们夸我的技术嘴硬速度"},
    {"evidence_ids": ["fabricated-event"]},
    {"focus_event_ids": ["fabricated-chat"]},
    {"start": -1},
    {"end": 121},
    {"peak": 100},
    {"score": 40},
    {"score": float("nan")},
    {"title_variants": ['主播大喊“我已经登上世界冠军宝座”']},
    {"title_variants": ["主播大喊‘我已经登上世界冠军宝座’"]},
    {"cover_text": "“凭空创造的名言”"},
])
async def test_hallucinated_or_invalid_model_evidence_is_rejected(scene, monkeypatch, patch):
    proposal = {**scene.proposal, **patch}
    model_result(monkeypatch, scene.detector, [proposal])
    assert await scene.detector.analyze(scene.sid) == []
    assert "丢弃" in scene.store.session(scene.sid)["analysis_warning"]


async def test_evidence_must_be_inside_the_proposed_cut(scene, monkeypatch):
    proposal = {**scene.proposal, "start": 35}
    model_result(monkeypatch, scene.detector, [proposal])
    assert await scene.detector.analyze(scene.sid) == []


async def test_empty_model_answer_does_not_trigger_rule_fillers(scene, monkeypatch):
    model_result(monkeypatch, scene.detector, [])
    assert await scene.detector.analyze(scene.sid) == []
    assert scene.store.session(scene.sid)["analysis_method"] == "model"


async def test_no_model_generates_explicit_review_candidates(scene):
    scene.settings.model_config = lambda: {}
    anchors = await scene.detector.analyze(scene.sid)
    assert anchors
    assert all(a["method"] == "rules" and a["status"] == "candidate" for a in anchors)
    assert "规则候选" in scene.store.session(scene.sid)["analysis_warning"]
    source_text = [e["text"] for e in scene.store.events(scene.sid) if e["kind"] == "transcript"]
    assert all(any(a["quote"] in text for text in source_text) for a in anchors)
    assert all(a["focus_event_ids"] == [] and a["highlighted_danmaku"] == [] for a in anchors)


@pytest.mark.parametrize("incidental_chat", [False, True])
async def test_model_accepts_self_contained_story_without_chat_interaction(scene, monkeypatch, incidental_chat):
    sid = scene.store.create_session(duration=90, minimum_score=72)["id"]
    scene.store.add_segment(sid, 0, 90, "story.mp4")
    setup = scene.store.add_event(sid, "transcript", 10, 18, "我出门前把伞放在门口，想着这次总不会忘了。")
    payoff = scene.store.add_event(sid, "transcript", 20, 28, "结果到了公司才发现，我把门口的垃圾袋当伞带走了。")
    if incidental_chat:
        scene.store.add_event(sid, "danmaku", 24, 24, "今天吃什么", {"uid": 1})
    proposal = {
        "start": 8, "end": 30, "peak": 24, "score": 82,
        "reason": "出门前专门准备了雨伞，到了公司才发现拿成垃圾袋，事件本身有完整反差",
        "quote": "我把门口的垃圾袋当伞带走了。",
        "evidence_ids": [setup, payoff], "focus_event_ids": [],
        "title_variants": ["出门前专门把伞放在门口，到了公司才发现拿成了垃圾袋"],
        "cover_text": "专门准备好了伞\n出门拿的却是垃圾袋",
    }
    model_result(monkeypatch, scene.detector, [proposal])
    anchors = await scene.detector.analyze(sid)
    assert len(anchors) == 1
    assert anchors[0]["status"] == "candidate"  # Complete short stories remain manually exportable.
    assert anchors[0]["score"] == 82
    assert anchors[0]["evidence_ids"] == [setup, payoff]
    assert anchors[0]["focus_event_ids"] == []


async def test_rule_fallback_can_offer_developed_story_without_chat(scene):
    scene.settings.model_config = lambda: {}
    sid = scene.store.create_session(duration=90, minimum_score=72)["id"]
    scene.store.add_segment(sid, 0, 90, "story.mp4")
    scene.store.add_event(sid, "transcript", 10, 20,
                         "以前我搬家那时候，特意把所有纸箱都写好了标签，当时还以为这样到了新家就能马上找到每一件东西。")
    scene.store.add_event(sid, "transcript", 22, 34,
                         "结果后来找了一晚上杯子都没找到，最后才发现我把每个箱子上面都写成了杂物，等于一个也没标。")
    anchors = await scene.detector.analyze(sid)
    assert anchors
    assert all(a["method"] == "rules" and a["status"] == "candidate" for a in anchors)
    assert all(a["focus_event_ids"] == [] and a["highlighted_danmaku"] == [] for a in anchors)
    assert all(a["end"] >= 34 for a in anchors)
    assert all(a["score"] >= 72 for a in anchors)


async def test_a_few_story_keywords_do_not_make_a_rule_candidate(scene):
    scene.settings.model_config = lambda: {}
    sid = scene.store.create_session(duration=90, minimum_score=72)["id"]
    scene.store.add_segment(sid, 0, 90, "ordinary.mp4")
    scene.store.add_event(sid, "transcript", 10, 18, "以前在那边，当时是这样。")
    scene.store.add_event(sid, "transcript", 20, 28, "后来我以为可以，结果就这样吧。")
    assert await scene.detector.analyze(sid) == []


async def test_model_failure_falls_back_without_leaking_endpoint_or_secret(scene, monkeypatch):
    async def fail(*args):
        raise httpx.ConnectError("secret-api-token-in-private-endpoint")
    monkeypatch.setattr(scene.detector, "_ask_model", fail)
    anchors = await scene.detector.analyze(scene.sid)
    assert anchors and anchors[0]["status"] == "candidate"
    warning = scene.store.session(scene.sid)["analysis_warning"]
    assert "ConnectError" in warning
    assert "secret-api-token" not in warning


async def test_heat_without_story_never_creates_an_anchor(scene):
    scene.settings.model_config = lambda: {}
    session = scene.store.create_session(duration=120, minimum_score=72)
    scene.store.add_segment(session["id"], 0, 120, "other.mp4")
    for i in range(60):
        scene.store.add_event(session["id"], "danmaku", 30 + i / 10, 30 + i / 10, "哈哈哈哈", {"uid": i})
    assert await scene.detector.analyze(session["id"]) == []
    assert scene.store.session(session["id"])["analysis_status"] == "waiting_for_events"
    scene.store.add_event(session["id"], "transcript", 30, 35, "今天的直播刚刚开始，大家晚上好。")
    assert await scene.detector.analyze(session["id"]) == []


async def test_one_spamming_sender_cannot_make_a_rule_highlight(scene):
    scene.settings.model_config = lambda: {}
    sid = scene.store.create_session(duration=120, minimum_score=72)["id"]
    scene.store.add_segment(sid, 0, 120, "other.mp4")
    scene.store.add_event(sid, "transcript", 30, 35, "我以为大家都睡了，结果你们居然都还在。")
    for i in range(100):
        scene.store.add_event(sid, "danmaku", 35 + i / 100, 35 + i / 100, f"哈哈哈{i}", {"uid": 999})
    assert await scene.detector.analyze(sid) == []
    h = heatmap(scene.store.events(sid), 120)
    peak = max(h, key=lambda x: x["count"])
    assert peak["count"] == 100 and peak["unique_authors"] == 1
    assert peak["score"] < 15


async def test_cut_boundary_expands_to_complete_utterance(scene, monkeypatch):
    scene.store.add_event(scene.sid, "transcript", 10, 17, "这是一个需要保持完整的开场。")
    scene.store.add_event(scene.sid, "transcript", 62, 70, "这一句话不能在中间被剪断。")
    model_result(monkeypatch, scene.detector, [scene.proposal])
    anchors = await scene.detector.analyze(scene.sid)
    assert anchors[0]["start"] == 10
    assert anchors[0]["end"] == 70


async def test_recording_gap_is_not_hidden_by_wide_clip(scene, monkeypatch):
    sid = scene.store.create_session(duration=120, minimum_score=72)["id"]
    scene.store.add_segment(sid, 0, 30, "first.mp4")
    scene.store.add_segment(sid, 40, 120, "second.mp4")
    first = scene.store.add_event(sid, "transcript", 20, 25, "结果居然是这样的。")
    second = scene.store.add_event(sid, "transcript", 43, 47, "终于回来了，真没想到。")
    proposal = {**scene.proposal, "evidence_ids": [first, second], "quote": "结果居然是这样的。",
                "focus_event_ids": []}
    model_result(monkeypatch, scene.detector, [proposal])
    assert await scene.detector.analyze(sid) == []


async def test_live_analysis_uses_only_finished_media(scene, monkeypatch):
    scene.store.update_session(scene.sid, status="live", duration=240)
    scene.store.add_segment(scene.sid, 120, 240, "still-writing.mp4", {"complete": False})
    calls = []
    async def ask(config, events, left, right, *args):
        calls.append((left, right))
        assert right <= 120
        return {"anchors": [deepcopy(scene.proposal)]}
    monkeypatch.setattr(scene.detector, "_ask_model", ask)
    anchors = await scene.detector.analyze(scene.sid, end=240)
    assert len(anchors) == 1 and calls == [(0, 120)]
    assert scene.store.session(scene.sid)["analyzed_until"] == 120


async def test_live_cut_needs_a_finished_tail(scene, monkeypatch):
    scene.store.update_session(scene.sid, status="live")
    proposal = {**scene.proposal, "end": 120}
    model_result(monkeypatch, scene.detector, [proposal])
    anchors = await scene.detector.analyze(scene.sid)
    assert len(anchors) == 1 and anchors[0]["status"] == "candidate"
    assert "后续素材" in anchors[0]["review_reason"]


async def test_no_finalized_media_does_not_trust_session_duration(scene):
    sid = scene.store.create_session(duration=3600, status="live", minimum_score=72)["id"]
    assert await scene.detector.analyze(sid) == []
    assert scene.store.session(sid)["analysis_status"] == "waiting_for_media"


async def test_complete_eleven_second_joke_is_not_forced_to_twenty(scene, monkeypatch):
    proposal = {**scene.proposal, "start": 39, "end": 50, "evidence_ids": [scene.speech],
                "focus_event_ids": []}
    model_result(monkeypatch, scene.detector, [proposal])
    anchors = await scene.detector.analyze(scene.sid)
    assert len(anchors) == 1 and anchors[0]["end"] - anchors[0]["start"] == 11


async def test_overlapping_model_proposals_merge_to_one_story(scene, monkeypatch):
    second = {**scene.proposal, "start": 10, "end": 70, "score": 84}
    model_result(monkeypatch, scene.detector, [scene.proposal, second])
    anchors = await scene.detector.analyze(scene.sid)
    assert len(anchors) == 1 and anchors[0]["start"] == 10 and anchors[0]["end"] == 70
    assert anchors[0]["score"] == 86


async def test_concurrent_analysis_is_not_sent_twice(scene, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def ask(*args):
        calls.append(1)
        entered.set()
        await release.wait()
        return {"anchors": [deepcopy(scene.proposal)]}
    monkeypatch.setattr(scene.detector, "_ask_model", ask)
    first = asyncio.create_task(scene.detector.analyze(scene.sid))
    await entered.wait()
    assert await scene.detector.analyze(scene.sid) == []
    release.set()
    assert len(await first) == 1 and len(calls) == 1


def test_visual_input_uses_real_frames_but_not_arbitrary_local_files(scene, tmp_path):
    image = scene.tmp / "frame.jpg"
    Image.new("RGB", (1280, 720), "navy").save(image)
    events = [{"id": "frame-id", "start": 1.0, "data": {"frame_path": str(image)}}]
    result = scene.detector._frame_content(events)
    assert len(result) == 2 and result[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    scene.settings.data_dir = tmp_path / "other-root"
    assert scene.detector._frame_content(events) == []


def test_heatmap_is_bounded_and_reports_unknown_authors_as_unknown():
    events = [{"kind": "danmaku", "start": 5, "text": "哈哈哈哈", "data": {}}] * 200
    bars = heatmap(events, 86400 * 2)
    assert len(bars) <= 2400
    assert bars[0]["unique_authors"] == 0
    assert bars[0]["anonymous_texts"] == 1
    assert heatmap(events, 0) == []


async def test_qwen_request_disables_thinking_and_requires_json(scene, monkeypatch):
    captured = {}
    class Client:
        def __init__(self, **kwargs):
            captured["client_options"] = kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, headers, json):
            captured.update(url=url, payload=json)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                   "message": {"content": '{"anchors":[]}'}}]},
                                  request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = await scene.detector._ask_model({"base_url": "http://test/v1", "model": "Qwen3.8"},
                                              [], 0, 120, [], {})
    assert result == '{"anchors":[]}'
    assert captured["url"] == "http://test/v1/chat/completions"
    assert captured["client_options"]["trust_env"] is False
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["payload"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(("session", "expected_name"), [
    ({"title": "今晚聊聊我的奇怪经历"}, ""),
    ({"title": "今晚聊天", "room": "47867", "streamer": "已确认主播"}, "已确认主播"),
    ({"title": "今晚聊天", "room": {"uname": "平台主播名"}, "streamer": "旧配置名"}, "平台主播名"),
    ({"title": "今晚聊天", "streamer": {"not": "a name"}}, ""),
])
async def test_model_receives_known_identity_separately_from_broadcast_title(
        scene, monkeypatch, session, expected_name):
    import json
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            captured.update(json)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                   "message": {"content": '{"anchors":[]}'}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    events = [
        {"id": "unattributed", "kind": "transcript", "start": 10, "end": 12,
         "text": "他终于吃到了", "data": {"audio_url": "private-source"}},
        {"id": "known-speaker", "kind": "transcript", "start": 14, "end": 16,
         "text": "这个视频挺有意思", "data": {"role": "streamer",
         "speaker_uncertain": False, "uncertain": False, "internal_path": "/private"}},
    ]
    await scene.detector._ask_model({"base_url": "http://test/v1", "model": "test"},
                                   events, 0, 120, [], session)
    info = json.loads(captured["messages"][1]["content"][0]["text"])
    assert info["streamer"] == expected_name
    assert info["broadcast_title"] == session["title"]
    examples = info["title_reference_examples"]
    assert len(examples) >= 12
    assert all(example["bvid"].startswith("BV") and example["title"] and example["technique"]
               for example in examples)
    assert all(set(example) == {"bvid", "title", "technique"} for example in examples)
    unknown, known = [dict(zip(info["event_columns"], row)) for row in info["events"]]
    assert unknown["role"] == "unknown"
    assert unknown["speaker_uncertain"] is True
    assert unknown["transcript_uncertain"] is True
    assert known["role"] == "streamer"
    assert known["speaker_uncertain"] is False
    assert known["transcript_uncertain"] is False
    assert "private-source" not in json.dumps(info)
    assert "/private" not in json.dumps(info)


async def test_compact_model_request_preserves_every_utterance_and_nonempty_chat_bucket(scene, monkeypatch):
    import json
    captured = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            captured.update(json)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                   "message": {"content": '{"anchors":[]}'}}]},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    speech = [{"id": f"speech-{i}", "kind": "transcript", "start": i * 3 + .123456,
               "end": i * 3 + 2.987654, "text": f"这是第{i}句完整原话。"} for i in range(280)]
    speech[70]["text"] = "这句话仍然属于同一个事件，不能擅自截掉后面的原文。" * 80
    events = speech + [
        {"id": "chat", "kind": "danmaku", "start": 240, "end": 240,
         "text": "这里有一条关键弹幕", "data": {"uid": 123}},
        {"id": "observation", "kind": "observation", "start": 241, "end": 241,
         "text": "画面中已经出现了比赛结果"},
    ]
    activity = [
        {"start": 0, "end": 10, "count": 0, "unique_authors": 0, "reactions": 0,
         "score": 0, "baseline": 2},
        {"start": 240, "end": 250, "count": 1, "unique_authors": 1, "reactions": 0,
         "score": 4, "baseline": 2},
        {"start": 910, "end": 920, "count": 1, "unique_authors": 1, "reactions": 0,
         "score": 4, "baseline": 2},
    ]
    await scene.detector._ask_model({"base_url": "http://test/v1", "model": "test"},
                                   events, 0, 900, activity, {})
    text = captured["messages"][1]["content"][0]["text"]
    info = json.loads(text)
    rows = [dict(zip(info["event_columns"], row)) for row in info["events"]]
    assert len(rows) == len(events)
    assert [(row["id"], row["text"]) for row in rows[:280]] == [
        (event["id"], event["text"]) for event in speech]
    assert rows[0]["start"] == .123 and rows[0]["end"] == 2.988
    assert rows[280]["author"] == "123" and rows[280]["role"] is None
    assert len(info["events"][281]) == 5
    assert info["chat_activity_columns"] == ["start", "end", "count", "unique_authors", "reactions", "score"]
    assert info["chat_activity"] == [[240, 250, 1, 1, 0, 4]]
    assert ": " not in text and ", " not in text
