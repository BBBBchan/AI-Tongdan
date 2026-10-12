"""Authenticated Bilibili sends for manual actions and explicitly enabled trials.

Protocol verified against official web assets on 2026-10-10:
https://live.bilibili.com/732
https://s1.hdslb.com/bfs/static/bilibili-live-player/room-player.5c8ac866.prod.min.js
https://s1.hdslb.com/bfs/static/blive/blfe-live-room/static/js/app.3f7250563d6495ab0c32.js
The player posts /msg/send with Cookie + bili_jct CSRF and WBI-signed query;
the room app defaults to 20 UTF-16 code units unless account privileges differ.
Selected source evidence is in test-results/danmaku-send/protocol-evidence.json.
Only a deliberate send action or a bounded, user-enabled trial invokes send().
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlparse

import httpx

from .bilibili import API, USER_AGENT, WBI_INDICES, parse_room_id, wbi_sign

MAX_DANMAKU_LENGTH = 20
_MAX_COOKIE_LENGTH = 16384
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"


class DanmakuSendError(RuntimeError):
    """Only fixed, safe messages may cross the server boundary."""

    def __init__(self, message: str, status_code: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _cookie_parts(raw: str) -> tuple[str, str]:
    if not isinstance(raw, str) or len(raw) > _MAX_COOKIE_LENGTH:
        raise DanmakuSendError("Cookie 格式无效或过长，请粘贴自己的 Bilibili 请求 Cookie。", code="invalid_cookie")
    if any(ord(char) < 32 or ord(char) > 126 for char in raw):
        raise DanmakuSendError("Cookie 不得包含换行、控制字符或非 ASCII 字符。", code="invalid_cookie")
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    parsed = SimpleCookie()
    try:
        parsed.load(raw)
    except CookieError:
        raise DanmakuSendError("Cookie 格式无效，请重新粘贴完整请求 Cookie。", code="invalid_cookie") from None
    if not parsed.get("SESSDATA") or not parsed["SESSDATA"].value or not parsed.get("bili_jct") or not parsed["bili_jct"].value:
        raise DanmakuSendError("请先在 Bilibili 登录，再提供含 SESSDATA 和 bili_jct 的本人 Cookie。", 401, "login_required")
    normalized = "; ".join(f"{name}={item.coded_value}" for name, item in parsed.items())
    return normalized, parsed["bili_jct"].value


class DanmakuCredentials:
    """Private local credentials; status() intentionally contains no tokens.

    A saved local value overrides BILIBILI_COOKIE. clear() removes only the
    local file, so an environment value becomes active again if configured.
    """

    def __init__(self, data_dir: Path, env_cookie: str = ""):
        self.path = Path(data_dir) / "bilibili_send_credentials.private.json"
        self._env_cookie = env_cookie

    def _read(self) -> tuple[str, str]:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return self._env_cookie, "environment" if self._env_cookie else "none"
        except OSError:
            raise DanmakuSendError("本地弹幕登录配置无法读取，请重新保存 Cookie。", 503, "credentials_unavailable") from None
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                payload = json.loads(stream.read(_MAX_COOKIE_LENGTH * 2 + 1))
            value = payload["cookie"]
            if not isinstance(value, str):
                raise ValueError()
            return value, "local"
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            raise DanmakuSendError("本地弹幕登录配置无效，请重新保存 Cookie。", 503, "credentials_unavailable") from None

    def status(self) -> dict:
        try:
            raw, source = self._read()
            if raw:
                _cookie_parts(raw)
            configured = bool(raw)
        except DanmakuSendError:
            configured, source = False, "local" if self.path.exists() else "environment" if self._env_cookie else "none"
        return {"configured": configured, "source": source, "max_length": MAX_DANMAKU_LENGTH}

    def save(self, cookie: str) -> dict:
        normalized, _ = _cookie_parts(cookie)  # Reject first; never overwrite a valid value on error.
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".bilibili-send-", dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"cookie": normalized}, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise DanmakuSendError("无法保存本地弹幕登录配置，请检查数据目录。", 503, "credentials_unavailable") from None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return self.status()

    def clear(self) -> dict:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            raise DanmakuSendError("无法清除本地弹幕登录配置，请检查数据目录。", 503, "credentials_unavailable") from None
        return self.status()

    def _authenticated_cookie(self) -> tuple[str, str]:
        raw, _ = self._read()
        if not raw:
            raise DanmakuSendError("请先在 Bilibili 登录，并配置本人 Cookie 后再发送弹幕。", 401, "login_required")
        return _cookie_parts(raw)


class DanmakuSender:
    def __init__(self, credentials: DanmakuCredentials, client: httpx.AsyncClient | None = None):
        self.credentials = credentials
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False)
        self._send_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _payload(response: httpx.Response) -> dict:
        if response.status_code in (401, 403):
            raise DanmakuSendError("Bilibili 拒绝了请求，请检查登录状态或到官方直播间完成验证。", 401, "login_required")
        if response.status_code == 429:
            raise DanmakuSendError("Bilibili 暂时限制发送频率，请稍后再试。", 429, "rate_limited")
        if response.status_code != 200:
            raise DanmakuSendError("Bilibili 接口暂不可用，请稍后查看直播间再决定是否重发。", 502, "platform_unavailable")
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            raise DanmakuSendError("Bilibili 响应格式异常，请先查看直播间确认发送结果。", 502, "invalid_response") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("code"), int) or isinstance(payload["code"], bool):
            raise DanmakuSendError("Bilibili 响应格式异常，请先查看直播间确认发送结果。", 502, "invalid_response")
        code = payload["code"]
        if code in (-101, -111):
            raise DanmakuSendError("Bilibili 登录或 CSRF 已失效，请更新本人 Cookie。", 401, "login_required")
        if code == 1990000:
            raise DanmakuSendError("Bilibili 要求验证码，请到官方直播间完成验证后再试。", 409, "verification_required")
        if code != 0:
            raise DanmakuSendError(f"Bilibili 未受理弹幕（错误码 {code}），请到官方直播间检查账号、禁言或发送限制。", 409, "platform_rejected")
        return payload

    async def send(self, room_id: int, text: str, *, before_post: Callable[[], None] | None = None) -> dict:
        try:
            room_id = parse_room_id(room_id)
        except ValueError:
            raise DanmakuSendError("请选择有效的 Bilibili 直播间。") from None
        if not isinstance(text, str) or any(ord(char) < 32 or ord(char) == 127 for char in text):
            raise DanmakuSendError("弹幕必须是单行文字，不能包含控制字符。")
        text = text.strip()
        try:
            length = len(text.encode("utf-16-le")) // 2
        except UnicodeError:
            raise DanmakuSendError("弹幕包含无效字符。") from None
        if not text or length > MAX_DANMAKU_LENGTH:
            raise DanmakuSendError(f"弹幕请输入 1–{MAX_DANMAKU_LENGTH} 字；部分表情按 2 字计。")
        cookie, csrf = self.credentials._authenticated_cookie()
        headers = {"User-Agent": USER_AGENT, "Referer": f"https://live.bilibili.com/{room_id}",
                   "Origin": "https://live.bilibili.com", "Cookie": cookie}
        async with self._send_lock:
            try:
                response = await self._client.get(_NAV_URL, headers=headers, follow_redirects=False)
                payload = self._payload(response)
                account = payload.get("data")
                if not isinstance(account, dict) or account.get("isLogin") is not True:
                    raise DanmakuSendError("Bilibili Cookie 未登录或已失效，请更新本人 Cookie。", 401, "login_required")
                images = account.get("wbi_img") or {}
                if not isinstance(images, dict):
                    images = {}
                raw_key = "".join(urlparse(str(images.get(name, ""))).path.rsplit("/", 1)[-1].split(".", 1)[0]
                                  for name in ("img_url", "sub_url"))
                if len(raw_key) < 64:
                    raise DanmakuSendError("Bilibili 未返回有效发送签名，请稍后再试。", 502, "signing_unavailable")
                key = "".join(raw_key[index] for index in WBI_INDICES)
                query = wbi_sign({"web_location": "444.8"}, key)
                body = {"msg": text, "roomid": str(room_id), "color": "16777215", "fontsize": "25",
                        "mode": "1", "rnd": str(int(time.time())), "data_extend": "{}",
                        "csrf": csrf, "csrf_token": csrf}
                # The official web player uses FormData. Never retry a POST:
                # even a timeout can mean it was accepted and the reply was lost.
                # A trial may have stopped or expired while login was checked.
                # This synchronous guard runs after every preceding await and
                # immediately before the external send, without a yield in between.
                if before_post is not None:
                    before_post()
                response = await self._client.post(API + "/msg/send", params=query, headers=headers,
                    files={name: (None, value) for name, value in body.items()}, follow_redirects=False)
                payload = self._payload(response)
            except httpx.TimeoutException:
                raise DanmakuSendError("请求超时，发送结果暂不确定；请先查看直播间，避免重复发送。", 504, "send_unknown") from None
            except httpx.HTTPError:
                raise DanmakuSendError("网络请求中断，发送结果暂不确定；请先查看直播间，避免重复发送。", 502, "send_unknown") from None
        # Some platform rejection paths use code=0 but supply an explanatory msg.
        # Do not return raw upstream messages: they can contain echoed credentials.
        for name in ("msg", "message"):
            message = str(payload.get(name) or "").strip().lower()
            if message not in ("", "ok", "success", "成功", "发送成功"):
                raise DanmakuSendError("Bilibili 未确认受理该弹幕，请到官方直播间查看发送限制。", 409, "platform_rejected")
        return {"sent": True, "room_id": room_id, "text": text, "platform_code": 0,
                "message": "Bilibili 已受理发送请求"}
