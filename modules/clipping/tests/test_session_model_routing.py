import asyncio
import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
import sys
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from clipper.config import Settings

EXTRA_PROFILE_UID = 13579
EXTRA_PROFILE_ROOM = 24680
from clipper.live_publishing import LivePublicationController, atomic_json
from clipper.server import create_app
from clipper.subtitle_review import review_subtitles, MODEL, ALIGNER


def configured(tmp_path):
    settings = Settings(data_dir=tmp_path)
    private = tmp_path / "model-profiles"
    private.mkdir(exist_ok=True)
    endpoints = {}
    for kind in ["model", "audio"]:
        key = private / (kind + ".key")
        key.write_text("independent-test-key")
        endpoint = private / (kind + ".json")
        endpoint.write_text(json.dumps({"base_url": f"https://extra-{kind}.example",
            "model": "independent-test-model", "api_key_file": str(key)}))
        endpoints[kind + "_endpoint"] = str(endpoint)
    (private / "extra-gpu.json").write_text(json.dumps({"version": 1, "profile": "extra-gpu", "owner_uid": EXTRA_PROFILE_UID, "room_id": EXTRA_PROFILE_ROOM, **endpoints}))
    return settings


def test_profile_is_isolated_from_global_environment_and_never_mutates_base(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    for prefix in ["MODEL", "AUDIO"]:
        monkeypatch.setenv(prefix + "_BASE_URL", "https://main.example")
        monkeypatch.setenv(prefix + "_API_KEY", "main-test-key")
    extra = settings.for_session({"model_profile": "extra-gpu"})
    assert extra.model_config()["base_url"] == "https://extra-model.example"
    assert extra.audio_config()["base_url"] == "https://extra-audio.example"
    assert extra.audio_config()["api_key"] == "independent-test-key"
    assert settings.for_session({}) is settings
    assert settings.model_config()["base_url"] == "https://main.example"
    assert settings.audio_config()["api_key"] == "main-test-key"
    assert settings.model_profile == "default"


def test_missing_or_unknown_extra_profile_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://main.example")
    settings = Settings(data_dir=tmp_path)
    with pytest.raises(ValueError, match="session_model_profile_unavailable"):
        settings.for_session({"model_profile": "extra-gpu"})
    with pytest.raises(ValueError, match="session_model_profile_unknown"):
        settings.for_session({"model_profile": "../../secret"})


def test_two_capture_lanes_route_asr_detection_and_render_without_blocking(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    monkeypatch.setenv("AUDIO_BASE_URL", "https://main-audio.example")
    monkeypatch.setenv("MODEL_BASE_URL", "https://main-model.example")
    captured, detected, rendered = {}, {}, {}
    release_main = Event()
    main_started = Event()

    class Capture:
        def __init__(self, config, sid, directory, event, segment, state):
            self.sid, self.segment, self.state = sid, segment, state
            self.done = asyncio.Event()
            captured[sid] = config.audio_config()["base_url"]
        async def run(self, room):
            await self.segment(0, 120, tmp_path / "recording.ts", {})
            await self.state({"status": "live"})
            await self.done.wait()
        async def stop(self): self.done.set()

    class Detector:
        def __init__(self, config, store): self.config, self.store = config, store
        async def analyze(self, sid, **kwargs):
            detected[sid] = self.config.model_config()["base_url"]
            if self.config.model_profile == "default":
                main_started.set()
                while not release_main.is_set(): await asyncio.sleep(.01)
            self.store.update_session(sid, analysis_status="complete")
            return []

    async def render(renderer, jid):
        rendered[jid] = renderer.settings.audio_config()["base_url"]
        renderer.store.update_job(jid, status="failed", error="fixture completed")
    monkeypatch.setattr("clipper.ingest.LiveCapture", Capture)
    monkeypatch.setattr("clipper.detector.Detector", Detector)
    monkeypatch.setattr("clipper.render.Renderer.run", render)
    app = create_app(settings)
    with TestClient(app) as client:
        try:
            main = client.post("/api/sessions/live", json={"room": "123", "capture_owner": "main"}).json()["session"]
            payload = {"room": str(EXTRA_PROFILE_ROOM), "streamer_uid": EXTRA_PROFILE_UID,
                       "streamer": "测试主播", "model_profile": "extra-gpu", "capture_owner": "extra"}
            response = client.post("/api/sessions/live", json=payload)
            assert response.status_code == 200, response.text
            extra = response.json()["session"]
            assert extra["capture_lane"] == "extra" and main["capture_lane"] == "main"
            repeated = client.post("/api/sessions/live", json=payload)
            assert repeated.json()["session"]["id"] == extra["id"] and repeated.json()["reused"]
            assert client.post("/api/sessions/live", json={"room": "124"}).status_code == 409
            assert client.post("/api/sessions/live", json={**payload, "capture_owner": "another"}).status_code == 409
            assert len(app.state.store.sessions()) == 2
            assert captured[main["id"]] == "https://main-audio.example"
            assert captured[extra["id"]] == "https://extra-audio.example"
            assert client.post(f"/api/sessions/{main['id']}/analyze", json={}).status_code == 202
            assert main_started.wait(2)
            assert client.post(f"/api/sessions/{extra['id']}/analyze", json={}).status_code == 202
            deadline = time.monotonic() + 2
            while extra["id"] not in detected and time.monotonic() < deadline: time.sleep(.01)
            assert detected[extra["id"]] == "https://extra-model.example"
            assert detected[main["id"]] == "https://main-model.example"
            for session in [main, extra]:
                a = app.state.store.add_anchor(session["id"], start=0, end=90, timestamp=45, title="测试")
                result = client.post(f"/api/anchors/{a['id']}/render", json={})
                jid = result.json()["id"]
                deadline = time.monotonic() + 2
                while jid not in rendered and time.monotonic() < deadline: time.sleep(.01)
                assert rendered[jid] == captured[session["id"]]
            state_text = client.get("/api/state").text
            assert "independent-test-key" not in state_text and "extra-model.example" not in state_text
        finally:
            release_main.set()


def test_enabled_policy_can_create_live_session_with_string_room_without_orphan(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    atomic_json(tmp_path / "live-publication-policy.json", {"enabled": True, "eligible_after": 1,
                "account_uid": 42})
    started = Event()
    class Capture:
        def __init__(self, config, sid, directory, event, segment, state): self.done = asyncio.Event()
        async def run(self, room): started.set(); await self.done.wait()
        async def stop(self): self.done.set()
    monkeypatch.setattr("clipper.ingest.LiveCapture", Capture)
    app = create_app(settings)
    with TestClient(app) as client:
        bad = client.post("/api/sessions/live", json={"room": str(EXTRA_PROFILE_ROOM),
              "streamer_uid": EXTRA_PROFILE_UID, "capture_owner": "extra", "model_profile": "extra-gpu"})
        assert bad.status_code == 422 and not app.state.store.sessions()
        result = client.post("/api/sessions/live", json={"room": "47867", "streamer": "星瞳", "streamer_uid": 123})
        assert result.status_code == 200, result.text
        assert started.wait(2)
        assert result.json()["session"]["auto_publish"]
        assert result.json()["session"]["publish_profile"]["source"] == "https://live.bilibili.com/47867"
        assert len(app.state.store.sessions()) == 1


def test_same_room_cannot_be_captured_by_both_lanes(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    class Capture:
        def __init__(self, *args): self.done = asyncio.Event()
        async def run(self, room): await self.done.wait()
        async def stop(self): self.done.set()
    monkeypatch.setattr("clipper.ingest.LiveCapture", Capture)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.post("/api/sessions/live", json={"room": str(EXTRA_PROFILE_ROOM)}).status_code == 200
        extra = client.post("/api/sessions/live", json={"room": str(EXTRA_PROFILE_ROOM),
                "streamer_uid": EXTRA_PROFILE_UID, "capture_owner": "extra", "model_profile": "extra-gpu"})
        assert extra.status_code == 409
        assert len(app.state.store.sessions()) == 1


def test_extra_profile_requires_owner_and_respects_configured_room_before_capture(tmp_path):
    app = create_app(configured(tmp_path))
    with TestClient(app) as client:
        payload = {"room": str(EXTRA_PROFILE_ROOM), "model_profile": "extra-gpu"}
        assert client.post("/api/sessions/live", json=payload).status_code == 422
        assert client.post("/api/sessions/live", json={**payload, "room": "1", "capture_owner": "extra"}).status_code == 422
        assert client.post("/api/sessions/live", json={**payload, "streamer_uid": 1, "capture_owner": "extra"}).status_code == 422
        assert not app.state.store.sessions()


@pytest.mark.asyncio
async def test_named_extra_profile_does_not_block_patrol_or_duplicate_its_room(tmp_path, monkeypatch):
    from clipper.patrol import FollowingPatrol
    monkeypatch.setattr("clipper.patrol.time.time", lambda: 1050)
    patrol = FollowingPatrol(Settings(data_dir=tmp_path), tmp_path / "patrol")
    patrol.capacity = lambda snapshot: None
    patrol.refreshed_at = 1050
    patrol.rooms = [{"room_id": 7, "name": "independent"}, {"room_id": 8, "name": "other"}]
    snapshot = {"sessions": [{"id": "extra", "mode": "live", "status": "live",
                              "room": {"room_id": 7}, "model_profile": "another-gpu", "capture_lane": "extra"}],
                "jobs": [], "anchors": []}
    started = []
    async def request(*args, **kwargs): return snapshot
    async def start(room, snapshot): started.append(room["room_id"])
    patrol.request, patrol.start_visit = request, start
    try:
        await patrol.tick()
        assert started == [8]
    finally:
        await patrol.client.aclose()


@pytest.mark.asyncio
async def test_whole_clip_strong_asr_uses_independent_audio_endpoint(tmp_path):
    settings = configured(tmp_path)
    folder = tmp_path / "export"
    folder.mkdir()
    async def extract(args, timeout):
        with wave.open(str(args[-1]), "wb") as out:
            out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            out.writeframes(b"\x01\x01" * 4 * 16000)
    called = []
    def handler(request):
        called.append(str(request.url))
        assert request.headers["authorization"] == "Bearer independent-test-key"
        return httpx.Response(200, json={"text": "你好", "words": [{"start": .2, "end": 1., "text": "你好"}],
            "model": MODEL, "aligner": ALIGNER, "timing_basis": "forced_alignment", "duration": 4.})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events, report = await review_subtitles(settings, folder, [], {"start": 0., "end": 4.}, 1920, 1080,
            offset=0, has_audio=True, source={"model_profile": "extra-gpu", "streamer": "测试主播"},
            enabled=True, command=extract, client=client)
    assert report["applied"] and report["model_profile"] == "extra-gpu"
    assert called == ["https://extra-audio.example/transcribe_clip"]


@pytest.mark.asyncio
async def test_semantic_review_uses_independent_model_endpoint_and_lock(tmp_path, monkeypatch):
    settings = configured(tmp_path)
    folder = tmp_path / "export"
    folder.mkdir()
    (folder / "clip.mp4").write_bytes(b"fixture")
    with wave.open(str(folder / "review.wav"), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed")); out.writeframes(b"\x01\x01" * 60 * 16000)
    (folder / "subtitle-review.json").write_text(json.dumps({"reviewed_subtitles": [{"start": 0, "end": 3, "text": "唱完这首歌"}]}))
    async def probe(*args): return {"width": 1920, "height": 1080, "audio": True, "duration": 60}
    async def command(args): Path(args[-1]).write_bytes(b"image fixture")
    monkeypatch.setattr("clipper.live_publishing.probe", probe)
    monkeypatch.setattr("clipper.live_publishing.command", command)
    monkeypatch.setattr("clipper.live_publishing.check_subtitle_integrity", lambda *args: {})
    class Publisher:
        async def categories(self): return [{"id": 31}]
        async def validate(self, metadata): return metadata
    called = []
    def handler(request):
        called.append(str(request.url))
        assert request.headers["authorization"] == "Bearer independent-test-key"
        value = {"ready": True, "caption_ids": [0], "evidence_quote": "唱完这首歌", "title_body": "唱完这首歌",
                 "cover_text": "唱完这首歌", "tags": ["唱歌"], "tag_evidence": [{"tag": "唱歌", "caption_ids": [0]}], "tid": 31}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr("clipper.live_publishing.httpx.AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handler)))
    controller = LivePublicationController(settings, None, Publisher())
    await controller.model_slot.acquire()  # Busy main GPU must not block extra review.
    session = {"model_profile": "extra-gpu", "streamer": "测试主播", "streamer_uid": EXTRA_PROFILE_UID,
               "room": {"room_id": EXTRA_PROFILE_ROOM}, "created_at": 1700000000}
    try:
        metadata, report = await asyncio.wait_for(controller.review({"id": "j", "anchor": {"start": 0}}, session,
                                                       folder_override=folder), timeout=2)
    finally:
        controller.model_slot.release()
    assert called == ["https://extra-model.example/v1/chat/completions"]
    assert report["model_profile"] == "extra-gpu" and metadata["tid"] == 31


@pytest.mark.asyncio
async def test_background_semantic_jobs_from_independent_profiles_can_advance_together(tmp_path):
    settings = Settings(data_dir=tmp_path)
    atomic_json(tmp_path / "live-publication-policy.json", {"enabled": True, "eligible_after": 1})
    sessions = {name: {"id": name, "mode": "live", "status": "live", "auto_publish": True,
                       "model_profile": name, "streamer_uid": 42, "streamer": "主播",
                       "room": {"room_id": 123}} for name in ["default", "extra-gpu"]}
    jobs = [{"id": name, "session_id": name, "status": "complete", "created_at": 2}
            for name in sessions]
    store = SimpleNamespace(sessions=lambda: list(sessions.values()), session=sessions.get,
                            jobs=lambda: jobs, publication_for_job=lambda jid: None)
    controller = LivePublicationController(settings, store, None)
    extra_ready = asyncio.Event()
    done = []
    async def process(jid):
        if jid == "default": await asyncio.wait_for(extra_ready.wait(), 1)
        else: extra_ready.set()
        done.append(jid)
    controller.process = process
    await controller.tick()
    assert done == ["extra-gpu", "default"]


@pytest.mark.parametrize("recovery_status", ["complete", "failed"])
def test_tail_recovery_uses_profile_and_failure_blocks_final_detection(tmp_path, monkeypatch, recovery_status):
    settings = configured(tmp_path)
    observed = []
    async def recover(config, store, sid):
        observed.append(("recover", config.audio_config()["base_url"]))
        store.update_session(sid, tail_recovery={"status": recovery_status})
    monkeypatch.setitem(sys.modules, "clipper.live_tail_recovery", SimpleNamespace(recover_live_tail=recover))
    class Detector:
        def __init__(self, config, store): self.config, self.store = config, store
        async def analyze(self, sid, **kwargs):
            observed.append(("detect", self.config.model_config()["base_url"]))
            self.store.update_session(sid, analysis_status="complete")
            return []
    monkeypatch.setattr("clipper.detector.Detector", Detector)
    app = create_app(settings)
    session = app.state.store.create_session(mode="live", status="stopped", model_profile="extra-gpu",
        duration=120, streamer="测试主播", streamer_uid=EXTRA_PROFILE_UID, room={"room_id": EXTRA_PROFILE_ROOM})
    with TestClient(app) as client:
        response = client.post(f"/api/sessions/{session['id']}/analyze", json={"recover_tail": True})
        assert response.status_code == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = app.state.store.session(session["id"])
            if current.get("final_analysis_status") in {"complete", "failed"}: break
            time.sleep(.01)
        assert observed[0] == ("recover", "https://extra-audio.example")
        assert current["final_analysis_status"] == recovery_status
        assert len(observed) == (2 if recovery_status == "complete" else 1)
        if recovery_status == "complete": assert observed[1] == ("detect", "https://extra-model.example")
