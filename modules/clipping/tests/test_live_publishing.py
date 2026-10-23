import asyncio
from copy import deepcopy
import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from clipper.config import Settings
from clipper.live_publishing import (
    LivePublicationController, POLICY_NAME, atomic_json, check_subtitle_integrity,
    publication_policy, validate_editorial, VALIDATION_VERSION,
)
from clipper.patrol import FollowingPatrol


class MemoryStore:
    def __init__(self):
        self.session_rows = {"s": {"id": "s", "mode": "live", "status": "live", "created_at": 1700000000,
                                   "auto_publish": False, "auto_render": True, "streamer": "Niya阿布",
                                   "streamer_uid": 188679, "room": {"owner_uid": 188679, "room_id": 685026}}}
        self.job_rows = {"j": {"id": "j", "session_id": "s", "anchor_id": "a", "created_at": 10,
                               "status": "complete", "duration": 90, "anchor": {"start": 100, "end": 190}}}
        self.publication_rows = []

    def sessions(self): return list(self.session_rows.values())
    def session(self, sid): return self.session_rows.get(sid)
    def jobs(self): return list(self.job_rows.values())
    def job(self, jid): return self.job_rows.get(jid)
    def publications(self): return self.publication_rows
    def publication(self, pid): return next((p for p in self.publication_rows if p["id"] == pid), None)
    def publication_for_job(self, jid): return next((p for p in self.publication_rows if p["job_id"] == jid), None)
    def update_session(self, sid, **values): self.session_rows[sid].update(values); return self.session_rows[sid]
    def update_job(self, jid, **values): self.job_rows[jid].update(values); return self.job_rows[jid]


class Publisher:
    def __init__(self, store): self.store, self.calls = store, []
    async def account(self, fresh=False): return {"logged_in": True, "uid": 42}
    def enqueue(self, job, metadata, automatic, expected_media=None, expected_account_uid=None):
        self.binding = {"expected_media": expected_media, "expected_account_uid": expected_account_uid}
        self.calls.append(("enqueue", job["id"], automatic))
        row = {"id": "p", "job_id": job["id"], "status": "queued"}
        self.store.publication_rows.append(row)
        return row
    async def run(self, pid):
        self.calls.append(("run", pid))
        self.store.publication_rows[-1]["status"] = "submitted"


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir()
    folder = settings.data_dir / "exports/j"
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"reviewed video")
    (folder / "cover.jpg").write_bytes(b"final cover")
    store = MemoryStore()
    publisher = Publisher(store)
    ctl = LivePublicationController(settings, store, publisher)
    return SimpleNamespace(settings=settings, store=store, publisher=publisher, ctl=ctl, root=tmp_path)


def arm(s, enabled=False):
    atomic_json(s.settings.data_dir / POLICY_NAME, {"enabled": enabled, "account_uid": 42, "armed_at": 5, "eligible_after": 5,
                "activate_after_batch": "data/batch/state.json", "activate_after_plan": "data/batch/plan.json"})
    atomic_json(s.settings.data_dir / "batch/plan.json", {"items": [{"job_id": "backlog"}]})


def test_activation_waits_for_batch_complete_and_actual_publication(setup):
    s = setup
    arm(s)
    atomic_json(s.settings.data_dir / "batch/state.json", {"status": "running"})
    assert not s.ctl.activate_if_ready()["enabled"]
    assert publication_policy(s.settings)["waiting_reason"] == "waiting_for_batch_complete"
    atomic_json(s.settings.data_dir / "batch/state.json", {"status": "complete"})
    s.store.publication_rows = [{"id": "p0", "job_id": "backlog", "status": "submitted"}]
    assert not s.ctl.activate_if_ready()["enabled"]
    assert publication_policy(s.settings)["waiting_reason"] == "waiting_for_all_backlog_publications_published"
    s.store.publication_rows[0]["status"] = "published"
    assert s.ctl.activate_if_ready()["enabled"]
    # Restart uses the durable state, without requiring another explicit enable.
    restarted = LivePublicationController(s.settings, s.store, s.publisher)
    assert restarted.activate_if_ready()["enabled"]
    assert publication_policy(s.settings)["eligible_after"] == 5


@pytest.mark.parametrize("status", ["failed", "uncertain", "private", "rejected", "queued"])
def test_bad_backlog_states_never_activate(setup, status):
    s = setup
    arm(s)
    atomic_json(s.settings.data_dir / "batch/state.json", {"status": "complete"})
    s.store.publication_rows = [{"id": "p0", "job_id": "backlog", "status": status}]
    assert not s.ctl.activate_if_ready()["enabled"]


def test_current_live_session_publishes_on_completion_without_end_or_delay(setup):
    s = setup
    arm(s, True)
    async def review(job, session):
        assert session["status"] == "live"
        return {"title": "approved"}, {"status": "ready", "cover_text": "approved"}
    async def apply(*args): pass
    s.ctl.review, s.ctl.apply_review = review, apply
    asyncio.run(s.ctl.process("j"))
    assert s.store.session("s")["status"] == "live"
    assert s.store.session("s")["auto_publish"]
    assert s.publisher.calls == [("enqueue", "j", True), ("run", "p")]
    assert s.store.job("j")["live_publication_review"]["status"] == "submitted"
    assert s.publisher.binding == {"expected_account_uid": 42, "expected_media": {
        "video_sha256": hashlib.sha256(b"reviewed video").hexdigest(),
        "cover_sha256": hashlib.sha256(b"final cover").hexdigest()}}


def test_policy_disabled_keeps_completed_clip_unpublished(setup):
    s = setup
    arm(s)
    asyncio.run(s.ctl.process("j"))
    assert s.publisher.calls == []


def test_render_callback_and_background_scan_cannot_duplicate(setup):
    s = setup
    arm(s, True)
    async def review(*args):
        await asyncio.sleep(0)
        return {}, {"status": "ready"}
    async def apply(*args): pass
    s.ctl.review, s.ctl.apply_review = review, apply
    async def race(): await asyncio.gather(s.ctl.process("j"), s.ctl.tick())
    asyncio.run(race())
    assert len([call for call in s.publisher.calls if call[0] == "enqueue"]) == 1


def test_pending_live_clips_survive_rotation_and_restart(setup):
    s = setup
    arm(s, True)
    s.store.session_rows["s"]["status"] = "stopped"
    s.store.job_rows["old"] = {**s.store.job_rows["j"], "id": "old", "created_at": 1}
    async def review(*args): return {}, {"status": "ready"}
    async def apply(*args): pass
    s.ctl.review, s.ctl.apply_review = review, apply
    asyncio.run(s.ctl.tick())
    assert s.publisher.calls == [("enqueue", "j", True), ("run", "p")]


def test_background_prioritizes_new_current_live_clips_over_ended_backlog(setup):
    s = setup
    arm(s, True)
    s.store.session_rows["ended"] = {**s.store.session_rows["s"], "id": "ended", "status": "stopped"}
    s.store.job_rows["old"] = {**s.store.job_rows["j"], "id": "old", "session_id": "ended", "created_at": 6}
    s.store.job_rows["newest"] = {**s.store.job_rows["j"], "id": "newest", "created_at": 11}
    order = []
    async def process(jid): order.append(jid)
    s.ctl.process = process
    asyncio.run(s.ctl.tick())
    assert order == ["newest", "j", "old"]


def test_review_failures_hold_clip_and_redact_endpoint(setup):
    s = setup
    arm(s, True)
    async def review(*args): raise RuntimeError("http://private-endpoint secret-token")
    s.ctl.review = review
    asyncio.run(s.ctl.process("j"))
    asyncio.run(s.ctl.tick())
    assert s.publisher.calls == []
    rendered = json.dumps(s.store.job("j"))
    assert "private-endpoint" not in rendered and "secret-token" not in rendered
    assert s.store.job("j")["live_publication_review"]["status"] == "held"


def test_account_change_prevents_enqueue(setup):
    s = setup
    arm(s, True)
    async def review(*args): return {}, {"status": "ready"}
    async def changed_account(fresh=False): return {"logged_in": True, "uid": 999}
    s.ctl.review = review
    s.publisher.account = changed_account
    asyncio.run(s.ctl.process("j"))
    assert s.publisher.calls == []
    assert s.store.job("j")["live_publication_review"]["reason"] == "publication_account_changed"


def test_video_replaced_while_semantic_review_runs_cannot_be_published(setup):
    s = setup
    arm(s, True)
    async def review(*args):
        (s.settings.data_dir / "exports/j/clip.mp4").write_bytes(b"different video")
        return {}, {"status": "ready"}
    async def apply(*args): pass
    s.ctl.review, s.ctl.apply_review = review, apply
    asyncio.run(s.ctl.process("j"))
    assert s.publisher.calls == []
    assert s.store.job("j")["live_publication_review"]["reason"] == "live_media_changed_during_review"


def test_known_overlap_error_has_a_useful_fixed_reason(setup):
    s = setup
    arm(s, True)
    async def review(*args): return {}, {"status": "ready"}
    async def apply(*args): pass
    def overlap(*args, **kwargs):
        raise ValueError("切片与已投稿或待投稿片段存在时间重叠，请保留不重叠的完整段落")
    s.ctl.review, s.ctl.apply_review = review, apply
    s.publisher.enqueue = overlap
    asyncio.run(s.ctl.process("j"))
    assert s.store.job("j")["live_publication_review"]["reason"] == "source_overlap"


def test_repaired_hold_gets_full_review_once_and_preserves_original(setup):
    s = setup
    arm(s, True)
    previous = {"status": "held", "reason": "editorial_evidence_invalid", "checked_at": 1}
    s.store.job_rows["j"]["live_publication_review"] = previous
    atomic_json(s.settings.data_dir / "exports/j/publication-review.json", {"reason": previous["reason"], "candidate": {"evidence_quote": "原话"}})
    s.ctl.validation_retry_candidate = lambda job: (job.get("live_publication_review") or {}).get("validation_version", 1) < VALIDATION_VERSION
    calls = []
    async def still_bad(*args):
        calls.append("full_review")
        raise ValueError("editorial_review_requires_changes")
    s.ctl.review = still_bad
    asyncio.run(s.ctl.tick())
    asyncio.run(s.ctl.tick())
    assert calls == ["full_review"]
    assert s.publisher.calls == []
    job = s.store.job("j")
    assert job["live_publication_review"]["validation_version"] == VALIDATION_VERSION
    assert job["live_publication_review"]["reason"] == "editorial_review_requires_changes"
    archive = s.settings.data_dir / "exports/j" / job["live_publication_review_history"][0]["archive"]
    assert json.loads(archive.read_text())["job_review"] == previous


def test_repaired_quote_cannot_republish_overlapping_footage(setup):
    from clipper.publication_overlap import capture_span
    s = setup
    arm(s, True)
    s.store.job_rows["j"]["live_publication_review"] = {"status": "held", "reason": "editorial_evidence_invalid"}
    s.ctl.validation_retry_candidate = lambda job: True
    s.store.publication_rows.append({"id": "existing", "job_id": "other", "status": "published",
                                     "capture_span": capture_span(s.store, s.store.job("j"))})
    async def should_not_review(*args):
        raise AssertionError("overlap must be checked before spending a model request")
    s.ctl.review = should_not_review
    asyncio.run(s.ctl.tick())
    assert s.publisher.calls == []
    assert s.store.job("j")["live_publication_review"]["reason"] == "source_overlap"


def test_validation_repairs_are_bounded_and_fresh_clips_go_first(setup):
    s = setup
    arm(s, True)
    for index in range(3):
        jid = f"held{index}"
        s.store.job_rows[jid] = {**s.store.job_rows["j"], "id": jid, "created_at": 6 + index,
                                "live_publication_review": {"status": "held", "reason": "editorial_evidence_invalid"}}
    s.ctl.validation_retry_candidate = lambda job: True
    order = []
    async def process(jid): order.append(jid)
    s.ctl.process = process
    asyncio.run(s.ctl.tick())
    assert order == ["j", "held2"]


def report():
    words = [{"start": i, "end": i + 1, "text": "说话"} for i in range(0, 90, 3)]
    return {"status": "complete", "applied": True, "model": "Qwen3-ASR-1.7B",
            "aligner": "Qwen3-ForcedAligner-0.6B", "timing_basis": "forced_alignment", "duration": 90,
            "text": "".join(w["text"] for w in words), "words": words,
            "original_subtitles": deepcopy(words), "reviewed_subtitles": deepcopy(words)}


def test_strong_asr_validates_actual_word_text_and_coverage():
    value = report()
    assert check_subtitle_integrity(value, 90)["word_count"] == 30
    value["words"] = value["words"][:2]
    value["text"] = "说话说话"
    value["reviewed_subtitles"] = value["reviewed_subtitles"][:2]
    with pytest.raises(ValueError, match="asr_speech_coverage_gap|asr_tail_missing"):
        check_subtitle_integrity(value, 90)


def test_applied_flag_cannot_hide_fallback_or_caption_omissions():
    value = report()
    value["timing_basis"] = "original_chunk_timing"
    with pytest.raises(ValueError, match="missing_forced_alignment"):
        check_subtitle_integrity(value, 90)
    value = report()
    value["reviewed_subtitles"] = value["reviewed_subtitles"][:-1]
    with pytest.raises(ValueError, match="caption_text_coverage_mismatch"):
        check_subtitle_integrity(value, 90)


def test_compressed_repetition_is_rejected_even_with_short_cue_duration():
    value = report()
    text = "看不见" * 12
    value["text"] += text
    value["words"].append({"start": 89, "end": 89.4, "text": text})
    value["reviewed_subtitles"].append({"start": 89, "end": 89.4, "text": text})
    with pytest.raises(ValueError, match="caption_repetition_compressed"):
        check_subtitle_integrity(value, 90)


def editorial():
    return {"ready": True, "reason": "已完整保留起因与收尾", "caption_ids": [0], "evidence_quote": "三首歌",
            "title_body": "今晚录三首歌，游戏先放一边", "cover_text": "今晚录三首\n游戏先放一边",
            "tags": ["录歌"], "tag_evidence": [{"tag": "录歌", "caption_ids": [0]}], "tid": 21}


def test_editorial_uses_real_identity_source_date_and_dynamic_category(setup):
    s = setup
    metadata, review = validate_editorial(editorial(), s.store.session("s"), s.store.job("j"),
                                          [{"id": 0, "text": "今晚要录三首歌"}], [{"id": 21}])
    assert metadata["title"].startswith("【阿布】")
    assert metadata["tags"] == ["Niya阿布", "阿布", "直播切片", "录歌"]
    assert "https://space.bilibili.com/188679" in metadata["description"]
    assert "https://live.bilibili.com/685026" in metadata["description"]
    assert metadata["tid"] == 21 and metadata["copyright"] == 2


def test_actual_wrong_password_claim_requires_cited_numeric_evidence(setup):
    s = setup
    value = {**editorial(), "caption_ids": [46, 83], "evidence_quote": "阀门是个奔驰。",
             "title_body": "把阀门图标看成奔驰，输入B246成功开启",
             "cover_text": "控制面板解谜\n图标看成奔驰\nB246成功开启", "tags": [], "tag_evidence": []}
    captions = [{"id": 46, "text": "阀门是个奔驰。"}, {"id": 83, "text": "开起来。"},
                {"id": 47, "text": "试一下B246，错了。"}, {"id": 82, "text": "所以B阀门是二五四。"}]
    # An actual model error from the real preflight: an uncited failed attempt
    # was presented as the successful final password.
    with pytest.raises(ValueError, match="editorial_numeric_claim_without_evidence"):
        validate_editorial(value, s.store.session("s"), s.store.job("j"), captions, [{"id": 21}])
    value.update(title_body="输入二四六成功开启", cover_text="输入二四六成功开启")
    with pytest.raises(ValueError, match="editorial_numeric_claim_without_evidence"):
        validate_editorial(value, s.store.session("s"), s.store.job("j"), captions, [{"id": 21}])
    value.update(title_body="把阀门图标看成奔驰", cover_text="阀门图标\n看成奔驰")
    assert validate_editorial(value, s.store.session("s"), s.store.job("j"), captions, [{"id": 21}])[0]["title"]


@pytest.mark.parametrize("number,accepted", [("254", True), ("25", False), ("246", False)])
def test_numeric_evidence_accepts_spoken_digits_but_not_partial_numbers(setup, number, accepted):
    s = setup
    value = {**editorial(), "title_body": "猜密码" + number, "cover_text": "猜密码" + number,
             "evidence_quote": "密码是二五四", "tags": [], "tag_evidence": []}
    args = (value, s.store.session("s"), s.store.job("j"), [{"id": 0, "text": "密码是二五四"}], [{"id": 21}])
    if accepted:
        assert validate_editorial(*args)[0]["title"].endswith(number)
    else:
        with pytest.raises(ValueError, match="editorial_numeric_claim_without_evidence"):
            validate_editorial(*args)


@pytest.mark.parametrize("change,expected", [
    ({"evidence_quote": "不存在的话"}, "editorial_evidence_invalid"),
    ({"caption_ids": [99]}, "editorial_evidence_invalid"),
    ({"tag_evidence": []}, "editorial_tag_without_evidence"),
    ({"tid": 31}, "editorial_category_invalid"),
    ({"ready": False}, "editorial_review_requires_changes"),
])
def test_editorial_rejects_unsupported_claims_and_tags(setup, change, expected):
    s = setup
    value = {**editorial(), **change}
    with pytest.raises(ValueError, match=expected):
        validate_editorial(value, s.store.session("s"), s.store.job("j"),
                           [{"id": 0, "text": "今晚要录三首歌"}], [{"id": 21}])


def test_patrol_adoption_follows_durable_policy(setup):
    s = setup
    arm(s, True)
    async def scenario():
        patrol = FollowingPatrol(s.settings, s.settings.data_dir / "patrol")
        calls = []
        async def request(method, path, payload=None): calls.append((method, path, payload)); return {}
        patrol.request = request
        await patrol.adopt(s.store.session("s"), {"room_id": 685026, "name": "Niya阿布"})
        await patrol.client.aclose()
        assert calls[0][2]["auto_publish"] is True
        assert calls[0][2]["publish_profile"]["source"] == "https://live.bilibili.com/685026"
        assert not any("/publish" in path for _, path, _ in calls)
    asyncio.run(scenario())


def test_http_render_completion_publishes_before_live_session_stops(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from clipper.server import create_app
    settings = Settings(data_dir=tmp_path / "data")
    settings.data_dir.mkdir()
    atomic_json(settings.data_dir / POLICY_NAME, {"enabled": True, "eligible_after": 1, "account_uid": 42})
    app = create_app(settings)
    store = app.state.store
    session = store.create_session(mode="live", status="live", duration=300, streamer="Niya阿布",
                                   streamer_uid=188679, room={"owner_uid": 188679, "room_id": 685026})
    anchor = store.add_anchor(session["id"], start=100, end=190, timestamp=150, title="待审标题")
    observed = []
    controller = app.state.live_publishing
    async def rendered(renderer, jid):
        folder = settings.data_dir / "exports" / jid
        folder.mkdir(parents=True)
        (folder / "clip.mp4").write_bytes(b"rendered video")
        (folder / "cover.jpg").write_bytes(b"reviewed cover")
        renderer.store.update_job(jid, status="complete", duration=90)
    async def review(job, current):
        assert current["status"] == "live"
        return {"title": "审核后的标题"}, {"status": "ready"}
    async def apply(*args): pass
    def enqueue(job, metadata, automatic=False, expected_media=None, expected_account_uid=None):
        assert expected_account_uid == 42
        assert expected_media["cover_sha256"] == hashlib.sha256(b"reviewed cover").hexdigest()
        observed.append((job["id"], metadata["title"], store.session(session["id"])["status"]))
        return store.create_publication(job["id"], status="queued")
    async def run(pid): store.update_publication(pid, status="submitted")
    async def account(fresh=False): return {"logged_in": True, "uid": 42}
    monkeypatch.setattr("clipper.server.Renderer.run", rendered)
    controller.review, controller.apply_review = review, apply
    controller.publisher.enqueue, controller.publisher.run = enqueue, run
    controller.publisher.account = account
    with TestClient(app) as client:
        response = client.post(f"/api/anchors/{anchor['id']}/render", json={})
        assert response.status_code == 202
        jid = response.json()["id"]
        deadline = time.monotonic() + 2
        while not observed and time.monotonic() < deadline:
            time.sleep(.01)
        assert observed == [(jid, "审核后的标题", "live")]
        assert store.session(session["id"])["status"] == "live"


def test_http_publish_binds_hashes_and_account_outside_metadata(tmp_path):
    from fastapi.testclient import TestClient
    from clipper.server import create_app
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings)
    store = app.state.store
    session = store.create_session(mode="file", status="ready")
    anchor = store.add_anchor(session["id"], start=0, end=90, timestamp=30, title="测试")
    job = store.create_job(anchor["id"], session["id"], anchor=anchor, status="complete")
    captured = {}
    async def validate(metadata):
        assert "expected_media" not in metadata and "expected_account_uid" not in metadata
        return metadata
    def enqueue(job, metadata, **binding):
        captured.update(binding)
        return store.create_publication(job["id"], status="queued")
    async def run(pid): pass
    app.state.publisher.validate, app.state.publisher.enqueue, app.state.publisher.run = validate, enqueue, run
    payload = {"title": "测试稿件", "description": "说明", "tags": ["直播切片"], "tid": 21,
               "copyright": 2, "source": "https://live.bilibili.com/123",
               "expected_media": {"video_sha256": "a" * 64, "cover_sha256": "b" * 64},
               "expected_account_uid": 42}
    with TestClient(app) as client:
        bad = deepcopy(payload)
        bad["expected_media"]["cover_sha256"] = "bad hash"
        assert client.post(f"/api/jobs/{job['id']}/publish", json=bad).status_code == 422
        assert not captured
        assert client.post(f"/api/jobs/{job['id']}/publish", json=payload).status_code == 202
        assert captured == {"expected_media": payload["expected_media"], "expected_account_uid": 42}
