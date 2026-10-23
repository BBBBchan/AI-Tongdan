from copy import deepcopy
from types import SimpleNamespace

import pytest

from clipper.detector import Detector, _same_story, _windows
from clipper.store import Store


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    sid = store.create_session(duration=700, minimum_score=72)["id"]
    store.add_segment(sid, 0, 700, "recording.mp4")
    setup = store.add_event(sid, "transcript", 20, 30, "当时我们在排队，他说这样选位置就能排进去。")
    quote = store.add_event(sid, "transcript", 100, 110, "我有我的歪理，你别问，你执行。")
    end = store.add_event(sid, "transcript", 350, 360, "结果还真的排进去了，那我们开始吧。")
    settings = SimpleNamespace(data_dir=tmp_path, minimum_score=72,
        model_config=lambda: {"base_url": "http://model.invalid", "model": "test"})
    proposal = {"start": 15, "end": 365, "peak": 105, "score": 85,
        "story_complete": True, "boundary_reason": "选位置争论到排队成功",
        "quote": "我有我的歪理，你别问，你执行。",
        "evidence_ids": [setup, quote, end], "title_variants": ["歪理居然真的管用"],
        "cover_text": "居然真的排进去了", "focus_event_ids": []}
    return SimpleNamespace(store=store, sid=sid, detector=Detector(settings, store),
                           proposal=proposal, setup=setup, quote=quote, ending=end)


def respond(monkeypatch, story, proposal):
    async def ask(*args):
        return {"anchors": [deepcopy(proposal)]}
    monkeypatch.setattr(story.detector, "_ask_model", ask)


async def test_complete_multi_minute_story_preserves_setup_and_ending(story, monkeypatch):
    respond(monkeypatch, story, story.proposal)
    a, = await story.detector.analyze(story.sid)
    assert (a["start"], a["end"], a["status"]) == (15, 365, "ready")
    assert story.setup in a["evidence_ids"] and story.ending in a["evidence_ids"]


async def test_short_candidate_expands_then_matures_without_duplicate(story, monkeypatch):
    story.store.update_session(story.sid, status="live")
    short = {**story.proposal, "start": 99, "end": 115, "evidence_ids": [story.quote]}
    respond(monkeypatch, story, short)
    a, = await story.detector.analyze(story.sid, end=200)
    assert a["status"] == "candidate" and a["end"] - a["start"] == 16
    respond(monkeypatch, story, story.proposal)
    b, = await story.detector.analyze(story.sid, end=400)
    assert b["id"] == a["id"] and b["status"] == "candidate"
    assert (b["start"], b["end"]) == (15, 365)
    c, = await story.detector.analyze(story.sid, end=425)
    assert c["id"] == a["id"] and c["status"] == "ready"
    assert await story.detector.analyze(story.sid, end=500) == []
    assert len(story.store.anchors(story.sid)) == 1


@pytest.mark.parametrize("closed", [None, False, "true", 1])
async def test_duration_alone_does_not_establish_story_closure(story, monkeypatch, closed):
    respond(monkeypatch, story, {**story.proposal, "story_complete": closed})
    a, = await story.detector.analyze(story.sid)
    assert a["status"] == "candidate" and "尚未确认收尾" in a["review_reason"]


async def test_final_stopped_session_releases_complete_tail(story, monkeypatch):
    respond(monkeypatch, story, story.proposal)
    story.store.update_session(story.sid, status="live")
    a, = await story.detector.analyze(story.sid, end=380)
    assert a["status"] == "candidate"
    story.store.update_session(story.sid, status="stopped")
    b, = await story.detector.analyze(story.sid, end=380)
    assert b["id"] == a["id"] and b["status"] == "ready"


@pytest.mark.parametrize("lock", ["job", "manual", "edit"])
async def test_existing_export_and_user_edit_are_never_overwritten(story, monkeypatch, lock):
    short = {**story.proposal, "start": 99, "end": 115, "evidence_ids": [story.quote]}
    respond(monkeypatch, story, short)
    a, = await story.detector.analyze(story.sid)
    if lock == "job":
        story.store.create_job(a["id"], story.sid, anchor=deepcopy(a))
    elif lock == "manual":
        story.store.update_anchor(a["id"], origin="manual")
    else:
        story.store.update_anchor(a["id"], editorial_locked=True)
    before = story.store.anchor(a["id"])
    respond(monkeypatch, story, story.proposal)
    assert await story.detector.analyze(story.sid) == []
    assert story.store.anchor(a["id"]) == before


async def test_expansion_still_rejects_a_recording_gap(story, monkeypatch):
    story.store.db.execute("DELETE FROM segments")
    story.store.db.commit()
    story.store.add_segment(story.sid, 0, 200, "one.mp4")
    story.store.add_segment(story.sid, 220, 700, "two.mp4")
    respond(monkeypatch, story, story.proposal)
    assert await story.detector.analyze(story.sid) == []


def test_long_context_keeps_every_speech_chunk():
    events = [{"id": str(i), "kind": "transcript", "start": i * 3, "end": i * 3 + 2,
               "text": "故事的连续发展。"} for i in range(280)]
    assert Detector._select_events(events) == events


def test_offline_windows_can_contain_a_ten_minute_story_at_any_offset():
    windows = list(_windows(0, 3600))
    for start in range(0, 3001, 10):
        assert any(left <= start and start + 600 <= right for left, right in windows)


def test_shared_context_does_not_swallow_a_different_later_story():
    a = {"start": 0, "end": 400, "peak": 60, "quote": "第一件事", "evidence_ids": ["setup", "a"]}
    b = {"start": 200, "end": 550, "peak": 400, "quote": "第二件事", "evidence_ids": ["setup", "b"]}
    assert not _same_story(a, b)


def test_truncated_subwindow_does_not_downgrade_complete_covering_story(story):
    complete = {**story.proposal, "method": "model"}
    partial = {**complete, "end": 300, "story_complete": False, "score": 90}
    a, = Detector._merge([complete, partial])
    assert (a["start"], a["end"], a["story_complete"]) == (15, 365, True)


def test_union_beyond_verified_complete_boundary_requires_review(story):
    complete = {**story.proposal, "method": "model"}
    extension = {**complete, "end": 400, "story_complete": False}
    a, = Detector._merge([complete, extension])
    assert a["end"] == 400 and not a["story_complete"]


async def test_old_high_score_exports_do_not_starve_new_live_story(story, monkeypatch):
    proposals = []
    for i in range(4):
        start = i * 150
        text = f"完整的第{i}个故事已经自然结束。"
        eid = story.store.add_event(story.sid, "transcript", start + 10, start + 20, text)
        p = {**story.proposal, "start": start, "end": start + 80, "peak": start + 15,
             "quote": text, "evidence_ids": [eid], "score": 95 - i}
        proposals.append(p)
        if i < 3:
            a = story.store.add_anchor(story.sid, **p, method="model", status="exported")
            story.store.create_job(a["id"], story.sid, status="complete", anchor=a)
    story.store.update_session(story.sid, status="live")
    async def ask(*args):
        return {"anchors": deepcopy(proposals)}
    monkeypatch.setattr(story.detector, "_ask_model", ask)
    a, = await story.detector.analyze(story.sid)
    assert a["quote"] == proposals[-1]["quote"] and a["status"] == "ready"
