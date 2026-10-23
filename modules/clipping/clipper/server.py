import asyncio
from contextlib import asynccontextmanager, suppress
import json
import math
from pathlib import Path
import re
import time
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, Settings, session_model_profile
from .store import Store, uid
from .render import DEFAULT_RESOLUTION, Renderer, STYLES, probe, validate_region
from .offline import prepare_file, read_event_file, parse_events, make_preview
from .publishing import PublicationManager, metadata_for
from .live_publishing import LivePublicationController, policy_enabled, automatic_profile, publication_policy
from .bili_collections import StreamerCollectionManager

REFERENCES = [
    {"uid":"13490123", "name":"恨也迷人", "url":"https://space.bilibili.com/13490123", "samples":30},
    {"uid":"274089", "name":"XING10086", "url":"https://space.bilibili.com/274089", "samples":30},
    {"uid":"5109111", "name":"TDD-珈蓝梦", "url":"https://space.bilibili.com/5109111", "samples":30},
    {"uid":"896830", "name":"七海Nana7mi单推人", "url":"https://space.bilibili.com/896830", "samples":30},
]
FINAL_ANALYSIS_TIMEOUT = 90


class LiveRequest(BaseModel):
    room: str = Field(min_length=1, max_length=300)
    capture_owner: str | None = Field(default=None, max_length=100)
    model_profile: str = Field(default="default", pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    streamer: str = Field(default="", max_length=80)
    streamer_uid: int | None = Field(default=None, gt=0)
    auto_render: bool = True
    minimum_score: int = Field(default=88, ge=40, le=100)
    style: Literal["reaction","punchline","context","clean"] = "reaction"


class ImportRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, max_length=160)
    events_path: str | None = None


class AnchorRequest(BaseModel):
    session_id: str
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    title: str = Field(default="刚才这一段", min_length=1, max_length=120)
    style: Literal["reaction","punchline","context","clean"] = "reaction"


class EditRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    cover_text: str | None = Field(default=None, max_length=80)
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    style: Literal["reaction","punchline","context","clean"] | None = None
    focus_event_ids: list[str] | None = Field(default=None, max_length=4)
    focus_region: dict | None = None


class RenderRequest(BaseModel):
    aspect: Literal["landscape","vertical"] = "landscape"
    resolution: Literal[720, 1080] = DEFAULT_RESOLUTION
    captions: bool = True
    emphasis: bool = True
    cover: bool = True
    cover_mode: Literal["designed", "frame"] = "designed"
    focus_region: dict | None = None


class PublishingProfile(BaseModel):
    tid: int = Field(gt=0)
    copyright: Literal[1, 2] = 2
    source: str = Field(default="", max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=10)
    description: str = Field(default="", max_length=2000)
    creator_name: str = Field(default="", max_length=20)
    title_prefix: str = Field(default="", max_length=30)
    follow_creator: bool | None = None
    description_style: Literal["profile", "source_links", "credit_date", "date_links", "legacy"] | None = None


class ExpectedPublicationMedia(BaseModel):
    video_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    cover_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PublicationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(min_length=1, max_length=10)
    tid: int = Field(gt=0)
    copyright: Literal[1, 2]
    source: str = Field(default="", max_length=200)
    expected_media: ExpectedPublicationMedia | None = None
    expected_account_uid: int | None = Field(default=None, gt=0)


class SessionSettings(BaseModel):
    auto_render: bool | None = None
    minimum_score: int | None = Field(default=None, ge=40, le=100)
    style: Literal["reaction","punchline","context","clean"] | None = None
    auto_publish: bool | None = None
    publish_profile: PublishingProfile | None = None


class AnalyzeRequest(BaseModel):
    transcribe: bool = True
    recover_tail: bool = False


class EventsRequest(BaseModel):
    events: list[dict] = Field(max_length=100000)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    store = Store(settings.data_dir)
    renderer = Renderer(settings, store)
    renderers = {"default": renderer}
    publisher = PublicationManager(settings, store)
    tasks, captures, analyses, stop_locks = {}, {}, {}, {}
    model_slot = asyncio.Semaphore(1)
    model_slots = {"default": model_slot}
    live_publishing = LivePublicationController(settings, store, publisher, model_slot, model_slots=model_slots)
    collections = StreamerCollectionManager(settings)

    def tracked(coro, key=None):
        key = key or uid()
        task = asyncio.create_task(coro)
        tasks[key] = task
        def finished(t):
            if tasks.get(key) is t:
                tasks.pop(key, None)
            if not t.cancelled():
                # Consume exceptions; user-facing tasks record their own failure state.
                t.exception()
        task.add_done_callback(finished)
        return task

    @asynccontextmanager
    async def lifespan(app):
        async def refresh_publications():
            while True:
                await asyncio.sleep(45)
                for p in store.publications():
                    if p["status"] == "submitted" and p.get("aid"):
                        with suppress(Exception):
                            await publisher.refresh(p["id"])
                    elif publisher.series_due(p):
                        with suppress(Exception):
                            await publisher.archive_series(p["id"])
                with suppress(Exception):
                    await collections.reconcile(store.publications(), store.sessions())
        checker = asyncio.create_task(refresh_publications())
        async def publish_ready_live_clips():
            while True:
                with suppress(Exception):
                    await live_publishing.tick()
                await asyncio.sleep(10)
        live_checker = asyncio.create_task(publish_ready_live_clips())
        yield
        checker.cancel()
        live_checker.cancel()
        with suppress(asyncio.CancelledError):
            await checker
        with suppress(asyncio.CancelledError):
            await live_checker
        for capture in list(captures.values()):
            with suppress(Exception):
                await capture.stop()
        remaining = list(tasks.values())
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
        store.close()

    app = FastAPI(title="小担 · B站自动切片", lifespan=lifespan)
    app.state.store, app.state.settings = store, settings
    app.state.publisher = publisher
    app.state.live_publishing = live_publishing
    app.state.collections = collections

    @app.middleware("http")
    async def origin_guard(request, call_next):
        if request.method in {"POST","PATCH","PUT","DELETE"}:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail":"仅接受本工作台发起的操作"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def session_or_404(sid):
        s = store.session(sid)
        if s is None:
            raise HTTPException(404, "素材不存在")
        return s

    def anchor_or_404(aid):
        a = store.anchor(aid)
        if a is None:
            raise HTTPException(404, "锚点不存在")
        return a

    def valid_range(sid, start, end):
        s = session_or_404(sid)
        if end <= start or end-start > 600 or end > s["duration"] + .25:
            raise HTTPException(422, "进出点需位于已保存素材内，切片最长 10 分钟")

    def health():
        return {"ffmpeg": bool(settings.ffmpeg and Path(settings.ffmpeg).is_file()),
                "recording_retention_version": 1,
                "model_configured": bool(settings.model_config().get("base_url")),
                "audio_configured": bool(settings.audio_config().get("base_url")),
                "font":Path(settings.font).is_file(), "platform":"bilibili"}

    @app.get("/api/health")
    async def get_health():
        return {"ok":True, **health()}

    @app.get("/api/state")
    async def state():
        policy = publication_policy(settings)
        return {"sessions":store.sessions(), "anchors":store.anchors(), "jobs":store.jobs(),
                "publications":store.publications(),
                "live_publication_policy": {key: policy.get(key) for key in (
                    "version", "enabled", "account_uid", "activated_at", "eligible_after", "waiting_reason")},
                "collections": collections.snapshot(),
                "health":health(), "styles":STYLES, "references":REFERENCES}

    @app.get("/api/sessions/{sid}")
    async def detail(sid):
        from .detector import heatmap
        s = session_or_404(sid)
        events = store.events(sid, max(0,s["duration"]-1800) if s.get("mode")=="live" else 0, limit=12000)
        segments = store.segments(sid)
        playback_segments, media_clock = [], 0.0
        if s.get("mode") == "live":
            for segment in segments:
                length = segment["end"] - segment["start"]
                playback_segments.append({"media_start":media_clock,"media_end":media_clock+length,
                                          "start":segment["start"],"end":segment["end"]})
                media_clock += length
        media = f"/api/sessions/{sid}/media" if s.get("media_path") else None
        if s.get("mode") == "live" and segments:
            media = f"/api/sessions/{sid}/hls/index.m3u8"
        return {"session":s, "events":events, "segments":segments, "anchors":store.anchors(sid),
                "jobs":store.jobs(sid), "media_url":media,
                "playback_segments":playback_segments,
                "frame_url":f"/api/sessions/{sid}/frame" if s.get("latest_frame") else None,
                "heatmap":heatmap(events,s["duration"])}

    @app.patch("/api/sessions/{sid}")
    async def update_session(sid, payload: SessionSettings):
        s = session_or_404(sid)
        values = payload.model_dump(exclude_none=True)
        candidate = {**s, **values}
        if candidate.get("auto_publish"):
            if not candidate.get("publish_profile"):
                raise HTTPException(422,"开启自动投稿前，请先设置投稿分区、类型和来源")
            dummy_job = {"anchor":{"title":"投稿设置校验","start":0,"end":6}}
            try:
                await publisher.validate(metadata_for(dummy_job,candidate))
            except Exception as exc:
                from .bili_publish import PublishError
                message = str(exc) if isinstance(exc,(ValueError,PublishError)) else "无法验证投稿账号或分区"
                raise HTTPException(422,message)
        return store.update_session(sid, **values)

    async def render_then_publish(jid):
        source = store.session(store.job(jid)["session_id"])
        try:
            profile = session_model_profile(source)
            scoped = settings.for_session(source)
            if profile not in renderers:
                renderers[profile] = Renderer(scoped, store)
        except ValueError as exc:
            store.update_job(jid, status="failed", error=str(exc))
            return
        await renderers[profile].run(jid)
        j = store.job(jid)
        if j and j["status"] == "complete":
            # No dwell/end-of-stream/pacing condition: review the completed clip
            # immediately, while capture and analysis continue in the same room.
            session = store.session(j["session_id"])
            if session.get("mode") == "live":
                await live_publishing.process(jid)
                with suppress(Exception):
                    await collections.reconcile(store.publications(), store.sessions())
            elif session.get("auto_publish"):
                # Existing explicitly configured imported-file workflows retain
                # their publication behavior; the durable patrol policy is live-only.
                try:
                    publication = publisher.enqueue(j, metadata_for(j, session), automatic=True)
                    store.update_job(jid, publication_id=publication["id"], publish_error=None)
                    tracked(publisher.run(publication["id"]), "publish:" + publication["id"])
                except ValueError as exc:
                    store.update_job(jid, publish_error=str(exc))

    def queue_render(anchor, options):
        from .titles import anchor_title_fields
        # All new automatic/manual clips use 1080p, including requests from
        # an older browser tab that still submits the former 720p setting.
        options = {**options, "resolution": DEFAULT_RESOLUTION}
        if options.get("focus_region"):
            validate_region(options["focus_region"])
        anchor = {**anchor, **anchor_title_fields(anchor, session_or_404(anchor["session_id"]))}
        job = store.create_job(anchor["id"], anchor["session_id"], anchor=dict(anchor), options=options)
        tracked(render_then_publish(job["id"]), "render:"+job["id"])
        return job

    async def analyze(sid, transcribe=True, live=False, recover_tail=False):
        from .detector import Detector, LIVE_LOOKBACK_SECONDS
        try:
            s = session_or_404(sid)
            scoped = settings.for_session(s)
            slot = model_slots.setdefault(session_model_profile(s), asyncio.Semaphore(1))
            async with slot:
                if recover_tail:
                    from .live_tail_recovery import recover_live_tail
                    await recover_live_tail(scoped, store, sid)
                    recovered = store.session(sid).get("tail_recovery") or {}
                    if recovered.get("status") != "complete":
                        raise ValueError("live_tail_recovery_incomplete")
                if not live:
                    store.update_session(sid, analysis_status="分析中", analysis_warning=None, transcription_warning=None)
                    await prepare_file(scoped, store, sid, transcribe)
                s = store.session(sid)
                end = max((x["end"] for x in store.segments(sid)), default=s["duration"])
                anchors = await Detector(scoped, store).analyze(
                    sid, start=max(0, end-LIVE_LOOKBACK_SECONDS) if live else 0, end=end)
                latest = store.session(sid)
                if latest.get("transcription_warning"):
                    store.update_session(sid, analysis_warning="；".join(filter(None, [
                        latest.get("transcription_warning"), latest.get("analysis_warning")])))
                # The model must substantiate automatic clips. Rule-only candidates stay reviewable.
                s = store.session(sid)
                if s.get("auto_render"):
                    for a in anchors:
                        if (a.get("method") == "model" and a.get("status") == "ready" and
                                a["end"] <= end and a["score"] >= s.get("minimum_score",88)):
                            if not any(j["anchor_id"] == a["id"] for j in store.jobs(sid)):
                                queue_render(a, {"aspect":"landscape","captions":True,"emphasis":True,"cover":True})
                store.update_session(sid, analysis_progress=100)
                if recover_tail:
                    store.update_session(sid, final_analysis_status=store.session(sid).get("analysis_status", "partial"))
        except asyncio.CancelledError:
            store.update_session(sid, analysis_status="分析已停止")
            if recover_tail:
                store.update_session(sid, final_analysis_status="interrupted")
            raise
        except Exception as exc:
            store.update_session(sid, analysis_status="分析未完成", analysis_warning=str(exc)[:500])
            if recover_tail:
                store.update_session(sid, final_analysis_status="failed")
        finally:
            store.update_session(sid, analysis_source_reserved=False)
            analyses.pop(sid, None)

    async def run_live(sid, room):
        from .ingest import LiveCapture
        try:
            scoped = settings.for_session(store.session(sid))
        except ValueError as exc:
            store.update_session(sid, status="error", warning=str(exc))
            return
        session_folder = settings.data_dir / "sessions" / sid
        async def event(kind, start, end, text, data, source_key=None):
            if kind == "audio_segment":
                kind = "transcript"
            store.add_event(sid, kind, start, end, text, data, source_key)
        async def segment(start, end, path, metadata):
            store.add_segment(sid,start,end,path,metadata)
            store.update_session(sid,duration=max(end,store.session(sid)["duration"]))
        async def update(values):
            # Recording duration means completed, exportable media, not wall time.
            values = dict(values)
            values.pop("duration", None)
            if isinstance(values.get("room"),dict) and values["room"].get("title"):
                values["title"] = values["room"]["title"]
            if isinstance(values.get("room"), dict):
                scoped.validate_profile_identity(room_id=values["room"].get("room_id"),
                                                 owner_uid=values["room"].get("owner_uid"), observed=True)
            if isinstance(values.get("room"), dict):
                resolved_id = values["room"].get("room_id")
                for other_id in captures:
                    if other_id == sid:
                        continue
                    other = store.session(other_id) or {}
                    other_room = other.get("room")
                    other_room_id = other_room.get("room_id") if isinstance(other_room, dict) else other_room
                    if resolved_id and (str(resolved_id) == str(other_room_id)
                            or other.get("source_url") == f"https://live.bilibili.com/{resolved_id}"):
                        raise ValueError("duplicate_live_room")
            store.update_session(sid,**values)
        capture = LiveCapture(scoped,sid,session_folder,event,segment,update)
        captures[sid] = capture
        async def detect_loop():
            last = 0
            while True:
                await asyncio.sleep(scoped.analyze_interval)
                duration = store.session(sid)["duration"]
                if duration-last >= 45 and sid not in analyses and duration >= 120:
                    last = duration
                    analyses[sid] = tracked(analyze(sid,False,True),"analyze:"+sid)
        detector = asyncio.create_task(detect_loop())
        try:
            await capture.run(room)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.update_session(sid,status="error",warning=str(exc)[:500])
        finally:
            detector.cancel()
            with suppress(asyncio.CancelledError):
                await detector
            await capture.stop()
            captures.pop(sid,None)
            s = store.session(sid)
            if s["status"] not in {"error","limit_reached","capacity_reached"}:
                store.update_session(sid,status="stopped")

    @app.post("/api/sessions/live")
    async def live(payload: LiveRequest):
        from .observer.bilibili import parse_room_id
        try:
            room = parse_room_id(payload.room)
        except ValueError as exc:
            raise HTTPException(422,str(exc))
        if payload.model_profile != "default" and not payload.capture_owner:
            raise HTTPException(422, "独立模型采集任务必须提供 capture_owner")
        try:
            scoped = settings.for_session({"model_profile": payload.model_profile})
            scoped.validate_profile_identity(room_id=room, owner_uid=payload.streamer_uid)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        active_ids = set(captures) | {k.split(":", 1)[1] for k in tasks if k.startswith("live:")}
        for sid in active_ids:
            current = store.session(sid) or {}
            known_room = current.get("room") or {}
            known_id = known_room.get("room_id") if isinstance(known_room, dict) else known_room
            same_room = str(known_id) == str(room) or current.get("source_url") == f"https://live.bilibili.com/{room}"
            known_uid = known_room.get("owner_uid") if isinstance(known_room, dict) else None
            known_uid = known_uid or current.get("streamer_uid")
            if payload.streamer_uid and str(payload.streamer_uid) == str(known_uid):
                same_room = True
            if (payload.capture_owner and payload.capture_owner == current.get("capture_owner")
                    and same_room and session_model_profile(current) == payload.model_profile):
                return {"session": current, "reused": True}
            if session_model_profile(current) == payload.model_profile:
                raise HTTPException(409,"请先停止当前采集任务，再开始同组的新直播间")
            if same_room:
                raise HTTPException(409,"这个直播间已有采集任务，不能重复采集")
        s = store.create_session(title=f"B站直播间 {room}",platform="bilibili",mode="live",
                                 status="connecting",source_url=f"https://live.bilibili.com/{room}",
                                 capture_lane="extra" if payload.model_profile != "default" else "main",
                                 **payload.model_dump())
        if policy_enabled(settings):
            with suppress(ValueError):
                s = store.update_session(s["id"], auto_publish=True,
                                         publish_profile=automatic_profile(s))
        tracked(run_live(s["id"],str(room)), "live:"+s["id"])
        return {"session":s}

    @app.post("/api/sessions/{sid}/stop")
    async def stop(sid):
        session_or_404(sid)
        async with stop_locks.setdefault(sid, asyncio.Lock()):
            s = session_or_404(sid)
            if s.get("stop_phase") == "complete":
                return s
            store.update_session(sid, stop_phase="draining")
            if sid in captures:
                # Capture stops its readers, indexes final completed media, and
                # drains already-decoded ASR chunks within its bounded budget.
                await captures[sid].stop()
            for key in ["live:"+sid, "analyze:"+sid]:
                task = tasks.get(key)
                if task and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            s = store.update_session(sid, status="stopped")
            if s.get("mode") == "live" and s.get("duration", 0) >= 6:
                store.update_session(sid, stop_phase="analyzing",
                                     final_analysis_status="running")
                final = tracked(analyze(sid, False, True), "analyze:"+sid)
                analyses[sid] = final
                try:
                    await asyncio.wait_for(final, timeout=FINAL_ANALYSIS_TIMEOUT)
                except asyncio.TimeoutError:
                    store.update_session(sid, final_analysis_status="timeout",
                                         analysis_warning="换房前末段分析超时；录像和已识别语音已保存，可稍后重新分析。")
                else:
                    latest = store.session(sid)
                    store.update_session(sid, final_analysis_status=latest.get("analysis_status", "complete"))
            # Patrol may lose its HTTP response; this durable marker makes
            # read-only recovery distinguish stopped capture from finished tail.
            return store.update_session(sid, status="stopped", stop_phase="complete")

    async def import_file(path, title=None, events_path=None):
        try:
            info = await probe(settings,path)
            rows = read_event_file(events_path) if events_path else []
        except (ValueError,RuntimeError,OSError) as exc:
            raise HTTPException(422,str(exc))
        if info["duration"] > 12*3600:
            raise HTTPException(422,"单份素材最长 12 小时，请分段导入")
        s = store.create_session(title=title or Path(path).stem,platform="bilibili",mode="file",
                                  status="ready",duration=info["duration"],media_path=str(path),
                                  media_info=info,auto_render=False)
        store.add_segment(s["id"],0,info["duration"],path,{"timestamp_basis":"file_media_seconds"})
        for row in rows:
            if row["start"] <= info["duration"]:
                store.add_event(s["id"],**row)
        async def preview():
            try:
                await make_preview(settings,store,s["id"])
            except Exception:
                store.update_session(s["id"],warning="浏览器预览转换失败，仍可选择时间并导出")
        if Path(path).suffix.lower() not in {".mp4",".webm",".m4v"}:
            tracked(preview(),"preview:"+s["id"])
        return s

    @app.post("/api/sessions/import")
    async def import_path(payload: ImportRequest):
        try:
            path = settings.import_path(payload.path)
            ev = settings.import_path(payload.events_path) if payload.events_path else None
        except ValueError as exc:
            raise HTTPException(422,str(exc))
        return {"session":await import_file(path,payload.title,ev)}

    @app.post("/api/uploads")
    async def upload(request: Request, filename: str):
        suffix = Path(filename).suffix.lower()
        if suffix not in {".mp4",".mkv",".mov",".webm",".flv",".ts",".m4v"}:
            raise HTTPException(422,"请选择视频文件")
        folder = settings.data_dir / "uploads"
        folder.mkdir(exist_ok=True)
        path = folder / (uid()+suffix)
        size = 0
        try:
            with path.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 2*1024**3:
                        raise HTTPException(413,"网页上传上限 2 GB；更大的录像请使用服务器路径导入")
                    out.write(chunk)
            return {"session":await import_file(path,Path(filename).stem[:160])}
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @app.post("/api/sessions/{sid}/events")
    async def import_events(sid,payload: EventsRequest):
        s=session_or_404(sid)
        try:
            rows=parse_events(payload.events)
        except (ValueError,TypeError) as exc:
            raise HTTPException(422,str(exc))
        for index,row in enumerate(rows):
            if row["end"]>s["duration"]+.2:
                raise HTTPException(422,f"第 {index+1} 条事件超出视频时长")
        for row in rows:
            store.add_event(sid,**row)
        return {"imported":len(rows)}

    @app.post("/api/sessions/{sid}/analyze",status_code=202)
    async def start_analyze(sid,payload: AnalyzeRequest):
        s=session_or_404(sid)
        if payload.recover_tail and (s.get("mode") != "live" or sid in captures
                or "live:" + sid in tasks):
            raise HTTPException(422, "尾部补识别只能在直播采集停止后进行")
        if sid in analyses:
            raise HTTPException(409,"该素材正在分析")
        changes = {}
        if payload.recover_tail:
            recovery = s.get("tail_recovery") or {}
            changes.update(tail_recovery={**recovery, "status": "queued", "requested_at": time.time()},
                           analysis_stage="tail_recovery", final_analysis_status="running")
        try:
            store.reserve_analysis(sid, **changes)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        analyses[sid]=tracked(analyze(sid,payload.transcribe,s.get("mode")=="live",payload.recover_tail),"analyze:"+sid)
        return {"status":"analyzing"}

    @app.post("/api/anchors")
    async def add_anchor(payload: AnchorRequest):
        from .titles import format_clip_title
        valid_range(payload.session_id,payload.start,payload.end)
        values=payload.model_dump()
        values.pop("session_id")
        values["title"] = format_clip_title(values["title"], session_or_404(payload.session_id))
        return store.add_anchor(payload.session_id,**values,origin="manual",score=100,
                                cover_text=payload.title,evidence_ids=[],reason="手动选段")

    @app.patch("/api/anchors/{aid}")
    async def edit_anchor(aid,payload: EditRequest):
        from .titles import format_clip_title
        a=anchor_or_404(aid)
        values=payload.model_dump(exclude_unset=True)
        if "title" in values:
            values["title"] = format_clip_title(values["title"], session_or_404(a["session_id"]))
        valid_range(a["session_id"],values.get("start",a["start"]),values.get("end",a["end"]))
        if values.get("focus_region"):
            try:
                validate_region(values["focus_region"])
            except (ValueError,KeyError,TypeError) as exc:
                raise HTTPException(422,str(exc))
        if values.get("focus_event_ids"):
            allowed={e["id"] for e in store.events(a["session_id"],values.get("start",a["start"]),values.get("end",a["end"])) if e["kind"]=="danmaku"}
            if any(e not in allowed for e in values["focus_event_ids"]):
                raise HTTPException(422,"只能强调选段内真实存在的弹幕")
        # Later automatic analysis may improve an unexported candidate. Once
        # edited in the workbench, its editorial choices belong to the user.
        return store.update_anchor(aid,**values,editorial_locked=True)

    @app.post("/api/anchors/{aid}/render",status_code=202)
    async def render(aid,payload: RenderRequest):
        a=anchor_or_404(aid)
        if any(j["anchor_id"]==aid and j["status"] in {"queued","rendering"} for j in store.jobs(a["session_id"])):
            raise HTTPException(409,"这个锚点正在导出")
        try:
            return queue_render(a,payload.model_dump())
        except (ValueError,KeyError,TypeError) as exc:
            raise HTTPException(422,str(exc))

    @app.get("/api/jobs/{jid}")
    async def job(jid):
        j=store.job(jid)
        if j is None:
            raise HTTPException(404,"导出任务不存在")
        return j

    @app.get("/api/publishing/account")
    async def publishing_account():
        try:
            return await publisher.account()
        except Exception:
            return {"configured":bool(settings.upload_cookie()),"logged_in":False,
                    "error":"暂时无法连接B站验证账号"}

    @app.get("/api/publishing/categories")
    async def publishing_categories():
        from .bili_publish import PublishError
        try:
            return {"categories":await publisher.categories()}
        except PublishError as exc:
            raise HTTPException(502,str(exc))
        except Exception:
            raise HTTPException(502,"B站分区列表暂时不可用")

    @app.get("/api/jobs/{jid}/publication-preview")
    async def publication_preview(jid):
        j=store.job(jid)
        if j is None:
            raise HTTPException(404,"导出任务不存在")
        try:
            return publisher.preview(j)
        except ValueError as exc:
            raise HTTPException(422,str(exc))

    @app.post("/api/jobs/{jid}/publish",status_code=202)
    async def publish_job(jid,payload:PublicationRequest):
        from .bili_publish import PublishError
        j=store.job(jid)
        if j is None:
            raise HTTPException(404,"导出任务不存在")
        try:
            values = payload.model_dump()
            expected_media = values.pop("expected_media")
            expected_account_uid = values.pop("expected_account_uid")
            metadata=await publisher.validate(values)
            p=publisher.enqueue(j,metadata,expected_media=expected_media,
                                expected_account_uid=expected_account_uid)
        except (ValueError,PublishError) as exc:
            raise HTTPException(422,str(exc))
        tracked(publisher.run(p["id"]),"publish:"+p["id"])
        store.update_job(jid,publication_id=p["id"],publish_error=None)
        return p

    @app.get("/api/publications")
    async def publications():
        return {"publications":store.publications()}

    @app.post("/api/publications/{pid}/refresh")
    async def refresh_publication(pid):
        from .bili_publish import PublishError
        if store.publication(pid) is None:
            raise HTTPException(404,"投稿任务不存在")
        try:
            return await publisher.refresh(pid)
        except PublishError as exc:
            raise HTTPException(502,str(exc))
        except Exception:
            raise HTTPException(502,"B站稿件状态暂时不可用")

    @app.get("/api/exports/{jid}/{filename}")
    async def exported(jid,filename):
        j=store.job(jid)
        if not j or j["status"]!="complete" or filename not in {"clip.mp4","cover.jpg","package.zip","subtitles.srt","metadata.json","titles.txt"}:
            raise HTTPException(404,"成片尚未完成或文件不存在")
        p=settings.data_dir/"exports"/jid/filename
        if not p.is_file():
            raise HTTPException(404,"此导出未包含该文件")
        return FileResponse(p,filename=filename if filename.endswith((".zip",".srt",".txt",".json")) else None)

    @app.get("/api/sessions/{sid}/media")
    async def media(sid):
        s=session_or_404(sid)
        p=Path(s.get("preview_path") or s.get("media_path") or "")
        if not p.is_file():
            raise HTTPException(404,"没有可播放的视频")
        return FileResponse(p)

    @app.get("/api/sessions/{sid}/frame")
    async def frame(sid):
        p=Path(session_or_404(sid).get("latest_frame") or "")
        if not p.is_file():
            raise HTTPException(404,"画面尚未就绪")
        return FileResponse(p,headers={"Cache-Control":"no-store"})

    @app.get("/api/sessions/{sid}/hls/{filename}")
    async def hls(sid,filename):
        s=session_or_404(sid)
        if filename=="index.m3u8":
            segments=store.segments(sid)
            if not segments or s.get("mode")!="live":
                raise HTTPException(404,"直播尚未就绪")
            target=max(1,math.ceil(max(x["end"]-x["start"] for x in segments)))
            lines=["#EXTM3U","#EXT-X-VERSION:3",f"#EXT-X-TARGETDURATION:{target}",
                   "#EXT-X-MEDIA-SEQUENCE:0","#EXT-X-PLAYLIST-TYPE:EVENT"]
            previous=None
            for part in segments:
                if previous and (part["metadata"].get("generation")!=previous["metadata"].get("generation")
                                 or part["start"] > previous["end"]+.2):
                    lines.append("#EXT-X-DISCONTINUITY")
                lines.extend([f"#EXTINF:{part['end']-part['start']:.6f},",f"{part['id']}.ts"])
                previous=part
            if s["status"] not in {"live","connecting","waiting","reconnecting","recording"}:
                lines.append("#EXT-X-ENDLIST")
            return Response("\n".join(lines)+"\n",media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control":"no-store"})
        if not re.fullmatch(r"[0-9a-f]{16}\.ts",filename):
            raise HTTPException(404)
        segment=next((x for x in store.segments(sid) if x["id"]==filename[:-3]),None)
        if not segment or not Path(segment["path"]).is_file():
            raise HTTPException(404)
        return FileResponse(segment["path"],media_type="video/mp2t")

    @app.get("/api/references")
    async def references():
        p=ROOT/"docs/reference-study.md"
        return {"accounts":REFERENCES,"study":p.read_text() if p.exists() else "样本研究正在整理"}

    app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")

    @app.get("/")
    async def home():
        return FileResponse(ROOT/"static/index.html")

    return app
