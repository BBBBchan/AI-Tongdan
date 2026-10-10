"""CPU unit checks for evidence bounds; no model inference is fabricated as QA."""
import asyncio
import base64
import io
import types
import unittest
from unittest.mock import patch
import wave

import numpy as np
import torch
from fastapi.testclient import TestClient
import service

def wav(seconds=2.0, rate=16000, channels=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(channels); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(b'\x00\x00' * int(rate * seconds * channels))
    return out.getvalue()

class BoundsTests(unittest.TestCase):
    def test_only_bounded_pcm16_mono_16k(self):
        self.assertEqual(service.decode_audio(base64.b64encode(wav()).decode()).shape, (32000,))
        for raw in [wav(16), wav(0.1), wav(rate=48000), wav(channels=2), b'invalid']:
            with self.assertRaises(ValueError):
                service.decode_audio(base64.b64encode(raw).decode())

    def test_invalid_base64(self):
        with self.assertRaises(ValueError):
            service.decode_audio('!!!')

    def test_http_auth_and_size(self):
        client = TestClient(service.app)
        self.assertEqual(client.get('/health').status_code, 401)
        self.assertEqual(client.post('/analyze', content=b'x' * (service.MAX_BODY + 1)).status_code, 413)

    def engine(self, regions):
        engine = service.AudioEngine.__new__(service.AudioEngine)
        engine.vad = object()
        engine.speech_timestamps = lambda *args, **kwargs: regions
        engine.calls = 0
        engine.asr = types.SimpleNamespace(transcribe=lambda audio, language: [types.SimpleNamespace(text='fixture stub', language='Chinese') for _ in audio])
        engine.speaker = lambda _: self.fail('Uncertain or short audio must not create an embedding')
        return engine

    def test_silence_never_calls_asr(self):
        engine = self.engine([])
        engine.asr.transcribe = lambda **kw: self.fail('Silence must skip ASR')
        result = engine.analyze(np.zeros(32000, dtype=np.float32), None)
        self.assertFalse(result['speech'])
        self.assertEqual(result['text'], '')
        self.assertIsNone(result['embedding'])

    @patch('service.torch.cuda.synchronize')
    def test_short_and_merged_regions_have_no_voiceprint(self, _):
        engine = self.engine([{'start': 0, 'end': 10000}])
        with patch('pathlib.Path.open', unittest.mock.mock_open()):
            result = engine.analyze(np.zeros(32000, dtype=np.float32), 'Chinese')
        self.assertFalse(result['segments'][0]['speaker_eligible'])
        self.assertEqual(result['overlap'], 'unknown')
        engine = self.engine([{'start': 0, 'end': 15000}, {'start': 15100, 'end': 31000},
                              {'start': 40000, 'end': 48000}, {'start': 60000, 'end': 68000}])
        with patch('pathlib.Path.open', unittest.mock.mock_open()):
            result = engine.analyze(np.zeros(80000, dtype=np.float32), 'Chinese')
        self.assertEqual(len(result['segments']), 3)
        merged = result['segments'][0]
        self.assertEqual(merged['merged_regions'], 2)
        self.assertFalse(merged['speaker_eligible'])
        self.assertIsNone(merged['embedding'])
        self.assertEqual(merged['speaker_quality'], 'merged_regions')

    @patch('service.torch.cuda.synchronize')
    def test_unstable_voice_never_becomes_one_speaker(self, _):
        engine = self.engine([{'start': 0, 'end': 64000}])
        vectors = iter([torch.tensor([1.0, 0.0]), torch.tensor([0.0, 1.0])])
        engine.voice_embedding = lambda _: next(vectors)
        with patch('pathlib.Path.open', unittest.mock.mock_open()):
            result = engine.analyze(np.zeros(64000, dtype=np.float32), 'Chinese')
        segment = result['segments'][0]
        self.assertEqual(segment['consistency_cosine'], 0.0)
        self.assertEqual(segment['speaker_quality'], 'unstable_voice')
        self.assertFalse(segment['speaker_eligible'])
        self.assertIsNone(segment['embedding'])
        self.assertIsNone(result['embedding'])
        self.assertEqual(result['overlap'], 'unknown')

    @patch('service.torch.cuda.synchronize')
    def test_consistent_voice_keeps_anonymous_embedding(self, _):
        engine = self.engine([{'start': 0, 'end': 64000}])
        engine.voice_embedding = lambda _: torch.tensor([1.0, 0.0])
        with patch('pathlib.Path.open', unittest.mock.mock_open()):
            result = engine.analyze(np.zeros(64000, dtype=np.float32), 'Chinese')
        segment = result['segments'][0]
        self.assertEqual(segment['consistency_cosine'], 1.0)
        self.assertTrue(segment['speaker_eligible'])
        self.assertEqual(segment['embedding'], [1.0, 0.0])
        self.assertEqual(result['overlap'], 'unknown')

if __name__ == '__main__':
    unittest.main()
