"""Regressions reproduced during review; all state and media placeholders are temporary."""
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from clipper.offline import parse_events, prepare_file
from clipper.render import write_subtitles
from clipper.store import Store


@pytest.fixture
def offline_scene(tmp_path, monkeypatch):
    store = Store(tmp_path / "journal")
    session = store.create_session(duration=36, media_path=str(tmp_path / "source.mp4"))
    settings = SimpleNamespace(
        data_dir=tmp_path,
        ffmpeg="never-executed",
        audio_config=lambda: {"base_url": "http://test.invalid", "api_key": ""},
    )
    calls = []
    responses = []

    async def command(args, **kwargs):
        if str(args[-1]).endswith(".jpg"):
            Path(args[-1]).touch()
            return b""
        return b"\0\0" * 16000

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            calls.append(kwargs)
            status, body = responses.pop(0)
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr("clipper.offline.command", command)
    monkeypatch.setattr("clipper.offline.httpx.AsyncClient", Client)
    yield SimpleNamespace(store=store, sid=session["id"], settings=settings,
                          calls=calls, responses=responses)
    store.db.close()


async def test_partial_asr_failure_retries_only_missing_chunks(offline_scene):
    s = offline_scene
    success = (200, {"segments": [{"start": 0, "end": 12, "text": "这个分段已经转写完成"}]})
    failure = (503, {"error": "temporary unavailable"})
    s.responses.extend([success, failure, failure])
    await prepare_file(s.settings, s.store, s.sid)
    assert len(s.calls) == 3
    assert [(e["start"], e["end"]) for e in s.store.events(s.sid)
            if e["kind"] == "transcript"] == [(0, 12)]

    s.responses.extend([success, success])
    await prepare_file(s.settings, s.store, s.sid)
    assert len(s.calls) == 5, "Second analysis must retry both failed chunks."
    transcripts = [e for e in s.store.events(s.sid) if e["kind"] == "transcript"]
    assert [(e["start"], e["end"]) for e in transcripts] == [(0, 12), (12, 24), (24, 36)]

    await prepare_file(s.settings, s.store, s.sid)
    assert len(s.calls) == 5, "A completed pass must not redo already transcribed audio."
    assert len([e for e in s.store.events(s.sid) if e["kind"] == "transcript"]) == 3


async def test_successful_silent_chunks_are_checkpointed(offline_scene):
    s = offline_scene
    s.responses.extend([(200, {"segments": [], "text": ""})] * 3)
    await prepare_file(s.settings, s.store, s.sid)
    assert len(s.calls) == 3
    assert not any(e["kind"] == "transcript" for e in s.store.events(s.sid))
    await prepare_file(s.settings, s.store, s.sid)
    assert len(s.calls) == 3, "Silence is a successfully processed interval, not a missing transcript."


async def test_external_subtitles_are_reused_without_replacing_them_with_asr(offline_scene):
    s = offline_scene
    event_id = s.store.add_event(s.sid, "transcript", 0, 36, "用户校对过的字幕",
                                {"source": "imported_srt"})
    await prepare_file(s.settings, s.store, s.sid)
    assert s.calls == []
    transcripts = [e for e in s.store.events(s.sid) if e["kind"] == "transcript"]
    assert len(transcripts) == 1 and transcripts[0]["id"] == event_id
    assert transcripts[0]["text"] == "用户校对过的字幕"


def test_timestamp_only_danmaku_defaults_end_relative_to_its_timestamp():
    event = parse_events([{"kind": "danmaku", "timestamp": 10, "text": "哈哈哈"}])[0]
    assert event["start"] == 10
    assert event["end"] == pytest.approx(10.1)


def test_explicit_end_does_not_evaluate_an_invalid_unused_default():
    event = parse_events([{"kind": "transcript", "start": "1.5", "end": "3.5",
                           "text": "数字字符串来自导入文件"}])[0]
    assert event["start"] == 1.5
    assert event["end"] == 3.5


@pytest.mark.parametrize("timestamp", [-1, float("nan"), float("inf")])
def test_timestamp_only_still_rejects_invalid_clocks(timestamp):
    with pytest.raises(ValueError):
        parse_events([{"kind": "danmaku", "timestamp": timestamp, "text": "哈哈"}])


def test_manual_cut_does_not_reinsert_words_from_partial_transcript_blocks(tmp_path):
    events = [
        {"id": "head", "kind": "transcript", "start": 0, "end": 12,
         "text": "被裁去前半段的原始字幕，缺少字级时间不能重新塞进成片"},
        {"id": "middle", "kind": "transcript", "start": 12, "end": 15,
         "text": "这个完整语句应该保留"},
        {"id": "tail", "kind": "transcript", "start": 15, "end": 24,
         "text": "被裁去后半段的原始字幕，也不能压缩到剩余三秒里"},
    ]
    write_subtitles(tmp_path, events, {"start": 6, "end": 18, "origin": "manual"}, 1280, 720)
    srt = (tmp_path / "subtitles.srt").read_text()
    ass = (tmp_path / "subtitles.ass").read_text()
    assert "这个完整语句应该保留" in srt
    assert "00:00:06,000 --> 00:00:09,000" in srt
    for text in ["被裁去前半段", "被裁去后半段"]:
        assert text not in srt and text not in ass


def test_exact_transcript_boundary_preserves_all_its_text(tmp_path):
    event = {"id": "aligned", "kind": "transcript", "start": 6, "end": 12,
             "text": "完整边界的这句话"}
    write_subtitles(tmp_path, [event], {"start": 6, "end": 12, "origin": "manual"}, 1280, 720)
    srt = (tmp_path / "subtitles.srt").read_text()
    assert "完整边界的这句话" in srt
    assert "00:00:00,000 --> 00:00:06,000" in srt
