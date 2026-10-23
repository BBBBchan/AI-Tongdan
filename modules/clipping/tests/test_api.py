import json
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
import pytest

from clipper.config import Settings
from clipper.server import create_app


@pytest.fixture
def workspace(tmp_path):
    settings=Settings(data_dir=tmp_path,import_roots=(tmp_path,))
    app=create_app(settings)
    store=app.state.store
    source=tmp_path/"stub.mp4"
    source.write_bytes(b"not read by metadata tests")
    s=store.create_session(title="API test",duration=90,mode="file",media_path=str(source))
    with TestClient(app) as client:
        yield client,store,s


def test_manual_anchor_edit_real_event_selection_and_invalid_ranges(workspace):
    c,store,s=workspace
    r=c.post("/api/anchors",json={"session_id":s["id"],"timestamp":30,"start":20,"end":50,"title":"反转"})
    assert r.status_code==200,r.text
    a=r.json()
    assert a["origin"]=="manual"
    event=store.add_event(s["id"],"danmaku",32,32,"真弹幕")
    outside=store.add_event(s["id"],"danmaku",65,65,"段外弹幕")
    assert c.patch("/api/anchors/"+a["id"],json={"focus_event_ids":[event]}).status_code==200
    assert c.patch("/api/anchors/"+a["id"],json={"focus_event_ids":[outside]}).status_code==422
    assert c.patch("/api/anchors/"+a["id"],json={"focus_event_ids":["invented"]}).status_code==422
    assert c.patch("/api/anchors/"+a["id"],json={"start":51,"end":50}).status_code==422
    assert c.patch("/api/anchors/"+a["id"],json={"end":91}).status_code==422


def test_import_event_time_validation_is_atomic(workspace):
    c,store,s=workspace
    r=c.post(f"/api/sessions/{s['id']}/events",json={"events":[
        {"kind":"transcript","start":1,"end":3,"text":"valid"},
        {"kind":"transcript","start":89,"end":92,"text":"invalid"}]})
    assert r.status_code==422
    assert store.events(s["id"])==[]


def test_editing_automatic_candidate_preserves_user_choices_against_reanalysis(workspace):
    c, store, s = workspace
    anchor = store.add_anchor(s["id"], start=10, end=35, timestamp=20,
                              title="自动候选", status="candidate", method="model")
    assert not anchor.get("editorial_locked")
    invalid = c.patch(f"/api/anchors/{anchor['id']}", json={"end": 91})
    assert invalid.status_code == 422
    assert not store.anchor(anchor["id"]).get("editorial_locked")
    edited = c.patch(f"/api/anchors/{anchor['id']}", json={"start": 5, "end": 65,
                                                               "title": "手工保留完整经过"})
    assert edited.status_code == 200
    assert edited.json()["editorial_locked"] is True
    saved = store.anchor(anchor["id"])
    assert saved["start"] == 5 and saved["end"] == 65
    assert saved["title"] == "手工保留完整经过"


def test_cross_origin_mutations_and_path_reads_are_blocked(workspace):
    c,store,s=workspace
    assert c.post("/api/sessions/live",json={"room":"1"},headers={"Origin":"https://elsewhere.example"}).status_code==403
    assert c.post("/api/sessions/import",json={"path":"/etc/passwd"}).status_code==422
    assert c.get(f"/api/sessions/{s['id']}/hls/credentials.json").status_code==404
    assert c.get("/api/exports/no-job/metadata.json").status_code==404


def test_unknown_source_and_model_configuration_are_explicit(workspace):
    c,store,s=workspace
    assert c.get("/api/sessions/missing").status_code==404
    r=c.get("/api/health")
    assert r.json()["platform"]=="bilibili"
    assert "api_key" not in r.text


def test_released_recording_still_exposes_history_but_refuses_source_work(workspace):
    client, store, session = workspace
    store.update_session(session["id"], source_retention={"status": "removed"})
    anchor = store.add_anchor(session["id"], start=10, end=70, title="保留的锚点")
    assert client.get(f"/api/sessions/{session['id']}").status_code == 200
    assert client.post(f"/api/sessions/{session['id']}/analyze", json={}).status_code == 409
    response = client.post(f"/api/anchors/{anchor['id']}/render", json={})
    assert response.status_code == 422
    assert "原录播缓冲已清理" in response.json()["detail"]
    assert client.get("/api/health").json()["recording_retention_version"] == 1


def test_description_preferences_survive_session_profile_updates(workspace):
    c, store, s = workspace
    profile = {"tid": 27, "creator_name": "阿梓", "follow_creator": False,
               "description_style": "source_links"}
    response = c.patch(f"/api/sessions/{s['id']}", json={"publish_profile": profile})
    assert response.status_code == 200
    saved = store.session(s["id"])["publish_profile"]
    assert saved["follow_creator"] is False
    assert saved["description_style"] == "source_links"
    profile.pop("follow_creator")
    profile.pop("description_style")
    response = c.patch(f"/api/sessions/{s['id']}", json={"publish_profile": profile})
    assert response.status_code == 200
    saved = response.json()["publish_profile"]
    assert "follow_creator" not in saved
    assert "description_style" not in saved
    profile["description_style"] = "unknown"
    assert c.patch(f"/api/sessions/{s['id']}", json={"publish_profile": profile}).status_code == 422


@pytest.mark.parametrize("payload", [{}, {"resolution": 720}, {"resolution": 1080}])
def test_manual_exports_use_1080p_even_from_older_clients(workspace, monkeypatch, payload):
    client, store, session = workspace
    anchor = store.add_anchor(session["id"], start=10, end=60, timestamp=25,
                              title="手动导出清晰度验证")
    rendered, finished = [], Event()

    async def fake_render(renderer, job_id):
        rendered.append(renderer.store.job(job_id))
        renderer.store.update_job(job_id, status="complete", progress=100)
        finished.set()

    monkeypatch.setattr("clipper.server.Renderer.run", fake_render)
    response = client.post(f"/api/anchors/{anchor['id']}/render", json=payload)
    assert response.status_code == 202, response.text
    assert response.json()["options"]["resolution"] == 1080
    assert finished.wait(timeout=3), "The queued render did not run."
    assert len(rendered) == 1
    assert rendered[0]["options"]["resolution"] == 1080
    assert client.get(f"/api/jobs/{rendered[0]['id']}").json()["options"]["resolution"] == 1080


def test_automatic_model_candidates_queue_1080p_exports(workspace, monkeypatch):
    client, store, session = workspace
    prepared, analyzed, rendered, finished = [], [], [], Event()

    async def fake_prepare(settings, source_store, session_id, transcribe):
        prepared.append((session_id, transcribe))

    async def fake_analyze(detector, session_id, start=0, end=None):
        analyzed.append((session_id, start, end))
        ready = detector.store.add_anchor(session_id, start=10, end=80, timestamp=35,
                                          title="完整的自动精彩片段", method="model",
                                          status="ready", score=90)
        low_score = detector.store.add_anchor(session_id, start=10, end=80, timestamp=35,
                                              title="未达到门槛的候选", method="model",
                                              status="ready", score=60)
        return [ready, low_score]

    async def fake_render(renderer, job_id):
        rendered.append(renderer.store.job(job_id))
        renderer.store.update_job(job_id, status="complete", progress=100)
        finished.set()

    monkeypatch.setattr("clipper.server.prepare_file", fake_prepare)
    monkeypatch.setattr("clipper.detector.Detector.analyze", fake_analyze)
    monkeypatch.setattr("clipper.server.Renderer.run", fake_render)
    response = client.post(f"/api/sessions/{session['id']}/analyze", json={"transcribe": False})
    assert response.status_code == 202, response.text
    assert finished.wait(timeout=3), "The automatic candidate was not rendered."
    assert prepared == [(session["id"], False)]
    assert analyzed == [(session["id"], 0, 90)]
    assert len(rendered) == 1
    assert rendered[0]["anchor"]["score"] == 90
    assert rendered[0]["options"]["resolution"] == 1080
    assert rendered[0]["options"]["aspect"] == "landscape"
    assert len(store.jobs(session["id"])) == 1
