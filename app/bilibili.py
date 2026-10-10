"""Bilibili room metadata, public playback and continuously reconnecting danmaku.

Protocol references (read-only; this client never posts chat):
https://github.com/xfgryujk/blivedm/blob/dev/blivedm/clients/web.py
https://github.com/xfgryujk/blivedm/blob/dev/blivedm/clients/ws_base.py
https://github.com/streamlink/streamlink/blob/master/src/streamlink/plugins/bilibili.py
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hashlib
from http.cookies import SimpleCookie
import json
import re
import struct
import time
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlencode, urlparse
import zlib

import brotli
import httpx
from websockets.asyncio.client import connect

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
API = "https://api.live.bilibili.com"
HEADER = struct.Struct(">IHHII")
MAX_PACKET_SIZE = 8 * 1024 * 1024
WBI_INDICES = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
               27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13)


class BilibiliError(RuntimeError):
    """A sanitized, actionable upstream error without tokens or cookies."""


@dataclass(frozen=True)
class RoomInfo:
    room_id: int
    short_id: int
    title: str
    owner_uid: int
    live_status: int
    cover: str


@dataclass(frozen=True)
class PlaySource:
    url: str
    protocol: str
    format: str
    codec: str
    quality: int
    request_headers: dict[str, str]
    fetched_at: float
    refresh_after: float


def parse_room_id(value: str | int) -> int:
    """Accept only a positive ID or an exact Bilibili live-room URL."""
    if isinstance(value, bool):
        raise ValueError("请输入 Bilibili 直播间地址或正整数房间号")
    text = str(value).strip()
    if re.fullmatch(r"[1-9][0-9]{0,15}", text):
        return int(text)
    parsed = urlparse(text)
    if (parsed.scheme not in ("http", "https") or parsed.netloc != "live.bilibili.com"
            or not re.fullmatch(r"/(?:blanc/)?[1-9][0-9]{0,15}/?", parsed.path)):
        raise ValueError("仅支持 https://live.bilibili.com/房间号 或房间号")
    return int(parsed.path.rstrip("/").rsplit("/", 1)[1])


def make_packet(body: bytes | dict[str, Any], operation: int, version: int = 1) -> bytes:
    if isinstance(body, dict):
        body = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    return HEADER.pack(len(body) + HEADER.size, HEADER.size, version, operation, 1) + body


def _decompress(body: bytes, version: int) -> bytes:
    if version == 2:
        decoder = zlib.decompressobj()
        result = decoder.decompress(body, MAX_PACKET_SIZE + 1)
        if len(result) > MAX_PACKET_SIZE or decoder.unconsumed_tail:
            raise BilibiliError("弹幕解压数据超过限制")
        if not decoder.eof:
            raise BilibiliError("弹幕压缩数据不完整")
        return result
    # Brotli >= 1.2 limits each native output allocation, including a payload
    # with a very high compression ratio. Never decompress an unbounded body.
    decoder = brotli.Decompressor()
    chunks, size, pending = [], 0, body
    while True:
        chunk = decoder.process(pending, output_buffer_limit=MAX_PACKET_SIZE - size + 1)
        size += len(chunk)
        if size > MAX_PACKET_SIZE:
            raise BilibiliError("弹幕解压数据超过限制")
        chunks.append(chunk)
        if decoder.is_finished():
            return b"".join(chunks)
        if decoder.can_accept_more_data():
            raise BilibiliError("弹幕压缩数据不完整")
        if not chunk:
            raise BilibiliError("弹幕压缩数据无效")
        pending = b""



def decode_packets(data: bytes, _depth: int = 0,
                   _budget: list[int] | None = None) -> list[tuple[int, Any]]:
    """Decode concatenated packets, including recursively compressed batches."""
    if _budget is None:
        _budget = [MAX_PACKET_SIZE]
    _budget[0] -= len(data)
    if _budget[0] < 0 or _depth > 3:
        raise BilibiliError("弹幕数据大小或压缩层数超过限制")
    packets: list[tuple[int, Any]] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            raise BilibiliError("弹幕包头不完整")
        length, header_size, version, operation, _ = HEADER.unpack_from(data, offset)
        if header_size < HEADER.size or length < header_size or length > len(data) - offset:
            raise BilibiliError("弹幕包长度无效")
        body = data[offset + header_size:offset + length]
        offset += length
        if version in (2, 3):
            try:
                unpacked = _decompress(body, version)
            except (zlib.error, brotli.error) as exc:
                raise BilibiliError("弹幕压缩数据无效") from exc
            packets.extend(decode_packets(unpacked, _depth + 1, _budget))
        elif version in (0, 1):
            if operation == 3:
                if len(body) < 4:
                    raise BilibiliError("弹幕心跳数据无效")
                packets.append((operation, struct.unpack(">I", body[:4])[0]))
            elif operation in (5, 8):
                try:
                    decoded = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeError) as exc:
                    raise BilibiliError("弹幕消息 JSON 无效") from exc
                if isinstance(decoded, dict):
                    packets.append((operation, decoded))
        else:
            raise BilibiliError("弹幕协议版本不支持")
        if len(packets) > 4096:
            raise BilibiliError("单批弹幕消息数量超过限制")
    return packets


def wbi_sign(params: dict[str, Any], key: str, timestamp: int | None = None) -> dict[str, str]:
    values = {**params, "wts": str(int(time.time()) if timestamp is None else timestamp)}
    cleaned = {name: re.sub(r"[!'()*]", "", str(values[name])) for name in sorted(values)}
    cleaned["w_rid"] = hashlib.md5((urlencode(cleaned) + key).encode()).hexdigest()
    return cleaned


def _timestamp(value: Any, fallback: float) -> float:
    try:
        stamp = float(value)
        return stamp / 1000 if stamp > 1e12 else stamp
    except (ValueError, TypeError):
        return fallback


def message_event(message: dict[str, Any], room_id: int) -> dict[str, Any] | None:
    now = time.time()
    command = str(message.get("cmd", "")).split(":", 1)[0]
    data = message.get("data") or {}
    if command == "DANMU_MSG":
        info = message.get("info") or []
        if len(info) < 3 or not isinstance(info[0], list) or not isinstance(info[2], list):
            return None
        meta, user = info[0], info[2]
        sent_at = _timestamp(meta[4] if len(meta) > 4 else None, now)
        text = str(info[1])
        uid = user[0] if user else 0
        uname = str(user[1]) if len(user) > 1 else "观众"
        # Server nonce is absent for some anonymous messages. Include the
        # timestamp and content so clients can suppress reconnect duplicates.
        nonce = str(meta[5]) if len(meta) > 5 else ""
        identity = f"{room_id}:{uid}:{sent_at}:{nonce}:{text}"
        return {"type": "danmaku", "room_id": room_id, "text": text,
                "user_id": uid, "user_name": uname, "sent_at": sent_at,
                "received_at": now, "message_id": hashlib.sha256(identity.encode()).hexdigest()[:24]}
    if command in ("LIVE", "PREPARING", "ROOM_CHANGE"):
        event: dict[str, Any] = {"type": "live_status", "room_id": room_id,
                                "received_at": now, "command": command}
        if command in ("LIVE", "PREPARING"):
            event["live_status"] = 1 if command == "LIVE" else 0
        if isinstance(data, dict) and data.get("title"):
            event["title"] = str(data["title"])
        return event
    return None


class BilibiliClient:
    def __init__(self, cookie: str = "", client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._headers = {"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com/",
                         "Origin": "https://live.bilibili.com"}
        if "\r" in cookie or "\n" in cookie:
            raise ValueError("BILIBILI_COOKIE 不得包含换行")
        self._cookie = SimpleCookie()
        if cookie:
            self._cookie.load(cookie)
            for name, item in self._cookie.items():
                self._client.cookies.set(name, item.value, domain=".bilibili.com", path="/")
        self._wbi_key = ""
        self._wbi_expires = 0.0
        self._uid = 0
        self._identity_lock = asyncio.Lock()
        self._closed = asyncio.Event()
        self._sockets: set[Any] = set()

    async def _json(self, path: str, params: dict[str, Any] | None = None, *, allow_nav: bool = False) -> dict:
        url = path if path.startswith("https://") else API + path
        try:
            response = await self._client.get(url, params=params, headers=self._headers)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise BilibiliError(f"Bilibili 接口 HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError):
            raise BilibiliError("Bilibili 接口暂不可达或响应格式错误") from None
        if not isinstance(payload, dict):
            raise BilibiliError("Bilibili 接口响应格式错误")
        if payload.get("code") != 0 and not (allow_nav and payload.get("code") == -101):
            raise BilibiliError(f"Bilibili 接口返回错误码 {payload.get('code', 'unknown')}；可能需要有效 Cookie")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BilibiliError("Bilibili 接口缺少数据")
        return data

    async def resolve_room(self, value: str | int) -> RoomInfo:
        requested = parse_room_id(value)
        initial = await self._json("/room/v1/Room/room_init", {"id": requested})
        room_id = int(initial["room_id"])
        detail = await self._json("/room/v1/Room/get_info", {"room_id": room_id})
        return RoomInfo(room_id=room_id, short_id=int(detail.get("short_id") or initial.get("short_id") or 0),
                        title=str(detail.get("title") or "Bilibili 直播"),
                        owner_uid=int(detail.get("uid") or initial.get("uid") or 0),
                        live_status=int(detail.get("live_status", initial.get("live_status", 0))),
                        cover=str(detail.get("user_cover") or detail.get("keyframe") or ""))

    async def get_play_sources(self, room_id: int) -> list[PlaySource]:
        room_id = parse_room_id(room_id)
        data = await self._json("/xlive/web-room/v2/index/getRoomPlayInfo", {
            "room_id": room_id, "protocol": "0,1", "format": "0,1,2", "codec": "0",
            "qn": 10000, "platform": "web", "ptype": 8,
        })
        playurl = (data.get("playurl_info") or {}).get("playurl") or {}
        now, seen, result = time.time(), set(), []
        for stream in playurl.get("stream") or []:
            protocol = str(stream.get("protocol_name", ""))
            for fmt in stream.get("format") or []:
                format_name = str(fmt.get("format_name", ""))
                for codec in fmt.get("codec") or []:
                    codec_name = str(codec.get("codec_name", ""))
                    if codec_name.lower() not in ("avc", "h264"):
                        continue
                    for host in codec.get("url_info") or []:
                        url = str(host.get("host", "")) + str(codec.get("base_url", "")) + str(host.get("extra", ""))
                        parsed = urlparse(url)
                        if parsed.scheme not in ("https", "http") or not parsed.hostname or url in seen:
                            continue
                        seen.add(url)
                        refresh_after = now + 120
                        deadline = parse_qs(parsed.query).get("deadline", [""])[0]
                        if deadline.isdigit():
                            refresh_after = min(refresh_after, max(now + 5, int(deadline) - 30))
                        result.append(PlaySource(url, protocol, format_name, codec_name,
                                                int(codec.get("current_qn") or 0),
                                                {**self._headers, "Referer": f"https://live.bilibili.com/{room_id}"},
                                                now, refresh_after))
        # Prefer HLS AVC for browsers; retain FLV for fallback and frame capture.
        return sorted(result, key=lambda source: (source.protocol != "http_hls", -source.quality))

    def _get_buvid(self) -> str:
        for cookie in self._client.cookies.jar:
            if cookie.name == "buvid3" and (cookie.domain == "bilibili.com" or cookie.domain.endswith(".bilibili.com")):
                return cookie.value
        return ""

    async def _identity(self) -> str:
        async with self._identity_lock:
            if not self._get_buvid():
                try:
                    response = await self._client.get("https://www.bilibili.com/", headers=self._headers)
                    response.raise_for_status()
                except httpx.HTTPError:
                    raise BilibiliError("无法获取弹幕设备标识；请检查网络或配置有效 BILIBILI_COOKIE") from None
                if not self._get_buvid():
                    raise BilibiliError("Bilibili 未返回 buvid3；请配置浏览器中的有效 BILIBILI_COOKIE")
            if not self._wbi_key or time.time() >= self._wbi_expires:
                data = await self._json("https://api.bilibili.com/x/web-interface/nav", allow_nav=True)
                self._uid = int(data.get("mid") or 0) if data.get("isLogin") else 0
                images = data.get("wbi_img") or {}
                raw = "".join(urlparse(str(images.get(name, ""))).path.rsplit("/", 1)[-1].split(".", 1)[0]
                              for name in ("img_url", "sub_url"))
                if len(raw) < 64:
                    raise BilibiliError("Bilibili 未返回有效 WBI 签名密钥")
                self._wbi_key = "".join(raw[index] for index in WBI_INDICES)
                self._wbi_expires = time.time() + 11 * 3600
            return self._get_buvid()

    async def _chat_config(self, room_id: int) -> tuple[str, dict[str, Any]]:
        buvid = await self._identity()
        try:
            data = await self._json("/xlive/web-room/v1/index/getDanmuInfo",
                                    wbi_sign({"id": room_id, "type": 0}, self._wbi_key))
        except BilibiliError:
            # A fresh signing key will be fetched on the next reconnect.
            self._wbi_expires = 0
            raise
        if not data.get("token") or not data.get("host_list"):
            raise BilibiliError("弹幕接口未返回连接凭据或服务器")
        return buvid, data

    async def _heartbeat(self, socket: Any) -> None:
        while True:
            await socket.send(make_packet(b"[object Object]", 2))
            await asyncio.sleep(30)

    async def events(self, room_id: int) -> AsyncIterator[dict[str, Any]]:
        room_id = parse_room_id(room_id)
        attempt = 0
        while not self._closed.is_set():
            socket = None
            heartbeat = None
            try:
                buvid, config = await self._chat_config(room_id)
                servers = config["host_list"]
                host = servers[attempt % len(servers)]
                hostname = str(host.get("host", ""))
                if not re.fullmatch(r"[a-zA-Z0-9.-]+\.bilibili\.com", hostname):
                    raise BilibiliError("弹幕服务器地址无效")
                port = int(host.get("wss_port") or 443)
                if not 1 <= port <= 65535:
                    raise BilibiliError("弹幕服务器端口无效")
                yield {"type": "connection", "room_id": room_id, "status": "connecting", "received_at": time.time()}
                socket = await connect(f"wss://{hostname}:{port}/sub", origin=self._headers["Origin"],
                                       user_agent_header=USER_AGENT, open_timeout=15, close_timeout=3,
                                       ping_interval=None, max_size=MAX_PACKET_SIZE, max_queue=32)
                self._sockets.add(socket)
                await socket.send(make_packet({"uid": self._uid, "roomid": room_id, "protover": 3,
                                               "platform": "web", "type": 2, "buvid": buvid,
                                               "key": config["token"]}, 7))
                authenticated = False
                auth_deadline = time.monotonic() + 15
                while not self._closed.is_set():
                    if not authenticated and time.monotonic() >= auth_deadline:
                        raise BilibiliError("弹幕认证超时")
                    timeout = 70 if authenticated else max(0.01, auth_deadline - time.monotonic())
                    message = await asyncio.wait_for(socket.recv(), timeout=timeout)
                    if not isinstance(message, bytes):
                        continue
                    for operation, payload in decode_packets(message):
                        if operation == 8:
                            if payload.get("code") != 0:
                                raise BilibiliError(f"弹幕认证被拒绝（错误码 {payload.get('code', 'unknown')}）；请检查 Cookie")
                            if not authenticated:
                                authenticated = True
                                attempt = 0
                                heartbeat = asyncio.create_task(self._heartbeat(socket))
                                yield {"type": "connection", "room_id": room_id,
                                       "status": "connected", "received_at": time.time()}
                        elif operation == 3 and authenticated:
                            yield {"type": "popularity", "room_id": room_id,
                                   "popularity": payload, "received_at": time.time()}
                        elif operation == 5 and authenticated:
                            event = message_event(payload, room_id)
                            if event:
                                yield event
                    if heartbeat and heartbeat.done():
                        heartbeat.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed.is_set():
                    break
                reason = str(exc) if isinstance(exc, BilibiliError) else f"弹幕连接中断（{type(exc).__name__}）"
                delay = min(60, 2 ** min(attempt + 1, 6))
                yield {"type": "connection", "room_id": room_id, "status": "reconnecting",
                       "reason": reason, "retry_in": delay, "received_at": time.time()}
                attempt += 1
                try:
                    await asyncio.wait_for(self._closed.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await heartbeat
                if socket:
                    self._sockets.discard(socket)
                    with suppress(Exception):
                        await socket.close()

    async def aclose(self) -> None:
        self._closed.set()
        for socket in tuple(self._sockets):
            with suppress(Exception):
                await socket.close()
        if self._owns_client:
            await self._client.aclose()
