"""Exercise the owned live API and provenance without GPU dependencies."""
import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import wave

import httpx
import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTH = {"Authorization": "Bearer live-audio-unit-test-key-only"}


def wav(seconds=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(bytes(round(seconds * 16000) * 2))
    return output.getvalue()


@pytest.fixture
def setup(monkeypatch, tmp_path):
    # Import the production module with tiny dependency doubles. All audio
    # decoding, VAD grouping, metadata, HTTP validation and logging below are
    # the real implementation; no GPU installation or real keys are needed.
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("AUDIO_API_KEY", AUTH["Authorization"].removeprefix("Bearer "))
    for name in ("AUDIO_MODEL_DIR", "AUDIO_STATE_DIR", "AUDIO_API_KEY_FILE",
                 "SPEAKER_MODEL_FILE", "AUDIO_SERVICE_PORT"):
        monkeypatch.delenv(name, raising=False)
    torch = SimpleNamespace(
        inference_mode=lambda: lambda function: function,
        cuda=SimpleNamespace(is_available=lambda: True, is_bf16_supported=lambda: True,
                             synchronize=lambda: None),
        set_num_threads=lambda _: None, load=lambda *_args, **_kwargs: {},
        bfloat16="test-bfloat16", from_numpy=lambda value: value, dot=np.dot)
    monkeypatch.setitem(sys.modules, "torch", torch)
    for name in ("torchaudio", "torchaudio.compliance", "torchaudio.compliance.kaldi",
                 "speakerlab", "speakerlab.models", "speakerlab.models.campplus",
                 "speakerlab.models.campplus.DTDNN"):
        module = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, _, attribute = name.rpartition(".")
            setattr(sys.modules[parent], attribute, module)

    class Speaker:
        def __init__(self, **_):
            pass

        def load_state_dict(self, _):
            pass

        def eval(self):
            pass

    sys.modules["speakerlab.models.campplus.DTDNN"].CAMPPlus = Speaker
    calls = []

    def transcribe(**kwargs):
        calls.append(("transcribe", kwargs))
        return [SimpleNamespace(text="这是测试语音。", language="Chinese")
                for _ in kwargs["audio"]]

    def load(*args, **kwargs):
        calls.append(("load", args, kwargs))
        return SimpleNamespace(transcribe=transcribe)

    monkeypatch.setitem(sys.modules, "qwen_asr", SimpleNamespace(
        Qwen3ASRModel=SimpleNamespace(from_pretrained=load)))
    regions = [{"start": 0, "end": 32000}]
    monkeypatch.setitem(sys.modules, "silero_vad", SimpleNamespace(
        load_silero_vad=lambda **_: object(),
        get_speech_timestamps=lambda *_args, **_kwargs: list(regions)))
    spec = importlib.util.spec_from_file_location(
        "clipper_owned_live_audio_test", ROOT / "deploy/live_audio_service.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    default_state = module.STATE_DIR
    module.STATE_DIR = tmp_path

    def engine():
        value = module.AudioEngine()
        value.voice_embedding = lambda _: np.array([1.0] + [0.0] * 191)
        module.ENGINE = value
        return value

    return SimpleNamespace(service=module, engine=engine, calls=calls, regions=regions,
                           state=tmp_path, default_state=default_state)


def test_default_ownership_model_and_checkpoint_identity(setup, monkeypatch):
    module = setup.service
    assert module.ASR_MODEL == ROOT / "data/models/Qwen3-ASR-1.7B"
    assert setup.default_state == ROOT / "data/audio-runtime"
    assert module.KEY_FILE == ROOT / "data/secrets/audio_api_key.txt"
    assert module.PORT == 8921
    assert module.SPEAKER_MODEL == ROOT.parents[1] / "data/models/campplus/campplus_cn_common.bin"
    assert module.VENDOR == ROOT.parents[1] / "deploy/audio/vendor"
    engine = setup.engine()
    _, args, kwargs = setup.calls[0]
    assert args == (str(module.ASR_MODEL),)
    assert kwargs["attn_implementation"] == "sdpa"
    assert kwargs["max_inference_batch_size"] == 3
    monkeypatch.setattr(module, "ASR_MODEL", ROOT / "data/models/Qwen3-ASR-0.6B")
    assert engine.asr_model_name == "Qwen3-ASR-1.7B"


@pytest.mark.asyncio
async def test_health_and_speech_share_loaded_identity_with_operational_log(setup, monkeypatch):
    module = setup.service
    engine = setup.engine()
    monkeypatch.setattr(module, "ASR_MODEL", ROOT / "data/models/changed-after-load")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                base_url="http://test") as client:
        health = (await client.get("/health", headers=AUTH)).json()
        assert health["model"] == "Qwen3-ASR-1.7B" and health["ready"]
        assert health["max_audio_seconds"] == 15
        assert health["source_separation"] is False
        response = await client.post("/analyze", headers=AUTH,
            json={"audio_b64": base64.b64encode(wav()).decode(), "language": "Chinese"})
        assert response.status_code == 200, response.text
        data = response.json()
    assert data["metadata"]["asr_model"] == health["model"]
    assert data["metadata"]["timestamp_kind"] == "vad_region"
    assert data["metadata"]["word_timestamps"] is False
    assert data["segments"][0]["speaker_eligible"] is True
    assert len(data["embedding"]) == 192
    assert data["segments"][0]["start"] == 0 and data["segments"][0]["end"] == 2
    assert engine.calls == 1
    record = json.loads((setup.state / "requests.jsonl").read_text())
    assert record["asr_model"] == health["model"]
    assert not {"text", "embedding", "audio_b64"}.intersection(record)
    assert data["text"] not in json.dumps(record, ensure_ascii=False)


@pytest.mark.asyncio
async def test_unready_auth_silence_and_live_body_boundaries(setup):
    module = setup.service
    payload = {"audio_b64": base64.b64encode(wav()).decode()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.post("/analyze", json=payload)).status_code == 401
        unready = (await client.get("/health", headers=AUTH)).json()
        assert unready["ready"] is False and unready["model"] == "Qwen3-ASR-1.7B"
        assert (await client.post("/analyze", json=payload, headers=AUTH)).status_code == 503
        setup.engine()
        setup.regions.clear()
        response = await client.post("/analyze", json=payload, headers=AUTH)
        assert response.status_code == 200
        data = response.json()
        assert data["text"] == "" and not data["speech"] and data["segments"] == []
        assert data["embedding"] is None
        assert data["metadata"]["asr_model"] == "Qwen3-ASR-1.7B"
        assert not any(call[0] == "transcribe" for call in setup.calls)
        assert (await client.post("/analyze", json={"audio_b64": "invalid!"},
                                  headers=AUTH)).status_code == 422
        assert (await client.post("/analyze", content=b" " * 1_500_001,
                                  headers=AUTH)).status_code == 413
        too_long = {"audio_b64": base64.b64encode(wav(15.1)).decode()}
        assert (await client.post("/analyze", json=too_long, headers=AUTH)).status_code == 422


@pytest.mark.asyncio
async def test_busy_live_inference_is_bounded(setup):
    module = setup.service
    setup.engine()
    await module.LOCK.acquire()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                    base_url="http://test") as client:
            response = await client.post("/analyze", headers=AUTH,
                json={"audio_b64": base64.b64encode(wav()).decode()})
        assert response.status_code == 429 and response.headers["Retry-After"] == "1"
        assert not any(call[0] == "transcribe" for call in setup.calls)
    finally:
        module.LOCK.release()


@pytest.mark.asyncio
async def test_wrapper_loads_owned_service_and_preserves_live_route(setup, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "clipper_owned_audio_wrapper_test", ROOT / "deploy/audio_service.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    assert wrapper.LIVE_SERVICE == ROOT / "deploy/live_audio_service.py"
    monkeypatch.setitem(sys.modules, "clipper_live_audio", None)
    loaded = wrapper.load_legacy_service()
    assert Path(loaded.__file__) == wrapper.LIVE_SERVICE
    loaded.ENGINE = setup.engine()
    app = wrapper.create_app(loaded)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        health = (await client.get("/health", headers=AUTH)).json()
    assert health["model"] == health["offline_model"] == "Qwen3-ASR-1.7B"
    assert health["offline_aligner"] == "Qwen3-ForcedAligner-0.6B"
    route = next(route for route in loaded.app.routes if route.path == "/analyze")
    assert next(route for route in app.routes if route.path == "/analyze") is route
