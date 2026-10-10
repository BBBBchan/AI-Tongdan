"""Authenticated short-chunk Qwen3-ASR, Silero VAD and CAM++ inference.

This service returns acoustic evidence, never a person's identity or inferred
microphone/source. VAD regions are not guaranteed to contain one speaker.
"""
from __future__ import annotations
import asyncio
import base64
import binascii
import hashlib
from contextlib import asynccontextmanager
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time
import wave

import numpy as np
import torch
import torchaudio.compliance.kaldi as kaldi
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STATE_DIR = Path(os.environ.get("AUDIO_STATE_DIR", str(ROOT / "data/deploy/audio"))).expanduser().resolve()
ASR_MODEL = Path(os.environ.get("AUDIO_MODEL_DIR", str(ROOT / "data/models/Qwen3-ASR-0.6B"))).expanduser().resolve()
SPEAKER_MODEL = Path(os.environ.get("SPEAKER_MODEL_FILE", str(ROOT / "data/models/campplus/campplus_cn_common.bin"))).expanduser().resolve()
sys.path.insert(0, str(HERE / 'vendor'))
from speakerlab.models.campplus.DTDNN import CAMPPlus

MAX_BODY = 1_500_000
MAX_SECONDS = 15
SAMPLE_RATE = 16000
PORT = int(os.environ.get('AUDIO_SERVICE_PORT', '8021'))
HOST = os.environ.get('AUDIO_HOST', '127.0.0.1')
KEY_FILE = Path(os.environ.get('AUDIO_API_KEY_FILE', str(ROOT / 'data/secrets/audio_api_key.txt'))).expanduser()
KEY = os.environ.get('AUDIO_API_KEY', '').strip() or (KEY_FILE.read_text().strip() if KEY_FILE.exists() else '')
ENGINE = None
LOCK = asyncio.Lock()

def atomic_json(path: Path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temp.replace(path)


class BoundedBody:
    """Bound the accumulated request before JSON/Pydantic decoding."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > MAX_BODY:
                return await JSONResponse({'detail': 'Audio request exceeds 1.5 MB'}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get('more_body'):
                break
        sent = False
        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        await self.app(scope, replay, send)


def decode_audio(encoded: str) -> np.ndarray:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('audio_b64 must contain standard base64 WAV bytes') from None
    try:
        with wave.open(io.BytesIO(raw), 'rb') as reader:
            if reader.getnchannels() != 1 or reader.getsampwidth() != 2 or reader.getframerate() != SAMPLE_RATE:
                raise ValueError('Expected mono 16-bit PCM WAV at 16000 Hz')
            frames = reader.getnframes()
            if not 3200 <= frames <= SAMPLE_RATE * MAX_SECONDS:
                raise ValueError('Audio duration must be between 0.2 and 15 seconds')
            data = reader.readframes(frames)
            if len(data) != frames * 2:
                raise ValueError('Truncated PCM WAV')
    except (wave.Error, EOFError):
        raise ValueError('Invalid PCM WAV') from None
    return np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0


class AudioEngine:
    def __init__(self):
        from qwen_asr import Qwen3ASRModel
        from silero_vad import load_silero_vad, get_speech_timestamps
        if not torch.cuda.is_available():
            raise RuntimeError('Audio GPU deployment requires a CUDA device')
        if not torch.cuda.is_bf16_supported():
            raise RuntimeError('This audio launcher requires a CUDA GPU with BF16 support')
        torch.set_num_threads(int(os.environ.get('AUDIO_CPU_THREADS', '4')))
        self.vad = load_silero_vad(onnx=False)
        self.speech_timestamps = get_speech_timestamps
        self.speaker = CAMPPlus(feat_dim=80, embedding_size=192)
        state = torch.load(SPEAKER_MODEL, map_location='cpu', weights_only=True)
        self.speaker.load_state_dict(state)
        self.speaker.eval()
        self.asr = Qwen3ASRModel.from_pretrained(
            str(ASR_MODEL), dtype=torch.bfloat16,
            device_map='cuda:0', attn_implementation='sdpa',
            max_inference_batch_size=3, max_new_tokens=160)
        self.calls = 0

    @torch.inference_mode()
    def voice_embedding(self, chunk):
        feature = kaldi.fbank(torch.from_numpy(chunk.copy()).unsqueeze(0),
            num_mel_bins=80, sample_frequency=SAMPLE_RATE, dither=0.0,
            use_energy=False)
        feature = feature - feature.mean(dim=0, keepdim=True)
        vector = self.speaker(feature.unsqueeze(0)).squeeze(0)
        return torch.nn.functional.normalize(vector, dim=0).cpu()

    @torch.inference_mode()
    def analyze(self, audio: np.ndarray, language: str | None):
        started = time.perf_counter()
        wave_tensor = torch.from_numpy(audio.copy())
        duration = len(audio) / SAMPLE_RATE
        regions = self.speech_timestamps(wave_tensor, self.vad, sampling_rate=SAMPLE_RATE,
            threshold=0.55, min_speech_duration_ms=250, min_silence_duration_ms=180,
            speech_pad_ms=80, max_speech_duration_s=5, return_seconds=False)
        vad_ms = (time.perf_counter() - started) * 1000
        speech_seconds = sum(r['end'] - r['start'] for r in regions) / SAMPLE_RATE
        response = {'text': '', 'language': '', 'duration': duration,
            'speech': bool(regions), 'speech_ratio': min(1.0, speech_seconds / duration),
            'speech_regions': [{'start': r['start'] / SAMPLE_RATE, 'end': r['end'] / SAMPLE_RATE} for r in regions],
            'segments': [], 'embedding': None, 'overlap': 'unknown',
            'metadata': {'asr_model': 'Qwen3-ASR-0.6B', 'speaker_model': 'CAM++',
                'vad_model': 'silero-vad-6.2.3', 'asr_backend': 'transformers-sdpa-bf16',
                'timestamp_kind': 'vad_region', 'word_timestamps': False,
                'speaker_embeddings_are_not_diarization': True,
                'voice_consistency_threshold': 0.65,
                'vad_ms': round(vad_ms, 2), 'asr_ms': 0.0, 'speaker_ms': 0.0}}
        if not regions:
            response['metadata']['total_ms'] = round((time.perf_counter() - started) * 1000, 2)
            return response
        # Merge shortest gaps if a highly fragmented clip has >3 regions. The
        # resulting group is explicitly not certified as a single-speaker turn.
        groups = [dict(region, merged_regions=1) for region in regions]
        while len(groups) > 3:
            at = min(range(len(groups) - 1), key=lambda i: groups[i + 1]['start'] - groups[i]['end'])
            groups[at]['end'] = groups[at + 1]['end']
            groups[at]['merged_regions'] += groups[at + 1]['merged_regions']
            groups.pop(at + 1)
        chunks = [audio[r['start']:r['end']] for r in groups]
        asr_started = time.perf_counter()
        results = self.asr.transcribe(audio=[(chunk, SAMPLE_RATE) for chunk in chunks], language=language)
        torch.cuda.synchronize()
        response['metadata']['asr_ms'] = round((time.perf_counter() - asr_started) * 1000, 2)
        speaker_started = time.perf_counter()
        for group, chunk, result in zip(groups, chunks, results):
            text = result.text.strip()
            span = len(chunk) / SAMPLE_RATE
            start, end = group['start'] / SAMPLE_RATE, group['end'] / SAMPLE_RATE
            voiced_seconds = sum(max(0, min(group['end'], r['end']) - max(group['start'], r['start'])) for r in regions) / SAMPLE_RATE
            segment = {'start': start, 'end': end, 'text': text, 'language': result.language,
                'speech_ratio': min(1.0, voiced_seconds / span), 'embedding': None,
                'speaker_eligible': False, 'speaker_quality': 'too_short',
                'consistency_cosine': None,
                'overlap': 'unknown', 'merged_regions': group['merged_regions']}
            # Short fragments are valid ASR evidence but poor enrollment data.
            if group['merged_regions'] > 1:
                segment['speaker_quality'] = 'merged_regions'
            if span >= 1.5 and voiced_seconds / span >= 0.65 and text and group['merged_regions'] == 1:
                consistent = True
                if span >= 3.0:
                    first = self.voice_embedding(chunk[:int(1.5 * SAMPLE_RATE)])
                    last = self.voice_embedding(chunk[-int(1.5 * SAMPLE_RATE):])
                    cosine = float(torch.dot(first, last))
                    segment['consistency_cosine'] = round(cosine, 6)
                    consistent = math.isfinite(cosine) and cosine >= 0.65
                if consistent:
                    vector = self.voice_embedding(chunk).tolist()
                    segment.update(embedding=vector, speaker_eligible=True,
                                   speaker_quality='unverified_single_speaker')
                else:
                    # An acoustic stability gate, NOT an overlap detector. It
                    # also rejects changed voices and some difficult single voices.
                    segment['speaker_quality'] = 'unstable_voice'
            response['segments'].append(segment)
        response['metadata']['speaker_ms'] = round((time.perf_counter() - speaker_started) * 1000, 2)
        response['text'] = ' '.join(s['text'] for s in response['segments'] if s['text'])
        response['language'] = ','.join(dict.fromkeys(s['language'] for s in response['segments'] if s['language']))
        if len(response['segments']) == 1:
            response['embedding'] = response['segments'][0]['embedding']
        response['metadata']['total_ms'] = round((time.perf_counter() - started) * 1000, 2)
        self.calls += 1
        log_path = STATE_DIR / 'requests.jsonl'
        if log_path.exists() and log_path.stat().st_size > 4 * 1024 * 1024:
            log_path.replace(STATE_DIR / 'requests.previous.jsonl')
        with log_path.open('a') as handle:
            # Operational timings only; no speech content, audio or embeddings.
            handle.write(json.dumps({'unix': time.time(), 'duration': duration,
                'segments': len(response['segments']), 'text_characters': len(response['text']),
                **response['metadata']}) + '\n')
        return response


@asynccontextmanager
async def lifespan(app):
    global ENGINE
    if len(KEY) < 20:
        raise RuntimeError('Missing private audio service API key')
    if not (ASR_MODEL / 'config.json').is_file() or not SPEAKER_MODEL.is_file():
        raise RuntimeError('Missing audio models; run deploy/download_models.py --component audio')
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    ENGINE = await asyncio.to_thread(AudioEngine)
    metadata = {'port': PORT,
        'started_unix': started, 'ready_unix': time.time(), 'startup_seconds': time.time() - started,
        'service_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'voice_consistency_threshold': 0.65,
        'gpu': torch.cuda.get_device_name(0), 'gpu_memory_allocated_mb': torch.cuda.memory_allocated() / 1024 ** 2,
        'versions': {name: importlib.metadata.version(name) for name in ['qwen-asr', 'torch', 'transformers', 'silero-vad']}}
    atomic_json(STATE_DIR / 'service.json', metadata)
    yield
    ENGINE = None


app = FastAPI(title='Stream Helper Audio Evidence', lifespan=lifespan)
app.add_middleware(BoundedBody)

class AnalyzeRequest(BaseModel):
    audio_b64: str = Field(min_length=1, max_length=1_400_000)
    language: str | None = Field(default=None, max_length=30)


def authorize(authorization):
    supplied = authorization.removeprefix('Bearer ') if authorization else ''
    if not KEY or not secrets.compare_digest(supplied, KEY):
        raise HTTPException(401, 'Authentication required')


@app.get('/health')
async def health(authorization: str | None = Header(default=None)):
    authorize(authorization)
    return {'ready': ENGINE is not None, 'model': 'Qwen3-ASR-0.6B', 'speaker_model': 'CAM++',
        'overlap_detection': False, 'source_separation': False, 'native_streaming': False,
        'max_audio_seconds': MAX_SECONDS, 'sample_rate': SAMPLE_RATE}


@app.post('/analyze')
async def analyze(request: AnalyzeRequest, authorization: str | None = Header(default=None)):
    authorize(authorization)
    if ENGINE is None:
        raise HTTPException(503, 'Models are still loading')
    try:
        audio = decode_audio(request.audio_b64)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    # One inference request at a time; reject overload instead of unbounded queues.
    try:
        await asyncio.wait_for(LOCK.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        raise HTTPException(429, 'Audio inference busy; retry the next bounded chunk', headers={'Retry-After': '1'}) from None
    task = asyncio.create_task(asyncio.to_thread(ENGINE.analyze, audio, request.language))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A disconnected client must not release the model lock mid-inference.
        await task
        raise
    except ValueError:
        raise HTTPException(422, 'Invalid language or audio content') from None
    except Exception as exc:
        print('Inference failure:', type(exc).__name__, flush=True)
        raise HTTPException(500, 'Audio inference failed; see service diagnostics') from None
    finally:
        LOCK.release()


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level='info', access_log=False)
