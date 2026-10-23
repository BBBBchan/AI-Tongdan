import json
from pathlib import Path
import sqlite3
import wave
import zipfile

import pytest

from scripts import reprocess_clip_subtitles as bulk


@pytest.fixture
def journal(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    job = {"id": "1" * 16, "anchor_id": "2" * 16, "session_id": "3" * 16,
           "status": "complete", "created_at": 10, "duration": 5.0,
           "anchor": {"id": "2" * 16, "session_id": "3" * 16,
                      "start": 0., "end": 5., "title": "approved title"},
           "options": {"captions": False}, "publication_id": "publication-original",
           "publication_metadata": {"title": "approved publication"},
           "video_url": "/api/exports/" + "1" * 16 + "/clip.mp4", "superseded_by": "older-lineage-pointer"}
    source = {"id": "3" * 16, "mode": "live", "status": "live", "auto_publish": False}
    con = sqlite3.connect(data / "clipper.sqlite")
    con.executescript("""
        CREATE TABLE jobs(id TEXT PRIMARY KEY, created REAL, payload TEXT);
        CREATE TABLE sessions(id TEXT PRIMARY KEY, created REAL, payload TEXT);
        CREATE TABLE anchors(id TEXT PRIMARY KEY, created REAL, payload TEXT);
        CREATE TABLE publications(id TEXT PRIMARY KEY, created REAL, payload TEXT);
        CREATE TABLE segments(id TEXT,session_id TEXT,start REAL,end REAL,path TEXT,metadata TEXT);
        CREATE TABLE events(id TEXT,session_id TEXT,kind TEXT,start REAL,end REAL,text TEXT,data TEXT);
    """)
    con.execute("INSERT INTO jobs VALUES(?,?,?)", (job["id"], 10, json.dumps(job)))
    con.execute("INSERT INTO sessions VALUES(?,?,?)", (source["id"], 10, json.dumps(source)))
    con.execute("INSERT INTO anchors VALUES(?,?,?)", (job["anchor_id"], 10, json.dumps(job["anchor"])))
    con.execute("INSERT INTO publications VALUES(?,?,?)", ("publication-original", 10, '{"status":"private"}'))
    con.commit()
    con.close()
    folder = data / "exports" / job["id"]
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"original video")
    return data, job, source


def make_staging(data, job):
    run = data / "bulk"
    staged = run / "staging" / "exports" / job["id"]
    staged.mkdir(parents=True)
    (staged / "clip.mp4").write_bytes(b"new strong video")
    return run, staged


def read_job(data):
    return bulk.snapshot(data)["jobs"][0]


def write_review_wav(folder, duration):
    with wave.open(str(folder / "review.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(round(duration * 16000) * 2))


def test_readonly_adapter_never_interrupts_live_session_or_writes_original_job(journal):
    data, job, source = journal
    adapter = bulk.ReadOnlyRenderStore(data, job)
    try:
        adapter.update_job(job["id"], status="rendering", progress=10)
        adapter.update_anchor(job["anchor_id"], status="exported")
        assert adapter.job(job["id"])["options"]["captions"] is True
        assert adapter.session(source["id"])["status"] == "live"
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            adapter.db.execute("DELETE FROM jobs")
    finally:
        adapter.db.close()
    assert read_job(data) == job
    assert bulk.snapshot(data)["sessions"][0] == source


def test_successful_exchange_preserves_job_identity_publication_and_removes_old_files(journal):
    data, job, _ = journal
    run, staged = make_staging(data, job)
    entry = {"status": "rendering"}
    fields = {"subtitle_review": {"applied": True}, "options": {"captions": True}}
    bulk.commit_replacement(data, run, entry, job, fields)
    assert (data / "exports" / job["id"] / "clip.mp4").read_bytes() == b"new strong video"
    assert not staged.exists()
    updated = read_job(data)
    for key in ("id", "anchor_id", "session_id", "created_at", "publication_id", "video_url", "superseded_by", "publication_metadata"):
        assert updated[key] == job[key]
    assert updated["subtitle_review"]["applied"]
    assert bulk.snapshot(data)["publications"] == [{"status": "private"}]
    assert entry["status"] == "complete"
    # Crash after commit but before progress receipt: no double exchange.
    bulk.commit_replacement(data, run, entry, job)
    assert (data / "exports" / job["id"] / "clip.mp4").read_bytes() == b"new strong video"


def test_database_failure_rolls_directory_exchange_back(journal, monkeypatch):
    data, job, _ = journal
    run, staged = make_staging(data, job)
    def fail(*args):
        raise bulk.ReprocessError("test_write_failed")
    monkeypatch.setattr(bulk, "write_job_fields", fail)
    with pytest.raises(bulk.ReprocessError, match="test_write_failed"):
        bulk.commit_replacement(data, run, {}, job, {"subtitle_review": {"applied": True}})
    assert (data / "exports" / job["id"] / "clip.mp4").read_bytes() == b"original video"
    assert (staged / "clip.mp4").read_bytes() == b"new strong video"
    assert read_job(data) == job
    assert bulk.read_json(run / "receipts" / (job["id"] + ".json"))["status"] == "rolled_back"


def test_recovery_after_exchange_before_database_write(journal):
    data, job, _ = journal
    run, staged = make_staging(data, job)
    receipt = {"operation_id": "interrupted", "id": job["id"], "status": "prepared",
               "fields": {"subtitle_review": {"applied": True}}, "anchor": job["anchor"]}
    bulk.atomic_json(staged / bulk.MARKER, {"operation_id": "interrupted"})
    bulk.atomic_json(run / "receipts" / (job["id"] + ".json"), receipt)
    bulk.exchange_directories(data / "exports" / job["id"], staged)
    assert read_job(data) == job
    bulk.commit_replacement(data, run, {}, job)
    assert read_job(data)["subtitle_review"]["applied"]
    assert not staged.exists()


def test_prepared_receipt_recovers_before_exchange(journal):
    data, job, _ = journal
    run, staged = make_staging(data, job)
    bulk.atomic_json(staged / bulk.MARKER, {"operation_id": "prepared"})
    bulk.atomic_json(run / "receipts" / (job["id"] + ".json"), {
        "operation_id": "prepared", "status": "prepared", "fields": {"subtitle_review": {"applied": True}}, "anchor": job["anchor"]})
    bulk.commit_replacement(data, run, {}, job)
    assert (data / "exports" / job["id"] / "clip.mp4").read_bytes() == b"new strong video"
    assert read_job(data)["subtitle_review"]["applied"]


@pytest.mark.parametrize("interrupted_after", [1, 2])
def test_fallback_recovers_after_each_directory_rename(journal, interrupted_after):
    data, job, _ = journal
    run, staged = make_staging(data, job)
    original = data / "exports" / job["id"]
    bulk.atomic_json(staged / bulk.MARKER, {"operation_id": "fallback"})
    bulk.atomic_json(run / "receipts" / (job["id"] + ".json"), {
        "operation_id": "fallback", "status": "prepared", "fields": {"subtitle_review": {"applied": True}}, "anchor": job["anchor"]})
    original.rename(bulk.exchange_backup(original))
    if interrupted_after == 2:
        staged.rename(original)
    bulk.commit_replacement(data, run, {}, job)
    assert (original / "clip.mp4").read_bytes() == b"new strong video"
    assert read_job(data)["subtitle_review"]["applied"]
    assert not staged.exists()
    assert not bulk.exchange_backup(original).exists()


def test_rename_fallback_restores_original_if_new_directory_move_fails(journal, monkeypatch):
    data, job, _ = journal
    _, staged = make_staging(data, job)
    original = data / "exports" / job["id"]
    rename = bulk.os.rename
    def fail_new(source, target):
        if Path(source) == staged:
            raise OSError("simulated move error")
        return rename(source, target)
    monkeypatch.setattr(bulk.os, "rename", fail_new)
    with pytest.raises(OSError, match="simulated"):
        bulk.exchange_via_renames(original, staged)
    assert (original / "clip.mp4").read_bytes() == b"original video"
    assert (staged / "clip.mp4").read_bytes() == b"new strong video"
    assert not bulk.exchange_backup(original).exists()


def test_metadata_repair_preserves_cover_copy_and_package_consistency(journal):
    data, job, _ = journal
    run, staged = make_staging(data, job)
    original = data / "exports" / job["id"]
    old = {"anchor": job["anchor"], "publication_metadata": {"description": "approved original"},
           "evidence_snapshot": [{"text": "original evidence"}], "title_variants": ["original title"]}
    bulk.atomic_json(original / "metadata.json", old)
    (original / "cover.jpg").write_bytes(b"approved cover")
    (original / "titles.txt").write_text("original title")
    bulk.atomic_json(staged / "metadata.json", {"duration": 5, "publication_metadata": {"description": "changed"}})
    bulk.atomic_json(staged / "subtitle-review.json", {"applied": True, "words": [1], "reviewed_subtitles": [2]})
    bulk.atomic_json(staged / "chat-focus.json", {})
    (staged / "subtitles.srt").write_text("new caption")
    fields = bulk.prepare_metadata(original, staged, job, {})
    updated = bulk.read_json(staged / "metadata.json")
    assert updated["publication_metadata"] == old["publication_metadata"]
    assert updated["evidence_snapshot"] == old["evidence_snapshot"]
    assert updated["anchor"] == job["anchor"]
    assert updated["options"]["captions"] is True
    assert (staged / "cover.jpg").read_bytes() == b"approved cover"
    assert fields["subtitle_review"]["word_count"] == 1
    with zipfile.ZipFile(staged / "package.zip") as package:
        assert json.loads(package.read("metadata.json")) == updated
        assert package.read("cover.jpg") == b"approved cover"
        assert package.read("subtitles.srt") == b"new caption"


def test_review_validation_rejects_fallback_and_old_font(tmp_path):
    job = {"anchor": {"start": 0, "end": 5}, "options": {}}
    report = {"applied": True, "status": "complete", "model": bulk.MODEL, "aligner": bulk.ALIGNER,
              "timing_basis": "forced_alignment", "duration": 5, "text": "你好",
              "words": [{"start": 0.1, "end": 1.5, "text": "你好"}]}
    bulk.atomic_json(tmp_path / "subtitle-review.json", report)
    write_review_wav(tmp_path, 5)
    (tmp_path / "subtitles.ass").write_text("Style: Caption,Font,37,other")
    assert not bulk.valid_review(tmp_path, job)
    (tmp_path / "subtitles.ass").write_text("Style: Caption,Font,48,other")
    assert bulk.valid_review(tmp_path, job)
    report.update(status="fallback", applied=False)
    bulk.atomic_json(tmp_path / "subtitle-review.json", report)
    assert not bulk.valid_review(tmp_path, job)


def test_review_validation_uses_actual_pcm_clock_with_bounded_trim_rounding(tmp_path):
    job = {"anchor": {"start": 195.612, "end": 246.7}, "options": {}}
    report = {"applied": True, "status": "complete", "model": bulk.MODEL, "aligner": bulk.ALIGNER,
              "timing_basis": "forced_alignment", "duration": 51.0933125, "text": "打折了",
              "words": [{"start": 50.56, "end": 51.0933125, "text": "打折了"}]}
    bulk.atomic_json(tmp_path / "subtitle-review.json", report)
    write_review_wav(tmp_path, 51.0933125)
    (tmp_path / "subtitles.ass").write_text("Style: Caption,Font,48,other")
    assert bulk.valid_review(tmp_path, job)
    report["duration"] = 52.
    bulk.atomic_json(tmp_path / "subtitle-review.json", report)
    assert not bulk.valid_review(tmp_path, job)
    # A claimed report clock cannot override the independently read WAV clock.
    report["duration"] = 51.1
    bulk.atomic_json(tmp_path / "subtitle-review.json", report)
    assert not bulk.valid_review(tmp_path, job)


@pytest.mark.parametrize(("path", "excluded"), [
    ("/data/xiaodan-intro-v6/production-d/render/intro.mp4", True),
    ("/data/xiaodan-intro-v2/assets/fictional-live-scene.mp4", True),
    ("/data/demo/publish-demo.mp4", True),
    ("/data/reference-samples/nana-key-chat-study.mp4", False),
])
def test_generated_intro_exclusion_is_explicit_source_provenance(path, excluded):
    assert bulk.generated_demo({"mode": "file", "media_path": path}) is excluded
    assert not bulk.generated_demo({"mode": "live", "media_path": path})


def test_inventory_does_not_ignore_superseded_exports(journal):
    data, job, _ = journal
    (data / "source.mp4").write_bytes(b"source")
    with sqlite3.connect(data / "clipper.sqlite") as con:
        con.execute("INSERT INTO segments VALUES(?,?,?,?,?,?)", ("seg", job["session_id"], 0, 5, str(data / "source.mp4"), "{}"))
    result = bulk.inventory(data)
    assert len(result["entries"]) == 1
    assert result["entries"][0]["superseded"] is True
    assert result["entries"][0]["status"] == "pending"
    assert result["entries"][0]["signature"]


@pytest.mark.asyncio
async def test_caption_repair_reuses_original_magnifier_and_evidence(journal, monkeypatch):
    import clipper.chat_focus as focus
    data, job, _ = journal
    run, staged = make_staging(data, job)
    original = data / "exports" / job["id"]
    report = {"status": "matched", "source_width": 1280, "source_height": 720,
              "timing_basis": "decoded_source_frame_pts",
              "plans": [{"start": 1., "end": 2., "bbox": [10, 10, 200, 100],
                         "timing_basis": "decoded_source_frame_pts"}], "omissions": []}
    bulk.atomic_json(original / "metadata.json", {"anchor": job["anchor"]})
    bulk.atomic_json(original / "chat-focus.json", report)
    bulk.atomic_json(original / "chat-focus-ocr.json", {"evidence": True})
    async def should_not_run(*args):
        raise AssertionError("OCR must not repeat for the same source range and selected messages")
    monkeypatch.setattr(focus, "align_chat_focus", should_not_run)
    with bulk.preserve_visual_edits(original, staged, job, (1280, 720)) as matched:
        assert matched
        result = await focus.align_chat_focus(None, None, 0, 5, [], job["anchor"], staged)
        assert result == report
    assert focus.align_chat_focus is should_not_run
    assert bulk.read_json(staged / "chat-focus-ocr.json") == {"evidence": True}


@pytest.mark.asyncio
async def test_changed_source_range_does_not_reuse_old_visual_alignment(journal, monkeypatch):
    import clipper.chat_focus as focus
    data, job, _ = journal
    _, staged = make_staging(data, job)
    original = data / "exports" / job["id"]
    bulk.atomic_json(original / "metadata.json", {"anchor": {**job["anchor"], "end": 9.}})
    bulk.atomic_json(original / "chat-focus.json", {"plans": [], "omissions": []})
    calls = []
    async def realign(*args):
        calls.append(True)
        return {"status": "recomputed"}
    monkeypatch.setattr(focus, "align_chat_focus", realign)
    with pytest.raises(bulk.ReprocessError, match="original_visual_edit_source_changed"):
        with bulk.preserve_visual_edits(original, staged, job):
            pass
    assert calls == []
    assert focus.align_chat_focus is realign


@pytest.mark.parametrize("change", [
    {"start": float("nan")}, {"end": 9}, {"bbox": [10, 10, 9000, 100]}, {"magnification": 3},
])
def test_original_focus_reuse_rejects_invalid_pixel_or_time_evidence(journal, change):
    data, job, _ = journal
    folder = data / "exports" / job["id"]
    report = {"plans": [{"start": 1, "end": 2, "bbox": [10, 10, 200, 100],
                         "timing_basis": "decoded_source_frame_pts", **change}],
              "omissions": [], "source_width": 1280, "source_height": 720,
              "timing_basis": "decoded_source_frame_pts"}
    (folder / "chat-focus.json").write_text(json.dumps(report))
    with pytest.raises(bulk.ReprocessError, match="invalid_original_focus_report"):
        bulk.original_focus_report(folder, job)
