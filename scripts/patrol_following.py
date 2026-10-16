#!/usr/bin/env python3
"""Bounded, explicitly authorized following-list patrol using the local demo.

No external send implementation lives here: the existing session-fenced auto
sender enforces freshness, jitter and uncertain-delivery handling. Records stay
under ignored data/patrol. Run `stop --run-dir ...` to request a graceful stop.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import signal
import sys
import time
import traceback
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from app.bilibili import BilibiliClient
from app.config import Settings
from app.danmaku_send import DanmakuCredentials

EVENTS = {"observation", "audio_segment", "audio_speakers", "danmaku", "danmaku_candidates",
          "danmaku_chorus", "danmaku_auto", "sent_danmaku", "error", "danmaku_send_status",
          "question_started", "plan", "answer_done"}
PRIVATE_KEYS = {"uid", "owner_uid", "user_id", "user_name", "uname", "user", "username",
                "cookie", "api_key", "token", "csrf", "csrf_token", "bili_jct", "sessdata",
                "embedding", "embeddings", "vector", "voiceprint", "reference_samples", "raw_response"}
ROOM_KEYS = ("room_id", "short_id", "title", "uname", "area_v2_name", "area_v2_parent_name")


def clean(value):
    if isinstance(value, dict):
        def allowed(key):
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            return (key.lower() not in PRIVATE_KEYS and not key.lower().endswith("_uid")
                    and not key.lower().endswith("_token") and normalized not in
                    {"userid", "owneruid", "username", "authorization", "apikey", "accesstoken", "refreshtoken",
                     "referencevoices", "voiceembedding", "userface", "useravatar"})
        return {k: clean(v) for k, v in value.items() if allowed(k)}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        if re.search(r"(?:SESSDATA|bili_jct|csrf_token|api_key)\s*[=:]", value, re.I):
            return "[credential-like value omitted]"
    return value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp.replace(path)


def append(path, value):
    with Path(path).open("a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def room_quota(room_id, visits, cap):
    # A repeated visit never resets the room's allowance for this 12-hour run.
    return max(0, cap - sum(int(v.get("sent_count", 0)) for v in visits
                           if str(v.get("room", {}).get("room_id")) == str(room_id)))


def choose_room(rooms, visits, current_room=None):
    last = {}
    for visit in visits:
        last[int(visit["room"]["room_id"])] = float(visit.get("started_at", 0))
    pool = list(rooms)
    random.shuffle(pool)
    pool.sort(key=lambda r: (last.get(int(r["room_id"]), 0), int(r["room_id"]) == current_room))
    return pool[0] if pool else None


def disable_reason(auto):
    if auto.get("status") == "error":
        return auto.get("error") or auto.get("reason") or "自动发送出现错误，待人工复核"
    return None


class Patrol:
    def __init__(self, args):
        self.args = args
        self.settings = Settings.from_env()
        self.root = args.run_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = None
        self.http = httpx.AsyncClient(base_url=args.base_url, timeout=25, trust_env=False)
        self.stop = asyncio.Event()
        self.visit = None
        self.visit_dir = None
        self.visits = []
        self.seen = set()
        self.recording = None
        self.recording_log = None
        self.recording_url = None
        self.recording_retry_at = 0
        self.sse_task = None
        self.owned_sid = None
        self.resume_visit = None
        self.question_history = []
        now = time.time()
        self.state = {"run_id": self.root.name, "status": "starting", "started_at": now,
                      "deadline": now + args.hours * 3600, "pid": os.getpid(),
                      "config": {"dwell_seconds": args.dwell, "duration_hours": args.hours,
                                 "interval_seconds": args.interval, "max_messages": args.max_messages,
                                 "jitter_ratio": .3, "quota_scope": "per_room_across_entire_run",
                                 "send_authorized": args.send, "warmup_seconds": args.warmup,
                                 "candidate_only": not args.send,
                                 "allow_manual_send": getattr(args, "allow_manual_send", False),
                                 "candidate_count": getattr(args, "candidate_count", 5),
                                 "candidate_scope": "per_generation_batch",
                                 "virtual_fallback": getattr(args, "virtual_fallback", False),
                                 "room_selection": "live_following_first",
                                 "first_room": args.first_room, "first_dwell_seconds": args.first_dwell,
                                 "first_max_messages": args.first_max_messages,
                                 "max_recording_gib": args.max_recording_gib},
                      "visits": [], "current_visit": None, "sending_disabled_reason": None,
                      "timezone": "UTC; review pages display Asia/Shanghai", "recording_bytes": 0}

    def active(self):
        return not self.stop.is_set() and not (self.root / "STOP").exists() and time.time() < self.state["deadline"]

    async def delay(self, seconds):
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop.wait(), min(seconds, max(0, self.state["deadline"]-time.time())))

    def save(self):
        self.state["updated_at"] = time.time()
        write_json(self.root / "status.json", self.state)
        if self.visit:
            write_json(self.visit_dir / "visit.json", self.visit)

    def log(self, kind, data):
        append(self.root / "run-events.jsonl", {"time": time.time(), "type": kind, "data": clean(data)})
        print(datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, json.dumps(clean(data), ensure_ascii=False), flush=True)

    async def request(self, method, path, payload=None):
        response = await self.http.request(method, path, json=payload)
        if response.status_code >= 400:
            # Local API errors are fixed strings, but never log a raw body.
            raise RuntimeError(f"local_api_http_{response.status_code}")
        return response.json()

    async def following(self):
        cookie, _ = DanmakuCredentials(self.settings.data_dir, self.settings.bilibili_cookie)._read()
        rooms, seen_rooms = {}, set()
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            bili = BilibiliClient(cookie=cookie, client=client)
            for page in range(1, 51):
                data = await bili._json("/xlive/web-ucenter/v1/xfetter/GetWebList", {"page": page, "page_size": 10})
                items = data.get("list") or data.get("rooms") or []
                new_ids = set()
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        rid = int(item["room_id"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if rid > 0:
                        new_ids.add(rid)
                        if str(item.get("live_status")) != "1":
                            continue
                        room = {k: item.get(k) for k in ROOM_KEYS}
                        room["room_id"] = rid
                        room["area"] = item.get("area_v2_name", "")
                        rooms[rid] = room
                repeated_page = bool(new_ids) and new_ids <= seen_rooms
                seen_rooms.update(new_ids)
                if len(items) < 10 or repeated_page:
                    break
        self.state["following_refreshed_at"] = time.time()
        self.state["live_following_count"] = len(rooms)
        # The full followed account list is neither persisted nor made public.
        return list(rooms.values())

    async def virtual_rooms(self):
        """Discover a bounded pool from Bilibili's live virtual-streamer area."""
        rooms = {}
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            bili = BilibiliClient(client=client)
            for page in range(1, 6):
                data = await bili._json("/room/v3/area/getRoomList", {
                    "platform": "web", "parent_area_id": 9, "area_id": 0,
                    "sort_type": "online", "page": page, "page_size": 30})
                items = data.get("list") or []
                before = len(rooms)
                for item in items:
                    if not isinstance(item, dict) or str(item.get("area_v2_parent_id")) != "9":
                        continue
                    try:
                        rid = int(item["roomid"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if rid > 0:
                        rooms[rid] = {"room_id": rid, "title": str(item.get("title") or ""),
                                      "area": str(item.get("area_v2_name") or "虚拟主播")}
                if len(items) < 30 or len(rooms) == before:
                    break
        self.state["virtual_refreshed_at"] = time.time()
        self.state["virtual_pool_count"] = len(rooms)
        return list(rooms.values())

    async def next_room(self):
        if not self.visits and self.args.first_room:
            self.state["selection_source"] = "requested_first"
            return {"room_id": self.args.first_room, "title": "用户指定首站",
                    "area": "", "selection_source": "requested_first"}
        plan = self.read_plan()
        used = {v.get("room", {}).get("plan_id") for v in self.visits}
        for item in plan.get("rooms", []):
            if item["plan_id"] not in used:
                self.state["selection_source"] = "requested_queue"
                return {**item, "selection_source": "requested_queue", "title": "用户指定下一站", "area": ""}
        # Failure to read follows is not evidence that no follows are live.
        rooms = await self.following()
        source = "following"
        if not rooms and getattr(self.args, "virtual_fallback", False):
            rooms = await self.virtual_rooms()
            source = "virtual_area"
        self.state["selection_source"] = source
        room = choose_room(rooms, self.visits, self.visits[-1]["room"]["room_id"] if self.visits else None)
        return {**room, "selection_source": source} if room else None

    def read_plan(self):
        path = self.root / "plan.json"
        if not path.exists():
            return {}
        plan = json.loads(path.read_text())
        for item in plan.get("rooms", []):
            if (type(item.get("room_id")) is not int or item["room_id"] <= 0
                    or not isinstance(item.get("plan_id"), str) or not item["plan_id"]
                    or not 60 <= item.get("dwell_seconds", self.args.dwell) <= 3600
                    or item.get("question_focus") not in {None, "game_and_speech"}):
                raise ValueError("Invalid patrol room plan")
            rounds = item.get("simulation_rounds_per_visit", 10)
            if type(rounds) is not int or not 1 <= rounds <= 60:
                raise ValueError("Invalid patrol simulation round count")
        return plan

    def restore(self):
        """Explicit candidate-only continuation; never renew a run's deadline."""
        state = json.loads((self.root / "status.json").read_text())
        config = state["config"]
        if self.args.send or not config.get("candidate_only") or state.get("status") == "user_took_over":
            raise ValueError("Only an owned candidate-only run may be resumed")
        allow_manual_send = config.get("allow_manual_send", False)
        if type(allow_manual_send) is not bool:
            raise ValueError("Invalid manual send authorization")
        self.args.allow_manual_send = allow_manual_send
        for attr, key in (("hours", "duration_hours"), ("dwell", "dwell_seconds"),
                          ("interval", "interval_seconds"), ("max_messages", "max_messages"),
                          ("warmup", "warmup_seconds"), ("first_room", "first_room"),
                          ("first_dwell", "first_dwell_seconds"), ("first_max_messages", "first_max_messages"),
                          ("virtual_fallback", "virtual_fallback"), ("max_recording_gib", "max_recording_gib")):
            if key in config:
                setattr(self.args, attr, config[key])
        self.state = state
        self.state.update(pid=os.getpid(), resumed_at=time.time())
        self.state.pop("ended_at", None)
        self.visits = []
        for vid in state["visits"]:
            if not re.fullmatch(r"\d+-\d+", vid):
                raise ValueError("Invalid visit path")
            visit = json.loads((self.root / "visits" / vid / "visit.json").read_text())
            self.visits.append(visit)
        wanted = getattr(self.args, "resume_visit", None)
        if wanted:
            matches = [v for v in self.visits if v["visit_id"] == wanted]
            if not matches or matches[0] is not self.visits[-1] or matches[0].get("error"):
                raise ValueError("Only the last healthy visit may be continued")
            if matches[0].get("scheduled_end_at", 0) > time.time():
                self.resume_visit = matches[0]
        self.configure_plan()

    def configure_plan(self):
        plan = self.read_plan()
        if not plan:
            return
        if self.args.send:
            raise ValueError("Simulation plans cannot authorize sending")
        self.args.candidate_count = 5
        self.state["config"].update(candidate_count=5, simulation_rounds_per_visit=10,
            simulation_candidate_count=5, simulation_scope="per_visit_evenly_spaced",
            questions_enabled=bool(plan.get("questions_enabled")),
            question_interval_seconds=plan.get("question_interval_seconds", 165),
            manual_questions_extra=True, question_queue_policy="one_planned_with_manual_serial")
        self.state["room_plan"] = plan.get("rooms", [])

    def emit(self, kind, data, event_time=None):
        if not self.visit:
            return
        data = clean(data)
        if kind == "danmaku":
            data = {k: data[k] for k in ("text", "timestamp", "sent_at", "received_at", "message_id", "room_id") if k in data}
        if kind in {"observation", "audio_segment", "danmaku", "danmaku_candidates", "danmaku_auto", "sent_danmaku",
                    "danmaku_chorus", "question_started", "question_requested", "plan", "answer_done"}:
            fingerprint = (kind, hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            if fingerprint in self.seen:
                return
            self.seen.add(fingerprint)
        append(self.visit_dir / "events.jsonl", {"time": event_time or time.time(), "session_id": self.owned_sid,
                                                "type": kind, "data": data})
        counts = self.visit.setdefault("event_counts", {})
        counts[kind] = counts.get(kind, 0) + 1
        if kind in {"question_started", "question_requested", "plan", "answer_done"}:
            append(self.visit_dir / "questions.jsonl", {"time": event_time or time.time(),
                "session_id": self.owned_sid, "type": kind, "data": data})
        if kind == "danmaku_chorus":
            append(self.visit_dir / "chorus.jsonl", {"time": event_time or time.time(),
                "session_id": self.owned_sid, "type": kind, "data": data})
        if kind == "danmaku_candidates" and data.get("status") == "ready" and data.get("items"):
            batch_key = ("candidate_batch", data.get("generated_at"),
                         tuple(item.get("text", "") for item in data["items"]))
            if batch_key not in self.seen:
                self.seen.add(batch_key)
                self.visit["candidate_batches"] = self.visit.get("candidate_batches", 0) + 1
                self.visit["candidate_total"] = self.visit.get("candidate_total", 0) + len(data["items"])
                self.visit["last_candidate_count"] = len(data["items"])
                append(self.visit_dir / "candidates.jsonl", {"time": event_time or time.time(),
                    "session_id": self.owned_sid, "batch": self.visit["candidate_batches"], "data": data})
        if kind == "danmaku_auto":
            self.visit["auto_status"] = data.get("status")
            self.visit["sent_count"] = max(self.visit.get("sent_count", 0), int(data.get("sent_count", 0)))
            self.visit["skipped_count"] = int(data.get("skipped_count", 0))
            if self.visit.get("auto_started") and disable_reason(data):
                self.state["sending_disabled_reason"] = disable_reason(data)
                self.visit["error"] = disable_reason(data)
        if kind == "sent_danmaku":
            self.visit["sent_count"] = max(self.visit.get("sent_count", 0), len(data.get("items", [])))

    async def stream_events(self):
        while self.active() and self.visit:
            try:
                async with self.http.stream("GET", "/api/events", timeout=httpx.Timeout(40, read=40)) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not self.active() or not self.visit:
                            return
                        if not line.startswith("data: "):
                            continue
                        item = json.loads(line[6:])
                        if item.get("session_id") != self.owned_sid:
                            continue
                        if item.get("type") in EVENTS:
                            self.emit(item["type"], item.get("data", {}), item.get("time"))
                        elif item.get("type") == "state":
                            self.snapshot_events(item["data"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.emit("capture_gap", {"error_type": type(exc).__name__, "message": "事件流中断；重新连接并补齐有界快照"})
                await self.delay(3)

    def snapshot_events(self, snap):
        if snap.get("session", {}).get("id") != self.owned_sid:
            return
        for kind, items in (("observation", snap.get("observations", [])),
                            ("audio_segment", snap.get("audio", {}).get("segments", [])),
                            ("danmaku", snap.get("danmaku", []))):
            for item in items:
                self.emit(kind, item, item.get("generated_at") or item.get("received_at"))
        for kind in ("danmaku_auto", "danmaku_candidates", "danmaku_chorus"):
            if snap.get(kind):
                self.emit(kind, snap[kind])
        self.emit("sent_danmaku", {"items": snap.get("sent_danmaku", [])})
        for message in snap.get("messages", []):
            if message.get("role") == "user":
                self.emit("question_started", message)
            elif message.get("role") == "assistant" and message.get("status") in {"complete", "error", "cancelled"}:
                self.emit("answer_done", message)

    def simulation_round_count(self):
        rounds = self.visit.get("simulation_rounds_per_visit",
            self.visit.get("room", {}).get("simulation_rounds_per_visit",
                self.state["config"].get("simulation_rounds_per_visit", 10)))
        if type(rounds) is not int or not 1 <= rounds <= 60:
            raise ValueError("Invalid visit simulation round count")
        return rounds

    def simulation_targets(self):
        start = self.visit.get("session_started_at", self.visit["started_at"])
        duration = self.visit["scheduled_end_at"] - start
        rounds = self.simulation_round_count()
        return [start + duration * (i + .5) / rounds for i in range(rounds)]

    def sample_simulation(self, snap, *, final=False):
        if not self.state["config"].get("simulation_rounds_per_visit"):
            return
        now = time.time()
        completed = self.visit.setdefault("simulation_rounds", [])
        targets = self.simulation_targets()
        gap = (self.visit["scheduled_end_at"] - self.visit["session_started_at"]) / self.simulation_round_count()
        grace = min(40, gap * .4)
        for number, target in enumerate(targets, 1):
            if number in completed or now < target:
                continue
            candidates = snap.get("danmaku_candidates", {})
            generated = candidates.get("generated_at") or 0
            fresh = (candidates.get("status") == "ready" and len(candidates.get("items", [])) >= 5
                     and candidates.get("session_id") == self.owned_sid
                     and 0 <= now - generated <= 35 and generated >= target - 30)
            # Never burst through missed slots with the same late candidates.
            if now > target + grace or final:
                status = "skipped"
                reason = "定时槽已过，未用迟到候选补造；原始生成记录另存"
            elif fresh:
                status, reason = "complete", None
            else:
                continue
            data = {"status": status, "reason": reason, "chorus": clean(snap.get("danmaku_chorus", {})),
                    "sent": False, "source_time": snap.get("observer", {}).get("last_timestamp")}
            if status == "complete":
                data.update(candidates=clean({**candidates, "items": candidates["items"][:5]}),
                            source_candidate_count=len(candidates["items"]))
                if len(candidates["items"]) > 5:
                    data.update(candidate_selection="first_5_of_legacy_10", raw_candidates=clean(candidates))
            else:
                data["chorus_capture_note"] = "跳过槽；此为记录时快照，不是过去目标时刻的弹幕"
            event = {"time": now, "session_id": self.owned_sid, "round": number,
                     "target_at": target, "data": data}
            append(self.visit_dir / "simulation_rounds.jsonl", event)
            completed.append(number)
            self.visit["simulation_completed"] = self.visit.get("simulation_completed", 0) + int(status == "complete")
            self.emit("simulation_round", event)
            self.log("simulation_round", {"visit_id": self.visit["visit_id"], "round": number, "status": status})

    async def maybe_question(self, snap):
        if not self.state["config"].get("questions_enabled") or time.time() < self.visit.get("question_retry_at", 0):
            return
        if time.time() >= self.visit["scheduled_end_at"] - 45:
            return
        # Reserve a short runway for each simulation's candidate update.
        if any(0 <= target - time.time() <= 25 for target in self.simulation_targets()):
            return
        from scripts.patrol_questions import next_question
        spec = next_question(self.question_planner_snapshot(snap), room_id=self.visit.get("requested_room_id", self.visit["room"]["room_id"]),
            elapsed_seconds=time.time() - self.visit["session_started_at"], history=self.question_history,
            interval_seconds=self.state["config"].get("question_interval_seconds", 165),
            focus=self.visit["room"].get("question_focus"))
        if not spec:
            return
        intent = {**spec, "origin": "planned", "snapshot_time": snap.get("observer", {}).get("last_timestamp"), "status": "submitting"}
        # On an uncertain POST do not immediately retry or duplicate a question.
        self.visit["question_retry_at"] = time.time() + 165
        self.question_history.append(intent)
        self.visit["question_history"] = self.question_history
        self.save()
        try:
            result = await self.request("POST", "/api/questions", {"session_id": self.owned_sid, "text": spec["text"]})
        except Exception as exc:
            intent["status"] = "submission_uncertain"
            self.emit("question_submission_uncertain", {**intent, "error_type": type(exc).__name__})
            self.save()
            return
        spec = {**spec, "question_id": result["question_id"]}
        intent.update(question_id=result["question_id"], status="queued")
        self.emit("question_requested", {**intent, "question_id": result["question_id"], "status": "queued"})
        self.log("question_requested", {"visit_id": self.visit["visit_id"], "text": spec["text"], "question_id": result["question_id"]})

    def question_planner_snapshot(self, snap):
        """Manual Q&A is extra, not a planned slot or a reset of its clock.

        The agent itself serializes model calls. Permit one planned question
        to queue behind at most two manual questions; never queue a second
        planned question or pretend an unknown in-flight request is manual.
        Only the planner view changes; all real messages remain archived.
        """
        pending = [m for m in snap.get("messages", []) if m.get("role") == "assistant"
                   and m.get("status") in {"queued", "running", "planning", "answering"}]
        count = snap.get("observer", {}).get("active_questions", 0)
        if not pending or len(pending) > 2 or count != len(pending):
            return snap
        planned_ids = {h.get("question_id") for h in self.question_history if h.get("question_id")}
        uncertain_texts = {h.get("text") for h in self.question_history
                           if not h.get("question_id") and h.get("status") in {"submitting", "submission_uncertain"}}
        users = {m.get("question_id"): m.get("text", m.get("content"))
                 for m in snap.get("messages", []) if m.get("role") == "user"}
        if any(m.get("question_id") in planned_ids
               or users.get(m.get("question_id")) in uncertain_texts for m in pending):
            return snap
        ids = {m.get("question_id") for m in pending}
        if None in ids or any(qid not in users for qid in ids):
            return snap
        return {**snap, "observer": {**snap.get("observer", {}), "active_questions": 0},
                "messages": [m for m in snap.get("messages", []) if m.get("question_id") not in ids]}

    def archive_media(self, snap):
        directory = self.settings.data_dir / "sessions" / self.owned_sid
        audio_target = self.visit_dir / "evidence/audio"
        audio_target.mkdir(parents=True, exist_ok=True)
        for item in snap.get("audio", {}).get("segments", []):
            key = item.get("id", "")
            if not re.fullmatch(r"[a-f0-9]{32}", key):
                continue
            source, target = directory / "audio" / f"{key}.wav", audio_target / f"{key}.wav"
            if source.exists() and not target.exists():
                with suppress(OSError):
                    shutil.copyfile(source, target)
        # Preserve exactly the evidence cited by each completed answer before
        # the live agent evicts its rolling frame/audio buffers.
        for message in snap.get("messages", []):
            if message.get("role") != "assistant" or message.get("status") != "complete":
                continue
            for item in message.get("evidence", []):
                key = item.get("frame_id", "")
                if not re.fullmatch(r"[a-f0-9]{32}", key):
                    continue
                source = directory / "agent/frames" / f"{key}.jpg"
                target = self.visit_dir / "evidence/frames" / f"{key}.jpg"
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.exists() and not target.exists():
                    with suppress(OSError):
                        shutil.copyfile(source, target)
            for item in message.get("audio_evidence", []):
                key = item.get("id", "")
                if not re.fullmatch(r"[a-f0-9]{32}", key):
                    continue
                source, target = directory / "audio" / f"{key}.wav", audio_target / f"{key}.wav"
                if source.exists() and not target.exists():
                    with suppress(OSError):
                        shutil.copyfile(source, target)
        # One representative frame every 30 seconds; full recording preserves
        # the remaining visual context without relying on rolling frame APIs.
        if time.time() - self.visit.get("last_frame_saved_at", 0) >= 30:
            files = []
            for path in (directory / "agent/frames").glob("*.jpg"):
                try:
                    files.append((path.stat().st_mtime, path))
                except FileNotFoundError:
                    # Agent pruning can remove a frame after glob yielded it.
                    continue
            for _, source in sorted(files, key=lambda item: item[0], reverse=True):
                target = self.visit_dir / "evidence/frames" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".jpg.tmp")
                try:
                    shutil.copyfile(source, temporary)
                    temporary.replace(target)
                except FileNotFoundError:
                    with suppress(OSError):
                        temporary.unlink()
                    continue  # Try another remaining frame, not another room.
                except OSError:
                    with suppress(OSError):
                        temporary.unlink()
                    break
                append(self.visit_dir / "frames.jsonl", {"time": time.time(), "file": str(target.relative_to(self.visit_dir))})
                self.visit["last_frame_saved_at"] = time.time()
                break

    async def stop_recording(self):
        proc, self.recording = self.recording, None
        if proc:
            if proc.returncode is None:
                with suppress(ProcessLookupError):
                    proc.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(proc.wait(), 8)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
            rec = self.visit["recordings"][-1]
            rec["ended_at"] = time.time()
            rec["exit_code"] = proc.returncode
            path = self.visit_dir / rec["file"]
            rec["bytes"] = path.stat().st_size if path.exists() else 0
            self.state["recording_bytes"] += rec["bytes"]
        if self.recording_log:
            self.recording_log.close()
            self.recording_log = None

    async def ensure_recording(self, snap, deadline):
        playback = snap.get("playback", {})
        url = playback.get("url", "")
        if not playback.get("ready") or not re.fullmatch(r"/api/media/"+self.owned_sid+r"/g\d+/index\.m3u8", url):
            return
        if self.recording and (self.recording.returncode is not None or url != self.recording_url):
            await self.stop_recording()
            self.recording_retry_at = time.time() + 5
        if self.recording or time.time() < self.recording_retry_at:
            return
        if self.state["recording_bytes"] >= self.args.max_recording_gib * 1024**3:
            self.state["sending_disabled_reason"] = "录像达到本轮容量上限，继续保留文本记录"
            return
        number = len(self.visit["recordings"]) + 1
        if number > 12:
            self.visit["recording_error"] = "录像反复中断，已停止本房重试"
            return
        configured = os.environ.get("FFMPEG_BIN", "").strip() or self.settings.ffmpeg_bin
        ffmpeg = shutil.which(str(Path(configured).expanduser())) if configured else None
        ffmpeg = ffmpeg or shutil.which("ffmpeg")
        if not ffmpeg:
            message = "找不到 FFmpeg：请安装并加入 PATH，或将 FFMPEG_BIN 设为可执行文件路径。"
            self.visit["recording_error"] = message
            self.log("recording_unavailable", {"message": message})
            raise FileNotFoundError(message)
        folder = self.visit_dir / "recordings"
        folder.mkdir(exist_ok=True)
        path = folder / f"part-{number:03d}.mkv"
        self.recording_log = (folder / f"part-{number:03d}.log").open("wb")
        try:
            self.recording = await asyncio.create_subprocess_exec(ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
                "-rw_timeout", "15000000", "-i", self.args.base_url.rstrip("/")+url,
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-t", str(max(1, deadline-time.time())),
                "-f", "matroska", str(path), stdout=asyncio.subprocess.DEVNULL, stderr=self.recording_log)
        except BaseException:
            self.recording_log.close()
            self.recording_log = None
            raise
        self.recording_url = url
        self.visit["recordings"].append({"file": str(path.relative_to(self.visit_dir)), "started_at": time.time(),
                                        "pid": self.recording.pid, "timestamp_basis": "recording_process_start; HLS/CDN offset not measured"})

    async def stop_auto(self):
        if self.owned_sid:
            with suppress(Exception):
                await self.request("POST", "/api/danmaku/auto/stop", {"session_id": self.owned_sid})

    async def report(self):
        try:
            from scripts.patrol_report import render_report
            await asyncio.to_thread(render_report, self.root)
        except Exception as exc:
            self.log("report_error", {"error_type": type(exc).__name__})

    async def visit_room(self, room, resume_visit=None):
        if not self.active():
            return
        number = len(self.visits) if resume_visit else len(self.visits)+1
        vid = resume_visit["visit_id"] if resume_visit else f"{number:03d}-{room['room_id']}"
        self.visit_dir = self.root / "visits" / vid
        self.visit_dir.mkdir(parents=True, mode=0o700, exist_ok=bool(resume_visit))
        self.visit = resume_visit or {"visit_id": vid, "room": room, "started_at": time.time(), "status": "connecting",
                      "sent_count": 0, "skipped_count": 0, "auto_started": False, "recordings": [], "lag_samples": []}
        if not resume_visit:
            self.visits.append(self.visit)
            self.state["visits"].append(vid)
        else:
            self.visit.pop("ended_at", None)
            self.visit["resumed_at"] = time.time()
        self.question_history = self.visit.get("question_history", [])
        self.state["current_visit"] = vid
        self.seen = set()
        self.owned_sid = None
        self.recording_url = None
        self.recording_retry_at = 0
        self.save()
        self.log("visit_start", {"visit_id": vid, "room_id": room["room_id"], "title": room.get("title")})
        try:
            before = await self.request("GET", "/api/state")
            before_sid = before.get("session", {}).get("id")
            # A user-changing room between visits takes precedence over patrol.
            if self.state.get("last_owned_session") and before_sid != self.state["last_owned_session"]:
                self.state["status"] = "user_took_over"
                self.stop.set()
                return
            if not self.active():
                return
            try:
                if resume_visit:
                    snap = before
                    if (before_sid != resume_visit.get("session_id")
                            or snap.get("session", {}).get("status") != "live"
                            or snap.get("room", {}).get("room_id") != room.get("room_id")):
                        self.state["status"] = "resume_session_mismatch"
                        self.stop.set()
                        return
                else:
                    payload = {"room": str(room["room_id"]),
                        "candidate_only": not self.args.send,
                        "candidate_count": getattr(self.args, "candidate_count", 5)}
                    if not self.args.send and getattr(self.args, "allow_manual_send", False):
                        payload["allow_manual_send"] = True
                    snap = await self.request("POST", "/api/session", payload)
            except httpx.TimeoutException:
                await self.delay(3)
                snap = await self.request("GET", "/api/state")
                returned_room = snap.get("room", {})
                same_room = room["room_id"] in (returned_room.get("room_id"), returned_room.get("short_id"))
                if not same_room or snap.get("session", {}).get("id") == before_sid:
                    self.state["status"] = "switch_outcome_uncertain"
                    self.stop.set()
                    return
            self.owned_sid = snap["session"]["id"]
            self.state["last_owned_session"] = self.owned_sid
            resolved_room = snap.get("room") or {}
            # Short room links (e.g. 213) resolve to a canonical room number.
            self.visit.setdefault("requested_room_id", room["room_id"])
            if resolved_room.get("room_id"):
                self.visit["room"] = {**room, "room_id": resolved_room["room_id"],
                                      "short_id": resolved_room.get("short_id", room.get("short_id"))}
            if resolved_room.get("title"):
                self.visit["room"]["title"] = resolved_room["title"]
            if not self.args.send:
                await self.stop_auto()
                if snap.get("session", {}).get("candidate_only") is not True:
                    # Old servers ignore new request fields. Never silently
                    # proceed while assuming the send guard was installed.
                    self.state["status"] = "candidate_guard_unavailable"
                    self.visit["error"] = "服务未启用仅候选模式"
                    self.stop.set()
                    return
                allow_manual_send = getattr(self.args, "allow_manual_send", False)
                policy = snap.get("danmaku_send", {})
                if (allow_manual_send and (snap.get("session", {}).get("allow_manual_send") is not True
                                           or policy.get("can_auto_send") is not False)):
                    self.state["status"] = "candidate_guard_unavailable"
                    self.visit["error"] = "服务未启用仅手动发送策略"
                    self.stop.set()
                    return
                self.visit["allow_manual_send"] = allow_manual_send
                if allow_manual_send:
                    self.visit.setdefault("allow_manual_send_since", time.time())
            self.visit.update(session_id=self.owned_sid, session_started_at=snap["session"]["started_at"], status="observing")
            if resume_visit:
                # Rebuild duplicate filters without appending/re-counting history.
                history_path = self.visit_dir / "events.jsonl"
                if history_path.exists():
                    for line in history_path.read_text().splitlines():
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        kind, data = item.get("type"), item.get("data", {})
                        self.seen.add((kind, hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()))
                        if kind == "danmaku_candidates" and data.get("items"):
                            self.seen.add(("candidate_batch", data.get("generated_at"), tuple(x.get("text", "") for x in data["items"])))
            self.snapshot_events(snap)
            self.sse_task = asyncio.create_task(self.stream_events())
            dwell = room.get("dwell_seconds", self.args.first_dwell if number == 1 and self.args.first_room else self.args.dwell)
            deadline = (resume_visit["scheduled_end_at"] if resume_visit
                        else min(self.state["deadline"], time.time()+dwell))
            self.visit["scheduled_end_at"] = deadline
            if self.state["config"].get("simulation_rounds_per_visit"):
                # Freeze this visit's target. A later next-room edit must not
                # redistribute already-recorded slots or change a resumed visit.
                self.visit.setdefault("simulation_rounds_per_visit", self.simulation_round_count())
            last_report, last_data, errors, last_following_check = 0, time.time(), 0, time.time()
            while self.active() and time.time() < deadline:
                try:
                    snap = await self.request("GET", "/api/state")
                    errors = 0
                except Exception as exc:
                    errors += 1
                    self.emit("error", {"source": "patrol_poll", "error_type": type(exc).__name__})
                    if errors >= 6:
                        self.state["sending_disabled_reason"] = "本地服务连续失联，自动发送不再恢复"
                        break
                    await self.delay(5)
                    continue
                if snap.get("session", {}).get("id") != self.owned_sid:
                    self.state["status"] = "user_took_over"
                    self.stop.set()
                    break
                self.snapshot_events(snap)
                self.sample_simulation(snap)
                await self.maybe_question(snap)
                observer = snap.get("observer", {})
                lag = observer.get("understanding_age_seconds")
                if isinstance(lag, (int, float)) and math.isfinite(lag):
                    self.visit["lag_samples"].append(round(lag, 2))
                self.visit["observed"] = observer.get("observed", 0)
                self.visit["style"] = clean(snap.get("danmaku_style", {}))
                health = {"time": time.time(), "session_status": snap.get("session", {}).get("status"),
                          "observer": clean(observer), "audio": {k: v for k, v in snap.get("audio", {}).items()
                          if k in {"status", "model_ready", "received_segments", "processed_segments", "error", "capture"}},
                          "candidate_status": snap.get("danmaku_candidates", {}).get("status"),
                          "auto_status": snap.get("danmaku_auto", {}).get("status")}
                append(self.visit_dir / "health.jsonl", health)
                await self.ensure_recording(snap, deadline)
                self.archive_media(snap)
                if observer.get("model_ready") and lag is not None and lag < 60:
                    last_data = time.time()
                auto = snap.get("danmaku_auto", {})
                if not self.args.send and auto.get("status") == "active":
                    await self.stop_auto()
                    self.emit("error", {"source": "candidate_only", "message": "已停止意外开启的自动发送"})
                if (snap.get("session", {}).get("status") == "stopped"
                        or (self.visit["auto_started"] and not self.state["sending_disabled_reason"]
                            and auto.get("status") == "stopped" and auto.get("reason") == "已手动停止")):
                    self.state["status"] = "user_took_over"
                    self.visit["exit_reason"] = "用户手动停止，轮巡不再接管"
                    self.stop.set()
                    break
                if self.state["sending_disabled_reason"] and auto.get("status") == "active":
                    await self.stop_auto()
                cap = self.args.first_max_messages if room["room_id"] == self.args.first_room else self.args.max_messages
                remaining = room_quota(room["room_id"], self.visits[:-1], cap)
                ready = (snap.get("session", {}).get("status") == "live" and observer.get("model_ready")
                         and isinstance(lag, (float, int)) and lag <= 15
                         and self.recording is not None and self.recording.returncode is None)
                if (self.args.send and not self.state["sending_disabled_reason"] and not self.visit["auto_started"]
                        and remaining > 0 and ready and time.time()-self.visit["started_at"] >= self.args.warmup
                        and deadline-time.time() > 90 and self.active()):
                    # Persist intent BEFORE the mutation, never retry an
                    # uncertain start or reset the app's per-session quota.
                    self.visit["auto_started"] = True
                    self.save()
                    try:
                        started = await self.request("POST", "/api/danmaku/auto/start", {
                            "session_id": self.owned_sid, "interval_seconds": self.args.interval, "max_messages": remaining})
                        self.emit("danmaku_auto", started)
                        self.log("auto_started", {"room_id": room["room_id"], "max_messages": remaining, "interval_seconds": self.args.interval})
                    except Exception as exc:
                        self.state["sending_disabled_reason"] = "启动自动发送未确认："+type(exc).__name__
                        await self.stop_auto()
                room_status = (snap.get("room") or {}).get("live_status")
                if room_status != 1 or snap.get("session", {}).get("status") in {"stopped", "offline", "error"}:
                    self.visit["exit_reason"] = "直播已下播或连接失败"
                    break
                if time.time()-last_data > 180:
                    self.visit["exit_reason"] = "180秒无新鲜理解，保存中断并换房"
                    break
                if (room.get("selection_source") == "virtual_area"
                        and time.time() - last_following_check >= 60):
                    last_following_check = time.time()
                    try:
                        if await self.following():
                            self.visit["exit_reason"] = "关注列表已有开播，优先返回关注房间"
                            break
                    except Exception as exc:
                        self.log("following_refresh_unavailable", {"error_type": type(exc).__name__})
                self.save()
                if time.time()-last_report >= 60:
                    await self.report()
                    last_report = time.time()
                await self.delay(5)
        except Exception as exc:
            self.visit["error"] = type(exc).__name__
            # Store code locations, never exception bodies, locals or secrets.
            locations = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                         for frame in traceback.extract_tb(exc.__traceback__)[-6:]]
            self.visit["error_locations"] = locations
            self.log("visit_error", {"room_id": room["room_id"], "error_type": type(exc).__name__, "locations": locations})
        finally:
            await self.stop_auto()
            if self.owned_sid:
                with suppress(Exception):
                    final = await self.request("GET", "/api/state")
                    if final.get("session", {}).get("id") == self.owned_sid:
                        self.snapshot_events(final)
                        self.sample_simulation(final, final=True)
                        self.archive_media(final)
                        write_json(self.visit_dir / "final-state.json", clean({k: final[k] for k in
                            ("session", "observer", "observations", "memory", "messages", "danmaku_chorus", "danmaku_candidates", "danmaku_auto", "sent_danmaku") if k in final}))
            if self.sse_task:
                self.sse_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.sse_task
                self.sse_task = None
            await self.stop_recording()
            self.visit.update(ended_at=time.time(), status="finished" if not self.visit.get("error") else "error")
            self.save()
            await self.report()
            self.log("visit_end", {"visit_id": vid, "sent_count": self.visit.get("sent_count", 0),
                "reason": self.visit.get("exit_reason") or self.visit.get("error") or "停留结束"})
            self.visit = None

    async def run(self):
        self.lock = (self.settings.data_dir / "patrol.lock").open("a+")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if getattr(self.args, "action", "run") == "resume":
            self.restore()
        elif (self.root / "status.json").exists():
            raise RuntimeError("Existing patrol run is never silently restarted; choose a new run directory.")
        else:
            self.configure_plan()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, self.stop.set)
        self.state["status"] = "running"
        self.save()
        async def deadline_guard():
            await asyncio.sleep(max(0, self.state["deadline"]-time.time()))
            self.stop.set()
            await self.stop_auto()
        guard = asyncio.create_task(deadline_guard())
        try:
            if self.resume_visit and self.active():
                await self.visit_room(self.resume_visit["room"], resume_visit=self.resume_visit)
            while self.active():
                try:
                    room = await self.next_room()
                except Exception as exc:
                    self.log("following_unavailable", {"error_type": type(exc).__name__})
                    self.save()
                    await self.delay(60)
                    continue
                if not room:
                    self.log("no_live_following", {"retry_in_seconds": 60})
                    self.save()
                    await self.report()
                    await self.delay(60)
                    continue
                await self.visit_room(room)
                await self.delay(3)
        finally:
            guard.cancel()
            with suppress(asyncio.CancelledError):
                await guard
            await self.stop_auto()
            if self.state["status"] == "running":
                self.state["status"] = "completed" if time.time() >= self.state["deadline"] else "stopped"
            self.state["ended_at"] = time.time()
            self.state["current_visit"] = None
            self.save()
            await self.report()
            await self.http.aclose()
            self.log("patrol_end", {"status": self.state["status"], "visits": len(self.visits)})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="本地关注列表轮巡与复盘采集")
    parser.add_argument("action", choices=["run", "resume", "stop", "status"])
    parser.add_argument("--resume-visit", help="仅接续指定的最后一站，保持原会话和截止时间")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:7862")
    parser.add_argument("--hours", type=float, default=12)
    parser.add_argument("--dwell", type=int, default=900)
    parser.add_argument("--interval", type=int, default=180)
    parser.add_argument("--max-messages", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=45)
    parser.add_argument("--first-room", type=int, default=0)
    parser.add_argument("--first-dwell", type=int, default=1800)
    parser.add_argument("--first-max-messages", type=int, default=10)
    parser.add_argument("--candidate-count", type=int, default=5, help="每轮候选目标数量（1–10），与发送配额无关")
    parser.add_argument("--virtual-fallback", action="store_true", help="仅关注列表无人开播时去虚拟分区，发现关注开播即返回")
    parser.add_argument("--max-recording-gib", type=float, default=30)
    parser.add_argument("--send", action="store_true", help="仅在用户明确授权本轮发送后使用")
    parser.add_argument("--allow-manual-send", action="store_true", help="允许用户点击手动发送，轮巡仍不自动发送")
    args = parser.parse_args()
    if args.send and args.allow_manual_send:
        parser.error("仅手动发送模式不能同时开启自动发送")
    if args.action == "stop":
        (args.run_dir / "STOP").touch(mode=0o600)
        print("已请求停止；当前发送任务会被关闭，复盘文件会保存。")
    elif args.action == "status":
        print((args.run_dir / "status.json").read_text())
    else:
        if not (0 < args.hours <= 24 and 60 <= args.dwell <= 3600 and 5 <= args.interval <= 3600
                and 1 <= args.max_messages <= 1000 and 0 <= args.warmup <= args.dwell and args.max_recording_gib > 0
                and args.first_room >= 0 and 60 <= args.first_dwell <= 3600 and 1 <= args.first_max_messages <= 1000
                and 1 <= args.candidate_count <= 10):
            parser.error("轮巡参数超出范围")
        if not re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):\d+", args.base_url):
            parser.error("仅允许本机demo地址")
        asyncio.run(Patrol(args).run())


if __name__ == "__main__":
    main()
