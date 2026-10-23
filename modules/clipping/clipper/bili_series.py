"""Collection inspection and durable per-streamer video-series archiving.

UGC collection requests come from the current official creator center bundle.
Ordinary video-series routes/schema come from the maintainer's published
bilibili-api-python 17.4.2 wheel. Inspection stays read-only; the separate client
and manager mutate ordinary series only after a live clip is publicly published.
"""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .bili_publish import BiliPublisher, PublishError


OFFICIAL_CREATOR_BUNDLE = (
    "https://s1.hdslb.com/bfs/static/studio/creativecenter-platform-next/"
    "static/js/async/js/creativecenter-v3.3543.a341b660.js"
)
OFFICIAL_CREATOR_UI = (
    "https://s1.hdslb.com/bfs/static/studio/creativecenter-platform-next/"
    "static/js/async/js/creativecenter-v3.2452.a341b660.js"
)
SDK_RELEASE = "https://pypi.org/project/bilibili-api-python/17.4.2/"
SERIES_CREATE_PATH = "/x/series/series/createAndAddArchives"
SERIES_APPEND_PATH = "/x/series/series/addArchives"


def positive_id(value):
    if isinstance(value, bool):
        raise ValueError("Identifier must be a positive integer")
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError("Identifier must be a positive integer")
    return int(text)


def streamer_series_plan(session, account_uid):
    """Describe a stable UID-keyed group without claiming a platform list exists.

    Introduction films and other imported files are deliberately excluded.
    Names never supply identity: a verified numeric streamer UID is required.
    """
    if session.get("mode") != "live":
        return None
    room = session.get("room") if isinstance(session.get("room"), dict) else {}
    room_uid = room.get("owner_uid")
    supplied_uid = session.get("streamer_uid")
    if room_uid and supplied_uid and positive_id(room_uid) != positive_id(supplied_uid):
        raise ValueError("Resolved room owner differs from supplied streamer identity")
    streamer_uid = positive_id(room_uid or supplied_uid)
    account_uid = positive_id(account_uid)
    profile = session.get("publish_profile") or {}
    name = room.get("uname") or session.get("streamer") or profile.get("creator_name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Verified streamer name is required")
    name = name.strip()
    if any(ord(c) < 32 for c in name):
        raise ValueError("Streamer name contains control characters")
    return {
        "key": f"{account_uid}:{streamer_uid}",
        "account_uid": account_uid,
        "streamer_uid": streamer_uid,
        "streamer_name": name,
        "title": f"{name}直播切片",
        "description": f"收录{name}的直播切片。\n主播主页：https://space.bilibili.com/{streamer_uid}",
        "platform_kind": "video_series",
        "platform_series_id": None,
        "status": "pending_platform_capability",
        "identity_source": "session.room.owner_uid" if room_uid else "session.streamer_uid",
    }


class BiliSeriesInspector(BiliPublisher):
    """Only verified GET endpoints; no create/edit/add methods."""

    async def ugc_remaining(self):
        body = await self._request("GET", "/x2/creative/web/season/remain")
        value = body.get("data")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PublishError("B站返回的合集剩余名额格式异常")
        return value

    async def ugc_seasons(self, page=1, size=30):
        page, size = positive_id(page), positive_id(size)
        if size > 30:
            raise ValueError("Read at most 30 collections per page")
        body = await self._request("GET", "/x2/creative/web/seasons", params={
            "pn": page, "ps": size, "order": "", "sort": "", "draft": 1, "source": 0,
        })
        value = body.get("data")
        if not isinstance(value, dict) or not isinstance(value.get("total"), int):
            raise PublishError("B站返回的合集列表格式异常")
        rows = value.get("seasons") or []
        if not isinstance(rows, list):
            raise PublishError("B站返回的合集列表格式异常")
        return {**value, "seasons": rows}

    async def ugc_season(self, season_id):
        body = await self._request("GET", "/x2/creative/web/season",
                                   params={"id": positive_id(season_id)})
        value = body.get("data")
        if not isinstance(value, dict):
            raise PublishError("B站返回的合集详情格式异常")
        return value

    async def ugc_section(self, section_id):
        body = await self._request("GET", "/x2/creative/web/season/section",
                                   params={"id": positive_id(section_id)})
        value = body.get("data")
        if not isinstance(value, dict):
            raise PublishError("B站返回的合集小节格式异常")
        return value

    async def video_series(self, account_uid, page=1, size=20):
        """List ordinary series and UGC seasons; their permissions are distinct."""
        account_uid, page, size = positive_id(account_uid), positive_id(page), positive_id(size)
        if size > 20:
            raise ValueError("Read at most 20 ordinary series per page")
        body = await self._request("GET", "/x/polymer/web-space/seasons_series_list", api=True, params={
            "mid": account_uid, "page_num": page, "page_size": size,
        })
        value = body.get("data")
        value = value.get("items_lists") if isinstance(value, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("series_list"), list):
            raise PublishError("B站返回的视频列表格式异常")
        return value

    async def series_info(self, series_id):
        body = await self._request("GET", "/x/series/series", api=True,
                                   params={"series_id": positive_id(series_id)})
        value = body.get("data")
        if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
            raise PublishError("B站返回的视频列表详情格式异常")
        return value

    async def series_archives(self, account_uid, series_id, page=1, size=20):
        page, size = positive_id(page), positive_id(size)
        if size > 20:
            raise ValueError("Read at most 20 archives per page")
        body = await self._request("GET", "/x/series/archives", api=True, params={
            "mid": positive_id(account_uid), "series_id": positive_id(series_id),
            "pn": page, "ps": size, "sort": "asc",
        })
        value = body.get("data")
        if not isinstance(value, dict) or not isinstance(value.get("aids"), list):
            raise PublishError("B站返回的视频列表稿件格式异常")
        return value


def _aid_list(aids):
    if not isinstance(aids, (list, tuple)):
        raise ValueError("AIDs must be an explicit list")
    values = list(dict.fromkeys(positive_id(aid) for aid in aids))
    return ",".join(str(aid) for aid in values)


def series_create_payload(account_uid, name, aids, description="", keywords=()):
    """Form fields only; caller supplies csrf/csrf_token and durable write policy."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Series name is required")
    if not isinstance(description, str) or not isinstance(keywords, (list, tuple)):
        raise ValueError("Invalid description or keywords")
    if any(not isinstance(k, str) or "," in k or "\n" in k for k in keywords):
        raise ValueError("Keywords must be separate plain strings")
    aid_text = _aid_list(aids)
    if not aid_text:
        # The published SDK permits empty lists, but current platform acceptance
        # has not been verified. Wait for this streamer's first published clip.
        raise ValueError("Wait for the first published streamer clip before creating its series")
    return {"mid": positive_id(account_uid), "aids": aid_text, "name": name.strip(),
            "keywords": ",".join(keywords), "description": description}


def series_append_payload(account_uid, series_id, aids):
    aid_text = _aid_list(aids)
    if not aid_text:
        raise ValueError("At least one published archive is required")
    return {"mid": positive_id(account_uid), "series_id": positive_id(series_id), "aids": aid_text}


class BiliSeriesClient(BiliSeriesInspector):
    """Explicit ordinary-series writes; callers own durable intent checkpoints."""

    async def create_series(self, account_uid, name, aids, description="", keywords=()):
        fields = series_create_payload(account_uid, name, aids, description, keywords)
        csrf = self._require_cookie(csrf=True)
        body = await self._request("POST", SERIES_CREATE_PATH, api=True, final_submit=True,
                                   data={**fields, "csrf": csrf, "csrf_token": csrf})
        return body.get("data")

    async def append_series(self, account_uid, series_id, aids):
        fields = series_append_payload(account_uid, series_id, aids)
        csrf = self._require_cookie(csrf=True)
        body = await self._request("POST", SERIES_APPEND_PATH, api=True, final_submit=True,
                                   data={**fields, "csrf": csrf, "csrf_token": csrf})
        return body.get("data")


class StreamerSeriesManager:
    """Archive published clips independently from the video submission journal.

    Atomic JSON checkpoints plus a nonblocking process lock prevent concurrent
    creates. Ambiguous writes are reconciled with GET only, at most six checks
    spaced one minute apart. This class never changes a publication's status.
    """

    RECHECK_SECONDS = 60
    MAX_RECHECKS = 6

    def __init__(self, settings, client_factory=None, clock=None):
        self.settings = settings
        self.client_factory = client_factory or BiliSeriesClient
        self.clock = clock or time.time
        self.directory = Path(settings.data_dir)
        self.path = self.directory / "streamer-series.json"
        self.lock_path = self.directory / "streamer-series.lock"
        self.slot = asyncio.Lock()
        self._busy_checks = {}

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "groups": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("groups"), dict):
            raise ValueError("Unsupported series registry")
        return value

    def _save(self, registry):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(registry, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.directory, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _result(self, status, plan=None, error=None, retryable=False, next_check_at=None):
        plan = plan or {}
        return {
            "status": status, "name": plan.get("title"), "series_id": plan.get("platform_series_id"),
            "url": (f"https://space.bilibili.com/{plan['account_uid']}" if plan.get("account_uid") else None),
            "url_kind": "account_space", "streamer_uid": plan.get("streamer_uid"),
            "account_uid": plan.get("account_uid"), "error": error, "retryable": retryable,
            "next_check_at": next_check_at, "checked_at": self.clock(),
        }

    @staticmethod
    def _error(exc):
        # Upstream exceptions may contain headers, response bodies or full URLs.
        if isinstance(exc, PublishError) and exc.code is not None:
            return f"B站系列操作失败（错误码 {exc.code}）"
        return "B站系列操作暂未完成，请到个人空间核对"

    @staticmethod
    def _same_streamer_description(meta, plan):
        identities = set(re.findall(r"https?://space\.bilibili\.com/([1-9]\d*)(?=$|[/\s?#])",
                                    str(meta.get("description", ""))))
        return identities == {str(plan["streamer_uid"])}

    def _finish(self, registry, group, member, status, error=None, reconcile=False):
        creating = not group.get("platform_series_id") and group.get("creation_state") in {
            "creating", "create_pending", "create_uncertain",
        }
        checks = group.get("creation_checks", 0) if creating else member.get("checks", 0)
        retryable = reconcile and checks < self.MAX_RECHECKS
        if reconcile and not retryable:
            error = (error or "") + "；自动核对次数已用完，需在个人空间人工核对"
        result = self._result(status, group, error, retryable,
                              self.clock() + self.RECHECK_SECONDS if retryable else None)
        if creating:
            group["creation_result"] = result
        member.update(result=result, updated_at=self.clock())
        if status == "added":
            member["phase"] = "added"
        self._save(registry)
        return result

    async def _find_existing(self, client, plan):
        found, read = {}, 0
        for page in range(1, 51):
            result = await client.video_series(plan["account_uid"], page=page, size=20)
            rows = result["series_list"]
            total = result.get("page", {}).get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("Invalid series pagination")
            for row in rows:
                meta = row.get("meta") or {}
                if (str(meta.get("mid")) == str(plan["account_uid"])
                        and meta.get("name") == plan["title"]
                        and self._same_streamer_description(meta, plan)):
                    found[positive_id(meta["series_id"])] = meta
            # Pagination includes both ordinary series and UGC seasons.
            read += len(rows) + len(result.get("seasons_list") or [])
            if read >= total:
                return found
            if not rows and not result.get("seasons_list"):
                raise ValueError("Incomplete series pagination")
        raise ValueError("Series list too large to verify completely")

    async def _contains(self, client, plan, aid):
        detail = await client.series_info(plan["platform_series_id"])
        meta = detail["meta"]
        if positive_id(meta.get("mid")) != plan["account_uid"]:
            raise ValueError("Series owner changed or mismatched")
        if positive_id(meta.get("series_id")) != plan["platform_series_id"]:
            raise ValueError("Series identifier mismatch")
        if not self._same_streamer_description(meta, plan):
            raise ValueError("Series streamer identity marker changed")
        read = 0
        for page in range(1, 501):
            result = await client.series_archives(plan["account_uid"], plan["platform_series_id"],
                                                   page=page, size=20)
            ids = [positive_id(value) for value in result["aids"]]
            if aid in ids:
                return True
            total = result.get("page", {}).get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("Invalid series membership pagination")
            read += len(ids)
            if read >= total:
                return False
            if not ids:
                raise ValueError("Incomplete series membership pagination")
        raise ValueError("Series membership too large to verify completely")

    async def archive(self, publication, session):
        if session.get("mode") != "live":
            return self._result("skipped")
        if publication.get("status") != "published":
            return self._result("waiting_for_publication")
        try:
            aid = positive_id(publication.get("aid"))
            plan = streamer_series_plan(session, publication.get("account_uid"))
        except (ValueError, TypeError):
            return self._result("blocked", error="主播身份或投稿账号缺失/不一致，无法确定系列")
        async with self.slot:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    key = f"{plan['key']}:{aid}"
                    self._busy_checks[key] = self._busy_checks.get(key, 0) + 1
                    retryable = self._busy_checks[key] < self.MAX_RECHECKS
                    return self._result("busy", plan, retryable=retryable,
                                        next_check_at=self.clock() + self.RECHECK_SECONDS if retryable else None)
                try:
                    return await self._archive_locked(publication, aid, plan)
                except asyncio.CancelledError:
                    # Creating/adding was persisted before any write. Reopening
                    # the registry will reconcile that checkpoint using GET.
                    raise
                except Exception:
                    return self._result("failed", plan, "系列归档记录无法读取或写入；视频投稿状态不受影响")
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    async def _archive_locked(self, publication, aid, plan):
        registry = self._load()
        group = registry["groups"].setdefault(plan["key"], {
            **plan, "creation_state": "planned", "members": {}, "created_at": self.clock(),
        })
        if (group.get("account_uid") != plan["account_uid"]
                or group.get("streamer_uid") != plan["streamer_uid"]):
            return self._result("blocked", plan, "系列归档记录的账号或主播身份不匹配")
        member = group["members"].setdefault(str(aid), {
            "publication_id": publication.get("id"), "phase": "planned", "checks": 0,
        })
        old = member.get("result") or {}
        creating = not group.get("platform_series_id") and group.get("creation_state") in {
            "creating", "create_pending", "create_uncertain",
        }
        if creating and group.get("creation_result"):
            old = group["creation_result"]
        if old.get("status") in {"added", "failed", "blocked"}:
            return old
        if old and (not old.get("retryable") or self.clock() < (old.get("next_check_at") or 0)):
            return old
        if old:
            if creating:
                group["creation_checks"] = group.get("creation_checks", 0) + 1
            else:
                member["checks"] += 1
        self._save(registry)
        async with self.client_factory(self.settings.upload_cookie()) as client:
            try:
                account = await client.account()
                if not account.get("logged_in") or account.get("uid") != group["account_uid"]:
                    return self._finish(registry, group, member, "blocked",
                                        "当前登录账号与该稿件投稿账号不一致")
                if not group.get("platform_series_id"):
                    matches = await self._find_existing(client, group)
                    if len(matches) > 1:
                        return self._finish(registry, group, member, "blocked",
                                            "发现多个包含同一主播身份的系列，请人工核对")
                    if matches:
                        group.update(platform_series_id=next(iter(matches)), creation_state="active")
                        self._save(registry)
                    elif group["creation_state"] in {"creating", "create_pending", "create_uncertain"}:
                        return self._finish(registry, group, member, "uncertain",
                                            "系列创建结果尚未查到；已停止重复创建", reconcile=True)
                    elif group["creation_state"] == "failed":
                        return self._finish(registry, group, member, "failed",
                                            "此主播系列的创建已被明确拒绝，请检查平台原因后处理")
                    else:
                        group.update(creation_state="creating", first_aid=aid)
                        member["phase"] = "creating"
                        self._save(registry)
                        try:
                            # Do not depend on an unverified response ID schema.
                            await client.create_series(group["account_uid"], group["title"], [aid],
                                                       group["description"])
                            group["creation_state"] = "create_pending"
                            self._save(registry)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            uncertain = not isinstance(exc, PublishError) or exc.uncertain
                            group["creation_state"] = "create_uncertain" if uncertain else "failed"
                            return self._finish(registry, group, member,
                                                "uncertain" if uncertain else "failed",
                                                self._error(exc), reconcile=uncertain)
                        matches = await self._find_existing(client, group)
                        if len(matches) != 1:
                            return self._finish(registry, group, member, "uncertain",
                                                "创建响应已收到，系列仍待查询确认", reconcile=True)
                        group.update(platform_series_id=next(iter(matches)), creation_state="active")
                        self._save(registry)
                if await self._contains(client, group, aid):
                    return self._finish(registry, group, member, "added")
                if member["phase"] in {"creating", "adding", "add_uncertain"}:
                    return self._finish(registry, group, member, "uncertain",
                                        "稿件加入系列的结果仍待确认；仅查询，不重复添加", reconcile=True)
                member["phase"] = "adding"
                self._save(registry)
                try:
                    await client.append_series(group["account_uid"], group["platform_series_id"], [aid])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    uncertain = not isinstance(exc, PublishError) or exc.uncertain
                    member["phase"] = "add_uncertain" if uncertain else "failed"
                    return self._finish(registry, group, member, "uncertain" if uncertain else "failed",
                                        self._error(exc), reconcile=uncertain)
                if await self._contains(client, group, aid):
                    return self._finish(registry, group, member, "added")
                return self._finish(registry, group, member, "pending",
                                    "添加响应已收到，等待系列成员查询确认", reconcile=True)
            except asyncio.CancelledError:
                raise
            except ValueError:
                return self._finish(registry, group, member, "blocked",
                                    "系列所有权、身份或分页数据无法可靠核对")
            except Exception as exc:
                ambiguous = (group["creation_state"] in {"creating", "create_pending", "create_uncertain"}
                             or member["phase"] in {"creating", "adding", "add_uncertain"})
                return self._finish(registry, group, member, "uncertain" if ambiguous else "pending",
                                    self._error(exc), reconcile=True)


def ugc_create_payload(title, description, uploaded_cover_url):
    """Pure request builder from official UI; never submits or checks permission."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Collection title is required")
    if not isinstance(description, str):
        raise ValueError("Description must be text")
    parsed = urlsplit(uploaded_cover_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or not any(parsed.hostname.endswith("." + suffix) for suffix in ("hdslb.com", "biliimg.com"))
            or parsed.query or parsed.fragment):
        raise ValueError("Use an existing Bilibili-uploaded cover URL")
    return {"cover": uploaded_cover_url, "title": title.strip(), "desc": description, "season_price": 0}


def ugc_append_payload(section_id, aid, cid, title):
    """Pure schema for one normal free episode; no automatic cross-list movement."""
    if not isinstance(title, str):
        raise ValueError("Episode title must be text")
    return {"sectionId": positive_id(section_id), "episodes": [
        {"title": title, "aid": positive_id(aid), "cid": positive_id(cid),
         "charging_pay": 0, "member_first": 0, "limited_free": False}
    ]}
