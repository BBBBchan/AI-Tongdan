"""Manual permission during patrol never grants automatic sending (offline I/O)."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from app.danmaku_send import DanmakuSendError
from test_candidate_only_session import application, client_for, parked


async def setup_manual(app, monkeypatch, *, local=False):
    ws = app.state.workspace
    await ws.start("213", candidate_only=True, allow_manual_send=True)
    ws.session["status"] = "live"
    monkeypatch.setattr(ws.danmaku_credentials, "status", lambda: {"configured": True, "max_length": 20})
    if local:
        ws.danmaku_transport = "local"
        return ws, ws.local_danmaku.connect(ws.local_danmaku.pairing()["code"])["token"]
    return ws, None


@pytest.mark.asyncio
async def test_manual_server_send_uses_real_guard_and_history_but_no_platform(application, monkeypatch):
    app = await application()
    ws, _ = await setup_manual(app, monkeypatch)
    posted = []

    async def fake_sender(room, text, *, before_post):
        before_post()
        posted.append((room, text))
        return {"sent": True, "text": text}

    monkeypatch.setattr(ws.danmaku_sender, "send", fake_sender)
    async with client_for(app) as client:
        state = (await client.get("/api/state")).json()
        status = state["danmaku_send"]
        assert status["status"] == "manual_only" and status["allow_manual_send"]
        assert status["can_send"] and not status["can_auto_send"]
        assert status["reason"] == status["auto_disabled_reason"] == "仅手动发送，自动发送已禁用"
        response = await client.post("/api/danmaku/send", json={"session_id": ws.session["id"], "text": "离线测试"})
    assert response.status_code == 200 and response.json()["sent"]
    assert posted == [(213, "离线测试")]
    assert ws.sent_danmaku[-1]["source"] == "manual"
    assert ws.danmaku_auto.task is None


@pytest.mark.asyncio
async def test_manual_local_can_claim_authorize_and_complete_without_server_sender(application, monkeypatch):
    app = await application()
    ws, token = await setup_manual(app, monkeypatch, local=True)
    headers = {"Authorization": "Bearer " + token}
    async with client_for(app) as client:
        pending = asyncio.create_task(client.post("/api/danmaku/send", json={"session_id": ws.session["id"], "text": "离线助手"}))
        for _ in range(100):
            job = (await client.post("/api/danmaku/local/poll", headers=headers, json={})).json()["job"]
            if job is not None:
                break
            await asyncio.sleep(.005)
        assert job is not None
        assert job["text"] == "离线助手"
        authorized = await client.post("/api/danmaku/local/authorize", headers=headers, json={"job_id": job["id"]})
        assert authorized.status_code == 200 and authorized.json()["authorized"]
        complete = await client.post("/api/danmaku/local/result", headers=headers, json={
            "job_id": job["id"], "outcome": "accepted", "code": "success", "receipt": {}})
        assert complete.status_code == 200
        response = await asyncio.wait_for(pending, 1)
    assert response.status_code == 200
    assert len(ws.sent_danmaku) == 1 and ws.sent_danmaku[0]["source"] == "manual"
    ws.danmaku_sender.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [False, True])
async def test_all_auto_entrypoints_remain_blocked(application, monkeypatch, local):
    app = await application()
    ws, _ = await setup_manual(app, monkeypatch, local=local)
    sid = ws.session["id"]
    async with client_for(app) as client:
        response = await client.post("/api/danmaku/auto/start", json={"session_id": sid})
    assert response.status_code == 409 and response.json()["code"] == "auto_send_disabled"
    for operation in (lambda: ws.danmaku_auto.start(sid),
                      lambda: ws._send_danmaku_locked(sid, "不得发送", source="auto"),
                      lambda: ws.local_danmaku.send(213, "不得入队", guard=lambda: None, source="auto")):
        with pytest.raises(DanmakuSendError) as denied:
            await operation()
        assert denied.value.code == "auto_send_disabled"
    ws.danmaku_auto.data.update(status="active", session_id=sid, room_id=213)
    await ws.danmaku_auto.tick()
    assert ws.danmaku_auto.data["status"] == "stopped"
    assert ws.danmaku_auto.task is None and ws.local_danmaku._job is None
    ws.danmaku_sender.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source,expected", [("auto", "auto_send_disabled"), ("manual", "candidate_only")])
async def test_final_post_guard_rechecks_policy_after_authentication(application, monkeypatch, source, expected):
    app = await application()
    ws = app.state.workspace
    await ws.start("213")
    ws.session["status"] = "live"
    posts = []

    async def delayed_auth(room, text, *, before_post):
        ws.session.update(candidate_only=True, allow_manual_send=source == "auto")
        before_post()
        posts.append(text)
        return {"sent": True, "text": text}

    monkeypatch.setattr(ws.danmaku_sender, "send", delayed_auth)
    with pytest.raises(DanmakuSendError) as denied:
        await ws._send_danmaku_locked(ws.session["id"], "不得发送", source=source)
    assert denied.value.code == expected and not posts and not ws.sent_danmaku


@pytest.mark.asyncio
@pytest.mark.parametrize("already_claimed", [False, True])
async def test_queued_auto_job_cannot_be_claimed_or_authorized_under_manual_policy(application, already_claimed):
    app = await application()
    ws = app.state.workspace
    await ws.start("213")
    broker = ws.local_danmaku
    token = broker.connect(broker.pairing()["code"])["token"]
    pending = asyncio.create_task(broker.send(213, "不得发旧任务", guard=lambda: None, source="auto"))
    await asyncio.sleep(0)
    job = broker._job
    if already_claimed:
        assert broker.poll(token)["job"]
    ws.session.update(candidate_only=True, allow_manual_send=True)
    try:
        for call in (lambda: broker.poll(token), lambda: broker.authorize(token, job.id)):
            with pytest.raises(DanmakuSendError) as denied:
                call()
            assert denied.value.code == "auto_send_disabled"
        assert job.delivered is already_claimed and not job.authorized
    finally:
        broker.cancel()
        with pytest.raises(DanmakuSendError):
            await pending


@pytest.mark.asyncio
async def test_policy_api_persists_without_switching_room_and_resumes_manual_only(application, monkeypatch):
    first = await application()
    ws = first.state.workspace
    await ws.start("213", candidate_only=True)
    sid, agent = ws.session["id"], ws.agent
    path = ws.settings.data_dir / "sessions" / sid / "session.json"
    async with client_for(first) as client:
        response = await client.post("/api/session/danmaku-policy", json={"session_id": sid, "allow_manual_send": True})
    assert response.status_code == 200
    assert response.json()["session"]["id"] == sid and ws.agent is agent
    assert response.json()["danmaku_send"]["status"] == "manual_only"
    assert ws.allow_manual_send and json.loads(path.read_text())["session"]["allow_manual_send"] is True
    event_types = [event["type"] for event in ws.bus.history]
    assert event_types[-2:] == ["danmaku_send_status", "state"]
    await ws.close()
    second = await application()
    await second.state.workspace.resume()
    resumed = second.state.workspace
    assert resumed.session["id"] == sid and resumed.candidate_only and resumed.allow_manual_send
    assert resumed.danmaku_send_status()["status"] == "manual_only"
    assert not resumed.danmaku_send_status()["can_auto_send"] and resumed.danmaku_auto.task is None
    async with client_for(second) as client:
        disabled = await client.post("/api/session/danmaku-policy", json={"session_id": sid, "allow_manual_send": False})
    assert disabled.status_code == 200 and disabled.json()["danmaku_send"]["status"] == "candidate_only"
    assert json.loads(path.read_text())["session"]["allow_manual_send"] is False


@pytest.mark.asyncio
async def test_policy_update_cancels_queued_jobs_and_stops_old_auto(application, monkeypatch):
    app = await application()
    ws = app.state.workspace
    await ws.start("213", candidate_only=True, allow_manual_send=True)
    sid = ws.session["id"]
    token = ws.local_danmaku.connect(ws.local_danmaku.pairing()["code"])["token"]
    pending = asyncio.create_task(ws.local_danmaku.send(213, "旧手动草稿", guard=lambda: None, source="manual"))
    await asyncio.sleep(0)
    old_job = ws.local_danmaku._job
    ws.danmaku_auto.data.update(status="active", session_id=sid)
    ws.danmaku_auto.task = asyncio.create_task(parked())
    auto_task = ws.danmaku_auto.task
    await ws.set_danmaku_policy(sid, True)
    with pytest.raises(DanmakuSendError):
        await pending
    assert ws.local_danmaku._job is None and auto_task.cancelled()
    assert ws.danmaku_auto.data["status"] == "stopped"
    with pytest.raises(DanmakuSendError):
        ws.local_danmaku.authorize(token, old_job.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "true", 1, 0, None, [], {}])
async def test_policy_strict_bool_and_session_creation_reject_bad_values(application, value):
    app = await application()
    ws = app.state.workspace
    await ws.start("213", candidate_only=True)
    sid = ws.session["id"]
    async with client_for(app) as client:
        for endpoint, body in (("/api/session/danmaku-policy", {"session_id": sid, "allow_manual_send": value}),
                               ("/api/session", {"room": "213", "allow_manual_send": value})):
            response = await client.post(endpoint, json=body)
            assert response.status_code == 422
    assert ws.session["id"] == sid and not ws.allow_manual_send


@pytest.mark.asyncio
async def test_stale_session_and_normal_session_cannot_mutate_policy(application):
    app = await application()
    ws = app.state.workspace
    await ws.start("213", candidate_only=True)
    sid = ws.session["id"]
    async with client_for(app) as client:
        stale = await client.post("/api/session/danmaku-policy", json={"session_id": "obsolete", "allow_manual_send": True})
        assert stale.status_code == 409 and stale.json()["code"] == "session_changed"
        assert not ws.allow_manual_send and ws.session["id"] == sid
        await ws.start("213")
        normal = await client.post("/api/session/danmaku-policy", json={"session_id": ws.session["id"], "allow_manual_send": True})
        assert normal.status_code == 409 and normal.json()["code"] == "policy_not_applicable"
        assert not ws.candidate_only and not ws.allow_manual_send


@pytest.mark.asyncio
async def test_atomic_save_failure_keeps_both_permissions_and_saved_session_unchanged(application, monkeypatch):
    app = await application()
    ws = app.state.workspace
    await ws.start("213", candidate_only=True)
    sid = ws.session["id"]
    path = ws.settings.data_dir / "sessions" / sid / "session.json"
    before = path.read_bytes()
    events_before = len(ws.bus.history)
    replace = os.replace
    failures = []

    def fail_session_replace(source, destination, *args, **kwargs):
        if Path(destination) == path:
            failures.append(destination)
            raise OSError("disk failed")
        return replace(source, destination, *args, **kwargs)

    # os is shared by server and pathlib: an unconditional module-wide patch
    # also breaks audio shutdown snapshots on Python 3.11+. Inject only the
    # policy commit fault, and restore it before application fixture cleanup.
    with monkeypatch.context() as fault:
        fault.setattr("app.server.os.replace", fail_session_replace)
        async with client_for(app) as client:
            response = await client.post("/api/session/danmaku-policy", json={"session_id": sid, "allow_manual_send": True})
    assert failures == [path]
    assert response.status_code == 503 and response.json()["code"] == "storage_unavailable"
    assert not ws.allow_manual_send and not ws.danmaku_send_status()["can_send"]
    assert path.read_bytes() == before and len(ws.bus.history) == events_before
    assert not list(path.parent.glob(".session-*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_value", ["missing", "false"])
async def test_legacy_missing_manual_permission_defaults_closed_and_invalid_saved_bool_blocks_resume(application, saved_value):
    first = await application()
    ws = first.state.workspace
    await ws.start("213", candidate_only=True)
    path = ws.settings.data_dir / "sessions" / ws.session["id"] / "session.json"
    await ws.close()
    saved = json.loads(path.read_text())
    if saved_value == "missing":
        saved["session"].pop("allow_manual_send")
    else:
        saved["session"]["allow_manual_send"] = saved_value
    path.write_text(json.dumps(saved))
    second = await application()
    restored = second.state.workspace
    await restored.resume()
    assert not restored.allow_manual_send and not restored.danmaku_send_status()["can_send"]
    assert (restored.session is not None) == (saved_value == "missing")
    assert restored.danmaku_auto.task is None
