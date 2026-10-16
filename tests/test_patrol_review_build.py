"""Static review exports: synthetic private fields, no actual patrol data."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

import pytest

from scripts import build_patrol_review as builder


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def inputs(tmp_path):
    run, output, assets = tmp_path / "run", tmp_path / "output", tmp_path / "templates"
    assets.mkdir()
    (assets / "index.html").write_text('<!doctype html><script id="review-data" type="application/json">{{REVIEW_DATA}}</script><script src="review.js"></script>')
    (assets / "review.css").write_text("body { color: white; }")
    (assets / "review.js").write_text("window.reviewTest = true;")
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"synthetic-logo")
    start = 1_800_000_000
    snapshot = datetime.fromtimestamp(start + 600, timezone.utc).isoformat(timespec="seconds")
    hostile = '</script><script>alert("x")</script><b>&\u2028\u2029'
    long_answer = "这是保留完整的回答。\n" * 2500 + hostile
    chorus = {"updated_at": start + 95, "recorded": True, "token": "SECRET_TOKEN",
        "items": [{"text": "大家一起看", "count": 8, "total": 12, "window_seconds": 30,
                   "uid": 123456, "uname": "SECRET_VIEWER"}]}
    visit = {"visit_id": "001-9999", "session_id": "SECRET_SESSION", "started_at": start,
        "session_started_at": start, "ended_at": None, "scheduled_end_at": start + 1800,
        "status": "observing", "simulation_rounds_per_visit": 10, "simulation_interval_seconds": 180,
        "room": {"room_id": 9999, "title": hostile, "area": "游戏", "uname": "SECRET_OWNER", "uid": 42},
        "sent_count": 0, "candidate_total": 17, "candidate_batches": 3,
        "lag": {"median": 5.2, "p95": 8.1, "max": 12, "token": "SECRET_TOKEN"},
        "contents": [{"type": "observation", "offset": i * 30, "text": f"第{i}段观察" + "字" * 300 + "DO_NOT_EXPORT_OBSERVATION_TAIL\n弹幕变化：SECRET_VIEWER"} for i in range(20)],
        "simulation_rounds": [{"round": i + 1, "time": start + i * 90 + 45,
            "target_at": start + i * 90 + 45, "status": "complete", "snapshot_time": i * 90 + 40,
            "candidates": [f"第{i}轮候选{k}" for k in range(5)], "chorus": chorus,
            "uid": 42, "session_id": "SECRET_SESSION"} for i in range(2)] + [
            {"round": 3, "status": "skipped", "reason": "没有足够候选", "candidates": [], "chorus": {}}],
        "questions": [{"question_id": "SECRET_QID", "question": "刚才游戏讲什么？", "answer": long_answer,
            "status": "complete", "kind": "game", "origin": "planned", "requested_at": start + 165,
            "answered_at": start + 175, "snapshot_time": 160, "evidence_times": [154, 156], "replay_times": [154],
            "session_id": "SECRET_SESSION", "api_url": "SECRET_ENDPOINT", "events": [{"cookie": "SECRET_COOKIE"}]},
            {"question": "还有呢？", "answer": "这是第二个完整回答。", "status": "complete", "origin": "manual"},
            {"question": "尚未完成？", "answer": "", "status": "queued", "origin": "planned"}],
        "candidates": [{"time": start + i * 15, "generated_at": start + i * 15,
            "items": [hostile if i == 0 and k == 0 else f"原始{i}-{k}" for k in range(n)],
            "status": "ready", "username": "SECRET_VIEWER", "token": "SECRET_TOKEN"}
            for i, n in enumerate((10, 5, 2))],
        "chorus_windows": [{"time": start + i * 10, **chorus} for i in range(12)],
        "recordings": [{"file": "recordings/part-001.mkv", "bytes": 123456,
                        "started_at": start + 3, "ended_at": start + 590, "pid": 9876}],
        "cookie": "SECRET_COOKIE", "rawlogs": "SECRET_RAWLOG"}
    report = {"schema_version": 1, "run_id": "synthetic-run", "generated_at": snapshot,
        "started_at": start, "deadline": start + 86400, "status": "running", "visits": [visit],
        "totals": {"candidate_total": 17, "candidate_batches": 3, "chorus_windows": 12},
        "config": {"cookie": "SECRET_COOKIE", "token": "SECRET_TOKEN"}}
    write_json(run / "report.json", report)
    # Visit files may advance after the frozen report; do not promote their
    # newer lifecycle into a snapshot containing older candidate/QA records.
    write_json(run / "visits/001-9999/visit.json", {"requested_room_id": "510", "ended_at": start + 900,
        "status": "finished", "session_id": "SECRET_SESSION", "cookie": "SECRET_COOKIE"})
    return {"run": run, "output": output, "assets": assets, "logo": logo,
            "report": report, "start": start, "snapshot": snapshot,
            "hostile": hostile, "long_answer": long_answer}


def build(fixture):
    return builder.build_review(fixture["run"], fixture["output"],
                                assets_dir=fixture["assets"], logo_source=fixture["logo"])


def payload(fixture):
    html = (fixture["output"] / "index.html").read_text()
    encoded = re.search(r'<script id="review-data" type="application/json">(.*?)</script>', html, re.S).group(1)
    return json.loads(encoded), encoded


def extra_payload(fixture):
    text = (fixture["output"] / "details/001-9999.js").read_text()
    prefix = 'window.PATROL_EXTRA=window.PATROL_EXTRA||{};window.PATROL_EXTRA["001-9999"]='
    assert text.startswith(prefix) and text.endswith(";\n")
    return json.loads(text[len(prefix):-2]), text


def test_complete_rounds_and_full_answers_are_exported_without_copying_raw_reports(inputs):
    metadata = build(inputs)
    data, _ = payload(inputs)
    visit = data["visits"][0]
    assert len(visit["rounds"]) == 3
    assert [row["candidates"] for row in visit["rounds"][:2]] == [row["candidates"] for row in inputs["report"]["visits"][0]["simulation_rounds"][:2]]
    assert visit["questions"][0]["answer"] == inputs["long_answer"]
    assert len(visit["questions"]) == 3 and visit["questions"][2]["status"] == "queued"
    assert visit["questions"][0]["id"] == "q1"
    assert visit["questions"][0]["snapshot_time"] == 160
    assert visit["questions"][0]["evidence_times"] == [154, 156]
    assert metadata["totals"]["simulated_candidates"] == 10
    assert metadata["totals"]["candidate_total"] == 17
    assert metadata["totals"]["answers_complete"] == 2
    assert len(visit["observations"]) == 5
    assert all(len(row["text"]) <= 220 for row in visit["observations"])
    assert not list(inputs["output"].glob("**/*.jsonl"))
    assert not (inputs["output"] / "report.json").exists()
    assert (inputs["output"] / "assets/logo.png").read_bytes() == b"synthetic-logo"


def test_script_json_cannot_close_script_or_insert_markup_in_main_or_lazy_archive(inputs):
    build(inputs)
    data, embedded = payload(inputs)
    extra, script = extra_payload(inputs)
    assert data["visits"][0]["title"] == inputs["hostile"]
    assert extra["candidate_batches"][0]["items"][0] == inputs["hostile"]
    for encoded in (embedded, script):
        assert "</script" not in encoded and "<" not in encoded and ">" not in encoded and "&" not in encoded
        assert "\u2028" not in encoded and "\u2029" not in encoded
        assert "\\u003c" in encoded and "\\u0026" in encoded
        assert "\\u2028" in encoded and "\\u2029" in encoded


def test_lazy_archives_keep_every_candidate_and_chorus_window_with_only_public_fields(inputs):
    build(inputs)
    data, _ = payload(inputs)
    extra, _ = extra_payload(inputs)
    assert data["visits"][0]["extra_url"] == "details/001-9999.js"
    assert [len(row["items"]) for row in extra["candidate_batches"]] == [10, 5, 2]
    assert len(extra["chorus_windows"]) == 12
    assert extra["chorus_windows"][0]["items"] == [{"text": "大家一起看", "count": 8, "total": 12, "window_seconds": 30}]
    texts = "\n".join(path.read_text() for path in inputs["output"].rglob("*") if path.is_file() and path.suffix in {".html", ".js"})
    for secret in ("SECRET_COOKIE", "SECRET_TOKEN", "SECRET_VIEWER", "SECRET_OWNER", "SECRET_SESSION",
                   "SECRET_QID", "SECRET_ENDPOINT", "SECRET_RAWLOG", "DO_NOT_EXPORT_OBSERVATION_TAIL"):
        assert secret not in texts

    def check_keys(value):
        if isinstance(value, dict):
            assert not set(value) & {"uid", "uname", "username", "cookie", "token", "api_url", "session_id", "question_id", "contents", "events"}
            for child in value.values():
                check_keys(child)
        elif isinstance(value, list):
            for child in value:
                check_keys(child)

    check_keys(data)
    check_keys(extra)


def test_running_export_remains_a_fixed_snapshot_not_the_build_or_newer_visit_time(inputs):
    build(inputs)
    data, _ = payload(inputs)
    visit = data["visits"][0]
    assert data["snapshot"] is True and data["snapshot_at"] == inputs["snapshot"]
    assert data["built_at"] != data["snapshot_at"]
    assert data["status"] == "running" and data["timezone"] == "Asia/Shanghai"
    assert visit["room_id"] == "510" and visit["canonical_room_id"] == "9999"
    assert visit["duration_seconds"] == 600 and visit["ended_at"] is None and visit["status"] == "observing"
    assert visit["thumbnail"] is None and visit["preview"] is None
    assert visit["recordings"][0]["url"] is None
    assert data["totals"]["recording_bytes"] == 123456


@pytest.mark.parametrize("visit_allowed,since,mismatch", [
    (True, 1_800_000_005, False), (True, 1_800_000_020, True),
    (False, None, True), (None, None, True),
])
def test_manual_sends_export_separately_with_frozen_visit_authorization(
        inputs, visit_allowed, since, mismatch):
    report = inputs["report"]
    report["config"].update(candidate_only=True, allow_manual_send=True, send_authorized=False)
    visit = report["visits"][0]
    if visit_allowed is not None:
        visit["allow_manual_send"] = visit_allowed
    visit["allow_manual_send_since"] = since
    visit["sent_count"] = 1
    visit["sends"] = [{"text": "好听 SESSDATA=SECRET_COOKIE", "sent_at": inputs["start"] + 10,
                       "offset": 9, "source": "manual", "uid": 123456,
                       "uname": "SECRET_VIEWER", "context": [{"cookie": "SECRET_COOKIE"}]}]
    write_json(inputs["run"] / "report.json", report)
    path = inputs["run"] / "visits/001-9999/visit.json"
    metadata = json.loads(path.read_text())
    metadata["allow_manual_send"] = True
    write_json(path, metadata)
    build(inputs)
    data, encoded = payload(inputs)
    exported = data["visits"][0]
    assert data["config"] == {"candidate_only": True, "allow_manual_send": True, "send_authorized": False}
    assert exported["allow_manual_send"] is (visit_allowed is True)
    assert exported["sending_policy_mismatch"] is mismatch
    assert data["totals"]["manual_sent"] == data["totals"]["sent"] == 1
    assert data["totals"]["sending_policy_mismatches"] == int(mismatch)
    assert exported["sends"] == [{"text": "好听 [已隐去敏感字段]", "sent_at": inputs["start"] + 10,
                                  "offset": 9, "source": "manual"}]
    assert data["totals"]["simulated_candidates"] == 10
    assert "SECRET_COOKIE" not in encoded and "SECRET_VIEWER" not in encoded
    assert '"uid"' not in encoded and '"uname"' not in encoded


@pytest.mark.parametrize("selection_source,expected", [
    ("requested_first", True), ("requested_queue", True),
    ("following", False), ("virtual_area", False), (None, False),
])
def test_requested_visit_requires_explicit_selection_source_not_requested_room_id(inputs, selection_source, expected):
    path = inputs["run"] / "visits/001-9999/visit.json"
    metadata = json.loads(path.read_text())
    metadata["room"] = {"selection_source": selection_source}
    write_json(path, metadata)
    build(inputs)
    data, _ = payload(inputs)
    visit = data["visits"][0]
    assert visit["room_id"] == "510"
    assert visit["is_requested"] is expected
    assert "selection_source" not in visit


def test_report_selection_source_takes_precedence_over_newer_metadata(inputs):
    inputs["report"]["visits"][0]["room"]["selection_source"] = "following"
    write_json(inputs["run"] / "report.json", inputs["report"])
    path = inputs["run"] / "visits/001-9999/visit.json"
    metadata = json.loads(path.read_text())
    metadata["room"] = {"selection_source": "requested_queue"}
    write_json(path, metadata)
    build(inputs)
    data, _ = payload(inputs)
    assert data["visits"][0]["is_requested"] is False


def test_existing_safe_media_manifest_merges_without_copying_private_metadata(inputs):
    media = inputs["output"] / "media/001-9999"
    media.mkdir(parents=True)
    for name in ("thumb.jpg", "preview.mp4", "part-001.mkv"):
        (media / name).write_bytes(b"fixture")
    write_json(inputs["output"] / "media-manifest.json", {"visits": {"001-9999": {
        "thumbnail": "media/001-9999/thumb.jpg",
        "preview": {"url": "media/001-9999/preview.mp4", "source_file": "/private/SECRET_SESSION/part-001.mkv",
                    "start_seconds": 10, "duration_seconds": 8, "timestamp_basis": "media seconds", "token": "SECRET_TOKEN"},
        "recordings": [{"name": "part-001.mkv", "url": "media/001-9999/part-001.mkv",
                        "duration_seconds": 580, "session_id": "SECRET_SESSION"}]}}})
    build(inputs)
    data, embedded = payload(inputs)
    visit = data["visits"][0]
    assert visit["thumbnail"].endswith("thumb.jpg")
    assert visit["preview"]["source_file"] == "part-001.mkv"
    assert visit["preview"]["start_seconds"] == 10
    assert visit["recordings"][0]["url"].endswith("part-001.mkv")
    assert visit["recordings"][0]["bytes"] == 123456
    assert "SECRET_SESSION" not in embedded and "SECRET_TOKEN" not in embedded


@pytest.mark.parametrize("url", ["https://example.com/a.mp4", "javascript:alert(1)", "//example.com/x", "media/../private.jpg", "media/%2e%2e/private.jpg", "media/x.mp4?token=SECRET", "/etc/passwd"])
def test_unsafe_or_credential_bearing_media_links_are_not_exported(inputs, url):
    write_json(inputs["output"] / "media-manifest.json", {"visits": {"001-9999": {"thumbnail": url, "preview": {"url": url}}}})
    build(inputs)
    data, _ = payload(inputs)
    assert data["visits"][0]["thumbnail"] is None and data["visits"][0]["preview"] is None


def test_invalid_visit_id_never_creates_a_lazy_script_outside_output(inputs):
    inputs["report"]["visits"][0]["visit_id"] = "../../escaped"
    write_json(inputs["run"] / "report.json", inputs["report"])
    with pytest.raises(ValueError, match="identifier"):
        build(inputs)
    assert not (inputs["output"] / "index.html").exists()


def test_atomic_index_write_keeps_previous_complete_snapshot_on_replace_failure(inputs, monkeypatch):
    build(inputs)
    old = (inputs["output"] / "index.html").read_bytes()
    inputs["report"]["visits"][0]["room"]["title"] = "下一份快照"
    write_json(inputs["run"] / "report.json", inputs["report"])
    original = builder.os.replace

    def fail_index(source, target):
        if Path(target).name == "index.html":
            raise OSError("synthetic rename failure")
        return original(source, target)

    monkeypatch.setattr(builder.os, "replace", fail_index)
    with pytest.raises(OSError):
        build(inputs)
    assert (inputs["output"] / "index.html").read_bytes() == old
    assert not list(inputs["output"].glob(".index.html.*"))


def test_sensitive_assignments_accidentally_quoted_in_prose_are_redacted(inputs):
    inputs["report"]["visits"][0]["questions"][0]["answer"] = "普通回答。 SESSDATA=SECRET_COOKIE token=SECRET_TOKEN uid：123456 结尾。"
    write_json(inputs["run"] / "report.json", inputs["report"])
    build(inputs)
    data, text = payload(inputs)
    answer = data["visits"][0]["questions"][0]["answer"]
    assert "普通回答" in answer and "结尾" in answer and "已隐去敏感字段" in answer
    assert "SECRET_COOKIE" not in text and "SECRET_TOKEN" not in text and "123456 结尾" not in text


def test_legacy_ten_to_five_selection_and_chorus_snapshot_notes_remain_visible(inputs):
    row = inputs["report"]["visits"][0]["simulation_rounds"][0]
    row.update(source_candidate_count=10, candidate_selection="first_5_of_legacy_10",
               chorus_capture_note="此处为记录时快照")
    write_json(inputs["run"] / "report.json", inputs["report"])
    build(inputs)
    data, _ = payload(inputs)
    exported = data["visits"][0]["rounds"][0]
    assert len(exported["candidates"]) == 5
    assert exported["source_candidate_count"] == 10
    assert exported["candidate_selection"] == "first_5_of_legacy_10"
    assert exported["chorus_capture_note"] == "此处为记录时快照"
