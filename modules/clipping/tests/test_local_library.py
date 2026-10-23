import asyncio
import copy
import json
from urllib.parse import urljoin

import httpx
import pytest

from clipper.library import LibraryRefreshError, build_local_manifest, refresh_library


JOB = "a" * 16
NEW_JOB = "b" * 16


@pytest.fixture
def gallery(tmp_path):
    state = {
        "sessions": [{"id": "patrol", "mode": "live", "capture_owner": "following-patrol-test",
                      "created_at": 1792498057, "streamer": "阿梓", "streamer_uid": 7706705,
                      "room": {"room_id": 80397}, "auto_render": True,
                      "cookie": "SESSDATA=do-not-expose", "model_endpoint": "private-internal"}],
        "jobs": [{"id": JOB, "session_id": "patrol", "created_at": 1792498153,
                  "status": "complete", "duration": 120, "anchor": {"title": "这是我的歪理", "start": 28},
                  "quality_review": {"publish_ready": False, "report": "/private/report"},
                  "publication_metadata": {"description": "private-custom-description"},
                  "error": "secret traceback", "video_url": "signed-private-url"}],
        "publications": [], "credential": "private-api-key",
    }
    data = tmp_path / "data"
    exports = data / "exports" / JOB
    exports.mkdir(parents=True)
    (exports / "clip.mp4").write_bytes(bytes(range(256)) * 4)
    (exports / "cover.jpg").write_bytes(b"jpeg")
    (exports / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n")
    (exports / "package.zip").write_bytes(b"DO NOT SERVE PRIVATE EXPORT ZIP")
    (exports / "metadata.json").write_text('{"secret":"private"}')
    output = tmp_path / "static" / "clips" / "library.json"
    return state, data, output


def test_manifest_uses_allowlist_real_files_and_prefix_safe_urls(gallery):
    state, data, output = gallery
    body = build_local_manifest(state, {}, data)
    assert body["counts"]["clips"] == 1
    files = body["clips"][0]["files"]
    assert set(files) == {"clip.mp4", "cover.jpg", "subtitles.srt"}
    assert files["clip.mp4"] == {"url": f"../../api/exports/{JOB}/clip.mp4", "size": 1024}
    for prefix in ("", "/proxy/6880"):
        assert urljoin(f"http://example{prefix}/static/clips/index.html", files["clip.mp4"]["url"]) == (
            f"http://example{prefix}/api/exports/{JOB}/clip.mp4")
    encoded = json.dumps(body)
    assert "private" not in encoded and "SESSDATA" not in encoded and "secret" not in encoded
    assert str(data) not in encoded


@pytest.mark.parametrize("field,value", [("status", "rendering"), ("superseded_by", NEW_JOB),
                                        ("session_id", "other"), ("id", "../secrets")])
def test_ineligible_clips_are_hidden(gallery, field, value):
    state, data, output = gallery
    state["jobs"][0][field] = value
    assert build_local_manifest(state, {}, data)["clips"] == []


def test_missing_video_is_hidden(gallery):
    state, data, output = gallery
    (data / "exports" / JOB / "clip.mp4").unlink()
    assert build_local_manifest(state, {}, data)["clips"] == []


@pytest.mark.parametrize("owner", [None, "", "manual", "extra-capture", "following-patrol-test"])
def test_manual_and_patrol_live_clips_are_visible_regardless_of_owner(gallery, owner):
    state, data, _ = gallery
    session = state["sessions"][0]
    if owner is None:
        session.pop("capture_owner")
    else:
        session["capture_owner"] = owner
    result = build_local_manifest(state, {}, data)
    assert [clip["id"] for clip in result["clips"]] == [JOB]
    assert result["clips"][0]["streamer"]["profile_url"] == "https://space.bilibili.com/7706705"
    assert "SESSDATA" not in json.dumps(result) and "private" not in json.dumps(result)


def test_import_without_bilibili_identity_is_visible_without_private_source_fields(gallery):
    state, data, _ = gallery
    state["sessions"][0] = {
        "id": "patrol", "mode": "file", "created_at": 1792498057,
        "title": "private-filename", "media_path": "/private/recording.mp4",
        "source_url": "https://private.example/recording?token=secret",
        "model_endpoint": "private-internal", "cookie": "SESSDATA=do-not-expose",
        "publish_profile": {"description": "private-profile-description"},
    }
    result = build_local_manifest(state, {}, data)
    clip = result["clips"][0]
    assert clip["streamer"] == {"uid": "", "name": "导入素材", "room_id": "",
                                 "profile_url": "", "live_url": ""}
    assert clip["title"] == "这是我的歪理"
    assert clip["description"] == "" and clip["source_url"] == "" and clip["captured_at"] == ""
    assert result["counts"]["clips"] == 1 and result["counts"]["streamers"] == 1
    assert set(clip["files"]) == {"clip.mp4", "cover.jpg", "subtitles.srt"}
    encoded = json.dumps(result)
    assert "private" not in encoded and "SESSDATA" not in encoded and "secret" not in encoded
    state["jobs"][0]["status"] = "rendering"
    assert build_local_manifest(state, {}, data)["clips"] == []


@pytest.mark.parametrize("identity,expected", [
    ({"streamer": "演示主播"}, {"name": "演示主播", "uid": "", "room_id": "", "profile_url": "", "live_url": ""}),
    ({"streamer_uid": 123}, {"name": "主播 123", "uid": "123", "room_id": "", "profile_url": "https://space.bilibili.com/123", "live_url": ""}),
    ({"room": {"room_id": 456}}, {"name": "手动直播", "uid": "", "room_id": "456", "profile_url": "", "live_url": "https://live.bilibili.com/456"}),
    ({}, {"name": "手动直播", "uid": "", "room_id": "", "profile_url": "", "live_url": ""}),
])
def test_manual_live_clip_with_partial_identity_keeps_only_known_links(gallery, identity, expected):
    state, data, _ = gallery
    state["sessions"][0] = {"id": "patrol", "mode": "live", **identity}
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["streamer"] == expected
    assert clip["source_url"] == expected["live_url"]


@pytest.mark.parametrize("mode", [None, "unsupported"])
def test_unsupported_session_modes_are_hidden(gallery, mode):
    state, data, _ = gallery
    state["sessions"][0]["mode"] = mode
    assert build_local_manifest(state, {}, data)["clips"] == []


def test_symlink_files_folders_and_exports_are_hidden(gallery, tmp_path):
    state, data, output = gallery
    video = data / "exports" / JOB / "clip.mp4"
    video.unlink()
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"private")
    video.symlink_to(secret)
    assert build_local_manifest(state, {}, data)["clips"] == []
    video.unlink()
    video.write_bytes(b"public")
    folder = data / "exports" / JOB
    moved = data / "exports" / "saved-job"
    folder.rename(moved)
    folder.symlink_to(moved, target_is_directory=True)
    assert build_local_manifest(state, {}, data)["clips"] == []
    folder.unlink()
    moved.rename(folder)
    exports = data / "exports"
    moved_exports = data / "moved-exports"
    exports.rename(moved_exports)
    exports.symlink_to(moved_exports, target_is_directory=True)
    assert build_local_manifest(state, {}, data)["clips"] == []


def test_new_completed_clip_appears_without_size_limit(gallery):
    state, data, output = gallery
    assert len(build_local_manifest(state, {}, data)["clips"]) == 1
    new_job = copy.deepcopy(state["jobs"][0])
    new_job.update(id=NEW_JOB, created_at=new_job["created_at"] + 1)
    state["jobs"].append(new_job)
    export = data / "exports" / NEW_JOB
    export.mkdir()
    with (export / "clip.mp4").open("wb") as file:
        file.truncate(100 * 1024 * 1024)
    body = build_local_manifest(state, {}, data)
    assert body["counts"]["clips"] == 2
    assert body["clips"][0]["id"] == NEW_JOB
    assert body["clips"][0]["files"]["clip.mp4"]["size"] == 100 * 1024 * 1024


def test_refresh_reads_workbench_and_patrol_then_atomically_updates(gallery):
    state, data, output = gallery
    patrol = data / "following-patrol" / "status.json"
    patrol.parent.mkdir()
    patrol.write_text(json.dumps({"status": "watching", "current_visit": {"session_id": "patrol"},
                                 "live_following_count": 3, "cookie": "private"}))
    requests = []

    def workbench(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=state)

    result = asyncio.run(refresh_library(data, output, transport=httpx.MockTransport(workbench)))
    assert requests == [("GET", "/api/state")]
    assert result == json.loads(output.read_text())
    assert result["patrol"]["current_streamer"]["name"] == "阿梓"
    assert result["patrol"]["status"] == "watching"
    assert "private" not in output.read_text()
    assert list(output.parent.iterdir()) == [output]
    state["jobs"][0]["superseded_by"] = NEW_JOB
    asyncio.run(refresh_library(data, output, transport=httpx.MockTransport(workbench)))
    assert json.loads(output.read_text())["clips"] == []


@pytest.mark.parametrize("failure", ["offline", "http", "invalid", "redirect", "bad_json"])
def test_workbench_failure_retains_last_manifest_without_secret_errors(gallery, failure):
    state, data, output = gallery
    output.parent.mkdir(parents=True)
    previous = json.dumps({"clips": [{"id": "previous"}]})
    output.write_text(previous)

    def workbench(request):
        if failure == "offline":
            raise httpx.ConnectError("private backend details", request=request)
        if failure == "http":
            return httpx.Response(500, text="private server error")
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://private.example"})
        if failure == "bad_json":
            return httpx.Response(200, text="private invalid JSON")
        return httpx.Response(200, json={"private": "invalid snapshot"})

    with pytest.raises(LibraryRefreshError, match="^workbench_unavailable$"):
        asyncio.run(refresh_library(data, output, transport=httpx.MockTransport(workbench)))
    assert output.read_text() == previous
    assert list(output.parent.iterdir()) == [output]


def test_refresh_write_failure_is_fixed_and_preserves_previous(gallery, monkeypatch):
    state, data, output = gallery
    output.parent.mkdir(parents=True)
    output.write_text("previous manifest")

    def fail_replace(*args):
        raise OSError("private pathname")

    monkeypatch.setattr("clipper.library.os.replace", fail_replace)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=state))
    with pytest.raises(LibraryRefreshError, match="^library_write_failed$"):
        asyncio.run(refresh_library(data, output, transport=transport))
    assert output.read_text() == "previous manifest"
    assert list(output.parent.iterdir()) == [output]


def test_published_automatic_review_is_not_a_manual_review_task(gallery):
    state, data, _ = gallery
    state["jobs"][0]["live_publication_review"] = {
        "status": "submitted", "reason": "private model response", "model": "private-model",
    }
    state["publications"] = [
        {"job_id": JOB, "status": "published", "bvid": "BV1sVns6AEuD", "created_at": 1},
        {"job_id": JOB, "status": "failed", "created_at": 2, "error": "private cookie"},
    ]
    result = build_local_manifest(state, {}, data)
    clip = result["clips"][0]
    assert clip["status"] == "published" and clip["status_label"] == "已公开"
    assert clip["review"] == {"status": "approved", "label": "自动审稿通过", "source": "automatic"}
    assert clip["review_required"] is False and clip["warning"] == ""
    assert result["counts"]["review_required"] == 0
    assert result["counts"]["published"] == 1 and result["counts"]["ready"] == 0
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("platform,display", [
    ("submitted", "submitted"), ("transcoding", "submitted"), ("reviewing", "submitted"),
    ("queued", "publishing"), ("uploading", "publishing"), ("submitting", "publishing"),
    ("uncertain", "publication_uncertain"), ("failed", "publication_failed"),
    ("private", "private"), ("rejected", "rejected"),
])
def test_platform_progress_is_preserved_and_takes_precedence(gallery, platform, display):
    state, data, _ = gallery
    state["jobs"][0]["live_publication_review"] = {"status": "held", "reason": "private-error"}
    state["publications"] = [{"job_id": JOB, "status": platform, "url": "https://secret.example/token"}]
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == display
    assert clip["publication"] == {"status": platform, "bvid": "", "url": ""}
    assert "private-error" not in json.dumps(clip) and "secret.example" not in json.dumps(clip)


@pytest.mark.parametrize("reason,expected", [
    ("editorial_evidence_invalid", "标题或内容的证据核验未通过。"),
    ("editorial_numeric_claim_without_evidence", "数字表述缺少足够证据。"),
    ("secret traceback https://internal.example/token", "自动审稿未通过，需要进一步核对。"),
])
def test_held_review_explains_safe_actual_reason(gallery, reason, expected):
    state, data, _ = gallery
    state["jobs"][0]["live_publication_review"] = {"status": "held", "reason": reason}
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == "held" and clip["status_detail"] == expected
    assert clip["review_required"] is True
    assert "secret" not in json.dumps(clip) and "internal.example" not in json.dumps(clip)


def test_historical_unreviewed_and_current_waiting_are_different(gallery):
    state, data, _ = gallery
    job = state["jobs"][0]
    state["live_publication_policy"] = {"enabled": True, "eligible_after": job["created_at"] + 1}
    state["sessions"][0]["auto_publish"] = True
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == "not_reviewed" and "历史片段" in clip["status_detail"]
    state["live_publication_policy"]["eligible_after"] = job["created_at"]
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == "pending_review" and clip["review_required"] is False
    job["live_publication_review"] = {"status": "reviewing"}
    assert build_local_manifest(state, {}, data)["clips"][0]["status"] == "reviewing"


def test_legacy_approved_is_preserved_but_short_video_is_not_ready(gallery):
    state, data, _ = gallery
    job = state["jobs"][0]
    job["editorial_review"] = {"publication_allowed": True, "reason": "private"}
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == "ready" and clip["review"]["source"] == "legacy_auto"
    assert clip["review"]["status"] == "approved"
    job["duration"] = 59.9
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == "below_minimum_duration" and "60秒" in clip["status_detail"]
    assert clip["review"]["source"] == "legacy_auto"
    job["duration"] = 60
    job["anchor"]["score"] = 72
    assert build_local_manifest(state, {}, data)["clips"][0]["status"] == "ready"


def test_known_halted_batch_is_displayed_without_changing_publication(gallery):
    state, data, _ = gallery
    directory = data / "publication-batches" / "old-batch"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps({
        "id": "old-batch", "status": "halted", "halt_reason": "媒体已变更，需要重新确认后另建计划", "records": {},
    }))
    (directory / "plan.json").write_text(json.dumps({
        "id": "old-batch", "items": [{"job_id": JOB}], "state_file": "/private/never-follow-this-path",
    }))
    state["jobs"][0]["editorial_review"] = {"publication_allowed": True}
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == "publication_paused"
    assert clip["review"]["source"] == "legacy_auto"
    assert "素材在审稿后有变更" in clip["status_detail"]
    assert "private" not in json.dumps(clip)
    state["publications"] = [{"job_id": JOB, "status": "published", "bvid": "BV1sVns6AEuD"}]
    assert build_local_manifest(state, {}, data)["clips"][0]["status"] == "published"
    state["publications"] = []
    state["jobs"][0]["duration"] = 30
    assert build_local_manifest(state, {}, data)["clips"][0]["status"] == "below_minimum_duration"


def test_batch_symlink_and_unknown_halt_text_are_not_exposed(gallery, tmp_path):
    state, data, _ = gallery
    root = data / "publication-batches"
    root.mkdir()
    outside = tmp_path / "private-batch"
    outside.mkdir()
    (outside / "state.json").write_text(json.dumps({
        "id": "private-batch", "status": "halted", "halt_reason": "SESSDATA=secret", "records": {},
    }))
    (outside / "plan.json").write_text(json.dumps({"id": "private-batch", "items": [{"job_id": JOB}]}))
    (root / "linked").symlink_to(outside, target_is_directory=True)
    clip = build_local_manifest(state, {}, data)["clips"][0]
    assert clip["status"] == "not_reviewed" and "secret" not in json.dumps(clip)
