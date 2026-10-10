import asyncio
from contextlib import suppress
import json
import struct
import zlib

import brotli
import httpx
import pytest

from app.bilibili import (BilibiliClient, BilibiliError, HEADER, MAX_PACKET_SIZE,
                          decode_packets, make_packet, message_event, parse_room_id,
                          wbi_sign)


@pytest.mark.parametrize("value, expected", [
    (510, 510), ("510", 510), ("https://live.bilibili.com/510?from=search", 510),
    ("https://live.bilibili.com/blanc/80397/", 80397),
])
def test_room_inputs(value, expected):
    assert parse_room_id(value) == expected


@pytest.mark.parametrize("value", [
    True, 0, -1, "file:///510", "https://localhost/510", "https://live.bilibili.com.evil.test/510",
    "https://live.bilibili.com@evil.test/510", "https://live.bilibili.com:8443/510",
    "https://live.bilibili.com/510/other", "https://live.bilibili.com/%35%31%30",
    "https://live.bilibili.com/../510", "000510", "https://live.bilibili.com/0",
])
def test_rejects_non_room_urls(value):
    with pytest.raises(ValueError):
        parse_room_id(value)


@pytest.mark.parametrize("version, compressor", [(2, zlib.compress), (3, brotli.compress)])
def test_decompresses_multiple_packets_and_auth_heartbeat(version, compressor):
    chat = {"cmd": "DANMU_MSG", "info": [[0, 1, 25, 16777215, 1700000000, 123], "你好", [42, "观众"]]}
    inner = make_packet(chat, 5) + make_packet(struct.pack(">I", 567), 3)
    wire = make_packet({"code": 0}, 8) + make_packet(compressor(inner), 5, version)
    decoded = decode_packets(wire)
    assert decoded == [(8, {"code": 0}), (5, chat), (3, 567)]
    event = message_event(decoded[1][1], 80397)
    assert event["text"] == "你好"
    assert event["user_id"] == 42
    assert event["sent_at"] == 1700000000
    assert event["message_id"] == message_event(chat, 80397)["message_id"]


@pytest.mark.parametrize("packet", [b"short", HEADER.pack(1, 16, 1, 5, 1),
                                      make_packet(b"not-json", 5), make_packet(b"", 3),
                                      make_packet(b"x", 5, 9)])
def test_malformed_packets_fail_closed(packet):
    with pytest.raises(BilibiliError):
        decode_packets(packet)


@pytest.mark.parametrize("version, compressor", [(2, zlib.compress), (3, brotli.compress)])
def test_decompression_bound(version, compressor):
    bomb = compressor(b"x" * (MAX_PACKET_SIZE + 1))
    with pytest.raises(BilibiliError, match="超过限制"):
        decode_packets(make_packet(bomb, 5, version))


def test_decompression_budget_is_shared_between_sibling_packets():
    data = make_packet({"text": "x" * (MAX_PACKET_SIZE // 2)}, 5)
    packet = make_packet(zlib.compress(data), 5, 2)
    with pytest.raises(BilibiliError, match="超过限制"):
        decode_packets(packet + packet)


def test_wbi_filters_and_orders_params():
    signed = wbi_sign({"foo": "114!", "bar": "514(", "baz": "1919810'"},
                      "ea1db124af3c7062474693fa704f4ff8", timestamp=1702204169)
    # Public protocol example, including reserved-character removal.
    assert signed == {"bar": "514", "baz": "1919810", "foo": "114",
                      "wts": "1702204169", "w_rid": "6149fdadf571698ca7e6a567265cd0ee"}


@pytest.mark.asyncio
async def test_offline_short_room_has_metadata_and_no_video_sources():
    def handler(request):
        path = request.url.path
        if path.endswith("room_init"):
            assert request.url.params["id"] == "510"
            data = {"room_id": 80397, "short_id": 510, "uid": 8, "live_status": 0}
        elif path.endswith("get_info"):
            assert request.url.params["room_id"] == "80397"
            data = {"room_id": 80397, "title": "等待开播", "uid": 8, "live_status": 0}
        else:
            data = {"live_status": 0, "playurl_info": None}
        return httpx.Response(200, json={"code": 0, "data": data})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BilibiliClient(client=http)
        room = await client.resolve_room("https://live.bilibili.com/510")
        assert (room.room_id, room.short_id, room.title, room.live_status) == (80397, 510, "等待开播", 0)
        assert await client.get_play_sources(room.room_id) == []
        await client.aclose()
        assert not http.is_closed  # Caller owns an injected client.


@pytest.mark.asyncio
async def test_stream_sources_prefer_hls_avc_and_never_forward_cookie():
    def handler(request):
        assert request.url.params["codec"] == "0"
        def stream(protocol, fmt, codec):
            return {"protocol_name": protocol, "format": [{"format_name": fmt, "codec": [{
                "codec_name": codec, "current_qn": 10000, "base_url": "/room/video." + fmt,
                "url_info": [{"host": "https://cdn.example.com", "extra": "?token=redacted"}],
            }]}]}
        return httpx.Response(200, json={"code": 0, "data": {"playurl_info": {"playurl": {
            "stream": [stream("http_stream", "flv", "avc"), stream("http_hls", "ts", "avc"),
                       stream("http_hls", "fmp4", "hevc")],
        }}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BilibiliClient(cookie="SESSDATA=private", client=http)
        sources = await client.get_play_sources(80397)
        assert len(sources) == 2
        assert sources[0].protocol == "http_hls"
        assert sources[1].format == "flv"
        assert all("Cookie" not in source.request_headers for source in sources)
        assert sources[0].refresh_after > sources[0].fetched_at


@pytest.mark.asyncio
async def test_signed_chat_setup_gets_real_buvid_and_checks_auth_ack(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == "www.bilibili.com":
            return httpx.Response(200, text="ok", headers={"set-cookie": "buvid3=device; Domain=.bilibili.com; Path=/"})
        if request.url.path.endswith("nav"):
            return httpx.Response(200, json={"code": -101, "data": {"isLogin": False, "wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
            }}})
        assert "w_rid" in request.url.params and "wts" in request.url.params
        assert "buvid3=device" in request.headers["cookie"]
        return httpx.Response(200, json={"code": 0, "data": {"token": "private-chat-key",
                    "host_list": [{"host": "broadcastlv.chat.bilibili.com", "wss_port": 443}]}})

    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.messages = [make_packet({"code": 0}, 8), make_packet({"cmd": "DANMU_MSG",
                  "info": [[0, 1, 25, 0, 1700000000, 123], "你好", [42, "观众"]]}, 5)]
            self.closed = False
        async def send(self, packet):
            self.sent.append(packet)
        async def recv(self):
            await asyncio.sleep(0)
            return self.messages.pop(0)
        async def close(self):
            self.closed = True

    socket = FakeSocket()
    async def fake_connect(url, **kwargs):
        assert url == "wss://broadcastlv.chat.bilibili.com:443/sub"
        assert kwargs["origin"] == "https://live.bilibili.com"
        return socket
    monkeypatch.setattr("app.bilibili.connect", fake_connect)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BilibiliClient(client=http)
        stream = client.events(80397)
        assert (await anext(stream))["status"] == "connecting"
        assert (await anext(stream))["status"] == "connected"
        event = await anext(stream)
        assert event["type"] == "danmaku" and event["text"] == "你好"
        auth = json.loads(socket.sent[0][16:])
        assert auth["buvid"] == "device" and auth["key"] == "private-chat-key"
        assert HEADER.unpack_from(socket.sent[0])[3] == 7
        await stream.aclose()
        assert socket.closed
        await client.aclose()


@pytest.mark.asyncio
async def test_failed_auth_is_not_connected_and_cancel_stops_reconnect(monkeypatch):
    class FakeSocket:
        closed = False
        async def send(self, packet):
            pass
        async def recv(self):
            return make_packet({"code": -101}, 8)
        async def close(self):
            self.closed = True
    socket = FakeSocket()
    async def fake_connect(*args, **kwargs):
        return socket
    async def config(self, room_id):
        return "device", {"token": "private", "host_list": [{"host": "broadcastlv.chat.bilibili.com"}]}
    monkeypatch.setattr("app.bilibili.connect", fake_connect)
    monkeypatch.setattr(BilibiliClient, "_chat_config", config)
    client = BilibiliClient()
    stream = client.events(80397)
    assert (await anext(stream))["status"] == "connecting"
    failure = await anext(stream)
    assert failure["status"] == "reconnecting" and "认证被拒绝" in failure["reason"]
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert socket.closed
    await client.aclose()
