import json
import sys
from types import SimpleNamespace

import imageio_ffmpeg
import pytest

from clipper.chat_focus import (match_text, plans_from_samples, focus_filter_chain,
                                selected_chats, align_chat_focus)
from clipper.config import ROOT, Settings
from clipper.render import command, Renderer
from clipper.store import Store


def row(text="早就说过你吃商很低了", x=110, y=400, width=290, height=28, confidence=.98):
    return [[[x, y], [x+width, y], [x+width, y+height], [x, y+height]], text, confidence]


def test_complete_exact_visual_text_and_side_panel_are_required():
    target = "早就说过你吃商很低了"
    assert match_text([row()], target, 1280, 720)
    assert not match_text([row("早就说过你吃商很低")], target, 1280, 720)
    assert not match_text([row(confidence=.5)], target, 1280, 720)
    assert not match_text([row(x=500)], target, 1280, 720)
    assert not match_text([row(), row(y=500)], target, 1280, 720)


def test_wrapped_message_requires_complete_adjacent_lines():
    rows = [row("但是椰奶海鲜汤真的还可", width=340),
            row("以", y=441, width=30)]
    assert match_text(rows, "但是椰奶海鲜汤真的还可以", 1280, 720)
    assert not match_text(rows[:1], "但是椰奶海鲜汤真的还可以", 1280, 720)
    rows[1] = row("以", y=530, width=30)
    assert not match_text(rows, "但是椰奶海鲜汤真的还可以", 1280, 720)


def test_websocket_time_cannot_make_effect_early_and_motion_ends_effect():
    event = {"id": "chat", "text": "早就说过你吃商很低了", "start": 2, "end": 2}
    samples = [{"time": 15., "rows": []},
               {"time": 15.5, "rows": [row()]},
               {"time": 16., "rows": [row()]},
               {"time": 16.5, "rows": [row(y=300)]},
               {"time": 17., "rows": []}]
    plans, omitted = plans_from_samples(samples, [event], 1280, 720, 20)
    assert omitted == []
    assert plans[0]["start"] == 15.5
    assert plans[0]["end"] == 16.
    assert plans[0]["confirmed_frame_times"] == [15.5, 16.]
    event["start"] = 9999
    assert plans == plans_from_samples(samples, [event], 1280, 720, 20)[0]


def test_isolated_matches_missing_frames_and_short_text_fail_closed():
    event = {"id": "chat", "text": "早就说过你吃商很低了"}
    samples = [{"time": 5, "rows": [row()]}, {"time": 6.5, "rows": [row()]}]
    assert plans_from_samples(samples, [event], 1280, 720, 20)[0] == []
    assert selected_chats([{"id": "a", "kind": "danmaku", "text": "哈哈哈"}],
                          {"focus_event_ids": ["a"]}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [{}, {"focus_event_ids": []}, {"focus_event_ids": None}])
async def test_cited_chat_without_explicit_selection_never_decodes_or_runs_ocr(
        tmp_path, monkeypatch, selection):
    import clipper.chat_focus as focus_module
    calls = []

    async def unexpected_decode(*args):
        calls.append("decode")
        raise AssertionError("Cited chat must not request a visual effect")

    def unexpected_recognize(*args):
        calls.append("ocr")
        raise AssertionError("Cited chat must not request OCR")

    monkeypatch.setattr(focus_module, "_extract_frames", unexpected_decode)
    monkeypatch.setattr(focus_module, "_recognize", unexpected_recognize)
    event = {"id": "chat", "kind": "danmaku", "text": "早就说过你吃商很低了"}
    anchor = {"evidence_ids": ["chat"], **selection}
    report = await align_chat_focus("unused", tmp_path / "unused.ffconcat", 0, 5,
                                    [event], anchor, tmp_path)
    assert report["status"] == "no_eligible_selected_chat"
    assert report["plans"] == []
    assert report["omissions"] == []
    assert calls == []
    assert not (tmp_path / "chat-focus-frames").exists()


@pytest.mark.asyncio
async def test_only_explicitly_selected_chat_is_visually_aligned(tmp_path, monkeypatch):
    import clipper.chat_focus as focus_module
    # This dispatch test does not depend on the optional local OCR installation.
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", SimpleNamespace())
    calls = []

    async def decoded(*args):
        calls.append("decode")
        return []

    def recognized(paths, chats):
        calls.append(("ocr", [event["id"] for event in chats]))
        return [{"time": 2, "rows": [row()]}, {"time": 2.5, "rows": [row()]}], 1280, 720

    monkeypatch.setattr(focus_module, "_extract_frames", decoded)
    monkeypatch.setattr(focus_module, "_recognize", recognized)
    events = [{"id": "cited", "kind": "danmaku", "text": "这个弹幕只是参考依据"},
              {"id": "selected", "kind": "danmaku", "text": "早就说过你吃商很低了"}]
    anchor = {"evidence_ids": ["cited"], "focus_event_ids": ["selected"]}
    report = await align_chat_focus("unused", tmp_path / "unused.ffconcat", 0, 5,
                                    events, anchor, tmp_path)
    assert calls == ["decode", ("ocr", ["selected"])]
    assert report["status"] == "matched"
    assert [plan["event_id"] for plan in report["plans"]] == ["selected"]


def test_filter_is_small_in_place_and_maps_portrait_padding():
    plan = {"bbox": [100, 300, 400, 340], "start": 5, "end": 6.5, "magnification": 1.3}
    filters, label = focus_filter_chain([plan], 1280, 720, 1280, 720)
    text = ";".join(filters)
    assert "scale=390:52" in text
    assert "overlay=55:294" in text
    assert "gte(t,5.000000)*lt(t,6.500000)" in text
    assert "drawtext" not in text
    assert label == "chat_focused_0"
    portrait, _ = focus_filter_chain([plan], 720, 1280, 1280, 720)
    assert "crop=168:22:56:606" in ";".join(portrait)


@pytest.mark.asyncio
async def test_real_decoding_budget_reaches_the_end_of_ten_minute_clip(tmp_path):
    from clipper.chat_focus import _extract_frames
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    source = tmp_path / "ten-minutes.mp4"
    # Small low-framerate footage exercises the full source timeline without
    # an expensive ten-minute HD render or an optional OCR dependency.
    await command([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i", "color=c=navy:s=160x90:r=2:d=600",
                   "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", source])
    concat = tmp_path / "input.ffconcat"
    concat.write_text(f"file '{source}'\n")
    frames = tmp_path / "frames"
    frames.mkdir()
    decoded = await _extract_frames(ffmpeg, concat, 0, 600, frames)
    assert decoded[0][0] == 0
    assert decoded[-1][0] >= 599
    assert all(path.is_file() for _, path in decoded[-2:])
    chats = [{"id": "late-chat", "text": "早就说过你吃商很低了", "start": 1, "end": 1}]
    samples = [{"time": timestamp, "rows": [row()]} for timestamp, _ in decoded[-2:]]
    plans, omitted = plans_from_samples(samples, chats, 1280, 720, 600)
    assert omitted == []
    assert plans[0]["start"] >= 599


@pytest.mark.asyncio
async def test_real_video_chat_is_found_after_actual_appearance(tmp_path, monkeypatch):
    pytest.importorskip("rapidocr_onnxruntime")
    font = ROOT / "static/fonts/NotoSansCJKsc-Bold.otf"
    if not font.is_file():
        pytest.skip("CJK test font absent")
    settings = Settings(data_dir=tmp_path, ffmpeg=imageio_ffmpeg.get_ffmpeg_exe(), font=str(font))
    source = tmp_path / "chat-appears-at-two.mp4"
    from PIL import Image, ImageDraw, ImageFont
    fixture = Image.new("RGB", (1280, 720), "#2388bb")
    ImageDraw.Draw(fixture).text((100, 390), "早就说过你吃商很低了",
                                font=ImageFont.truetype(str(font), 32), fill="white")
    fixture.save(tmp_path / "chat.png")
    await command([settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=0x2388bb:s=1280x720:r=30:d=5",
        "-loop", "1", "-framerate", "30", "-i", tmp_path / "chat.png",
        "-filter_complex", "[0:v][1:v]overlay=enable='gte(t,2)'",
        "-c:v", "libx264", "-threads", "1", "-filter_complex_threads", "1",
        "-t", "5", "-pix_fmt", "yuv420p", source])
    store = Store(tmp_path)
    try:
        session = store.create_session(title="Actual pixel timing fixture", duration=5, mode="file")
        store.add_segment(session["id"], 0, 5, source)
        event_id = store.add_event(session["id"], "danmaku", .2, .2, "早就说过你吃商很低了")
        anchor = store.add_anchor(session["id"], start=0, end=5, timestamp=2,
                                  title="视觉时间测试", evidence_ids=[event_id],
                                  focus_event_ids=[event_id])
        job = store.create_job(anchor["id"], session["id"], anchor=anchor,
                               options={"captions": False, "emphasis": True, "cover": False})
        await Renderer(settings, store).run(job["id"])
        done = store.job(job["id"])
        assert done["status"] == "complete", done.get("error")
        focus = done["chat_focus"]
        assert focus["status"] == "matched"
        assert len(focus["plans"]) == 1
        plan = focus["plans"][0]
        assert 2 <= plan["start"] < 2.6
        assert plan["end"] <= plan["start"] + 1.5
        assert all(t >= 2 for t in plan["confirmed_frame_times"])
        folder = tmp_path / "exports" / job["id"]
        assert "早就说过" not in (folder / "subtitles.ass").read_text()
        metadata = json.loads((folder / "metadata.json").read_text())
        assert metadata["emphasized_chat"] == 1
        assert metadata["chat_focus"] == focus
        # Reaching a scan budget can yield one additional showinfo line, but
        # must not invalidate already decoded evidence frames.
        import clipper.chat_focus as focus_module
        monkeypatch.setattr(focus_module, "MAX_SAMPLES", 3)
        budget_folder = tmp_path / "budget-frames"
        budget_folder.mkdir()
        pairs = await focus_module._extract_frames(
            settings.ffmpeg, folder / "input.ffconcat", 0, 5, budget_folder)
        assert len(pairs) == 3
        assert pairs[0][0] == 0
        assert pairs[-1][0] <= 1.1
    finally:
        store.db.close()
