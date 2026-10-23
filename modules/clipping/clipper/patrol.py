"""Following-list patrol through the local workbench API.

Only reads Bilibili follows and captures live media. This module has no chat
sending or publication endpoint. All mutable session operations are restricted
to an owned session persisted before advancing to the next room.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict, dataclass
import fcntl
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import time
from urllib.parse import urlparse
import uuid

import httpx

from .config import Settings
from .observer.bilibili import BilibiliClient
from .live_publishing import policy_enabled

ACTIVE = {"connecting", "live", "waiting", "reconnecting", "recording"}
TERMINAL = {"stopped", "error", "interrupted", "limit_reached", "capacity_reached"}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def session_room(session):
    value = session.get("room")
    if isinstance(value, dict):
        value = value.get("room_id")
    try:
        return int(value or session.get("source_url", "").rstrip("/").split("/")[-1])
    except (TypeError, ValueError):
        return None


def choose_room(rooms, visits, retry_after, now):
    """Oldest-visited first, deterministic ties; failed rooms have a cooldown."""
    last = {int(v["room"]["room_id"]): float(v["started_at"]) for v in visits}
    candidates = [r for r in rooms if retry_after.get(str(r["room_id"]), 0) <= now]
    return min(candidates, key=lambda r: (last.get(r["room_id"], 0), r["room_id"]),
               default=None)


def folder_bytes(root: Path):
    total = 0
    if root.is_dir():
        for directory, _, filenames in os.walk(root, followlinks=False):
            for filename in filenames:
                path = Path(directory) / filename
                with suppress(OSError):
                    if not path.is_symlink():
                        total += path.stat().st_size
    return total


@dataclass
class PatrolConfig:
    base_url: str = "http://127.0.0.1:6880"
    dwell_seconds: float | None = None
    dwell_min_seconds: float = 2700
    dwell_max_seconds: float = 3720
    tail_seconds: float = 25
    poll_seconds: float = 10
    refresh_seconds: float = 300
    room_status_seconds: float = 30
    startup_timeout: float = 120
    minimum_score: int = 88
    max_recording_gib: float = 20
    minimum_free_gib: float = 5
    first_room: int | None = None
    next_room: int | None = None
    until_offline_room: int | None = None

    def validate(self):
        target = urlparse(self.base_url)
        if target.scheme != "http" or target.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("轮巡只连接本机工作台 API")
        durations = (self.dwell_min_seconds, self.dwell_max_seconds, self.tail_seconds,
                     self.room_status_seconds)
        if any(not math.isfinite(x) for x in durations):
            raise ValueError("停留与房间状态检查时间必须为有限数值")
        if (self.dwell_min_seconds < 180 or self.dwell_max_seconds < self.dwell_min_seconds
                or self.dwell_seconds is not None and
                (not math.isfinite(self.dwell_seconds) or self.dwell_seconds < 180)
                or not 10 <= self.tail_seconds <= 120):
            raise ValueError("每站至少 180 秒，尾部上下文保留 10–120 秒")
        if self.room_status_seconds < 10:
            raise ValueError("房间状态检查间隔至少 10 秒")
        if self.poll_seconds < 2 or self.refresh_seconds < 60 or self.startup_timeout < 30:
            raise ValueError("轮询至少 2 秒，关注刷新至少 60 秒，连接等待至少 30 秒")
        if not 40 <= self.minimum_score <= 100:
            raise ValueError("评分阈值需在 40–100")
        if self.max_recording_gib <= 0 or self.minimum_free_gib < 1:
            raise ValueError("容量预算需大于 0，磁盘至少保留 1 GiB")
        if self.first_room is not None and self.first_room <= 0:
            raise ValueError("首站房间号需为正整数")
        if self.next_room is not None and self.next_room <= 0:
            raise ValueError("下次优先房间号需为正整数")
        if self.until_offline_room is not None and self.until_offline_room <= 0:
            raise ValueError("看到下播的房间号需为正整数")


class FollowingPatrol:
    def __init__(self, settings: Settings, directory: Path, config=None):
        self.settings = settings
        self.directory = Path(directory).resolve()
        self.config = config or PatrolConfig()
        self.config.validate()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_path = self.directory / "status.json"
        self.state = read_json(self.state_path, {}) or {}
        self.state.setdefault("version", 1)
        self.state.setdefault("capture_owner", "following-patrol-" + uuid.uuid4().hex[:16])
        self.state.setdefault("created_at", time.time())
        self.state.setdefault("visits", [])
        self.state.setdefault("retry_after", {})
        self.state.setdefault("current_visit", None)
        self.state.setdefault("pending_start", None)
        hold = self.state.get("until_offline") or {}
        if self.config.until_offline_room and hold.get("room_id") != self.config.until_offline_room:
            self.state["until_offline"] = {"room_id": self.config.until_offline_room,
                                            "status": "active", "requested_at": time.time()}
        self.state.update(config=asdict(self.config), auto_render=True,
                          auto_publish=policy_enabled(settings), send_danmaku=False)
        self.client = httpx.AsyncClient(base_url=self.config.base_url, timeout=45,
                                        trust_env=False)
        self.stopping = asyncio.Event()
        self.rooms = []
        self.refreshed_at = 0
        self.refresh_retry_at = float(self.state.get('following_retry_at') or 0)
        self._lock = None
        self._disk_checked = 0

    def save(self, **changes):
        self.state.update(changes, updated_at=time.time())
        write_json(self.state_path, self.state)

    def event(self, kind, **data):
        record = {"time": time.time(), "type": kind, **data}
        # All callsites pass only fixed error categories or selected public
        # fields; never serialize HTTP exception bodies, headers or URLs.
        with (self.directory / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def cookie_revision(self):
        path = (Path(self.settings.upload_cookie_file) if self.settings.upload_cookie_file
                else self.settings.data_dir / 'private/bilibili-upload.cookie')
        try:
            stat = path.stat()
            return f'{stat.st_mtime_ns}:{stat.st_size}'
        except OSError:
            return None

    async def request(self, method, path, payload=None):
        timeout = 180 if path.endswith("/stop") else 45
        response = await self.client.request(method, path, json=payload, timeout=timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"local_api_http_{response.status_code}")
        return response.json()

    async def follows(self, persist=True):
        cookie = self.settings.upload_cookie()
        if not cookie:
            raise RuntimeError("upload_cookie_missing")
        rooms, seen = {}, set()
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            bili = BilibiliClient(cookie=cookie, client=client)
            nav = await bili._json("https://api.bilibili.com/x/web-interface/nav", allow_nav=True)
            if not nav.get("isLogin"):
                raise RuntimeError("upload_cookie_not_logged_in")
            for page in range(1, 51):
                data = await bili._json("/xlive/web-ucenter/v1/xfetter/GetWebList",
                                        {"page": page, "page_size": 10})
                items = data.get("list") or data.get("rooms") or []
                page_ids = set()
                for row in items:
                    if not isinstance(row, dict):
                        continue
                    try:
                        room_id = int(row.get("room_id", 0))
                    except (ValueError, TypeError):
                        continue
                    if room_id <= 0:
                        continue
                    page_ids.add(room_id)
                    if str(row.get("live_status")) == "1":
                        try:
                            streamer_uid = int(row.get("uid", 0))
                        except (TypeError, ValueError):
                            streamer_uid = 0
                        rooms[room_id] = {
                            "room_id": room_id,
                            "name": str(row.get("uname") or "")[:80],
                            "streamer_uid": streamer_uid or None,
                            "title": str(row.get("title") or "")[:200],
                        }
                repeated = bool(page_ids) and page_ids <= seen
                seen.update(page_ids)
                if len(items) < 10 or repeated:
                    break
        self.refreshed_at = time.time()
        self.rooms = list(rooms.values())
        changes = dict(following_refreshed_at=self.refreshed_at, live_following_count=len(rooms),
                       account_name=str(nav.get("uname", ""))[:100], following_error=None,
                       following_retry_at=0, following_failure_count=0, error=None)
        self.refresh_retry_at = 0
        if persist:
            self.save(**changes)
        else:
            self.state.update(changes)
        return self.rooms

    def capacity(self, snapshot):
        now = time.time()
        if now - self._disk_checked < 30:
            return self.state.get("capacity_reason")
        owned = {v["session_id"] for v in self.state["visits"] if v.get("session_id")}
        if self.state["current_visit"]:
            owned.add(self.state["current_visit"]["session_id"])
        used = sum(folder_bytes(self.settings.data_dir / "sessions" / sid) for sid in owned)
        # Finished clips are the retained product, not the recording buffer.
        # Their growth must never exhaust the raw-recording budget.
        free = shutil.disk_usage(self.settings.data_dir).free
        reason = ("recording_budget_reached" if used >= self.config.max_recording_gib * 1024**3
                  else "disk_reserve_reached" if free < self.config.minimum_free_gib * 1024**3
                  else None)
        self._disk_checked = now
        self.save(recording_bytes=used, free_bytes=free, capacity_reason=reason,
                  capacity_checked_at=now, capacity_scan_interval_seconds=30)
        return reason

    async def reconcile_start(self, snapshot):
        """Resolve a lost POST response without retrying creation blindly."""
        pending = self.state.get("pending_start")
        if not pending:
            return
        candidates = [
            s for s in snapshot["sessions"]
            if s["id"] not in pending["before_ids"]
            and s.get("mode") == "live"
            and s.get("capture_owner") == self.state["capture_owner"]
            and session_room(s) == pending["room"]["room_id"]
            and s.get("created_at", 0) >= pending["requested_at"] - 1
        ]
        if len(candidates) == 1:
            await self.adopt(candidates[0], pending["room"])
        else:
            # A timed-out POST may still be finishing. Block new creation until
            # the next read instead of risking a duplicate capture.
            if time.time() - pending["requested_at"] > 120:
                self.save(status="start_uncertain", error="无法确定上次开播采集结果，等待人工查看")
                raise RuntimeError("session_start_uncertain")

    async def adopt(self, session, room):
        sid = session["id"]
        profile = session.get("publish_profile") or {}
        # Policy may be explicitly enabled immediately, or armed after backlog.
        automatic = policy_enabled(self.settings)
        await self.request("PATCH", f"/api/sessions/{sid}",
                           {"auto_render": True, "auto_publish": automatic,
                            "minimum_score": self.config.minimum_score,
                            "publish_profile": {
                                "tid": 21, "copyright": 2,
                                "source": f"https://live.bilibili.com/{room['room_id']}",
                                "creator_name": room["name"][:20],
                                "tags": [room["name"][:20], "直播切片"],
                                "follow_creator": profile.get("follow_creator", True),
                                "description_style": profile.get("description_style"),
                                "description": profile.get("description", ""),
                            }})
        # Draw once per visit, then persist the deadline. The range includes
        # tail context; explicit --dwell retains the legacy fixed-time contract.
        duration = (self.config.dwell_seconds + self.config.tail_seconds
                    if self.config.dwell_seconds is not None else
                    random.uniform(self.config.dwell_min_seconds, self.config.dwell_max_seconds))
        finish_after = session["created_at"] + duration
        visit = {
            "room": room, "session_id": sid, "started_at": session["created_at"],
            "planned_duration_seconds": duration,
            "scheduled_end_at": finish_after - self.config.tail_seconds,
            "finish_after": finish_after,
            "last_media_at": time.time(), "last_duration": float(session.get("duration", 0)),
        }
        hold = self.state.get("until_offline") or {}
        if hold.get("status") == "active" and hold.get("room_id") == room["room_id"]:
            hold["room"] = room
            visit.update(until_offline=True, planned_duration_seconds=None,
                         scheduled_end_at=None, finish_after=None)
        if self.state.get("requested_next_room") == room["room_id"]:
            self.state["requested_next_room"] = None
        self.save(current_visit=visit, pending_start=None, status="watching", error=None,
                  auto_publish=automatic)
        self.event("visit_started", session_id=sid, room_id=room["room_id"], name=room["name"])

    async def start_visit(self, room, snapshot):
        self.save(pending_start={"room": room, "requested_at": time.time(),
                                 "before_ids": [s["id"] for s in snapshot["sessions"]]})
        try:
            result = await self.request("POST", "/api/sessions/live",
                                        {"room": str(room["room_id"]), "auto_render": True,
                                         "capture_owner": self.state["capture_owner"],
                                         "streamer": room["name"],
                                         "streamer_uid": room.get("streamer_uid"),
                                         "minimum_score": self.config.minimum_score, "style": "reaction"})
        except RuntimeError as exc:
            if str(exc) == "local_api_http_409":
                # A definite conflict is a refused creation, not a lost reply.
                self.save(pending_start=None, status="waiting_for_workbench")
            raise
        await self.adopt(result["session"], room)

    async def finish_visit(self, reason):
        visit = self.state.get("current_visit")
        if not visit:
            return
        sid = visit["session_id"]
        if visit.get("stop_requested_at"):
            result = await self.request("GET", f"/api/sessions/{sid}")
            session = result["session"]
            if session.get("stop_phase") == "complete":
                self.complete_visit(visit.get("finish_reason", reason))
                return
            if (session.get("stop_phase") in {"draining", "analyzing"}
                    and time.time() - visit["stop_requested_at"] < 210):
                self.save(status="finishing_visit")
                return
        visit["stop_requested_at"] = visit.get("stop_requested_at", time.time())
        visit["finish_reason"] = reason
        self.save(status="finishing_visit")
        await self.request("POST", f"/api/sessions/{sid}/stop")
        self.complete_visit(reason)

    def complete_visit(self, reason):
        visit = self.state.get("current_visit")
        if not visit:
            return
        sid = visit["session_id"]
        visit["ended_at"], visit["finish_reason"] = time.time(), reason
        self.state["visits"].append(visit)
        self.state["current_visit"] = None
        hold = self.state.get("until_offline") or {}
        held = hold.get("status") == "active" and hold.get("room_id") == visit["room"]["room_id"]
        if held and reason == "stream_ended":
            hold.update(status="complete", ended_at=time.time())
        if reason in {"capture_failed", "stream_stalled", "no_media"}:
            self.state["retry_after"][str(visit["room"]["room_id"])] = time.time() + (30 if held else 600)
        elif reason == "stream_ended":
            room_id = visit["room"]["room_id"]
            self.state["retry_after"][str(room_id)] = time.time() + 60
            self.rooms = [room for room in self.rooms if room["room_id"] != room_id]
            self.refreshed_at = 0
            self.refresh_retry_at = 0
        self.save(status="between_rooms")
        self.event("visit_finished", session_id=sid, room_id=visit["room"]["room_id"], reason=reason)

    async def read_room_live_status(self, room_id):
        # Read the explicit field: resolve_room defaults a missing status to 0,
        # which is useful for display but cannot prove that a stream has ended.
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            bili = BilibiliClient(cookie=self.settings.upload_cookie(), client=client)
            data = await bili._json("/room/v1/Room/get_info", {"room_id": room_id})
        value = data.get("live_status")
        return int(value) if type(value) in {str, int} and str(value) in {"0", "1", "2"} else None

    async def room_ended(self, visit):
        now = time.time()
        checked = visit.get("room_status_checked_at")
        if checked is not None and now - checked < self.config.room_status_seconds:
            return False
        visit["room_status_checked_at"] = now
        try:
            status = await asyncio.wait_for(self.read_room_live_status(visit["room"]["room_id"]), timeout=12)
        except Exception as exc:
            visit["room_status_error"] = type(exc).__name__
            # A timeout, access error or malformed response is unknown, not offline.
            visit.pop("observed_live_status", None)
            self.save()
            return False
        visit["observed_live_status"] = status
        visit["room_status_error"] = None if status is not None else "unknown_live_status"
        self.save()
        return status in {0, 2}

    async def tick(self):
        snapshot = await self.request("GET", "/api/state")
        await self.reconcile_start(snapshot)
        if self.state.get("pending_start"):
            self.save(status="reconciling_start")
            return
        reason = self.capacity(snapshot)
        if reason:
            await self.finish_visit(reason)
            self.save(status="capacity_reached", error=reason)
            self.stopping.set()
            return
        if (self.state.get('following_error') in {'upload_cookie_missing', 'upload_cookie_not_logged_in'}
                and self.cookie_revision() != self.state.get('following_cookie_revision')):
            # A user-supplied fresh login can resume on the next tick. This does
            # not reset rate-limit cooldowns or try a different identity.
            self.refresh_retry_at = 0
        if (time.time() - self.refreshed_at >= self.config.refresh_seconds
                and time.time() >= self.refresh_retry_at):
            try:
                await asyncio.wait_for(self.follows(), timeout=45)
            except Exception as exc:
                # Follow-list outages must not prevent monitoring/stopping the
                # room already being captured.
                message = str(exc)
                code = (message if message in {'upload_cookie_missing', 'upload_cookie_not_logged_in'}
                        else 'bilibili_rate_limited' if re.search(r'HTTP (?:412|429)|错误码 -(?:412|509)', message)
                        else type(exc).__name__)
                failures = int(self.state.get('following_failure_count') or 0) + 1
                delay = (300 if code.startswith('upload_cookie_') else
                         900 if code == 'bilibili_rate_limited' else min(900, 60 * 2 ** min(failures-1, 4)))
                self.refresh_retry_at = time.time() + delay
                self.save(following_error=code, following_retry_at=self.refresh_retry_at,
                          following_failure_count=failures,
                          following_cookie_revision=self.cookie_revision())
                self.event('following_refresh_failed', error=code, retry_at=self.refresh_retry_at)
        visit = self.state.get("current_visit")
        if visit:
            sid = visit["session_id"]
            session = next((s for s in snapshot["sessions"] if s["id"] == sid), None)
            if not session:
                self.save(status="owned_session_missing", error="所属会话不存在，停止轮巡")
                self.stopping.set()
                return
            if visit.get("stop_requested_at"):
                # A lost stop response cannot cause a second stop or next-room
                # creation while the server still flushes audio/final analysis.
                if session.get("stop_phase") == "complete":
                    self.complete_visit(visit.get("finish_reason", "recovered_stop"))
                elif (session["status"] == "interrupted" or
                      time.time() - visit["stop_requested_at"] >= 210 or
                      (session["status"] in ACTIVE and not session.get("stop_phase"))):
                    # After the bounded drain/analysis window, a persisted
                    # phase can be stale following a workbench restart. Its
                    # idempotent stop endpoint serializes a safe retry.
                    await self.finish_visit(visit.get("finish_reason", "recovered_stop"))
                else:
                    self.save(status="finishing_visit")
                return
            automatic = policy_enabled(self.settings)
            if bool(session.get("auto_publish")) != automatic or not session.get("auto_render"):
                await self.request("PATCH", f"/api/sessions/{sid}",
                                   {"auto_publish": automatic, "auto_render": True})
            self.state["auto_publish"] = automatic
            duration = float(session.get("duration", 0))
            if duration > visit["last_duration"]:
                visit.update(last_duration=duration, last_media_at=time.time())
            jobs = [j for j in snapshot["jobs"] if j["session_id"] == sid]
            self.save(status="watching", current_duration=duration,
                      current_anchors=sum(a["session_id"] == sid for a in snapshot["anchors"]),
                      current_completed_clips=sum(j["status"] == "complete" for j in jobs),
                      total_completed_clips=sum(j["status"] == "complete" for j in snapshot["jobs"]
                                                if j["session_id"] in
                                                {v["session_id"] for v in self.state["visits"]} | {sid}))
            if await self.room_ended(visit):
                await self.finish_visit("stream_ended")
            elif session["status"] in TERMINAL:
                await self.finish_visit("capture_failed" if session["status"] != "stopped" else "capture_stopped")
            elif duration == 0 and time.time() - visit["started_at"] > self.config.startup_timeout:
                await self.finish_visit("no_media")
            elif time.time() - visit["last_media_at"] > self.config.startup_timeout:
                await self.finish_visit("stream_stalled")
            elif not visit.get("until_offline") and time.time() >= visit["finish_after"]:
                await self.finish_visit("scheduled_rotation")
            return
        if any(s.get("mode") == "live" and s["status"] in ACTIVE
               and s.get("capture_lane") != "extra"
               for s in snapshot["sessions"]):
            self.save(status="waiting_for_workbench", error="已有其他直播采集，轮巡等待")
            return
        occupied = {session_room(s) for s in snapshot["sessions"] if s.get("mode") == "live" and s["status"] in ACTIVE}
        available = [r for r in self.rooms if r["room_id"] not in occupied]
        hold = self.state.get("until_offline") or {}
        if hold.get("status") == "active":
            held_room = next((r for r in available if r["room_id"] == hold["room_id"]), None)
            if held_room is None:
                try:
                    status = await self.read_room_live_status(hold["room_id"])
                except Exception:
                    status = None
                if status in {0, 2}:
                    hold.update(status="complete", ended_at=time.time())
                    self.save()
                elif status == 1 and hold.get("room") and hold["room_id"] not in occupied:
                    held_room = hold["room"]
                else:
                    self.save(status="waiting_for_held_room")
                    return
            if hold.get("status") == "active":
                if self.state["retry_after"].get(str(hold["room_id"]), 0) > time.time():
                    self.save(status="reconnecting_held_room")
                    return
                await self.start_visit(held_room, snapshot)
                return
        if self.state.get("following_error"):
            auth = self.state['following_error'] in {'upload_cookie_missing', 'upload_cookie_not_logged_in'}
            self.save(status="waiting_for_login" if auth else "waiting_for_following_api",
                      error=self.state['following_error'])
            return
        room = choose_room(available, self.state["visits"], self.state["retry_after"], time.time())
        if not self.state["visits"] and self.config.first_room:
            # Preference applies once and only if the room is actually present
            # in the authenticated live-following list.
            room = next((r for r in available if r["room_id"] == self.config.first_room), room)
        if self.state.get("requested_next_room"):
            requested = self.state["requested_next_room"]
            preferred = next((r for r in available if r["room_id"] == requested), None)
            if preferred:
                room = preferred
            else:
                self.event("requested_room_not_live", room_id=requested)
                self.save(requested_next_room=None)
        if room:
            await self.start_visit(room, snapshot)
        else:
            self.save(status="waiting_for_live_following", error=None)

    async def run(self):
        # One lock per workbench data directory prevents separate run directories
        # from creating duplicate patrol controllers.
        lock_path = self.settings.data_dir / "following-patrol.lock"
        self._lock = lock_path.open("a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            await self.client.aclose()
            self._lock.close()
            raise RuntimeError("已有关注轮巡进程正在运行") from None
        # Only the process that acquired the singleton lock may resume a
        # previously stopped run; a duplicate launch cannot cancel STOP.
        (self.directory / "STOP").unlink(missing_ok=True)
        hold = self.state.get("until_offline") or {}
        consumed_hold_preference = (hold.get("status") == "complete"
                                    and hold.get("room_id") == self.config.next_room)
        if self.config.next_room and not consumed_hold_preference:
            self.state["requested_next_room"] = self.config.next_room
            self.state["retry_after"].pop(str(self.config.next_room), None)
            self.event("requested_next_room", room_id=self.config.next_room)
        hold = self.state.get("until_offline") or {}
        visit = self.state.get("current_visit")
        if (visit and hold.get("status") == "active"
                and visit["room"]["room_id"] == hold.get("room_id")):
            visit.update(until_offline=True, planned_duration_seconds=None,
                         scheduled_end_at=None, finish_after=None)
        self.save(pid=os.getpid(), status="starting", error=None)
        failures = 0
        try:
            while not self.stopping.is_set() and not (self.directory / "STOP").exists():
                try:
                    await self.tick()
                    failures = 0
                    self.save(consecutive_errors=0)
                except Exception as exc:
                    failures += 1
                    code = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith(
                        ("local_api_http_", "upload_cookie_", "session_start_uncertain")) else type(exc).__name__
                    self.save(status="retrying", error=code, consecutive_errors=failures)
                    self.event("retry", error=code, consecutive_errors=failures)
                    if code == "session_start_uncertain":
                        self.stopping.set()
                        break
                delay = min(300, self.config.poll_seconds * 2 ** min(failures, 5))
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), delay)
        finally:
            with suppress(Exception):
                await self.finish_visit("patrol_stopped")
            terminal = self.state.get("status")
            unresolved = bool(self.state.get("current_visit") or self.state.get("pending_start"))
            self.save(status=terminal if terminal in {"capacity_reached", "owned_session_missing"}
                      else "stop_pending" if unresolved else "stopped", stopped_at=time.time())
            await self.client.aclose()
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()


def health(directory: Path):
    """Read state only; never instantiate Store (its startup mutates sessions)."""
    state = read_json(Path(directory) / "status.json", {}) or {}
    pid = state.get("pid")
    running = False
    if pid:
        try:
            os.kill(int(pid), 0)
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            running = b"run_following_patrol.py" in cmdline
        except (OSError, ValueError):
            pass
    return {**state, "process_running": running,
            "heartbeat_age_seconds": round(time.time() - state.get("updated_at", time.time()), 1),
            "stop_requested": (Path(directory) / "STOP").exists()}
