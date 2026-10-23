from copy import deepcopy
import pytest
from clipper.publication_overlap import capture_span, overlaps, validate_no_overlap


class Store:
    def __init__(self):
        self.sources = {"s": {"mode": "live", "created_at": 1000, "room": {"owner_uid": 42}},
                        "later": {"mode": "live", "created_at": 1050, "streamer_uid": 42},
                        "different": {"mode": "live", "created_at": 1000, "streamer_uid": 43}}
        self.jobs = {"old": {"id": "old", "session_id": "s", "anchor": {"start": 0, "end": 100}}}
        self.rows = [{"id": "p", "job_id": "old", "status": "published", "video_sha256": "same"}]
    def session(self, sid): return self.sources.get(sid)
    def job(self, jid): return self.jobs.get(jid)
    def publications(self): return deepcopy(self.rows)


@pytest.mark.parametrize("status", ["queued", "uploading", "submitted", "published", "uncertain", "private"])
def test_any_positive_overlap_is_reserved(status):
    store = Store(); store.rows[0]["status"] = status
    job = {"session_id": "s", "anchor": {"start": 99.999, "end": 160}}
    with pytest.raises(ValueError, match="重叠"):
        validate_no_overlap(store, job)


def test_touching_endpoints_and_other_streamers_are_allowed():
    store = Store()
    assert validate_no_overlap(store, {"session_id": "s", "anchor": {"start": 100, "end": 160}})
    assert validate_no_overlap(store, {"session_id": "different", "anchor": {"start": 0, "end": 60}})


def test_cross_session_uses_absolute_capture_clock():
    store = Store()
    with pytest.raises(ValueError, match="重叠"):
        validate_no_overlap(store, {"session_id": "later", "anchor": {"start": 0, "end": 60}})
    with pytest.raises(ValueError, match="重叠"):
        validate_no_overlap(store, {"session_id": "later", "anchor": {"start": 50, "end": 110}})
    assert validate_no_overlap(store, {"session_id": "later", "anchor": {"start": 170, "end": 230}})


def test_real_capture_clock_overrides_session_creation():
    store = Store()
    store.sources["later"]["capture_clock_started_at"] = 1000
    span = capture_span(store, {"session_id": "later", "anchor": {"start": 0, "end": 60}})
    assert span["absolute_start"] == 1000
    assert span["clock_basis"] == "capture_receipt"


def test_missing_current_or_historical_source_fails_closed():
    store = Store()
    with pytest.raises(ValueError, match="来源记录"):
        capture_span(store, {"session_id": "missing", "anchor": {"start": 0, "end": 60}})
    store.jobs.clear()
    with pytest.raises(ValueError, match="来源记录"):
        validate_no_overlap(store, {"session_id": "s", "anchor": {"start": 100, "end": 160}})


def test_failed_upload_does_not_reserve_footage_and_own_record_is_ignored():
    store = Store(); job = store.jobs["old"]
    assert validate_no_overlap(store, job, publication_id="p")
    store.rows[0]["status"] = "failed"
    assert validate_no_overlap(store, job)


def test_identical_media_is_blocked_even_with_different_source_identity():
    store = Store()
    with pytest.raises(ValueError, match="相同成片"):
        validate_no_overlap(store, {"session_id": "different", "anchor": {"start": 0, "end": 60}}, video_sha256="same")


@pytest.mark.parametrize("broken", [{"start": 0, "end": float("nan")}, {"start": 100, "end": 90}, {}])
def test_unknown_live_time_fails_closed(broken):
    with pytest.raises(ValueError, match="源时间"):
        capture_span(Store(), {"session_id": "s", "anchor": broken})
