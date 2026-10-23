"""Small cookie-authenticated Bilibili upload client.

Protocol references checked 2026-10-20 (independently implemented with httpx):
https://github.com/biliup/biliup/blob/336de37469aae68d11252fa979cdc9b72ed2c8de/crates/biliup/src/uploader/line/upos.rs
https://github.com/biliup/biliup/blob/336de37469aae68d11252fa979cdc9b72ed2c8de/crates/biliup/src/uploader/bilibili.rs
https://github.com/renmu123/biliLive-tools/blob/54d835b7836bfcc8804016c8da074da2ea1c9683/packages/shared/src/task/bili.ts

An accepted submission is still subject to transcoding, review and publication.
The final submission request is deliberately never retried.
"""
from __future__ import annotations

import asyncio
import base64
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote, urlsplit

import httpx

API = "https://api.bilibili.com"
MEMBER = "https://member.bilibili.com"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
Progress = Callable[[int, int], Awaitable[None]]
_ERRORS = {
    -101: "B站登录已失效，请更新登录 Cookie",
    -111: "B站登录校验失败，请更新包含 bili_jct 的 Cookie",
    -400: "B站未接受投稿参数，请检查标题、分区和标签",
    -403: "B站拒绝此操作，请到创作中心检查账号状态",
    -412: "B站暂时限制访问，请稍后在创作中心重试",
    -352: "B站需要人工验证，请到创作中心完成验证",
    406: "B站限制上传频率，请在创作中心完成验证或稍后重试",
    601: "B站限制上传频率，请稍后重试",
    21071: "B站不允许转载稿件参加该活动",
    21590: "B站已停用此投稿接口，请到创作中心投稿",
}


class PublishError(RuntimeError):
    """A credential-free upstream error, including ambiguous submission outcomes."""

    def __init__(self, message: str, code: int | None = None,
                 uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    return None


async def _remove_cookies(request: httpx.Request) -> None:
    # Discard even cookies set by an earlier CDN/public response.
    request.headers.pop("Cookie", None)


class BiliPublisher:
    def __init__(self, cookie: str, transport: httpx.AsyncBaseTransport | None = None):
        self._cookie = str(cookie or "").strip()
        self._cookies: dict[str, str] = {}
        if self._cookie.isascii() and not any(ord(char) < 32 for char in self._cookie):
            try:
                parsed = SimpleCookie()
                parsed.load(self._cookie)
                self._cookies = {key: value.value for key, value in parsed.items()}
            except CookieError:
                pass
        self._secrets = {self._cookie, *self._cookies.values()}
        self._secrets |= {unquote(value) for value in self._secrets}
        options = dict(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(60, connect=15),
            headers={"User-Agent": USER_AGENT},
        )
        # No default Cookie header or shared cookie jar: upload hosts only get UPOS auth.
        self._api = httpx.AsyncClient(**options)
        self._storage = httpx.AsyncClient(**options, event_hooks={"request": [_remove_cookies]})
        self._public = httpx.AsyncClient(**options, event_hooks={"request": [_remove_cookies]})

    async def __aenter__(self) -> "BiliPublisher":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._api.aclose()
        await self._storage.aclose()
        await self._public.aclose()

    def _clean(self, value: Any, limit: int = 240) -> str:
        text = str(value or "")
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        text = re.sub(r"https?://[^\s\"<>]+", "[链接已隐藏]", text)
        text = re.sub(r"(?i)(sessdata|bili_jct|cookie|authorization|x-upos-auth|"
                      r"access_token|refresh_token|upload_id|uploadid|csrf)\s*[:=]\s*\S+",
                      r"\1=[已隐藏]", text)
        return text.replace("\r", " ").replace("\n", " ")[:limit]

    def _require_cookie(self, *, csrf: bool = False) -> str:
        if not self._cookies.get("SESSDATA"):
            raise PublishError("请先配置有效的 B站登录 Cookie")
        if csrf and not self._cookies.get("bili_jct"):
            raise PublishError("投稿需要 Cookie 中包含 bili_jct，请重新复制完整 Cookie")
        return self._cookies.get("bili_jct", "")

    @staticmethod
    def _decode(response: httpx.Response, *, uncertain: bool = False) -> dict[str, Any]:
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            raise PublishError("B站返回了无法识别的响应，请到创作中心核对",
                               uncertain=uncertain) from None
        if not isinstance(data, dict):
            raise PublishError("B站响应格式异常，请到创作中心核对",
                               uncertain=uncertain)
        return data

    @staticmethod
    def _check_code(data: dict[str, Any], *, uncertain: bool = False,
                    required: bool = True) -> None:
        code = _integer(data.get("code"))
        if code is not None and code != 0:
            raise PublishError(_ERRORS.get(code, f"B站拒绝请求（错误码 {code}），请检查创作中心"),
                               code=code)
        if required and code is None:
            raise PublishError("B站没有返回有效的操作结果，请到创作中心核对",
                               uncertain=uncertain)

    async def _request(self, method: str, path: str, *, api: bool = False,
                       final_submit: bool = False, **kwargs: Any) -> dict[str, Any]:
        self._require_cookie(csrf=final_submit)
        url = (API if api else MEMBER) + path
        headers = {
            "Cookie": self._cookie,
            "Referer": MEMBER + "/platform/upload/video/frame",
            "Origin": MEMBER,
        }
        try:
            response = await self._api.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise PublishError(
                "提交结果暂时无法确认，请先在创作中心核对，勿重复投稿" if final_submit
                else "连接 B站失败，请稍后重试",
                uncertain=final_submit,
            ) from None
        # HTTP 5xx can occur after a write is committed, regardless of body shape.
        if response.status_code >= 500:
            raise PublishError("B站服务暂时异常，请到创作中心核对",
                               uncertain=final_submit)
        body = self._decode(response, uncertain=final_submit)
        self._check_code(body, uncertain=final_submit)
        if not response.is_success:
            raise PublishError("B站未返回成功响应，请到创作中心核对",
                               uncertain=final_submit)
        return body

    async def account(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "configured": bool(self._cookie), "logged_in": False, "uid": None, "name": "",
        }
        if not self._cookie:
            return result
        try:
            body = await self._request("GET", "/x/web-interface/nav", api=True)
            data = body.get("data")
            if not isinstance(data, dict) or data.get("isLogin") is not True:
                raise PublishError("B站登录已失效，请更新登录 Cookie")
            uid = _integer(data.get("mid"))
            if not uid or uid < 0:
                raise PublishError("B站没有返回有效的账号信息")
            result.update(logged_in=True, uid=uid, name=self._clean(data.get("uname"), 80))
        except PublishError as exc:
            result["error"] = str(exc)
        return result

    async def categories(self) -> list[dict[str, Any]]:
        body = await self._request("GET", "/x/vupre/web/archive/pre")
        data = body.get("data")
        roots = data.get("typelist") if isinstance(data, dict) else None
        if not isinstance(roots, list):
            raise PublishError("B站未返回可用的投稿分区，请到创作中心检查账号")
        result: list[dict[str, Any]] = []
        seen: set[int] = set()

        def visit(nodes: list[Any], parent: str = "") -> None:
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                name = self._clean(node.get("name"), 80)
                children = node.get("children")
                if isinstance(children, list) and children:
                    visit(children, name)
                    continue
                tid = _integer(node.get("id"))
                if tid and tid > 0 and name and tid not in seen:
                    seen.add(tid)
                    item: dict[str, Any] = {"id": tid, "name": name}
                    if parent:
                        item["parent"] = parent
                    result.append(item)

        visit(roots)
        if not result:
            raise PublishError("B站当前没有可用的投稿分区")
        return result

    async def activity_topics(self, tid: int, keywords: list[str]) -> list[dict[str, Any]]:
        """Read the current uploader's activity topics for this account/category."""
        body = await self._request("GET", "/x/vupre/web/topic/type",
                                   params={"type_id": tid, "pn": 0, "ps": 20})
        data = body.get("data") or {}
        rows = data.get("topics")
        if not isinstance(rows, list):
            raise PublishError("B站未返回可识别的投稿活动列表")
        result = list(rows)
        # Search existing metadata and the narrowly reviewed content policy;
        # never invent an activity ID from a topic title or a stale cache.
        for word in list(dict.fromkeys(keywords))[:4]:
            found = await self._request("GET", "/x/vupre/web/topic/search", params={
                "keywords": word, "page_size": 20, "offset": 0})
            topics = ((found.get("data") or {}).get("result") or {}).get("topics")
            if isinstance(topics, list):
                result.extend(topics)
        return result

    async def activity_category_allowed(self, tid: int, topic_id: int) -> dict[str, Any]:
        body = await self._request("GET", "/x/vupre/web/topic/type/check",
                                   params={"type_id": tid, "topic_id": topic_id})
        data = body.get("data") or {}
        return {"allowed": _integer(data.get("code")) == 0 and not data.get("content"),
                "code": _integer(data.get("code")), "content": self._clean(data.get("content")),
                "tid": tid, "topic_id": topic_id}

    async def activity_details(self, topic_id: int) -> dict[str, Any]:
        body = await self._request("GET", "/x/topic/web/details/top", api=True,
                                   params={"topic_id": topic_id, "source": "Web"})
        data = body.get("data")
        if not isinstance(data, dict):
            raise PublishError("B站未返回可识别的活动详情")
        return data

    @staticmethod
    def _upos_url(endpoint: str, uri: str) -> str:
        parsed = urlsplit("https:" + endpoint if endpoint.startswith("//") else endpoint)
        host = parsed.hostname or ""
        try:
            valid_port = parsed.port in (None, 443)
        except ValueError:
            valid_port = False
        if (parsed.scheme != "https" or not valid_port or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")
                or not any(host.endswith("." + suffix)
                           for suffix in ("bilivideo.com", "bilivideo.cn"))):
            raise PublishError("B站返回了不支持的上传地址，请稍后重试")
        if not uri.startswith("upos://"):
            raise PublishError("B站返回的上传文件信息无效")
        object_path = uri[len("upos://"):]
        if (not object_path or any(part in ("", ".", "..") for part in object_path.split("/"))
                or any(char in object_path for char in ("?", "#", "\\", "\r", "\n", "%"))):
            raise PublishError("B站返回的上传文件信息无效")
        return f"https://{host}/{quote(object_path, safe='/._-')}"

    async def _storage_request(self, method: str, url: str, auth: str,
                               **kwargs: Any) -> httpx.Response:
        try:
            response = await self._storage.request(
                method, url, headers={"X-Upos-Auth": auth}, **kwargs,
            )
        except httpx.HTTPError:
            raise PublishError("视频传输失败，请稍后重试") from None
        if not response.is_success:
            raise PublishError("B站视频存储暂时不可用，请稍后重试")
        return response

    async def upload_video(self, path: str | Path,
                           progress: Progress | None = None) -> dict[str, Any]:
        path = Path(path)
        try:
            total = path.stat().st_size
        except OSError:
            raise PublishError("导出视频不存在或无法读取") from None
        if not total:
            raise PublishError("导出视频为空，无法上传")
        # Choose an established UPOS route; only storage domains are accepted below.
        self._require_cookie()
        params = {
            "name": path.name, "size": total, "r": "upos", "profile": "ugcupos/bup",
            "ssl": 0, "version": "2.14.0", "build": 2140000, "upcdn": "bda2",
            "zone": "cs", "probe_version": 20221109,
        }
        try:
            response = await self._api.get(
                MEMBER + "/preupload", params=params, timeout=30,
                headers={"Cookie": self._cookie,
                         "Referer": MEMBER + "/platform/upload/video/frame"},
            )
        except httpx.HTTPError:
            raise PublishError("无法初始化 B站上传，请稍后重试") from None
        pre = self._decode(response)
        self._check_code(pre, required=False)
        if not response.is_success or pre.get("OK") != 1:
            raise PublishError("B站未接受视频上传，请在创作中心检查账号")
        chunk_size = _integer(pre.get("chunk_size"))
        cid = _integer(pre.get("biz_id"))
        auth, endpoint, uri = pre.get("auth"), pre.get("endpoint"), pre.get("upos_uri")
        if (not chunk_size or not 1 <= chunk_size <= 64 * 1024 * 1024 or not cid or cid < 0
                or not all(isinstance(value, str) and value for value in (auth, endpoint, uri))):
            raise PublishError("B站返回的视频上传参数不完整")
        self._secrets.add(auth)
        url = self._upos_url(endpoint, uri)
        initialized = self._decode(await self._storage_request(
            "POST", url, auth, params={"uploads": "", "output": "json"},
        ))
        upload_id = initialized.get("upload_id")
        if not isinstance(upload_id, str) or not upload_id:
            raise PublishError("B站没有创建视频上传会话")
        self._secrets.add(upload_id)
        chunks = (total + chunk_size - 1) // chunk_size
        parts: list[dict[str, Any]] = []
        uploaded = 0
        if progress:
            await progress(0, total)
        try:
            with path.open("rb") as source:
                for index in range(chunks):
                    content = await asyncio.to_thread(source.read, chunk_size)
                    expected = min(chunk_size, total - uploaded)
                    if len(content) != expected:
                        raise PublishError("视频文件在上传过程中发生变化，请重新导出")
                    query = {
                        "uploadId": upload_id, "chunks": chunks, "total": total,
                        "chunk": index, "size": len(content), "partNumber": index + 1,
                        "start": uploaded, "end": uploaded + len(content),
                    }
                    for attempt in range(3):
                        try:
                            piece = await self._storage.put(
                                url, params=query, content=content,
                                headers={"X-Upos-Auth": auth,
                                         "Content-Type": "application/octet-stream"},
                                timeout=httpx.Timeout(240, connect=15),
                            )
                        except httpx.HTTPError:
                            piece = None
                        if piece is not None and piece.is_success:
                            break
                        if piece is not None and piece.status_code < 500:
                            raise PublishError("B站拒绝了视频分片，请稍后在创作中心重试")
                        if attempt == 2:
                            raise PublishError("视频分片传输失败，请稍后重试")
                        await asyncio.sleep(0.5 * 2 ** attempt)
                    parts.append({"partNumber": index + 1, "eTag": "etag"})
                    uploaded += len(content)
                    if progress:
                        await progress(uploaded, total)
                if await asyncio.to_thread(source.read, 1):
                    raise PublishError("视频文件在上传过程中发生变化，请重新导出")
        except OSError:
            raise PublishError("无法读取导出视频，请重新导出") from None
        complete = self._decode(await self._storage_request(
            "POST", url, auth,
            params={"name": path.name, "uploadId": upload_id, "biz_id": cid,
                    "output": "json", "profile": "ugcupos/bup"},
            json={"parts": parts},
        ))
        if complete.get("OK") != 1:
            raise PublishError("B站未完成视频合并，请稍后重试")
        filename = Path(uri).stem
        if not filename:
            raise PublishError("B站没有返回上传文件编号")
        return {"filename": filename, "cid": cid}

    async def upload_cover(self, path: str | Path) -> str:
        csrf = self._require_cookie(csrf=True)
        try:
            size = Path(path).stat().st_size
            if not 0 < size <= 10 * 1024 * 1024:
                raise PublishError("封面图片为空或超过 10 MB")
            content = await asyncio.to_thread(Path(path).read_bytes)
        except OSError:
            raise PublishError("封面图片不存在或无法读取") from None
        if content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        else:
            raise PublishError("封面需要使用 JPEG 或 PNG 图片")
        body = await self._request(
            "POST", "/x/vu/web/cover/up",
            data={"cover": f"data:{mime};base64," + base64.b64encode(content).decode("ascii"),
                  "csrf": csrf},
        )
        data = body.get("data")
        url = data.get("url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise PublishError("B站没有返回封面地址")
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not any((parsed.hostname or "").endswith("." + domain)
                                              for domain in ("hdslb.com", "biliimg.com"))
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise PublishError("B站返回的封面地址无效")
        return url

    @staticmethod
    def submission_payload(metadata: dict[str, Any], video: dict[str, Any], cover: str) -> dict[str, Any]:
        title = str(metadata.get("title", "")).strip()
        description = str(metadata.get("description", ""))
        tags = metadata.get("tags")
        tid, copyright = _integer(metadata.get("tid")), _integer(metadata.get("copyright"))
        source = str(metadata.get("source", "")).strip()
        filename = video.get("filename")
        if (not title or len(title) > 80 or len(description) > 2000
                or not tid or tid < 0 or copyright not in (1, 2)
                or not isinstance(tags, list) or not tags
                or not all(isinstance(tag, str) and tag.strip() and "," not in tag for tag in tags)
                or not isinstance(filename, str) or not filename
                or (copyright == 2 and (not source or len(source) > 200))):
            raise PublishError("请检查投稿标题、简介、标签、分区和转载来源")
        payload = {
            "copyright": copyright, "source": source if copyright == 2 else "",
            "tid": tid, "cover": cover, "title": title, "desc_format_id": 0,
            "desc": description, "dynamic": "", "tag": ",".join(tag.strip() for tag in tags),
            "videos": [{"filename": filename, "title": title, "desc": ""}],
            "subtitle": {"open": 0, "lan": ""}, "interactive": 0,
            "no_reprint": 0, "dolby": 0, "lossless_music": 0,
        }
        if "mission_id" in metadata or "topic_id" in metadata:
            mission_id, topic_id = _integer(metadata.get("mission_id")), _integer(metadata.get("topic_id"))
            if copyright != 1 or not mission_id or mission_id < 0 or not topic_id or topic_id < 0:
                raise PublishError("活动投稿须为原创且包含有效的活动和话题编号")
            payload.update(mission_id=mission_id, topic_id=topic_id)
        return payload

    async def submit(self, metadata: dict[str, Any], video: dict[str, Any],
                     cover: str) -> dict[str, Any]:
        csrf = self._require_cookie(csrf=True)
        payload = self.submission_payload(metadata, video, cover)
        body = await self._request(
            "POST", "/x/vu/web/add/v3", final_submit=True,
            params={"t": int(time.time() * 1000), "csrf": csrf}, json=payload,
        )
        data = body.get("data")
        aid = _integer(data.get("aid")) if isinstance(data, dict) else None
        bvid = data.get("bvid") if isinstance(data, dict) else None
        if not aid or aid < 0 or not isinstance(bvid, str) or not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
            raise PublishError("B站可能已收到稿件，但未返回完整编号，请在创作中心核对",
                               uncertain=True)
        return {"aid": aid, "bvid": bvid}

    async def status(self, aid: int) -> dict[str, Any]:
        aid = _integer(aid)
        if not aid or aid < 0:
            raise PublishError("稿件编号无效")
        body = await self._request("GET", "/x/vupre/web/archive/view", params={"aid": aid})
        data = body.get("data")
        archive = data.get("archive") if isinstance(data, dict) else None
        state = _integer(archive.get("state")) if isinstance(archive, dict) else None
        if state is None:
            raise PublishError("B站暂未返回稿件状态，请稍后刷新")
        message = self._clean(archive.get("state_desc"))
        is_private = _integer(archive.get("is_only_self")) == 1 or state == -50
        status = "private" if is_private else "submitted"
        pending = {-1, -6, -30, -40, -50, -60}
        if not is_private and state not in pending and (state in {-2, -4}
                                    or re.search(r"未通过|不通过|退回|锁定|已删除|转码失败", message)):
            status = "rejected"
        bvid = archive.get("bvid")
        result: dict[str, Any] = {
            "status": status, "platform_state": state,
            "platform_message": "仅自己可见" if is_private else message or (
                "已提交，等待平台处理" if status == "submitted" else "稿件未通过"),
        }
        if isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
            result.update(bvid=bvid, url=f"https://www.bilibili.com/video/{bvid}")
        # Explicitly anonymous verification distinguishes public visibility from
        # "审核通过", scheduled release and "仅自己可见".
        if state == 0 and not is_private:
            try:
                response = await self._public.get(API + "/x/web-interface/view", params={"aid": aid})
                public = self._decode(response)
                visible = public.get("data")
                if (response.is_success and public.get("code") == 0
                        and isinstance(visible, dict) and visible.get("state") == 0
                        and _integer(visible.get("aid")) == aid):
                    result["status"] = "published"
            except (httpx.HTTPError, PublishError):
                pass
        return result
