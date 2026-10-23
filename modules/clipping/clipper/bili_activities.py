"""Select a current uploader activity without silently rewriting reviewed copy.

The official uploader's topic API supersedes the empty legacy mission list.
An activity must be returned for this authenticated account, carry a positive
mission ID, and satisfy reviewed activity rules as well as the official category
check. At most one required topic tag is appended under the user's instruction;
the base reviewed metadata and the actual submission metadata are both bound.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from zoneinfo import ZoneInfo


VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def positive(value):
    return int(value) if not isinstance(value, bool) and re.fullmatch(r"[1-9]\d*", str(value)) else None


def normal(value):
    return unicodedata.normalize("NFKC", str(value)).casefold().strip()


def topic_candidates(rows, metadata, rules, job):
    """Current IDs come from B站; topic suitability comes from reviewed rules."""
    tags = {normal(tag) for tag in metadata.get("tags", [])}
    approved = {normal(rule.get("topic_name")): rule for rule in rules
                if isinstance(rule, dict) and rule.get("content_profile") == job.get("content_profile")
                and rule.get("reviewed") is True and rule.get("requires_external_signup") is False
                and rule.get("requires_additional_agreement") is False
                and isinstance(rule.get("required_content_tags"), list)
                and rule["required_content_tags"]
                and all(normal(tag) in tags for tag in rule["required_content_tags"])}
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        topic_id = positive(row.get("topic_id", row.get("id")))
        mission_id = positive(row.get("mission_id"))
        name = row.get("topic_name", row.get("name"))
        if (not topic_id or not mission_id or not isinstance(name, str)
                or not 1 <= len(name) <= 20 or normal(name) not in approved
                or re.search(r"[,，\r\n]", name) or row.get("state", 0) != 0):
            continue
        found[topic_id] = {"topic_id": topic_id, "mission_id": mission_id,
                           "topic_name": name, "rule": approved[normal(name)]}
    return sorted(found.values(), key=lambda row: row["topic_id"])


def automatic_source(publication, job):
    if publication.get("automatic") is True:
        return True
    provenance = job.get("provenance") or {}
    # Daily reports enter the same local publish API as manual uploads. Only
    # their frozen passthrough provenance and unchanged metadata qualify here.
    return (provenance.get("kind") == "daily_report_passthrough"
            and bool(job.get("daily_report_date"))
            and job["daily_report_date"] == provenance.get("report_date")
            and job.get("publication_metadata") == publication.get("metadata"))


def current_details(candidate, details, now):
    """Check the current official activity deadline and the reviewed rule page."""
    top = details.get("top_details") or {}
    topic = top.get("topic_item") or {}
    card = (details.get("functional_card") or {}).get("traffic_card") or {}
    rule = candidate["rule"]
    if (positive(topic.get("id")) != candidate["topic_id"]
            or topic.get("name") != candidate["topic_name"]
            or top.get("close_pub_layer_entry") is not False
            or card.get("name") != rule.get("activity_name")
            or card.get("jump_url") != rule.get("activity_url")):
        return None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})截止", str(card.get("card_desc", "")))
    if not match:
        return None
    try:
        end = datetime.strptime(match[1], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    except ValueError:
        return None
    if now >= end - 60:
        return None
    return {"topic_id": candidate["topic_id"], "topic_name": candidate["topic_name"],
            "mission_id": candidate["mission_id"], "activity_name": card["name"],
            "activity_url": card["jump_url"], "deadline_local": match[1],
            "deadline_timestamp": end, "topic_open": True}


def read_policy(data_dir):
    path = Path(data_dir) / "publication-activity-policy.json"
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("activity_policy_too_large")
    value = json.loads(raw)
    if value.get("version") != VERSION or value.get("enabled") is not True:
        raise ValueError("activity_policy_not_enabled")
    return value, hashlib.sha256(raw).hexdigest()


def unmet_requirements(rule, metadata, job, categories, now):
    reasons = []
    try:
        start = datetime.fromisoformat(rule["start_local"]).timestamp()
        end = datetime.fromisoformat(rule["end_exclusive_local"]).timestamp()
        minimum = float(rule["minimum_duration_seconds"])
        duration = float(job["duration"])
        parents = rule["allowed_category_parents"]
        suffix = rule["required_title_suffix"]
        if not isinstance(parents, list) or not isinstance(suffix, str):
            raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        return ["rules_unverified"]
    if not start <= now < end:
        reasons.append("outside_reviewed_activity_window")
    if not minimum <= duration:
        reasons.append("activity_duration_requirement")
    if suffix and not metadata["title"].endswith(suffix):
        reasons.append("activity_title_suffix_required")
    if parents and not any(row.get("id") == metadata["tid"] and row.get("parent") in parents
                           for row in categories):
        reasons.append("activity_category_requirement")
    return reasons


async def submission_activity(client, publication, job, account_uid, data_dir):
    metadata = publication["metadata"]
    receipt = {"version": VERSION, "checked_at": time.time(), "account_uid": account_uid,
               "metadata_sha256": digest(metadata), "status": "skipped",
               "submission_metadata_sha256": digest(metadata),
               "metadata_hash_format": "canonical_json_utf8_sorted_keys",
               "policy": "fresh_uploader_activity_with_reviewed_content_rules",
               "source": "https://member.bilibili.com/x/vupre/web/topic/type"}
    if not automatic_source(publication, job):
        receipt["reason"] = "not_automatic"
        return dict(metadata), receipt
    if metadata.get("copyright") != 1:
        receipt["reason"] = "copyright_not_eligible"
        return dict(metadata), receipt
    async def select_activity():
        policy, policy_sha = read_policy(data_dir)
        rules = policy.get("rules", [])
        receipt["policy_sha256"] = policy_sha
        keywords = [rule["topic_name"] for rule in rules if isinstance(rule, dict)
                    and isinstance(rule.get("topic_name"), str)]
        rows = await client.activity_topics(metadata["tid"], keywords)
        candidates = topic_candidates(rows, metadata, rules, job)
        receipt.update(returned_topic_count=len(rows), matching_activity_count=len(candidates))
        categories = await client.categories() if candidates else []
        rejected = []
        for candidate in candidates[:5]:
            requirements = unmet_requirements(candidate["rule"], metadata, job, categories, time.time())
            if requirements:
                rejected.append({"topic_name": candidate["topic_name"], "reasons": requirements})
                continue
            category = await client.activity_category_allowed(metadata["tid"], candidate["topic_id"])
            if category.get("allowed") is not True:
                rejected.append({"topic_name": candidate["topic_name"], "reasons": ["platform_category_check"]})
                continue
            details = await client.activity_details(candidate["topic_id"])
            eligibility = current_details(candidate, details, time.time())
            if not eligibility:
                rejected.append({"topic_name": candidate["topic_name"], "reasons": ["activity_closed_or_rules_changed"]})
                continue
            tags = list(metadata["tags"])
            additions = []
            if normal(candidate["topic_name"]) not in {normal(tag) for tag in tags}:
                if len(tags) >= 10:
                    receipt["reason"] = "no_free_activity_tag_slot"
                    continue
                tags.append(candidate["topic_name"])
                additions.append(candidate["topic_name"])
            from .publishing import validate_metadata
            selected = {**validate_metadata({**metadata, "tags": tags}), "mission_id": candidate["mission_id"],
                        "topic_id": candidate["topic_id"]}
            receipt.update(status="selected", reason="reviewed_content_rule_and_platform_checks",
                           selected=eligibility, category_check=category,
                           metadata_delta={"added_tags": additions, "mission_id": candidate["mission_id"],
                                           "topic_id": candidate["topic_id"]},
                           base_copy_unchanged=True, activity_tag_review="separate_content_rule_review",
                           availability_basis="fresh_authenticated_uploader_result",
                           submission_metadata_sha256=digest(selected))
            return selected, receipt
        receipt["rejected_candidates"] = rejected
        receipt.setdefault("reason", "no_eligible_activity_matching_reviewed_content")
        return dict(metadata), receipt

    try:
        return await asyncio.wait_for(select_activity(), timeout=20)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Optional discovery must not abort uploads or expose response/cookie data.
        receipt["reason"] = "activity_lookup_unavailable"
    return dict(metadata), receipt
