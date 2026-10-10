import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from app.media import MediaCapture
from app.config import Settings


@pytest.mark.asyncio
async def test_real_ffmpeg_http_input_produces_hls_and_jpeg(tmp_path):
    ffmpeg = shutil.which(Settings.from_env().ffmpeg_bin)
    if not ffmpeg:
        pytest.skip("FFmpeg not installed")
    fixture = tmp_path / "fixture.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "testsrc2=size=320x180:rate=10", "-t", "8", "-c:v", "libx264",
        "-threads", "2", "-g", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(fixture)], check=True)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    capture = MediaCapture(ffmpeg, tmp_path / "output", interval=2)
    frames = []

    async def collect(path, timestamp):
        with Image.open(path) as image:
            image.verify()
        frames.append((timestamp, path.read_bytes()))

    try:
        source = SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/fixture.mp4",
                                 request_headers={"User-Agent": "Test", "Cookie": "must-not-forward"})
        await asyncio.wait_for(capture.run(source, collect), 20)
        assert capture.ready
        assert len(frames) == 4
        assert len({content for _, content in frames}) == 4
        assert all(t >= 0 for t, _ in frames)
        assert capture.process.returncode == 0
        playlist = (capture.directory / "index.m3u8").read_text()
        segments = [line for line in playlist.splitlines() if line and not line.startswith("#")]
        assert len(segments) >= 3
        assert all((capture.directory / segment).stat().st_size > 0 for segment in segments)
    finally:
        await capture.stop()
        await asyncio.to_thread(server.shutdown)
        server.server_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_audio", [True, False])
async def test_real_hls_audio_uses_video_media_timeline_even_when_receipts_burst(tmp_path, with_audio):
    import io
    import time
    import wave
    ffmpeg = shutil.which(Settings.from_env().ffmpeg_bin)
    if not ffmpeg:
        pytest.skip("FFmpeg not installed")
    fixture = tmp_path / "av.mp4"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "testsrc2=size=320x180:rate=10"]
    if with_audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:a", "aac"]
    command += ["-t", "8", "-c:v", "libx264", "-threads", "2", "-g", "20",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(fixture)]
    subprocess.run(command, check=True)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    capture = MediaCapture(ffmpeg, tmp_path / "g2", interval=1,
                           timeline_floor=10, generation="g2")
    frames, audio = [], []
    async def collect_frame(path, timestamp):
        frames.append((timestamp, time.monotonic()))
    async def collect_audio(segment):
        audio.append(segment)
    try:
        source = SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/av.mp4", request_headers={})
        await asyncio.wait_for(capture.run(source, collect_frame, collect_audio), 20)
        assert len(frames) == 8
        assert [round(frames[i + 1][0] - frames[i][0], 6) for i in range(7)] == [1.0] * 7
        assert frames[-1][1] - frames[0][1] < 4  # VOD decoded quickly, yet timestamps span 7 seconds
        assert frames[0][0] >= 10
        assert capture.audio_stream_present is with_audio
        assert capture.audio_failures == 0
        if not with_audio:
            assert audio == []
            return
        assert len(audio) >= 2
        assert audio[0]["discontinuity"] is True
        for item in audio:
            with wave.open(io.BytesIO(item["wav_bytes"]), "rb") as wav:
                assert wav.getframerate() == 16000 and wav.getnchannels() == 1
                assert wav.getsampwidth() == 2
                duration = wav.getnframes() / 16000
            assert item["end"] - item["start"] == pytest.approx(duration, abs=1e-6)
            assert item["start"] == pytest.approx(
                capture.timeline_anchor + item["source_pts_start"] - capture._audio_origin_pts, abs=1e-6)
            assert item["generation"] == "g2"
            assert item["timestamp_basis"] == capture.timestamp_basis
            assert duration <= 4.001
        assert abs(audio[0]["start"] - frames[0][0]) < 0.1
        assert abs(audio[-1]["end"] - (frames[0][0] + 8)) < 0.1
        assert not capture._audio_processes and not capture._audio_tasks
    finally:
        await capture.stop()
        await asyncio.to_thread(server.shutdown)
        server.server_close()


@pytest.mark.asyncio
async def test_audio_callback_backpressure_is_bounded_and_marks_gap(tmp_path):
    import time
    capture = MediaCapture("ffmpeg", tmp_path / "g1", interval=1)
    capture.timeline_anchor = 0
    capture._audio_origin_pts = 0
    capture._anchor_ready.set()
    delivered = []
    gate = asyncio.Event()
    async def slow_audio(item):
        delivered.append(item)
        await gate.wait()
    task = asyncio.create_task(capture._deliver_audio(slow_audio))
    capture._audio_tasks = [task]
    pcm = b"\0\0" * (16000 * 4)
    try:
        await capture._enqueue_audio(pcm, 0)
        await asyncio.sleep(0)
        started = time.monotonic()
        for index in range(1, 7):
            capture._audio_discontinuity = False
            await capture._enqueue_audio(pcm, index * 4)
            assert capture._audio_queue.qsize() <= 2
        assert time.monotonic() - started < 1
        assert capture.audio_dropped_chunks == 4
        gate.set()
        await asyncio.wait_for(capture._audio_queue.join(), 2)
        assert [item["start"] for item in delivered] == [0, 20, 24]
        assert delivered[1]["discontinuity"] is True
    finally:
        await capture.stop()


@pytest.mark.asyncio
async def test_ffprobe_failure_does_not_interrupt_video(tmp_path):
    ffmpeg = shutil.which(Settings.from_env().ffmpeg_bin)
    if not ffmpeg:
        pytest.skip("FFmpeg not installed")
    fixture = tmp_path / "fixture.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=160x90:rate=10", "-t", "4", "-c:v", "libx264",
                    "-threads", "1", "-g", "20", str(fixture)], check=True)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    capture = MediaCapture(ffmpeg, tmp_path / "output", interval=1)
    capture.ffprobe = "/missing/ffprobe"
    frames, audio = [], []
    async def collect_frame(path, timestamp):
        frames.append(timestamp)
    async def collect_audio(item):
        audio.append(item)
    try:
        source = SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/fixture.mp4", request_headers={})
        await asyncio.wait_for(capture.run(source, collect_frame, collect_audio), 20)
        assert len(frames) == 4 and capture.ready
        assert capture.audio_failures > 0 and capture.audio_last_error
        assert not audio
        assert capture.process.returncode == 0
    finally:
        await capture.stop()
        await asyncio.to_thread(server.shutdown)
        server.server_close()
