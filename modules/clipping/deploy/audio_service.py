"""Clipper-owned long-form ASR beside its stronger live audio service.

The local live service preserves the original authentication, short-request
route, VAD and speaker behavior. The extra
endpoint rereads source audio with a larger model and a forced aligner; it
never silently substitutes the live model when that pass fails.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
import importlib.util
import io
import math
import os
from pathlib import Path
import sys
import unicodedata
import wave

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
LIVE_SERVICE = ROOT / "deploy/live_audio_service.py"
SAMPLE_RATE = 16000
MAX_SECONDS = 600
MAX_WAV_BYTES = MAX_SECONDS * SAMPLE_RATE * 2 + 4096
MAX_ENCODED_BYTES = 4 * ((MAX_WAV_BYTES + 2) // 3)
MAX_OFFLINE_BODY = MAX_ENCODED_BYTES + 32_768
MODEL_NAME = "Qwen3-ASR-1.7B"
ALIGNER_NAME = "Qwen3-ForcedAligner-0.6B"


class TranscribeClipRequest(BaseModel):
    audio_base64: str = Field(min_length=1, max_length=MAX_ENCODED_BYTES)
    context: str = Field(default="", max_length=2000)
    language: Literal["Chinese"] = "Chinese"


def decode_clip(encoded: str) -> tuple[bytes, float]:
    """Validate a bounded PCM recording without loading torch or a model."""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Expected base64 PCM WAV") from None
    if len(raw) > MAX_WAV_BYTES:
        raise ValueError("Clip audio exceeds the size limit")
    try:
        with wave.open(io.BytesIO(raw), "rb") as reader:
            if (reader.getnchannels() != 1 or reader.getsampwidth() != 2
                    or reader.getframerate() != SAMPLE_RATE or reader.getcomptype() != "NONE"):
                raise ValueError("Expected mono 16-bit PCM WAV at 16000 Hz")
            frames = reader.getnframes()
            if not 3200 <= frames <= MAX_SECONDS * SAMPLE_RATE:
                raise ValueError("Clip audio duration must be between 0.2 and 600 seconds")
            if len(reader.readframes(frames)) != frames * 2:
                raise ValueError("Truncated PCM WAV")
    except (wave.Error, EOFError):
        raise ValueError("Invalid PCM WAV") from None
    return raw, frames / SAMPLE_RATE


def _letters(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if unicodedata.category(c)[0] in {"L", "N"})


def aligned_words(result, duration):
    """Reject incomplete/invalid alignment instead of inventing equal timing.

The aligner quantizes its boundaries, so a zero-length token is valid when
adjacent tokens share a timestamp. Its timestamp is preserved for the caption
grouper; a wholly zero-length transcript is not accepted.
"""
    text = str(result.text or "").strip()
    stamps = getattr(result, "time_stamps", None)
    items = list(stamps.items) if stamps is not None else []
    words, previous = [], -1.0
    for item in items:
        word = str(item.text or "").strip()
        start, end = float(item.start_time), float(item.end_time)
        if (not word or not math.isfinite(start + end) or start < 0 or end < start
                or start < previous - 0.001 or end > duration + 0.08):
            raise ValueError("Invalid forced alignment")
        # End-of-recording rounding is bounded to one aligner time step.
        start, end = min(start, duration), min(end, duration)
        words.append({"start": start, "end": end, "text": word})
        previous = start
    if text and (not words or not any(w["end"] > w["start"] for w in words)):
        raise ValueError("Forced alignment is missing")
    if _letters(text) != _letters("".join(w["text"] for w in words)):
        raise ValueError("Forced alignment does not cover the transcript")
    return words


class OfflineEngine:
    def __init__(self, model_dir, aligner_dir):
        import torch
        from qwen_asr import Qwen3ASRModel
        from silero_vad import load_silero_vad, get_speech_timestamps
        # Silero's recurrent state is mutable. Offline and live calls must
        # never share that model when their GPU engines run concurrently.
        self.vad = load_silero_vad(onnx=False)
        self.speech_timestamps = get_speech_timestamps
        self.model = Qwen3ASRModel.from_pretrained(
            str(model_dir), forced_aligner=str(aligner_dir),
            forced_aligner_kwargs={"dtype": torch.bfloat16,
                                   "device_map": "cuda:0", "attn_implementation": "sdpa"},
            dtype=torch.bfloat16, device_map="cuda:0", attn_implementation="sdpa",
            max_inference_batch_size=1, max_new_tokens=4096)

    def transcribe(self, raw, duration, context, language):
        import numpy as np
        import torch
        with wave.open(io.BytesIO(raw), "rb") as reader:
            audio = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
        audio = audio.astype(np.float32) / 32768.0
        # Keep silence as silence. This is a speech-presence guard, not source
        # separation or speaker attribution; game voices remain audible speech.
        regions = self.speech_timestamps(
            torch.from_numpy(audio.copy()), self.vad, sampling_rate=SAMPLE_RATE,
            threshold=0.55, min_speech_duration_ms=250, min_silence_duration_ms=180,
            speech_pad_ms=80, return_seconds=False)
        if not regions:
            return {"text": "", "words": [], "speech": False}
        result = self.model.transcribe(audio=(audio, SAMPLE_RATE), context=context,
                                       language=language, return_time_stamps=True)[0]
        torch.cuda.synchronize()
        words = aligned_words(result, duration)
        return {"text": str(result.text or "").strip(), "words": words, "speech": bool(words)}


def load_legacy_service():
    spec = importlib.util.spec_from_file_location("clipper_live_audio", LIVE_SERVICE)
    module = importlib.util.module_from_spec(spec)
    # Pydantic resolves postponed request-model annotations through __module__.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BoundedBody:
    """Keep the old request limit; allow large bodies only for clip ASR."""
    def __init__(self, app, legacy):
        self.app, self.legacy = app, legacy

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        offline = scope.get("path") == "/transcribe_clip"
        if offline:
            # Authenticate before accumulating a potentially 25 MB body.
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"").decode("latin1")
            try:
                self.legacy.authorize(authorization)
            except HTTPException as exc:
                return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
        limit = MAX_OFFLINE_BODY if offline else self.legacy.MAX_BODY
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                return await JSONResponse({"detail": "Audio request exceeds the size limit"},
                                          status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


def create_app(legacy=None, *, engine_factory=OfflineEngine, model_dir=None, aligner_dir=None):
    legacy = legacy or load_legacy_service()
    model_dir = Path(model_dir or os.environ.get("AUDIO_OFFLINE_MODEL_DIR", ROOT / "data/models" / MODEL_NAME))
    aligner_dir = Path(aligner_dir or os.environ.get("AUDIO_ALIGNER_MODEL_DIR", ROOT / "data/models" / ALIGNER_NAME))
    state = {"engine": None, "loading": False, "last_error": None}
    offline_lock = asyncio.Lock()

    def configured():
        return (model_dir / "config.json").is_file() and (aligner_dir / "config.json").is_file()

    @asynccontextmanager
    async def lifespan(app):
        async with legacy.lifespan(app):
            yield
        state["engine"] = None

    app = FastAPI(title="Xiaodan Live and Clip Audio", lifespan=lifespan)
    app.add_middleware(BoundedBody, legacy=legacy)
    # Reuse the exact live route object so validation and locking have one
    # implementation, independently of the long-form transcription endpoint.
    app.router.routes.extend(route for route in legacy.app.router.routes
                             if getattr(route, "path", None) == "/analyze")

    @app.get("/health")
    async def health(authorization: str | None = Header(default=None)):
        result = await legacy.health(authorization)
        return {**result, "offline_configured": configured(),
                "offline_ready": state["engine"] is not None,
                "offline_loading": state["loading"], "offline_error": state["last_error"],
                "offline_busy": offline_lock.locked(),
                "offline_model": MODEL_NAME, "offline_aligner": ALIGNER_NAME,
                "offline_max_audio_seconds": MAX_SECONDS,
                "offline_timing_basis": "forced_alignment", "offline_service_version": 1}

    def infer(raw, duration, context, language):
        if state["engine"] is None:
            state["loading"] = True
            try:
                state["engine"] = engine_factory(model_dir, aligner_dir)
            finally:
                state["loading"] = False
        return state["engine"].transcribe(raw, duration, context, language)

    @app.post("/transcribe_clip")
    async def transcribe_clip(request: TranscribeClipRequest,
                              authorization: str | None = Header(default=None)):
        legacy.authorize(authorization)
        if legacy.ENGINE is None:
            raise HTTPException(503, "Models are still loading")
        if not configured():
            raise HTTPException(503, "Offline subtitle models are not configured")
        try:
            raw, duration = decode_clip(request.audio_base64)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        # This lock protects only the independent offline model and VAD.
        # Long clip inference must not make every live request return 429.
        try:
            await asyncio.wait_for(offline_lock.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            raise HTTPException(429, "Audio inference busy; retry clip transcription",
                                headers={"Retry-After": "2"}) from None
        task = asyncio.create_task(asyncio.to_thread(infer, raw, duration,
                                                     request.context, request.language))
        try:
            result = await asyncio.shield(task)
            state["last_error"] = None
            return {**result, "model": MODEL_NAME, "aligner": ALIGNER_NAME,
                    "duration": duration, "timing_basis": "forced_alignment"}
        except asyncio.CancelledError:
            # A disconnected caller cannot release the GPU while its worker is
            # still running. Preserve cancellation even if that worker fails.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()  # Retrieve a worker error without exposing it.
            raise
        except Exception as exc:
            state["last_error"] = "offline_transcription_failed"
            print("Offline transcription failure:", type(exc).__name__, flush=True)
            raise HTTPException(500, "Offline transcription or alignment failed") from None
        finally:
            offline_lock.release()

    return app


if __name__ == "__main__":
    import uvicorn
    old_service = load_legacy_service()
    uvicorn.run(create_app(old_service), host=old_service.HOST, port=old_service.PORT,
                log_level="info", access_log=False)
