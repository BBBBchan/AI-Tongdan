from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import shutil

import pytest

from clipper import live_tail_recovery as tail


class FakeStore:
    def __init__(self, segments, **changes):
        self.value = {"id": "sid", "mode": "live", "status": "stopped", "model_profile": "extra-gpu",
                      "asr_drained_until": 15., **changes}
        self.rows = segments
        self.added = {}
        self.fail = False

    def session(self, sid):
        return deepcopy(self.value)

    def update_session(self, sid, **changes):
        self.value.update(deepcopy(changes))

    def segments(self, sid):
        return deepcopy(self.rows)

    def events(self, sid, start, end, limit):
        return list(self.added.values())

    def add_event(self, sid, kind, start, end, text, data, source_key):
        if self.fail:
            self.fail = False
            raise RuntimeError("simulated insertion crash")
        self.added.setdefault(source_key, dict(kind=kind, start=start, end=end, text=text, data=data))


def segments(tmp_path, ranges):
    rows = []
    for i, (start, end) in enumerate(ranges):
        path = tmp_path / f"{i}.ts"
        path.write_bytes(b"fixture")
        rows.append(dict(start=start, end=end, path=str(path)))
    return rows


@pytest.fixture
def configured(tmp_path, monkeypatch):
    calls = []
    settings = SimpleNamespace(data_dir=tmp_path, model_profile="extra-gpu",
        audio_config=lambda: {"base_url": "https://fixture.example", "api_key": "test-secret"})

    async def extract(settings, folder, chunk):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "audio.wav"
        path.write_bytes(b"fixture wav")
        return path, chunk["end"]-chunk["start"], False

    async def request(client, config, data, context, duration):
        calls.append(duration)
        return {"text": "最后一句。", "words": [{"text": "最后一句", "start": .1, "end": min(duration, 1.)}],
                "duration": duration, "model": tail.MODEL, "aligner": tail.ALIGNER, "timing_basis": "forced_alignment"}

    monkeypatch.setattr(tail, "_extract", extract)
    monkeypatch.setattr(tail, "request_review", request)
    return settings, calls


def test_plan_only_tail_real_clock_and_gaps(tmp_path):
    rows = segments(tmp_path, [(0, 10), (10, 20), (30, 60)])
    plan = tail.recovery_plan({"asr_drained_until": 15}, rows)
    assert [(c["start"], c["end"]) for c in plan["chunks"]] == [(15, 20), (30, 50), (50, 60)]
    assert plan["gaps"] == [{"start": 20., "end": 30.}]
    assert plan["scope"] == "after_live_asr_watermark"


def test_missing_file_and_overlapping_timeline_fail_closed(tmp_path):
    rows = segments(tmp_path, [(0, 20), (19, 30)])
    with pytest.raises(tail.ReviewError, match="overlapping"):
        tail.recovery_plan({"asr_drained_until": 15}, rows)
    Path(rows[0]["path"]).unlink()
    with pytest.raises(tail.ReviewError, match="missing"):
        tail.recovery_plan({}, rows)


def test_no_watermark_is_explicit_bounded_scope(tmp_path):
    rows = segments(tmp_path, [(0, 1000)])
    plan = tail.recovery_plan({}, rows)
    assert plan["start"] == 100 and plan["scope"].endswith("no_watermark")
    assert max(c["end"]-c["start"] for c in plan["chunks"]) == 20


@pytest.mark.asyncio
async def test_success_real_offsets_and_idempotence(tmp_path, configured):
    settings, calls = configured
    store = FakeStore(segments(tmp_path, [(0, 20), (20, 40)]))
    result = await tail.recover_live_tail(settings, store, "sid")
    assert result["status"] == "complete" and calls == [20, 5]
    assert [e["start"] for e in store.added.values()] == [15.1, 35.1]
    await tail.recover_live_tail(settings, store, "sid")
    assert len(calls) == 2 and len(store.added) == 2
    assert "test-secret" not in json.dumps(store.value)


@pytest.mark.asyncio
async def test_checkpoint_survives_event_insertion_crash(tmp_path, configured):
    settings, calls = configured
    store = FakeStore(segments(tmp_path, [(0, 20)]))
    store.fail = True
    with pytest.raises(tail.ReviewError, match="incomplete"):
        await tail.recover_live_tail(settings, store, "sid")
    assert store.value["tail_recovery"]["status"] == "failed"
    await tail.recover_live_tail(settings, store, "sid")
    assert calls == [5] and len(store.added) == 1


@pytest.mark.asyncio
async def test_no_media_completes_without_model_calls(tmp_path, configured):
    settings, calls = configured
    store = FakeStore([])
    result = await tail.recover_live_tail(settings, store, "sid")
    assert result["scope"] == "no_recorded_media" and result["status"] == "complete" and not calls


@pytest.mark.asyncio
async def test_active_or_wrong_profile_never_runs(tmp_path, configured):
    settings, calls = configured
    store = FakeStore([], status="live")
    with pytest.raises(tail.ReviewError, match="stopped"):
        await tail.recover_live_tail(settings, store, "sid")
    store.value.update(status="stopped", model_profile="default")
    with pytest.raises(tail.ReviewError, match="profile"):
        await tail.recover_live_tail(settings, store, "sid")
    assert not calls


@pytest.mark.asyncio
async def test_model_failure_is_incomplete_not_silent(tmp_path, configured, monkeypatch):
    settings, _ = configured
    store = FakeStore(segments(tmp_path, [(0, 20)]))
    async def fail(*args):
        raise tail.ReviewError("http_500")
    monkeypatch.setattr(tail, "request_review", fail)
    with pytest.raises(tail.ReviewError):
        await tail.recover_live_tail(settings, store, "sid")
    assert store.value["tail_recovery"]["status"] == "failed"
    assert store.value["tail_recovery"]["error"] == "http_500"


@pytest.mark.asyncio
async def test_real_ts_extraction_across_segment_boundary(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            pytest.skip("ffmpeg unavailable")
    rows = []
    for index in range(2):
        path = tmp_path / f"real-{index}.ts"
        await tail.command([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                            "-t", "1", "-c:a", "aac", "-f", "mpegts", path])
        rows.append({"start": float(index), "end": float(index+1), "path": str(path)})
    chunk = {"start": .5, "end": 1.5, "sources": rows}
    wav, duration, silent = await tail._extract(SimpleNamespace(ffmpeg=ffmpeg), tmp_path / "extraction", chunk)
    # AAC packet padding is preserved and is subject to the same <=250 ms
    # measured-duration tolerance as the strong transcription endpoint.
    assert wav.is_file() and abs(duration-1) <= .25 and silent is False
