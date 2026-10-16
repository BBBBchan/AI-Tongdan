"""Offline report regression checks; no application or network dependencies."""
import csv
import json
from pathlib import Path
import pytest

from scripts.patrol_report import render_plan, render_report


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_events(path, values):
    path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
                    encoding="utf-8")


def make_run(tmp_path, *, candidate_only=True):
    config = {
        "candidate_only": candidate_only, "candidate_count": 10,
        "candidate_scope": "per_generation_batch", "virtual_fallback": True,
        "room_selection": "live_following_first", "duration_hours": 24,
        "dwell_seconds": 900, "first_room": 213, "first_dwell_seconds": 1800,
        # These legacy fields must not appear as active candidate-only rules.
        "interval_seconds": 180, "max_messages": 5, "first_max_messages": 10,
        "jitter_ratio": .3, "quota_scope": "per_room_across_entire_run",
    }
    write_json(tmp_path / "status.json", {"run_id": "offline", "status": "running",
               "started_at": 1_800_000_000, "deadline": 1_800_086_400, "config": config})
    directory = tmp_path / "visits" / "001-213"
    write_json(directory / "visit.json", {
        "session_id": "offline-session", "room": {"room_id": 213, "title": "离线房间"},
        "started_at": 1_800_000_000, "status": "observing",
    })
    return directory


def test_candidate_plan_and_report_display_generation_rules(tmp_path):
    make_run(tmp_path)
    render_report(tmp_path)
    plan_path = render_plan(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    pages = [(tmp_path / "index.html").read_text(), Path(plan_path).read_text(encoding="utf-8")]
    for page in pages:
        for text in ("每轮目标 10 条", "全程不发送弹幕", "24 小时", "15 分钟", "30 分钟", "213",
                     "只要有关注的直播间开播，就只去关注", "仅确认关注列表无人开播时", "虚拟主播分区"):
            assert text in page
        for text in ("发送配额", "累计最多", "发送间隔", "基准间隔", "±30%"):
            assert text not in page
    assert report["room_quotas"] == []
    assert report["config"]["candidate_count"] == 10
    assert report["config"]["candidate_scope"] == "per_generation_batch"
    assert report["config"]["virtual_fallback"] is True
    assert "成功发送" not in pages[0]


def test_all_ten_candidates_and_repeated_generation_survive_archive_merge(tmp_path):
    directory = make_run(tmp_path)
    texts = [f"候选第{index}条" for index in range(1, 10)] + ["第十条<script>仅文字</script>"]

    def batch(generated_at, number):
        return {"time": generated_at + 1, "type": "danmaku_candidates", "batch": number,
                "data": {"status": "ready", "generated_at": generated_at,
                         "items": [{"text": text} for text in texts]}}

    first = batch(1_800_000_010, 1)
    second = batch(1_800_000_020, 2)
    third = batch(1_800_000_030, 3)
    write_events(directory / "events.jsonl", [first, first, second])
    # Third batch exists only in the dedicated archive (e.g. a partial event log).
    write_events(directory / "candidates.jsonl", [{key: value for key, value in event.items() if key != "type"}
                                                 for event in [first, second, third]])
    result = render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    visit = report["visits"][0]
    assert result["totals"]["candidate_batches"] == 3
    assert result["totals"]["candidate_total"] == 30
    assert visit["last_candidate_count"] == 10
    assert [candidate["items"] for candidate in visit["candidates"]] == [texts] * 3
    page = (tmp_path / "index.html").read_text()
    assert page.count("10. 第十条&lt;script&gt;仅文字&lt;/script&gt;") == 3
    assert "<script>仅文字</script>" not in page
    assert "下载本房间全部候选 candidates.jsonl" in page
    with (tmp_path / "summary.csv").open(encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["candidate_batches"] == "3"
    assert row["candidate_total"] == "30"


def test_visit_candidate_counters_survive_missing_event_details(tmp_path):
    directory = make_run(tmp_path)
    visit_path = directory / "visit.json"
    visit = json.loads(visit_path.read_text())
    visit.update(candidate_batches=4, candidate_total=37, last_candidate_count=7)
    write_json(visit_path, visit)
    write_events(directory / "events.jsonl", [{"time": 1_800_000_010, "type": "danmaku_candidates",
                 "data": {"status": "ready", "items": [{"text": "较早的一轮"}]}}])
    result = render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert result["totals"]["candidate_batches"] == 4
    assert result["totals"]["candidate_total"] == 37
    assert report["visits"][0]["last_candidate_count"] == 7
    assert "部分候选详情尚未写入或未被日志捕获" in (tmp_path / "index.html").read_text()


def test_legacy_send_report_and_plan_keep_quota_information(tmp_path):
    directory = make_run(tmp_path, candidate_only=False)
    write_events(directory / "events.jsonl", [
        {"time": 1_800_000_010, "type": "sent_danmaku", "data": {
            "items": [{"text": "历史发送原句", "sent_at": 1_800_000_009, "source": "auto"}]}}
    ])
    result = render_report(tmp_path)
    render_plan(tmp_path)
    page = (tmp_path / "index.html").read_text()
    plan = (tmp_path / "轮巡计划.html").read_text()
    assert result["totals"]["sent"] == 1
    assert "历史发送原句" in page
    assert "基准间隔 3 分钟 ±30%" in page
    assert "本轮累计最多 10 条" in page
    assert "发送间隔" in plan
    assert "回访同一房间不会重置发送配额" in plan
    assert "全程不发送弹幕" not in page


def test_candidate_mode_does_not_hide_unexpected_send_records(tmp_path):
    directory = make_run(tmp_path)
    write_events(directory / "events.jsonl", [
        {"time": 1_800_000_010, "type": "sent_danmaku", "data": {
            "items": [{"text": "需要复核的发送", "sent_at": 1_800_000_009}]}}
    ])
    render_report(tmp_path)
    page = (tmp_path / "index.html").read_text()
    assert "与“只生成候选”的计划不符" in page
    assert "需要复核的发送" in page


@pytest.mark.parametrize("visit_allowed,source,since,mismatch", [
    (True, "manual", 1_800_000_005, False),
    (False, "manual", None, True),
    (None, "manual", None, True),
    (True, "auto", None, True),
    (True, "", None, True),
    (True, "manual", 1_800_000_010, True),
])
def test_manual_send_is_authorized_per_visit_and_time_not_current_config(
        tmp_path, visit_allowed, source, since, mismatch):
    directory = make_run(tmp_path)
    status = json.loads((tmp_path / "status.json").read_text())
    status["config"].update(allow_manual_send=True, send_authorized=False)
    write_json(tmp_path / "status.json", status)
    path = directory / "visit.json"
    visit = json.loads(path.read_text())
    if visit_allowed is not None:
        visit["allow_manual_send"] = visit_allowed
    if since is not None:
        visit["allow_manual_send_since"] = since
    write_json(path, visit)
    write_events(directory / "events.jsonl", [
        {"time": 1_800_000_010, "type": "sent_danmaku", "data": {
            "items": [{"text": "用户点了发送", "sent_at": 1_800_000_009, "source": source}]}},
        {"time": 1_800_000_020, "type": "simulation_round", "round": 1,
         "data": {"status": "complete", "sent": False, "candidates": {
             "status": "ready", "items": [{"text": "模拟候选"}]}}},
    ])
    original_events = (directory / "events.jsonl").read_bytes()
    render_report(tmp_path)
    render_plan(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["config"]["allow_manual_send"] is True
    assert report["config"]["send_authorized"] is False
    assert report["totals"]["sent"] == 1
    assert report["totals"]["manual_sent"] == int(source == "manual")
    assert report["visits"][0]["sending_policy_mismatch"] is mismatch
    assert report["totals"]["sending_policy_mismatches"] == int(mismatch)
    assert report["visits"][0]["sends"][0]["source"] == source
    assert (directory / "events.jsonl").read_bytes() == original_events
    assert report["totals"]["simulation_complete"] == 1
    for filename in ("index.html", "轮巡计划.html"):
        page = (tmp_path / filename).read_text()
        assert "候选模拟不自动发送；允许用户手动发送，实际发送单独记录" in page
        assert "全程不发送弹幕" not in page
    page = (tmp_path / "index.html").read_text()
    assert ("与“只生成候选”的计划不符" in page) is mismatch
    assert "用户点了发送" in page


def test_current_manual_policy_does_not_change_old_strict_visits(tmp_path):
    first = make_run(tmp_path)
    status = json.loads((tmp_path / "status.json").read_text())
    status["config"].update(allow_manual_send=True, send_authorized=False)
    write_json(tmp_path / "status.json", status)
    second = tmp_path / "visits/002-214"
    write_json(second / "visit.json", {"started_at": 1_800_000_100,
        "room": {"room_id": 214}, "allow_manual_send": True,
        "allow_manual_send_since": 1_800_000_100})
    for directory, when in ((first, 1_800_000_010), (second, 1_800_000_110)):
        write_events(directory / "events.jsonl", [{"time": when, "type": "sent_danmaku", "data": {
            "items": [{"text": "手动发送", "sent_at": when, "source": "manual"}]}}])
    render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert [visit["allow_manual_send"] for visit in report["visits"]] == [False, True]
    assert [visit["sending_policy_mismatch"] for visit in report["visits"]] == [True, False]
    assert report["totals"]["manual_sent"] == 2
    assert report["totals"]["sending_policy_mismatches"] == 1
