"""Durable publishing workflow. Media upload and final archive submission are distinct."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import math
from pathlib import Path
import re
import shutil
import tempfile
import time
import unicodedata
from urllib.parse import urlparse


PROTECTED_STATES = {"queued", "uploading", "submitting", "submitted", "published", "private", "rejected", "uncertain"}
MIN_PUBLICATION_DURATION = 60.0
PUBLICATION_DIMENSIONS = {(1920, 1080), (1080, 1920)}


def validate_publication_duration(value):
    """Use an inclusive, unrounded cutoff; absent/invalid durations fail closed."""
    try:
        if isinstance(value, bool):
            raise ValueError
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("无法确认成片时长；只允许投稿至少60秒的切片") from None
    if not math.isfinite(duration) or duration < MIN_PUBLICATION_DURATION:
        raise ValueError("只允许投稿至少60秒的切片，请检查成片实际时长")
    return duration


async def validate_publication_media(settings, path):
    """Reinspect the current file before uploads, including previously queued jobs."""
    from .render import probe
    try:
        media = await probe(settings, path)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Probe errors may contain internal paths or untrusted media metadata.
        raise ValueError("无法读取投稿成片，请重新导出后再试") from None
    duration = validate_publication_duration(media.get("duration"))
    dimensions = (media.get("width"), media.get("height"))
    if dimensions not in PUBLICATION_DIMENSIONS:
        raise ValueError("只允许投稿1080P成片（1920×1080或1080×1920），请重新导出")
    return {"duration": duration, "width": dimensions[0], "height": dimensions[1]}


def public_source(value):
    value = str(value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and len(value) <= 200:
        return value
    return ""


def _text(value, maximum=200):
    return value.strip()[:maximum] if isinstance(value, str) else ""


def _positive_id(value):
    text = str(value).strip()
    return text if re.fullmatch(r"[1-9]\d*", text) else ""


def _bili_url(value, host):
    value = public_source(value)
    parsed = urlparse(value)
    if (parsed.hostname == host and re.fullmatch(r"/[1-9]\d*/?", parsed.path)
            and not parsed.query and not parsed.fragment):
        return value.rstrip("/")
    return ""


def _source_fields(anchor, session, profile):
    """Keep field provenance separate from the concise public description."""
    room = session.get("room") if isinstance(session.get("room"), dict) else {}
    fields = {}

    def put(key, value, source):
        if value:
            fields[key] = {"value": value, "source": source}

    for value, source in [(room.get("uname"), "session.room.uname"),
                          (session.get("streamer"), "session.streamer"),
                          (profile.get("creator_name"), "session.publish_profile.creator_name")]:
        name = _text(value, 100)
        if name:
            put("creator_name", name, source)
            break
    for value, source in [(room.get("owner_uid"), "session.room.owner_uid"),
                          (session.get("streamer_uid"), "session.streamer_uid"),
                          (profile.get("creator_uid"), "session.publish_profile.creator_uid")]:
        uid = _positive_id(value)
        if uid:
            put("creator_uid", uid, source)
            put("homepage", f"https://space.bilibili.com/{uid}", source)
            break
    # User-configured source remains authoritative for explicitly imported material.
    source = public_source(profile.get("source")) or public_source(session.get("source_url"))
    source_basis = "session.publish_profile.source" if public_source(profile.get("source")) else "session.source_url"
    put("source", source, source_basis)
    if _bili_url(source, "live.bilibili.com"):
        put("live_room", _bili_url(source, "live.bilibili.com"), source_basis)
    elif room_id := _positive_id(room.get("room_id")):
        put("live_room", f"https://live.bilibili.com/{room_id}", "session.room.room_id")
    if not fields.get("homepage"):
        put("homepage", _bili_url(profile.get("creator_homepage"), "space.bilibili.com"),
            "session.publish_profile.creator_homepage")

    # Import/upload timestamps say nothing about when a recorded broadcast happened.
    if session.get("mode") == "live":
        try:
            created = float(session["created_at"])
            start, end = float(anchor.get("start", 0)), float(anchor.get("end", 0))
            if not all(math.isfinite(x) for x in (created, start, end)) or created <= 0 or not 0 <= start <= end:
                raise ValueError("Invalid capture clock")
            dates = [datetime.fromtimestamp(created + offset, timezone(timedelta(hours=8))).date()
                     for offset in (start, end)]
            labels = [f"{d.year}年{d.month}月{d.day}日" for d in dates]
            label = labels[0] if dates[0] == dates[1] else "—".join(labels)
            fields["capture_date"] = {
                "value": label, "source": "session.created_at + anchor.start/end",
                "timezone": "Asia/Shanghai", "created_at": created,
                "anchor_start": start, "anchor_end": end,
                "meaning": "本次直播采集日期，不是主播开播日期",
            }
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            pass
    return fields


TOPIC_PATTERNS = [
    ("CS2", r"(?<![A-Za-z0-9])CS\s*2(?![A-Za-z0-9])|反恐精英\s*2"),
    ("无畏契约", r"无畏契约|(?<![A-Za-z])VALORANT(?![A-Za-z])"),
    ("英雄联盟", r"英雄联盟"),
    ("金铲铲之战", r"金铲铲之战"),
    ("魔兽世界", r"魔兽世界"),
    ("动物园之星", r"动物园之星"),
    ("我的世界", r"(?:玩|游戏|启动|打开).{0,6}我的世界|(?<![A-Za-z])Minecraft(?![A-Za-z])"),
    ("原神", r"原神"),
    ("方言", r"方言|四川话|重庆话|粤语|东北话|上海话|闽南语"),
]


def _tag_evidence(anchor, profile, fields, snapshot):
    entries, seen = [], set()

    def add(tag, category, source, **evidence):
        tag = _text(tag, 100)
        normalized = unicodedata.normalize("NFKC", tag).casefold()
        if (not tag or len(tag) > 20 or re.search(r"[,\n\r，]", tag)
                or normalized in seen or len(entries) >= 10):
            return
        seen.add(normalized)
        entries.append({"tag": tag, "category": category, "source": source, **evidence})

    if creator := fields.get("creator_name"):
        add(creator["value"], "creator", creator["source"])
    add("直播切片", "format", "current clip export")
    configured = profile.get("tags")
    if isinstance(configured, list):
        for index, tag in enumerate(configured):
            add(tag, "user_configured", f"session.publish_profile.tags[{index}]")
    ids = set(x for x in anchor.get("evidence_ids", []) if isinstance(x, str))
    verified = []
    for event in snapshot if isinstance(snapshot, list) else []:
        if not isinstance(event, dict) or event.get("id") not in ids or event.get("kind") not in {"transcript", "danmaku"}:
            continue
        try:
            start, end = float(event["start"]), float(event["end"])
            if (not math.isfinite(start) or not math.isfinite(end) or end < start
                    or start < float(anchor["start"]) - 0.05 or end > float(anchor["end"]) + 0.05):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        text = _text(event.get("text"), 5000)
        if text:
            verified.append((event["id"], text))
    for tag, pattern in TOPIC_PATTERNS:
        matches = [(eid, match.group()) for eid, text in verified
                   if (match := re.search(pattern, text, re.IGNORECASE))]
        if matches:
            add(tag, "topic", "job.evidence_snapshot", evidence_ids=[x[0] for x in matches],
                matched_text=[x[1] for x in matches])
    return entries


def publication_metadata_evidence(job, session):
    """Trace metadata to verified session fields, user settings and saved events.

    evidence_snapshot must be populated from Store.events by the renderer. A model
    title/reason or a quote without its saved source row never supplies topic tags.
    """
    anchor = job["anchor"]
    profile = session.get("publish_profile") or {}
    fields = _source_fields(anchor, session, profile)
    entries = _tag_evidence(anchor, profile, fields, job.get("evidence_snapshot"))
    return {"version": 1, "source_fields": fields, "tags": entries,
            "omissions": [key for key in ["creator_name", "homepage", "live_room", "capture_date"]
                          if key not in fields]}


def tags_for(anchor, profile, session=None, evidence_snapshot=None):
    session = {**(session or {}), "publish_profile": profile}
    result = publication_metadata_evidence({"anchor": anchor, "evidence_snapshot": evidence_snapshot}, session)
    return [item["tag"] for item in result["tags"]]


DESCRIPTION_STYLES = {"profile", "source_links", "credit_date", "date_links", "legacy"}


def description_style_for(creator_name, configured=None):
    if isinstance(configured, str) and configured in DESCRIPTION_STYLES:
        return configured
    if creator_name in {"阿梓", "阿梓从小就很可爱"}:
        return "profile"
    if creator_name == "七海Nana7mi":
        return "date_links"
    return "credit_date"


def public_description_from_fields(fields, *, follow_creator=False, style="legacy"):
    """Render already verified public source values, without session/user prose.

    Callers supply the allowlisted field values rather than a session, job or
    profile. This shared formatter never copies unrelated fields into public
    copy; local user descriptions remain a separate, editable addition.
    """
    lines = []
    creator = fields.get("creator_name", "")
    homepage = fields.get("homepage", "")
    live_room = fields.get("live_room", "")
    date = fields.get("capture_date", "")
    if style == "profile":
        return (creator + ("：" + homepage if homepage else "") if creator else homepage).strip()
    if style == "source_links":
        if homepage:
            lines.append(f"{creator}的个人主页：{homepage}" if creator else homepage)
        if live_room:
            lines.append(f"{creator}的直播间：{live_room}" if creator else f"直播间：{live_room}")
        return "\n".join(lines) or creator
    if style == "credit_date":
        if creator:
            lines.append(("欢迎关注👉 @" if follow_creator else "") + creator
                         + ("  " + homepage if homepage else ""))
        elif homepage:
            lines.append(homepage)
        elif live_room:
            lines.append("直播间：" + live_room)
        if date:
            if lines:
                lines.append("")
            lines.append(f"素材：{date}直播")
        return "\n".join(lines)
    if style == "date_links":
        if date:
            lines.append(f"▷{date}直播")
        if homepage:
            lines.append(f"▷{creator}的空间：{homepage}")
        if live_room:
            lines.append(f"▷{creator}的直播间：{live_room}")
        if creator and follow_creator:
            lines.append(f"▷欢迎关注@{creator}")
        return "\n".join(lines) or creator
    if creator:
        credit = "欢迎关注 @" + creator if follow_creator else creator
        lines.append(credit + ("：" + homepage if homepage else ""))
    elif homepage:
        lines.append(homepage)
    if fields.get("live_room"):
        lines.append("直播间：" + fields["live_room"])
    source = fields.get("source", "")
    if source and source not in [homepage, fields.get("live_room")]:
        lines.append("素材来源：" + source)
    if fields.get("capture_date"):
        # These dates identify when this live excerpt was captured. They do not
        # claim when the broadcaster went live; retain that distinction in the
        # source-field provenance rather than adding it to the public copy.
        lines.extend(["", f"素材：{fields['capture_date']}直播"])
    return "\n".join(lines).strip()


def metadata_for(job, session):
    from .titles import format_clip_title
    a = job["anchor"]
    profile = session.get("publish_profile") or {}
    title = format_clip_title(_text(profile.get("title_prefix"), 30) + _text(a.get("title"), 120), session)
    evidence = publication_metadata_evidence(job, session)
    fields = evidence["source_fields"]
    source = fields.get("source", {}).get("value", "")
    lines = [public_description_from_fields(
        {key: fields[key]["value"] for key in ("creator_name", "homepage", "live_room", "source", "capture_date")
         if key in fields}, follow_creator=bool(profile.get("follow_creator", session.get("mode") == "live")),
        style=description_style_for(fields.get("creator_name", {}).get("value", ""), profile.get("description_style"))
        if session.get("mode") == "live" else "legacy")]
    if description := _text(profile.get("description"), 2000):
        lines.extend(["", description])
    return {"title": title, "description": "\n".join(lines).strip()[:2000],
            "tags": [entry["tag"] for entry in evidence["tags"]],
            "tid": profile.get("tid", 0), "copyright": profile.get("copyright", 2), "source": source}


def validate_metadata(value):
    result = {}
    for key, maximum in [("title", 80), ("description", 2000), ("source", 200)]:
        result[key] = str(value.get(key) or "").strip()
        if len(result[key]) > maximum:
            raise ValueError(f"{key} 超过 {maximum} 字符")
    if not result["title"]:
        raise ValueError("请填写投稿标题")
    result["tid"] = int(value.get("tid") or 0)
    if result["tid"] <= 0:
        raise ValueError("请选择B站投稿分区")
    result["copyright"] = int(value.get("copyright") or 0)
    if result["copyright"] not in {1, 2}:
        raise ValueError("请选择原创或转载")
    if result["copyright"] == 2 and not public_source(result["source"]):
        raise ValueError("转载投稿需要填写可访问的素材来源链接")
    tags = value.get("tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 10:
        raise ValueError("投稿标签需为 1–10 项")
    result["tags"] = list(dict.fromkeys(str(x).strip() for x in tags))
    if any(not x or len(x) > 20 or re.search(r"[,\n\r，]", x) for x in result["tags"]):
        raise ValueError("每个标签需为 1–20 字符，且不能含逗号或换行")
    return result


class PublicationManager:
    def __init__(self, settings, store, client_factory=None, series_manager=None):
        if client_factory is None:
            from .bili_publish import BiliPublisher
            client_factory = BiliPublisher
        self.settings, self.store, self.client_factory = settings, store, client_factory
        self.slot = asyncio.Semaphore(1)
        self._categories = (0, [])
        self._account = (0, {})
        self.series_manager = series_manager

    def client(self):
        return self.client_factory(self.settings.upload_cookie())

    async def account(self, fresh=False):
        if not fresh and time.monotonic() - self._account[0] < 30:
            return self._account[1]
        if not self.settings.upload_cookie():
            result = {"configured": False, "logged_in": False, "error": "尚未配置本机投稿Cookie"}
        else:
            async with self.client() as client:
                result = await client.account()
        self._account = (time.monotonic(), result)
        return result

    async def categories(self):
        if time.monotonic() - self._categories[0] < 600 and self._categories[1]:
            return self._categories[1]
        async with self.client() as client:
            result = await client.categories()
        self._categories = (time.monotonic(), result)
        return result

    def preview(self, job):
        if job["status"] != "complete":
            raise ValueError("成片完成后才能准备投稿")
        session = self.store.session(job["session_id"])
        metadata = metadata_for(job, session)
        evidence = publication_metadata_evidence(job, session)
        warnings = []
        if not metadata["source"] and metadata["copyright"] == 2:
            warnings.append("请填写原直播或视频来源链接")
        if not metadata["tid"]:
            warnings.append("请选择与本条内容相符的分区")
        if not evidence["source_fields"].get("creator_name"):
            warnings.append("未设置主播名；不会自动猜测主播身份或添加姓名标签")
        return {"metadata": metadata, "metadata_evidence": evidence, "warnings": warnings}

    async def validate(self, metadata):
        metadata = validate_metadata(metadata)
        account = await self.account()
        if not account.get("logged_in"):
            raise ValueError(account.get("error") or "投稿账号登录已失效")
        categories = await self.categories()
        if metadata["tid"] not in {x["id"] for x in categories}:
            raise ValueError("该投稿分区当前不可用，请重新选择")
        return metadata

    def enqueue(self, job, metadata, automatic=False, expected_media=None, expected_account_uid=None):
        if job["status"] != "complete":
            raise ValueError("只有已完成的成片可以投稿")
        validate_publication_duration(job.get("duration"))
        metadata = validate_metadata(metadata)
        old = self.store.publication_for_job(job["id"])
        if old and old["status"] in PROTECTED_STATES:
            raise ValueError("此成片已有投稿任务，请查看现有结果，避免重复投稿")
        for other in self.store.publications():
            if (other.get("anchor_id") == job["anchor_id"] and
                    other["job_id"] != job["id"] and other["status"] in PROTECTED_STATES):
                raise ValueError("这个锚点的其他导出已有投稿记录，请先查看原稿件")
        from .publication_overlap import validate_no_overlap
        capture = validate_no_overlap(self.store, job, publication_id=old["id"] if old else None)
        folder = self.settings.data_dir / "exports" / job["id"]
        if not (folder / "clip.mp4").is_file():
            raise ValueError("成片文件不存在")
        if expected_media is not None:
            if (not isinstance(expected_media, dict)
                    or set(expected_media) != {"video_sha256", "cover_sha256"}
                    or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                           for v in expected_media.values())):
                raise ValueError("审核文件校验信息不完整，请重新审核")
            expected_media = dict(expected_media)
        if expected_account_uid is not None:
            expected_account_uid = _positive_id(expected_account_uid)
            if not expected_account_uid:
                raise ValueError("无法确认目标投稿账号")
        values = {"metadata": metadata, "title": metadata["title"], "anchor_id": job["anchor_id"],
                  "session_id": job["session_id"], "automatic": automatic, "status": "queued",
                  "progress": 0, "error": None, "capture_span": capture,
                  "expected_media": expected_media, "expected_account_uid": expected_account_uid}
        if old:
            return self.store.update_publication(old["id"], **values)
        return self.store.create_publication(job["id"], **values)

    async def run(self, pid):
        from .bili_publish import PublishError
        async with self.slot:
            p = self.store.publication(pid)
            if not p or p["status"] != "queued":
                return
            folder = self.settings.data_dir / "exports" / p["job_id"]
            frozen = None
            try:
                # The private copies are the exact files both inspected and uploaded.
                # Renderers may atomically replace exports while network uploads wait.
                frozen = tempfile.TemporaryDirectory(prefix="publication-", dir=self.settings.data_dir)
                source_folder, folder = folder, Path(frozen.name)
                await asyncio.to_thread(shutil.copyfile, source_folder / "clip.mp4", folder / "clip.mp4")
                if (source_folder / "cover.jpg").is_file():
                    await asyncio.to_thread(shutil.copyfile, source_folder / "cover.jpg", folder / "cover.jpg")
                digest = await asyncio.to_thread(self._digest, folder / "clip.mp4")
                cover_path = folder / "cover.jpg"
                cover_digest = await asyncio.to_thread(self._digest, cover_path) if cover_path.is_file() else None
                actual_media = {"video_sha256": digest, "cover_sha256": cover_digest}
                if p.get("expected_media") is not None and p["expected_media"] != actual_media:
                    raise ValueError("成片或封面在审核后发生变化，请重新审核后投稿")
                media = await validate_publication_media(self.settings, folder / "clip.mp4")
                from .publication_overlap import validate_no_overlap
                job = self.store.job(p["job_id"])
                if not job:
                    raise ValueError("投稿任务的源切片记录缺失，暂不投稿")
                capture = validate_no_overlap(self.store, job, publication_id=pid, video_sha256=digest)
                if p.get("capture_span") and p["capture_span"] != capture:
                    raise ValueError("入队后切片源时间范围发生变化，请重新核对")
                await self.validate(p["metadata"])
                async with self.client() as client:
                    account = await client.account()
                    if not account.get("logged_in"):
                        raise ValueError("投稿账号登录已失效")
                    if (p.get("expected_account_uid") is not None
                            and str(account.get("uid")) != p["expected_account_uid"]):
                        raise ValueError("投稿账号已变化，已阻止向其他账号投稿")
                    same_account = p.get("account_uid") == account["uid"]
                    reset_uploads = {} if same_account else {
                        "uploaded_video": None, "video_sha256": None,
                        "uploaded_cover": None, "cover_sha256": None,
                    }
                    self.store.update_publication(pid, status="uploading", progress=2,
                                                  account_uid=account["uid"], account_name=account["name"],
                                                  media_validation=media,
                                                  **reset_uploads)

                    async def progress(done, total):
                        self.store.update_publication(pid, progress=5 + int(75 * done / max(1, total)))

                    # Confirmed uploads may be reused after a definite submission rejection.
                    video = p.get("uploaded_video") if same_account else None
                    if not video or p.get("video_sha256") != digest:
                        video = await client.upload_video(folder / "clip.mp4", progress)
                        self.store.update_publication(pid, uploaded_video=video, video_sha256=digest)
                    cover = (p.get("uploaded_cover") or "") if same_account and p.get("cover_sha256") == cover_digest else ""
                    if not cover and cover_digest:
                        cover = await client.upload_cover(cover_path)
                        self.store.update_publication(pid, uploaded_cover=cover, cover_sha256=cover_digest)
                    # Activities are optional and checked against the current
                    # account immediately before submitting. Keep the reviewed
                    # base copy intact; record the separately authorized tag
                    # addition and mission/topic fields with both hashes.
                    from .bili_activities import digest as metadata_digest, submission_activity
                    from .bili_publish import BiliPublisher
                    submitted_metadata, activity = await submission_activity(
                        client, p, job, account["uid"], self.settings.data_dir)
                    if metadata_digest(self.store.publication(pid).get("metadata")) != activity["metadata_sha256"]:
                        raise ValueError("投稿资料在活动核验期间发生变化，请重新审核")
                    activity["submission_payload_sha256"] = metadata_digest(
                        BiliPublisher.submission_payload(submitted_metadata, video, cover))
                    self.store.update_publication(pid, activity_selection=activity,
                                                  submission_metadata=submitted_metadata)
                    # This checkpoint must be durable before the non-idempotent HTTP call.
                    self.store.update_publication(pid, status="submitting", progress=90)
                    result = await client.submit(submitted_metadata, video, cover)
                    if not result.get("aid") or not result.get("bvid"):
                        raise PublishError("投稿响应缺少稿件标识，需在创作中心核实", uncertain=True)
                    self.store.update_publication(pid, status="submitted", progress=100,
                                                  aid=result["aid"], bvid=result["bvid"],
                                                  url=f"https://www.bilibili.com/video/{result['bvid']}/",
                                                  submitted_at=time.time(), error=None,
                                                  platform_message="已提交，等待B站审核")
            except asyncio.CancelledError:
                status = "uncertain" if self.store.publication(pid)["status"] == "submitting" else "failed"
                self.store.update_publication(pid, status=status, error="任务中断；提交结果待核实" if status == "uncertain" else "上传已中断，尚未提交")
                raise
            except PublishError as exc:
                self.store.update_publication(pid, status="uncertain" if exc.uncertain else "failed",
                                              error=str(exc), platform_code=exc.code)
            except Exception as exc:
                # Do not leak third-party exceptions containing URLs, cookies or headers.
                status = "uncertain" if self.store.publication(pid)["status"] == "submitting" else "failed"
                message = str(exc) if isinstance(exc, ValueError) else "投稿处理失败，请检查账号状态后重试"
                self.store.update_publication(pid, status=status, error=message)
            finally:
                if frozen is not None:
                    frozen.cleanup()

    @staticmethod
    def _digest(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as file:
            while data := file.read(1024 * 1024):
                digest.update(data)
        return digest.hexdigest()

    async def refresh(self, pid):
        p = self.store.publication(pid)
        if not p.get("aid"):
            return p
        async with self.client() as client:
            result = await client.status(p["aid"])
        result = {key: value for key, value in result.items()
                  if key in {"status", "platform_state", "platform_message", "bvid", "url"}}
        self.store.update_publication(pid, **result, checked_at=time.time())
        return await self.archive_series(pid)

    async def archive_series(self, pid):
        """Series errors cannot change a successful video's publication state."""
        p = self.store.publication(pid)
        session = self.store.session(p["session_id"]) if p.get("session_id") else None
        if p.get("status") != "published" or not session or session.get("mode") != "live":
            return p
        if self.series_manager is None:
            from .bili_series import StreamerSeriesManager
            self.series_manager = StreamerSeriesManager(self.settings)
        try:
            result = await self.series_manager.archive(p, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never expose third-party URLs, cookies, or exception text.
            result = {"status": "failed", "error": "稿件已发布，系列归档未完成；可刷新结果核对",
                      "retryable": False}
        return self.store.update_publication(pid, series=result, series_checked_at=time.time(),
                                             series_checks=int(p.get("series_checks", 0)) + 1)

    def series_due(self, publication):
        """Bounded recovery for interrupted grouping, independent of video upload."""
        p = publication
        if p.get("status") != "published" or not p.get("aid") or not p.get("session_id"):
            return False
        session = self.store.session(p["session_id"])
        if not session or session.get("mode") != "live":
            return False
        series = p.get("series") or {}
        status = series.get("status", "pending")
        return (status in {"pending", "busy", "uncertain"}
                and series.get("retryable", True)
                and time.time() >= float(series.get("next_check_at") or 0))
