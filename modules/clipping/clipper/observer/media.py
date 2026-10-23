"""One network input supplies HLS, sampled video and independently decoded audio.

The first sampled video frame anchors a generation's media timeline to the
session clock once. Later timestamps use media time, never individual receipt
latency. HLS audio PTS uses the same origin. This is an approximate session
anchor, not an absolute broadcast/CDN clock.
"""
import asyncio
from collections import deque
from contextlib import suppress
import inspect
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import uuid
import wave


class MediaCapture:
    def __init__(self, ffmpeg, directory: Path, interval=2, clock_start=None,
                 timeline_floor=0, generation=None, audio_segment_seconds=4):
        self.ffmpeg = ffmpeg
        self.subprocess_env = None
        self.directory = Path(directory)
        self.interval = interval
        self.clock_start = time.monotonic() if clock_start is None else clock_start
        self.timeline_floor = max(0.0, float(timeline_floor))
        self.generation = generation if generation is not None else self.directory.name
        self.audio_segment_seconds = min(6.0, max(3.0, float(audio_segment_seconds)))
        self.process = None
        self.last_frame_at = None
        self.last_frame_timestamp = None
        self.frames_seen = 0
        self.stderr_tail = []
        self.timeline_anchor = None
        self.timestamp_basis = "generation_media_pts_anchored_at_first_video_receipt"
        self._anchor_ready = asyncio.Event()
        self._video_done = asyncio.Event()
        self._audio_tasks = []
        self._audio_processes = set()
        self._audio_queue = asyncio.Queue(maxsize=2)
        self._audio_origin_pts = None
        self._audio_buffer = bytearray()
        self._audio_buffer_pts = None
        self._audio_expected_pts = None
        self._audio_discontinuity = True
        self.audio_chunks = 0
        self.audio_dropped_chunks = 0
        self.audio_skipped_segments = 0
        self.audio_failures = 0
        self.audio_last_error = None
        self.audio_stream_present = None
        candidate = Path(str(ffmpeg)).with_name("ffprobe")
        self.ffprobe = str(candidate) if candidate.is_file() else shutil.which("ffprobe")

    @property
    def ready(self):
        playlist = self.directory / "index.m3u8"
        return playlist.is_file() and "#EXTINF" in playlist.read_text(errors="replace")

    async def run(self, source, on_frame, on_audio=None):
        self.directory.mkdir(parents=True, exist_ok=True)
        headers = "".join(f"{k}: {v}\r\n" for k, v in source.request_headers.items()
                          if k.lower() in {"user-agent", "referer", "origin"})
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
               "-rw_timeout", "15000000", "-headers", headers,
               "-i", source.url,
               "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
               "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
               "-hls_flags", "independent_segments+temp_file",
               "-hls_segment_filename", str(self.directory / "segment_%06d.ts"),
               str(self.directory / "index.m3u8"),
               "-map", "0:v:0", "-an", "-vf",
               f"fps=1/{self.interval},scale=960:-2:force_original_aspect_ratio=decrease",
               "-c:v", "mjpeg", "-q:v", "5", "-threads", "2", "-f", "image2pipe", "pipe:1"]
        self.process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self.subprocess_env)

        async def drain_errors():
            while line := await self.process.stderr.readline():
                raw = line.decode(errors="replace").strip()
                if "http" not in raw and raw:
                    self.stderr_tail.append(raw[:300])
                    self.stderr_tail = self.stderr_tail[-8:]

        errors = asyncio.create_task(drain_errors())
        if on_audio is not None:
            self._audio_tasks = [asyncio.create_task(self._watch_audio_segments()),
                                 asyncio.create_task(self._deliver_audio(on_audio))]
        try:
            buffer = bytearray()
            while True:
                chunk = await asyncio.wait_for(self.process.stdout.read(65536), timeout=35)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > 8 * 1024 * 1024:
                    raise RuntimeError("视频采样返回的单帧过大")
                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", max(0, start + 2))
                    if start < 0 or end < 0:
                        break
                    picture = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]
                    self.frames_seen += 1
                    self.last_frame_at = time.monotonic()
                    if self.timeline_anchor is None:
                        self.timeline_anchor = max(self.timeline_floor, self.last_frame_at - self.clock_start)
                        self._anchor_ready.set()
                    # fps output is a regular media-time grid. Receipt bursts or
                    # audio decoding latency must not shift audiovisual alignment.
                    timestamp = self.timeline_anchor + (self.frames_seen - 1) * self.interval
                    self.last_frame_timestamp = timestamp
                    frame = self.directory / "sample.jpg"
                    frame.write_bytes(picture)
                    await on_frame(frame, timestamp)
            code = await self.process.wait()
            self._video_done.set()
            if code:
                detail = self.stderr_tail[-1] if self.stderr_tail else f"FFmpeg exit {code}"
                raise RuntimeError("视频连接中断：" + detail)
            if self._audio_tasks:
                # Finish the final local HLS segment, but a slow ASR consumer may
                # not hold the next media generation indefinitely.
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._audio_tasks[0]), 10)
                    await asyncio.wait_for(self._audio_queue.join(), 2)
        finally:
            self._video_done.set()
            await self.stop()
            errors.cancel()
            with suppress(asyncio.CancelledError):
                await errors

    def _completed_segments(self):
        playlist = self.directory / "index.m3u8"
        if not playlist.is_file():
            return []
        result, discontinuity = deque(maxlen=4), False
        first = None
        for line in playlist.open(encoding="utf-8", errors="replace"):
            line = line.strip()
            if line == "#EXT-X-DISCONTINUITY":
                discontinuity = True
            match = re.fullmatch(r"segment_([0-9]+)\.ts", line.strip())
            if match:
                item = (int(match.group(1)), self.directory / line.strip(), discontinuity)
                if item[0] == 0 and self._audio_origin_pts is None:
                    first = item
                result.append(item)
                discontinuity = False
        values = list(result)
        if first is not None and all(item[0] != 0 for item in values):
            values.insert(0, first)
        return values

    async def _local_command(self, command, limit=2 * 1024 * 1024, timeout=8):
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self.subprocess_env)
        self._audio_processes.add(process)

        async def drain():
            last = b""
            while data := await process.stderr.read(4096):
                last = (last + data)[-4096:]
            return last

        errors = asyncio.create_task(drain())
        async def collect():
            output = bytearray()
            while data := await process.stdout.read(65536):
                output.extend(data)
                if len(output) > limit:
                    raise RuntimeError("音频片段解码超过大小限制")
            code = await process.wait()
            if code:
                raise RuntimeError("本地音频片段无法解码")
            return bytes(output)
        try:
            return await asyncio.wait_for(collect(), timeout)
        finally:
            drain = asyncio.create_task(self._drain_stdout(process))
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()
            await drain
            self._audio_processes.discard(process)
            errors.cancel()
            with suppress(asyncio.CancelledError):
                await errors

    async def _decode_segment(self, path):
        if not self.ffprobe:
            raise RuntimeError("缺少 ffprobe，无法可靠对齐音频时间戳")
        metadata = await self._local_command([
            self.ffprobe, "-v", "error", "-show_entries", "stream=codec_type,start_time",
            "-of", "json", str(path)], limit=65536)
        streams = json.loads(metadata).get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        self.audio_stream_present = audio is not None
        video_pts = float(video["start_time"]) if video and "start_time" in video else None
        if not audio:
            return video_pts, None, b""
        audio_pts = float(audio["start_time"])
        if not math.isfinite(audio_pts) or video_pts is None or not math.isfinite(video_pts):
            raise RuntimeError("音频片段缺少有效媒体 PTS")
        pcm = await self._local_command([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-threads", "1",
            "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"])
        return video_pts, audio_pts, pcm

    async def _watch_audio_segments(self):
        last_segment = -1
        try:
            while True:
                available = [item for item in self._completed_segments() if item[0] > last_segment]
                # Establish origin from actual first segment before skipping any
                # backlog. If segment zero was already lost, do not invent PTS.
                if self._audio_origin_pts is None and available and available[0][0] != 0:
                    raise RuntimeError("首个媒体片段已过期，无法确定声画共同时间原点")
                for number, source, discontinuity in available:
                    if len(available) > 3 and number != 0 and number < available[-3][0]:
                        self.audio_skipped_segments += 1
                        last_segment = number
                        self._audio_discontinuity = True
                        continue
                    scratch = self.directory / ".audio_segment.ts"
                    try:
                        scratch.unlink(missing_ok=True)
                        # Keep a completed segment alive while FFmpeg rotates HLS.
                        try:
                            os.link(source, scratch)
                        except OSError:
                            await asyncio.to_thread(shutil.copyfile, source, scratch)
                        video_pts, audio_pts, pcm = await self._decode_segment(scratch)
                        if self._audio_origin_pts is None:
                            if number != 0 or video_pts is None:
                                raise RuntimeError("首个视频片段没有可用 PTS")
                            self._audio_origin_pts = video_pts
                        gap = number != last_segment + 1 or discontinuity
                        if pcm and audio_pts is not None:
                            await self._append_pcm(pcm, audio_pts, gap)
                        self.audio_last_error = None
                        last_segment = number
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.audio_failures += 1
                        self.audio_last_error = str(exc)[:240]
                        self._audio_discontinuity = True
                        last_segment = number
                    finally:
                        scratch.unlink(missing_ok=True)
                if self._video_done.is_set():
                    # HLS writes its final playlist before the input process exits.
                    remaining = [item for item in self._completed_segments() if item[0] > last_segment]
                    if not remaining:
                        break
                await asyncio.sleep(0.2)
            await self._flush_pcm(final=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.audio_failures += 1
            self.audio_last_error = str(exc)[:240]

    async def _append_pcm(self, pcm, pts, discontinuity=False):
        if len(pcm) % 2:
            pcm = pcm[:-1]
        expected = self._audio_expected_pts
        if expected is not None and abs(pts - expected) > 0.1:
            discontinuity = True
        if discontinuity:
            await self._flush_pcm(final=True)
            self._audio_buffer_pts = None
            self._audio_discontinuity = True
        if self._audio_buffer_pts is None:
            self._audio_buffer_pts = pts
        self._audio_buffer.extend(pcm)
        self._audio_expected_pts = pts + len(pcm) / 32000
        await self._flush_pcm()

    async def _flush_pcm(self, final=False):
        wanted = int(self.audio_segment_seconds * 16000) * 2
        while len(self._audio_buffer) >= wanted or (final and self._audio_buffer):
            size = min(wanted, len(self._audio_buffer))
            pcm = bytes(self._audio_buffer[:size])
            del self._audio_buffer[:size]
            pts = self._audio_buffer_pts
            self._audio_buffer_pts = pts + size / 32000
            await self._enqueue_audio(pcm, pts, size < wanted)
            self._audio_discontinuity = False
        if final:
            self._audio_buffer_pts = None

    async def _enqueue_audio(self, pcm, pts, partial=False):
        await asyncio.wait_for(self._anchor_ready.wait(), 5)
        start = self.timeline_anchor + pts - self._audio_origin_pts
        if start < 0:
            trim = min(len(pcm) // 2, math.ceil(-start * 16000))
            pcm = pcm[trim * 2:]
            pts += trim / 16000
            start += trim / 16000
        if not pcm:
            return
        duration = len(pcm) / 32000
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        item = {"id": uuid.uuid4().hex, "wav_bytes": output.getvalue(), "start": max(0.0, start),
                "end": max(0.0, start) + duration, "generation": self.generation,
                "discontinuity": self._audio_discontinuity, "partial": partial,
                "source_pts_start": pts, "source_pts_end": pts + duration,
                "timestamp_basis": self.timestamp_basis,
                "timeline_anchor": self.timeline_anchor,
                "timing_uncertainty_seconds": max(0.05, self.interval / 2),
                "sample_rate": 16000, "channels": 1}
        if self._audio_queue.full():
            pending = []
            while not self._audio_queue.empty():
                pending.append(self._audio_queue.get_nowait())
                self._audio_queue.task_done()
            pending.pop(0)
            self.audio_dropped_chunks += 1
            if pending:
                pending[0]["discontinuity"] = True
            else:
                item["discontinuity"] = True
            for previous in pending:
                self._audio_queue.put_nowait(previous)
        self._audio_queue.put_nowait(item)
        self.audio_chunks += 1

    async def _deliver_audio(self, callback):
        while True:
            item = await self._audio_queue.get()
            try:
                result = callback(item)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.audio_failures += 1
                self.audio_last_error = str(exc)[:240]
            finally:
                self._audio_queue.task_done()

    async def _drain_stdout(self, process):
        if process.stdout:
            while await process.stdout.read(65536):
                pass

    async def stop(self):
        # A cancelled image reader leaves a full pipe. Drain it
        # while waiting, otherwise Process.wait can deadlock.
        drain = asyncio.create_task(self._drain_stdout(self.process)) if self.process else None
        if self.process and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        if drain:
            await drain
        for task in self._audio_tasks:
            task.cancel()
        if self._audio_tasks:
            await asyncio.gather(*self._audio_tasks, return_exceptions=True)
        self._audio_tasks = []
        while not self._audio_queue.empty():
            self._audio_queue.get_nowait()
            self._audio_queue.task_done()
            self.audio_dropped_chunks += 1
        self._audio_buffer.clear()
