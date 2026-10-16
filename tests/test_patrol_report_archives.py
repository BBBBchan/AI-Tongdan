"""Synthetic, offline archive tests; never invoke a model or live-room sender."""
import json
import csv

from scripts.patrol_report import render_plan, render_report


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_events(path, values):
    path.write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values), encoding="utf-8")


def setup_run(tmp_path, *, simulation=False):
    config = {"candidate_only": True, "candidate_count": 5 if simulation else 10,
              "candidate_scope": "per_generation_batch", "virtual_fallback": True,
              "duration_hours": 24, "dwell_seconds": 900,
              "first_room": 213, "first_dwell_seconds": 1800}
    if simulation:
        config.update(simulation_rounds_per_visit=10, simulation_candidate_count=5,
                      simulation_scope="per_visit_evenly_spaced")
    write_json(tmp_path / "status.json", {"run_id": "offline", "config": config})
    directory = tmp_path / "visits" / "001-213"
    write_json(directory / "visit.json", {
        "room": {"room_id": 213}, "session_id": "offline-session", "status": "observing",
        "started_at": 1_800_000_000, "session_started_at": 1_800_000_000,
        "recordings": [{"file": "recordings/part-001.mkv", "started_at": 1_800_000_005,
                        "ended_at": 1_800_000_300}],
    })
    (directory / "recordings").mkdir()
    (directory / "recordings" / "part-001.mkv").touch()
    return directory


def event(kind, data, when=1_800_000_120, **extra):
    return {"time": when, "type": kind, "session_id": "offline-session", "data": data, **extra}


def test_questions_merge_lifecycle_preserve_full_answer_and_locate_content(tmp_path):
    directory = setup_run(tmp_path)
    answer = "完整测试回答\n" * 1500 + "<script>仅文字</script>末尾原文"
    records = [
        event("question_requested", {"question_id": "q1", "text": "离线问题原文", "kind": "review"}),
        event("question_started", {"question_id": "q1", "text": "离线问题原文", "status": "queued"}, 1_800_000_121),
        event("plan", {"question_id": "q1", "plan": "原始检索计划", "snapshot_time": 120,
                       "replay_times": [90], "evidence": [{"timestamp": 80}]}, 1_800_000_122),
        event("answer_done", {"question_id": "q1", "content": answer, "status": "complete",
                              "snapshot_time": 120}, 1_800_000_130),
    ]
    write_events(directory / "events.jsonl", records)
    write_events(directory / "questions.jsonl", [{**record, "time": record["time"] + .1} for record in records])
    result = render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    question = report["visits"][0]["questions"][0]
    assert result["totals"]["questions"] == result["totals"]["answers_complete"] == 1
    assert len(question["events"]) == 4
    assert question["question"] == "离线问题原文"
    assert question["answer"] == answer
    assert question["snapshot_time"] == 120
    page = (tmp_path / "index.html").read_text()
    assert "#t=115.00" in page  # Content snapshot at 120s, recording started at 5s.
    assert "#t=85.00" in page
    assert "#t=75.00" in page
    assert "按录制起点估算" in page
    assert "&lt;script&gt;仅文字&lt;/script&gt;末尾原文" in page
    assert "<script>仅文字</script>" not in page


def test_unanswered_failed_cancelled_and_other_session_are_not_successful_answers(tmp_path):
    directory = setup_run(tmp_path)
    records = [
        event("question_requested", {"question_id": "pending", "text": "同一问题"}),
        event("question_requested", {"question_id": "failed", "text": "同一问题"}),
        event("answer_done", {"question_id": "failed", "status": "error", "text": "", "error": "测试失败"}),
        event("question_started", {"question_id": "cancelled", "text": "取消的问题"}),
        event("answer_done", {"question_id": "cancelled", "status": "cancelled", "text": ""}),
        {**event("answer_done", {"question_id": "foreign", "status": "complete", "text": "别的会话"}),
         "session_id": "another-session"},
    ]
    write_events(directory / "questions.jsonl", records)
    result = render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert result["totals"]["questions"] == 3
    assert result["totals"]["answers_complete"] == 0
    assert {item["status"] for item in report["visits"][0]["questions"]} == {"requested", "error", "cancelled"}
    page = (tmp_path / "index.html").read_text()
    assert "测试失败" in page and "别的会话" not in page


def test_all_chorus_items_and_distinct_empty_windows_survive_duplicate_archives(tmp_path):
    directory = setup_run(tmp_path)
    items = [{"text": f"合成复读{number}", "count": number, "total": 40,
              "window_seconds": 30, "source_time": 110 + number, "user_id": "do-not-project"}
             for number in range(1, 8)]
    records = [event("danmaku_chorus", {"updated_at": 1_800_000_120, "items": items}),
               event("danmaku_chorus", {"updated_at": 1_800_000_150, "items": items}, 1_800_000_150),
               event("danmaku_chorus", {"updated_at": 1_800_000_180, "items": []}, 1_800_000_180)]
    write_events(directory / "events.jsonl", records)
    write_events(directory / "chorus.jsonl", records)
    result = render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert result["totals"]["chorus_windows"] == 3
    assert len(report["visits"][0]["chorus_windows"][0]["items"]) == 7
    assert "do-not-project" not in (tmp_path / "report.json").read_text()
    page = (tmp_path / "index.html").read_text()
    assert page.count("合成复读7") == 2
    assert "该窗口没有达到阈值的复读内容" in page


def test_simulation_rounds_are_primary_with_skips_and_legacy_batch_provenance(tmp_path):
    directory = setup_run(tmp_path, simulation=True)
    raw = {"status": "ready", "generated_at": 1_800_000_110,
           "items": [{"text": f"原始候选{number}"} for number in range(1, 11)]}
    chorus = {"updated_at": 1_800_000_120, "items": [{"text": "本轮复读原句", "count": 8,
              "total": 30, "window_seconds": 30, "source_time": 115}]}
    records = [
        event("simulation_round", {"status": "skipped", "reason": "指令开始前"}, round=1, target_at=1_800_000_060),
        event("simulation_round", {"status": "complete", "candidates": {**raw, "items": raw["items"][:5]},
              "raw_candidates": raw, "chorus": chorus, "source_candidate_count": 10,
              "candidate_selection": "first_5_of_legacy_10", "snapshot_time": 120}, round=2, target_at=1_800_000_120),
    ]
    write_events(directory / "simulation_rounds.jsonl", records)
    write_events(directory / "events.jsonl", records)
    write_events(directory / "candidates.jsonl", [event("danmaku_candidates", raw)])
    result = render_report(tmp_path)
    render_plan(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert result["totals"]["simulation_rounds"] == 2
    assert result["totals"]["simulation_complete"] == result["totals"]["simulation_skipped"] == 1
    assert len(report["visits"][0]["simulation_rounds"][1]["candidates"]) == 5
    assert len(report["visits"][0]["candidates"][0]["items"]) == 10
    for filename in ("index.html", "轮巡计划.html"):
        page = (tmp_path / filename).read_text()
        assert "每房均匀安排 10 次模拟记录，每次保存 5 条候选" in page
        assert "首站约 3 分钟一次" in page
        assert "普通房间约 1.5 分钟一次" in page
        assert "全程不发送弹幕" in page
        assert "每轮目标 10 条" not in page
    page = (tmp_path / "index.html").read_text()
    assert page.index("模拟轮次 ·") < page.index("候选弹幕批次 ·")
    assert "从旧会话原生 10 条候选中取前 5 条" in page
    assert "本轮未记录复读窗口" in page
    assert "本轮复读原句" in page and "10. 原始候选10" in page


def test_recording_links_respect_content_ranges_and_do_not_link_outside_run(tmp_path):
    directory = setup_run(tmp_path)
    visit_path = directory / "visit.json"
    visit = json.loads(visit_path.read_text())
    visit["recordings"][0].update(content_start=80, content_end=160)
    write_json(visit_path, visit)
    records = [event("question_requested", {"question_id": "q", "text": "内容定位", "snapshot_time": 100})]
    write_events(directory / "questions.jsonl", records)
    render_report(tmp_path)
    page = (tmp_path / "index.html").read_text()
    assert "#t=20.00" in page and "按内容时间" in page
    outside = tmp_path.parent / "outside.mp4"
    outside.touch()
    visit["recordings"][0]["file"] = str(outside)
    write_json(visit_path, visit)
    render_report(tmp_path)
    page = (tmp_path / "index.html").read_text()
    assert "#t=20.00" not in page
    assert "该时刻暂无可定位的录像分段" in page


def test_nested_simulation_envelope_merges_with_archive_and_keeps_capture_note(tmp_path):
    directory = setup_run(tmp_path, simulation=True)
    record = event("simulation_round", {"status": "skipped", "reason": "错过目标时刻",
        "chorus": {"updated_at": 1_800_000_120, "items": []},
        "chorus_capture_note": "跳过槽；此为记录时快照，不是过去目标时刻的弹幕",
        "source_time": 115}, round=1, target_at=1_800_000_060)
    write_events(directory / "simulation_rounds.jsonl", [record])
    write_events(directory / "events.jsonl", [event("simulation_round", record, 1_800_000_121)])
    render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    rounds = report["visits"][0]["simulation_rounds"]
    assert len(rounds) == 1
    assert rounds[0]["status"] == "skipped"
    assert rounds[0]["snapshot_time"] == 115
    page = (tmp_path / "index.html").read_text()
    assert "不是过去目标时刻的弹幕" in page


def test_room_plan_overrides_default_dwell_and_user_message_completion_is_not_answer(tmp_path):
    directory = setup_run(tmp_path, simulation=True)
    status_path = tmp_path / "status.json"
    status = json.loads(status_path.read_text())
    status["room_plan"] = [{"room_id": 510, "dwell_seconds": 1800},
                           {"room_id": 21452505, "dwell_seconds": 900}]
    write_json(status_path, status)
    visit_path = directory / "visit.json"
    visit = json.loads(visit_path.read_text())
    visit["scheduled_end_at"] = 1_800_001_800
    write_json(visit_path, visit)
    write_events(directory / "questions.jsonl", [event("question_started", {
        "question_id": "q", "role": "user", "status": "complete", "text": "仅提交的问题", "type": "review"})])
    result = render_report(tmp_path)
    render_plan(tmp_path)
    assert result["totals"]["answers_complete"] == 0
    report = json.loads((tmp_path / "report.json").read_text())
    question = report["visits"][0]["questions"][0]
    assert question["kind"] == "review" and question["status"] == "queued"
    for filename in ("index.html", "轮巡计划.html"):
        page = (tmp_path / filename).read_text()
        assert "510（30 分钟）" in page
        assert "21452505（15 分钟）" in page
        assert "指定房间" in page
    assert "本次计划停留 30.0 分钟" in (tmp_path / "index.html").read_text()


def test_manual_and_planned_same_text_use_separate_ids_even_when_request_arrives_late(tmp_path):
    directory = setup_run(tmp_path)
    records = [
        event("question_started", {"question_id": "planned", "text": "同一个问题", "status": "queued"}, 1_800_000_120),
        event("question_started", {"question_id": "manual", "text": "同一个问题", "status": "queued"}, 1_800_000_121),
        event("answer_done", {"question_id": "planned", "content": "规划回答原文", "status": "complete",
                              "evidence": [{"timestamp": 100}], "snapshot_time": 120}, 1_800_000_130),
        event("question_requested", {"question_id": "planned", "text": "同一个问题", "origin": "planned"}, 1_800_000_131),
        event("answer_done", {"question_id": "manual", "text": "手动回答原文", "status": "complete",
                              "evidence": [{"timestamp": 110}], "snapshot_time": 121}, 1_800_000_132),
    ]
    write_events(directory / "events.jsonl", records)
    write_events(directory / "questions.jsonl", records)
    result = render_report(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    visit = report["visits"][0]
    questions = {question["question_id"]: question for question in visit["questions"]}
    assert result["totals"]["questions"] == 2
    assert result["totals"]["planned_questions"] == result["totals"]["manual_questions"] == 1
    assert result["totals"]["historical_questions"] == 0
    assert questions["planned"]["origin"] == "planned"
    assert questions["planned"]["status"] == "complete"
    assert questions["manual"]["origin"] == "manual"
    assert questions["planned"]["answer"] == "规划回答原文"
    assert questions["manual"]["answer"] == "手动回答原文"
    assert questions["planned"]["evidence_times"] == [100]
    assert questions["manual"]["evidence_times"] == [110]
    with (tmp_path / "summary.csv").open(encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["planned_questions"] == row["manual_questions"] == "1"
    page = (tmp_path / "index.html").read_text()
    assert "规划提问 1 次 · 手动提问 1 次" in page
    assert "手动提问作为额外信息保存，不计入规划提问数" in page


def test_explicit_planned_origin_and_legacy_request_classify_without_guessing_history(tmp_path):
    directory = setup_run(tmp_path)
    records = [
        event("question_started", {"question_id": "explicit", "text": "明确来源", "origin": "planned"}),
        event("question_requested", {"question_id": "legacy", "text": "旧规划请求"}),
        event("answer_done", {"question_id": "historical", "text": "只有历史回答", "status": "complete"}),
    ]
    write_events(directory / "questions.jsonl", records)
    result = render_report(tmp_path)
    assert result["totals"]["planned_questions"] == 2
    assert result["totals"]["manual_questions"] == 0
    assert result["totals"]["historical_questions"] == 1
    report = json.loads((tmp_path / "report.json").read_text())
    origins = {question["question_id"]: question["origin"] for question in report["visits"][0]["questions"]}
    assert origins == {"explicit": "planned", "legacy": "planned", "historical": "manual_or_historical"}


def test_room_override_uses_fifteen_rounds_without_changing_default_or_history(tmp_path):
    directory = setup_run(tmp_path, simulation=True)
    status_path = tmp_path / "status.json"
    status = json.loads(status_path.read_text())
    status["room_plan"] = [{"room_id": 1713546334, "dwell_seconds": 1800,
                            "simulation_rounds_per_visit": 15}]
    write_json(status_path, status)
    # Earlier visit has no stored override and must retain the default ten.
    earlier = json.loads((directory / "visit.json").read_text())
    earlier["scheduled_end_at"] = 1_800_001_800
    write_json(directory / "visit.json", earlier)
    next_visit = {**earlier, "session_id": "next-session", "room": {"room_id": 1713546334},
                  "started_at": 1_800_001_800, "session_started_at": 1_800_001_800,
                  "scheduled_end_at": 1_800_003_600, "simulation_rounds_per_visit": 15, "recordings": []}
    write_json(tmp_path / "visits" / "002-1713546334" / "visit.json", next_visit)
    render_report(tmp_path)
    render_plan(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["config"]["simulation_rounds_per_visit"] == 10
    assert [visit["simulation_rounds_per_visit"] for visit in report["visits"]] == [10, 15]
    assert [visit["simulation_interval_seconds"] for visit in report["visits"]] == [180, 120]
    assert report["room_plan"][0]["simulation_rounds_per_visit"] == 15
    with (tmp_path / "summary.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert [float(row["simulation_rounds_per_visit"]) for row in rows] == [10, 15]
    for filename in ("index.html", "轮巡计划.html"):
        page = (tmp_path / filename).read_text()
        assert "默认每房均匀安排 10 次模拟记录" in page
        assert "1713546334（30 分钟，目标 15 轮，约 2 分钟一次）" in page
        assert "每次保存 5 条候选" in page
        assert "全程不发送弹幕" in page
    page = (tmp_path / "index.html").read_text()
    assert "本次模拟目标 10 轮 · 约 3 分钟一次" in page
    assert "本次模拟目标 15 轮 · 约 2 分钟一次" in page
