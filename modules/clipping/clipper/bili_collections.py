"""Durable, real UGC collections; ordinary video series are never success here.

Request fields verified against the official creator-center 3543/2452 bundles
on 2026-10-21. Evidence: data/publication-batches/all-remaining-20261021/
collection-protocol/evidence.json. A write intent is fsynced before every POST;
ambiguous results are reconciled with GET only, including after a restart.
"""
import asyncio
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .bili_publish import PublishError
from .bili_series import (
    BiliSeriesInspector, OFFICIAL_CREATOR_BUNDLE, OFFICIAL_CREATOR_UI,
    positive_id, streamer_series_plan, ugc_append_payload, ugc_create_payload,
)

PERMISSION_SECONDS = 600
SECTION_TITLE = "直播切片"
PERMISSION_SOURCES = {
    "whitelist": "https://member.bilibili.com/x/vupre/web/archive/white",
    "remaining": "https://member.bilibili.com/x2/creative/web/season/remain",
    "rating": "https://api.bilibili.com/studio/up-rating/v3/rating/status",
    "official_guide": "https://www.bilibili.com/read/cv14415006",
}


class BiliCollectionClient(BiliSeriesInspector):
    async def collection_capability(self):
        white = (await self._request("GET", "/x/vupre/web/archive/white")).get("data")
        if not isinstance(white, dict) or not isinstance(white.get("season"), bool):
            raise ValueError("Unknown collection whitelist schema")
        remaining = await self.ugc_remaining()
        # Rating explains the account's state; the live whitelist/quota gates
        # writes. A below-threshold exception granted by Bilibili is respected.
        rating, rating_error = {}, None
        try:
            rating = (await self._request("GET", "/studio/up-rating/v3/rating/status", api=True)).get("data")
            if not isinstance(rating, dict):
                raise ValueError("Unknown rating schema")
        except Exception as exc:
            rating, rating_error = {}, _safe_error(exc)
        # The official UI checks remaining only in onAddEp (new season).
        # onVideoAddComplete uses episode verification and append without this
        # creation quota. Existing owned collections can still be maintained.
        enabled = white["season"]
        can_create = enabled and remaining > 0
        return {
            "allowed": enabled, "can_manage_existing": enabled, "can_create": can_create,
            "status": ("available" if can_create else "available_existing_only") if enabled else "blocked_permission",
            "season": white["season"], "new_season": white.get("new_season"),
            "remaining": remaining, "level": rating.get("level"),
            "rating_status": rating.get("status"), "rating_error": rating_error,
            "reason": (None if enabled else "当前账号未获得创建真实合集的权限或可用额度"),
            "sources": PERMISSION_SOURCES,
        }

    async def create_collection(self, title, description, cover):
        payload = ugc_create_payload(title, description, cover)
        return (await self._request("POST", "/x2/creative/web/season/add", final_submit=True,
                    params={"csrf": self._require_cookie(csrf=True)}, json=payload)).get("data")

    async def create_section(self, season_id, title=SECTION_TITLE):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("Section title required")
        return (await self._request("POST", "/x2/creative/web/season/section/add", final_submit=True,
                    params={"csrf": self._require_cookie(csrf=True)},
                    json={"type": 0, "seasonId": positive_id(season_id), "title": title})).get("data")

    async def verify_episode(self, aid):
        await self._request("GET", "/x2/creative/web/season/section/episode/verify",
                            params={"aid": positive_id(aid), "from": 0})

    async def append_episodes(self, section_id, episodes):
        if not episodes or len(episodes) > 50:
            raise ValueError("Add 1–50 explicitly selected episodes")
        payload = {"sectionId": positive_id(section_id), "episodes": []}
        for ep in episodes:
            payload["episodes"].extend(ugc_append_payload(section_id, ep["aid"], ep["cid"], ep["title"])["episodes"])
        await self._request("POST", "/x2/creative/web/season/section/episodes/add", final_submit=True,
                            params={"csrf": self._require_cookie(csrf=True)}, json=payload)


def _safe_error(exc):
    # Never serialize exception text: transport errors can contain credentials.
    if isinstance(exc, PublishError) and exc.code is not None:
        return {"kind": "platform_rejected", "code": exc.code}
    return {"kind": "unconfirmed_response", "type": type(exc).__name__}


def _matches(meta, group):
    identities = set(re.findall(r"https?://space\.bilibili\.com/([1-9]\d*)(?=$|[/\s?#])",
                                str(meta.get("desc", ""))))
    owner = meta.get("mid")
    return (identities == {str(group["streamer_uid"])}
            and (owner is None or positive_id(owner) == group["account_uid"]))


def published_collection_groups(publications, sessions):
    """Build all-history intent from verified live sessions, deduplicated by AID.

    This deliberately ignores ordinary-series IDs and historical clip duration:
    the user's new submission threshold does not delete old public archives.
    """
    sessions = sessions if isinstance(sessions, dict) else {s["id"]: s for s in sessions}
    groups, excluded, seen = {}, [], {}
    for pub in publications:
        if pub.get("status") != "published":
            continue
        session = sessions.get(pub.get("session_id")) or {}
        if session.get("mode") != "live":
            continue
        try:
            plan = streamer_series_plan(session, pub.get("account_uid"))
            aid = positive_id(pub.get("aid"))
            # AID cannot be assigned to two streamer identities in one account.
            duplicate_key = (plan["account_uid"], aid)
            if duplicate_key in seen and seen[duplicate_key] != plan["key"]:
                raise ValueError("Conflicting streamer identity for one archive")
            seen[duplicate_key] = plan["key"]
            group = groups.setdefault(plan["key"], {
                "key": plan["key"], "account_uid": plan["account_uid"],
                "streamer_uid": plan["streamer_uid"], "streamer_name": plan["streamer_name"],
                "title": plan["title"][:20], "description": plan["description"],
                "platform_kind": "ugc_collection", "members": {},
            })
            metadata = pub.get("metadata") or {}
            raw_cid = (pub.get("uploaded_video") or {}).get("cid")
            try:
                cid = positive_id(raw_cid)
            except (TypeError, ValueError):
                cid = None
            group["members"][str(aid)] = {
                "aid": aid, "cid": cid, "title": pub.get("title") or metadata.get("title") or "",
                "publication_id": pub.get("id"), "session_id": pub.get("session_id"),
                "bvid": pub.get("bvid"), "cover": pub.get("uploaded_cover"),
                "published_at": pub.get("submitted_at") or pub.get("created_at") or 0,
            }
        except (TypeError, ValueError):
            excluded.append({"publication_id": pub.get("id"), "reason": "invalid_or_conflicting_verified_identity"})
    # Identity conflicts remove the AID from every group, never silently choose.
    conflicts = {e["publication_id"] for e in excluded}
    for pub in publications:
        if pub.get("id") in conflicts:
            for group in groups.values():
                group["members"].pop(str(pub.get("aid")), None)
    return groups, excluded


class StreamerCollectionManager:
    """Call reconcile with the full publication/session history at any cadence.

    Capability is checked at most every ten minutes. Completed membership and
    uncertain writes are checked at the same interval; new public archives may
    be appended sooner while capability remains valid. snapshot() is read-only.
    """
    def __init__(self, settings, client_factory=None, clock=None):
        self.settings = settings
        self.client_factory = client_factory or BiliCollectionClient
        self.clock = clock or time.time
        self.directory = Path(settings.data_dir)
        self.path = self.directory / "streamer-collections.json"
        self.lock_path = self.directory / "streamer-collections.lock"
        self.slot = asyncio.Lock()

    def snapshot(self):
        if not self.path.exists():
            return {"version": 1, "platform_kind": "ugc_collection", "capabilities": {}, "groups": {}}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("groups"), dict):
            raise ValueError("Unsupported collection registry")
        return value

    def _save(self, registry):
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(registry, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        fd = os.open(self.directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _ingest(self, registry, desired):
        now = self.clock()
        for key, plan in desired.items():
            group = registry["groups"].setdefault(key, {
                **plan, "members": {}, "operations": {}, "created_at": now,
                "season_id": None, "section_id": None,
            })
            group["streamer_name"] = plan["streamer_name"]
            for aid, member in plan["members"].items():
                old = group["members"].get(aid, {})
                group["members"][aid] = {**old, **member, "phase": old.get("phase", "pending")}
            # Caller supplies the full history, so a newly private/removed
            # archive is no longer selected for a new append.
            group["desired_aids"] = sorted(int(a) for a in plan["members"])
            group["count"] = len(group["desired_aids"])
            group["added_aids"] = [a for a in group["desired_aids"] if group["members"][str(a)].get("phase") == "added"]
            group["pending_aids"] = [a for a in group["desired_aids"] if a not in group["added_aids"]]
            group["eligible"] = group["count"] >= 3
            if not group["eligible"]:
                group["status"] = "waiting_for_three_publications"
            elif not registry["capabilities"].get(str(group["account_uid"]), {}).get("allowed"):
                cap = registry["capabilities"].get(str(group["account_uid"]), {})
                group.update(status=cap.get("status", "pending_permission_check"), permission=deepcopy(cap),
                             next_check_at=cap.get("next_check_at", 0))
            group["updated_at"] = now
        for key, group in registry["groups"].items():
            if key not in desired:
                group.update(desired_aids=[], count=0, eligible=False, status="waiting_for_three_publications")

    async def reconcile(self, publications, sessions, *, allow_writes=True):
        """Persist pending history and reconcile actual UGC, without editing posts."""
        desired, excluded = published_collection_groups(publications, sessions)
        async with self.slot:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"status": "busy", "platform_kind": "ugc_collection"}
                try:
                    registry = self.snapshot()
                    self._ingest(registry, desired)
                    registry["excluded"] = excluded
                    registry["updated_at"] = self.clock()
                    self._save(registry)
                    accounts = {g["account_uid"] for g in registry["groups"].values() if g["eligible"]}
                    if not accounts:
                        return deepcopy(registry)
                    due = any(self.clock() >= registry["capabilities"].get(str(uid), {}).get("next_check_at", 0)
                              for uid in accounts)
                    active = any(registry["capabilities"].get(str(uid), {}).get("allowed") for uid in accounts)
                    if not due and not active:
                        return deepcopy(registry)
                    try:
                        async with self.client_factory(self.settings.upload_cookie()) as client:
                            account = await client.account()
                            uid = positive_id(account.get("uid"))
                            if not account.get("logged_in"):
                                raise ValueError("Account login not verified")
                            for candidate in accounts - {uid}:
                                registry["capabilities"][str(candidate)] = {
                                    "allowed": False, "status": "blocked_account_mismatch",
                                    "checked_at": self.clock(), "next_check_at": self.clock() + PERMISSION_SECONDS,
                                }
                            if uid in accounts:
                                cap = registry["capabilities"].get(str(uid), {})
                                if self.clock() >= cap.get("next_check_at", 0):
                                    try:
                                        cap = await client.collection_capability()
                                    except Exception as exc:
                                        cap = {"allowed": False, "status": "permission_check_failed", "error": _safe_error(exc)}
                                    cap.update(account_uid=uid, checked_at=self.clock(), next_check_at=self.clock() + PERMISSION_SECONDS)
                                    registry["capabilities"][str(uid)] = cap
                                    self._save(registry)
                                for group in registry["groups"].values():
                                    if not group["eligible"] or group["account_uid"] != uid:
                                        continue
                                    group["permission"] = deepcopy(cap)
                                    if not cap.get("allowed"):
                                        group.update(status=cap["status"], permission=deepcopy(cap),
                                                     next_check_at=cap["next_check_at"])
                                        continue
                                    if group.get("next_check_at", 0) > self.clock() and all(
                                            group["members"][str(a)].get("phase") == "added" for a in group["desired_aids"]):
                                        continue
                                    if group.get("status") == "uncertain" and group.get("next_check_at", 0) > self.clock():
                                        continue
                                    await self._reconcile_group(client, registry, group, allow_writes,
                                        can_create=cap.get("can_create", cap.get("remaining", 0) > 0))
                            for group in registry["groups"].values():
                                if group["eligible"] and group["account_uid"] != uid:
                                    group["status"] = "blocked_account_mismatch"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        registry["error"] = _safe_error(exc)
                        for candidate in accounts:
                            registry["capabilities"][str(candidate)] = {
                                "allowed": False, "status": "permission_check_failed", "error": _safe_error(exc),
                                "checked_at": self.clock(), "next_check_at": self.clock() + PERMISSION_SECONDS,
                            }
                        for group in registry["groups"].values():
                            if group["eligible"]:
                                group["status"] = "permission_check_failed"
                    self._save(registry)
                    return deepcopy(registry)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    async def _find_collection(self, client, group):
        matches, count = {}, 0
        for page in range(1, 101):
            response = await client.ugc_seasons(page=page, size=30)
            rows = response["seasons"]
            for row in rows:
                meta = row.get("season", {})
                if _matches(meta, group):
                    matches[positive_id(meta.get("id"))] = meta
            count += len(rows)
            if count >= response["total"]:
                if len(matches) > 1:
                    raise ValueError("Multiple collections claim the same streamer identity")
                return next(iter(matches), None)
            if not rows:
                raise ValueError("Incomplete collection pagination")
        raise ValueError("Collection pagination limit reached")

    async def _write_once(self, registry, group, key, kind, payload, callback, allow_writes):
        operations = group["operations"]
        if key in operations:
            # Even a crash between fsync and POST is treated as ambiguous.
            group["status"] = "uncertain" if operations[key]["phase"] != "rejected" else "blocked_operation"
            return False
        if not allow_writes:
            group["status"] = "pending_write_disabled"
            return False
        op = {"kind": kind, "payload": payload, "phase": "intent", "created_at": self.clock()}
        operations[key] = op
        group["status"] = "uncertain"
        self._save(registry)
        try:
            await callback()
            op["phase"] = "response_received"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            op["phase"] = "rejected" if isinstance(exc, PublishError) and not exc.uncertain else "uncertain"
            op["error"] = _safe_error(exc)
        self._save(registry)
        # A successful HTTP response alone never marks collection membership.
        return True

    async def _read_sections(self, client, group):
        detail = await client.ugc_season(group["season_id"])
        meta = detail.get("season") or {}
        if positive_id(meta.get("id")) != group["season_id"] or not _matches(meta, group):
            raise ValueError("Collection identity changed")
        if meta.get("is_pay"):
            raise ValueError("Paid collections are outside this workflow")
        sections = detail.get("sections") or {}
        rows = sections.get("sections")
        if not isinstance(rows, list) or sections.get("total", len(rows)) != len(rows):
            raise ValueError("Incomplete section list")
        group["platform_state"] = meta.get("state")
        members, section_meta = {}, []
        for row in rows:
            sid = positive_id(row.get("id"))
            section = await client.ugc_section(sid)
            sm = section.get("section") or {}
            if positive_id(sm.get("id")) != sid or positive_id(sm.get("seasonId")) != group["season_id"]:
                raise ValueError("Section ownership changed")
            eps = section.get("episodes")
            if not isinstance(eps, list):
                raise ValueError("Incomplete episode list")
            for ep in eps:
                members[positive_id(ep.get("aid"))] = {"cid": positive_id(ep.get("cid")), "section_id": sid}
            section_meta.append(sm)
        return section_meta, members

    async def _reconcile_group(self, client, registry, group, allow_writes, *, can_create=True):
        group["next_check_at"] = self.clock() + PERMISSION_SECONDS
        try:
            if not group.get("season_id"):
                group["season_id"] = await self._find_collection(client, group)
            if not group.get("season_id"):
                if not can_create:
                    group["status"] = "blocked_creation_quota"
                    return
                cover = next((m["cover"] for m in group["members"].values() if m.get("cover")), None)
                payload = ugc_create_payload(group["title"], group["description"], cover or "")
                posted = await self._write_once(registry, group, "create", "create_collection", payload,
                    lambda: client.create_collection(group["title"], group["description"], cover), allow_writes)
                if not posted:
                    return
                group["season_id"] = await self._find_collection(client, group)
                if not group["season_id"]:
                    return
            if "create" in group["operations"]:
                group["operations"]["create"]["phase"] = "confirmed"
            group["url"] = f"https://space.bilibili.com/{group['account_uid']}/lists/{group['season_id']}?type=season"
            self._save(registry)
            sections, members = await self._read_sections(client, group)
            for operation in group["operations"].values():
                if operation.get("kind") == "append_episodes" and all(
                        ep["aid"] in members and ep["cid"] == members[ep["aid"]]["cid"]
                        for ep in operation["payload"]["episodes"]):
                    operation["phase"] = "confirmed"
            selected = [s for s in sections if s.get("title") == SECTION_TITLE]
            if not selected and len(sections) == 1:
                selected = sections  # A newly created season may have a default section.
            if len(selected) > 1:
                raise ValueError("Ambiguous target section")
            if selected:
                group["section_id"] = positive_id(selected[0]["id"])
            else:
                payload = {"type": 0, "seasonId": group["season_id"], "title": SECTION_TITLE}
                posted = await self._write_once(registry, group, "section", "create_section", payload,
                    lambda: client.create_section(group["season_id"]), allow_writes)
                if not posted:
                    return
                sections, members = await self._read_sections(client, group)
                selected = [s for s in sections if s.get("title") == SECTION_TITLE]
                if len(selected) != 1:
                    return
                group["section_id"] = positive_id(selected[0]["id"])
            if "section" in group["operations"]:
                group["operations"]["section"]["phase"] = "confirmed"
            pending = []
            for aid in group["desired_aids"]:
                member = group["members"][str(aid)]
                if aid in members:
                    if member.get("cid") and member["cid"] != members[aid]["cid"]:
                        raise ValueError("Archive CID differs from collection episode")
                    member.update(phase="added", section_id=members[aid]["section_id"], verified_at=self.clock())
                elif member.get("phase") in {"adding", "uncertain", "rejected"}:
                    group["status"] = "uncertain" if member["phase"] != "rejected" else "blocked_operation"
                    return
                elif not member.get("cid") or not member.get("title"):
                    member["phase"] = "missing_episode_metadata"
                else:
                    try:
                        await client.verify_episode(aid)
                    except Exception as exc:
                        member.update(phase="blocked_episode", error=_safe_error(exc))
                        continue
                    pending.append(member)
            for start in range(0, len(pending), 50):
                batch = pending[start:start + 50]
                payload = {"sectionId": group["section_id"], "episodes": [
                    {"aid": m["aid"], "cid": m["cid"], "title": m["title"]} for m in batch]}
                key = "append:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                if not allow_writes:
                    group["status"] = "pending_write_disabled"
                    return
                for member in batch:
                    member["phase"] = "adding"
                await self._write_once(registry, group, key, "append_episodes", payload,
                    lambda batch=batch: client.append_episodes(group["section_id"], batch), allow_writes)
                _, members = await self._read_sections(client, group)
                for member in batch:
                    if member["aid"] in members and member["cid"] == members[member["aid"]]["cid"]:
                        member.update(phase="added", section_id=members[member["aid"]]["section_id"], verified_at=self.clock())
                    else:
                        member["phase"] = "rejected" if group["operations"][key]["phase"] == "rejected" else "uncertain"
                if all(m["phase"] == "added" for m in batch):
                    group["operations"][key]["phase"] = "confirmed"
                else:
                    group["status"] = "uncertain"
                    return
            phases = {group["members"][str(a)]["phase"] for a in group["desired_aids"]}
            group["status"] = "integrated" if phases == {"added"} else "pending_episode_metadata"
            group["added_aids"] = [a for a in group["desired_aids"] if group["members"][str(a)]["phase"] == "added"]
            group["pending_aids"] = [a for a in group["desired_aids"] if a not in group["added_aids"]]
            group["verified_at"] = self.clock()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            group["status"] = "blocked_verification" if isinstance(exc, ValueError) else "uncertain"
            group["error"] = _safe_error(exc)
        finally:
            self._save(registry)
