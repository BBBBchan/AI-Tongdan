import json
from urllib.parse import parse_qs

import httpx
import pytest

from clipper.bili_publish import BiliPublisher, PublishError


COOKIE = "SESSDATA=private-session; bili_jct=private-csrf; DedeUserID=42"


@pytest.mark.asyncio
async def test_upload_protocol_and_credentials_are_separated(tmp_path):
    video = tmp_path / "test.mp4"
    video.write_bytes(b"0123456789abcdef")
    seen_parts, progress, merged = [], [], []
    transient_failure = False

    def handle(request):
        nonlocal transient_failure
        if request.url.host == "member.bilibili.com":
            assert request.headers["cookie"] == COOKIE
            assert request.url.path == "/preupload"
            assert request.url.params["profile"] == "ugcupos/bup"
            assert request.url.params["size"] == "16"
            return httpx.Response(200, json={
                "OK": 1, "chunk_size": 5, "auth": "private-upos-auth", "biz_id": 123,
                "endpoint": "//upos-cs-upcdnbda2.bilivideo.com",
                "upos_uri": "upos://bucket/server-video.mp4",
            })
        assert request.url.host == "upos-cs-upcdnbda2.bilivideo.com"
        assert "cookie" not in request.headers
        assert request.headers["x-upos-auth"] == "private-upos-auth"
        if request.method == "PUT":
            if not transient_failure:
                transient_failure = True
                return httpx.Response(503)
            q = request.url.params
            assert q["partNumber"] == str(int(q["chunk"]) + 1)
            assert q["chunks"] == "4" and q["total"] == "16"
            assert len(request.content) == int(q["size"])
            assert int(q["end"]) - int(q["start"]) == len(request.content)
            seen_parts.append(request.content)
            return httpx.Response(200)
        if "uploads" in request.url.params:
            return httpx.Response(200, json={"upload_id": "private-upload-id"},
                                  headers={"Set-Cookie": "unexpected=secret; Path=/"})
        merged.append(json.loads(request.content))
        return httpx.Response(200, json={"OK": 1})

    async def on_progress(done, total):
        progress.append((done, total))

    async with BiliPublisher(COOKIE, transport=httpx.MockTransport(handle)) as client:
        result = await client.upload_video(video, on_progress)
    assert result == {"filename": "server-video", "cid": 123}
    assert b"".join(seen_parts) == video.read_bytes()
    assert progress == [(0, 16), (5, 16), (10, 16), (15, 16), (16, 16)]
    assert merged == [{"parts": [{"partNumber": n, "eTag": "etag"} for n in range(1, 5)]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("cover_host", ["i0.hdslb.com", "archive.biliimg.com"])
async def test_account_categories_and_cover(tmp_path, cover_host):
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xfffake-cover")

    def handle(request):
        assert request.headers["cookie"] == COOKIE
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json={
                "code": 0, "data": {"isLogin": True, "mid": 42, "uname": "测试账号"},
            })
        if request.url.path.endswith("/pre"):
            return httpx.Response(200, json={"code": 0, "data": {"typelist": [
                {"id": 4, "name": "游戏", "children": [
                    {"id": 17, "name": "单机游戏", "children": []},
                ]},
            ]}})
        assert request.url.path.endswith("/cover/up")
        form = parse_qs(request.content.decode())
        assert form["csrf"] == ["private-csrf"]
        assert form["cover"][0].startswith("data:image/jpeg;base64,")
        return httpx.Response(200, json={
            "code": 0, "data": {"url": f"http://{cover_host}/bfs/archive/test.jpg"},
        })

    async with BiliPublisher(COOKIE, transport=httpx.MockTransport(handle)) as client:
        assert await client.account() == {
            "configured": True, "logged_in": True, "uid": 42, "name": "测试账号",
        }
        assert await client.categories() == [{"id": 17, "name": "单机游戏", "parent": "游戏"}]
        assert await client.upload_cover(cover) == f"https://{cover_host}/bfs/archive/test.jpg"


META = {
    "title": "切片", "description": "简介", "tags": ["游戏", "精彩时刻"],
    "tid": 17, "copyright": 2, "source": "https://live.bilibili.com/1",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "uncertain"), [
    ("timeout", True), ("server", True), ("html", True), ("missing", True),
    ("rejected", False),
])
async def test_submit_never_retries_and_preserves_ambiguous_outcome(kind, uncertain):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.path == "/x/vu/web/add/v3"
        assert request.url.params["csrf"] == "private-csrf"
        payload = json.loads(request.content)
        assert payload["tag"] == "游戏,精彩时刻"
        assert payload["videos"] == [{"filename": "object-key", "title": "切片", "desc": ""}]
        if kind == "timeout":
            raise httpx.ReadTimeout("private-session", request=request)
        if kind == "server":
            return httpx.Response(503, json={"code": -101, "message": COOKIE})
        if kind == "html":
            return httpx.Response(200, text=COOKIE)
        if kind == "missing":
            return httpx.Response(200, json={"code": 0, "data": {"aid": 123}})
        return httpx.Response(200, json={"code": 406, "message": COOKIE})

    async with BiliPublisher(COOKIE, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PublishError) as error:
            await client.submit(META, {"filename": "object-key"}, "")
    assert len(calls) == 1
    assert error.value.uncertain is uncertain
    assert "private-session" not in str(error.value)
    assert "private-csrf" not in str(error.value)


@pytest.mark.asyncio
async def test_successful_submit_returns_ids_without_claiming_publication():
    def handle(request):
        return httpx.Response(200, json={"code": 0, "data": {"aid": 123, "bvid": "BVtest123"}})

    async with BiliPublisher(COOKIE, transport=httpx.MockTransport(handle)) as client:
        assert await client.submit(META, {"filename": "object-key"}, "") == {
            "aid": 123, "bvid": "BVtest123",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "public", "expected"), [
    (-30, False, "submitted"), (-40, False, "submitted"), (-50, False, "private"),
    (-2, False, "rejected"), (-4, False, "rejected"), (0, False, "submitted"),
    (0, True, "published"), (-999, False, "submitted"),
])
async def test_publication_requires_anonymous_visibility(state, public, expected):
    public_calls = []

    def handle(request):
        if request.url.host == "member.bilibili.com":
            assert request.headers["cookie"] == COOKIE
            assert request.url.path == "/x/vupre/web/archive/view"
            return httpx.Response(200, json={"code": 0, "data": {"archive": {
                "aid": 123, "bvid": "BVtest123", "state": state, "state_desc": "平台状态",
            }}})
        public_calls.append(request)
        assert "cookie" not in request.headers
        assert "x-upos-auth" not in request.headers
        return httpx.Response(200, json={
            "code": 0 if public else -404,
            "data": {"aid": 123, "state": 0} if public else None,
        })

    async with BiliPublisher(COOKIE, transport=httpx.MockTransport(handle)) as client:
        result = await client.status(123)
    assert result["status"] == expected
    assert bool(public_calls) is (state == 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "is_only_self"), [(-50, 0), (0, 1)])
async def test_private_archive_never_consults_potentially_stale_public_view(state, is_only_self):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.host == "member.bilibili.com", "A private archive must not use the public cache."
        return httpx.Response(200, json={"code": 0, "data": {"archive": {
            "aid": 123, "bvid": "BVtest123", "state": state,
            "state_desc": "开放浏览", "is_only_self": is_only_self,
        }}})

    async with BiliPublisher(COOKIE, transport=httpx.MockTransport(handle)) as client:
        result = await client.status(123)
    assert result["status"] == "private"
    assert result["platform_message"] == "仅自己可见"
    assert len(requests) == 1


@pytest.mark.parametrize("endpoint", [
    "//bilivideo.com.evil.test", "//evilbilivideo.com",
    "http://upos-cs-upcdnbda2.bilivideo.com", "//127.0.0.1",
    "https://user:secret@upos-cs-upcdnbda2.bilivideo.com",
    "https://upos-cs-upcdnbda2.bilivideo.com:8080",
])
def test_untrusted_upos_endpoints_are_rejected(endpoint):
    with pytest.raises(PublishError):
        BiliPublisher._upos_url(endpoint, "upos://bucket/test.mp4")
