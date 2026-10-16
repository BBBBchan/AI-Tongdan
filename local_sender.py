#!/usr/bin/env python3
"""AI 同担本机发送助手。Cookie stays on this computer; never retry Bilibili POSTs."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable

import httpx

from app.danmaku_send import DanmakuCredentials, DanmakuSender, DanmakuSendError, _cookie_parts

DEFAULT_DEMO = "http://127.0.0.1:7862"
LOCAL_CODES = frozenset({"local_authorization_denied", "local_authorization_expired", "duplicate_job",
                         "sender_busy", "storage_unavailable", "local_protocol_error", "local_sender_error"})
SEND_CODES = frozenset({"invalid_request", "invalid_cookie", "login_required", "credentials_unavailable",
                       "rate_limited", "verification_required", "wealth_level_required", "level_required",
                       "room_muted", "platform_rejected", "platform_unavailable", "invalid_response",
                       "signing_unavailable", "send_unknown", "trial_stopped", "trial_expired"})
RECEIPT_KINDS = SEND_CODES | LOCAL_CODES | {"not_checked", "success", "unknown_message", "platform_f_notice",
                                         "cancelled", "local_error"}


class LocalSenderError(RuntimeError):
    """Fixed local errors only; never include requests, server bodies or tokens."""

    def __init__(self, message: str, *, code: str = "local_protocol_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class PairingLost(LocalSenderError):
    def __init__(self):
        super().__init__("配对已失效。请在网页重新生成配对码，再启动助手。")


def validate_demo_url(value: str) -> str:
    # Only these explicit loopback spellings; no credentials, remote host, path,
    # escaped host, query, fragment or redirect is accepted.
    match = re.fullmatch(r"http://(127\.0\.0\.1|localhost|\[::1\])(?::([0-9]{1,5}))?/?", value)
    if not match or (match[2] is not None and not 1 <= int(match[2]) <= 65535):
        raise LocalSenderError("Demo 地址必须是本机 http://127.0.0.1:端口（也支持 localhost 或 [::1]），不能带路径或登录参数。")
    return value.rstrip("/")


def load_cookie_file(path: Path) -> str:
    try:
        with path.expanduser().open("r", encoding="utf-8-sig") as handle:
            raw = handle.read(16386)
    except (OSError, UnicodeError):
        raise LocalSenderError("无法读取本机 Cookie 文件，请先按说明导出自己的 B 站 Cookie。") from None
    if len(raw) > 16384:
        raise LocalSenderError("本机 Cookie 文件过长，请重新导出。")
    # Exporter adds a final newline. Internal control characters remain invalid.
    raw = raw.strip()
    try:
        normalized, _ = _cookie_parts(raw)
    except DanmakuSendError:
        raise LocalSenderError("本机 Cookie 格式无效，需要包含 SESSDATA 和 bili_jct；请重新导出。") from None
    return normalized


class LocalCredentials(DanmakuCredentials):
    """Use only the explicitly loaded local cookie, never a saved server config."""

    def _read(self) -> tuple[str, str]:
        return self._env_cookie, "environment"


class JobLedger:
    """Durable claim before a send; no text, cookie, pairing token or account."""

    def __init__(self, private_dir: Path):
        self.directory = private_dir / "jobs"

    def claim(self, job_id: str) -> bool:
        path = self.directory / (hashlib.sha256(job_id.encode()).hexdigest() + ".json")
        try:
            self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        except OSError:
            raise LocalSenderError("无法记录本机发送任务；本条未发送，请检查助手目录权限。", code="storage_unavailable") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                json.dump({"job_id": job_id, "claimed_at": time.time()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # Leave even a partial claim: failure must never permit a resend.
            raise LocalSenderError("本机任务记录保存失败；本条未发送。", code="storage_unavailable") from None
        return True


def safe_receipt(receipt) -> dict | None:
    if not isinstance(receipt, dict):
        return None
    result = {}
    for name, allowed in (("phase", {"validation", "nav", "send"}),
                          ("outcome", {"pending", "accepted", "rejected", "unknown"}),
                          ("message_kind", RECEIPT_KINDS)):
        value = receipt.get(name)
        if isinstance(value, str) and value in allowed:
            result[name] = value
    for name, low, high in (("http_status", 100, 599), ("platform_code", -(2**31), 2**31 - 1)):
        value = receipt.get(name)
        if value is None:
            result[name] = None
        elif type(value) is int and low <= value <= high:
            result[name] = value
    timestamp = receipt.get("updated_at")
    if type(timestamp) in (int, float) and math.isfinite(timestamp) and 0 <= timestamp <= 4102444800:
        result["updated_at"] = timestamp
    return result


def validate_job(job) -> dict:
    if not isinstance(job, dict):
        raise LocalSenderError("Demo 返回的任务格式异常；本条未发送。")
    job_id, room_id, text = job.get("id"), job.get("room_id"), job.get("text")
    if (not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id)
            or type(room_id) is not int or not 1 <= room_id <= 10**12
            or not isinstance(text, str) or len(text) > 200):
        raise LocalSenderError("Demo 返回的任务格式异常；本条未发送。")
    return {"id": job_id, "room_id": room_id, "text": text}


class LocalSender:
    def __init__(self, demo: str, sender: DanmakuSender, private_dir: Path, *,
                 demo_client: httpx.AsyncClient | None = None,
                 output: Callable[[str], None] = print, monotonic: Callable[[], float] = time.monotonic):
        self.demo = validate_demo_url(demo)
        self.sender = sender
        self.ledger = JobLedger(private_dir)
        self.client = demo_client or httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False)
        self._owns_client = demo_client is None
        self.output = output
        self.monotonic = monotonic
        self.token = ""
        self.poll_interval = 1.0
        self.stop_requested = asyncio.Event()
        self._job_lock = asyncio.Lock()
        self._active_job_id = None

    async def close(self):
        self.stop_requested.set()
        self.token = ""
        if self._owns_client:
            await self.client.aclose()
        await self.sender.close()

    async def _request(self, endpoint: str, data: dict, *, authenticated: bool = True) -> dict:
        headers = {}
        if authenticated:
            if not self.token:
                raise PairingLost()
            headers["Authorization"] = "Bearer " + self.token
        try:
            response = await self.client.post(self.demo + endpoint, json=data, headers=headers,
                                              follow_redirects=False)
        except httpx.HTTPError:
            raise LocalSenderError("本机与 Demo 暂时断开，请检查端口转发。", code="demo_unavailable") from None
        if response.status_code == 401:
            self.stop_requested.set()
            self.token = ""
            raise PairingLost()
        if response.status_code != 200:
            raise LocalSenderError("Demo 未接受该请求，请在网页检查连接状态。", status=response.status_code)
        try:
            if len(response.content) > 65536:
                raise ValueError()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise LocalSenderError("Demo 返回格式异常；请检查助手与网页是否为同一版本。") from None
        return payload

    async def connect(self, code: str):
        if not re.fullmatch(r"[0-9]{8}", code):
            raise LocalSenderError("请输入网页显示的 8 位数字配对码。")
        payload = await self._request("/api/danmaku/local/connect", {"code": code}, authenticated=False)
        token = payload.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{16,1024}", token):
            raise LocalSenderError("配对响应无效，请在网页重新生成配对码。")
        self.token = token
        interval = payload.get("poll_interval", 1)
        if type(interval) in (int, float) and math.isfinite(interval):
            self.poll_interval = min(5.0, max(0.25, interval))
        self.output("本机助手已连接。Cookie 留在这台电脑；请在网页发送弹幕，保持此窗口打开。")

    async def report(self, job_id: str, outcome: str, code: str, receipt=None) -> bool:
        body = {"job_id": job_id, "outcome": outcome,
                "code": code if code in SEND_CODES | LOCAL_CODES | {"success"} else "local_sender_error",
                "receipt": safe_receipt(receipt)}
        for attempt in range(3):
            try:
                await self._request("/api/danmaku/local/result", body)
                return True
            except PairingLost:
                raise
            except LocalSenderError as exc:
                # Only reporting may retry, with the same job ID. The Bilibili
                # operation is never entered again, even when the report is lost.
                if exc.status is not None and exc.status < 500 and exc.status != 429:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        self.output("发送结果未能同步到网页。请核对官方直播间；助手不会重发这条弹幕。")
        return False

    async def process_job(self, raw_job) -> dict:
        """One delivered job; directly injectable for ASGI/MockTransport tests."""
        job = validate_job(raw_job)
        job_id = job["id"]
        try:
            claimed = self.ledger.claim(job_id)
        except LocalSenderError:
            outcome, code = "rejected", "storage_unavailable"
            await self.report(job_id, outcome, code)
            return {"outcome": outcome, "code": code}
        if not claimed:
            if self._active_job_id == job_id:
                # A duplicate delivery must not replace the pending result.
                return {"outcome": "unknown", "code": "duplicate_job"}
            await self.report(job_id, "unknown", "duplicate_job")
            self.output("已跳过本机处理过的任务，避免重复发送。")
            return {"outcome": "unknown", "code": "duplicate_job"}
        if self.stop_requested.is_set() or self._job_lock.locked():
            code = "local_authorization_denied" if self.stop_requested.is_set() else "sender_busy"
            await self.report(job_id, "rejected", code)
            return {"outcome": "rejected", "code": code}
        async with self._job_lock:
            self._active_job_id = job_id
            deadline = None

            async def authorize():
                nonlocal deadline
                if self.stop_requested.is_set():
                    raise DanmakuSendError("本机助手已停止，本条未发送。", 409, "local_authorization_denied")
                started = self.monotonic()
                try:
                    permit = await self._request("/api/danmaku/local/authorize", {"job_id": job_id})
                except PairingLost:
                    raise
                except LocalSenderError:
                    raise DanmakuSendError("本条发送授权未确认，已取消发送。", 409, "local_authorization_denied") from None
                valid_for = permit.get("valid_for_seconds")
                if (permit.get("authorized") is not True or type(valid_for) not in (int, float)
                        or not math.isfinite(valid_for) or valid_for <= 0):
                    raise DanmakuSendError("本条发送授权已失效，已取消发送。", 409, "local_authorization_denied")
                # Start at the request start, so transport delay can only shorten
                # the backend's three-second permit, never extend it.
                deadline = started + min(3.0, valid_for)

            def guard():
                if self.stop_requested.is_set() or not self.token:
                    raise DanmakuSendError("本机助手已停止，本条未发送。", 409, "local_authorization_denied")
                if deadline is None or self.monotonic() >= deadline:
                    raise DanmakuSendError("本条发送授权已过期，已取消发送。", 409, "local_authorization_expired")

            try:
                await self.sender.send(job["room_id"], job["text"], authorize_post=authorize, before_post=guard)
                outcome, code = "accepted", "success"
                self.output("B 站接口已受理 1 条弹幕；实际展示请以直播间为准。")
            except PairingLost:
                raise
            except DanmakuSendError as exc:
                code = exc.code if exc.code in SEND_CODES | LOCAL_CODES else "local_sender_error"
                outcome = "unknown" if code == "send_unknown" else "rejected"
                self.output("本条发送结果待确认，助手不会重发。" if outcome == "unknown" else "本条未完成发送，请查看网页状态。")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Unexpected local failures after a POST may have lost the reply.
                # Do not echo exception details or attempt the operation again.
                outcome, code = "unknown", "local_sender_error"
                self.output("本条发生异常，结果待确认；助手不会重发。")
            receipt = safe_receipt(self.sender.last_receipt)
            await self.report(job_id, outcome, code, receipt)
            self._active_job_id = None
            return {"outcome": outcome, "code": code, "receipt": receipt}

    async def run(self):
        active = None
        busy_report = None
        disconnected = False
        try:
            while not self.stop_requested.is_set():
                if active is not None and active.done():
                    await active
                    active = None
                if busy_report is not None and busy_report.done():
                    await busy_report
                    busy_report = None
                try:
                    payload = await self._request("/api/danmaku/local/poll", {})
                    if disconnected:
                        self.output("与 Demo 的连接已恢复。")
                        disconnected = False
                    job = payload.get("job")
                    if job is not None:
                        if active is None:
                            active = asyncio.create_task(self.process_job(job))
                        else:
                            # Keep heartbeats running during nav and send. The
                            # broker should not deliver overlapping jobs, but an
                            # unexpected one must still never create a second POST.
                            extra = validate_job(job)
                            if extra["id"] == self._active_job_id:
                                pass
                            elif busy_report is None:
                                busy_report = asyncio.create_task(self.process_job(extra))
                            else:
                                # Bound task count even if the peer misbehaves.
                                # A claimed but unreported job expires at the Demo.
                                self.ledger.claim(extra["id"])
                except PairingLost:
                    raise
                except LocalSenderError:
                    if not disconnected:
                        self.output("与 Demo 暂时断开，正在等待恢复；不会重放已经处理过的任务。")
                        disconnected = True
                try:
                    await asyncio.wait_for(self.stop_requested.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.stop_requested.set()
            for task in (active, busy_report):
                if task is not None:
                    if not task.done():
                        task.cancel()
                    with suppress(asyncio.CancelledError, PairingLost, LocalSenderError):
                        await task


async def run_local(args, code: str):
    demo = validate_demo_url(args.demo)
    private_dir = Path(args.private_dir).expanduser()
    cookie = load_cookie_file(Path(args.cookie_file))
    sender = DanmakuSender(LocalCredentials(private_dir, env_cookie=cookie))
    helper = LocalSender(demo, sender, private_dir)
    try:
        await helper.connect(code)
        await helper.run()
    finally:
        await helper.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI 同担本机发送助手：Cookie 只在你的电脑使用")
    parser.add_argument("--demo", default=DEFAULT_DEMO, help=f"本机转发后的 Demo 根地址（默认 {DEFAULT_DEMO}）")
    parser.add_argument("--cookie-file", default="bilibili_cookie.txt", help="本机 Cookie 文本文件")
    parser.add_argument("--private-dir", default=".aitongdan-local", help="本机去重记录与脱敏诊断目录")
    args = parser.parse_args(argv)
    try:
        validate_demo_url(args.demo)
        # Validate the file before asking for a short-lived pairing code.
        load_cookie_file(Path(args.cookie_file))
        code = getpass.getpass("请输入网页上的 8 位配对码（输入不显示）：").strip()
        asyncio.run(run_local(args, code))
    except KeyboardInterrupt:
        print("本机助手已停止。已经发出的请求可能仍在处理，请在直播间核对结果。")
        return 0
    except (LocalSenderError, PairingLost) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("本机助手未能运行，请检查部署说明；没有输出登录信息。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
