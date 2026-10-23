import asyncio
import base64
import json
import math
from pathlib import Path
import wave

import httpx
import pytest

from clipper import subtitle_review as review
from clipper.render import write_subtitles


ANCHOR = {"start": 10., "end": 14.}
ORIGINAL = [{"kind": "transcript", "start": 10., "end": 14., "text": "旧的错误字幕"}]
CONFIG = {"base_url": "https://audio.example", "api_key": "secret-test-api-key"}


class Settings:
    ffmpeg = "fixture-ffmpeg"

    def audio_config(self):
        return dict(CONFIG)


def response():
    return {"text": "你好，世界！", "words": [
        {"start": .12, "end": .2, "text": "你"},
        {"start": .4, "end": .5, "text": "好"},
        {"start": 1.5, "end": 1.8, "text": "世"},
        {"start": 2.1, "end": 2.2, "text": "界"},
    ], "model": review.MODEL, "aligner": review.ALIGNER,
        "duration": 4., "timing_basis": "forced_alignment"}


def wav_file(path, *, silent=False, seconds=4):
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes((b"\x00\x00" if silent else b"\x01\x01") * int(seconds * 16000))


async def extraction(args, timeout):
    assert args[args.index("-af") + 1] == "atrim=start=2.500000:duration=4.000000,asetpts=PTS-STARTPTS"
    assert args[args.index("-fs") + 1] == str(review.MAX_WAV_BYTES)
    assert timeout <= 600
    wav_file(args[-1])


async def run(folder, client, **overrides):
    args = dict(settings=Settings(), folder=folder, events=ORIGINAL, anchor=ANCHOR,
                width=1280, height=720, offset=2.5, has_audio=True, enabled=True,
                source={"room": {"uname": "阿梓"}, "title": "模型生成的错误标题"},
                command=extraction, client=client)
    args.update(overrides)
    return await review.review_subtitles(**args)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["request", "retry_wait"])
async def test_whole_review_deadline_cancels_request_and_retry_wait(tmp_path, monkeypatch, blocked_phase):
    wait_for = asyncio.wait_for
    cancelled, deadlines, calls = [], [], []

    async def short_deadline(awaitable, timeout):
        deadlines.append(timeout)
        return await wait_for(awaitable, .01)

    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(blocked_phase)

    async def handler(request):
        calls.append(request)
        if blocked_phase == "request":
            await blocked()
        return httpx.Response(429, headers={"Retry-After": "10"})

    async def retry_sleep(delay):
        assert delay == 10
        await blocked()

    monkeypatch.setattr(review.asyncio, "wait_for", short_deadline)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, report = await run(tmp_path, client, sleep=retry_sleep)
    assert deadlines == [225]
    assert cancelled == [blocked_phase] and len(calls) == 1
    assert events is ORIGINAL and report["reason"] == "request_timeout"
    assert not (tmp_path / "subtitle-review-cache.json").exists()


@pytest.mark.asyncio
async def test_full_clip_replaces_caption_events_with_real_word_boundaries(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/transcribe_clip"
        assert request.headers["authorization"] == "Bearer " + CONFIG["api_key"]
        assert payload["context"] == "B站直播。主播名：阿梓。"
        assert "旧的错误字幕" not in request.content.decode()
        assert "模型生成的错误标题" not in request.content.decode()
        assert payload["language"] == "Chinese"
        assert base64.b64decode(payload["audio_base64"])[:4] == b"RIFF"
        return httpx.Response(200, json=response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, report = await run(tmp_path, client)
    assert len(requests) == 1
    assert report["status"] == "complete" and report["applied"]
    assert report["timing_basis"] == "forced_alignment"
    assert report["original_subtitles"][0]["text"] == ORIGINAL[0]["text"]
    assert [(e["text"], e["start"], e["end"]) for e in events] == [
        ("你好，", 10.12, 10.5), ("世界！", 11.5, 12.2)]
    assert all(e["data"]["speaker"] == "unknown" for e in events)
    assert ORIGINAL[0]["text"] == "旧的错误字幕"
    write_subtitles(tmp_path, events, ANCHOR, 1280, 720)
    srt = (tmp_path / "subtitles.srt").read_text()
    assert "00:00:00,120 --> 00:00:00,500\n你好，" in srt
    assert "00:00:01,500 --> 00:00:02,200\n世界！" in srt
    assert CONFIG["api_key"] not in (tmp_path / "subtitle-review.json").read_text()


def test_zero_duration_and_overlapping_words_keep_text_and_real_interval(tmp_path):
    value = response()
    value["words"] = [
        {"start": .1, "end": .1, "text": "你"},
        {"start": .1, "end": .45, "text": "好"},
        {"start": .4, "end": .7, "text": "世"},
        {"start": .7, "end": .8, "text": "界"},
    ]
    events = review.caption_events(review.validate_response(value, 4), 10, 13)
    assert "".join(e["text"] for e in events) == "你好，世界！"
    assert events[0]["start"] == 10.1 and events[-1]["end"] == 10.8
    assert all(e["end"] > e["start"] for e in events)


def test_indivisible_word_wraps_without_distributing_its_timing(tmp_path):
    text = "这是同一个对齐词中的全部文字绝不能按字数重新分配时间"
    value = {**response(), "text": text,
             "words": [{"start": .35, "end": 2.75, "text": text}]}
    events = review.caption_events(review.validate_response(value, 4), 10, 13)
    write_subtitles(tmp_path, events, ANCHOR, 720, 1280)
    srt = (tmp_path / "subtitles.srt").read_text()
    assert srt.count("-->") == 1
    assert "00:00:00,350 --> 00:00:02,750" in srt
    assert "".join(srt.splitlines()[2:]) == text
    ass = (tmp_path / "subtitles.ass").read_text()
    assert ass.count("Dialogue:") == 1 and r"\N" in ass


@pytest.mark.parametrize(("mutation", "reason"), [
    (lambda r: r["words"].pop(), "word_text_mismatch"),
    (lambda r: r.update(words=[]), "missing_word_alignment"),
    (lambda r: r["words"][1].update(start=.05), "invalid_word_timing"),
    (lambda r: r["words"][0].update(start=-.1), "invalid_word_timing"),
    (lambda r: r["words"][0].update(start=math.nan), "invalid_word_timing"),
    (lambda r: r["words"][0].update(end=math.inf), "invalid_word_timing"),
    (lambda r: r["words"][0].update(start=True), "invalid_word_timing"),
    (lambda r: r["words"][-1].update(end=4.01), "invalid_word_timing"),
    (lambda r: r["words"][0].update(text=""), "invalid_word_text"),
    (lambda r: r.update(model="old-model"), "unexpected_model"),
    (lambda r: r.update(timing_basis="estimated"), "missing_forced_alignment"),
    (lambda r: r.update(duration=3.5), "audio_duration_mismatch"),
    (lambda r: [w.update(end=w["start"]) for w in r["words"]], "zero_duration_alignment"),
])
def test_bad_alignment_is_rejected(mutation, reason):
    value = response()
    mutation(value)
    with pytest.raises(review.ReviewError, match=reason):
        review.validate_response(value, 4)


@pytest.mark.asyncio
async def test_incomplete_result_preserves_old_captions_with_explicit_warning(tmp_path):
    value = response()
    value["words"].pop()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=value))) as client:
        events, report = await run(tmp_path, client)
    assert events is ORIGINAL
    assert report["status"] == "fallback" and not report["applied"]
    assert report["reason"] == "word_text_mismatch" and "回退原字幕" in report["warning"]
    assert not (tmp_path / "subtitle-review-cache.json").exists()


@pytest.mark.asyncio
async def test_cache_reuses_verified_audio_but_changes_with_context_audio_or_version(tmp_path, monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=response())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await run(tmp_path, client)
        _, cached = await run(tmp_path, client)
        assert cached["cache_hit"] and len(calls) == 1
        changed_source = {"streamer": "七海Nana7mi"}
        await run(tmp_path, client, source=changed_source)
        assert len(calls) == 2
        async def changed_audio(args, timeout):
            await extraction(args, timeout)
            path = Path(args[-1])
            content = bytearray(path.read_bytes())
            content[-1] ^= 1
            path.write_bytes(content)
        await run(tmp_path, client, source=changed_source, command=changed_audio)
        assert len(calls) == 3
        monkeypatch.setattr(review, "REVIEW_VERSION", review.REVIEW_VERSION + 1)
        await run(tmp_path, client, source=changed_source, command=changed_audio)
        assert len(calls) == 4
        monkeypatch.setattr(review, "MODEL", "fixture-next-model")
        await run(tmp_path, client, source=changed_source, command=changed_audio)
        assert len(calls) == 5
    cache = (tmp_path / "subtitle-review-cache.json").read_text()
    assert CONFIG["api_key"] not in cache and CONFIG["base_url"] not in cache


@pytest.mark.asyncio
@pytest.mark.parametrize(("options", "status"), [
    ({"has_audio": False}, "no_audio"), ({"enabled": False}, "disabled"),
])
async def test_disabled_or_no_audio_skips_extraction_and_request(tmp_path, options, status):
    async def unexpected(*args, **kwargs):
        pytest.fail("Disabled/no-audio review must not extract or request")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        events, report = await run(tmp_path, client, command=unexpected, **options)
    assert events is ORIGINAL and report["status"] == status
    assert not (tmp_path / "review.wav").exists()


@pytest.mark.asyncio
async def test_missing_configuration_is_explicit_and_preserves_old_captions(tmp_path):
    settings = Settings()
    settings.audio_config = lambda: {}
    events, report = await run(tmp_path, None, settings=settings)
    assert events is ORIGINAL and report["status"] == "not_configured"
    assert "未复听" in report["warning"] and not (tmp_path / "review.wav").exists()


@pytest.mark.asyncio
async def test_digital_silence_is_not_sent_to_asr(tmp_path):
    async def silent(args, timeout):
        wav_file(args[-1], silent=True)
    def unexpected(request):
        pytest.fail("Digital silence must not reach ASR")
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        events, report = await run(tmp_path, client, command=silent)
    assert events == [] and report["status"] == "silent_audio"
    assert report["applied"] and report["timing_basis"] == "digital_silence"


@pytest.mark.asyncio
async def test_empty_model_result_is_recorded_without_silently_erasing_speech(tmp_path):
    value = {**response(), "text": "", "words": [], "speech": False}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=value))) as client:
        events, report = await run(tmp_path, client)
    assert events is ORIGINAL and report["status"] == "empty_result"
    assert not report["applied"] and "保留原字幕" in report["warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("eventual_success", [False, True])
async def test_busy_retry_is_bounded(tmp_path, eventual_success):
    calls, delays = [], []
    def handler(request):
        calls.append(1)
        if eventual_success and len(calls) == 3:
            return httpx.Response(200, json=response())
        return httpx.Response(429, headers={"Retry-After": "999"}, text=CONFIG["api_key"])
    async def sleep(delay):
        delays.append(delay)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _, report = await run(tmp_path, client, sleep=sleep)
    assert len(calls) == 3 and delays == [10, 10]
    assert report["status"] == ("complete" if eventual_success else "fallback")
    assert CONFIG["api_key"] not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "unauthorized", "large_response"])
async def test_request_failures_are_bounded_and_sanitized(tmp_path, failure, monkeypatch):
    monkeypatch.setattr(review, "MAX_RESPONSE_BYTES", 1024)
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(CONFIG["api_key"], request=request)
        if failure == "unauthorized":
            return httpx.Response(401, text=CONFIG["api_key"])
        return httpx.Response(200, content=b"x" * 1025)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, report = await run(tmp_path, client)
    assert events is ORIGINAL and report["status"] == "fallback"
    assert report["reason"] == {"timeout": "request_timeout", "unauthorized": "http_401",
                                "large_response": "response_too_large"}[failure]
    assert CONFIG["api_key"] not in json.dumps(report)


@pytest.mark.asyncio
async def test_extraction_failure_never_exposes_raw_errors(tmp_path):
    async def broken(args, timeout):
        raise RuntimeError("private endpoint " + CONFIG["api_key"])
    events, report = await run(tmp_path, None, command=broken)
    assert events is ORIGINAL and report["reason"] == "local_review_failed"
    assert CONFIG["api_key"] not in (tmp_path / "subtitle-review.json").read_text()
