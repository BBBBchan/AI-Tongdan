import asyncio
import base64
from io import BytesIO
import json
import math
import wave

import httpx
import pytest

from app.audio import AudioPipeline, VoiceTracker, wav_info


def wav(seconds=4):
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * int(seconds * 16000))
    return output.getvalue()


def vector(index):
    return [1.0 if i == index else 0.0 for i in range(16)]


def chunk(start=0):
    return {"wav_bytes": wav(), "start": start, "end": start + 4,
            "source_pts_start": 100 + start, "generation": "g1"}


def result(index=0):
    return {"language": "Chinese", "segments": [{"start": .5, "end": 3.5,
            "text": "真实转写在此由测试替身提供", "embedding": vector(index)}]}


def test_voice_assignment_does_not_invent_host_or_force_ambiguous_matches():
    tracker = VoiceTracker()
    assert tracker.assign(vector(0), 3)[0] is None
    first, _, uncertain = tracker.assign(vector(0), 3)
    assert not uncertain and tracker.profiles[first]["role"] == "unknown"
    assert tracker.assign(vector(0), 3) == (first, 1.0, False)
    assert tracker.assign(vector(1), 3)[0] is None
    second, _, _ = tracker.assign(vector(1), 3)
    assert second != first
    mixed = [math.sqrt(.5), math.sqrt(.5)] + [0] * 14
    assert tracker.assign(mixed, 3)[0] is None
    assert tracker.assign(vector(0), .4)[0] is None
    assert tracker.assign(vector(0), 4, eligible=False)[0] is None
    assert tracker.assign([float("nan")] * 16, 4)[0] is None
    assert "vector" not in json.dumps(tracker.public())


def nearby(cosine, axis=1):
    return [cosine if i == 0 else math.sqrt(1 - cosine * cosine) if i == axis else 0 for i in range(16)]


def test_repeated_confirmation_tolerant_matching_and_one_outlier_does_not_spawn():
    tracker = VoiceTracker()
    assert tracker.assign(vector(0), 3, timestamp=3, evidence_id="one")[0] is None
    # Replaying the same piece is not independent confirmation.
    assert tracker.assign(vector(0), 3, timestamp=3, evidence_id="one")[0] is None
    first = tracker.assign(vector(0), 3, timestamp=7, evidence_id="two")[0]
    assert first and len(tracker.profiles) == 1
    assert tracker.assign(nearby(.67), 3, timestamp=11)[0] == first
    matched, score, uncertain = tracker.assign(nearby(.60), 3, timestamp=15)
    assert matched == first and uncertain and score == .6
    assert len(tracker.references(tracker.profiles[first])) == 1  # no weak-reference drift
    assert tracker.assign(vector(2), 3, timestamp=19)[0] is None
    assert len(tracker.profiles) == 1
    assert tracker.assign(vector(0), 3, timestamp=23)[0] == first
    # A clearly separate voice does become a distinct group with repeated evidence.
    second = tracker.assign(vector(2), 3, timestamp=27)[0]
    assert second != first and len(tracker.profiles) == 2
    ambiguous = [math.sqrt(.5), 0, math.sqrt(.5)] + [0] * 13
    assert tracker.assign(ambiguous, 3, timestamp=31)[0] is None


def test_pending_candidates_expire_and_stay_bounded():
    tracker = VoiceTracker()
    assert tracker.assign(vector(0), 3, timestamp=0)[0] is None
    assert tracker.assign(vector(0), 3, timestamp=60)[0] is None
    for i in range(1, 15):
        tracker.assign(vector(i), 3, timestamp=60 + i)
    assert len(tracker.candidates) <= 8 and not tracker.profiles


@pytest.mark.asyncio
async def test_relabel_precedence_evidence_retention_and_no_biometric_leak(tmp_path):
    emitted, memory = [], {}
    async def emit(kind, data):
        emitted.append((kind, json.dumps(data)))
    pipeline = AudioPipeline("session", tmp_path, lambda: {}, emit,
                             lambda s: memory.update({s["id"]: s}), max_segments=2)
    await pipeline._accept(chunk(), result(), 4)
    first = pipeline.snapshot()["segments"][0]
    assert first["start"] == .5 and first["end"] == 3.5
    assert first["source_pts_start"] == 100.5
    assert first["overlap"] == "unknown" and first["role"] == "unknown"
    assert wav_info((tmp_path / (first["id"] + ".wav")).read_bytes()) == 3
    await pipeline.label_segment(first["id"], "background")
    assert first["speaker_id"] is None
    await pipeline._accept(chunk(4), result(), 4)
    first = pipeline.snapshot()["segments"][0]
    assert first["speaker_id"] is not None and first["revision"] >= 3
    await pipeline.label_speaker(first["speaker_id"], "host", "已标记声音")
    assert memory[first["id"]]["role"] == "background"
    assert memory[first["id"]]["role_basis"] == "manual_segment"
    later = pipeline.snapshot()["segments"][-1]
    assert later["role"] == "host" and later["role_basis"] == "manual_voice_reference"
    await pipeline._accept(chunk(8), result(), 4)
    assert len(list(tmp_path.glob("*.wav"))) == 2
    assert not (tmp_path / (first["id"] + ".wav")).exists()
    with pytest.raises(KeyError):
        await pipeline.label_segment(first["id"], "unknown")
    public = json.dumps(pipeline.snapshot()) + json.dumps(emitted) + (tmp_path / "state.json").read_text()
    assert "embedding" not in public and "vector" not in public
    private = tmp_path / "state.private.json"
    assert private.stat().st_mode & 0o777 == 0o600
    saved = json.loads(private.read_text())
    assert len(saved["segment_vectors"]) == 2
    await pipeline.stop()


@pytest.mark.asyncio
async def test_manual_merge_upserts_history_preserves_clip_override_and_keeps_references(tmp_path):
    memory = {}
    async def emit(*_):
        pass
    pipeline = AudioPipeline("merge", tmp_path, lambda: {}, emit, lambda s: memory.update({s["id"]: s}))
    for i in range(4):
        await pipeline._accept(chunk(i * 4), result(0 if i < 2 else 1), 4)
    first, second = list(pipeline.tracker.profiles)
    await pipeline.label_speaker(first, "host", "已确认声音")
    await pipeline.label_speaker(second, "host")
    overridden = pipeline.segments[-1]["id"]
    await pipeline.label_segment(overridden, "background")
    revisions = {s["id"]: s["revision"] for s in pipeline.segments}
    snapshot = await pipeline.merge_speakers(second, first)
    assert len(snapshot["speakers"]) == 1 and snapshot["speakers"][0]["reference_count"] == 2
    for segment in snapshot["segments"]:
        assert segment["speaker_id"] == first
        assert segment["speaker_label"] == "已确认声音"
        assert segment["revision"] > revisions[segment["id"]]
        assert memory[segment["id"]] == segment
    assert memory[overridden]["role"] == "background"
    assert memory[overridden]["role_basis"] == "manual_segment"
    # Both manually joined voice variants match the chosen group afterwards.
    assert pipeline.tracker.assign(vector(0), 3)[0] == first
    assert pipeline.tracker.assign(vector(1), 3)[0] == first
    assert "vector" not in json.dumps(snapshot)
    await pipeline.stop()


@pytest.mark.asyncio
async def test_merge_rejects_known_role_conflict_without_mutation(tmp_path):
    async def emit(*_):
        pass
    pipeline = AudioPipeline("merge", tmp_path, lambda: {}, emit, lambda _: None)
    for i in range(4):
        await pipeline._accept(chunk(i * 4), result(i // 2), 4)
    first, second = list(pipeline.tracker.profiles)
    await pipeline.label_speaker(first, "host")
    await pipeline.label_speaker(second, "background")
    before = pipeline.snapshot()
    with pytest.raises(ValueError, match="冲突"):
        await pipeline.merge_speakers(second, first)
    assert pipeline.snapshot() == before
    await pipeline.stop()


@pytest.mark.asyncio
async def test_private_restore_preserves_user_roles_and_matching_without_public_vectors(tmp_path):
    async def emit(*_):
        pass
    pipeline = AudioPipeline("restore", tmp_path, lambda: {}, emit, lambda _: None)
    await pipeline._accept(chunk(), result(), 4)
    await pipeline._accept(chunk(4), result(), 4)
    speaker = next(iter(pipeline.tracker.profiles))
    await pipeline.label_speaker(speaker, "host", "用户指定")
    await pipeline.label_segment(pipeline.segments[0]["id"], "singing")
    await pipeline.stop()
    updates = {}
    restored = AudioPipeline("restore", tmp_path, lambda: {}, emit, lambda s: updates.update({s["id"]: s}))
    assert restored.restore_private_state()["restored"]
    assert len(updates) == 2
    assert restored.tracker.public()[0]["label"] == "用户指定"
    assert restored.tracker.public()[0]["reference_ready"] is True
    assert restored.segments[0]["role_basis"] == "manual_segment" and restored.segments[0]["role"] == "singing"
    await restored._accept(chunk(8), {"segments": [{"start": .5, "end": 3.5,
        "text": "another sample", "embedding": nearby(.68)}]}, 4)
    assert restored.segments[-1]["speaker_id"] == speaker
    assert restored.segments[-1]["role"] == "host"
    assert "vector" not in json.dumps(restored.snapshot())
    await restored.stop()


@pytest.mark.asyncio
async def test_legacy_public_restore_bootstraps_references_without_rewriting_transcript(tmp_path):
    async def emit(*_):
        pass
    pipeline = AudioPipeline("legacy", tmp_path, lambda: {}, emit, lambda _: None)
    for i in range(3):
        await pipeline._accept(chunk(i * 4), result(), 4)
    speaker = next(iter(pipeline.tracker.profiles))
    await pipeline.label_speaker(speaker, "host", "保留用户标注")
    before = list(pipeline.snapshot()["segments"])
    await pipeline.stop()
    (tmp_path / "state.private.json").unlink()
    restored = AudioPipeline("legacy", tmp_path, lambda: {"base_url": "http://audio"}, emit, lambda _: None)
    assert restored.restore_private_state()["speakers"] == 1
    assert restored.tracker.public()[0]["reference_ready"] is False
    assert restored.tracker.assign(vector(1), 3)[0] is None  # do not invent duplicates before recovery
    async def handler(request):
        return httpx.Response(200, json=result())
    await restored.client.aclose()
    restored.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    outcome = await restored.rebuild_missing_references()
    assert outcome["recovered_speakers"] == 1 and outcome["attempted_requests"] <= 3
    assert restored.tracker.public()[0]["reference_ready"]
    assert restored.snapshot()["segments"] == before
    assert restored.tracker.assign(vector(0), 3)[0] == speaker
    assert restored.tracker.public()[0]["label"] == "保留用户标注"
    await restored.stop()


@pytest.mark.asyncio
async def test_unrecoverable_legacy_profile_does_not_block_new_voices_forever(tmp_path):
    async def emit(*_):
        pass
    pipeline = AudioPipeline("legacy-missing", tmp_path, lambda: {}, emit, lambda _: None)
    old = "speaker_oldreference"
    pipeline.tracker.restore({"profiles": [{"id": old, "label": "用户旧声音", "role": "host", "samples": 2}]})
    assert pipeline.tracker.assign(vector(1), 3)[0] is None
    assert pipeline.tracker.waiting_for_references
    result = await pipeline.rebuild_missing_references()
    assert result["reference_missing"] == 1 and not pipeline.tracker.waiting_for_references
    assert pipeline.tracker.assign(vector(1), 3)[0] is None
    new = pipeline.tracker.assign(vector(1), 3)[0]
    assert new is not None and new != old
    assert pipeline.tracker.profiles[old]["role"] == "host"
    assert pipeline.tracker.profiles[new]["role"] == "unknown"
    await pipeline.stop()


@pytest.mark.asyncio
async def test_audio_queue_is_bounded_and_stop_cancels_inference(tmp_path):
    entered, unblock = asyncio.Event(), asyncio.Event()
    async def handler(request):
        assert request.headers["authorization"] == "Bearer private-test-key"
        assert wav_info(base64.b64decode(json.loads(request.content)["audio_b64"])) == 4
        entered.set()
        await unblock.wait()
        return httpx.Response(200, json=result())
    async def emit(*_):
        pass
    pipeline = AudioPipeline("s", tmp_path,
        lambda: {"base_url": "http://audio", "api_key": "private-test-key"}, emit, lambda _: None)
    await pipeline.client.aclose()
    pipeline.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await pipeline.start()
    await pipeline.offer(chunk())
    await asyncio.wait_for(entered.wait(), 1)
    for i in range(1, 10):
        await pipeline.offer(chunk(i * 4))
    assert pipeline.queue.qsize() == 2 and pipeline.dropped == 7
    assert "private-test-key" not in json.dumps(pipeline.snapshot())
    await asyncio.wait_for(pipeline.stop(), 1)
    assert pipeline.queue.empty() and pipeline.task is None


@pytest.mark.asyncio
async def test_failed_audio_request_is_visible_and_next_chunk_recovers(tmp_path):
    attempts = 0
    progress = []
    async def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, json=result())
    async def emit(*_):
        pass
    pipeline = AudioPipeline("s", tmp_path, lambda: {"base_url": "http://audio"}, emit, lambda _: None,
                             on_progress=progress.append)
    await pipeline.client.aclose()
    pipeline.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await pipeline.start()
    await pipeline.offer(chunk())
    await asyncio.wait_for(pipeline.queue.join(), 1)
    assert pipeline.snapshot()["failed_segments"] == 1
    assert pipeline.snapshot()["status"] == "error"
    assert not progress and pipeline.processed_until is None
    await pipeline.offer(chunk(4))
    await asyncio.wait_for(pipeline.queue.join(), 1)
    assert pipeline.snapshot()["processed_segments"] == 1
    assert pipeline.snapshot()["status"] == "live"
    assert pipeline.snapshot()["segments"]
    assert len(progress) == 1 and progress[0]["start"] == 4 and progress[0]["end"] == 8
    await pipeline.stop()


@pytest.mark.asyncio
async def test_silent_chunk_reports_full_source_coverage_without_transcript(tmp_path):
    progress = []
    async def emit(*_):
        pass
    pipeline = AudioPipeline("silent", tmp_path, lambda: {}, emit, lambda _: None,
                             on_progress=progress.append)
    try:
        await pipeline._accept(chunk(20), {"segments": []}, 4)
        assert pipeline.snapshot()["segments"] == []
        assert progress == [{"start": 20, "end": 24, "status": "complete", "generation": "g1",
                             "discontinuity": False, "transcribed_segments": 0}]
        assert pipeline.snapshot()["processed_until"] == 24
    finally:
        await pipeline.stop()
