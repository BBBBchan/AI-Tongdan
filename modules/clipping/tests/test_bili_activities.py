import asyncio
import copy
import json

import httpx
import pytest

from clipper.bili_activities import digest, submission_activity, topic_candidates, current_details
from clipper.bili_publish import BiliPublisher, PublishError


META = {"title": "小担工作日记【AI全民制作人】", "description": "原创 AI 漫画",
        "tid": 182, "copyright": 1, "source": "", "tags": ["小担", "AI漫画"]}
RULE = {"topic_name": "AI整活全宇宙", "activity_name": "AI全民制作人三期",
        "activity_url": "https://www.bilibili.com/blackboard/era/GBslbhKUtDJ8QpLc.html",
        "content_profile": "xiaodan_daily", "required_content_tags": ["AI漫画"],
        "allowed_category_parents": ["影视"], "required_title_suffix": "【AI全民制作人】",
        "minimum_duration_seconds": 60, "start_local": "2020-01-01T00:00:00+08:00",
        "end_exclusive_local": "2099-01-01T00:00:00+08:00",
        "requires_external_signup": False, "requires_additional_agreement": False, "reviewed": True}
TOPIC = {"id": 123, "name": "AI整活全宇宙", "mission_id": 456, "state": 0}
JOB = {"content_profile": "xiaodan_daily", "duration": 60}


class Client:
    async def activity_topics(self, tid, keywords):
        return [dict(TOPIC)]

    async def categories(self):
        return [{"id": 182, "parent": "影视"}, {"id": 230, "parent": "生活"}]

    async def activity_category_allowed(self, tid, topic):
        return {"allowed": True, "code": 0, "content": "", "tid": tid, "topic_id": topic}

    async def activity_details(self, topic):
        return {"top_details": {"topic_item": {"id": topic, "name": TOPIC["name"]},
                                "close_pub_layer_entry": False},
                "functional_card": {"traffic_card": {"name": RULE["activity_name"],
                 "jump_url": RULE["activity_url"], "card_desc": "2098-12-31 23:59截止"}}}


@pytest.fixture
def policy(tmp_path):
    (tmp_path / "publication-activity-policy.json").write_text(json.dumps({
        "version": 1, "enabled": True, "rules": [RULE]}, ensure_ascii=False))
    return tmp_path


async def choose(policy, *, metadata=None, job=None, automatic=True, client=None):
    return await submission_activity(client or Client(),
        {"metadata": copy.deepcopy(metadata or META), "automatic": automatic},
        copy.deepcopy(job or JOB), 42, policy)


@pytest.mark.asyncio
async def test_authorized_single_tag_addition_binds_base_and_final(policy):
    final, receipt = await choose(policy)
    assert receipt["status"] == "selected"
    assert final["tags"] == META["tags"] + [TOPIC["name"]]
    assert final["mission_id"] == 456 and final["topic_id"] == 123
    for key in ("title", "description", "tid", "copyright", "source"):
        assert final[key] == META[key]
    assert receipt["metadata_sha256"] == digest(META)
    assert receipt["submission_metadata_sha256"] == digest(final)
    assert receipt["metadata_delta"]["added_tags"] == [TOPIC["name"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"copyright": 2, "source": "https://live.bilibili.com/1"}, "copyright_not_eligible"),
    ({"title": "小担工作日记"}, "activity_title_suffix_required"),
    ({"tid": 230}, "activity_category_requirement"),
])
async def test_eligibility_is_more_than_platform_code_zero(policy, change, reason):
    meta = {**META, **change}
    final, receipt = await choose(policy, metadata=meta)
    assert final == meta and receipt["status"] == "skipped"
    assert reason == receipt["reason"] or any(reason in row["reasons"] for row in receipt.get("rejected_candidates", []))


@pytest.mark.asyncio
async def test_manual_not_selected_and_no_network(policy):
    class Forbidden(Client):
        async def activity_topics(self, *args):
            pytest.fail("manual uploads do not auto-select")
    final, receipt = await choose(policy, automatic=False, client=Forbidden())
    assert final == META and receipt["reason"] == "not_automatic"


@pytest.mark.asyncio
async def test_trusted_daily_passthrough_is_automatic(policy):
    job = {**JOB, "daily_report_date": "2026-10-23", "publication_metadata": META,
           "provenance": {"kind": "daily_report_passthrough", "report_date": "2026-10-23"}}
    final, receipt = await choose(policy, job=job, automatic=False)
    assert receipt["status"] == "selected"
    job["publication_metadata"] = {**META, "title": "different frozen title"}
    final, receipt = await choose(policy, job=job, automatic=False)
    assert receipt["reason"] == "not_automatic"


@pytest.mark.asyncio
async def test_no_free_tag_slot_preserves_all_existing_tags(policy):
    meta = {**META, "tags": META["tags"] + [f"标签{i}" for i in range(8)]}
    final, receipt = await choose(policy, metadata=meta)
    assert final == meta and receipt["reason"] == "no_free_activity_tag_slot"


@pytest.mark.asyncio
async def test_unknown_or_unrelated_topic_never_selected(policy):
    assert not topic_candidates([{**TOPIC, "name": "DeepSeek挑战"}], META, [RULE], JOB)
    assert not topic_candidates([{**TOPIC, "mission_id": 0}], META, [RULE], JOB)
    assert not topic_candidates([TOPIC], {**META, "tags": ["游戏"]}, [RULE], JOB)
    assert not topic_candidates([TOPIC], META, [{**RULE, "reviewed": False}], JOB)


@pytest.mark.asyncio
async def test_expired_changed_or_closed_activity_is_skipped(policy):
    for change in ("closed", "expired", "rules_changed"):
        class Changed(Client):
            async def activity_details(self, topic):
                d = await super().activity_details(topic)
                if change == "closed": d["top_details"]["close_pub_layer_entry"] = True
                if change == "expired": d["functional_card"]["traffic_card"]["card_desc"] = "2020-01-01 00:00截止"
                if change == "rules_changed": d["functional_card"]["traffic_card"]["jump_url"] = "https://www.bilibili.com/other"
                return d
        final, receipt = await choose(policy, client=Changed())
        assert final == META and receipt["status"] == "skipped"


@pytest.mark.asyncio
async def test_lookup_failure_does_not_leak_or_prevent_submission(policy):
    class Failed(Client):
        async def activity_topics(self, *args):
            raise RuntimeError("secret-cookie")
    final, receipt = await choose(policy, client=Failed())
    assert final == META and receipt["reason"] == "activity_lookup_unavailable"
    assert "secret" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_activity_deadline_cancels_lookup_and_preserves_metadata(policy, monkeypatch):
    wait_for = asyncio.wait_for
    deadlines, cancelled = [], []

    async def short_deadline(awaitable, timeout):
        deadlines.append(timeout)
        return await wait_for(awaitable, .01)

    class Blocked(Client):
        async def activity_topics(self, *args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    monkeypatch.setattr(asyncio, "wait_for", short_deadline)
    final, receipt = await choose(policy, client=Blocked())
    assert deadlines == [20] and cancelled == [True]
    assert final == META and receipt["status"] == "skipped"
    assert receipt["reason"] == "activity_lookup_unavailable"


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_swallowed_by_activity_lookup(policy):
    started, cancelled = asyncio.Event(), []

    class Blocked(Client):
        async def activity_topics(self, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    task = asyncio.create_task(choose(policy, client=Blocked()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [True]


@pytest.mark.asyncio
async def test_official_topic_interfaces_and_submission_fields():
    calls = []
    def handler(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("/topic/type"):
            assert request.url.params["type_id"] == "182"
            return httpx.Response(200, json={"code": 0, "data": {"topics": []}})
        if path.endswith("/topic/search"):
            return httpx.Response(200, json={"code": 0, "data": {"result": {"topics": [TOPIC]}}})
        if path.endswith("/topic/type/check"):
            return httpx.Response(200, json={"code": 0, "data": {"code": 0, "content": ""}})
        if path.endswith("/details/top"):
            return httpx.Response(200, json={"code": 0, "data": {"top_details": {}}})
        payload = json.loads(request.content)
        assert payload["mission_id"] == 456 and payload["topic_id"] == 123
        assert payload["tag"].endswith("AI整活全宇宙")
        return httpx.Response(200, json={"code": 0, "data": {"aid": 789, "bvid": "BVactivity123"}})
    async with BiliPublisher("SESSDATA=test; bili_jct=testcsrf", transport=httpx.MockTransport(handler)) as client:
        assert await client.activity_topics(182, [TOPIC["name"]]) == [TOPIC]
        assert (await client.activity_category_allowed(182, 123))["allowed"]
        await client.activity_details(123)
        await client.submit({**META, "tags": META["tags"] + [TOPIC["name"]], "mission_id": 456, "topic_id": 123}, {"filename": "test-object"}, "")
        with pytest.raises(PublishError):
            await client.submit({**META, "copyright": 2, "source": "https://live.bilibili.com/1", "mission_id": 456, "topic_id": 123}, {"filename": "test-object"}, "")
    assert sum(r.method == "POST" for r in calls) == 1
