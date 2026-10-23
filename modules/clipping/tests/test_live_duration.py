"""Live selection waits for context and advances in meaningful media increments."""
import asyncio
import time

from fastapi.testclient import TestClient

from clipper.config import Settings
from clipper.server import create_app


def wait_until(condition, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(.005)
    assert condition()


def test_periodic_detection_waits_for_two_minutes_then_45_seconds_of_new_media(tmp_path, monkeypatch):
    calls = []

    class Capture:
        def __init__(self, settings, sid, directory, event, segment, state):
            self.segment, self.state = segment, state
            self.finished = asyncio.Event()

        async def run(self, room):
            await self.segment(0, 90, tmp_path / "first.ts", {})
            await self.state({"status": "live"})
            await self.finished.wait()

        async def stop(self):
            self.finished.set()

    class Detector:
        def __init__(self, settings, store):
            self.store = store

        async def analyze(self, sid, **kwargs):
            calls.append(kwargs)
            self.store.update_session(sid, analysis_status="complete")
            return []

    monkeypatch.setattr("clipper.ingest.LiveCapture", Capture)
    monkeypatch.setattr("clipper.detector.Detector", Detector)
    settings = Settings(data_dir=tmp_path, import_roots=(tmp_path,), analyze_interval=.01)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/api/sessions/live", json={"room": "47867"})
        assert response.status_code == 200
        sid = response.json()["session"]["id"]
        store = app.state.store
        wait_until(lambda: store.session(sid)["duration"] == 90)
        time.sleep(.04)
        assert calls == []

        store.add_segment(sid, 90, 120, tmp_path / "second.ts", {})
        store.update_session(sid, duration=120)
        wait_until(lambda: len(calls) == 1)
        assert calls[0]["end"] == 120

        store.add_segment(sid, 120, 164, tmp_path / "third.ts", {})
        store.update_session(sid, duration=164)
        time.sleep(.04)
        assert len(calls) == 1

        store.add_segment(sid, 164, 165, tmp_path / "fourth.ts", {})
        store.update_session(sid, duration=165)
        wait_until(lambda: len(calls) == 2)
        assert calls[1]["end"] == 165
