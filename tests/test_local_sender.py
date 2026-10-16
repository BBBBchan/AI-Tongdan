"""Desktop sends use only mock transports: no Bilibili requests leave these tests."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

import httpx
import pytest

import local_sender as sender_module
from app.danmaku_send import DanmakuSender, DanmakuSendError
from local_sender import (JobLedger, LocalCredentials, LocalSender, LocalSenderError,
                          PairingLost, load_cookie_file, safe_receipt, validate_demo_url)

COOKIE = "SESSDATA=private-desktop-session; bili_jct=private-desktop-csrf; DedeUserID=123456789"
TOKEN = "pairing-token-private-123456789"
JOB = {"id": "a" * 32, "room_id": 732, "text": "好听好听"}
NAV = {"code": 0, "data": {"isLogin": True, "mid": 123456789, "uname": "private-account-name",
    "wbi_img": {"img_url": "https://i0.hdslb.com/" + "a" * 32 + ".png",
                "sub_url": "https://i0.hdslb.com/" + "b" * 32 + ".png"}}}


@pytest.mark.parametrize("arguments,expected", [
    ([], "http://127.0.0.1:7862"),
    (["--demo", "http://localhost:9876"], "http://localhost:9876"),
])
def test_cli_uses_app_default_port_and_accepts_a_forwarded_port(monkeypatch, arguments, expected):
    calls = []
    monkeypatch.setattr(sender_module, "load_cookie_file", lambda _: COOKIE)
    monkeypatch.setattr(sender_module.getpass, "getpass", lambda _: "12345678")

    async def run(args, code):
        calls.append((args.demo, code))

    monkeypatch.setattr(sender_module, "run_local", run)
    assert sender_module.main(arguments) == 0
    assert calls == [(expected, "12345678")]


@asynccontextmanager
async def harness(tmp_path, *, bili_reply=None, demo_reply=None, monotonic=None):
    events, bili_requests, demo_requests, reports, outputs = [], [], [], [], []
    async def bili(request):
        bili_requests.append(request)
        assert request.headers["cookie"] == COOKIE
        assert "authorization" not in request.headers
        if request.method == "GET":
            events.append("nav")
            return httpx.Response(200, json=NAV)
        events.append("bili_post")
        if bili_reply is not None:
            return await bili_reply(request)
        return httpx.Response(200, json={"code": 0, "message": "0"})
    async def demo(request):
        demo_requests.append(request)
        assert "cookie" not in request.headers
        assert "private-desktop" not in request.content.decode()
        assert "private-account-name" not in request.content.decode()
        if request.url.path.endswith("connect"):
            assert json.loads(request.content) == {"code": "12345678"}
            return httpx.Response(200, json={"token": TOKEN, "poll_interval": 1})
        assert request.headers["authorization"] == "Bearer " + TOKEN
        if request.url.path.endswith("authorize"):
            events.append("authorize")
        if request.url.path.endswith("result"):
            reports.append(json.loads(request.content))
        if demo_reply is not None:
            response = await demo_reply(request)
            if response is not None:
                return response
        if request.url.path.endswith("authorize"):
            return httpx.Response(200, json={"authorized": True, "valid_for_seconds": 3})
        return httpx.Response(200, json={"ok": True, "job": None})
    async with httpx.AsyncClient(transport=httpx.MockTransport(bili)) as bili_client:
        async with httpx.AsyncClient(transport=httpx.MockTransport(demo)) as demo_client:
            sender = DanmakuSender(LocalCredentials(tmp_path, env_cookie=COOKIE), bili_client)
            helper = LocalSender("http://127.0.0.1:7864", sender, tmp_path, demo_client=demo_client,
                                 output=outputs.append, **({"monotonic": monotonic} if monotonic else {}))
            await helper.connect("12345678")
            try:
                yield helper, events, bili_requests, demo_requests, reports, outputs
            finally:
                await helper.close()


@pytest.mark.parametrize("url", ["http://127.0.0.1:7864", "http://localhost:7863/", "http://[::1]:7864", "http://127.0.0.1"])
def test_demo_accepts_only_explicit_loopback(url):
    assert validate_demo_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["https://127.0.0.1", "http://example.com", "http://127.0.0.2",
    "http://user:secret@localhost", "http://localhost/path", "http://localhost?token=x",
    "http://localhost#x", "http://localhost//", "http://localhost:0", "http://localhost:65536",
    "http://127.1", "http://2130706433", "http://localhost%2e", " http://localhost"])
def test_demo_rejects_remote_or_ambiguous_urls(url):
    with pytest.raises(LocalSenderError):
        validate_demo_url(url)


@pytest.mark.asyncio
async def test_cookie_remains_on_desktop_and_authorize_is_after_nav(tmp_path):
    async with harness(tmp_path) as (helper, events, bili, demo, reports, outputs):
        result = await helper.process_job(JOB)
        assert result["outcome"] == "accepted"
        assert events == ["nav", "authorize", "bili_post"]
        assert len(bili) == 2
        assert len(reports) == 1 and reports[0]["code"] == "success"
        assert reports[0]["receipt"]["outcome"] == "accepted"
        ledger = list((tmp_path / "jobs").glob("*.json"))
        assert len(ledger) == 1
        assert set(json.loads(ledger[0].read_text())) == {"job_id", "claimed_at"}
        public = json.dumps(reports) + "".join(outputs)
        for secret in ("private-desktop", "private-account-name", TOKEN, JOB["text"], "123456789"):
            assert secret not in public
        assert all(request.url.host == "127.0.0.1" for request in demo)
        assert not (tmp_path / "bilibili_send_credentials.private.json").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected", [("deny", "local_authorization_denied"), ("expired", "local_authorization_expired"),
                                           ("stop", "local_authorization_denied")])
async def test_authorization_failure_never_posts_to_bilibili(tmp_path, state, expected):
    clock = [100.0]
    helper_ref = []
    async def reply(request):
        if request.url.path.endswith("authorize"):
            if state == "deny":
                return httpx.Response(409, json={"detail": "untrusted-private-server-message"})
            if state == "expired":
                clock[0] += 3.1
            if state == "stop":
                helper_ref[0].stop_requested.set()
    async with harness(tmp_path, demo_reply=reply, monotonic=lambda: clock[0]) as (helper, events, bili, _, reports, outputs):
        helper_ref.append(helper)
        result = await helper.process_job(JOB)
        assert result["code"] == expected
        assert result["outcome"] == "rejected"
        assert events == ["nav", "authorize"]
        assert len(bili) == 1 and reports[-1]["code"] == expected
        assert "untrusted" not in "".join(outputs)


@pytest.mark.asyncio
async def test_durable_dedup_after_helper_restart(tmp_path):
    async with harness(tmp_path) as (helper, _, bili, _, _, _):
        assert (await helper.process_job(JOB))["outcome"] == "accepted"
        assert len(bili) == 2
    async with harness(tmp_path) as (helper, events, bili, _, reports, _):
        assert (await helper.process_job(JOB))["code"] == "duplicate_job"
        assert not bili and not events
        assert reports[0]["outcome"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_kind", ["timeout", "f", "unknown", "invalid_json"])
async def test_uncertain_bilibili_reply_is_never_retried(tmp_path, reply_kind):
    async def reply(request):
        if reply_kind == "timeout":
            raise httpx.ReadTimeout("sensitive-network-error", request=request)
        if reply_kind == "invalid_json":
            return httpx.Response(200, text="private-desktop-session")
        return httpx.Response(200, json={"code": 0, "msg": "f" if reply_kind == "f" else "k"})
    async with harness(tmp_path, bili_reply=reply) as (helper, events, bili, _, reports, outputs):
        result = await helper.process_job(JOB)
        assert result["code"] == "send_unknown" and result["outcome"] == "unknown"
        assert events.count("bili_post") == 1
        await helper.process_job(JOB)
        assert events.count("bili_post") == 1
        assert "sensitive-network-error" not in "".join(outputs)


@pytest.mark.asyncio
async def test_only_result_reporting_retries_with_same_job_id(tmp_path):
    attempts = []
    async def demo_reply(request):
        if request.url.path.endswith("result"):
            attempts.append(request.content)
            if len(attempts) < 3:
                raise httpx.ConnectError("do-not-print", request=request)
    async with harness(tmp_path, demo_reply=demo_reply) as (helper, events, _, _, _, _):
        assert (await helper.process_job(JOB))["outcome"] == "accepted"
        assert len(attempts) == 3 and len(set(attempts)) == 1
        assert events.count("bili_post") == 1


@pytest.mark.asyncio
async def test_poll_heartbeats_continue_during_post_and_duplicate_delivery(tmp_path):
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    polls = []
    async def bili_reply(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"code": 0})
    async def demo_reply(request):
        if request.url.path.endswith("poll"):
            polls.append(None)
            return httpx.Response(200, json={"job": JOB if len(polls) <= 3 else None})
        if request.url.path.endswith("result"):
            finished.set()
    async with harness(tmp_path, bili_reply=bili_reply, demo_reply=demo_reply) as (helper, events, _, _, reports, _):
        helper.poll_interval = .01
        task = asyncio.create_task(helper.run())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.sleep(.07)
            assert len(polls) >= 4 and not reports
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            assert len(reports) == 1 and reports[0]["outcome"] == "accepted"
            assert events.count("bili_post") == 1
        finally:
            helper.stop_requested.set()
            await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_401_stops_before_external_post(tmp_path):
    async def demo_reply(request):
        if request.url.path.endswith("authorize"):
            return httpx.Response(401, json={"detail": "token-private-error"})
    async with harness(tmp_path, demo_reply=demo_reply) as (helper, events, _, _, reports, _):
        with pytest.raises(PairingLost):
            await helper.process_job(JOB)
        assert helper.stop_requested.is_set() and helper.token == ""
        assert events == ["nav", "authorize"] and not reports


@pytest.mark.asyncio
async def test_demo_redirect_is_not_followed(tmp_path):
    async def demo_reply(request):
        if request.url.path.endswith("authorize"):
            return httpx.Response(307, headers={"location": "https://external.example/secret"})
    async with harness(tmp_path, demo_reply=demo_reply) as (helper, events, _, requests, _, _):
        assert (await helper.process_job(JOB))["code"] == "local_authorization_denied"
        assert events == ["nav", "authorize"]
        assert all(request.url.host == "127.0.0.1" for request in requests)


def test_ledger_on_windows_without_fchmod_contains_only_task_metadata(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "fchmod", raising=False)
    ledger = JobLedger(tmp_path)
    assert ledger.claim(JOB["id"])
    assert not JobLedger(tmp_path).claim(JOB["id"])
    saved = json.loads(next((tmp_path / "jobs").iterdir()).read_text())
    assert saved["job_id"] == JOB["id"] and set(saved) == {"job_id", "claimed_at"}


def test_receipt_allowlist_and_cookie_file_validation(tmp_path):
    safe = safe_receipt({"phase": "send", "outcome": "unknown", "message_kind": "platform_f_notice",
        "http_status": 200, "platform_code": 0, "updated_at": float("nan"),
        "cookie": COOKIE, "token": TOKEN, "message": "private-account-name", "data": {"uid": 12}})
    assert safe == {"phase": "send", "outcome": "unknown", "message_kind": "platform_f_notice", "http_status": 200, "platform_code": 0}
    cookie = tmp_path / "cookie.txt"
    cookie.write_text(COOKIE + "\n")
    assert load_cookie_file(cookie) == COOKIE
    cookie.write_text("SESSDATA=private-desktop-session")
    with pytest.raises(LocalSenderError):
        load_cookie_file(cookie)


@pytest.mark.asyncio
async def test_claim_failure_prevents_even_login_check(tmp_path, monkeypatch):
    async with harness(tmp_path) as (helper, events, bili, _, reports, _):
        def fail_claim(_):
            raise LocalSenderError("保存失败", code="storage_unavailable")
        monkeypatch.setattr(helper.ledger, "claim", fail_claim)
        result = await helper.process_job(JOB)
        assert result["code"] == "storage_unavailable"
        assert not bili and not events
        assert reports[-1]["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_overlapping_job_is_claimed_and_rejected_without_second_post(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()
    async def reply(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json={"code": 0})
    async with harness(tmp_path, bili_reply=reply) as (helper, events, _, _, reports, _):
        first = asyncio.create_task(helper.process_job(JOB))
        try:
            await asyncio.wait_for(started.wait(), 1)
            extra = {**JOB, "id": "b" * 32}
            result = await helper.process_job(extra)
            assert result == {"outcome": "rejected", "code": "sender_busy"}
            assert not helper.ledger.claim(extra["id"])
            assert events.count("bili_post") == 1
            release.set()
            assert (await first)["outcome"] == "accepted"
            assert len(reports) == 2
        finally:
            release.set()
            await first
