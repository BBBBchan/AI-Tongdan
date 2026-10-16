"""Local Bilibili workspace with independent video, danmaku and agent tasks."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from functools import wraps
from io import BytesIO
import base64
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Literal
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image

from .agent import LiveAgent
from .audio import AudioPipeline
from .bilibili import BilibiliClient
from .config import ROOT, Settings
from .danmaku_candidates import candidate_context, generate_candidates
from .danmaku_chorus import detect_choruses
from .danmaku_corpus import DanmakuCorpus
from .danmaku_auto import AutoDanmaku
from .danmaku_send import MAX_DANMAKU_LENGTH, DanmakuCredentials, DanmakuSender, DanmakuSendError
from .events import EventBus
from .journal import SessionJournal
from .local_danmaku import LocalDanmakuBroker
from .media import MediaCapture
from .model import ModelBusy, VisionModel


def _fence_session_change(method):
    """Fence every overlapping switch before it can wait on room I/O/locks."""
    @wraps(method)
    async def changing(workspace, *args, **kwargs):
        workspace.pending_session_changes += 1
        workspace.local_danmaku.cancel()
        try:
            return await method(workspace, *args, **kwargs)
        finally:
            workspace.pending_session_changes -= 1
    return changing


class SessionRequest(BaseModel):
    room: str = Field(min_length=1, max_length=500)
    candidate_count: int = Field(default=5, ge=1, le=10, strict=True)
    candidate_only: bool = Field(default=False, strict=True)
    allow_manual_send: bool = Field(default=False, strict=True)


class DanmakuPolicyRequest(BaseModel):
    session_id: str
    allow_manual_send: bool = Field(strict=True)


class QuestionRequest(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=2000)


class AudioLabelRequest(BaseModel):
    session_id: str
    role: Literal["host", "background", "singing", "unknown"]
    label: str | None = Field(default=None, max_length=40)


class SpeakerMergeRequest(BaseModel):
    session_id: str
    target_id: str = Field(min_length=1, max_length=80)


class DanmakuRequest(BaseModel):
    session_id: str
    text: str = Field(min_length=1, max_length=1000)


class DanmakuTransportRequest(BaseModel):
    mode: Literal["server", "local"]


class AutoDanmakuRequest(BaseModel):
    session_id: str
    interval_seconds: int = Field(default=300, ge=5, le=3600, strict=True)
    max_messages: int = Field(default=6, ge=1, le=1000, strict=True)


class AutoDanmakuStopRequest(BaseModel):
    session_id: str


class DanmakuStyleRequest(BaseModel):
    session_id: str
    style_value: int = Field(ge=0, le=100, strict=True)
    mode: Literal["builtin", "local"] | None = None


class Workspace:
    def __init__(self, settings):
        self.settings = settings
        self.bus = EventBus()
        self.client = BilibiliClient(cookie=settings.bilibili_cookie)
        self.agent = None
        self.audio = None
        self.model = None
        self.session = None
        self.room = None
        self.playback = {"url": "", "embed_url": "", "type": "hls", "ready": False}
        self.danmaku = deque(maxlen=150)
        self.sent_danmaku = deque(maxlen=30)
        self.tasks = []
        self._shutdown_pending = set()
        self._shutdown_background = set()
        self._shutdown_failed = False
        self.shutdown_timeout = 5.0
        self.shutdown_retry_timeout = 1.0
        self.capture = None
        self.lock = asyncio.Lock()
        self.local_danmaku_send_lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()
        self.clock_start = time.monotonic()
        self.danmaku_status = {"status": "disconnected"}
        self.resume_error = None
        self.danmaku_credentials = DanmakuCredentials(settings.data_dir, env_cookie=settings.bilibili_cookie)
        self.danmaku_sender = DanmakuSender(self.danmaku_credentials)
        self.local_danmaku = LocalDanmakuBroker(source_guard=self.require_danmaku_send_enabled)
        self.danmaku_transport = "server"
        self.pending_danmaku_transport_updates = 0
        self.pending_session_changes = 0
        try:
            transport = json.loads((settings.data_dir / "danmaku_transport.json").read_text())
            self.danmaku_transport = transport["mode"] if transport.get("mode") in {"server", "local"} else "local"
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self.danmaku_transport = "local"  # Never fall back on a damaged saved choice.
        self.last_danmaku_sent_at = None
        self.danmaku_send_error = None
        self.danmaku_candidates = {"items": [], "status": "waiting", "generated_at": None}
        self.danmaku_chorus = {"items": [], "session_id": None, "updated_at": None}
        self.danmaku_style = {"value": 30, "revision": 0, "mode": "builtin"}
        try:
            with (settings.data_dir / "danmaku_preferences.json").open() as handle:
                preferences = json.loads(handle.read(1024))
                value = preferences.get("style_value")
            if type(value) is int and 0 <= value <= 100:
                self.danmaku_style["value"] = value
            if preferences.get("mode") in {"builtin", "local"}:
                self.danmaku_style["mode"] = preferences["mode"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self._candidate_refresh = asyncio.Event()
        self.danmaku_corpus = DanmakuCorpus(ROOT / "danmakus_style")
        self._corpus_task = None
        self._corpus_last_check = None
        self._corpus_error = False
        self.pending_danmaku_style_updates = 0
        self.danmaku_auto = AutoDanmaku(self)

    def danmaku_send_status(self):
        connected = self.local_danmaku.connected
        local = self.danmaku_transport == "local"
        status = ({"configured": connected, "source": "local_helper", "max_length": MAX_DANMAKU_LENGTH}
                  if local else self.danmaku_credentials.status())
        receipt = getattr(self.local_danmaku if local else self.danmaku_sender, "last_receipt", None)
        active = bool(self.session_active(self.session["id"] if self.session else None)
                      and self.room and self.room.live_status == 1)
        candidate_only = self.candidate_only
        manual_only = candidate_only and self.allow_manual_send
        policy_reason = ("仅手动发送，自动发送已禁用" if manual_only else
                         "当前会话只生成候选，不发送弹幕。" if candidate_only else None)
        return {**status, "can_send": status["configured"] and active and (not candidate_only or manual_only),
                "can_auto_send": status["configured"] and active and not candidate_only,
                "status": "manual_only" if manual_only else "candidate_only" if candidate_only else "ready" if status["configured"] else "local_disconnected" if local else "login_required",
                "candidate_only": candidate_only,
                "allow_manual_send": self.allow_manual_send,
                "reason": policy_reason,
                "auto_disabled_reason": policy_reason,
                "transport": self.danmaku_transport, "local_connected": connected,
                "local_paired": self.local_danmaku.paired,
                "pairing_required": local and not connected,
                "error": self.danmaku_send_error,
                "last_receipt": dict(receipt) if isinstance(receipt, dict) else None}

    @property
    def candidate_only(self):
        return bool(self.session and self.session.get("candidate_only", False))

    @property
    def allow_manual_send(self):
        return bool(self.session and self.session.get("allow_manual_send") is True)

    def require_danmaku_send_enabled(self, source="manual"):
        """The session policy applies to every transport, including final POST guards."""
        if source not in {"manual", "auto"}:
            raise ValueError("Unknown danmaku source")
        if self.candidate_only:
            if self.allow_manual_send:
                if source == "manual":
                    return
                raise DanmakuSendError("仅手动发送，自动发送已禁用", 409, "auto_send_disabled")
            raise DanmakuSendError("当前会话只生成候选，不发送弹幕。", 409, "candidate_only")

    def _persist_session(self, session):
        """Commit session policy before publishing it in memory or to clients."""
        directory = self.settings.data_dir / "sessions" / session["id"]
        fd, temporary = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"session": session, "room": asdict(self.room),
                           "model": self.settings.model_config()["model"]},
                          stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "session.json")
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    async def set_danmaku_policy(self, session_id, allow_manual_send):
        if type(allow_manual_send) is not bool:
            raise DanmakuSendError("手动发送配置必须为布尔值。", 400, "invalid_request")
        async with self.lock:
            if (not self.session_active(session_id) or self.pending_session_changes):
                raise DanmakuSendError("直播会话已变更，请刷新当前房间。", 409, "session_changed")
            if not self.candidate_only:
                raise DanmakuSendError("该设置只用于轮巡候选会话。", 409, "policy_not_applicable")
            updated = {**self.session, "allow_manual_send": allow_manual_send}
            # Stops and cancellations fail closed. A failed save must never
            # grant manual permission in memory while leaving the old policy on disk.
            await self.danmaku_auto.stop("自动发送已禁用")
            self.local_danmaku.cancel()
            self._persist_session(updated)
            self.session = updated
            self.danmaku_send_error = None
            status = self.danmaku_send_status()
            await self.emit("danmaku_send_status", status)
            await self.state()
            return {"session": dict(self.session), "danmaku_send": status,
                    "danmaku_auto": self.danmaku_auto.snapshot()}

    async def set_danmaku_transport(self, mode):
        self.pending_danmaku_transport_updates += 1
        self.danmaku_auto.stop_requested = True
        self.local_danmaku.disconnect()
        try:
            async with self.lock:
                await self.danmaku_auto.stop("发送方式已更改")
                self.settings.data_dir.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".danmaku-transport-", dir=self.settings.data_dir)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump({"mode": mode}, stream)
                    os.replace(temporary, self.settings.data_dir / "danmaku_transport.json")
                finally:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary)
                self.danmaku_transport = mode
                self.danmaku_send_error = None
                status = self.danmaku_send_status()
                await self.emit("danmaku_send_status", status)
                return status
        finally:
            self.pending_danmaku_transport_updates -= 1

    def session_active(self, sid):
        return bool(self.session and self.session["id"] == sid
                    and self.session["status"] not in {"stopping", "stopping_failed", "stopped", "idle"})

    @property
    def active_path(self):
        return self.settings.data_dir / "active_session.json"

    def _remember_active(self):
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.active_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"session_id": self.session["id"],
            "room": str(self.room.room_id)}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.active_path)

    async def resume(self):
        if not self.active_path.is_file():
            return
        try:
            saved = json.loads(self.active_path.read_text())
            sid, room = saved["session_id"], saved["room"]
            if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{32}", sid):
                raise ValueError("Invalid saved session")
            await self.start(str(room), resume_id=sid)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.resume_error = "上次会话暂未恢复；历史记录和说话人标注仍保存在本地。"

    def snapshot(self):
        model = self.settings.model_config()
        data = self.agent.snapshot() if self.agent else {
            "observer": {"status": "idle", "last_timestamp": None, "dropped_frames": 0,
                         "model_ready": False},
            "observations": [], "memory": {"working": [], "episodes": [], "pins": []},
            "messages": [],
        }
        return {
            **data, "session": self.session or {"id": None, "status": "idle"},
            "room": asdict(self.room) if self.room else None,
            "playback": dict(self.playback), "danmaku": list(self.danmaku),
            "danmaku_status": self.danmaku_status,
            "model": {"name": model["model"], "configured": bool(model["base_url"]),
                      "error": getattr(self.settings, "model_error", None)},
            "audio": self.audio_snapshot(),
            "resume_error": self.resume_error,
            "danmaku_send": self.danmaku_send_status(),
            "sent_danmaku": [dict(item) for item in self.sent_danmaku],
            "danmaku_candidates": dict(self.danmaku_candidates),
            "danmaku_chorus": dict(self.danmaku_chorus),
            "danmaku_style": dict(self.danmaku_style),
            "danmaku_corpus": self.corpus_status(),
            "danmaku_auto": self.danmaku_auto.snapshot(),
        }

    def audio_snapshot(self):
        data = self.audio.snapshot() if self.audio else {
            "status": "idle", "segments": [], "speakers": [], "model_ready": False,
            "enabled": self.settings.audio_enabled}
        if self.capture:
            data["capture"] = {"chunks": self.capture.audio_chunks,
                "dropped_chunks": self.capture.audio_dropped_chunks,
                "skipped_media_segments": self.capture.audio_skipped_segments,
                "failures": self.capture.audio_failures,
                "error": self.capture.audio_last_error,
                "has_audio_stream": getattr(self.capture, "audio_stream_present", None),
                "timestamp_basis": self.capture.timestamp_basis}
            if self.settings.audio_enabled and data["capture"]["has_audio_stream"] is False:
                data["status"] = "no_audio_stream"
            if data["capture"]["error"]:
                data["capture_error"] = "音轨采集异常：" + data["capture"]["error"]
        return data

    async def emit(self, event_type, data):
        sid = self.session["id"] if self.session else None
        await self.bus.emit(event_type, data, sid)

    async def state(self):
        await self.emit("state", self.snapshot())

    @_fence_session_change
    async def start(self, room_input, *, resume_id=None, candidate_count=5, candidate_only=False,
                    allow_manual_send=False):
        async with self.lock:
            # A malformed new room must not stop a working session.
            room = await self.client.resolve_room(room_input.strip())
            saved = None
            if resume_id:
                if not re.fullmatch(r"[a-f0-9]{32}", resume_id):
                    raise ValueError("无效的历史会话")
                saved = json.loads((self.settings.data_dir / "sessions" / resume_id / "session.json").read_text())
                if saved["room"]["room_id"] != room.room_id:
                    raise ValueError("历史会话与直播间不一致")
                # Resume the saved policy instead of silently turning a
                # candidate-only run into a send-enabled default session.
                candidate_count = saved["session"].get("candidate_count", 5)
                candidate_only = saved["session"].get("candidate_only", False)
                allow_manual_send = saved["session"].get("allow_manual_send", False)
            if (type(candidate_count) is not int or not 1 <= candidate_count <= 10
                    or type(candidate_only) is not bool or type(allow_manual_send) is not bool):
                raise ValueError("候选会话配置无效，未启动或恢复会话。")
            await self._stop()
            self.danmaku_auto = AutoDanmaku(self)
            sid = resume_id or uuid.uuid4().hex
            directory = self.settings.data_dir / "sessions" / sid
            directory.mkdir(parents=True, exist_ok=bool(resume_id))
            self.clock_start = time.monotonic()
            self.room = room
            self.session = {"id": sid, "status": "connecting", "room_input": room_input,
                            "started_at": time.time(), "error": None,
                            "candidate_count": candidate_count, "candidate_only": candidate_only,
                            "allow_manual_send": allow_manual_send}
            if saved:
                started = float(saved["session"]["started_at"])
                if math.isfinite(started) and started <= time.time():
                    self.session["started_at"] = started
                    self.clock_start = time.monotonic() - (time.time() - started)
                self.session["resumed_at"] = time.time()
            self.resume_error = None
            self.danmaku.clear()
            self.sent_danmaku.clear()
            if saved:
                self._restore_sent_danmaku(directory)
            self.danmaku_candidates = {"items": [], "status": "waiting", "generated_at": None}
            self.danmaku_chorus = {"items": [], "session_id": sid, "updated_at": None}
            self.danmaku_send_error = None
            self.danmaku_status = {"status": "connecting"}
            self.playback = {
                "url": "", "type": "hls", "ready": False,
                "embed_url": f"https://www.bilibili.com/blackboard/live/live-activity-player.html?cid={room.room_id}&quality=0&danmaku=1",
            }
            self.model = VisionModel(**self.settings.model_config())
            journal = SessionJournal(directory)

            async def emit_for_session(kind, payload):
                # A cancelled old question cannot publish into the next room.
                if self.session_active(sid):
                    if kind in {"observation", "question_started", "plan", "answer_done", "error", "audio_segment", "audio_speakers"}:
                        try:
                            journal.append(kind, payload)
                        except OSError:
                            await self.bus.emit("error", {"source": "storage", "message": "观察日志写入失败，请检查磁盘空间。"}, sid)
                    await self.bus.emit(kind, payload, sid)
                    # Speaker profiles are independent knowledge, including
                    # bindings whose earlier audio evidence has expired.
                    if kind == "audio_speakers" and self.agent:
                        self.agent.add_audio_speakers(payload.get("speakers", []))
                    if kind in {"audio_speakers", "audio_segment"} and self.agent:
                        await self.bus.emit("memory", self.agent.snapshot()["memory"], sid)

            self.agent = LiveAgent(sid, directory / "agent", self.model, emit_for_session,
                observation_frames=self.settings.observation_frames,
                observation_stride=self.settings.observation_stride,
                observation_concurrency=self.settings.observation_concurrency,
                adaptive_observation=self.settings.adaptive_observation,
                observation_max_frames=self.settings.observation_max_frames,
                observation_target_lag=self.settings.observation_target_lag,
                session_clock_start=self.clock_start,
                audio_alignment_wait_seconds=(self.settings.audio_alignment_wait_seconds
                    if self.settings.audio_enabled and self.settings.audio_config().get("base_url") else 0))
            if saved:
                self.agent.restore_from_state()
            await self.agent.start()
            self.audio = AudioPipeline(sid, directory / "audio", self.settings.audio_config,
                emit_for_session, self.agent.add_audio_segment, enabled=self.settings.audio_enabled,
                on_progress=self.agent.add_audio_progress)
            if saved:
                self.audio.restore_private_state()
                self.agent.add_audio_speakers(self.audio.tracker.public())
                if self.settings.audio_enabled:
                    # Legacy voice references have a bounded rebuild budget;
                    # finish it before live audio competes for the ASR service.
                    await self.audio.rebuild_missing_references()
            await self.audio.start()
            self._persist_session(self.session)
            self.tasks = [asyncio.create_task(self._stream_loop(sid, directory)),
                          asyncio.create_task(self._danmaku_loop(sid)),
                          asyncio.create_task(self._model_loop(sid)),
                          asyncio.create_task(self._candidate_loop(sid)),
                          asyncio.create_task(self._chorus_loop(sid))]
            self._remember_active()
            await self.state()
            return self.snapshot()

    @_fence_session_change
    async def stop(self):
        async with self.lock:
            await self._stop()
            self.active_path.unlink(missing_ok=True)
            await self.state()
        return self.snapshot()

    async def _stop(self):
        # Fence old producers before the first await. A cancelled receive can
        # still finish, and a slow cleanup must never look like a live session.
        if self.session:
            self.session.update(status="stopping", error=None)
        self.playback["ready"] = False
        self.danmaku_status = {"status": "disconnected"}
        self.danmaku_auto.stop_requested = True
        self.local_danmaku.cancel()
        await self.state()
        if not self._shutdown_pending:
            self._shutdown_failed = False
            tasks, self.tasks = self.tasks, []
            self._shutdown_background = set(tasks)
            for task in tasks:
                task.cancel()
            cleanup = [("auto", self.danmaku_auto.stop("直播会话已结束或服务正在重启"))]
            for name, component, method in (("capture", self.capture, "stop"),
                                             ("audio", self.audio, "stop"),
                                             ("agent", self.agent, "stop"),
                                             ("model", self.model, "close")):
                if component:
                    cleanup.append((name, getattr(component, method)()))
            self._shutdown_pending = set(tasks) | {
                asyncio.create_task(coro, name=f"session-stop:{name}") for name, coro in cleanup}
        else:
            # Retry the same old resources; never lose ownership of a task
            # that ignored cancellation or create a new room on top of it.
            for task in self._shutdown_background:
                if not task.done():
                    task.cancel()
        tracked = set(self._shutdown_pending)
        try:
            _, pending = await asyncio.wait(tracked, timeout=self.shutdown_timeout)
            if pending:
                for task in pending & self._shutdown_background:
                    task.cancel()
                _, pending = await asyncio.wait(pending, timeout=self.shutdown_retry_timeout)
        except asyncio.CancelledError:
            # An HTTP disconnect must not erase cleanup ownership. The next
            # Stop/Connect request can finish it and release the workspace lock.
            if self.session:
                self.session.update(status="stopping_failed", error="停止被中断，请再次停止后重连。")
            raise
        earlier_failure = self._shutdown_failed
        failed = [task for task in tracked if task.done() and not task.cancelled() and task.exception()]
        self._shutdown_failed = earlier_failure or bool(failed)
        self._shutdown_pending = pending
        if pending or failed:
            message = "旧直播会话尚未完全停止，请再次停止后重连。"
            if self.session:
                self.session.update(status="stopping_failed", error=message)
            await self.state()
            raise RuntimeError(message)
        if earlier_failure:
            # Some components failed while other tasks were still pending on
            # an earlier attempt. Draining those tasks alone is insufficient:
            # run component cleanup again before allowing the next room.
            return await self._stop()
        self._shutdown_background.clear()
        self.capture = None
        if self.session:
            self.session.update(status="stopped", stopped_at=time.time())
            self._update_chorus(self.session["id"])
        self.playback["ready"] = False
        self.danmaku_status = {"status": "disconnected"}

    def _persist_sent_danmaku(self):
        directory = self.settings.data_dir / "sessions" / self.session["id"]
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".sent-danmaku-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"session_id": self.session["id"], "room_id": self.room.room_id,
                           "items": list(self.sent_danmaku)}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, directory / "sent_danmaku.private.json")
        finally:
            with suppress(OSError):
                os.unlink(temporary)

    def _restore_sent_danmaku(self, directory):
        """Only confirmed sends from this session/room become public history."""
        items = []
        migrated = False
        for name, legacy in (("sent_danmaku.private.json", False), ("danmaku_auto.private.json", True)):
            try:
                with (directory / name).open(encoding="utf-8") as handle:
                    saved = json.loads(handle.read(32769))
                if (not isinstance(saved, dict) or saved.get("session_id") != self.session["id"]
                        or saved.get("room_id") != self.room.room_id):
                    continue
                records = saved.get("history" if legacy else "items", [])
                if not isinstance(records, list):
                    continue
                for item in records[-30:]:
                    if not isinstance(item, dict) or (legacy and item.get("status") != "accepted"):
                        continue
                    text, sent_at = item.get("text"), item.get("sent_at")
                    source = "auto" if legacy else item.get("source")
                    if (not isinstance(text, str) or not text.strip() or len(text) > 1000
                            or type(sent_at) not in (int, float) or not math.isfinite(sent_at)
                            or sent_at < self.session["started_at"] or source not in {"manual", "auto"}):
                        continue
                    timestamp = sent_at - self.session["started_at"] if legacy else item.get("timestamp")
                    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
                        continue
                    # The trial writes its own success time just after the
                    # shared send path. Do not display that acknowledgement twice.
                    if legacy and any(entry["source"] == "auto" and entry["text"] == text.strip()
                                      and abs(entry["sent_at"] - sent_at) < 3 for entry in items):
                        continue
                    items.append({"text": text.strip(), "timestamp": timestamp,
                                  "sent_at": sent_at, "source": source})
                    migrated = migrated or legacy
            except (OSError, ValueError, TypeError, UnicodeError):
                continue
        self.sent_danmaku.extend(sorted(items, key=lambda item: item["sent_at"])[-30:])
        if migrated:
            # Starting another trial replaces its legacy file, so migrate now
            # even if no new message is sent before the next server restart.
            with suppress(OSError):
                self._persist_sent_danmaku()

    async def _send_danmaku_locked(self, session_id, text, *, before_post=None, source="manual"):
        """One send path for manual clicks and explicitly authorized trials."""
        self.require_danmaku_send_enabled(source)
        if source not in {"manual", "auto"}:
            raise ValueError("Unknown danmaku source")
        if self.pending_danmaku_transport_updates or self.pending_session_changes:
            raise DanmakuSendError("直播间或发送方式正在更改，请稍后重新操作。", 409, "trial_stopped")
        if not self.session or session_id != self.session["id"]:
            raise DanmakuSendError("直播会话已变更，请检查房间后重新发送。", 409)
        if not self.session_active(session_id) or not self.room or self.room.live_status != 1:
            raise DanmakuSendError("请先连接正在直播的房间。", 409)
        if self.last_danmaku_sent_at is not None and time.monotonic() - self.last_danmaku_sent_at < 3:
            raise DanmakuSendError("发送过于频繁，请稍等几秒。", 429, "rate_limited")
        transport, room_id = self.danmaku_transport, self.room.room_id
        def guard():
            self.require_danmaku_send_enabled(source)
            if (self.danmaku_transport != transport or self.pending_danmaku_transport_updates
                    or self.pending_session_changes or not self.session_active(session_id)
                    or not self.room or self.room.room_id != room_id or self.room.live_status != 1
                    or (transport == "local" and self.session["status"] != "live")):
                raise DanmakuSendError("直播间或发送方式已变更，未发送。", 409, "trial_stopped")
            if before_post is not None:
                before_post()
        try:
            if transport == "local":
                result = await self.local_danmaku.send(room_id, text, guard=guard, source=source)
            else:
                result = await self.danmaku_sender.send(room_id, text, before_post=guard)
        except DanmakuSendError as exc:
            self.danmaku_send_error = str(exc)
            await self.emit("danmaku_send_status", self.danmaku_send_status())
            raise
        if not self.session or self.session["id"] != session_id:
            raise DanmakuSendError("发送回执对应的直播会话已变更，请先核对原直播间，避免重复发送。", 409, "send_unknown")
        self.last_danmaku_sent_at = time.monotonic()
        self.sent_danmaku.append({"timestamp": self.last_danmaku_sent_at - self.clock_start,
                                  "text": result.get("text", text).strip(),
                                  "sent_at": time.time(), "source": source})
        self.danmaku_send_error = None
        try:
            self._persist_sent_danmaku()
        except OSError:
            # Delivery already succeeded. Never turn a local write error into
            # a failed send response that could encourage a duplicate retry.
            await self.emit("error", {"source": "storage", "message": "弹幕已发送，但记录保存失败；重启后可能无法恢复。"})
        await self.emit("sent_danmaku", {"items": [dict(item) for item in self.sent_danmaku]})
        await self.emit("danmaku_send_status", self.danmaku_send_status())
        return result

    async def ensure_model(self):
        """Pick up the first deployed endpoint without interrupting the session."""
        async with self.model_lock:
            if not self.model or self.model.configured:
                return
            config = self.settings.model_config()
            if config["base_url"]:
                previous = self.model
                self.model = VisionModel(**config)
                self.agent.model = self.model
                await previous.close()
                await self.state()

    async def _model_loop(self, sid):
        while self.session_active(sid):
            await self.ensure_model()
            if self.model.configured:
                return
            await asyncio.sleep(3)

    def corpus_status(self):
        status = dict(self.danmaku_corpus.snapshot())
        if self._corpus_task and not self._corpus_task.done():
            status["status"] = "loading"
        elif self._corpus_error:
            status["status"] = "error"
        return status

    def _request_corpus_refresh(self, force=False):
        """Only local mode opts into disk access; indexing never awaits on the live path."""
        if self.danmaku_style.get("mode", "builtin") != "local":
            return None
        if self._corpus_task and not self._corpus_task.done():
            return self._corpus_task
        now = time.monotonic()
        if not force and self._corpus_last_check is not None and now - self._corpus_last_check < 5:
            return None
        self._corpus_last_check = now
        self._corpus_task = asyncio.create_task(self._refresh_corpus())
        return self._corpus_task

    async def _refresh_corpus(self):
        try:
            changed = await asyncio.to_thread(self.danmaku_corpus.refresh)
            self._corpus_error = False
        except Exception:
            changed = False
            self._corpus_error = True
        # Emit the completed state, not the still-running wrapper task state.
        status = dict(self.danmaku_corpus.snapshot())
        if self._corpus_error:
            status["status"] = "error"
        await self.emit("danmaku_corpus", status)
        if changed and self.danmaku_style.get("mode", "builtin") == "local":
            self._candidate_refresh.set()

    async def set_danmaku_style(self, session_id, value, mode=None):
        """Update preference under the room lock; invalidate drafts before waking generation."""
        if not self.session or self.session["id"] != session_id:
            raise DanmakuSendError("直播会话已变更，请在当前房间调整风格。", 409)
        if type(value) is not int or not 0 <= value <= 100:
            raise DanmakuSendError("弹幕风格请输入 0–100 的整数。")
        mode = self.danmaku_style.get("mode", "builtin") if mode is None else mode
        if mode not in {"builtin", "local"}:
            raise DanmakuSendError("请选择内置风格或本地语料。")
        if value != self.danmaku_style["value"] or mode != self.danmaku_style.get("mode", "builtin"):
            try:
                target = self.settings.data_dir / "danmaku_preferences.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(".tmp")
                temp.write_text(json.dumps({"style_value": value, "mode": mode}), encoding="utf-8")
                temp.replace(target)
            except OSError:
                raise DanmakuSendError("无法保存弹幕风格，请检查数据目录。", 503) from None
            self.danmaku_style = {"value": value, "revision": self.danmaku_style["revision"] + 1, "mode": mode}
            self.danmaku_candidates = {"items": [], "status": "waiting", "generated_at": None,
                "session_id": session_id, "style_revision": self.danmaku_style["revision"]}
            self._candidate_refresh.set()
            await self.emit("danmaku_style", dict(self.danmaku_style))
            await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
            self._request_corpus_refresh(force=True)
        return {"danmaku_style": dict(self.danmaku_style),
                "danmaku_candidates": dict(self.danmaku_candidates),
                "danmaku_corpus": self.corpus_status()}

    async def _candidate_pause(self, seconds):
        """Keep the normal cadence, but allow a changed style to wake it early."""
        sleeping = asyncio.create_task(asyncio.sleep(seconds))
        refreshing = asyncio.create_task(self._candidate_refresh.wait())
        try:
            await asyncio.wait((sleeping, refreshing), return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeping.cancel()
            refreshing.cancel()
            await asyncio.gather(sleeping, refreshing, return_exceptions=True)

    def _update_chorus(self, sid):
        """Keep live audience repetitions separate from the five model drafts."""
        if not self.session or self.session["id"] != sid:
            return False
        before = self.danmaku_chorus
        active = (self.session_active(sid)
                  and self.room and self.room.live_status == 1)
        matches = detect_choruses(list(self.danmaku),
            end=time.monotonic() - self.clock_start,
            max_length=MAX_DANMAKU_LENGTH) if active else []
        old_items = ({item["text"]: item for item in before["items"]}
                     if before["session_id"] == sid else {})
        items = []
        now = time.time()
        for match in matches[:3]:
            old = old_items.get(match["text"])
            # Preserve IDs and update timestamps when the evidence changes.
            # source_time is the latest actual matching audience event, not
            # this ticker's time, so automated selection can assess its age.
            if old and all(old.get(key) == value for key, value in match.items()):
                items.append(old)
            else:
                items.append({**match, "id": old["id"] if old else uuid.uuid4().hex,
                              "kind": "chorus", "updated_at": now})
        if items == before["items"] and before["session_id"] == sid:
            return False
        self.danmaku_chorus = {"items": items, "session_id": sid, "updated_at": now}
        return True

    async def _refresh_chorus(self, sid):
        if self._update_chorus(sid):
            await self.emit("danmaku_chorus", dict(self.danmaku_chorus))

    async def _chorus_loop(self, sid):
        """Expire repetitions even while a model request or question is pending."""
        while self.session_active(sid):
            await self._refresh_chorus(sid)
            await asyncio.sleep(1)

    async def _candidate_loop(self, sid):
        """Refresh drafts from actual recent sources; never calls the send API."""
        previous = None
        deferred_since = None
        while self.session_active(sid):
            self._candidate_refresh.clear()
            self._request_corpus_refresh()
            started = time.monotonic()
            allow_observations_busy = False
            if self.settings.adaptive_observation and self.agent and self.model and self.model.configured:
                observer = self.agent._observer_snapshot()
                age = observer.get("understanding_age_seconds")
                busy = (age is None or age > self.settings.observation_target_lag * 0.65
                        or observer.get("active_questions", 0)
                        or not getattr(self.model, "background_available", True))
                if busy:
                    if deferred_since is None:
                        deferred_since = started
                    # Yield normally, but do not permanently disable drafts on
                    # a slower machine. At most one bounded background request
                    # may join busy observations after 30s; questions still win.
                    if started - deferred_since < 30 or observer.get("active_questions", 0):
                        if self.danmaku_candidates.get("status") != "deferred":
                            self.danmaku_candidates = {**self.danmaku_candidates, "status": "deferred", "error": None}
                            await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
                        await self._candidate_pause(2)
                        continue
                    allow_observations_busy = True
                    deferred_since = started
                else:
                    deferred_since = None
            evidence = self.snapshot()
            # Reuse the agent's bounded, session-scoped audience record, which
            # also restores with a resumed session. Do not enlarge UI snapshots.
            evidence["danmaku_history"] = list(getattr(self.agent, "danmaku", evidence.get("danmaku", [])))
            evidence["sent_danmaku"] = list(self.sent_danmaku)
            evidence["danmaku_style"] = dict(self.danmaku_style)
            style_revision = self.danmaku_style["revision"]
            context = candidate_context(evidence, source_time=time.monotonic() - self.clock_start)
            corpus_revision = self.danmaku_corpus.snapshot()["revision"]
            if context["style_mode"] == "local":
                # The bounded in-memory selector cannot read source files or
                # make model calls. References are expression examples only.
                context["style_references"] = self.danmaku_corpus.select(context, context["style_value"])
            signature = json.dumps(context, ensure_ascii=False, sort_keys=True)
            if not self.room or self.room.live_status != 1 or not self.model or not self.model.configured or not any(
                    context.get(key) for key in ("visual", "audio_transcripts", "audience_comments")):
                self.danmaku_candidates = {"items": [], "generated_at": None, "status": "waiting"}
                await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
                await self._candidate_pause(3)
                continue
            if signature != previous:
                self.danmaku_candidates = {**self.danmaku_candidates, "status": "generating", "error": None}
                await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
                try:
                    texts = await asyncio.wait_for(generate_candidates(self.model, context,
                        max_length=self.danmaku_credentials.status()["max_length"],
                        count=self.session.get("candidate_count", 5),
                        allow_observations_busy=allow_observations_busy), timeout=10)
                    if not self.session_active(sid):
                        return
                    if (style_revision != self.danmaku_style["revision"] or
                            (context["style_mode"] == "local" and
                             corpus_revision != self.danmaku_corpus.snapshot()["revision"])):
                        continue  # A late response must not resurrect the old style.
                    self.danmaku_candidates = {"items": [{"id": uuid.uuid4().hex, "text": text}
                                                         for text in texts],
                        "generated_at": time.time(), "source_time": context["source_time"], "session_id": sid,
                        "requested_count": self.session.get("candidate_count", 5),
                        "style_revision": style_revision, "style_value": self.danmaku_style["value"],
                        "reference_mode": "local" if context.get("style_references") else "builtin",
                        "reference_count": len(context.get("style_references", [])),
                        "history_count": len(context.get("audience_history", [])),
                        "recently_sent_count": len(context.get("recently_sent", [])),
                        "sources": [key for key in ("visual", "audio_transcripts", "audience_comments")
                                    if context[key]], "status": "ready", "error": None}
                    previous = signature
                except asyncio.CancelledError:
                    raise
                except ModelBusy:
                    if not self.session_active(sid):
                        return
                    if style_revision != self.danmaku_style["revision"]:
                        continue
                    self.danmaku_candidates = {**self.danmaku_candidates, "status": "deferred", "error": None}
                except Exception:
                    if not self.session_active(sid):
                        return
                    if style_revision != self.danmaku_style["revision"]:
                        continue
                    self.danmaku_candidates = {**self.danmaku_candidates, "status": "error",
                        "error": "候选暂未更新，稍后自动重试；可以直接编辑弹幕。"}
                await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
            await self._candidate_pause(max(1, 12 - (time.monotonic() - started)))

    async def _stream_loop(self, sid, directory):
        media_root = directory / "media"
        generations = [int(p.name[1:]) for p in media_root.iterdir()
                       if p.is_dir() and re.fullmatch(r"g[0-9]+", p.name)] if media_root.exists() else []
        generation = max(generations, default=0)
        backoff = 3
        while self.session_active(sid):
            try:
                room = await self.client.resolve_room(self.room.room_id)
                if not self.session_active(sid):
                    return
                self.room = room
                if self.room.live_status != 1:
                    self.session.update(status="waiting", error=None)
                    self.playback["ready"] = False
                    await self.state()
                    await asyncio.sleep(self.settings.room_poll_seconds)
                    continue
                if not self.settings.ffmpeg_available:
                    raise RuntimeError("未找到 FFmpeg。官方播放器仍可观看，安装 FFmpeg 后重新连接即可启用观察。")
                sources = await self.client.get_play_sources(self.room.room_id)
                if not self.session_active(sid):
                    return
                if not sources:
                    raise RuntimeError("直播平台暂未提供可读取的视频流")
                # On reconnect rotate through returned CDNs before fetching again.
                source = sources[generation % len(sources)]
                generation += 1
                media_dir = directory / "media" / f"g{generation}"
                media_dir.mkdir(parents=True, exist_ok=True)
                # Clean failed attempts too; a failing CDN must not fill disk.
                for old in media_dir.parent.iterdir():
                    if old.is_dir() and not old.is_symlink() and re.fullmatch(r"g[0-9]+", old.name):
                        if int(old.name[1:]) < generation - 1:
                            shutil.rmtree(old)
                self.capture = MediaCapture(self.settings.ffmpeg_bin, media_dir,
                    self.settings.sample_interval, self.clock_start,
                    timeline_floor=(self.agent._latest_offered_timestamp or 0) + self.settings.sample_interval)
                capture = self.capture
                self.session.update(status="live", error=None)
                self.playback.update(url=f"/api/media/{sid}/g{generation}/index.m3u8", ready=False)
                await self.state()
                task = asyncio.create_task(capture.run(source, self.agent.offer_frame,
                    self.audio.offer if self.audio and self.settings.audio_enabled else None))
                last_poll = time.monotonic()
                last_audio_status = None
                try:
                    while not task.done() and self.session_active(sid):
                        if capture.ready and not self.playback["ready"]:
                            self.playback["ready"] = True
                            await self.emit("playback", dict(self.playback))
                            backoff = 3
                        audio_status = (capture.audio_failures, getattr(capture, "audio_stream_present", None))
                        if audio_status != last_audio_status:
                            last_audio_status = audio_status
                            self.agent.audio_alignment_wait_seconds = (
                                self.settings.audio_alignment_wait_seconds
                                if self.settings.audio_enabled and self.settings.audio_config().get("base_url")
                                and capture.audio_stream_present is not False else 0)
                            await self.emit("audio_status", {k: v for k, v in self.audio_snapshot().items()
                                if k not in {"segments", "speakers"}})
                        if time.monotonic() - last_poll >= self.settings.room_poll_seconds:
                            current = await self.client.resolve_room(self.room.room_id)
                            if not self.session_active(sid):
                                break
                            last_poll = time.monotonic()
                            self.room = current
                            if current.live_status != 1:
                                break
                        await asyncio.sleep(1)
                    if task.done():
                        await task
                finally:
                    task.cancel()
                    await capture.stop()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
                self.playback["ready"] = False
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.session_active(sid):
                    return
                self.session.update(status="connecting", error=self.safe_error(exc))
                self.playback["ready"] = False
                await self.emit("error", {"source": "video", "message": self.session["error"]})
                await self.state()
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def _danmaku_loop(self, sid):
        try:
            async for event in self.client.events(self.room.room_id):
                if not self.session_active(sid):
                    break
                kind = event.get("type", "danmaku")
                if kind == "danmaku":
                    event["timestamp"] = time.monotonic() - self.clock_start
                    self.danmaku.append(event)
                    if hasattr(self.agent, "add_danmaku"):
                        result = self.agent.add_danmaku(event)
                        if asyncio.iscoroutine(result):
                            await result
                    await self.emit("danmaku", event)
                    await self._refresh_chorus(sid)
                elif kind == "connection":
                    self.danmaku_status = event
                    await self.state()
                else:
                    await self.emit(kind, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.session_active(sid):
                return
            self.danmaku_status = {"status": "error", "reason": self.safe_error(exc)}
            await self.emit("error", {"source": "danmaku", "message": self.safe_error(exc)})
            await self.state()

    def safe_error(self, exc):
        text = str(exc)
        for secret in (self.settings.bilibili_cookie, self.settings.model_config().get("api_key"),
                       self.settings.audio_config().get("api_key")):
            if secret:
                text = text.replace(secret, "[private]")
        return text[:400]

    @_fence_session_change
    async def close(self):
        # Process shutdown preserves the active-session pointer. Only the
        # browser's explicit Disconnect prevents reconnection on the next boot.
        self.local_danmaku.disconnect()
        async with self.lock:
            await self._stop()
        if self._corpus_task:
            with suppress(asyncio.CancelledError, Exception):
                await self._corpus_task
        await self.client.aclose()
        await self.danmaku_sender.close()


def create_app(settings=None):
    workspace = Workspace(settings or Settings.from_env())

    @asynccontextmanager
    async def lifespan(_):
        try:
            await workspace.resume()
            yield
        finally:
            await workspace.close()

    app = FastAPI(title="DeltaStreamer Live", lifespan=lifespan)
    app.state.workspace = workspace

    @app.middleware("http")
    async def local_origin(request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "PUT", "DELETE", "PATCH"} and origin:
            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin changes are not allowed"}, status_code=403)
        return await call_next(request)

    @app.get("/api/health")
    async def health():
        config = workspace.settings.model_config()
        return {"ok": True, "ffmpeg": workspace.settings.ffmpeg_available,
                "model": {"name": config["model"], "configured": bool(config["base_url"])},
                "sample_interval": workspace.settings.sample_interval,
                "observation_frames": workspace.settings.observation_frames,
                "audio": {"enabled": workspace.settings.audio_enabled,
                          "configured": bool(workspace.settings.audio_config()["base_url"])},
                "observation_stride": workspace.settings.observation_stride,
                "observation_concurrency": workspace.settings.observation_concurrency,
                "adaptive_observation": workspace.settings.adaptive_observation,
                "observation_max_frames": workspace.settings.observation_max_frames,
                "observation_target_lag": workspace.settings.observation_target_lag}

    @app.get("/api/state")
    async def state():
        return workspace.snapshot()

    @app.post("/api/session")
    async def start_session(payload: SessionRequest):
        try:
            return await workspace.start(payload.room, candidate_count=payload.candidate_count,
                                         candidate_only=payload.candidate_only,
                                         allow_manual_send=payload.allow_manual_send)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, workspace.safe_error(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "直播平台连接失败，请稍后重试。") from exc

    @app.post("/api/session/stop")
    async def stop_session():
        try:
            return await workspace.stop()
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/session/danmaku-policy")
    async def update_danmaku_policy(payload: DanmakuPolicyRequest):
        try:
            return await workspace.set_danmaku_policy(payload.session_id, payload.allow_manual_send)
        except DanmakuSendError as exc:
            return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)
        except OSError:
            return JSONResponse({"detail": "无法保存发送策略，权限未更改。", "code": "storage_unavailable"}, status_code=503)

    @app.post("/api/danmaku/credentials")
    async def save_danmaku_credentials(request: Request):
        if workspace.danmaku_transport == "local" or workspace.pending_danmaku_transport_updates:
            raise HTTPException(409, "本机发送模式请在自己电脑的助手中配置 Cookie，不要上传到服务器。")
        # Parse this secret separately: ordinary validation errors may echo the
        # rejected input. Neither success nor failure returns cookie contents.
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 16384:
                raise HTTPException(400, "登录信息过长，请仅粘贴 Cookie 内容。")
        try:
            value = json.loads(body)
            cookie = value.get("cookie") if isinstance(value, dict) else None
            if not isinstance(cookie, str):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(400, "请提供有效的 Cookie 文本。") from None
        async with workspace.lock:
            if workspace.danmaku_transport == "local" or workspace.pending_danmaku_transport_updates:
                raise HTTPException(409, "本机发送模式请在自己电脑的助手中配置 Cookie，不要上传到服务器。")
            try:
                if cookie.strip():
                    workspace.danmaku_credentials.save(cookie)
                else:
                    workspace.danmaku_credentials.clear()
            except (ValueError, DanmakuSendError) as exc:
                # Credential validation messages are intentionally fixed.
                raise HTTPException(400, "登录信息无效，请检查本人 Cookie 中的 SESSDATA 和 bili_jct。") from exc
            await workspace.danmaku_auto.stop("弹幕登录配置已更改")
            workspace.danmaku_send_error = None
            status = workspace.danmaku_send_status()
            await workspace.emit("danmaku_send_status", status)
            return status

    @app.post("/api/danmaku/send")
    async def send_danmaku(payload: DanmakuRequest):
        transport = workspace.danmaku_transport
        lock = workspace.local_danmaku_send_lock if transport == "local" else workspace.lock
        async with lock:
            try:
                if transport != workspace.danmaku_transport:
                    raise DanmakuSendError("发送方式已更改，请检查后重新操作。", 409, "trial_stopped")
                return await workspace._send_danmaku_locked(payload.session_id, payload.text)
            except DanmakuSendError as exc:
                return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)

    def local_error(exc):
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code,
                            headers={"Cache-Control": "no-store"})

    def require_local():
        if workspace.danmaku_transport != "local" or workspace.pending_danmaku_transport_updates:
            raise DanmakuSendError("请先在网页切换为本机发送模式。", 409, "local_mode_required")

    def helper_token(request):
        authorization = request.headers.get("authorization", "")
        return authorization[7:] if authorization.startswith("Bearer ") else None

    async def helper_body(request, fields):
        # Do not use model validation for pairing secrets: its error payload can
        # include rejected input. Unknown fields are never reflected or stored.
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 8192:
                raise DanmakuSendError("本机助手请求格式无效。", 400, "local_protocol_error")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise DanmakuSendError("本机助手请求格式无效。", 400, "local_protocol_error") from None
        if not isinstance(payload, dict) or set(payload) - fields:
            raise DanmakuSendError("本机助手请求格式无效。", 400, "local_protocol_error")
        return payload

    @app.post("/api/danmaku/transport")
    async def danmaku_transport(payload: DanmakuTransportRequest):
        try:
            return await workspace.set_danmaku_transport(payload.mode)
        except OSError:
            return local_error(DanmakuSendError("无法保存发送方式，请检查本地数据目录。", 503, "storage_unavailable"))

    @app.post("/api/danmaku/local/pairing")
    async def local_pairing():
        try:
            require_local()
            return JSONResponse(workspace.local_danmaku.pairing(), headers={"Cache-Control": "no-store"})
        except DanmakuSendError as exc:
            return local_error(exc)

    @app.post("/api/danmaku/local/connect")
    async def local_connect(request: Request):
        try:
            payload = await helper_body(request, {"code"})
            require_local()
            result = workspace.local_danmaku.connect(payload.get("code"))
            await workspace.danmaku_auto.stop("本机助手连接已更新，请手动启动一键融入")
            await workspace.emit("danmaku_send_status", workspace.danmaku_send_status())
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except DanmakuSendError as exc:
            return local_error(exc)

    @app.post("/api/danmaku/local/poll")
    async def local_poll(request: Request):
        try:
            require_local()
            workspace.require_danmaku_send_enabled()
            return JSONResponse(workspace.local_danmaku.poll(helper_token(request)), headers={"Cache-Control": "no-store"})
        except DanmakuSendError as exc:
            return local_error(exc)

    @app.post("/api/danmaku/local/authorize")
    async def local_authorize(request: Request):
        try:
            payload = await helper_body(request, {"job_id"})
            require_local()
            workspace.require_danmaku_send_enabled()
            # The pending send may own its send lock. Never acquire the global
            # workspace lock here; authorization only executes synchronous guards.
            return workspace.local_danmaku.authorize(helper_token(request), payload.get("job_id"))
        except DanmakuSendError as exc:
            return local_error(exc)

    @app.post("/api/danmaku/local/result")
    async def local_result(request: Request):
        try:
            payload = await helper_body(request, {"job_id", "outcome", "code", "receipt"})
            require_local()
            if not isinstance(payload.get("outcome"), str) or not isinstance(payload.get("code"), str):
                raise DanmakuSendError("本机助手回执格式无效。", 400, "local_protocol_error")
            result = workspace.local_danmaku.result(helper_token(request), payload.get("job_id"),
                                                   payload["outcome"], payload["code"], payload.get("receipt"))
            await workspace.emit("danmaku_send_status", workspace.danmaku_send_status())
            return result
        except DanmakuSendError as exc:
            return local_error(exc)

    @app.post("/api/danmaku/local/disconnect")
    async def local_disconnect():
        workspace.local_danmaku.disconnect()
        await workspace.danmaku_auto.stop("本机助手已断开")
        status = workspace.danmaku_send_status()
        await workspace.emit("danmaku_send_status", status)
        return status

    @app.get("/api/danmaku/local/download")
    async def local_download():
        from .local_sender_bundle import build_local_sender_bundle
        return Response(build_local_sender_bundle(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="ai-tongdan-local-sender.zip"',
                                 "Cache-Control": "no-store"})

    @app.get("/api/danmaku/local/guide")
    async def local_guide():
        path = ROOT / "docs" / "local-sender.md"
        if not path.is_file():
            raise HTTPException(404, "本机助手使用指南暂不可用。")
        return FileResponse(path, media_type="text/plain; charset=utf-8", headers={"Cache-Control": "no-store"})

    @app.post("/api/danmaku/style")
    async def update_danmaku_style(payload: DanmakuStyleRequest):
        # Like Stop, a valid style-change request arriving during an upstream
        # lookup must block a not-yet-started POST before it can acquire the lock.
        pending = bool(workspace.session and payload.session_id == workspace.session["id"])
        if pending:
            workspace.pending_danmaku_style_updates += 1
        try:
            async with workspace.lock:
                try:
                    return await workspace.set_danmaku_style(payload.session_id, payload.style_value, payload.mode)
                except DanmakuSendError as exc:
                    raise HTTPException(exc.status_code, str(exc)) from None
        finally:
            if pending:
                workspace.pending_danmaku_style_updates -= 1

    @app.post("/api/danmaku/auto/start")
    async def start_auto_danmaku(payload: AutoDanmakuRequest):
        async with workspace.lock:
            try:
                workspace.require_danmaku_send_enabled("auto")
                return await workspace.danmaku_auto.start(
                    payload.session_id, interval_seconds=payload.interval_seconds,
                    max_messages=payload.max_messages)
            except DanmakuSendError as exc:
                return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)

    @app.post("/api/danmaku/auto/stop")
    async def stop_auto_danmaku(payload: AutoDanmakuStopRequest):
        # Signal before acquiring the lock: an in-progress room lookup must
        # not start a POST after the user has already pressed Stop.
        if workspace.session and payload.session_id == workspace.session["id"]:
            workspace.danmaku_auto.stop_requested = True
            workspace.local_danmaku.cancel(source="auto")
        async with workspace.lock:
            if not workspace.session or payload.session_id != workspace.session["id"]:
                raise HTTPException(409, "直播会话已变更，请刷新当前房间。")
            return await workspace.danmaku_auto.stop()

    @app.post("/api/questions", status_code=202)
    async def question(payload: QuestionRequest):
        async with workspace.lock:
            if not workspace.session or payload.session_id != workspace.session["id"]:
                raise HTTPException(409, "直播会话已变更，请重新提问。")
            if not workspace.session_active(payload.session_id):
                raise HTTPException(409, "请先连接直播间。")
            if not payload.text.strip():
                raise HTTPException(400, "请输入问题。")
            await workspace.ensure_model()
            if not workspace.model or not workspace.model.configured:
                raise HTTPException(503, "模型服务尚未配置。")
            try:
                value = workspace.agent.ask(payload.text.strip())
                if asyncio.iscoroutine(value):
                    value = await value
                return {"question_id": value}
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(429, str(exc)) from exc

    @app.post("/api/questions/{question_id}/cancel")
    async def cancel(question_id: str):
        if workspace.agent:
            await workspace.agent.cancel_question(question_id)
        return {"ok": True}

    def validate_audio_session(sid):
        if not workspace.session or sid != workspace.session["id"]:
            raise HTTPException(409, "直播会话已变更，请重新操作。")
        if not workspace.session_active(sid) or not workspace.audio:
            raise HTTPException(409, "请先连接直播间。")

    @app.post("/api/audio/segments/{segment_id}/label")
    async def label_audio_segment(segment_id: str, payload: AudioLabelRequest):
        async with workspace.lock:
            validate_audio_session(payload.session_id)
            try:
                return await workspace.audio.label_segment(segment_id, payload.role)
            except KeyError as exc:
                raise HTTPException(410, "音频片段已超出保留窗口。") from exc

    @app.post("/api/audio/speakers/{speaker_id}/label")
    async def label_audio_speaker(speaker_id: str, payload: AudioLabelRequest):
        async with workspace.lock:
            validate_audio_session(payload.session_id)
            try:
                return await workspace.audio.label_speaker(speaker_id, payload.role, payload.label)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc

    @app.post("/api/audio/speakers/{speaker_id}/merge")
    async def merge_audio_speakers(speaker_id: str, payload: SpeakerMergeRequest):
        async with workspace.lock:
            validate_audio_session(payload.session_id)
            try:
                return await workspace.audio.merge_speakers(speaker_id, payload.target_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.post("/api/model/check")
    async def check_model():
        config = workspace.settings.model_config()
        if not config["base_url"]:
            raise HTTPException(503, getattr(workspace.settings, "model_error", None) or "模型服务尚未配置。")
        model = VisionModel(**config)
        parts = []
        for color in ("red", "blue"):
            buffer = BytesIO()
            Image.new("RGB", (64, 64), color).save(buffer, format="JPEG")
            parts.append({"type": "image_url", "image_url": {"url":
                "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()}})
        parts.append({"type": "text", "text": "Name the dominant color of the first and second image in that order. Answer briefly."})
        started = time.monotonic()
        try:
            answer = await model.complete([{"role": "user", "content": parts}], max_tokens=80)
            return {"ok": True, "response": answer, "elapsed_seconds": time.monotonic() - started,
                    "model": config["model"]}
        except Exception as exc:
            raise HTTPException(502, "模型图像请求失败：" + workspace.safe_error(exc)) from exc
        finally:
            await model.close()

    @app.get("/api/events")
    async def events(request: Request):
        # Every connection starts with authoritative state. Replaying older state
        # after that snapshot could briefly restore a stopped or different room.

        def encode(item):
            return f'id: {item["id"]}\nevent: {item["type"]}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n'

        async def generate():
            queue = workspace.bus.subscribe()
            try:
                # A full state also recovers from a ring-buffer gap after a long disconnect.
                yield encode({"id": workspace.bus.sequence, "type": "state", "data": workspace.snapshot(),
                              "session_id": workspace.session["id"] if workspace.session else None,
                              "time": time.time()})
                while not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(queue.get(), 15)
                        yield encode(item)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                workspace.bus.unsubscribe(queue)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/media/{sid}/{generation}/{filename}")
    async def media(sid: str, generation: str, filename: str):
        if not workspace.session or sid != workspace.session["id"]:
            raise HTTPException(404)
        if not generation.startswith("g") or not generation[1:].isdigit():
            raise HTTPException(404)
        if filename != "index.m3u8" and not (filename.startswith("segment_") and filename.endswith(".ts") and filename[8:-3].isdigit()):
            raise HTTPException(404)
        path = workspace.settings.data_dir / "sessions" / sid / "media" / generation / filename
        if not path.is_file():
            raise HTTPException(404)
        if filename == "index.m3u8" and path.stat().st_size == 0:
            raise HTTPException(503, "直播播放列表尚未就绪。", headers={"Retry-After": "2"})
        return FileResponse(path, media_type="application/vnd.apple.mpegurl" if filename.endswith("m3u8") else "video/mp2t",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{sid}/frames/{frame_id}.jpg")
    async def evidence_frame(sid: str, frame_id: str):
        if not workspace.session or sid != workspace.session["id"] or not re.fullmatch(r"[a-f0-9]{32}", frame_id):
            raise HTTPException(404)
        path = workspace.settings.data_dir / "sessions" / sid / "agent" / "frames" / f"{frame_id}.jpg"
        if not path.is_file():
            raise HTTPException(410, "证据帧已超出保留窗口。")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/sessions/{sid}/audio/{segment_id}.wav")
    async def audio_evidence(sid: str, segment_id: str):
        if not workspace.session or sid != workspace.session["id"] or not re.fullmatch(r"[a-f0-9]{32}", segment_id):
            raise HTTPException(404)
        path = workspace.settings.data_dir / "sessions" / sid / "audio" / f"{segment_id}.wav"
        if not path.is_file():
            raise HTTPException(410, "音频证据已超出保留窗口。")
        return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


app = create_app()
