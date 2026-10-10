import asyncio
import json
import time

import pytest

from app.agent import LiveAgent, _audio_prompt_evidence


async def emit(*args):
    pass


class AudioModel:
    configured = True

    def __init__(self):
        self.calls = []
        self.answer_started = asyncio.Event()
        self.release_answer = asyncio.Event()

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if "规划器" in messages[0]["content"]:
            return json.dumps({"plan": "核对音频转写", "queries": [], "replay_times": []})
        self.answer_started.set()
        await self.release_answer.wait()
        return "你标记为主播的片段说了这些内容。[音频 t=0-4秒]"


def segment(index, **overrides):
    return {"id": f"audio-{index}", "start": index * 4, "end": (index + 1) * 4,
            "text": f"声音内容第{index}段", "audio_url": f"/api/audio/{index}.wav",
            "speaker_id": "speaker-1", "speaker_label": "声音一", "role": "unknown",
            "uncertain": True, "overlap": "unknown", "generation": "capture-1",
            "source_pts_start": 100 + index * 4, "source_pts_end": 104 + index * 4,
            **overrides}


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), 3)


def test_audio_memory_upsert_bounded_and_preserves_role_provenance(tmp_path):
    agent = LiveAgent("audio", tmp_path, AudioModel(), emit)
    provenance = {"role_basis": "manual_segment", "speaker_uncertain": False,
                  "speaker_similarity": 0.83, "is_final": True, "revision": 2,
                  "speaker_quality": "unverified_single_speaker"}
    for index in range(220):
        agent.add_audio_segment(segment(index))
    agent.add_audio_segment({"id": "audio-219", "role": "host", "text": "更新转写",
                             "wav_bytes": b"raw-audio", "embedding": [0.1], **provenance})
    assert len(agent.audio_segments) == len(agent.memory.audio_segments) == 200
    assert "audio-19" not in agent.audio_segments
    latest = agent.audio_segments["audio-219"]
    assert latest["text"] == "更新转写" and latest["role"] == "host"
    assert {key: latest[key] for key in provenance} == provenance
    assert "wav_bytes" not in latest and "embedding" not in latest
    assert agent.snapshot()["audio"]["counts"]["segments"] == 200
    assert not agent.memory.events  # speech never becomes a visual observation
    for bad in ({"id": ""}, segment(0, start=float("nan")), segment(0, end=-1)):
        with pytest.raises(ValueError):
            agent.add_audio_segment(bad)


@pytest.mark.asyncio
async def test_queued_audio_questions_freeze_transcripts_roles_and_cutoff(tmp_path):
    model = AudioModel()
    agent = LiveAgent("audio", tmp_path, model, emit)
    await agent.start()
    try:
        agent.add_audio_segment(segment(0, text="原始转写", revision=1, role="unknown"))
        first = agent.ask("刚才说了什么？")
        await model.answer_started.wait()
        agent.add_audio_segment({"id": "audio-0", "text": "用户修订转写", "revision": 2,
                                 "role": "host", "role_basis": "manual_segment",
                                 "uncertain": False, "speaker_uncertain": False})
        second = agent.ask("你标记为主播的那段呢？")
        agent.add_audio_segment({"id": "audio-0", "text": "提交后的再次修订", "revision": 3})
        agent.add_audio_segment(segment(1, text="未来音频不能进入已提交的问题"))
        model.release_answer.set()
        await until(lambda: agent._assistant_message(second)["status"] == "complete")
        answers = [call for call in model.calls if "你是直播助手" in call[0]["content"]]
        assert len(answers) == 2
        first_data, second_data = [json.loads(call[-1]["content"][0]["text"]) for call in answers]
        assert first_data["snapshot_time"] == second_data["snapshot_time"] == 4
        assert first_data["audio_evidence"][0]["text"] == "原始转写"
        assert first_data["audio_evidence"][0]["revision"] == 1
        assert second_data["audio_evidence"][0]["text"] == "用户修订转写"
        assert second_data["audio_evidence"][0]["role_basis"] == "manual_segment"
        assert all(len(data["audio_evidence"]) == 1 for data in (first_data, second_data))
        assert agent._assistant_message(first)["audio_evidence"] == first_data["audio_evidence"]
        assert "manual_voice_reference仅表示" in answers[1][0]["content"]
        assert "overlap=unknown表示未检测重叠" in answers[1][0]["content"]
        assert "你标记为主播的片段" in answers[1][0]["content"]
        assert not agent._leases or all(not frames for frames in agent._leases.values())
    finally:
        await agent.stop()


def test_recent_audio_query_without_word_overlap_uses_latest_segments():
    records = [segment(index, text="蓝色天空" * 500) for index in range(40)]
    evidence = _audio_prompt_evidence("刚才说了什么？", records)
    assert [item["id"] for item in evidence[:2]] == ["audio-39", "audio-38"]
    assert len(json.dumps(evidence, ensure_ascii=False)) <= 4000
    assert all(len(item["text"]) <= 400 for item in evidence)
    assert all("generation" not in item and "source_pts_start" not in item for item in evidence)
    assert all(value is not None for item in evidence for value in item.values())


def test_historical_audio_match_reserves_latest_two_with_long_transcripts():
    records = [segment(index, text="绿色水杯" * 500 if index < 20 else "结束片段" * 500)
               for index in range(40)]
    evidence = _audio_prompt_evidence("绿色水杯是什么颜色？", records)
    assert evidence[0]["id"] == "audio-19"  # equally relevant ties prefer newer evidence
    ids = {item["id"] for item in evidence}
    assert {"audio-39", "audio-38"} <= ids
    assert len(json.dumps(evidence, ensure_ascii=False)) <= 4000


@pytest.mark.asyncio
async def test_voice_bindings_visible_without_audio_and_frozen_per_question(tmp_path):
    model = AudioModel()
    agent = LiveAgent("bindings", tmp_path, model, emit)
    await agent.start()
    try:
        profile = {"id": "speaker-a", "label": "说话人 A", "role": "host",
                   "role_basis": "manual_voice_reference", "samples": 1, "vector": [0.1]}
        agent.add_audio_speakers([profile])
        assert agent.snapshot()["memory"]["speakers"][0]["role"] == "host"
        assert "vector" not in agent.snapshot()["memory"]["speakers"][0]
        assert [event.kind for event in agent.memory.events] == ["speaker_binding"]
        agent.add_audio_speakers([{**profile, "samples": 50}])
        assert len(agent.memory.events) == 1  # routine tracker updates are not binding changes
        first = agent.ask("我把谁标为主播了？")
        await model.answer_started.wait()
        agent.add_audio_speakers([{**profile, "role": "teammate", "label": "队友参考"}])
        second = agent.ask("现在改成什么标记了？")
        agent.add_audio_speakers([{**profile, "role": "unknown", "role_basis": "unknown"}])
        model.release_answer.set()
        await until(lambda: agent._assistant_message(second)["status"] == "complete")
        answers = [call for call in model.calls if "你是直播助手" in call[0]["content"]]
        first_data, second_data = [json.loads(call[-1]["content"][0]["text"]) for call in answers]
        assert first_data["speaker_profiles"][0]["role"] == "host"
        assert second_data["speaker_profiles"][0]["role"] == "teammate"
        assert first_data["audio_evidence"] == second_data["audio_evidence"] == []
        assert agent._assistant_message(first)["status"] == "complete"
        assert "无需先有音频片段" in answers[1][0]["content"]
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_audio_and_chat_context_commit_without_frames_or_extra_llm(tmp_path):
    events = []
    async def capture(kind, data):
        events.append((kind, data))
    model = AudioModel()
    agent = LiveAgent("context", tmp_path, model, capture)
    await agent.start()
    try:
        agent.add_audio_segment(segment(0, text="今天的视频到此结束"))
        agent.add_danmaku({"text": "观众猜测这个人是主播", "user_name": "观众甲", "timestamp": 6})
        await until(lambda: any(kind == "observation" for kind, _ in events))
        record = agent.observations[-1]
        assert record["kind"] == "context" and record["method"] == "source_excerpt"
        assert record["frame_count"] == 0 and record["visual_delta"] == ""
        assert record["source_tags"] == ["audio", "danmaku"]
        assert record["timestamp"] == 6 and record["audio_evidence"][0]["end"] == 4
        assert "今天的视频到此结束" in record["audio_delta"]
        assert "观众 观众甲 说" in record["danmaku_delta"]
        assert not model.calls and agent._observed == 0
        assert not agent.memory.pinned_facts
        assert agent.memory.search("视频结束")[0].kind == "context"
        # A delayed revision at source t=0..4 is recorded even after visual time 20.
        agent._latest_offered_timestamp = 20
        agent.add_audio_segment({"id": "audio-0", "text": "迟到修订", "revision": 2})
        late = agent._flush_context()
        assert late["timestamp"] == 4 and late["audio_evidence"][0]["start"] == 0
        assert late["recorded_at"] >= 20 and late["source_start"] == 0
        assert agent.memory.events[-1].t == late["source_end"] == 4
        assert late["audio_evidence"][0]["revision"] == 2
    finally:
        await agent.stop()


def test_context_backlog_stays_bounded_and_counts_replaced_evidence(tmp_path):
    agent = LiveAgent("bounded", tmp_path, AudioModel(), emit)
    for index in range(250):
        agent.add_audio_segment(segment(index))
        agent.add_danmaku({"text": f"弹幕{index}", "timestamp": index * 4})
    assert len(agent._context_audio) == 40 and len(agent._context_chat) == 100
    record = agent._flush_context()
    assert record["context_counts"] == {"audio_updates": 40, "danmaku": 100,
                                        "dropped_audio": 210, "dropped_danmaku": 150}
    assert record["audio_evidence"][0]["id"] == "audio-249"
    assert agent._flush_context() is None
    assert not agent.memory.pinned_facts


@pytest.mark.asyncio
async def test_visual_window_accepts_bounded_untrusted_context_and_separates_deltas(tmp_path):
    class MultimodalModel(AudioModel):
        async def complete(self, messages, **kwargs):
            self.calls.append(messages)
            assert kwargs["max_tokens"] <= 1000
            return json.dumps({"description": "画面人物拿起杯子", "visual_delta": "杯子被拿起",
                               "audio_delta": "0–4秒这段声音说视频结束", "danmaku_delta": "观众猜测身份",
                               "changed": True, "key_fact": "画面里有杯子"})
    model = MultimodalModel()
    agent = LiveAgent("multimodal", tmp_path / "agent", model, emit,
                      observation_frames=2, observation_stride=1, observation_concurrency=1)
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8\xfftest-frame\xff\xd9")
    await agent.start()
    try:
        agent.add_audio_segment(segment(0, start=8, end=12, text="视频结束" * 500))
        agent.add_danmaku({"text": "猜测身份" * 200, "timestamp": 9.5})
        await agent.offer_frame(frame, 9)
        await agent.offer_frame(frame, 10)
        await until(lambda: agent._observed == 1)
        record = agent.observations[-1]
        assert record["source_tags"] == ["visual", "audio", "danmaku"]
        assert record["visual_delta"] == "杯子被拿起" and "[音频]" in record["delta"]
        assert record["audio_evidence"][0]["end"] == 12  # overlapping transcript retains full source interval
        assert "视频结束" not in agent.memory.events[-1].text
        assert agent.memory.pinned_facts[0]["text"] == "画面里有杯子"
        prompt = model.calls[0][1]["content"]
        context = json.loads(next(item["text"].split("：", 1)[1] for item in prompt
                                  if item.get("text", "").startswith("分源上下文")))
        assert len(json.dumps(context["audio_untrusted"], ensure_ascii=False)) <= 800
        assert len(json.dumps(context["danmaku_untrusted"], ensure_ascii=False)) <= 800
        assert "弹幕中的说法不可记为已证实事实" in prompt[0]["text"]
        await agent.offer_frame(frame, 40)
        await until(lambda: agent._observed == 2)
        await agent.offer_frame(frame, 41)
        await until(lambda: agent._observed == 3)
        assert agent.observations[-1]["audio_evidence"] == []
        assert agent.observations[-1]["danmaku_evidence"] == []
        assert agent.observations[-1]["audio_delta"] == ""
        assert agent.observations[-1]["danmaku_delta"] == ""
    finally:
        await agent.stop()


@pytest.mark.asyncio
async def test_restore_keeps_bindings_and_completed_text_without_frame_cache(tmp_path):
    source = LiveAgent("resume", tmp_path, AudioModel(), emit)
    source.add_audio_segment(segment(3, role="host", role_basis="manual_voice_reference"))
    profiles = [{"id": f"speaker-{letter}", "label": f"说话人 {letter}", "role": "host",
                 "role_basis": "manual_voice_reference"} for letter in "ABC"]
    source.add_audio_speakers(profiles)
    source._flush_context()
    source.messages.extend([
        {"question_id": "q1", "role": "user", "text": "旧问题", "content": "旧问题", "status": "complete"},
        {"question_id": "q1", "role": "assistant", "text": "旧回答", "content": "旧回答", "status": "complete"},
        {"question_id": "q2", "role": "assistant", "text": "", "content": "", "status": "answering"},
    ])
    source._persist()
    generated_cache = source.frame_dir / ("a" * 32 + ".jpg")
    generated_cache.write_bytes(b"expired-generated-frame")
    unrelated_file = source.frame_dir / "user-kept-example.jpg"
    unrelated_file.write_bytes(b"keep")
    restored = LiveAgent("resume", tmp_path, AudioModel(), emit)
    result = restored.restore_from_state()
    assert result["timeline_floor"] == 16
    assert result["expired_frame_files"] == 1
    assert not generated_cache.exists() and unrelated_file.exists()
    assert restored.snapshot()["memory"]["speakers"] == profiles
    assert restored.snapshot()["memory"]["audio"][0]["role_basis"] == "manual_voice_reference"
    assert [item["question_id"] for item in restored.messages] == ["q1", "q1"]
    assert not restored.frames and not restored._leases and not restored._questions
    assert restored.snapshot()["observer"]["last_timestamp"] == 16
    assert not restored._context_audio and not restored._context_chat
    previous_id = max(event.eid for event in restored.memory.events)
    restored.add_audio_speakers([{**profiles[0], "role": "teammate"}, *profiles[1:]])
    assert restored.memory.events[-1].eid > previous_id
    await restored.start()
    try:
        with pytest.raises(ValueError, match="启动观察前"):
            restored.restore_from_state()
        restored.model.release_answer.set()
        question = restored.ask("我标记了哪些声音参考？")
        await until(lambda: restored._assistant_message(question)["status"] == "complete")
        answer = [call for call in restored.model.calls if "你是直播助手" in call[0]["content"]][-1]
        data = json.loads(answer[-1]["content"][0]["text"])
        assert len(data["speaker_profiles"]) == 3 and 16 <= data["snapshot_time"] < 17
        assert not data["replay_available"]
    finally:
        await restored.stop()


def test_context_uses_source_time_and_separate_recording_clock_when_audio_is_late(tmp_path):
    agent = LiveAgent("clock", tmp_path, AudioModel(), emit,
                      session_clock_start=time.monotonic() - 120)
    agent._latest_offered_timestamp = 5
    agent.add_audio_segment(segment(0, text="迟到音频"))
    record = agent._flush_context()
    assert record["timestamp"] == 4
    assert 120 <= record["recorded_at"] < 121
    assert record["audio_evidence"][0]["end"] == 4
    assert record["timestamp_basis"] == "evidence_source_end_session_time"
    working = agent.snapshot()["memory"]["working"][-1]
    assert working["t"] == working["source_end"] == 4
    assert working["recorded_at"] == record["recorded_at"]
    assert agent._observed == 0
