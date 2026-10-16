"""In-memory, one-delivery jobs for a separately paired local sender.

The broker never handles Bilibili cookies. A helper must claim and authorize a
job before its external POST; lost or revoked jobs are never queued again.
"""
import asyncio
from dataclasses import dataclass
import secrets
import time
from typing import Callable
import uuid

from .danmaku_send import DanmakuSendError, MAX_DANMAKU_LENGTH


_ERRORS = {
    "login_required": ("本机助手的 Bilibili 登录已失效，请在自己电脑上更新 Cookie。", 401),
    "invalid_cookie": ("本机 Cookie 格式无效，请在自己电脑上重新配置。", 400),
    "credentials_unavailable": ("本机助手无法读取登录配置，请在自己电脑上检查。", 503),
    "wealth_level_required": ("Bilibili 提示未满足直播间的荣耀等级要求。", 409),
    "level_required": ("Bilibili 提示未满足直播间的等级要求。", 409),
    "room_muted": ("Bilibili 提示账号或直播间存在禁言限制。", 409),
    "rate_limited": ("Bilibili 暂时限制发送频率，请勿连续重发。", 429),
    "verification_required": ("请在自己电脑上的官方直播间完成 Bilibili 验证。", 409),
    "platform_rejected": ("Bilibili 未受理该弹幕，请到官方直播间核对。", 409),
    "invalid_request": ("本机助手未发送，请检查弹幕内容。", 400),
    "signing_unavailable": ("本机助手暂时无法取得发送签名，未发送。", 502),
    "trial_stopped": ("本次发送已取消，未发送。", 409),
    "trial_stale": ("候选依据已过期，未发送。", 409),
    "trial_expired": ("本次发送已过期，未发送。", 409),
    "local_authorization_denied": ("本机发送任务已撤销，未发送。", 409),
    "local_authorization_expired": ("本机发送授权已过期，未发送。", 409),
    "duplicate_job": ("本机助手已处理过此任务，不会重复发送。", 409),
    "sender_busy": ("本机助手正在处理其他任务，未发送。", 409),
    "storage_unavailable": ("本机助手无法保存防重记录，未发送。", 503),
    "local_protocol_error": ("本机助手返回了异常状态，请检查助手窗口。", 502),
    "local_error": ("本机助手处理失败，请检查自己电脑上的助手窗口。", 502),
}
_UNKNOWN_MESSAGE = "本机发送结果尚未确认，请先到官方直播间核对，避免重复发送。"
_RECEIPT_KINDS = set(_ERRORS) | {
    "success", "not_checked", "unknown_message", "platform_f_notice", "send_unknown",
    "invalid_response", "platform_unavailable", "cancelled", "local_disconnected", "local_job_expired",
}


def safe_receipt(raw, outcome, code):
    """Treat helper reports as untrusted: no account, cookie or raw text fields."""
    source = raw if isinstance(raw, dict) else {}
    kind = source.get("message_kind")
    phase = source.get("phase")
    def number(name, low, high):
        value = source.get(name)
        return value if type(value) is int and low <= value <= high else None
    return {"phase": phase if isinstance(phase, str) and phase in {"validation", "nav", "send"} else "send",
            "outcome": outcome, "message_kind": kind if isinstance(kind, str) and kind in _RECEIPT_KINDS else code,
            "http_status": number("http_status", 100, 599),
            "platform_code": number("platform_code", -1000000000, 1000000000),
            "updated_at": time.time()}


@dataclass
class _Job:
    id: str
    room_id: int
    text: str
    source: str
    guard: Callable
    deadline: float
    future: asyncio.Future
    delivered: bool = False
    authorized: bool = False


class LocalDanmakuBroker:
    pair_lifetime = 300.0
    heartbeat_timeout = 35.0
    job_timeout = 45.0
    authorization_lifetime = 3

    def __init__(self, *, send_guard=None, source_guard=None):
        self._send_guard = send_guard
        self._source_guard = source_guard
        self._pair_code = None
        self._pair_expires = 0.0
        self._pair_attempts = 0
        self._token = None
        self.paired = False
        self._heartbeat = 0.0
        self._job = None
        self._completed = []
        self.last_receipt = None

    @property
    def connected(self):
        if self._token and time.monotonic() - self._heartbeat > self.heartbeat_timeout:
            self.disconnect(revoke_pairing=False)
        return self._token is not None

    def pairing(self):
        self._pair_code = f"{secrets.randbelow(100000000):08d}"
        self._pair_expires = time.monotonic() + self.pair_lifetime
        self._pair_attempts = 0
        return {"code": self._pair_code, "expires_in": int(self.pair_lifetime)}

    def connect(self, code):
        valid = (self._pair_code is not None and time.monotonic() < self._pair_expires
                 and self._pair_attempts < 10 and isinstance(code, str)
                 and code.isascii() and len(code) == 8
                 and secrets.compare_digest(code, self._pair_code))
        if not valid:
            self._pair_attempts += 1
            if self._pair_attempts >= 10 or time.monotonic() >= self._pair_expires:
                self._pair_code = None
            raise DanmakuSendError("配对码无效或已过期，请在网页重新生成。", 401, "pairing_invalid")
        self.disconnect()
        self._token = secrets.token_urlsafe(32)
        self.paired = True
        self._heartbeat = time.monotonic()
        self._completed = []
        return {"token": self._token, "poll_interval": 1}

    def authenticate(self, token):
        if (not self.connected or not isinstance(token, str) or not token.isascii()
                or not secrets.compare_digest(token, self._token)):
            raise DanmakuSendError("本机助手连接已失效，请重新配对。", 401, "local_auth_required")

    def disconnect(self, *, revoke_pairing=True):
        self._token = None
        if revoke_pairing:
            self.paired = False
        self._pair_code = None
        self._pair_expires = 0.0
        self.cancel("local_disconnected")

    def cancel(self, reason="trial_stopped", *, source=None):
        job = self._job
        if job is None or (source is not None and job.source != source):
            return
        self._job = None
        uncertain = job.authorized
        self.last_receipt = safe_receipt({}, "unknown" if uncertain else "rejected", reason)
        self.last_receipt["phase"] = "send" if uncertain else "validation"
        if not job.future.done():
            if uncertain:
                error = DanmakuSendError(_UNKNOWN_MESSAGE, 409, "send_unknown")
            elif reason == "local_disconnected":
                error = DanmakuSendError("本机助手已断开，任务已取消且不会转由服务器发送。", 409, reason)
            elif reason == "local_job_expired":
                error = DanmakuSendError("本机发送任务已过期，未发送。", 409, reason)
            else:
                error = DanmakuSendError("本机发送任务已取消，未发送。", 409, "trial_stopped")
            job.future.set_exception(error)

    def _current_job(self, job_id):
        job = self._job
        if job is not None and time.monotonic() >= job.deadline:
            self.cancel("local_job_expired")
            job = None
        if job is None or not isinstance(job_id, str) or not job_id.isascii() or not secrets.compare_digest(job.id, job_id):
            raise DanmakuSendError("本机发送任务已失效，不能再次发送。", 409, "local_job_expired")
        return job

    def _require_source(self, source):
        if self._send_guard is not None:
            self._send_guard()
        if self._source_guard is not None:
            self._source_guard(source)

    def poll(self, token):
        self._require_source(self._job.source if self._job else "manual")
        self.authenticate(token)
        self._heartbeat = time.monotonic()
        job = self._job
        if job is not None and time.monotonic() >= job.deadline:
            self.cancel("local_job_expired")
            job = None
        if job is None or job.delivered:
            return {"job": None}
        job.delivered = True
        return {"job": {"id": job.id, "room_id": job.room_id, "text": job.text}}

    def authorize(self, token, job_id):
        self._require_source(self._job.source if self._job else "manual")
        self.authenticate(token)
        self._heartbeat = time.monotonic()
        job = self._current_job(job_id)
        if not job.delivered or job.authorized:
            raise DanmakuSendError("该任务尚未领取或已使用发送授权，不会重复授权。", 409, "local_authorization_denied")
        try:
            job.guard()
        except DanmakuSendError as exc:
            self._job = None
            self.last_receipt = safe_receipt({}, "rejected", exc.code if exc.code in _ERRORS else "trial_stopped")
            if not job.future.done():
                job.future.set_exception(exc)
            raise
        job.authorized = True
        self.last_receipt = safe_receipt({}, "pending", "not_checked")
        return {"authorized": True, "valid_for_seconds": self.authorization_lifetime}

    def result(self, token, job_id, outcome, code, receipt):
        self.authenticate(token)
        self._heartbeat = time.monotonic()
        if isinstance(job_id, str) and job_id in self._completed:
            return {"ok": True}
        job = self._current_job(job_id)
        if not job.delivered or outcome not in {"accepted", "rejected", "unknown"}:
            raise DanmakuSendError("本机助手回执格式无效。", 400, "local_protocol_error")
        if outcome == "accepted" and (not job.authorized or code != "success"):
            raise DanmakuSendError("任务未经发送授权，不能记为成功。", 409, "local_authorization_denied")
        if outcome == "unknown" or code == "send_unknown":
            outcome, code = "unknown", "send_unknown"
        elif outcome == "rejected":
            code = code if isinstance(code, str) and code in _ERRORS else "local_error"
        self.last_receipt = safe_receipt(receipt, outcome, code)
        self._job = None
        self._completed.append(job.id)
        del self._completed[:-64]
        if outcome == "accepted":
            job.future.set_result({"sent": True, "room_id": job.room_id, "text": job.text,
                                   "platform_code": 0, "message": "Bilibili 已受理本机发送请求"})
        elif outcome == "unknown":
            message = ("Bilibili 返回提示 f，弹幕尚未确认送达；平台未说明具体原因，请勿连续重发。"
                       if self.last_receipt["message_kind"] == "platform_f_notice" else _UNKNOWN_MESSAGE)
            job.future.set_exception(DanmakuSendError(message, 409, "send_unknown"))
        else:
            message, status = _ERRORS[code]
            job.future.set_exception(DanmakuSendError(message, status, code))
        return {"ok": True}

    async def send(self, room_id, text, *, guard, source):
        self._require_source(source)
        if not self.connected:
            raise DanmakuSendError("请先连接自己电脑上的发送助手；不会转由服务器发送。", 409, "local_disconnected")
        if self._job is not None:
            raise DanmakuSendError("本机助手正在处理上一条弹幕，请等待回执。", 409, "sender_busy")
        try:
            length = len(text.strip().encode("utf-16-le")) // 2
        except (AttributeError, UnicodeError):
            length = 0
        if not 1 <= length <= MAX_DANMAKU_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise DanmakuSendError("弹幕请输入 1–20 字的单行文字。", 400, "invalid_request")
        guard()
        future = asyncio.get_running_loop().create_future()
        job = _Job(uuid.uuid4().hex, room_id, text.strip(), source, guard,
                   time.monotonic() + self.job_timeout, future)
        self._job = job
        self.last_receipt = safe_receipt({}, "pending", "not_checked")
        self.last_receipt["phase"] = "validation"
        try:
            while not future.done():
                if not self.connected:
                    self.cancel("local_disconnected")
                elif time.monotonic() >= job.deadline:
                    self.cancel("local_job_expired")
                else:
                    await asyncio.wait({future}, timeout=min(1.0, max(0.0, job.deadline - time.monotonic())))
            return future.result()
        except asyncio.CancelledError:
            if self._job is job:
                self.cancel()
            if future.done() and not future.cancelled():
                future.exception()  # Consume cancellation metadata; never retry.
            raise
