"""Long-form ASR contract and live-service isolation, without GPU or secrets."""
import asyncio
import base64
from contextlib import asynccontextmanager
import importlib.util
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import wave

from fastapi import FastAPI, Header, HTTPException
import httpx
import pytest


SPEC = importlib.util.spec_from_file_location(
    "clipper_offline_audio_test", Path(__file__).resolve().parents[1] / "deploy/audio_service.py")
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)
AUTH = {"Authorization": "Bearer test-only-audio-key"}


def wav(seconds=1, *, rate=16000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.setframerate(rate)
        writer.writeframes(bytes(round(seconds * rate) * channels * width))
    return output.getvalue()


def payload(seconds=1):
    return {"audio_base64": base64.b64encode(wav(seconds)).decode(),
            "context": "阿梓、小担", "language": "Chinese"}


def legacy_service():
    legacy = SimpleNamespace(app=FastAPI(), ENGINE=object(), MAX_BODY=1_500_000,
                             LOCK=asyncio.Lock())

    def authorize(value):
        if value != AUTH["Authorization"]:
            raise HTTPException(401, "Authentication required")

    async def health(authorization=None):
        authorize(authorization)
        return {"ready": True, "model": "Qwen3-ASR-0.6B", "speaker_model": "CAM++",
                "max_audio_seconds": 15}

    @legacy.app.post("/analyze")
    async def analyze(authorization: str | None = Header(default=None)):
        authorize(authorization)
        async with legacy.LOCK:
            return {"text": "实时接口", "segments": [], "embedding": None}

    @asynccontextmanager
    async def lifespan(app):
        yield

    legacy.authorize, legacy.health, legacy.lifespan = authorize, health, lifespan
    return legacy


@pytest.fixture
def setup(tmp_path):
    model, aligner = tmp_path / "model", tmp_path / "aligner"
    for directory in (model, aligner):
        directory.mkdir()
        (directory / "config.json").write_text("{}")
    legacy = legacy_service()
    calls = []

    class Engine:
        def __init__(self, model_dir, aligner_dir):
            calls.append(("load", model_dir, aligner_dir))

        def transcribe(self, raw, duration, context, language):
            calls.append(("transcribe", duration, context, language))
            return {"text": "你好。", "words": [{"start": .1, "end": .6, "text": "你好"}],
                    "speech": True}

    def app(factory=Engine):
        return service.create_app(legacy, engine_factory=factory, model_dir=model, aligner_dir=aligner)

    return SimpleNamespace(app=app, legacy=legacy, calls=calls, model=model, aligner=aligner)


@pytest.mark.parametrize("seconds", [.2, 1, 180, 600])
def test_pcm_duration_boundaries(seconds):
    raw = wav(seconds)
    decoded, duration = service.decode_clip(base64.b64encode(raw).decode())
    assert decoded == raw and duration == seconds


@pytest.mark.parametrize("kwargs", [
    {"seconds": .199}, {"seconds": 600.01}, {"rate": 8000}, {"channels": 2}, {"width": 1},
])
def test_pcm_rejects_invalid_duration_or_format(kwargs):
    with pytest.raises(ValueError):
        service.decode_clip(base64.b64encode(wav(**kwargs)).decode())


@pytest.mark.parametrize("data", ["!not-base64!", base64.b64encode(b"bad wav").decode(),
    base64.b64encode(wav()[:-100]).decode()])
def test_pcm_rejects_malformed_or_truncated_recordings(data):
    with pytest.raises(ValueError):
        service.decode_clip(data)


def alignment(text="你好。", spans=((.1, .3, "你"), (.3, .7, "好"))):
    return SimpleNamespace(text=text, time_stamps=SimpleNamespace(items=[
        SimpleNamespace(start_time=a, end_time=b, text=t) for a, b, t in spans]))


def test_alignment_preserves_times_punctuation_coverage_and_zero_tokens():
    words = service.aligned_words(alignment(), 1)
    assert words == [{"start": .1, "end": .3, "text": "你"},
                     {"start": .3, "end": .7, "text": "好"}]
    assert service.aligned_words(alignment(spans=((.1, .1, "你"), (.1, .7, "好"))), 1)[0]["end"] == .1
    assert service.aligned_words(alignment(spans=((.1, .3, "你"), (.3, 1.04, "好"))), 1)[1]["end"] == 1


@pytest.mark.parametrize("result", [
    alignment(spans=((float("nan"), .3, "你"), (.3, .7, "好"))),
    alignment(spans=((-1, .3, "你"), (.3, .7, "好"))),
    alignment(spans=((.3, .1, "你"), (.3, .7, "好"))),
    alignment(spans=((.4, .6, "你"), (.3, .7, "好"))),
    alignment(spans=((.1, .3, "你"), (.3, 1.1, "好"))),
    alignment(spans=((.1, .3, "你"),)),
    alignment(spans=((.1, .1, "你"), (.3, .3, "好"))),
    SimpleNamespace(text="你好", time_stamps=None),
])
def test_alignment_rejects_missing_text_and_invalid_times(result):
    with pytest.raises(ValueError):
        service.aligned_words(result, 1)


def test_real_model_constructor_isolated_vad_and_official_aligner(monkeypatch, tmp_path):
    calls, vad = [], object()
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(bfloat16="mock-bfloat16"))
    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(Qwen3ASRModel=SimpleNamespace(
        from_pretrained=lambda *args, **kwargs: calls.append((args, kwargs)) or object())))
    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(
        load_silero_vad=lambda **_: vad, get_speech_timestamps=object()))
    engine = service.OfflineEngine(tmp_path / "model", tmp_path / "aligner")
    assert engine.vad is vad
    _, kwargs = calls[0]
    assert kwargs["max_new_tokens"] == 4096 and kwargs["max_inference_batch_size"] == 1
    assert kwargs["forced_aligner"] == str(tmp_path / "aligner")
    assert kwargs["forced_aligner_kwargs"]["device_map"] == "cuda:0"
    assert kwargs["attn_implementation"] == "sdpa"


@pytest.mark.parametrize("speech", [False, True])
def test_offline_pass_keeps_whole_audio_and_silence_guard(monkeypatch, speech):
    calls = []
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        from_numpy=lambda x: x, cuda=SimpleNamespace(synchronize=lambda: None)))
    engine = service.OfflineEngine.__new__(service.OfflineEngine)
    engine.vad = object()
    engine.speech_timestamps = lambda *_args, **_kwargs: [{"start": 1, "end": 9000}] if speech else []
    engine.model = SimpleNamespace(transcribe=lambda **kwargs: calls.append(kwargs) or [alignment()])
    result = engine.transcribe(wav(240), 240, "阿梓、小担", "Chinese")
    if speech:
        assert len(calls) == 1
        assert len(calls[0]["audio"][0]) == 240 * 16000
        assert calls[0]["return_time_stamps"] is True
        assert calls[0]["context"] == "阿梓、小担"
        assert result["text"] == "你好。" and len(result["words"]) == 2
    else:
        assert calls == [] and result == {"text": "", "words": [], "speech": False}


@pytest.mark.asyncio
async def test_auth_health_lazyload_reuse_and_live_contract(setup):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app()), base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.post("/transcribe_clip", content=b"invalid json")).status_code == 401
        before = (await client.get("/health", headers=AUTH)).json()
        assert before["offline_configured"] and not before["offline_ready"]
        assert before["model"] == "Qwen3-ASR-0.6B" and before["max_audio_seconds"] == 15
        assert setup.calls == []
        result = await client.post("/transcribe_clip", headers=AUTH, json=payload())
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["model"] == "Qwen3-ASR-1.7B" and data["aligner"] == "Qwen3-ForcedAligner-0.6B"
        assert data["duration"] == 1 and data["timing_basis"] == "forced_alignment"
        assert data["words"][0]["start"] == .1
        await client.post("/transcribe_clip", headers=AUTH, json=payload())
        assert [call[0] for call in setup.calls].count("load") == 1
        after = (await client.get("/health", headers=AUTH)).json()
        assert after["offline_ready"] and not after["offline_busy"]
        assert str(setup.model) not in str(after)
        assert (await client.post("/analyze", headers=AUTH, json={})).json()["text"] == "实时接口"


@pytest.mark.asyncio
async def test_old_body_limit_and_long_clip_body_allowance(setup):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app()), base_url="http://test") as client:
        assert (await client.post("/analyze", headers=AUTH, content=b" " * 1_500_001)).status_code == 413
        assert (await client.post("/transcribe_clip", headers=AUTH, json=payload(60))).status_code == 200
        too_large = b" " * (service.MAX_OFFLINE_BODY + 1)
        assert (await client.post("/transcribe_clip", headers=AUTH, content=too_large)).status_code == 413


@pytest.mark.asyncio
async def test_context_language_and_unconfigured_model_errors(setup):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app()), base_url="http://test") as client:
        assert (await client.post("/transcribe_clip", headers=AUTH,
                                  json={**payload(), "context": "字" * 2001})).status_code == 422
        assert (await client.post("/transcribe_clip", headers=AUTH,
                                  json={**payload(), "language": "invalid"})).status_code == 422
        (setup.model / "config.json").unlink()
        response = await client.post("/transcribe_clip", headers=AUTH, json=payload())
        assert response.status_code == 503 and setup.calls == []


@pytest.mark.asyncio
async def test_model_failure_is_explicit_and_does_not_fall_back(setup):
    class Broken:
        def __init__(self, *_):
            raise RuntimeError("private path and credential must never reach HTTP")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app(Broken)), base_url="http://test") as client:
        response = await client.post("/transcribe_clip", headers=AUTH, json=payload())
        assert response.status_code == 500
        assert "private path" not in response.text
        health = (await client.get("/health", headers=AUTH)).json()
        assert health["offline_error"] == "offline_transcription_failed"
        assert not health["offline_ready"] and not health["offline_busy"]


@pytest.mark.asyncio
async def test_offline_does_not_take_live_lock_and_rejects_duplicate_work(setup):
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def __init__(self, *_):
            pass

        def transcribe(self, *_):
            entered.set()
            assert release.wait(5)
            return {"text": "", "words": [], "speech": False}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app(Slow)), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/transcribe_clip", headers=AUTH, json=payload()))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert not setup.legacy.LOCK.locked()
            live = await asyncio.wait_for(client.post("/analyze", headers=AUTH, json={}), timeout=.5)
            assert live.status_code == 200
            second = await client.post("/transcribe_clip", headers=AUTH, json=payload())
            assert second.status_code == 429 and second.headers["retry-after"] == "2"
        finally:
            release.set()
        assert (await first).status_code == 200


@pytest.mark.asyncio
async def test_disconnected_client_keeps_offline_lock_until_worker_finishes(setup):
    entered, release = threading.Event(), threading.Event()

    class Slow:
        def __init__(self, *_):
            pass

        def transcribe(self, *_):
            entered.set()
            assert release.wait(5)
            return {"text": "", "words": [], "speech": False}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup.app(Slow)), base_url="http://test") as client:
        first = asyncio.create_task(client.post("/transcribe_clip", headers=AUTH, json=payload()))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            first.cancel()
            await asyncio.sleep(.01)
            assert not first.done()
            first.cancel()
            await asyncio.sleep(.01)
            assert not first.done()
            assert (await client.post("/transcribe_clip", headers=AUTH, json=payload())).status_code == 429
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not (await client.get("/health", headers=AUTH)).json()["offline_busy"]
