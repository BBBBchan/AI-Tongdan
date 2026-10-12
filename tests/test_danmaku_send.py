"""No test sends to Bilibili: every HTTP request uses MockTransport."""
from email.parser import BytesParser
from email.policy import default
import json
import stat

import httpx
import pytest

from app.bilibili import WBI_INDICES, wbi_sign
from app.danmaku_send import DanmakuCredentials, DanmakuSender, DanmakuSendError

COOKIE = "SESSDATA=test-session-secret; bili_jct=test-csrf-secret; DedeUserID=123"
IMAGE_KEY = "0123456789abcdef0123456789abcdef"
SUB_KEY = "abcdef0123456789abcdef0123456789"
NAV = {"code": 0, "data": {"isLogin": True, "wbi_img": {
    "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMAGE_KEY}.png",
    "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png"}}}


def multipart_fields(request):
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.content)
    return {part.get_param("name", header="content-disposition"): part.get_payload(decode=True).decode()
            for part in message.iter_parts()}


def test_credentials_are_private_atomic_and_local_overrides_environment(tmp_path):
    credentials = DanmakuCredentials(tmp_path, "SESSDATA=environment-secret; bili_jct=environment-csrf")
    assert credentials.status() == {"configured": True, "source": "environment", "max_length": 20}
    status = credentials.save(COOKIE)
    assert status == {"configured": True, "source": "local", "max_length": 20}
    assert stat.S_IMODE(credentials.path.stat().st_mode) == 0o600
    original = credentials.path.read_bytes()
    for invalid in ["SESSDATA=only", "bili_jct=only", COOKIE + "\r\nInjected: value", "", "x" * 20000]:
        with pytest.raises(DanmakuSendError):
            credentials.save(invalid)
        assert credentials.path.read_bytes() == original
    assert "secret" not in json.dumps(status)
    assert credentials.clear()["source"] == "environment"
    assert not credentials.path.exists()
    assert DanmakuCredentials(tmp_path).status()["configured"] is False


def test_credentials_do_not_read_symlinks_or_expose_bad_contents(tmp_path):
    outside = tmp_path / "other.json"
    outside.write_text(json.dumps({"cookie": COOKIE}))
    credentials = DanmakuCredentials(tmp_path)
    credentials.path.symlink_to(outside)
    assert credentials.status()["configured"] is False
    with pytest.raises(DanmakuSendError) as error:
        credentials._authenticated_cookie()
    assert "secret" not in str(error.value)
    credentials.clear()
    assert outside.exists()


@pytest.mark.asyncio
async def test_official_form_cookie_csrf_wbi_and_safe_acceptance_result(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.headers["cookie"] == COOKIE
        if request.url.path == "/x/web-interface/nav":
            assert request.method == "GET"
            return httpx.Response(200, json=NAV)
        assert request.method == "POST" and request.url.host == "api.live.bilibili.com"
        assert request.url.path == "/msg/send"
        assert request.headers["referer"] == "https://live.bilibili.com/6154037"
        fields = multipart_fields(request)
        assert fields["msg"] == "这段好精彩"
        assert fields["roomid"] == "6154037"
        assert fields["csrf"] == fields["csrf_token"] == "test-csrf-secret"
        assert (fields["mode"], fields["fontsize"], fields["color"]) == ("1", "25", "16777215")
        assert fields["rnd"].isdigit()
        query = dict(request.url.params)
        key = "".join((IMAGE_KEY + SUB_KEY)[index] for index in WBI_INDICES)
        assert query == wbi_sign({"web_location": "444.8"}, key, int(query["wts"]))
        assert "csrf" not in query and "msg" not in query
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {"mode_info": {"extra": "private-upstream"}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        result = await sender.send(6154037, "这段好精彩")
        await sender.close()
        assert not client.is_closed
    assert len(calls) == 2
    assert result == {"sent": True, "room_id": 6154037, "text": "这段好精彩", "platform_code": 0,
                      "message": "Bilibili 已受理发送请求"}
    assert "secret" not in json.dumps(result) and "private-upstream" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " ", "字" * 21, "😀" * 11, "多行\n弹幕", "控制\x00字符"])
async def test_invalid_text_never_contacts_platform(tmp_path, text):
    def handle(request):
        pytest.fail("Invalid input must not contact any endpoint")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DanmakuSendError):
            await DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client).send(732, text)


@pytest.mark.asyncio
async def test_missing_credentials_never_contacts_platform(tmp_path):
    def handle(request):
        pytest.fail("Missing credentials must not contact any endpoint")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DanmakuSendError) as error:
            await DanmakuSender(DanmakuCredentials(tmp_path), client).send(732, "你好")
    assert error.value.status_code == 401 and error.value.code == "login_required"


@pytest.mark.asyncio
async def test_expired_login_never_posts(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"code": -101, "message": COOKIE})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DanmakuSendError) as error:
            await DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client).send(732, "你好")
    assert len(calls) == 1 and error.value.status_code == 401
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status_code,code", [
    ({"code": -111, "msg": COOKIE}, 401, "login_required"),
    ({"code": 1990000, "msg": COOKIE}, 409, "verification_required"),
    ({"code": 10031, "msg": COOKIE}, 409, "platform_rejected"),
    ({"code": 0, "msg": "f:频率限制 " + COOKIE}, 409, "platform_rejected"),
    ({"code": True, "msg": COOKIE}, 502, "invalid_response"),
    ([COOKIE], 502, "invalid_response"),
])
async def test_upstream_rejections_are_sanitized_and_not_retried(tmp_path, payload, status_code, code):
    posts = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        posts.append(request)
        return httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DanmakuSendError) as error:
            await DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client).send(732, "你好")
    assert len(posts) == 1
    assert error.value.status_code == status_code and error.value.code == code
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_never_retries_or_leaks_exception_text(tmp_path):
    posts = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        posts.append(request)
        raise httpx.ReadTimeout(COOKIE, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(DanmakuSendError) as error:
            await DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client).send(732, "你好")
    assert len(posts) == 1 and error.value.code == "send_unknown"
    assert "暂不确定" in str(error.value) and "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_redirect_does_not_forward_authenticated_request(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(DanmakuSendError):
            await DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client).send(732, "你好")
    assert len(calls) == 1 and calls[0].url.host == "api.bilibili.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["trial_stopped", "trial_expired"])
async def test_trial_guard_can_abort_after_login_get_without_posting(tmp_path, code):
    calls = []
    stopped_or_expired = False

    def handle(request):
        nonlocal stopped_or_expired
        calls.append(request.method)
        assert request.method == "GET", "A revoked trial must never reach POST"
        # State can change while the login request is in flight.
        stopped_or_expired = True
        return httpx.Response(200, json=NAV)

    def before_post():
        calls.append("guard")
        assert stopped_or_expired, "The guard must run after the login GET"
        raise DanmakuSendError("本轮试运行已停止", 409, code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好", before_post=before_post)
        assert not sender._send_lock.locked()
    assert calls == ["GET", "guard"]
    assert error.value.code == code and error.value.status_code == 409


@pytest.mark.asyncio
async def test_valid_trial_guard_runs_once_between_get_and_post(tmp_path):
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": 0})

    def before_post():
        calls.append("guard")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        result = await sender.send(732, "你好", before_post=before_post)
    assert result["sent"] is True
    assert calls == ["GET", "guard", "POST"]
