"""Bounded asynchronous transcription and conservative session voice tracking.

Voice similarity is not a source detector. No speaker is automatically the host;
manual per-clip attribution overrides an optional voice-role reference. Overlap
remains unknown unless the inference service explicitly reports a detection.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import suppress
import copy
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
import wave

import httpx


ROLES = {"host", "background", "singing", "unknown"}


def normalized(value):
    if not isinstance(value, list) or not 16 <= len(value) <= 2048:
        return None
    try:
        values = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(sum(v * v for v in values))
    if not math.isfinite(norm) or norm < 1e-8:
        return None
    return [v / norm for v in values]


class VoiceTracker:
    """Confirm repeated voices, then match bounded reference sets with hysteresis.

    Scores are similarities, not calibrated probabilities. One unusual chunk
    never creates a public speaker. Role labels never participate in matching.
    """
    def __init__(self, limit=16, match=.64, new=.48, margin=.08):
        self.profiles = {}
        self.limit, self.match, self.new, self.margin = limit, match, new, margin
        self.candidates = {}
        self.last_id, self.last_at = None, None
        self.next_label = 0
        self.reassignments = []
        self.waiting_for_references = False

    @staticmethod
    def references(profile):
        refs = profile.get("references", [])
        if not refs and profile.get("vector"):
            refs = [profile["vector"]]
        return refs

    @classmethod
    def score(cls, vector, profile):
        return max((sum(a * b for a, b in zip(vector, ref))
                    for ref in cls.references(profile) if len(ref) == len(vector)), default=-1.0)

    @classmethod
    def add_reference(cls, profile, vector):
        refs = list(cls.references(profile))
        if not refs or cls.score(vector, {"references": refs}) < .995:
            refs.append(vector)
        # Retain the first anchor and recent variants, never unlimited audio history.
        profile["references"] = refs[:1] + refs[-7:] if len(refs) > 8 else refs
        profile["vector"] = normalized([sum(values) for values in zip(*profile["references"])])

    def assign(self, embedding, duration, eligible=True, *, timestamp=None, evidence_id=None):
        self.reassignments = []
        vector = normalized(embedding)
        if not eligible or vector is None or duration < 1.5:
            return None, None, True
        now = float(timestamp) if timestamp is not None else time.monotonic()
        if not math.isfinite(now):
            return None, None, True
        self.candidates = {key: value for key, value in self.candidates.items()
                           if 0 <= now - value["last_seen"] <= 45}
        ranked = sorted(((self.score(vector, p), p) for p in self.profiles.values()
                         if self.references(p) and len(self.references(p)[0]) == len(vector)),
                        key=lambda pair: pair[0], reverse=True)
        score = ranked[0][0] if ranked else None
        separated = bool(ranked) and (len(ranked) == 1 or score - ranked[1][0] >= self.margin)
        continuous = (bool(ranked) and ranked[0][1]["id"] == self.last_id
                      and self.last_at is not None and 0 <= now - self.last_at <= 12
                      and score >= .58 and separated)
        if ranked and separated and (score >= self.match or continuous):
            profile = ranked[0][1]
            profile["samples"] += 1
            profile["last_seen"] = now
            # Weak continuity matches retain the ID, but do not drift its references.
            if score >= .74:
                self.add_reference(profile, vector)
            self.last_id, self.last_at = profile["id"], now
            return profile["id"], round(score, 4), score < self.match
        # Unknown stored references must be rebuilt before manufacturing duplicates.
        if self.waiting_for_references and any(not self.references(p) for p in self.profiles.values()):
            return None, round(score, 4) if score is not None else None, True
        if score is not None and score > self.new:
            return None, round(score, 4), True
        if len(self.profiles) >= self.limit:
            return None, round(score, 4) if score is not None else None, True
        evidence_id = evidence_id or uuid.uuid4().hex
        pending = sorted(((self.score(vector, p), p) for p in self.candidates.values()),
                         key=lambda pair: pair[0], reverse=True)
        if pending and pending[0][0] >= .70 and (len(pending) == 1 or pending[0][0] - pending[1][0] >= .08):
            candidate = pending[0][1]
        else:
            key = uuid.uuid4().hex
            candidate = {"id": key, "references": [], "evidence": [], "last_seen": now}
            self.candidates[key] = candidate
            while len(self.candidates) > 8:
                del self.candidates[next(iter(self.candidates))]
        if evidence_id not in {e["id"] for e in candidate["evidence"]}:
            candidate["evidence"].append({"id": evidence_id, "timestamp": now, "duration": duration})
            candidate["evidence"] = candidate["evidence"][-3:]
            self.add_reference(candidate, vector)
        candidate["last_seen"] = now
        if len(candidate["evidence"]) < 2:
            return None, round(score, 4) if score is not None else None, True
        key = "speaker_" + uuid.uuid4().hex[:12]
        label = "说话人 " + (chr(65 + self.next_label) if self.next_label < 26 else str(self.next_label + 1))
        self.next_label += 1
        profile = {"id": key, "label": label, "role": "unknown", "role_basis": "unknown",
                   "samples": len(candidate["evidence"]), "references": list(candidate["references"]),
                   "vector": candidate["vector"], "last_seen": now, "confirmed": True}
        self.profiles[key] = profile
        self.reassignments = [(e["id"], key) for e in candidate["evidence"]]
        del self.candidates[candidate["id"]]
        self.last_id, self.last_at = key, now
        return key, None, False

    def merge(self, source_id, target_id):
        if source_id == target_id or source_id not in self.profiles or target_id not in self.profiles:
            raise ValueError("请选择两个不同的已确认说话人")
        source, target = self.profiles[source_id], self.profiles[target_id]
        if source["role"] != "unknown" and target["role"] != "unknown" and source["role"] != target["role"]:
            raise ValueError("两个说话人的明确来源标签冲突，请先调整来源标签再合并")
        if target["role"] == "unknown" and source["role"] != "unknown":
            target.update(role=source["role"], role_basis=source["role_basis"])
        refs = self.references(target)[:4] + self.references(source)[:4]
        target["references"] = []
        target.pop("vector", None)
        for ref in refs:
            self.add_reference(target, ref)
        target["samples"] += source["samples"]
        target["last_seen"] = max(target.get("last_seen", 0), source.get("last_seen", 0))
        del self.profiles[source_id]
        if self.last_id == source_id:
            self.last_id = target_id
        return target

    def public(self):
        return [{**{k: p[k] for k in ("id", "label", "role", "role_basis", "samples")},
                 "confirmed": True, "reference_ready": bool(self.references(p)),
                 "reference_count": len(self.references(p)), "last_seen": p.get("last_seen")}
                for p in self.profiles.values()]

    def private_state(self):
        return {"profiles": copy.deepcopy(self.profiles), "candidates": copy.deepcopy(self.candidates),
                "last_id": self.last_id, "last_at": self.last_at, "next_label": self.next_label}

    def restore(self, state):
        self.profiles, self.candidates = {}, {}
        values = state.get("profiles", {})
        values = list(values.values()) if isinstance(values, dict) else values
        for entry in values[:self.limit] if isinstance(values, list) else []:
            if not isinstance(entry, dict) or not re.fullmatch(r"speaker_[a-zA-Z0-9]+", str(entry.get("id", ""))):
                continue
            role = entry.get("role") if entry.get("role") in ROLES else "unknown"
            profile = {"id": entry["id"], "label": str(entry.get("label") or "说话人")[:40],
                "role": role, "role_basis": "manual_voice_reference" if role != "unknown" else "unknown",
                "samples": max(1, min(1_000_000, int(entry.get("samples", 1)))),
                "last_seen": entry.get("last_seen") or 0, "references": [], "confirmed": True}
            for ref in self.references(entry)[:8]:
                vector = normalized(ref)
                if vector is not None:
                    self.add_reference(profile, vector)
            self.profiles[profile["id"]] = profile
        self.next_label = max(len(self.profiles), int(state.get("next_label", len(self.profiles))))
        self.last_id = state.get("last_id") if state.get("last_id") in self.profiles else None
        self.last_at = state.get("last_at") if isinstance(state.get("last_at"), (int, float)) else None
        # Pending candidates intentionally reset on restart; confirmed user
        # profiles survive. Suppression ends when the bounded bootstrap finishes.
        self.waiting_for_references = any(not self.references(p) for p in self.profiles.values())


def wav_info(data):
    if not isinstance(data, bytes) or len(data) > 2 * 1024 * 1024:
        raise ValueError("音频块无效或超过大小限制")
    with wave.open(BytesIO(data), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 16000:
            raise ValueError("音频必须是 16 kHz 单声道 16-bit WAV")
        duration = source.getnframes() / 16000
        if not 0 < duration <= 20:
            raise ValueError("音频块长度无效")
        return duration


def slice_wav(data, start, end):
    with wave.open(BytesIO(data), "rb") as source:
        first = max(0, min(source.getnframes(), round(start * 16000)))
        last = max(first, min(source.getnframes(), round(end * 16000)))
        source.setpos(first)
        samples = source.readframes(last - first)
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(samples)
    return output.getvalue()


class AudioPipeline:
    def __init__(self, session_id, directory, config, emit, on_segment, *, enabled=True,
                 max_segments=200, queue_size=2, on_progress=None):
        self.session_id, self.directory = session_id, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config, self.emit, self.on_segment = config, emit, on_segment
        self.on_progress = on_progress
        self.processed_until = None
        self.enabled = enabled
        self.segments = deque()
        self.max_segments = max_segments
        self.tracker = VoiceTracker()
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.task = None
        self.client = httpx.AsyncClient(timeout=35, limits=httpx.Limits(max_connections=2), trust_env=False)
        self.received = self.processed = self.dropped = self.failed = self.silent = 0
        self.status = "idle" if enabled else "disabled"
        self.error = None
        self.ready = False
        self.inference_seconds = None
        self._gap_pending = False
        self._mutation_lock = asyncio.Lock()
        self._segment_vectors = {}

    def snapshot(self):
        return {"status": self.status, "enabled": self.enabled, "model_ready": self.ready,
                "model": self.config().get("model", "Qwen3-ASR-0.6B + CAM++"),
                "speaker_model": "CAM++",
                "segments": copy.deepcopy(list(self.segments)), "speakers": self.tracker.public(),
                "received_segments": self.received, "processed_segments": self.processed,
                "dropped_segments": self.dropped, "failed_segments": self.failed,
                "silent_segments": self.silent, "pending_segments": self.queue.qsize(),
                "inference_seconds": self.inference_seconds, "error": self.error,
                "processed_until": self.processed_until,
                "pending_speakers": len(self.tracker.candidates),
                "overlap_detection": "unavailable", "automatic_source_detection": False}

    async def _status(self):
        await self.emit("audio_status", {k: v for k, v in self.snapshot().items()
                                         if k not in {"segments", "speakers"}})

    async def start(self):
        if self.enabled and self.task is None:
            self.status = "waiting_audio" if self.config().get("base_url") else "waiting_model"
            self.task = asyncio.create_task(self._worker(), name=f"audio:{self.session_id}")
        await self._status()

    async def offer(self, chunk):
        if not self.enabled or self.task is None or self.task.done():
            return
        self.received += 1
        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
            self._gap_pending = True
        # Only bounded raw chunks stay in memory; raw PCM never enters SSE/logs.
        self.queue.put_nowait(chunk)
        await self._status()

    async def _worker(self):
        while True:
            chunk = await self.queue.get()
            try:
                if self._gap_pending:
                    chunk = {**chunk, "discontinuity": True}
                    self._gap_pending = False
                config = self.config()
                if not config.get("base_url"):
                    self.status = "waiting_model"
                    self.dropped += 1
                    self._gap_pending = True
                    continue
                duration = wav_info(chunk["wav_bytes"])
                if duration < .2:
                    self.dropped += 1
                    self._gap_pending = True
                    continue
                self.status, self.error = "transcribing", None
                await self._status()
                started = time.monotonic()
                headers = {"Authorization": "Bearer " + config["api_key"]} if config.get("api_key") else {}
                result = await self.client.post(config["base_url"].rstrip("/") + "/analyze", headers=headers,
                    json={"audio_b64": base64.b64encode(chunk["wav_bytes"]).decode("ascii")})
                result.raise_for_status()
                data = result.json()
                self.inference_seconds = round(time.monotonic() - started, 3)
                await self._accept(chunk, data, duration)
                self.processed += 1
                self.ready, self.status = True, "live"
            except asyncio.CancelledError:
                raise
            except Exception:
                # HTTP error strings may contain an endpoint or sensitive request.
                self.failed += 1
                self._gap_pending = True
                self.status = "error"
                self.error = "音频识别请求失败；画面观察继续，后续音频块将自动重试。"
            finally:
                self.queue.task_done()
                await self._status()

    async def _accept(self, chunk, result, duration):
        async with self._mutation_lock:
            await self._accept_locked(chunk, result, duration)

    async def _accept_locked(self, chunk, result, duration):
        if not isinstance(result, dict):
            raise ValueError("Invalid audio response")
        parts = result.get("segments")
        if not isinstance(parts, list):
            parts = [{"start": 0, "end": duration, "text": result.get("text", ""),
                      "embedding": result.get("embedding")}]
        accepted = 0
        for part in parts[:24]:
            if not isinstance(part, dict):
                continue
            text = str(part.get("text") or "").strip()[:2000]
            start, end = float(part.get("start", 0)), float(part.get("end", duration))
            if not text or not math.isfinite(start) or not math.isfinite(end):
                continue
            start, end = max(0, start), min(duration, end)
            if end <= start:
                continue
            overlap = "detected" if part.get("overlap") == "detected" else "unknown"
            key = uuid.uuid4().hex
            eligible = part.get("speaker_eligible", True) is not False and overlap != "detected"
            voice, similarity, uncertain = self.tracker.assign(part.get("embedding"), end - start,
                eligible=eligible, timestamp=float(chunk["start"]) + end, evidence_id=key)
            profile = self.tracker.profiles.get(voice, {})
            vector = normalized(part.get("embedding")) if eligible and end - start >= 1.5 else None
            if vector is not None:
                self._segment_vectors[key] = vector
            path = self.directory / (key + ".wav")
            path.write_bytes(slice_wav(chunk["wav_bytes"], start, end))
            item = {"id": key, "start": round(float(chunk["start"]) + start, 3),
                    "end": round(float(chunk["start"]) + end, 3), "text": text,
                    "speaker_id": voice, "speaker_label": profile.get("label", "说话人待定"),
                    "speaker_similarity": similarity, "speaker_uncertain": uncertain,
                    "speaker_quality": part.get("speaker_quality", "unverified_single_speaker"),
                    "speaker_consistency": part.get("consistency_cosine"),
                    "role": profile.get("role", "unknown"), "role_basis": profile.get("role_basis", "unknown"),
                    "uncertain": uncertain or profile.get("role", "unknown") == "unknown",
                    "overlap": overlap, "is_final": True, "status": "final", "revision": 1,
                    "audio_url": f"/api/sessions/{self.session_id}/audio/{key}.wav",
                    "language": part.get("language", result.get("language", "")),
                    "inference_seconds": self.inference_seconds, "generation": chunk.get("generation"),
                    "discontinuity": bool(chunk.get("discontinuity")),
                    "timestamp_basis": chunk.get("timestamp_basis", "capture_audio_samples"),
                    "timing_uncertainty_seconds": chunk.get("timing_uncertainty_seconds"),
                    "source": "audio_transcript", "transcription_method": "final_chunk_asr",
                    "source_pts_start": (float(chunk["source_pts_start"]) + start
                                         if chunk.get("source_pts_start") is not None else None),
                    "source_pts_end": (float(chunk["source_pts_start"]) + end
                                       if chunk.get("source_pts_start") is not None else None)}
            self.segments.append(item)
            while len(self.segments) > self.max_segments:
                expired = self.segments.popleft()
                (self.directory / (expired["id"] + ".wav")).unlink(missing_ok=True)
                self._segment_vectors.pop(expired["id"], None)
            self.on_segment(copy.deepcopy(item))
            await self.emit("audio_segment", item)
            # Once repeated evidence confirms a voice, update earlier pending
            # captions in place; manual per-clip labels remain authoritative.
            for prior_id, speaker_id in self.tracker.reassignments:
                if prior_id == key:
                    continue
                prior = next((s for s in self.segments if s["id"] == prior_id), None)
                if prior is not None:
                    self._bind(prior, self.tracker.profiles[speaker_id], uncertain=False)
                    await self._updated(prior)
            accepted += 1
        if not accepted:
            self.silent += 1
        # Completion covers the decoded chunk, including VAD silence. A lack
        # of transcript must not make the visual observer wait indefinitely.
        start = float(chunk["start"])
        end = start + duration
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end:
            self.processed_until = max(self.processed_until or 0, end)
            if self.on_progress:
                self.on_progress({"start": start, "end": end, "status": "complete",
                    "generation": chunk.get("generation"),
                    "discontinuity": bool(chunk.get("discontinuity")),
                    "transcribed_segments": accepted})
        await self.emit("audio_speakers", {"speakers": self.tracker.public()})
        self._persist()

    async def label_speaker(self, speaker_id, role, label=None):
        async with self._mutation_lock:
            return await self._label_speaker_locked(speaker_id, role, label)

    async def _label_speaker_locked(self, speaker_id, role, label=None):
        if role not in ROLES or speaker_id not in self.tracker.profiles:
            raise ValueError("说话人不存在或来源标签无效")
        profile = self.tracker.profiles[speaker_id]
        profile.update(role=role, role_basis="manual_voice_reference" if role != "unknown" else "unknown")
        if label is not None and label.strip():
            profile["label"] = label.strip()[:40]
        for segment in self.segments:
            if segment["speaker_id"] == speaker_id:
                segment["speaker_label"] = profile["label"]
                if segment["role_basis"] != "manual_segment":
                    segment.update(role=role, role_basis=profile["role_basis"],
                                   uncertain=segment["speaker_uncertain"] or role == "unknown")
                await self._updated(segment)
        await self.emit("audio_speakers", {"speakers": self.tracker.public()})
        self._persist()
        return self.snapshot()

    async def label_segment(self, segment_id, role):
        async with self._mutation_lock:
            return await self._label_segment_locked(segment_id, role)

    async def _label_segment_locked(self, segment_id, role):
        if role not in ROLES:
            raise ValueError("来源标签无效")
        segment = next((s for s in self.segments if s["id"] == segment_id), None)
        if segment is None:
            raise KeyError("音频片段已超出保留窗口")
        segment.update(role=role, role_basis="manual_segment", uncertain=role == "unknown")
        await self._updated(segment)
        self._persist()
        return copy.deepcopy(segment)

    @staticmethod
    def _bind(segment, profile, *, uncertain=None):
        segment.update(speaker_id=profile["id"], speaker_label=profile["label"])
        if uncertain is not None:
            segment["speaker_uncertain"] = uncertain
        if segment.get("role_basis") != "manual_segment":
            segment.update(role=profile["role"], role_basis=profile["role_basis"],
                           uncertain=bool(segment.get("speaker_uncertain")) or profile["role"] == "unknown")

    async def merge_speakers(self, source_id, target_id):
        """Merge an explicitly selected group; reject conflicting known roles."""
        async with self._mutation_lock:
            target = self.tracker.merge(source_id, target_id)
            for segment in self.segments:
                if segment.get("speaker_id") in {source_id, target_id}:
                    self._bind(segment, target)
                    await self._updated(segment)
            await self.emit("audio_speakers", {"speakers": self.tracker.public()})
            self._persist()
            return self.snapshot()

    def restore_private_state(self, public_state_fallback=True):
        """Restore this session only, including user labels before live capture.

        Legacy public state retains metadata but has no usable voice references;
        rebuild_missing_references can recover them from retained audio clips.
        """
        private_path = self.directory / "state.private.json"
        state, tracking = None, None
        try:
            if private_path.is_file() and private_path.stat().st_size <= 8 * 1024 * 1024:
                private = json.loads(private_path.read_text())
                if private.get("version") == 1 and private.get("session_id") == self.session_id:
                    state, tracking = private.get("snapshot"), private.get("tracker", {})
                    cache = private.get("segment_vectors", {})
                    self._segment_vectors = {str(k): v for k, raw in list(cache.items())[-self.max_segments:]
                                             if (v := normalized(raw)) is not None}
        except (OSError, ValueError, TypeError, AttributeError):
            state = None
        if state is None and public_state_fallback:
            try:
                path = self.directory / "state.json"
                if path.stat().st_size <= 4 * 1024 * 1024:
                    state = json.loads(path.read_text())
                    tracking = {"profiles": state.get("speakers", [])}
            except (OSError, ValueError, TypeError, AttributeError):
                state = None
        if not isinstance(state, dict):
            return {"restored": False, "segments": 0, "speakers": 0}
        self.tracker.restore(tracking or {})
        restored = []
        for value in state.get("segments", [])[-self.max_segments:]:
            if not isinstance(value, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(value.get("id", ""))):
                continue
            try:
                start, end = float(value["start"]), float(value["end"])
            except (ValueError, TypeError, KeyError):
                continue
            if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                continue
            item = copy.deepcopy(value)
            item["audio_url"] = f"/api/sessions/{self.session_id}/audio/{item['id']}.wav"
            restored.append(item)
        self.segments = deque(restored)
        ids = {item["id"] for item in restored}
        self._segment_vectors = {key: vector for key, vector in self._segment_vectors.items() if key in ids}
        for attribute, key in (("received", "received_segments"), ("processed", "processed_segments"),
                ("dropped", "dropped_segments"), ("failed", "failed_segments"), ("silent", "silent_segments")):
            setattr(self, attribute, max(0, int(state.get(key, 0))))
        for item in restored:
            self.on_segment(copy.deepcopy(item))
        self._persist()
        return {"restored": True, "segments": len(restored), "speakers": len(self.tracker.profiles)}

    async def rebuild_missing_references(self):
        """Bootstrap legacy groups from up to three retained clips; never relabel text."""
        self.tracker.waiting_for_references = True
        try:
            return await self._rebuild_missing_references()
        finally:
            self.tracker.waiting_for_references = False
            async with self._mutation_lock:
                await self.emit("audio_speakers", {"speakers": self.tracker.public()})
                self._persist()

    async def _rebuild_missing_references(self):
        attempted = recovered = 0
        deadline = time.monotonic() + 12
        for speaker_id in list(self.tracker.profiles):
            if time.monotonic() >= deadline:
                break
            profile = self.tracker.profiles.get(speaker_id)
            if not profile or self.tracker.references(profile):
                continue
            clips = [s for s in reversed(self.segments) if s.get("speaker_id") == speaker_id
                     and s.get("end", 0) - s.get("start", 0) >= 1.5
                     and s.get("speaker_quality") not in {"unstable_voice", "merged_regions", "too_short"}]
            for segment in clips[:3]:
                if time.monotonic() >= deadline:
                    break
                vector = self._segment_vectors.get(segment["id"])
                if vector is None:
                    path = self.directory / (segment["id"] + ".wav")
                    config = self.config()
                    if not path.is_file() or not config.get("base_url"):
                        continue
                    try:
                        raw = path.read_bytes()
                        duration = wav_info(raw)
                        if not .2 <= duration <= 15:
                            continue
                        headers = {"Authorization": "Bearer " + config["api_key"]} if config.get("api_key") else {}
                        response = None
                        for attempt in range(2):
                            attempted += 1
                            response = await self.client.post(config["base_url"].rstrip("/") + "/analyze", headers=headers,
                                json={"audio_b64": base64.b64encode(raw).decode("ascii")}, timeout=5)
                            if response.status_code != 429:
                                break
                            await asyncio.sleep(.15 * (attempt + 1))
                        response.raise_for_status()
                        parts = response.json().get("segments", [])
                        possible = [p for p in parts if isinstance(p, dict) and p.get("speaker_eligible") is not False
                                    and p.get("overlap") != "detected" and normalized(p.get("embedding")) is not None]
                        if possible:
                            selected = max(possible, key=lambda p: float(p.get("end", 0)) - float(p.get("start", 0)))
                            vector = normalized(selected["embedding"])
                    except (httpx.HTTPError, ValueError, OSError, TypeError, KeyError):
                        continue
                if vector is not None:
                    async with self._mutation_lock:
                        profile = self.tracker.profiles.get(speaker_id)
                        if profile is None:
                            break
                        self.tracker.add_reference(profile, vector)
                        if any(s["id"] == segment["id"] for s in self.segments):
                            self._segment_vectors[segment["id"]] = vector
            profile = self.tracker.profiles.get(speaker_id)
            if profile and self.tracker.references(profile):
                recovered += 1
        return {"attempted_requests": attempted, "recovered_speakers": recovered,
                "reference_missing": sum(not self.tracker.references(p) for p in self.tracker.profiles.values())}

    async def _updated(self, segment):
        segment["revision"] += 1
        self.on_segment(copy.deepcopy(segment))
        await self.emit("audio_segment", segment)

    def _persist(self):
        retained = {item["id"] for item in self.segments}
        self._segment_vectors = {key: value for key, value in self._segment_vectors.items() if key in retained}
        snapshot = self.snapshot()
        destination = self.directory / "state.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        temporary.replace(destination)
        destination = self.directory / "state.private.json"
        temporary = destination.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "session_id": self.session_id, "snapshot": snapshot,
                       "tracker": self.tracker.private_state(), "segment_vectors": self._segment_vectors},
                      handle, ensure_ascii=False)
        temporary.replace(destination)

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
        self.status = "stopped" if self.enabled else "disabled"
        await self.client.aclose()
        self._persist()
        await self._status()
