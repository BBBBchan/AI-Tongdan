"""Independent, read-only Bilibili capture for Stream Clipper.

The observer modules derive from AI同担 (BBBBchan/AI-Tongdan), CC BY-NC 4.0.
This module intentionally has no dependency on its server or interaction code.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import time

from .observer.audio import AudioPipeline
from .observer.bilibili import BilibiliClient, BilibiliError, parse_room_id
from .observer.media import MediaCapture


class RecordingLimitReached(RuntimeError):
    pass


def playlist_segments(path):
    """Stream completed local HLS entries with their generation-relative times.

    FFmpeg atomically replaces the playlist. Incomplete temp segments never
    appear here. Only a scalar cumulative time is retained for long recordings.
    """
    elapsed, duration = 0.0, None
    try:
        handle = Path(path).open(encoding="utf-8")
    except FileNotFoundError:
        return
    with handle:
        for raw in handle:
            line = raw.strip()
            if line.startswith("#EXTINF:"):
                try:
                    duration = float(line.split(":", 1)[1].split(",", 1)[0])
                except ValueError:
                    duration = None
                if duration is not None and (not math.isfinite(duration) or duration <= 0):
                    duration = None
            elif line and not line.startswith("#"):
                match = re.fullmatch(r"segment_(\d+)\.ts", line)
                if match and duration is not None:
                    yield int(match.group(1)), elapsed, duration, Path(path).parent / line
                    elapsed += duration
                duration = None


class ArchivalMediaCapture(MediaCapture):
    """Retain completed HLS segments and publish each exactly once in order."""

    def __init__(self, *args, on_segment, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_segment = on_segment
        self.last_segment = -1
        self.recorded_until = self.timeline_floor
        self._archive_finished = asyncio.Event()

    async def harvest(self):
        if self.timeline_anchor is None:
            return
        for number, offset, duration, path in playlist_segments(self.directory / "index.m3u8"):
            if number <= self.last_segment or not path.is_file():
                continue
            start = self.timeline_anchor + offset
            end = start + duration
            await self.on_segment(start, end, path, {
                "generation": self.generation,
                "segment_index": number,
                "generation_offset": offset,
                "timeline_anchor": self.timeline_anchor,
                "timestamp_basis": "generation_extinf_anchored_at_first_video_receipt",
                "timing_uncertainty_seconds": max(.05, self.interval / 2),
                "bytes": path.stat().st_size,
            })
            self.last_segment = number
            self.recorded_until = max(self.recorded_until, end)

    async def _archive_loop(self):
        while not self._archive_finished.is_set():
            await self.harvest()
            try:
                await asyncio.wait_for(self._archive_finished.wait(), .25)
            except asyncio.TimeoutError:
                pass

    async def run(self, source, on_frame, on_audio=None):
        media = asyncio.create_task(super().run(source, on_frame, on_audio), name="clipper:media")
        archive = asyncio.create_task(self._archive_loop(), name="clipper:archive")
        try:
            done, _ = await asyncio.wait({media, archive}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            if archive in done and not media.done():
                raise RuntimeError("录像索引任务意外结束")
            await media
        finally:
            self._archive_finished.set()
            # Cancellation reaches MediaCapture.run's finally, which terminates
            # its process and every decoder. Await it before the final harvest.
            for task in (media, archive):
                if not task.done():
                    task.cancel()
            await asyncio.gather(media, archive, return_exceptions=True)
            await self.harvest()


class LiveCapture:
    """One room, one clock, independent media generations; never sends chat.

    callbacks:
      on_event(kind, start, end, text, data, source_key=None)
      on_segment(start, end, path: Path, metadata)
      on_state(partial_state: dict)
    All callbacks are asynchronous. ``stop`` is idempotent and waits for cleanup.
    """

    def __init__(self, settings, session_id, directory, on_event, on_segment, on_state):
        self.settings = settings
        self.session_id = session_id
        self.directory = Path(directory).resolve()
        self.on_event, self.on_segment, self.on_state = on_event, on_segment, on_state
        self.client = None
        self.audio = None
        self.capture = None
        self.clock_start = None
        self._runner = None
        self._danmaku_task = None
        self._stopping = asyncio.Event()
        self._finished = asyncio.Event()
        self._started = False
        self._bytes = 0
        self._duration = 0.0
        self._generation = 0
        self._terminal = "stopped"
        self._limit_reached = False
        self._state = {"status": "idle", "duration": 0.0, "recording_bytes": 0}

    async def _publish(self, **changes):
        self._state.update(changes)
        await self.on_state(dict(self._state))

    async def _pause(self, delay):
        try:
            await asyncio.wait_for(self._stopping.wait(), delay)
        except asyncio.TimeoutError:
            pass

    def _safe_error(self, exc):
        # Network errors may contain signed URLs. Only our client's sanitized
        # message and local, known messages are suitable for UI state.
        if isinstance(exc, BilibiliError):
            return str(exc)[:300]
        if isinstance(exc, RecordingLimitReached):
            return "录像达到容量上限，已停止采集；已完成的片段仍可分析和导出。"
        return f"直播采集暂时中断（{type(exc).__name__}），正在重新连接。"

    async def _accept_segment(self, start, end, path, metadata):
        if self._limit_reached:
            raise RecordingLimitReached()
        await self.on_segment(start, end, path, metadata)
        self._bytes += metadata["bytes"]
        self._duration = max(self._duration, end)
        await self._publish(duration=self._duration, recording_bytes=self._bytes,
                            playlist=str(path.parent / "index.m3u8"))
        limit = max(0.0, float(getattr(self.settings, "max_recording_gib", 30))) * 1024**3
        if self._bytes >= limit:
            self._limit_reached = True
            raise RecordingLimitReached()

    async def _frame(self, source, timestamp):
        capture = self.capture
        path = self.directory / "frames" / f"{capture.generation}-{capture.frames_seen:08d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        await self.on_event("frame", timestamp, timestamp, "", {
            "frame_path": str(path), "generation": capture.generation,
            "timestamp_basis": capture.timestamp_basis,
        }, source_key=f"{capture.generation}:frame:{capture.frames_seen}")
        await self._publish(status="live", latest_frame=str(path),
                            duration=max(self._duration, timestamp))

    async def _audio_event(self, kind, data):
        if kind == "audio_segment":
            await self.on_event(kind, data["start"], data["end"], data["text"], data,
                                source_key=data["id"])
        elif kind == "audio_status":
            await self._publish(asr=data)

    async def _finish_audio(self):
        """Finish already-decoded ASR work before a room is rotated away.

        The producer must have stopped first. A failed/unavailable ASR service
        cannot hold a patrol forever, so only a bounded drain is attempted.
        This does not claim that an incomplete decoder buffer was transcribed.
        """
        if self.audio is None:
            return
        timeout = max(0.0, min(45.0, float(
            getattr(self.settings, "audio_drain_seconds", 12.0))))
        drained = True
        task = self.audio.task
        try:
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(self.audio.queue.join(), timeout=timeout)
                except asyncio.TimeoutError:
                    drained = False
            elif not self.audio.queue.empty():
                drained = False
        finally:
            # stop marks any remaining queue items as dropped and persists
            # processed_until; the caller can distinguish a timeout from silence.
            await self.audio.stop()
        await self._publish(asr_drain_status="complete" if drained else "timeout",
                            asr_drained_until=self.audio.processed_until)

    async def _read_danmaku(self, room_id):
        failures = 0
        retries = max(0, int(getattr(self.settings, "max_retries", 5)))
        try:
            async for event in self.client.events(room_id):
                if self._stopping.is_set():
                    return
                kind = event.get("type")
                if kind == "danmaku":
                    stamp = time.monotonic() - self.clock_start
                    public = {k: event[k] for k in
                              ("message_id", "room_id", "sent_at", "received_at") if k in event}
                    if event.get("user_id"):
                        public["author_id"] = hashlib.sha256(
                            str(event["user_id"]).encode()).hexdigest()[:16]
                    public["timestamp_basis"] = "local_receipt_session_clock"
                    await self.on_event("danmaku", stamp, stamp, event.get("text", ""),
                                        public, source_key=event.get("message_id"))
                elif kind == "connection":
                    status = event.get("status")
                    failures = 0 if status == "connected" else failures + (status == "reconnecting")
                    await self._publish(danmaku_status=status)
                    if failures > retries:
                        await self._publish(danmaku_status="error", warning="弹幕连接多次失败；录像与音频识别继续。")
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._publish(danmaku_status="error", warning="弹幕暂时不可用；录像与音频识别继续。")

    async def run(self, room_input):
        if self._started:
            raise RuntimeError("每个 LiveCapture 实例只能运行一次")
        self._started = True
        self._runner = asyncio.current_task()
        self.clock_start = time.monotonic()
        self._state["capture_clock_started_at"] = time.time()
        retries = max(0, int(getattr(self.settings, "max_retries", 5)))
        failures = 0
        try:
            parse_room_id(room_input)
            if self._stopping.is_set():
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            self.client = BilibiliClient(cookie=getattr(self.settings, "cookie", ""))
            audio_enabled = bool(self.settings.audio_config().get("base_url"))
            self.audio = AudioPipeline(
                self.session_id, self.directory / "audio", self.settings.audio_config,
                self._audio_event, lambda segment: None, enabled=audio_enabled,
            )
            await self.audio.start()
            if not audio_enabled:
                await self._publish(warning="尚未配置音频服务；录像与弹幕采集可正常使用。")
            while not self._stopping.is_set():
                try:
                    await self._publish(status="connecting", retry=failures)
                    room = await self.client.resolve_room(room_input)
                    await self._publish(room=asdict(room))
                    if room.live_status != 1:
                        await self._publish(status="waiting", warning="直播间尚未开播，正在等待。")
                        await self._pause(max(5, float(getattr(self.settings, "room_poll_seconds", 20))))
                        continue
                    if self._danmaku_task is None:
                        self._danmaku_task = asyncio.create_task(
                            self._read_danmaku(room.room_id), name="clipper:danmaku")
                    sources = await self.client.get_play_sources(room.room_id)
                    if not sources:
                        raise BilibiliError("直播平台暂未提供可读取的视频流")
                    self._generation += 1
                    directory = self.directory / "media" / f"g{self._generation}"
                    capture_binary = getattr(self.settings, "capture_ffmpeg", "") or self.settings.ffmpeg
                    capture = ArchivalMediaCapture(
                        capture_binary, directory,
                        interval=max(.5, float(getattr(self.settings, "sample_interval", 2))),
                        clock_start=self.clock_start, timeline_floor=self._duration,
                        on_segment=self._accept_segment,
                    )
                    if getattr(self.settings, "capture_ffmpeg", ""):
                        # Keep the capture binary's own linked libraries. The
                        # notebook/ML runtime may export incompatible LD paths;
                        # remove them for these media children only.
                        capture.subprocess_env = {
                            key: value for key, value in os.environ.items()
                            if key not in {"LD_LIBRARY_PATH", "LD_PRELOAD"}
                        }
                    capture.ffprobe = self.settings.ffprobe
                    self.capture = capture
                    await self._publish(generation=capture.generation,
                                        playlist=str(directory / "index.m3u8"))
                    before = self._duration
                    try:
                        await capture.run(sources[(self._generation - 1) % len(sources)],
                                          self._frame, self.audio.offer if audio_enabled else None)
                    finally:
                        self._duration = max(self._duration, capture.recorded_until,
                                             capture.last_frame_timestamp or 0)
                        if self._duration - before >= 30:
                            failures = 0
                        await capture.stop()
                        self.capture = None
                    if self._stopping.is_set():
                        break
                    # Sustained useful capture resets the consecutive-failure
                    # budget; loops that deliver only one frame do not.
                    raise BilibiliError("直播流已结束，正在检查直播间状态")
                except RecordingLimitReached as exc:
                    self._terminal = "capacity_reached"
                    await self._publish(status=self._terminal, warning=self._safe_error(exc))
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failures += 1
                    message = self._safe_error(exc)
                    if failures > retries:
                        self._terminal = "error"
                        await self._publish(status="error", warning="达到重试次数上限，采集已停止。", error=message)
                        break
                    await self._publish(status="reconnecting", warning=message, retry=failures,
                                        retry_in=min(30, 2 ** failures))
                    await self._pause(min(30, 2 ** failures))
        except asyncio.CancelledError:
            if not self._stopping.is_set():
                raise
        except Exception:
            self._terminal = "error"
            raise
        finally:
            self._stopping.set()
            try:
                if self._danmaku_task:
                    self._danmaku_task.cancel()
                    await asyncio.gather(self._danmaku_task, return_exceptions=True)
                cleanup = []
                if self.capture:
                    # Stop producers before draining their ASR consumer.
                    with suppress(Exception):
                        await self.capture.stop()
                if self.audio:
                    cleanup.append(self._finish_audio())
                if self.client:
                    cleanup.append(self.client.aclose())
                # A failed final status callback must not prevent closing the
                # network client or another subprocess.
                if cleanup:
                    await asyncio.gather(*cleanup, return_exceptions=True)
                await self._publish(status=self._terminal, duration=self._duration)
            finally:
                self._finished.set()

    async def stop(self):
        already_stopping = self._stopping.is_set()
        self._stopping.set()
        task = self._runner
        if task and task is not asyncio.current_task():
            if not task.done() and not already_stopping:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif task is None:
            self._finished.set()
