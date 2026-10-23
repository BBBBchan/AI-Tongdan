import json
import shutil
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageFont
import pytest

from clipper.config import Settings, ROOT
from clipper.render import (command, probe, Renderer, validate_range, validate_region,
                            write_subtitles, publication_evidence_snapshot)
from clipper.store import Store


def test_recording_gap_is_not_silently_removed():
    with pytest.raises(ValueError,match="缺口"):
        validate_range({"start":3,"end":18},[{"start":0,"end":10},{"start":14,"end":20}])
    validate_range({"start":3,"end":18},[{"start":0,"end":10},{"start":10,"end":20}])


def test_regions_and_subtitle_injection(tmp_path):
    with pytest.raises(ValueError):
        validate_region({"x":.9,"y":0,"w":.5,"h":.5,"start":0,"end":4})
    a={"start":10,"end":20,"style":"reaction","evidence_ids":["real"]}
    events=[{"id":"real","kind":"danmaku","start":12,"end":12.1,"text":r"{\pos(0,0)}真正的弹幕"},
            {"id":"other","kind":"danmaku","start":13,"end":13.1,"text":"不应被强调"},
            {"id":"voice","kind":"transcript","start":9,"end":13,"text":"跨边界的原话"}]
    write_subtitles(tmp_path,events,a,1280,720)
    text=(tmp_path/"subtitles.ass").read_text()
    assert "不应被强调" not in text
    assert r"{\pos(0,0)}" not in text
    assert "真正的弹幕" not in text
    assert "Chat,," not in text
    assert "跨边界的原话" not in (tmp_path/"subtitles.srt").read_text()


@pytest.mark.parametrize(("width", "height", "font_size"), [
    (1280, 720, 48), (1920, 1080, 72),
    (720, 1280, 44), (1080, 1920, 66),
])
def test_larger_captions_fit_safe_area_and_preserve_full_speech(
        tmp_path, width, height, font_size):
    font_path = ROOT / "static/fonts/NotoSansCJKsc-Bold.otf"
    if not font_path.is_file():
        pytest.skip("Run scripts/setup_assets.py first")
    speech = "这是一段较长的完整台词需要在放大字幕后仍然全部保留而且每一句都不能超出画面安全区域。" * 2
    write_subtitles(tmp_path, [{"kind": "transcript", "start": 10, "end": 22,
                               "text": speech}], {"start": 10, "end": 22}, width, height)
    ass = (tmp_path / "subtitles.ass").read_text()
    style = next(line for line in ass.splitlines() if line.startswith("Style: Caption,")).split(",")
    assert int(style[2]) == font_size
    assert float(style[16]) / font_size == pytest.approx(3 / (48 if width > height else 44))
    assert float(style[17]) / font_size == pytest.approx(1 / (48 if width > height else 44))
    assert int(style[19]) == int(style[20]) == round(min(width, height) / 18)
    assert int(style[21]) == round(min(width, height) * 38 / 720)
    font = ImageFont.truetype(str(font_path), font_size)
    rows = [line.split(",", 9) for line in ass.splitlines() if line.startswith("Dialogue:")]
    safe_width = width - int(style[19]) - int(style[20])
    assert all(font.getlength(row[9]) + 2 * float(style[16]) <= safe_width for row in rows)
    assert "".join(row[9] for row in rows) == speech
    assert rows[0][1] == "0:00:00.00" and rows[-1][2] == "0:00:12.00"
    assert all(first[2] == second[1] for first, second in zip(rows, rows[1:]))
    srt_rows = (tmp_path / "subtitles.srt").read_text().strip().split("\n\n")
    assert "".join(row.splitlines()[2] for row in srt_rows) == speech


def test_publication_evidence_uses_only_cited_whole_source_events():
    events = [
        {"id": "cited", "kind": "transcript", "start": 10., "end": 14.,
         "text": "今天聊聊方言", "data": {"private_embedding": [1, 2]}},
        {"id": "unrelated", "kind": "transcript", "start": 14., "end": 18.,
         "text": "未经引用的游戏名称"},
        {"id": "partial", "kind": "transcript", "start": 6., "end": 12.,
         "text": "切片已裁去的上下文"},
    ]
    anchor = {"start": 10., "end": 20., "evidence_ids": ["cited", "partial", "fabricated"],
              "title": "不能拿标题补证据"}
    snapshot = publication_evidence_snapshot(events, anchor)
    assert snapshot == [{"id": "cited", "kind": "transcript", "start": 10.,
                         "end": 14., "text": "今天聊聊方言"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(("aspect", "resolution", "cover_mode", "size"), [
    ("landscape", None, None, (1920, 1080)),
    ("vertical", None, None, (1080, 1920)),
    ("landscape", 1080, "frame", (1920, 1080)),
    ("vertical", 1080, "frame", (1080, 1920)),
    # Offline historical subtitle replacement can preserve an explicit size.
    ("landscape", 720, None, (1280, 720)),
])
async def test_real_video_exports_with_audio_subtitles_roi_and_package(
        tmp_path, aspect, resolution, cover_mode, size):
    font=ROOT/"static/fonts/NotoSansCJKsc-Bold.otf"
    if not font.is_file():
        pytest.skip("Run scripts/setup_assets.py first")
    settings=Settings(data_dir=tmp_path,ffmpeg=imageio_ffmpeg.get_ffmpeg_exe(),font=str(font))
    source=tmp_path/"fixture.mp4"
    source_size = (1920, 1080) if resolution == 1080 else (640, 360)
    await command([settings.ffmpeg,"-hide_banner","-loglevel","error","-y",
                   "-f","lavfi","-i",f"testsrc2=size={source_size[0]}x{source_size[1]}:rate=25:duration=5",
                   "-f","lavfi","-i","sine=frequency=440:sample_rate=44100:duration=5",
                   "-c:v","libx264","-threads","1","-pix_fmt","yuv420p","-c:a","aac",source])
    store=Store(tmp_path)
    s=store.create_session(title="Synthetic export verification",duration=5,mode="file")
    store.add_segment(s["id"],0,5,source)
    eid=store.add_event(s["id"],"danmaku",2,2.1,"聊聊方言，这是真实测试事件")
    store.add_event(s["id"],"transcript",1,4,"这是一句用于验证字幕的测试台词")
    a=store.add_anchor(s["id"],start=1,end=4,timestamp=2,title="测试片段",
                       cover_text="保留时间轴\n放大关键弹幕",evidence_ids=[eid])
    options = {"aspect": aspect,
               "focus_region": {"x": .2, "y": .2, "w": .4, "h": .4, "start": 2, "end": 3}}
    # The original cases omit the new options to cover existing queued jobs.
    if resolution is not None:
        options["resolution"] = resolution
    if cover_mode is not None:
        options["cover_mode"] = cover_mode
    try:
        j=store.create_job(a["id"],s["id"],anchor=a,options=options)
        await Renderer(settings,store).run(j["id"])
        job=store.job(j["id"])
        assert job["status"]=="complete",job.get("error")
        folder=tmp_path/"exports"/j["id"]
        info=await probe(settings,folder/"clip.mp4")
        assert abs(info["duration"]-3)<.15
        assert info["audio"]
        assert (info["width"], info["height"]) == size
        assert (folder/"package.zip").stat().st_size>10000
        metadata = json.loads((folder/"metadata.json").read_text())
        # Citing a chat event for publication metadata must not select a visual
        # effect or trigger OCR. The explicit manual region still works.
        assert metadata["emphasized_chat"] == 0
        assert metadata["chat_focus"]["status"] == "no_eligible_selected_chat"
        assert metadata["chat_focus"]["plans"] == []
        assert metadata["options"] == {"resolution": 1080, **options}
        assert job["options"] == metadata["options"]
        from clipper.publishing import metadata_for, publication_metadata_evidence
        assert metadata["evidence_snapshot"] == job["evidence_snapshot"]
        assert [event["id"] for event in job["evidence_snapshot"]] == [eid]
        assert metadata["publication_metadata"] == metadata_for(job, s)
        assert metadata["publication_metadata_evidence"] == publication_metadata_evidence(job, s)
        assert "方言" in metadata["publication_metadata"]["tags"]
        import zipfile
        with zipfile.ZipFile(folder/"package.zip") as package:
            assert json.loads(package.read("metadata.json")) == metadata
            assert json.loads(package.read("subtitle-review.json")) == metadata["subtitle_review"]
        with Image.open(folder/"cover.jpg") as cover:
            if cover_mode == "frame":
                assert cover.size == source_size, "Original-frame covers keep the source dimensions."
                assert (folder/"cover.jpg").read_bytes() == (folder/"frame.jpg").read_bytes(), \
                    "An already designed source frame must not gain a banner, title or shading."
            else:
                assert cover.size == ((1280, 720) if aspect == "landscape" else (1080, 1440))
                assert (folder/"cover.jpg").read_bytes() != (folder/"frame.jpg").read_bytes()
    finally:
        store.db.close()


@pytest.mark.asyncio
async def test_export_relistens_to_exact_audio_without_changing_publication_evidence(tmp_path, monkeypatch):
    import base64
    from io import BytesIO
    import wave
    import zipfile

    import httpx
    from clipper import subtitle_review

    font = ROOT / "static/fonts/NotoSansCJKsc-Bold.otf"
    if not font.is_file():
        pytest.skip("Run scripts/setup_assets.py first")
    settings = Settings(data_dir=tmp_path, ffmpeg=imageio_ffmpeg.get_ffmpeg_exe(), font=str(font))
    settings.audio_config = lambda: {"base_url": "https://audio.example", "api_key": "private-test-key"}
    source = tmp_path / "source.mp4"
    await command([settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "color=c=blue:size=320x180:rate=25:duration=5",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=5",
                   "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", source])
    calls = []
    def handler(request):
        calls.append(1)
        payload = json.loads(request.content)
        with wave.open(BytesIO(base64.b64decode(payload["audio_base64"])), "rb") as wav:
            assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
            assert wav.getnframes() / wav.getframerate() == pytest.approx(3., abs=.001)
        return httpx.Response(200, json={"text": "改正的字幕。", "words": [
            {"start": .2, "end": .8, "text": "改正"},
            {"start": 1.5, "end": 2., "text": "的字幕"}],
            "model": subtitle_review.MODEL, "aligner": subtitle_review.ALIGNER,
            "duration": 3., "timing_basis": "forced_alignment"})

    actual_review = subtitle_review.review_subtitles
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def with_mock_service(*args, **kwargs):
            return await actual_review(*args, **kwargs, client=client)
        monkeypatch.setattr(subtitle_review, "review_subtitles", with_mock_service)
        store = Store(tmp_path)
        try:
            session = store.create_session(title="Actual audio export", duration=5, mode="file")
            store.add_segment(session["id"], 0, 5, source)
            evidence_id = store.add_event(session["id"], "transcript", 1, 4, "原来识别错的字幕")
            anchor = store.add_anchor(session["id"], start=1, end=4, timestamp=2, title="证据保持原样",
                                      evidence_ids=[evidence_id])
            job = store.create_job(anchor["id"], session["id"], anchor=anchor,
                                   options={"cover": False, "emphasis": False})
            await Renderer(settings, store).run(job["id"])
            result = store.job(job["id"])
            assert result["status"] == "complete", result.get("error")
            folder = tmp_path / "exports" / job["id"]
            srt = (folder / "subtitles.srt").read_text()
            assert "00:00:00,200 --> 00:00:00,800\n改正" in srt
            assert "00:00:01,500 --> 00:00:02,000\n的字幕。" in srt
            assert "原来识别错" not in srt and len(calls) == 1
            metadata = json.loads((folder / "metadata.json").read_text())
            assert metadata["subtitle_review"]["status"] == "complete"
            assert metadata["subtitle_review"]["timing_basis"] == "forced_alignment"
            assert result["subtitle_review"]["status"] == "complete"
            assert result["subtitle_review"]["word_count"] == 2
            assert "words" not in result["subtitle_review"]
            assert "original_subtitles" not in result["subtitle_review"]
            assert metadata["evidence_snapshot"][0]["text"] == "原来识别错的字幕"
            assert store.events(session["id"])[0]["text"] == "原来识别错的字幕"
            assert "private-test-key" not in (folder / "metadata.json").read_text()
            with zipfile.ZipFile(folder / "package.zip") as package:
                assert json.loads(package.read("subtitle-review.json")) == metadata["subtitle_review"]
                assert "review.wav" not in package.namelist()
                assert "subtitle-review-cache.json" not in package.namelist()
        finally:
            store.db.close()


@pytest.mark.parametrize("status", ["live", "waiting", "recording"])
def test_crashed_jobs_and_sessions_are_marked_interrupted(tmp_path, status):
    store=Store(tmp_path)
    s=store.create_session(status=status)
    j=store.create_job("anchor",s["id"])
    store.db.close()
    reopened=Store(tmp_path)
    assert reopened.session(s["id"])["status"]=="interrupted"
    assert reopened.job(j["id"])["status"]=="failed"


def test_crashed_file_analysis_can_be_restarted_from_web(tmp_path):
    store = Store(tmp_path)
    session = store.create_session(mode="file", status="ready", analysis_status="analyzing",
                                   asr_completed_chunks=[0, 12])
    store.db.close()
    reopened = Store(tmp_path)
    restored = reopened.session(session["id"])
    assert restored["status"] == "ready"
    assert restored["analysis_status"] == "分析已中断"
    assert restored["asr_completed_chunks"] == [0, 12]
