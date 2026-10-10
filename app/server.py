"""Local Bilibili workspace with independent video, danmaku and agent tasks."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from io import BytesIO
import base64
import json
import math
from pathlib import Path
import re
import shutil
import time
from typing import Literal
from urllib.parse import urlparse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image

from .agent import LiveAgent
from .audio import AudioPipeline
from .bilibili import BilibiliClient
from .config import ROOT, Settings
from .danmaku_candidates import candidate_context, generate_candidates
from .danmaku_send import DanmakuCredentials, DanmakuSender, DanmakuSendError
from .events import EventBus
from .journal import SessionJournal
from .media import MediaCapture
from .model import ModelBusy, VisionModel


class SessionRequest(BaseModel):
    room: str = Field(min_length=1, max_length=500)


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
        self.tasks = []
        self.capture = None
        self.lock = asyncio.Lock()
        self.model_lock = asyncio.Lock()
        self.clock_start = time.monotonic()
        self.danmaku_status = {"status": "disconnected"}
        self.resume_error = None
        self.danmaku_credentials = DanmakuCredentials(settings.data_dir, env_cookie=settings.bilibili_cookie)
        self.danmaku_sender = DanmakuSender(self.danmaku_credentials)
        self.last_danmaku_sent_at = None
        self.danmaku_send_error = None
        self.danmaku_candidates = {"items": [], "status": "waiting", "generated_at": None}

    def danmaku_send_status(self):
        status = self.danmaku_credentials.status()
        active = bool(self.session and self.session["status"] not in {"stopped", "idle"}
                      and self.room and self.room.live_status == 1)
        return {**status, "can_send": status["configured"] and active,
                "status": "ready" if status["configured"] else "login_required",
                "error": self.danmaku_send_error}

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
            "danmaku_candidates": dict(self.danmaku_candidates),
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

    async def start(self, room_input, *, resume_id=None):
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
            await self._stop()
            sid = resume_id or uuid.uuid4().hex
            directory = self.settings.data_dir / "sessions" / sid
            directory.mkdir(parents=True, exist_ok=bool(resume_id))
            self.clock_start = time.monotonic()
            self.room = room
            self.session = {"id": sid, "status": "connecting", "room_input": room_input,
                            "started_at": time.time(), "error": None}
            if saved:
                started = float(saved["session"]["started_at"])
                if math.isfinite(started) and started <= time.time():
                    self.session["started_at"] = started
                    self.clock_start = time.monotonic() - (time.time() - started)
                self.session["resumed_at"] = time.time()
            self.resume_error = None
            self.danmaku.clear()
            self.danmaku_candidates = {"items": [], "status": "waiting", "generated_at": None}
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
                if self.session and self.session["id"] == sid:
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
            (directory / "session.json").write_text(json.dumps({"session": self.session,
                "room": asdict(room), "model": self.settings.model_config()["model"]}, ensure_ascii=False, indent=2))
            self.tasks = [asyncio.create_task(self._stream_loop(sid, directory)),
                          asyncio.create_task(self._danmaku_loop(sid)),
                          asyncio.create_task(self._model_loop(sid)),
                          asyncio.create_task(self._candidate_loop(sid))]
            self._remember_active()
            await self.state()
            return self.snapshot()

    async def stop(self):
        async with self.lock:
            await self._stop()
            self.active_path.unlink(missing_ok=True)
            await self.state()
        return self.snapshot()

    async def _stop(self):
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        if self.capture:
            await self.capture.stop()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self.agent:
            if self.audio:
                await self.audio.stop()
            await self.agent.stop()
        if self.model:
            await self.model.close()
        self.capture = None
        if self.session:
            self.session.update(status="stopped", stopped_at=time.time())
        self.playback["ready"] = False
        self.danmaku_status = {"status": "disconnected"}

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
        while self.session and self.session["id"] == sid:
            await self.ensure_model()
            if self.model.configured:
                return
            await asyncio.sleep(3)

    async def _candidate_loop(self, sid):
        """Refresh drafts from actual recent sources; never calls the send API."""
        previous = None
        deferred_since = None
        while self.session and self.session["id"] == sid:
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
                        await asyncio.sleep(2)
                        continue
                    allow_observations_busy = True
                    deferred_since = started
                else:
                    deferred_since = None
            context = candidate_context(self.snapshot(), source_time=time.monotonic() - self.clock_start)
            signature = json.dumps(context, ensure_ascii=False, sort_keys=True)
            if not self.room or self.room.live_status != 1 or not self.model or not self.model.configured or not any(
                    context.get(key) for key in ("visual", "audio_transcripts", "audience_comments")):
                self.danmaku_candidates = {"items": [], "generated_at": None, "status": "waiting"}
                await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
                await asyncio.sleep(3)
                continue
            if signature != previous:
                self.danmaku_candidates = {**self.danmaku_candidates, "status": "generating", "error": None}
                await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
                try:
                    texts = await asyncio.wait_for(generate_candidates(self.model, context,
                        max_length=self.danmaku_credentials.status()["max_length"],
                        allow_observations_busy=allow_observations_busy), timeout=10)
                    if not self.session or self.session["id"] != sid:
                        return
                    self.danmaku_candidates = {"items": [{"id": uuid.uuid4().hex, "text": text}
                                                         for text in texts],
                        "generated_at": time.time(), "source_time": context["source_time"],
                        "sources": [key for key in ("visual", "audio_transcripts", "audience_comments")
                                    if context[key]], "status": "ready", "error": None}
                    previous = signature
                except asyncio.CancelledError:
                    raise
                except ModelBusy:
                    self.danmaku_candidates = {**self.danmaku_candidates, "status": "deferred", "error": None}
                except Exception:
                    self.danmaku_candidates = {**self.danmaku_candidates, "status": "error",
                        "error": "候选暂未更新，稍后自动重试；可以直接编辑弹幕。"}
                await self.emit("danmaku_candidates", dict(self.danmaku_candidates))
            await asyncio.sleep(max(1, 12 - (time.monotonic() - started)))

    async def _stream_loop(self, sid, directory):
        media_root = directory / "media"
        generations = [int(p.name[1:]) for p in media_root.iterdir()
                       if p.is_dir() and re.fullmatch(r"g[0-9]+", p.name)] if media_root.exists() else []
        generation = max(generations, default=0)
        backoff = 3
        while self.session and self.session["id"] == sid:
            try:
                self.room = await self.client.resolve_room(self.room.room_id)
                if self.room.live_status != 1:
                    self.session.update(status="waiting", error=None)
                    self.playback["ready"] = False
                    await self.state()
                    await asyncio.sleep(self.settings.room_poll_seconds)
                    continue
                if not self.settings.ffmpeg_available:
                    raise RuntimeError("未找到 FFmpeg。官方播放器仍可观看，安装 FFmpeg 后重新连接即可启用观察。")
                sources = await self.client.get_play_sources(self.room.room_id)
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
                    while not task.done():
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
                self.session.update(status="connecting", error=self.safe_error(exc))
                self.playback["ready"] = False
                await self.emit("error", {"source": "video", "message": self.session["error"]})
                await self.state()
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def _danmaku_loop(self, sid):
        try:
            async for event in self.client.events(self.room.room_id):
                if not self.session or self.session["id"] != sid:
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
                elif kind == "connection":
                    self.danmaku_status = event
                    await self.state()
                else:
                    await self.emit(kind, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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

    async def close(self):
        # Process shutdown preserves the active-session pointer. Only the
        # browser's explicit Disconnect prevents reconnection on the next boot.
        async with self.lock:
            await self._stop()
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
            return await workspace.start(payload.room)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, workspace.safe_error(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, "直播平台连接失败，请稍后重试。") from exc

    @app.post("/api/session/stop")
    async def stop_session():
        return await workspace.stop()

    @app.post("/api/danmaku/credentials")
    async def save_danmaku_credentials(request: Request):
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
            try:
                if cookie.strip():
                    workspace.danmaku_credentials.save(cookie)
                else:
                    workspace.danmaku_credentials.clear()
            except (ValueError, DanmakuSendError) as exc:
                # Credential validation messages are intentionally fixed.
                raise HTTPException(400, "登录信息无效，请检查本人 Cookie 中的 SESSDATA 和 bili_jct。") from exc
            workspace.danmaku_send_error = None
            status = workspace.danmaku_send_status()
            await workspace.emit("danmaku_send_status", status)
            return status

    @app.post("/api/danmaku/send")
    async def send_danmaku(payload: DanmakuRequest):
        async with workspace.lock:
            if not workspace.session or payload.session_id != workspace.session["id"]:
                raise HTTPException(409, "直播会话已变更，请检查房间后重新发送。")
            if workspace.session["status"] == "stopped" or not workspace.room or workspace.room.live_status != 1:
                raise HTTPException(409, "请先连接正在直播的房间。")
            if workspace.last_danmaku_sent_at is not None and time.monotonic() - workspace.last_danmaku_sent_at < 3:
                raise HTTPException(429, "发送过于频繁，请稍等几秒。")
            try:
                result = await workspace.danmaku_sender.send(workspace.room.room_id, payload.text)
            except DanmakuSendError as exc:
                workspace.danmaku_send_error = str(exc)
                await workspace.emit("danmaku_send_status", workspace.danmaku_send_status())
                raise HTTPException(exc.status_code, str(exc)) from None
            workspace.last_danmaku_sent_at = time.monotonic()
            workspace.danmaku_send_error = None
            await workspace.emit("danmaku_send_status", workspace.danmaku_send_status())
            return result

    @app.post("/api/questions", status_code=202)
    async def question(payload: QuestionRequest):
        async with workspace.lock:
            if not workspace.session or payload.session_id != workspace.session["id"]:
                raise HTTPException(409, "直播会话已变更，请重新提问。")
            if workspace.session["status"] == "stopped":
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
        if workspace.session["status"] == "stopped" or not workspace.audio:
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
