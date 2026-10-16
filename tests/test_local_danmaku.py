"""No Bilibili traffic: exercise the in-memory local-helper broker only."""
import asyncio
import json
import time

import pytest

from app.danmaku_send import DanmakuSendError
from app.local_danmaku import LocalDanmakuBroker, safe_receipt


def connected():
    broker = LocalDanmakuBroker()
    token = broker.connect(broker.pairing()["code"])["token"]
    return broker, token


async def claim(broker, token, guard=lambda: None, source="manual"):
    task = asyncio.create_task(broker.send(123, "测试草稿", guard=guard, source=source))
    await asyncio.sleep(0)
    job = broker.poll(token)["job"]
    assert job
    return task, job


@pytest.mark.asyncio
async def test_job_is_claimed_and_authorized_once_and_completion_is_idempotent():
    broker, token = connected()
    checks = []
    task, job = await claim(broker, token, lambda: checks.append("guard"))
    assert broker.poll(token) == {"job": None}
    assert broker.authorize(token, job["id"]) == {"authorized": True, "valid_for_seconds": 3}
    with pytest.raises(DanmakuSendError):
        broker.authorize(token, job["id"])
    for _ in range(2):
        assert broker.result(token, job["id"], "accepted", "success", {}) == {"ok": True}
    result = await task
    assert result["sent"] and result["text"] == "测试草稿"
    assert checks == ["guard", "guard"]
    assert broker.last_receipt["outcome"] == "accepted"
    assert broker.poll(token) == {"job": None}


@pytest.mark.asyncio
async def test_acceptance_requires_authorization_but_nav_failure_does_not():
    broker, token = connected()
    task, job = await claim(broker, token)
    with pytest.raises(DanmakuSendError) as error:
        broker.result(token, job["id"], "accepted", "success", {})
    assert error.value.code == "local_authorization_denied"
    broker.result(token, job["id"], "rejected", "login_required", {"phase": "nav"})
    with pytest.raises(DanmakuSendError) as error:
        await task
    assert error.value.code == "login_required"
    assert broker.last_receipt["phase"] == "nav"


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
@pytest.mark.parametrize("action", ["cancel", "expire", "disconnect", "replace"])
async def test_revoked_jobs_are_never_requeued(authorized, action):
    broker, token = connected()
    task, job = await claim(broker, token)
    if authorized:
        broker.authorize(token, job["id"])
    if action == "cancel":
        broker.cancel()
    elif action == "expire":
        broker._job.deadline = time.monotonic() - 1
        with pytest.raises(DanmakuSendError):
            broker.authorize(token, job["id"])
    elif action == "disconnect":
        broker.disconnect()
    else:
        token = broker.connect(broker.pairing()["code"])["token"]
    with pytest.raises(DanmakuSendError) as error:
        await task
    assert (error.value.code == "send_unknown") == authorized
    assert broker.last_receipt["outcome"] == ("unknown" if authorized else "rejected")
    with pytest.raises(DanmakuSendError):
        broker.authorize(token, job["id"])
    if broker.connected:
        assert broker.poll(token) == {"job": None}


@pytest.mark.asyncio
async def test_guard_is_rechecked_after_helper_login():
    broker, token = connected()
    stopped = False
    def guard():
        if stopped:
            raise DanmakuSendError("测试任务已停止", 409, "trial_stopped")
    task, job = await claim(broker, token, guard)
    stopped = True
    with pytest.raises(DanmakuSendError):
        broker.authorize(token, job["id"])
    with pytest.raises(DanmakuSendError) as error:
        await task
    assert error.value.code == "trial_stopped"


@pytest.mark.asyncio
async def test_timeout_has_no_automatic_reclaim():
    broker, token = connected()
    broker.job_timeout = .02
    task, job = await claim(broker, token)
    with pytest.raises(DanmakuSendError) as error:
        await asyncio.wait_for(task, .3)
    assert error.value.code == "local_job_expired"
    assert broker.poll(token) == {"job": None}


@pytest.mark.asyncio
async def test_cancelling_waiter_retires_authorized_job():
    broker, token = connected()
    task, job = await claim(broker, token)
    broker.authorize(token, job["id"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.last_receipt["outcome"] == "unknown"
    assert broker.poll(token) == {"job": None}


def test_pairing_is_one_use_expires_and_has_bounded_attempts():
    broker = LocalDanmakuBroker()
    code = broker.pairing()["code"]
    assert len(code) == 8 and code.isdecimal()
    assert broker.connect(code)["token"]
    with pytest.raises(DanmakuSendError):
        broker.connect(code)
    code = broker.pairing()["code"]
    broker._pair_expires = time.monotonic() - 1
    with pytest.raises(DanmakuSendError):
        broker.connect(code)
    code = broker.pairing()["code"]
    for _ in range(10):
        with pytest.raises(DanmakuSendError):
            broker.connect("无效配对码")
    with pytest.raises(DanmakuSendError):
        broker.connect(code)


def test_tokens_and_heartbeat_expiry_revoke_connection():
    broker, token = connected()
    for invalid in [None, "", "wrong", "无效"]:
        with pytest.raises(DanmakuSendError):
            broker.poll(invalid)
    broker._heartbeat -= broker.heartbeat_timeout + 1
    assert not broker.connected and broker.paired
    with pytest.raises(DanmakuSendError):
        broker.poll(token)
    broker.disconnect()
    assert not broker.paired


@pytest.mark.asyncio
async def test_authorize_refreshes_heartbeat_before_long_post():
    broker, token = connected()
    task, job = await claim(broker, token)
    broker._heartbeat -= 30
    broker.authorize(token, job["id"])
    assert time.monotonic() - broker._heartbeat < 1
    broker.result(token, job["id"], "unknown", "send_unknown", {})
    with pytest.raises(DanmakuSendError):
        await task


@pytest.mark.asyncio
async def test_result_does_not_reflect_raw_account_cookie_or_error_fields():
    broker, token = connected()
    task, job = await claim(broker, token)
    broker.result(token, job["id"], "unknown", "raw-secret", {
        "message_kind": "raw-secret", "phase": {"secret": "raw-secret"},
        "cookie": "raw-secret", "uid": 12345, "detail": "raw-secret", "http_status": "raw-secret"})
    with pytest.raises(DanmakuSendError) as error:
        await task
    public = json.dumps(broker.last_receipt) + str(error.value)
    assert "raw-secret" not in public and "cookie" not in public and "uid" not in public
    assert broker.last_receipt["message_kind"] == "send_unknown"
    assert broker.last_receipt["http_status"] is None


@pytest.mark.parametrize("text", ["", "字" * 21, "x\ny", "\ud800"])
@pytest.mark.asyncio
async def test_invalid_text_never_creates_job(text):
    broker, token = connected()
    with pytest.raises(DanmakuSendError):
        await broker.send(123, text, guard=lambda: None, source="manual")
    assert broker.poll(token) == {"job": None}
