"""Publishing regressions using temporary journals and a network-free publisher."""
import asyncio
from collections import deque
from copy import deepcopy
from pathlib import Path
import re
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

from clipper.bili_publish import PublishError
from clipper.config import Settings
from clipper.publishing import PublicationManager, metadata_for, validate_metadata
from clipper.server import create_app
from clipper.store import Store


COOKIE = "SESSDATA=workflow-session-secret; bili_jct=workflow-csrf-secret; DedeUserID=42"
PROFILE = {
    "tid": 17,
    "copyright": 2,
    "source": "https://live.bilibili.com/12345",
    "creator_name": "测试主播",
    "tags": ["直播切片"],
}


class FakePublisher:
    """A complete manager-facing fake; even account/category requests stay local."""

    def __init__(self):
        self.received_cookies = []
        self.account_result = {
            "configured": True, "logged_in": True, "uid": 42, "name": "测试投稿账号",
        }
        self.category_result = [{"id": 17, "name": "单机游戏", "parent": "游戏"}]
        self.account_error = None
        self.category_error = None
        self.upload_error = None
        self.outcomes = deque()
        self.video_uploads = []
        self.cover_uploads = []
        self.submissions = []
        self.before_submit = None
        self.status_calls = []

    def __call__(self, cookie):
        self.received_cookies.append(cookie)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def account(self):
        if self.account_error:
            raise self.account_error
        return dict(self.account_result)

    async def categories(self):
        if self.category_error:
            raise self.category_error
        return deepcopy(self.category_result)

    async def upload_video(self, path, progress):
        if self.upload_error:
            raise self.upload_error
        data = Path(path).read_bytes()
        self.video_uploads.append(data)
        await progress(len(data), len(data))
        return {"filename": f"fake-video-{len(self.video_uploads)}", "cid": 123}

    async def upload_cover(self, path):
        self.cover_uploads.append(Path(path).read_bytes())
        return f"https://i0.hdslb.com/bfs/archive/fake-{len(self.cover_uploads)}.jpg"

    async def submit(self, metadata, video, cover):
        self.submissions.append({
            "metadata": deepcopy(metadata), "video": deepcopy(video), "cover": cover,
        })
        if self.before_submit:
            self.before_submit()
        outcome = self.outcomes.popleft() if self.outcomes else {
            "aid": 321, "bvid": "BVworkflow123",
        }
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def status(self, aid):
        self.status_calls.append(aid)
        return {"status": "submitted", "platform_message": "审核中"}


def local_settings(tmp_path):
    private = tmp_path / "private"
    private.mkdir(parents=True)
    (private / "bilibili-upload.cookie").write_text(COOKIE)
    return Settings(data_dir=tmp_path, import_roots=(tmp_path,))


def complete_job(store, settings, session=None, anchor=None):
    session = session or store.create_session(
        title="测试素材", duration=90, mode="file", publish_profile=deepcopy(PROFILE),
    )
    anchor = anchor or store.add_anchor(
        session["id"], title="真实片段", start=12, end=82, timestamp=20,
        reason="手动选段", origin="manual", score=100, evidence_ids=[],
    )
    job = store.create_job(
        anchor["id"], session["id"], status="complete",
        anchor=dict(anchor), options={"captions": False}, duration=70,
    )
    folder = settings.data_dir / "exports" / job["id"]
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"local-video-placeholder")
    (folder / "cover.jpg").write_bytes(b"local-cover-placeholder")
    return session, anchor, job, folder


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    async def blocked(*args, **kwargs):
        pytest.fail("Publishing workflow tests must not perform HTTP requests.")

    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)


@pytest.fixture(autouse=True)
def placeholder_media_probe(monkeypatch):
    # This suite tests durable upload/submission behavior using byte placeholders.
    # Actual ffprobe/media-policy coverage lives in test_publication_media.py.
    async def inspected(settings, path):
        return {"duration": 70.0, "width": 1920, "height": 1080, "audio": True}
    monkeypatch.setattr("clipper.render.probe", inspected)


@pytest.fixture
def workflow(tmp_path):
    settings = local_settings(tmp_path)
    store = Store(tmp_path)
    fake = FakePublisher()
    manager = PublicationManager(settings, store, client_factory=fake)
    session, anchor, job, folder = complete_job(store, settings)
    yield SimpleNamespace(
        settings=settings, store=store, fake=fake, manager=manager,
        session=session, anchor=anchor, job=job, folder=folder,
    )
    store.db.close()


@pytest.fixture
def publishing_api(tmp_path):
    settings = local_settings(tmp_path)
    app = create_app(settings)
    fake = FakePublisher()
    app.state.publisher.client_factory = fake
    session, anchor, job, folder = complete_job(app.state.store, settings)
    with TestClient(app) as client:
        yield SimpleNamespace(
            app=app, client=client, settings=settings, store=app.state.store, fake=fake,
            session=session, anchor=anchor, job=job, folder=folder,
        )


def enqueue(scene):
    return scene.manager.enqueue(scene.job, metadata_for(scene.job, scene.session))


def reviewed_media(scene):
    return {"video_sha256": scene.manager._digest(scene.folder / "clip.mp4"),
            "cover_sha256": scene.manager._digest(scene.folder / "cover.jpg")}


@pytest.mark.parametrize("mutate_metadata", [False, True])
async def test_activity_submission_preserves_base_audit_and_blocks_changed_copy(workflow, mutate_metadata):
    import json
    from test_bili_activities import Client, RULE, META
    from clipper.bili_activities import digest
    from clipper.bili_publish import BiliPublisher
    s = workflow
    (s.settings.data_dir / "publication-activity-policy.json").write_text(json.dumps({
        "version": 1, "enabled": True, "rules": [RULE]}))
    s.job = s.store.update_job(s.job["id"], content_profile="xiaodan_daily")
    s.fake.category_result = [{"id": 182, "name": "影视杂谈", "parent": "影视"}]
    activity = Client()
    p = s.manager.enqueue(s.job, META, automatic=True, expected_media=reviewed_media(s))
    async def topics(tid, keywords):
        if mutate_metadata:
            s.store.update_publication(p["id"], metadata={**META, "title": "Changed after review"})
        return await activity.activity_topics(tid, keywords)
    s.fake.activity_topics = topics
    s.fake.activity_category_allowed = activity.activity_category_allowed
    s.fake.activity_details = activity.activity_details
    await s.manager.run(p["id"])
    saved = s.store.publication(p["id"])
    if mutate_metadata:
        assert saved["status"] == "failed" and not s.fake.submissions
        assert "活动核验期间发生变化" in saved["error"]
        return
    assert saved["status"] == "submitted" and len(s.fake.submissions) == 1
    assert saved["metadata"] == META
    final = s.fake.submissions[0]
    assert saved["submission_metadata"] == final["metadata"]
    assert final["metadata"]["tags"] == META["tags"] + ["AI整活全宇宙"]
    receipt = saved["activity_selection"]
    assert receipt["metadata_sha256"] == digest(META)
    assert receipt["submission_metadata_sha256"] == digest(final["metadata"])
    assert receipt["submission_payload_sha256"] == digest(BiliPublisher.submission_payload(
        final["metadata"], final["video"], final["cover"]))


@pytest.mark.parametrize("filename", ["clip.mp4", "cover.jpg"])
async def test_reviewed_media_replacement_blocks_all_uploads(workflow, filename):
    s = workflow
    p = s.manager.enqueue(s.job, metadata_for(s.job, s.session), expected_media=reviewed_media(s))
    (s.folder / filename).write_bytes(b"unreviewed replacement")
    await s.manager.run(p["id"])
    assert s.store.publication(p["id"])["status"] == "failed"
    assert "审核后发生变化" in s.store.publication(p["id"])["error"]
    assert not s.fake.video_uploads and not s.fake.cover_uploads and not s.fake.submissions


async def test_upload_uses_frozen_reviewed_bytes_if_export_changes_during_network(workflow):
    s = workflow
    video, cover = (s.folder / "clip.mp4").read_bytes(), (s.folder / "cover.jpg").read_bytes()
    expected = reviewed_media(s)
    account = s.fake.account
    async def replacing_account():
        (s.folder / "clip.mp4").write_bytes(b"new video rendered while waiting")
        (s.folder / "cover.jpg").write_bytes(b"new cover rendered while waiting")
        return await account()
    s.fake.account = replacing_account
    p = s.manager.enqueue(s.job, metadata_for(s.job, s.session), expected_media=expected,
                          expected_account_uid=42)
    await s.manager.run(p["id"])
    assert s.store.publication(p["id"])["status"] == "submitted"
    assert s.fake.video_uploads == [video] and s.fake.cover_uploads == [cover]
    assert not list(s.settings.data_dir.glob("publication-*"))


async def test_account_switch_after_enqueue_prevents_upload(workflow):
    s = workflow
    p = s.manager.enqueue(s.job, metadata_for(s.job, s.session), expected_media=reviewed_media(s),
                          expected_account_uid=999)
    await s.manager.run(p["id"])
    assert s.store.publication(p["id"])["status"] == "failed"
    assert "账号已变化" in s.store.publication(p["id"])["error"]
    assert not s.fake.video_uploads and not s.fake.submissions


@pytest.mark.parametrize("duration", [None, 59.999, float("nan"), float("inf")])
def test_enqueue_refuses_short_or_unverified_jobs_without_writes(workflow, duration):
    s = workflow
    s.job["duration"] = duration
    with pytest.raises(ValueError, match="60秒"):
        enqueue(s)
    assert s.store.publications() == []
    assert s.fake.video_uploads == s.fake.cover_uploads == s.fake.submissions == []


async def test_exactly_sixty_seconds_can_be_uploaded_and_submitted(workflow, monkeypatch):
    s = workflow
    s.job["duration"] = 60
    async def inspected(settings, path):
        return {"duration": 60.0, "width": 1920, "height": 1080}
    monkeypatch.setattr("clipper.render.probe", inspected)
    p = enqueue(s)
    await s.manager.run(p["id"])
    result = s.store.publication(p["id"])
    assert result["status"] == "submitted"
    assert result["media_validation"]["duration"] == 60
    assert len(s.fake.video_uploads) == len(s.fake.submissions) == 1


def test_api_rejects_short_clip_with_422_without_creating_publication(publishing_api):
    s = publishing_api
    s.store.update_job(s.job["id"], duration=59.999)
    response = s.client.post(f"/api/jobs/{s.job['id']}/publish", json=metadata_for(s.job, s.session))
    assert response.status_code == 422 and "60秒" in response.text
    assert s.store.publications() == []
    assert s.fake.video_uploads == s.fake.cover_uploads == s.fake.submissions == []


@pytest.mark.parametrize("series_outcome", ["added", "failed", "exception", "cancelled"])
async def test_series_failure_never_requeues_or_relabels_published_video(workflow, series_outcome):
    s = workflow
    s.store.update_session(s.session["id"], mode="live", streamer_uid=7, streamer="测试主播")
    p = enqueue(s)
    await s.manager.run(p["id"])

    async def published(aid):
        return {"status": "published", "bvid": "BVworkflow123", "platform_message": "开放浏览"}

    class FakeSeries:
        async def archive(self, publication, session):
            assert publication["status"] == "published" and session["streamer_uid"] == 7
            if series_outcome == "exception":
                raise RuntimeError(COOKIE)
            if series_outcome == "cancelled":
                raise asyncio.CancelledError()
            return {"status": series_outcome, "name": "测试主播直播切片", "retryable": False}

    s.fake.status = published
    s.manager.series_manager = FakeSeries()
    if series_outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await s.manager.refresh(p["id"])
    else:
        await s.manager.refresh(p["id"])
    result = s.store.publication(p["id"])
    assert result["status"] == "published" and result["aid"] == 321
    assert len(s.fake.submissions) == 1 and len(s.fake.video_uploads) == 1
    assert "workflow-session-secret" not in str(result)
    if series_outcome != "cancelled":
        assert result["series"]["status"] == ("failed" if series_outcome == "exception" else series_outcome)
    with pytest.raises(ValueError, match="重复"):
        enqueue(s)


async def test_intro_does_not_enter_streamer_series(workflow):
    s = workflow
    p = enqueue(s)
    s.store.update_publication(p["id"], status="published", aid=321)

    class ForbiddenSeries:
        async def archive(self, *args):
            pytest.fail("An imported intro must stay out of streamer series")

    s.manager.series_manager = ForbiddenSeries()
    assert not s.manager.series_due(s.store.publication(p["id"]))
    result = await s.manager.archive_series(p["id"])
    assert "series" not in result


def test_series_recovery_obeys_backoff_and_terminal_states(workflow):
    s = workflow
    s.store.update_session(s.session["id"], mode="live", streamer_uid=7, streamer="测试主播")
    p = enqueue(s)
    p = s.store.update_publication(p["id"], status="published", aid=321)
    assert s.manager.series_due(p)
    assert not s.manager.series_due({**p, "series": {"status": "uncertain", "retryable": True,
                                                   "next_check_at": time.time() + 60}})
    assert s.manager.series_due({**p, "series": {"status": "uncertain", "retryable": True,
                                               "next_check_at": time.time() - 1}})
    for state in ["added", "skipped", "failed", "blocked"]:
        assert not s.manager.series_due({**p, "series": {"status": state}})
    assert not s.manager.series_due({**p, "series": {"status": "uncertain", "retryable": False}})


@pytest.mark.parametrize("outcome", [
    PublishError("提交超时，需核实", uncertain=True),
    TimeoutError(COOKIE),
    {"aid": 321},  # A successful-looking response without a BVID is still ambiguous.
])
async def test_uncertain_submission_cannot_be_requeued_or_rerun(workflow, outcome):
    s = workflow
    s.fake.outcomes.append(outcome)
    p = enqueue(s)
    await s.manager.run(p["id"])
    result = s.store.publication(p["id"])
    assert result["status"] == "uncertain"
    assert len(s.fake.submissions) == 1
    assert "workflow-session-secret" not in str(result)
    with pytest.raises(ValueError, match="重复"):
        enqueue(s)

    await s.manager.run(p["id"])
    assert len(s.fake.submissions) == 1, "An ambiguous final write must never be replayed."
    assert s.store.publication(p["id"])["status"] == "uncertain"


async def test_submitted_is_durable_and_duplicate_workers_do_not_resubmit(workflow):
    s = workflow
    p = enqueue(s)
    checkpoints = []

    def check_checkpoint():
        # Inspect another SQLite connection without running Store's restart recovery.
        import sqlite3
        import json
        with sqlite3.connect(s.settings.data_dir / "clipper.sqlite") as db:
            row = db.execute("SELECT payload FROM publications WHERE id=?", (p["id"],)).fetchone()
        checkpoints.append(json.loads(row[0])["status"])

    s.fake.before_submit = check_checkpoint
    await asyncio.gather(s.manager.run(p["id"]), s.manager.run(p["id"]))
    assert len(s.fake.submissions) == 1
    assert checkpoints == ["submitting"], "The journal must commit before the final HTTP write."
    result = s.store.publication(p["id"])
    assert result["status"] == "submitted"
    assert result["aid"] == 321 and result["bvid"] == "BVworkflow123"
    assert result["progress"] == 100
    with pytest.raises(ValueError, match="重复"):
        enqueue(s)
    await s.manager.run(p["id"])
    assert len(s.fake.submissions) == 1


async def test_reexport_of_same_anchor_cannot_create_a_second_archive(workflow):
    s = workflow
    p = enqueue(s)
    await s.manager.run(p["id"])
    _, _, second_job, _ = complete_job(s.store, s.settings, s.session, s.anchor)
    with pytest.raises(ValueError, match="锚点"):
        s.manager.enqueue(second_job, metadata_for(second_job, s.session))
    assert len(s.store.publications()) == 1


async def test_definite_failure_can_retry_and_reuse_confirmed_uploads(workflow):
    s = workflow
    s.fake.outcomes.append(PublishError("平台明确拒绝标题", code=406))
    p = enqueue(s)
    await s.manager.run(p["id"])
    failed = s.store.publication(p["id"])
    assert failed["status"] == "failed" and failed["platform_code"] == 406
    assert failed["uploaded_video"]["filename"] == "fake-video-1"
    assert failed["uploaded_cover"]

    metadata = metadata_for(s.job, s.session)
    metadata["title"] = "修改后的真实标题"
    retry = s.manager.enqueue(s.job, metadata)
    assert retry["id"] == p["id"] and retry["status"] == "queued"
    await s.manager.run(retry["id"])
    assert s.store.publication(p["id"])["status"] == "submitted"
    assert len(s.fake.video_uploads) == len(s.fake.cover_uploads) == 1
    assert len(s.fake.submissions) == 2
    assert s.fake.submissions[1]["metadata"]["title"] == "修改后的真实标题"
    assert s.fake.submissions[0]["video"] == s.fake.submissions[1]["video"]


@pytest.mark.parametrize("change", ["video", "account"])
async def test_retry_does_not_reuse_video_after_content_or_account_changes(workflow, change):
    s = workflow
    s.fake.outcomes.append(PublishError("明确失败", code=406))
    p = enqueue(s)
    await s.manager.run(p["id"])
    if change == "video":
        (s.folder / "clip.mp4").write_bytes(b"replacement-video")
    else:
        s.fake.account_result["uid"] = 99
        s.fake.account_result["name"] = "另一测试账号"
    retry = enqueue(s)
    await s.manager.run(retry["id"])
    assert s.store.publication(p["id"])["status"] == "submitted"
    assert len(s.fake.video_uploads) == 2
    assert s.fake.submissions[1]["video"]["filename"] == "fake-video-2"
    assert len(s.fake.cover_uploads) == (2 if change == "account" else 1)


async def test_account_change_then_failed_upload_does_not_relabel_old_uploads(workflow):
    s = workflow
    s.fake.outcomes.append(PublishError("旧账号明确投稿失败", code=406))
    p = enqueue(s)
    await s.manager.run(p["id"])
    assert s.store.publication(p["id"])["uploaded_video"]["filename"] == "fake-video-1"

    s.fake.account_result["uid"] = 99
    s.fake.upload_error = PublishError("新账号的视频上传中断")
    retry = enqueue(s)
    await s.manager.run(retry["id"])
    assert s.store.publication(p["id"])["status"] == "failed"
    assert len(s.fake.submissions) == 1

    s.fake.upload_error = None
    retry = enqueue(s)
    await s.manager.run(retry["id"])
    assert s.store.publication(p["id"])["status"] == "submitted"
    assert len(s.fake.video_uploads) == 2, "The old account's object must not survive as this account's upload."
    assert len(s.fake.cover_uploads) == 2
    assert s.fake.submissions[-1]["video"]["filename"] == "fake-video-2"


@pytest.mark.parametrize("change", ["replaced", "removed"])
async def test_retry_uses_current_cover_while_reusing_unchanged_video(workflow, change):
    s = workflow
    s.fake.outcomes.append(PublishError("明确失败", code=406))
    p = enqueue(s)
    await s.manager.run(p["id"])
    if change == "replaced":
        (s.folder / "cover.jpg").write_bytes(b"newly-reviewed-cover")
    else:
        (s.folder / "cover.jpg").unlink()
    retry = enqueue(s)
    await s.manager.run(retry["id"])
    assert s.store.publication(p["id"])["status"] == "submitted"
    assert len(s.fake.video_uploads) == 1
    if change == "replaced":
        assert s.fake.cover_uploads == [b"local-cover-placeholder", b"newly-reviewed-cover"]
        assert s.fake.submissions[-1]["cover"].endswith("/fake-2.jpg")
    else:
        assert len(s.fake.cover_uploads) == 1
        assert s.fake.submissions[-1]["cover"] == ""


@pytest.mark.parametrize(("stage", "expected"), [
    ("upload", "failed"), ("submit", "uncertain"),
])
async def test_cancelled_worker_preserves_submission_boundary(workflow, stage, expected):
    s = workflow
    if stage == "upload":
        s.fake.upload_error = asyncio.CancelledError()
    else:
        s.fake.outcomes.append(asyncio.CancelledError())
    p = enqueue(s)
    with pytest.raises(asyncio.CancelledError):
        await s.manager.run(p["id"])
    assert s.store.publication(p["id"])["status"] == expected
    assert len(s.fake.submissions) == (1 if stage == "submit" else 0)


def test_restart_recovers_inflight_states_without_claiming_a_retry_is_safe(tmp_path):
    store = Store(tmp_path)
    states = ["queued", "uploading", "submitting", "submitted", "uncertain", "failed", "published"]
    records = {
        state: store.create_publication("job-" + state, status=state, uploaded_video={"filename": "keep"})
        for state in states
    }
    store.db.close()
    reopened = Store(tmp_path)
    try:
        expected = {"queued": "failed", "uploading": "failed", "submitting": "uncertain"}
        for old_state, p in records.items():
            current = reopened.publication(p["id"])
            assert current["status"] == expected.get(old_state, old_state)
            assert current["uploaded_video"] == {"filename": "keep"}
        assert "勿重复投稿" in reopened.publication(records["submitting"]["id"])["error"]
    finally:
        reopened.db.close()


def test_metadata_does_not_invent_identity_source_or_stream_date(workflow):
    s = workflow
    session = {
        **s.session,
        "title": "星瞳2026-10-19录像文件名",
        "media_path": "/tmp/七海-2026-10-19.mp4",
        "created_at": 1792368000,
        "publish_profile": {"tid": 17, "copyright": 1},
    }
    metadata = metadata_for(s.job, session)
    assert metadata["source"] == ""
    assert metadata["tags"] == ["直播切片"]
    assert "https://" not in metadata["description"]
    assert not re.search(r"20\d\d[-年/]\d{1,2}", metadata["description"])
    for invented in ["星瞳", "七海", "阿梓", "已获授权", "官方切片", "侵删"]:
        assert invented not in metadata["description"]
        assert invented not in metadata["tags"]
    assert metadata["description"] == ""


def test_metadata_preserves_explicit_source_and_deduplicates_relevant_tags(workflow):
    s = workflow
    session = {
        **s.session, "source_url": "https://live.bilibili.com/99999",
        "publish_profile": {
            **PROFILE, "creator_name": "小林", "tags": ["CS2", "游戏实况", " CS2 "],
            "description": "用户确认的直播日期：2026年10月19日",
        },
    }
    job = {**s.job, "anchor": {**s.anchor, "title": "游戏中的一次配合"}}
    metadata = metadata_for(job, session)
    assert metadata["source"] == PROFILE["source"]
    assert PROFILE["source"] in metadata["description"]
    assert "99999" not in metadata["description"]
    assert "2026年10月19日" in metadata["description"]
    assert metadata["tags"] == ["小林", "直播切片", "CS2", "游戏实况"]
    assert not {"萌宠", "星瞳", "天猫双11买数码家电更值得"} & set(metadata["tags"])
    assert validate_metadata(metadata) == metadata


@pytest.mark.parametrize("source", ["", "not-a-url", "file:///tmp/recording.mp4",
                                    "https://name:secret@example.test/source"])
def test_reposted_metadata_requires_a_public_source_link(workflow, source):
    metadata = metadata_for(workflow.job, workflow.session)
    metadata["source"] = source
    with pytest.raises(ValueError, match="转载"):
        validate_metadata(metadata)
    metadata["copyright"] = 1
    validate_metadata(metadata)


def test_api_requires_valid_profile_account_and_category_before_enabling_auto_publish(publishing_api):
    s = publishing_api
    path = f"/api/sessions/{s.session['id']}"
    s.store.update_session(s.session["id"], publish_profile=None)
    assert s.client.patch(path, json={"auto_publish": True}).status_code == 422
    assert not s.store.session(s.session["id"])["auto_publish"]

    invalid_source = {**PROFILE, "source": ""}
    response = s.client.patch(path, json={"auto_publish": True, "publish_profile": invalid_source})
    assert response.status_code == 422 and "来源" in response.text
    assert not s.store.session(s.session["id"])["auto_publish"]

    s.fake.account_result.update(logged_in=False, error="测试登录失效")
    response = s.client.patch(path, json={"auto_publish": True, "publish_profile": PROFILE})
    assert response.status_code == 422 and "登录失效" in response.text
    s.fake.account_result.update(logged_in=True)
    s.app.state.publisher._account = (0, {})

    response = s.client.patch(
        path, json={"auto_publish": True, "publish_profile": {**PROFILE, "tid": 999999}},
    )
    assert response.status_code == 422 and "分区" in response.text
    assert not s.store.session(s.session["id"])["auto_publish"]

    response = s.client.patch(path, json={"auto_publish": True, "publish_profile": PROFILE})
    assert response.status_code == 200, response.text
    assert response.json()["auto_publish"] is True
    # Invalid edits while automatic mode is already active must also be atomic.
    response = s.client.patch(path, json={"publish_profile": invalid_source})
    assert response.status_code == 422
    assert s.store.session(s.session["id"])["publish_profile"]["source"] == PROFILE["source"]


@pytest.mark.parametrize("state", ["submitted", "uncertain", "private"])
def test_api_rejects_repeated_final_submission_without_changing_existing_state(publishing_api, state):
    s = publishing_api
    metadata = metadata_for(s.job, s.session)
    p = s.app.state.publisher.enqueue(s.job, metadata)
    s.store.update_publication(p["id"], status=state)
    response = s.client.post(f"/api/jobs/{s.job['id']}/publish", json=metadata)
    assert response.status_code == 422 and "重复" in response.text
    assert s.store.publication(p["id"])["status"] == state
    assert len(s.store.publications()) == 1
    assert s.fake.video_uploads == [] and s.fake.submissions == []


@pytest.mark.parametrize(("automatic", "render_success"), [(True, True), (False, True), (True, False)])
def test_render_completion_queues_automatic_publication_only_when_enabled(
        publishing_api, monkeypatch, automatic, render_success):
    s = publishing_api

    async def fake_render(renderer, jid):
        job = renderer.store.job(jid)
        assert not renderer.store.publication_for_job(jid), "No archive before rendering completes."
        if render_success:
            folder = renderer.settings.data_dir / "exports" / jid
            folder.mkdir(parents=True)
            (folder / "clip.mp4").write_bytes(b"rendered-placeholder")
            (folder / "cover.jpg").write_bytes(b"rendered-cover")
            renderer.store.update_job(jid, status="complete", progress=100, duration=70)
        else:
            renderer.store.update_job(jid, status="failed", error="测试渲染失败")

    monkeypatch.setattr("clipper.server.Renderer.run", fake_render)
    response = s.client.patch(
        f"/api/sessions/{s.session['id']}",
        json={"auto_publish": automatic, "publish_profile": PROFILE},
    )
    assert response.status_code == 200, response.text
    response = s.client.post(f"/api/anchors/{s.anchor['id']}/render", json={"captions": False})
    assert response.status_code == 202, response.text
    jid = response.json()["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = s.client.get(f"/api/jobs/{jid}").json()
        records = s.client.get("/api/publications").json()["publications"]
        if automatic and render_success:
            if records and records[0]["status"] == "submitted":
                break
        elif current["status"] in {"complete", "failed"}:
            break
        time.sleep(0.01)
    if automatic and render_success:
        assert len(records) == 1 and records[0]["status"] == "submitted"
        assert records[0]["automatic"] is True and records[0]["job_id"] == jid
        assert current["publication_id"] == records[0]["id"]
        assert len(s.fake.submissions) == 1
    else:
        assert records == [] and s.fake.submissions == []
        assert current["status"] == ("complete" if render_success else "failed")


def test_frontend_apis_and_download_routes_never_return_private_cookie(publishing_api):
    s = publishing_api
    routes = [
        "/api/health", "/api/state", f"/api/sessions/{s.session['id']}",
        "/api/publishing/account", "/api/publishing/categories", "/api/publications",
        f"/api/jobs/{s.job['id']}", f"/api/jobs/{s.job['id']}/publication-preview",
    ]
    for path in routes:
        response = s.client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert "workflow-session-secret" not in response.text
        assert "workflow-csrf-secret" not in response.text
        assert "upload_cookie_file" not in response.text
    assert s.fake.received_cookies and set(s.fake.received_cookies) == {COOKIE}
    for path in [
        "/private/bilibili-upload.cookie", "/data/private/bilibili-upload.cookie",
        "/static/%2e%2e/data/private/bilibili-upload.cookie",
        f"/api/exports/{s.job['id']}/bilibili-upload.cookie",
    ]:
        response = s.client.get(path)
        assert response.status_code == 404, path
        assert "workflow-session-secret" not in response.text


def test_upstream_exceptions_do_not_expose_cookie_in_api_errors(publishing_api):
    s = publishing_api
    s.fake.account_error = RuntimeError(COOKIE)
    response = s.client.get("/api/publishing/account")
    assert response.status_code == 200 and response.json()["logged_in"] is False
    assert "workflow-session-secret" not in response.text
    response = s.client.patch(
        f"/api/sessions/{s.session['id']}",
        json={"auto_publish": True, "publish_profile": PROFILE},
    )
    assert response.status_code == 422
    assert "workflow-session-secret" not in response.text
    s.fake.category_error = RuntimeError(COOKIE)
    response = s.client.get("/api/publishing/categories")
    assert response.status_code == 502
    assert "workflow-csrf-secret" not in response.text
