"""Stopping a patrol visit must include the final decoded speech exactly once."""
import asyncio
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from clipper.config import Settings
from clipper.server import create_app


@pytest.fixture
def stopped_live(tmp_path, monkeypatch):
    calls = []

    class Capture:
        def __init__(self, settings, sid, directory, event, segment, state):
            self.event, self.segment, self.state = event, segment, state
            self.end = asyncio.Event()
            self.did_stop = False

        async def run(self, room):
            await self.segment(0, 60, tmp_path / "recording.ts", {})
            await self.state({"status": "live"})
            await self.end.wait()

        async def stop(self):
            if not self.did_stop:
                self.did_stop = True
                # This is the transcript which used to be lost on room switches.
                await self.event("audio_segment", 54, 59, "最后这句话也是笑点。",
                                 {}, "tail-speech")
                self.end.set()

    class Detector:
        def __init__(self, settings, store):
            self.store = store

        async def analyze(self, sid, **kwargs):
            events = self.store.events(sid)
            calls.append({"texts": [e["text"] for e in events], **kwargs})
            if self.store.session(sid).get("streamer") == "timeout":
                await asyncio.Event().wait()
            self.store.update_session(sid, analysis_status="complete")
            return [self.store.add_anchor(
                sid, title="最后这句话", method="model", score=90,
                start=45, end=60, timestamp=55, status="ready",
                evidence_ids=[e["id"] for e in events],
            )]

    async def render(renderer, jid):
        renderer.store.update_job(jid, status="complete")

    monkeypatch.setattr("clipper.ingest.LiveCapture", Capture)
    monkeypatch.setattr("clipper.detector.Detector", Detector)
    monkeypatch.setattr("clipper.render.Renderer.run", render)
    monkeypatch.setattr("clipper.server.FINAL_ANALYSIS_TIMEOUT", .1)
    app = create_app(Settings(data_dir=tmp_path, import_roots=(tmp_path,)))
    with TestClient(app) as client:
        yield client, app.state.store, calls


def begin(client, name="已核验主播"):
    result = client.post("/api/sessions/live", json={
        "room": "47867", "streamer": name, "streamer_uid": 123,
        "capture_owner": "patrol-test-run", "auto_render": True,
    })
    assert result.status_code == 200
    session = result.json()["session"]
    for _ in range(50):
        current = client.get(f"/api/sessions/{session['id']}").json()["session"]
        if current["duration"] == 60:
            break
        time.sleep(.01)
    return session["id"]


def test_stop_drains_before_final_detection_and_retry_does_not_duplicate(stopped_live):
    client, store, calls = stopped_live
    sid = begin(client)
    first = client.post(f"/api/sessions/{sid}/stop")
    assert first.status_code == 200
    assert first.json()["stop_phase"] == "complete"
    assert first.json()["final_analysis_status"] == "complete"
    assert calls == [{"texts": ["最后这句话也是笑点。"], "start": 0, "end": 60}]
    assert len(store.jobs(sid)) == 1
    assert client.post(f"/api/sessions/{sid}/stop").status_code == 200
    assert len(calls) == 1 and len(store.jobs(sid)) == 1
    assert store.session(sid)["capture_owner"] == "patrol-test-run"
    assert store.session(sid)["streamer_uid"] == 123


def test_stop_finishes_on_model_timeout_and_keeps_saved_tail(stopped_live):
    client, store, calls = stopped_live
    sid = begin(client, "timeout")
    response = client.post(f"/api/sessions/{sid}/stop")
    assert response.status_code == 200
    assert response.json()["stop_phase"] == "complete"
    assert response.json()["final_analysis_status"] == "timeout"
    assert store.events(sid)[0]["text"] == "最后这句话也是笑点。"
    assert store.jobs(sid) == []


def test_final_live_analysis_sees_the_whole_long_story_context(stopped_live):
    client, store, calls = stopped_live
    sid = begin(client)
    store.add_segment(sid, 60, 1200, "remaining-recording.ts", {})
    store.update_session(sid, duration=1200)
    store.add_event(sid, "transcript", 350, 370, "这件事情的来龙去脉要从这里说起。")
    store.add_event(sid, "transcript", 1100, 1120, "后来终于弄明白了，这就是最后的结果。")
    response = client.post(f"/api/sessions/{sid}/stop")
    assert response.status_code == 200
    assert response.json()["stop_phase"] == "complete"
    assert len(calls) == 1
    # The server must pass the earlier setup to the detector, not just a
    # three-minute excerpt containing the ending.
    assert calls[0]["start"] <= 350
    assert calls[0]["end"] == 1200
    assert calls[0]["end"] - calls[0]["start"] == 900
