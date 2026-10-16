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
from collections.abc import Awaitable, Callable
from http.cookies import CookieError, SimpleCookie
import json
import os
import re
from pathlib import Path
import tempfile
import time
import unicodedata
from urllib.parse import quote, unquote, urlparse

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
        self.last_receipt: dict | None = None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _receipt(self, **fields) -> None:
        # Only call with fixed metadata below. Never store upstream message/data,
        # submitted text, credentials or account details in this public receipt.
        self.last_receipt = {**(self.last_receipt or {}), **fields, "updated_at": time.time()}

    @staticmethod
    def _diagnostic_preview(value: str, redaction_tokens: list[str]) -> str:
        # Omit oversized values entirely rather than cutting through a secret
        # before redaction. Mask the full bounded string before the 240-char view.
        if len(value) > 16384 or len(value.encode("utf-8", errors="replace")) > 32768:
            return "[过长提示，已省略]"
        tokens = set()
        for token in redaction_tokens:
            if token and len(token) <= 32768:
                tokens.update((token, unquote(token), quote(token, safe=""),
                               json.dumps(token, ensure_ascii=True)[1:-1]))
        tokens.discard("")
        ordered_tokens = sorted(tokens, key=len, reverse=True)
        for token in ordered_tokens:
            value = value.replace(token, "[已脱敏]")
        value = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
        # Repeat after removing control/format characters that could split a token.
        for token in ordered_tokens:
            value = value.replace(token, "[已脱敏]")
        value = re.sub(r"(?i)(?:[a-z][a-z0-9+.-]{1,15}://|(?<!\w)//|www\.)[^\s\"'<>]+"
                       r"|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s\"'<>]*)?", "[已脱敏]", value)
        value = re.sub(
            r"(?i)(?<![a-z0-9_])(?:sessdata|bili_jct|csrf(?:_token)?|dedeuserid(?:__ckmd5)?|"
            r"uid|uname|user(?:_?(?:id|name))?|mid|(?:access_|refresh_)?token|cookie|authorization|"
            r"nickname|用户名|昵称|账号|用户id|用户uid|密码)[\"']?(?:\s*[:=：]\s*|\s+)"
            r"(?:\"[^\"]*\"|'[^']*'|[^,，;；}\r\n]+)", "[已脱敏]", value)
        value = re.sub(r"\d{5,}|[a-zA-Z0-9][a-zA-Z0-9._~+/%=-]{15,}", "[已脱敏]", value)
        return value[:240]

    def _write_diagnostic(self, response: httpx.Response, redaction_tokens: list[str]) -> None:
        """Best-effort private metadata; never changes the send result or retries."""
        temporary = None
        try:
            parse_status = "parsed"
            if len(response.content) > 262144:
                payload, parse_status = {}, "oversized_response"
            else:
                try:
                    payload = response.json()
                except (ValueError, UnicodeError):
                    payload, parse_status = {}, "invalid_json"
            source = payload if isinstance(payload, dict) else {}

            def field_type(name: str) -> str:
                if name not in source:
                    return "missing"
                value = source[name]
                if value is None:
                    return "null"
                if isinstance(value, bool):
                    return "boolean"
                if isinstance(value, str):
                    return "string"
                if isinstance(value, (int, float)):
                    return "number"
                return "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "other"

            code = source.get("code")
            diagnostic = {"updated_at": time.time(), "http_status": response.status_code,
                          "parse_status": parse_status,
                          "code": code if isinstance(code, int) and not isinstance(code, bool) else None,
                          "has_data": "data" in source, "data_type": field_type("data")}
            for name in ("msg", "message"):
                entry = {"type": field_type(name)}
                # Never traverse structured messages or retain data/account fields.
                if isinstance(source.get(name), str):
                    entry["preview"] = self._diagnostic_preview(source[name], redaction_tokens)
                diagnostic[name] = entry
            directory = self.credentials.path.parent
            directory.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".bilibili-send-diagnostic-", dir=directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(diagnostic, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "bilibili_send_diagnostic.private.json")
            temporary = None
        except Exception:
            # Diagnostics must never interrupt a completed POST or invite a retry.
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @staticmethod
    def _message_kind(payload: dict) -> str:
        values = [payload.get(name) for name in ("msg", "message")]
        # Inspect strings only; malformed values must not become success markers.
        messages = [value.strip().lower() for value in values if isinstance(value, str)]
        combined = "\n".join(messages)
        failed_prefix = any(re.match(r"f\s*[:：]", message) for message in messages)
        explicit_rejection = failed_prefix or payload.get("code") not in (None, 0)
        patterns = (
            ("login_required", r"未登录|尚未登录|登录.{0,8}(?:失效|过期)|(?:请先|重新).{0,4}登录|csrf.{0,8}(?:失效|错误|校验失败)"),
            ("verification_required", r"验证码|风控|安全验证|风险.{0,8}(?:验证|校验)|captcha|risk_control" if explicit_rejection
             else r"(?:需要|要求|请|完成|输入).{0,12}(?:验证码|安全验证)|(?:验证码|安全验证|风控|风险校验).{0,12}(?:失败|错误|未通过|不通过|拒绝|异常|限制|拦截)|captcha required"),
            ("wealth_level_required", r"荣耀.{0,20}(?:不足|不够|低于|至少|未达到|要求|限制|禁言|才可|才能)|(?:不足|不够|低于|至少|未达到|要求).{0,20}荣耀"),
            ("level_required", r"等级.{0,20}(?:不足|不够|低于|至少|未达到|要求|限制|禁言|才可|才能)|(?:低于|至少|未达到|要求).{0,12}等级"),
            ("room_muted", r"禁言|禁止发言|禁止发送弹幕" if explicit_rejection
             else r"(?:被|处于|开启|全员|全体).{0,8}禁言|禁言中|禁止发言|禁止发送弹幕"),
            ("rate_limited", r"频率|频繁|太快|发送.{0,4}快" if explicit_rejection
             else r"频繁|太快|频率.{0,8}(?:限制|过快|过高)|发送.{0,4}(?:过快|太快)"),
        )
        for kind, pattern in patterns:
            if re.search(pattern, combined):
                return kind
        if failed_prefix:
            return "platform_rejected"
        if payload.get("code") == 0 and any(isinstance(value, str) and value.strip() == "f" for value in values):
            return "platform_f_notice"
        success = {"", "0", "ok", "success", "成功", "发送成功"}
        if all(value is None or (value == 0 and not isinstance(value, bool))
               or (isinstance(value, str) and value.strip().lower() in success) for value in values):
            return "success"
        return "unknown_message"

    @staticmethod
    def _message_error(kind: str) -> DanmakuSendError:
        errors = {
            "login_required": ("Bilibili 登录或 CSRF 已失效，请更新本人 Cookie。", 401),
            "verification_required": ("Bilibili 要求验证，请到官方直播间完成验证后再试。", 409),
            "wealth_level_required": ("Bilibili 提示未满足直播间的荣耀等级门槛；请在官方直播间核对本人账号和荣耀等级。", 409),
            "level_required": ("Bilibili 提示未满足直播间的等级门槛；请在官方直播间核对具体等级要求。", 409),
            "room_muted": ("Bilibili 提示当前账号或直播间处于禁言限制，请到官方直播间查看。", 409),
            "rate_limited": ("Bilibili 暂时限制发送频率，请稍后再试。", 429),
            "platform_rejected": ("Bilibili 明确拒绝了该弹幕，请到官方直播间查看发送限制。", 409),
        }
        message, status = errors[kind]
        return DanmakuSendError(message, status, kind)

    def _payload(self, response: httpx.Response) -> dict:
        self._receipt(http_status=response.status_code)
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
        self._receipt(platform_code=code)
        if code in (-101, -111):
            raise DanmakuSendError("Bilibili 登录或 CSRF 已失效，请更新本人 Cookie。", 401, "login_required")
        if code == 1990000:
            raise DanmakuSendError("Bilibili 要求验证码，请到官方直播间完成验证后再试。", 409, "verification_required")
        if code != 0:
            kind = self._message_kind(payload)
            if kind not in ("success", "unknown_message"):
                raise self._message_error(kind)
            raise DanmakuSendError(f"Bilibili 未受理弹幕（错误码 {code}），请到官方直播间检查账号、禁言或发送限制。", 409, "platform_rejected")
        return payload

    async def send(self, room_id: int, text: str, *, before_post: Callable[[], None] | None = None,
                   authorize_post: Callable[[], Awaitable[None]] | None = None) -> dict:
        async with self._send_lock:
            self.last_receipt = None
            self._receipt(phase="validation", outcome="pending", message_kind="not_checked",
                          platform_code=None, http_status=None)
            try:
                result = await self._send(room_id, text, before_post=before_post, authorize_post=authorize_post)
            except DanmakuSendError as exc:
                safe_kinds = {"invalid_request", "invalid_cookie", "login_required", "credentials_unavailable",
                              "rate_limited", "verification_required", "wealth_level_required", "level_required",
                              "room_muted", "platform_rejected", "platform_unavailable", "invalid_response",
                              "signing_unavailable", "send_unknown", "trial_stopped", "trial_expired"}
                kind = exc.code if exc.code in safe_kinds else "local_error"
                notice_kind = (self.last_receipt or {}).get("message_kind")
                if notice_kind in {"unknown_message", "platform_f_notice"}:
                    kind = notice_kind
                uncertain = (self.last_receipt or {}).get("phase") == "send" and exc.code in {
                    "send_unknown", "invalid_response", "platform_unavailable"}
                self._receipt(outcome="unknown" if uncertain else "rejected", message_kind=kind)
                if uncertain and exc.code != "send_unknown":
                    raise DanmakuSendError("Bilibili 返回异常，发送结果暂不确定；请先在官方直播间核对，避免重复发送。",
                                           exc.status_code, "send_unknown") from None
                raise
            except asyncio.CancelledError:
                self._receipt(outcome="unknown" if (self.last_receipt or {}).get("phase") == "send" else "rejected",
                              message_kind="cancelled")
                raise
            self._receipt(outcome="accepted", message_kind="success")
            return result

    async def _send(self, room_id: int, text: str, *, before_post: Callable[[], None] | None = None,
                    authorize_post: Callable[[], Awaitable[None]] | None = None) -> dict:
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
        self._receipt(phase="nav", http_status=None, platform_code=None)
        try:
            response = await self._client.get(_NAV_URL, headers=headers, follow_redirects=False)
            payload = self._payload(response)
            account = payload.get("data")
            if not isinstance(account, dict) or account.get("isLogin") is not True:
                raise DanmakuSendError("Bilibili Cookie 未登录或已失效，请更新本人 Cookie。", 401, "login_required")
            # Actual credential/account values are used only in memory to mask
            # echoed hints. Submitted text is also masked if the platform echoes it.
            cookie_jar = SimpleCookie()
            cookie_jar.load(cookie)
            redaction_tokens = [cookie, csrf, text]
            redaction_tokens.extend(item.value for item in cookie_jar.values())
            for name in ("mid", "uname"):
                value = account.get(name)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    redaction_tokens.append(str(value))
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
            if authorize_post is not None:
                await authorize_post()
            if before_post is not None:
                before_post()
            self._receipt(phase="send", http_status=None, platform_code=None)
            response = await self._client.post(API + "/msg/send", params=query, headers=headers,
                files={name: (None, value) for name, value in body.items()}, follow_redirects=False)
            self._write_diagnostic(response, redaction_tokens)
            payload = self._payload(response)
        except httpx.TimeoutException:
            raise DanmakuSendError("请求超时，发送结果暂不确定；请先查看直播间，避免重复发送。", 504, "send_unknown") from None
        except httpx.HTTPError:
            raise DanmakuSendError("网络请求中断，发送结果暂不确定；请先查看直播间，避免重复发送。", 502, "send_unknown") from None
        # Official web code accepts code=0, including message="0". Preserve
        # explicit rejection signals; unfamiliar notices are inconclusive and
        # must never trigger an automatic retry.
        kind = self._message_kind(payload)
        if kind == "platform_f_notice":
            self._receipt(message_kind=kind)
            raise DanmakuSendError("Bilibili 返回提示 f，弹幕尚未确认送达；平台未说明具体原因，请勿连续重发。", 409, "send_unknown")
        if kind == "unknown_message":
            self._receipt(message_kind=kind)
            raise DanmakuSendError("Bilibili 返回了未识别的发送提示，结果暂不确定；请先在官方直播间核对，避免重复发送。", 409, "send_unknown")
        if kind != "success":
            raise self._message_error(kind)
        return {"sent": True, "room_id": room_id, "text": text, "platform_code": 0,
                "message": "Bilibili 已受理发送请求"}
