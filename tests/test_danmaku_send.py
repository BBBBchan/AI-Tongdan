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
    ({"code": 0, "msg": "f:频率限制 " + COOKIE}, 429, "rate_limited"),
    ({"code": True, "msg": COOKIE}, 502, "send_unknown"),
    ([COOKIE], 502, "send_unknown"),
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


@pytest.mark.asyncio
@pytest.mark.parametrize("markers", [
    {"msg": "f", "message": "f"}, {"msg": "f"}, {"message": "f"},
    {"msg": "f", "message": "0"}, {"msg": "0", "message": "f"},
    {"msg": " f ", "message": 0}, {"msg": "", "message": "f"},
    {"msg": "f", "message": "unexplained " + COOKIE},
])
async def test_short_f_notice_is_specific_but_delivery_remains_unknown(tmp_path, markers):
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": 0, **markers})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好")
    assert calls == ["GET", "POST"]
    assert error.value.code == "send_unknown" and error.value.status_code == 409
    assert str(error.value) == "Bilibili 返回提示 f，弹幕尚未确认送达；平台未说明具体原因，请勿连续重发。"
    assert sender.last_receipt["message_kind"] == "platform_f_notice"
    assert sender.last_receipt["outcome"] == "unknown"
    assert sender.last_receipt["phase"] == "send"
    assert sender.last_receipt["http_status"] == 200 and sender.last_receipt["platform_code"] == 0
    assert "secret" not in json.dumps(sender.last_receipt) + str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_code, markers, kind, status", [
    (0, {"msg": "f", "message": "f:发送频率过快"}, "rate_limited", 429),
    (0, {"msg": "f:未公开的拒绝原因", "message": "f"}, "platform_rejected", 409),
    (0, {"msg": "f", "message": "用户等级不足"}, "level_required", 409),
    (-1, {"msg": "f", "message": "f"}, "platform_rejected", 409),
])
async def test_explicit_rejection_takes_priority_over_short_f(tmp_path, platform_code, markers, kind, status):
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": platform_code, **markers})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好")
    assert calls == ["GET", "POST"]
    assert error.value.code == kind and error.value.status_code == status
    assert sender.last_receipt["outcome"] == "rejected"
    assert sender.last_receipt["message_kind"] == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("markers", [
    {"message": "0"}, {"msg": "0", "message": "0"},
    {"msg": "", "message": "0"}, {"msg": "0", "message": "success"},
    {"msg": " 0 ", "message": "OK"}, {"msg": 0, "message": None},
    {"msg": "成功", "message": "发送成功"},
])
async def test_success_markers_match_official_web_acceptance(tmp_path, markers):
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": 0, **markers})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        result = await sender.send(732, "你好")
    assert result["sent"] is True
    assert calls == ["GET", "POST"]
    receipt = sender.last_receipt
    assert receipt["phase"] == "send"
    assert receipt["outcome"] == "accepted"
    assert receipt["message_kind"] == "success"
    assert receipt["platform_code"] == 0 and receipt["http_status"] == 200
    assert set(receipt) == {"phase", "outcome", "message_kind", "platform_code", "http_status", "updated_at"}


@pytest.mark.asyncio
@pytest.mark.parametrize("platform_code,message,kind,status", [
    (0, "f:本房间要求荣耀等级达到1级才可发言", "wealth_level_required", 409),
    (10031, "用户等级不足", "level_required", 409),
    (0, "f:您已被禁言", "room_muted", 409),
    (0, "f:发送频率过快", "rate_limited", 429),
    (0, "f:请完成验证码", "verification_required", 409),
    (0, "f:风控校验失败", "verification_required", 409),
    (0, "f:登录已经过期", "login_required", 401),
    (0, "f:未公开的拒绝原因", "platform_rejected", 409),
])
async def test_explicit_rejection_is_classified_without_echoing_upstream(tmp_path, platform_code, message, kind, status):
    posts = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        posts.append(request)
        return httpx.Response(200, json={"code": platform_code, "msg": "0", "message": message + " " + COOKIE,
                                         "data": {"uid": 123, "extra": COOKIE}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好")
    assert len(posts) == 1
    assert error.value.code == kind and error.value.status_code == status
    assert sender.last_receipt["message_kind"] == kind
    assert sender.last_receipt["outcome"] == "rejected"
    assert sender.last_receipt["platform_code"] == platform_code
    public = json.dumps(sender.last_receipt, ensure_ascii=False) + str(error.value)
    assert COOKIE not in public and "secret" not in public and "uid" not in public
    assert "你好" not in public


@pytest.mark.asyncio
@pytest.mark.parametrize("markers", [
    {"message": "未公开的提示 " + COOKIE},
    {"msg": "success", "message": "未公开的提示 " + COOKIE},
    {"msg": "未公开的提示 " + COOKIE, "message": "0"},
    {"message": {"secret": COOKIE}}, {"message": [COOKIE]}, {"message": False},
    {"message": "验证码验证已通过"}, {"message": "风控校验成功"},
    {"message": "荣耀等级已经达到1级"}, {"message": "发送频率正常"},
    {"msg": "coffee"}, {"message": "friendly"}, {"msg": "ff", "message": "0"},
    {"message": "f f"},
])
async def test_unknown_code_zero_notice_is_uncertain_and_never_retried(tmp_path, markers):
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": 0, **markers})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好")
    assert calls == ["GET", "POST"]
    assert error.value.code == "send_unknown" and error.value.status_code == 409
    assert "暂不确定" in str(error.value) and "官方直播间" in str(error.value)
    assert sender.last_receipt["outcome"] == "unknown"
    assert sender.last_receipt["message_kind"] == "unknown_message"
    assert sender.last_receipt["platform_code"] == 0
    assert "secret" not in json.dumps(sender.last_receipt) + str(error.value)


@pytest.mark.asyncio
async def test_receipt_resets_and_nav_failures_never_claim_posted(tmp_path):
    logged_in = True
    calls = []
    def handle(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=NAV if logged_in else {"code": -101, "message": COOKIE})
        return httpx.Response(200, json={"code": 0, "message": "0"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        assert sender.last_receipt is None
        await sender.send(732, "你好")
        first_receipt = sender.last_receipt.copy()
        logged_in = False
        with pytest.raises(DanmakuSendError):
            await sender.send(732, "你好")
        assert first_receipt["outcome"] == "accepted"
        assert sender.last_receipt["phase"] == "nav"
        assert sender.last_receipt["outcome"] == "rejected"
        assert sender.last_receipt["message_kind"] == "login_required"
        assert sender.last_receipt["platform_code"] == -101
        with pytest.raises(DanmakuSendError):
            await sender.send(732, "")
        assert sender.last_receipt["phase"] == "validation"
        assert sender.last_receipt["platform_code"] is None
        assert sender.last_receipt["http_status"] is None
    assert calls == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_receipt_on_post_timeout_is_unknown_without_nav_success_code(tmp_path):
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        raise httpx.ReadTimeout(COOKIE, request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError):
            await sender.send(732, "你好")
    assert sender.last_receipt["phase"] == "send"
    assert sender.last_receipt["outcome"] == "unknown"
    assert sender.last_receipt["platform_code"] is None
    assert sender.last_receipt["http_status"] is None
    assert "secret" not in json.dumps(sender.last_receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,content,kind", [
    (200, b"not json", "invalid_response"),
    (503, b"upstream unavailable", "platform_unavailable"),
])
async def test_abnormal_post_response_is_uncertain_and_safe(tmp_path, status, content, kind):
    calls = []
    def handle(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        return httpx.Response(status, content=content + COOKIE.encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好")
    assert error.value.code == "send_unknown"
    assert sender.last_receipt["message_kind"] == kind
    assert sender.last_receipt["outcome"] == "unknown"
    assert sender.last_receipt["http_status"] == status
    assert sender.last_receipt["platform_code"] is None
    assert calls == ["GET", "POST"]
    assert "secret" not in str(error.value) + json.dumps(sender.last_receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("message,expected,kind", [
    ("k", "k", "send_unknown"),
    ("f:发送频率过快", "f:发送频率过快", "rate_limited"),
    ("f:本房间要求荣耀等级达到1级才能发言", "f:本房间要求荣耀等级达到1级才能发言", "wealth_level_required"),
])
async def test_private_diagnostic_preserves_short_hint_without_changing_result(tmp_path, message, expected, kind):
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": 0, "msg": message,
                                                                          "data": {"secret": COOKIE}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好")
    assert error.value.code == kind
    path = tmp_path / "bilibili_send_diagnostic.private.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    diagnostic = json.loads(path.read_text())
    assert diagnostic["msg"] == {"type": "string", "preview": expected}
    assert diagnostic["message"] == {"type": "missing"}
    assert diagnostic["has_data"] is True and diagnostic["data_type"] == "object"
    assert diagnostic["http_status"] == 200 and diagnostic["code"] == 0
    assert diagnostic["parse_status"] == "parsed" and diagnostic["updated_at"] > 0
    assert "preview" not in json.dumps(sender.last_receipt)
    assert "secret" not in path.read_text()
    assert calls == ["GET", "POST"]


@pytest.mark.asyncio
async def test_private_diagnostic_masks_credentials_account_text_urls_and_ids(tmp_path):
    cookie = COOKIE + "; opaque_flag=small-private-value"
    nav = {**NAV, "data": {**NAV["data"], "mid": 321, "uname": "私密昵称甲"}}
    text = "不保存提交正文"
    message = ("k " + cookie + " csrf test-csrf-secret test-session-secret small-private-value "
               "account 321 私密昵称甲 " + text + " https://private.example/path?x=7 "
               "uid=987; uname=未知敏感昵称; token=short; nickname:'另一个私名'; "
               "id 12345678901234 AbCdEfGhIjKlMnOpQrs 你好\x00\u202e")
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=nav if request.method == "GET" else {"code": 0, "message": message,
                   "uid": 321, "uname": "私密昵称甲", "data": {"msg": text, "account": COOKIE}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, cookie), client)
        with pytest.raises(DanmakuSendError):
            await sender.send(732, text)
    saved = (tmp_path / "bilibili_send_diagnostic.private.json").read_text()
    diagnostic = json.loads(saved)
    # The wall-clock timestamp can coincidentally contain a short test UID.
    # Check all retained response metadata, excluding that locally generated time.
    response_metadata = json.dumps({key: value for key, value in diagnostic.items() if key != "updated_at"},
                                   ensure_ascii=False)
    for secret in ("test-session-secret", "test-csrf-secret", "small-private-value", "321", "私密昵称甲", text,
                   "private.example", "987", "未知敏感昵称", "short", "另一个私名", "12345678901234", "AbCdEfGhIjKlMnOpQrs"):
        assert secret not in response_metadata
    preview = diagnostic["message"]["preview"]
    assert len(preview) <= 240 and "\x00" not in preview and "\u202e" not in preview
    assert "[已脱敏]" in preview
    assert calls == ["GET", "POST"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message,kind", [({"uid": 123, "cookie": COOKIE}, "object"), ([COOKIE], "array"), (12345, "number")])
async def test_structured_private_messages_only_record_type(tmp_path, message, kind):
    def handle(request):
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": 0, "msg": message})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        with pytest.raises(DanmakuSendError):
            await sender.send(732, "你好")
    saved = (tmp_path / "bilibili_send_diagnostic.private.json").read_text()
    assert json.loads(saved)["msg"] == {"type": kind}
    assert "secret" not in saved and "12345" not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("message,accepted", [("0", True), ("k", False)])
async def test_diagnostic_write_failure_never_changes_send_result_or_retries(tmp_path, monkeypatch, message, accepted):
    calls = []
    def replace_fails(*args):
        raise PermissionError("private exception " + COOKIE)
    monkeypatch.setattr("app.danmaku_send.os.replace", replace_fails)
    def handle(request):
        calls.append(request.method)
        return httpx.Response(200, json=NAV if request.method == "GET" else {"code": 0, "message": message})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        if accepted:
            assert (await sender.send(732, "你好"))["sent"] is True
        else:
            with pytest.raises(DanmakuSendError) as error:
                await sender.send(732, "你好")
            assert error.value.code == "send_unknown" and "secret" not in str(error.value)
    assert calls == ["GET", "POST"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_invalid_json_overwrites_previous_hint_with_fresh_metadata(tmp_path):
    calls = []
    first = True
    def handle(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=NAV)
        return httpx.Response(200, json={"code": 0, "message": "0"}) if first else httpx.Response(200, content=COOKIE.encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        sender = DanmakuSender(DanmakuCredentials(tmp_path, COOKIE), client)
        await sender.send(732, "你好")
        path = tmp_path / "bilibili_send_diagnostic.private.json"
        previous = json.loads(path.read_text())
        first = False
        with pytest.raises(DanmakuSendError) as error:
            await sender.send(732, "你好")
        assert error.value.code == "send_unknown"
    current = json.loads(path.read_text())
    assert current["parse_status"] == "invalid_json" and current["code"] is None
    assert current["message"] == {"type": "missing"}
    assert current["updated_at"] >= previous["updated_at"]
    assert "secret" not in path.read_text()
    assert calls == ["GET", "POST", "GET", "POST"]


def test_diagnostic_masks_before_truncation_and_omits_oversized_input():
    token = "private-secret-value-1234"
    assert token[:8] not in DanmakuSender._diagnostic_preview("词" * 236 + token, [token])
    assert DanmakuSender._diagnostic_preview("字" * 17000 + token, [token]) == "[过长提示，已省略]"
    assert DanmakuSender._diagnostic_preview("priva\x00te-secret", ["private-secret"]) == "[已脱敏]"
