import asyncio
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import subprocess
import threading
from types import SimpleNamespace
import time

import pytest

from clipper.config import Settings
from clipper.ingest import ArchivalMediaCapture, LiveCapture, playlist_segments
from clipper.observer.bilibili import BilibiliClient, RoomInfo


@pytest.fixture
def media_file(tmp_path):
    settings = Settings.from_env()
    path = tmp_path / "source.ts"
    subprocess.run([
        settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=600:sample_rate=16000",
        "-t", "22", "-c:v", "libx264", "-preset", "ultrafast",
        "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(path),
    ], check=True, timeout=30)
    return settings, path


@contextmanager
def serve_file(path, slow=False):
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def copyfile(self, source, output):
            try:
                if not slow:
                    return super().copyfile(source, output)
                while chunk := source.read(188 * 40):
                    output.write(chunk)
                    output.flush()
                    time.sleep(.03)
            except (ConnectionResetError, BrokenPipeError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=path.parent))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{path.name}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_playlist_uses_extinf_not_filename_or_wall_clock(tmp_path):
    playlist = tmp_path / "index.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:2.3,\nsegment_000000.ts\n"
                        "#EXTINF:1.7,\nsegment_000001.ts\n"
                        "#EXTINF:3.0,\nsegment_000002.ts\n")
    entries = list(playlist_segments(playlist))
    assert [(n, start, duration) for n, start, duration, _ in entries] == [
        (0, 0., 2.3), (1, 2.3, 1.7), (2, 4., 3.),
    ]
    assert all(path.parent == tmp_path for _, _, _, path in entries)


@pytest.mark.asyncio
async def test_archive_real_ffmpeg_retains_all_segments_and_audio(media_file, tmp_path):
    settings, source = media_file
    segments, frames, chunks = [], [], []

    async def segment(start, end, path, metadata):
        segments.append((start, end, path, metadata))

    async def frame(path, timestamp):
        assert Path(path).read_bytes().startswith(b"\xff\xd8")
        frames.append(timestamp)

    async def audio(chunk):
        assert chunk["wav_bytes"][:4] == b"RIFF"
        chunks.append(chunk)

    capture = ArchivalMediaCapture(settings.ffmpeg, tmp_path / "g1", interval=2,
                                   timeline_floor=10, on_segment=segment)
    capture.ffprobe = settings.ffprobe
    with serve_file(source) as url:
        await asyncio.wait_for(capture.run(SimpleNamespace(url=url, request_headers={}), frame, audio), 30)
    assert len(segments) > 8
    assert len({str(item[2]) for item in segments}) == len(segments)
    assert all(path.is_file() for _, _, path, _ in segments)
    assert segments[0][0] >= 10
    assert segments[-1][1] - segments[0][0] == pytest.approx(22, abs=.2)
    assert all(left[1] == pytest.approx(right[0]) for left, right in zip(segments, segments[1:]))
    assert frames and chunks
    # AAC encoder priming may precede the first video PTS by ~64 ms.
    assert chunks[0]["start"] >= 9.8
    assert capture.process.returncode is not None
    assert not capture._audio_tasks
    assert not capture._audio_processes


@pytest.mark.asyncio
async def test_live_stop_closes_real_media_process_and_chat(media_file, tmp_path, monkeypatch):
    settings, source = media_file
    first_frame = asyncio.Event()
    states = []
    clients = []

    class LocalRoomClient(BilibiliClient):
        def __init__(self, cookie=""):
            super().__init__(cookie=cookie)
            self.closed = False
            self.chat_cancelled = False
            clients.append(self)

        async def resolve_room(self, room):
            return RoomInfo(732, 0, "test", 0, 1, "")

        async def get_play_sources(self, room):
            return [SimpleNamespace(url=url, request_headers={})]

        async def events(self, room):
            try:
                yield {"type": "connection", "status": "connected"}
                await asyncio.Event().wait()
            finally:
                self.chat_cancelled = True

        async def aclose(self):
            self.closed = True
            await super().aclose()

    async def event(kind, start, end, text, data, source_key=None):
        if kind == "frame":
            first_frame.set()

    async def segment(*args):
        pass

    async def state(value):
        states.append(value)

    monkeypatch.setattr("clipper.ingest.BilibiliClient", LocalRoomClient)
    settings = replace(settings, audio_endpoint="")
    monkeypatch.setattr(settings, "audio_config", lambda: {})
    live = LiveCapture(settings, "test", tmp_path / "live", event, segment, state)
    with serve_file(source, slow=True) as url:
        task = asyncio.create_task(live.run("732"))
        await asyncio.wait_for(first_frame.wait(), 20)
        process = live.capture.process
        await asyncio.wait_for(asyncio.gather(live.stop(), live.stop()), 10)
        await live.stop()
    assert task.done()
    assert process.returncode is not None
    assert clients[0].closed and clients[0].chat_cancelled
    assert states[-1]["status"] == "stopped"
    assert live._finished.is_set()
    assert live.audio.client.is_closed


@pytest.mark.asyncio
async def test_capacity_stops_after_indexing_complete_segment(tmp_path):
    settings = Settings(max_recording_gib=0)
    indexed, states = [], []

    async def event(*args, **kwargs):
        pass

    async def segment(*args):
        indexed.append(args)

    async def state(value):
        states.append(value)

    live = LiveCapture(settings, "test", tmp_path, event, segment, state)
    path = tmp_path / "segment_000000.ts"
    path.write_bytes(b"complete segment")
    from clipper.ingest import RecordingLimitReached
    with pytest.raises(RecordingLimitReached):
        await live._accept_segment(0, 2, path, {"bytes": path.stat().st_size})
    with pytest.raises(RecordingLimitReached):
        await live._accept_segment(0, 2, path, {"bytes": path.stat().st_size})
    assert len(indexed) == 1
    assert states[-1]["duration"] == 2


@pytest.mark.asyncio
async def test_stop_drain_preserves_in_flight_and_queued_asr(tmp_path):
    states, completed = [], []

    async def state(value):
        states.append(value)

    async def ignore(*args, **kwargs):
        pass

    class Audio:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.processed_until = 0
            self.stopped = False
            for item in (4, 8, 12):
                self.queue.put_nowait(item)
            self.task = asyncio.create_task(self.worker())

        async def worker(self):
            while True:
                item = await self.queue.get()
                try:
                    await asyncio.sleep(.01)
                    completed.append(item)
                    self.processed_until = item
                finally:
                    self.queue.task_done()

        async def stop(self):
            self.stopped = True
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    live = LiveCapture(Settings(), "test", tmp_path, ignore, ignore, state)
    live.audio = Audio()
    await live._finish_audio()
    assert completed == [4, 8, 12]
    assert live.audio.stopped
    assert states[-1]["asr_drain_status"] == "complete"
    assert states[-1]["asr_drained_until"] == 12


@pytest.mark.asyncio
async def test_stop_drain_is_bounded_when_asr_stalls(tmp_path):
    states = []

    async def state(value):
        states.append(value)

    async def ignore(*args, **kwargs):
        pass

    class Audio:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.queue.put_nowait("unfinished")
            self.processed_until = 8
            self.stopped = False
            self.task = asyncio.create_task(asyncio.Event().wait())

        async def stop(self):
            self.stopped = True
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.queue.get_nowait()
            self.queue.task_done()

    settings = Settings()
    settings.audio_drain_seconds = .03
    live = LiveCapture(settings, "test", tmp_path, ignore, ignore, state)
    live.audio = Audio()
    await asyncio.wait_for(live._finish_audio(), .5)
    assert live.audio.stopped
    assert states[-1]["asr_drain_status"] == "timeout"
    assert states[-1]["asr_drained_until"] == 8


@pytest.mark.asyncio
async def test_live_capture_uses_dedicated_binary_without_mutating_parent_environment(
        tmp_path, monkeypatch):
    entered = asyncio.Event()
    configured = []
    monkeypatch.setenv("LD_LIBRARY_PATH", "/test/ml-runtime-libraries")
    monkeypatch.setenv("LD_PRELOAD", "/test/ml-runtime-preload.so")

    class Client:
        def __init__(self, cookie=""):
            pass

        async def resolve_room(self, room):
            return RoomInfo(732, 0, "test", 0, 1, "")

        async def get_play_sources(self, room):
            return [SimpleNamespace(url="unused", request_headers={})]

        async def events(self, room):
            yield {"type": "connection", "status": "connected"}
            await asyncio.Event().wait()

        async def aclose(self):
            pass

    class Capture:
        def __init__(self, binary, directory, **kwargs):
            self.binary = binary
            self.generation = "g1"
            self.recorded_until = 0
            self.last_frame_timestamp = None
            configured.append(self)

        async def run(self, *args):
            entered.set()
            await asyncio.Event().wait()

        async def stop(self):
            pass

    async def ignore(*args, **kwargs):
        pass

    settings = Settings(ffmpeg="render-with-libass",
                        capture_ffmpeg="network-compatible-capture")
    monkeypatch.setattr(settings, "audio_config", lambda: {})
    monkeypatch.setattr("clipper.ingest.BilibiliClient", Client)
    monkeypatch.setattr("clipper.ingest.ArchivalMediaCapture", Capture)
    live = LiveCapture(settings, "test", tmp_path, ignore, ignore, ignore)
    task = asyncio.create_task(live.run("732"))
    await asyncio.wait_for(entered.wait(), 2)
    await live.stop()
    assert task.done()
    assert configured[0].binary == "network-compatible-capture"
    assert "LD_LIBRARY_PATH" not in configured[0].subprocess_env
    assert "LD_PRELOAD" not in configured[0].subprocess_env
    assert os.environ["LD_LIBRARY_PATH"] == "/test/ml-runtime-libraries"
    assert os.environ["LD_PRELOAD"] == "/test/ml-runtime-preload.so"
    assert settings.ffmpeg == "render-with-libass"


@pytest.mark.asyncio
async def test_capture_environment_reaches_video_and_audio_decoders(
        media_file, tmp_path, monkeypatch):
    settings, source = media_file
    calls, chunks = [], []
    launch = asyncio.create_subprocess_exec
    child_env = {key: value for key, value in os.environ.items()
                 if key not in {"LD_LIBRARY_PATH", "LD_PRELOAD"}}
    child_env["CLIPPER_TEST_CAPTURE_CHILD"] = "1"

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs.get("env")))
        return await launch(*args, **kwargs)

    async def ignore(*args, **kwargs):
        pass

    async def audio(chunk):
        chunks.append(chunk)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    capture = ArchivalMediaCapture(settings.ffmpeg, tmp_path/"media", on_segment=ignore)
    capture.ffprobe = settings.ffprobe
    capture.subprocess_env = child_env
    with serve_file(source) as url:
        await asyncio.wait_for(capture.run(
            SimpleNamespace(url=url, request_headers={}), ignore, audio), 30)
    assert chunks and capture.frames_seen
    assert calls and all(env == child_env for _, env in calls)
    assert any(args[0] == settings.ffprobe for args, _ in calls)
    assert any(args[0] == settings.ffmpeg and "s16le" in args for args, _ in calls)
    assert any(args[0] == settings.ffmpeg and "image2pipe" in args for args, _ in calls)
