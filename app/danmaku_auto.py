"""Explicitly enabled, session-bound, finite danmaku trials.

The process never restores an active trial from disk. A send with an uncertain
outcome stops the trial, rather than replaying a possibly delivered message.
"""
import asyncio
import json
import math
import os
import random
import time
from contextlib import suppress

from .danmaku_send import DanmakuSendError


class AutoDanmaku:
    def __init__(self, workspace):
        self.workspace = workspace
        self.task = None
        self.stop_requested = False
        self.deadline = self.next_due = 0.0
        self.sent_texts = set()
        self.data = {"status": "idle", "session_id": None, "room_id": None,
                     "sent_count": 0, "skipped_count": 0, "history": [],
                     "next_send_at": None, "last_text": None, "error": None}

    def snapshot(self):
        return {**self.data, "history": [dict(item) for item in self.data["history"]]}

    def _persist(self):
        sid = self.data["session_id"]
        if not sid:
            return
        folder = self.workspace.settings.data_dir / "sessions" / sid
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "danmaku_auto.private.json"
        temp = target.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(self.data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)

    async def publish(self):
        self._persist()
        await self.workspace.emit("danmaku_auto", self.snapshot())

    async def start(self, session_id, interval_seconds=300, duration_seconds=1800, max_messages=6):
        ws = self.workspace
        if not ws.session or ws.session["id"] != session_id:
            raise DanmakuSendError("直播会话已变更，请在当前房间重新启动。", 409)
        if not ws.room or ws.room.live_status != 1 or ws.session["status"] != "live":
            raise DanmakuSendError("请先连接正在直播的房间。", 409)
        if not ws.danmaku_credentials.status()["configured"]:
            raise DanmakuSendError("请先配置本人弹幕登录。", 401)
        if type(interval_seconds) is not int or not 5 <= interval_seconds <= 3600:
            raise DanmakuSendError("发送间隔请输入 5–3600 秒的整数。")
        if (duration_seconds, max_messages) != (1800, 6):
            raise DanmakuSendError("一键融入每轮最多 30 分钟、6 条。")
        if self.data["status"] == "active":
            return self.snapshot()  # Repeated clicks cannot reset the quota.
        now = time.time()
        self.deadline = time.monotonic() + duration_seconds
        self.next_due = time.monotonic()  # First eligible message, then a randomized interval.
        self.sent_texts = set()
        self.data = {"status": "active", "session_id": session_id, "room_id": ws.room.room_id,
                     "started_at": now, "ends_at": now + duration_seconds, "next_send_at": now,
                     "interval_seconds": interval_seconds, "jitter_ratio": 0.3,
                     "next_interval_seconds": None, "max_messages": max_messages,
                     "sent_count": 0, "skipped_count": 0, "last_text": None,
                     "last_sent_at": None, "reason": "等待本轮新鲜候选", "error": None, "history": []}
        self.stop_requested = False
        try:
            await self.publish()
        except Exception:
            self.data.update(status="error", reason="无法保存试运行记录", next_send_at=None)
            raise DanmakuSendError("无法保存试运行记录，未启动自动发送。", 503, "storage_unavailable") from None
        self.task = asyncio.create_task(self.run())
        return self.snapshot()

    def _schedule_next(self):
        """Draw once per slot; polling and reconnecting cannot reroll the due time."""
        base = self.data["interval_seconds"]
        interval = random.uniform(base * 0.7, base * 1.3)
        self.next_due = time.monotonic() + interval
        self.data.update(next_send_at=time.time() + interval, next_interval_seconds=interval)

    async def stop(self, reason="已手动停止"):
        self.stop_requested = True
        if self.data["status"] == "active":
            self.data.update(status="stopped", reason=reason, next_send_at=None, next_interval_seconds=None)
            task, self.task = self.task, None
            if task and task is not asyncio.current_task():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await self.publish()
        return self.snapshot()

    async def finish(self, status, reason, error=None):
        self.data.update(status=status, reason=reason, error=error, next_send_at=None, next_interval_seconds=None)
        await self.publish()

    def choose(self):
        ws = self.workspace
        candidates = ws.danmaku_candidates
        generated = candidates.get("generated_at")
        # Old drafts remain useful for manual editing, but cannot be auto-sent.
        if (candidates.get("session_id") != self.data["session_id"]
                or getattr(ws, "pending_danmaku_style_updates", 0)
                or candidates.get("style_revision") != ws.danmaku_style["revision"]
                or not isinstance(generated, (int, float)) or not math.isfinite(generated)
                or not 0 <= time.time() - generated <= 30
                or generated < self.data["started_at"]
                or not candidates.get("sources")):
            return None
        age = ws.agent._observer_snapshot().get("understanding_age_seconds") if ws.agent else None
        if not isinstance(age, (int, float)) or not math.isfinite(age) or age > 15:
            return None
        audience = {str(item.get("text", "")).strip() for item in ws.danmaku}
        choices = []
        for item in candidates.get("items", []):
            text = item.get("text")
            if not isinstance(text, str):
                continue
            text = text.strip()
            try:
                length = len(text.encode("utf-16-le")) // 2
            except UnicodeError:
                continue
            if (1 <= length <= 20 and not any(ord(c) < 32 or ord(c) == 127 for c in text)
                    and text not in self.sent_texts and text not in audience):
                choices.append(text)
        return random.choice(choices) if choices else None

    async def tick(self):
        """Called under the workspace lock, shared with room switches and sends."""
        ws = self.workspace
        if self.data["status"] != "active":
            return
        if self.stop_requested:
            await self.finish("stopped", "已手动停止")
            return
        if not ws.session or ws.session["id"] != self.data["session_id"] or not ws.room or ws.room.room_id != self.data["room_id"]:
            await self.finish("stopped", "直播间已变更")
            return
        if ws.room.live_status != 1 or ws.session["status"] != "live":
            await self.finish("stopped", "直播已结束或连接中断")
            return
        now = time.monotonic()
        if now >= self.deadline or self.data["sent_count"] >= self.data["max_messages"]:
            await self.finish("completed", "本轮试运行已结束")
            return
        if now < self.next_due:
            return
        # Give the first fresh candidate a brief warm-up without sending old
        # drafts. Later missing candidates skip an entire slot, never catch up.
        text = self.choose()
        if not text and self.data["sent_count"] == 0 and time.time() - self.data["started_at"] < 25:
            self.next_due = now + 2
            self.data["next_send_at"] = time.time() + 2
            return
        if not text or (ws.last_danmaku_sent_at is not None and now - ws.last_danmaku_sent_at < 3):
            self._schedule_next()
            self.data["skipped_count"] += 1
            self.data["reason"] = "本轮没有新鲜且不重复的候选，已跳过"
            await self.publish()
            return
        # Resolve again just before the external side effect; a cached live
        # flag must not send after the room has gone offline.
        try:
            current = await asyncio.wait_for(ws.client.resolve_room(ws.room.room_id), timeout=8)
        except Exception:
            await self.finish("error", "已停止", "无法确认直播间状态，请检查后重新启动。")
            return
        if current.live_status != 1 or current.room_id != self.data["room_id"]:
            await self.finish("stopped", "直播已结束或直播间已变更")
            return
        if self.stop_requested:
            await self.finish("stopped", "已手动停止")
            return
        if time.monotonic() >= self.deadline:
            await self.finish("completed", "本轮试运行已结束")
            return
        text = self.choose()  # The room lookup may have outlived the evidence.
        if not text:
            self._schedule_next()
            self.data["skipped_count"] += 1
            self.data["reason"] = "候选已过期，本轮跳过"
            await self.publish()
            return
        entry = {"attempted_at": time.time(), "text": text, "status": "pending"}
        self.data["history"].append(entry)
        await self.publish()  # Record intent before POST; disk failure prevents send.
        generated = ws.danmaku_candidates["generated_at"]
        style_revision = ws.danmaku_style["revision"]

        def before_post():
            # Authentication may await a slow upstream GET. Check again at the
            # actual POST boundary, without an await between check and POST.
            if (self.stop_requested or self.data["status"] != "active"
                    or not ws.session or ws.session["id"] != self.data["session_id"]
                    or not ws.room or ws.room.room_id != self.data["room_id"]
                    or ws.room.live_status != 1 or ws.session["status"] != "live"):
                raise DanmakuSendError("本轮已停止或直播连接已变更，未发送。", 409, "trial_stopped")
            if time.monotonic() >= self.deadline:
                raise DanmakuSendError("本轮时间已到，未发送。", 409, "trial_expired")
            if (style_revision != ws.danmaku_style["revision"]
                    or not 0 <= time.time() - generated <= 30 or self.choose() is None):
                raise DanmakuSendError("候选依据已过期，未发送。", 409, "trial_stale")

        try:
            await ws._send_danmaku_locked(self.data["session_id"], text, before_post=before_post, source="auto")
        except DanmakuSendError as exc:
            if exc.code in {"trial_stopped", "trial_expired", "trial_stale"}:
                entry.update(status="not_sent", code=exc.code)
                if exc.code == "trial_stale":
                    self._schedule_next()
                    self.data["skipped_count"] += 1
                    self.data["reason"] = str(exc)
                    await self.publish()
                else:
                    await self.finish("completed" if exc.code == "trial_expired" else "stopped", str(exc))
                return
            entry.update(status="unknown" if exc.code == "send_unknown" else "failed", code=exc.code)
            await self.finish("error", "发送未确认，已停止且不会自动重试", str(exc))
            return
        self.sent_texts.add(text)
        self.data["sent_count"] += 1
        entry.update(status="accepted", sent_at=time.time())
        self.data.update(last_text=text, last_sent_at=entry["sent_at"], reason="Bilibili 已受理，等待下一轮")
        if self.data["sent_count"] >= self.data["max_messages"]:
            await self.finish("completed", "已发送 6 条，本轮结束")
        else:
            self._schedule_next()
            await self.publish()

    async def run(self):
        try:
            while self.data["status"] == "active":
                async with self.workspace.lock:
                    await self.tick()
                if self.data["status"] != "active":
                    break
                await asyncio.sleep(max(.1, min(2, self.next_due - time.monotonic(), self.deadline - time.monotonic())))
        except asyncio.CancelledError:
            raise
        except Exception:
            self.data.update(status="error", reason="一键融入已停止", error="处理异常，未自动重试，请检查发送记录。", next_send_at=None, next_interval_seconds=None)
            with suppress(Exception):
                await self.publish()
