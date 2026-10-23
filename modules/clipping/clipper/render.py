"""Deterministic, evidence-backed video, cover and subtitle export."""
import asyncio
from contextlib import suppress
import json
import math
from pathlib import Path
import re
import shutil
import time
import zipfile

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance

DEFAULT_RESOLUTION = 1080

STYLES = [
    {"id": "reaction", "name": "精彩瞬间", "description": "保留起因与结果 · 按需强调关键信息", "color": "#70eed1"},
    {"id": "punchline", "name": "一句封神", "description": "黄色大字 · 原话和表情成为主角", "color": "#ffe768"},
    {"id": "context", "name": "剧情反转", "description": "橙色事件标题 · 保留起因与回收", "color": "#ffab73"},
    {"id": "clean", "name": "留白实录", "description": "简洁白字 · 保留更多原始画面", "color": "#f2f0ff"},
]


async def command(args, timeout=120):
    proc = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        raise
    if proc.returncode:
        # Render commands only contain local media paths, never cookies/endpoints.
        raise RuntimeError("媒体处理失败：" + err.decode(errors="replace")[-1600:])
    return out


async def probe(settings, path):
    if settings.ffprobe:
        out = await command([settings.ffprobe, "-v", "error", "-show_entries",
                             "format=duration:stream=codec_type,width,height,duration",
                             "-of", "json", str(path)], timeout=45)
        d = json.loads(out)
        video = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
        if not video:
            raise ValueError("素材没有视频轨")
        duration = float(d.get("format", {}).get("duration") or video.get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("无法读取素材时长")
        return {"duration": duration, "width": video["width"], "height": video["height"],
                "audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}
    # imageio's binary includes ffmpeg but not ffprobe.
    proc = await asyncio.create_subprocess_exec(settings.ffmpeg, "-hide_banner", "-i", str(path),
                                               stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 45)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    text = err.decode(errors="replace")
    d = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    v = re.search(r"Video:[^\n]*? (\d{2,5})x(\d{2,5})", text)
    if not d or not v:
        raise ValueError("无法读取素材时长或视频轨")
    return {"duration": int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]),
            "width": int(v[1]), "height": int(v[2]), "audio": "Audio:" in text}


def ass_text(text):
    # Prevent source chat from introducing ASS formatting or control sequences.
    return re.sub(r"[{}\\\r\n]", " ", str(text)).strip()


def ass_time(seconds):
    centis = max(0, round(seconds * 100))
    return f"{centis//360000}:{centis//6000%60:02}:{centis//100%60:02}.{centis%100:02}"


def srt_time(seconds):
    millis = max(0, round(seconds * 1000))
    return f"{millis//3600000:02}:{millis//60000%60:02}:{millis//1000%60:02},{millis%1000:03}"


def wrap_text(text, limit=22):
    # Chinese CJK letters occupy one editorial cell; avoid long unbroken subtitles.
    clean = ass_text(text)
    return [clean[i:i + limit] for i in range(0, len(clean), limit)] or [""]


def write_subtitles(folder, events, anchor, width, height, captions=True, emphasis=True):
    start, end = anchor["start"], anchor["end"]
    spoken = [e for e in events if e["kind"] in {"transcript", "audio_segment"} and e["text"]]
    # Keep captions equally readable at 720p and 1080p in either orientation.
    # ASS coordinates use the output resolution, so borders and margins must
    # scale with the font instead of becoming thinner at higher resolutions.
    scale = min(width, height) / 720
    size = round((48 if width > height else 44) * scale)
    margin = round(40 * scale)
    bottom = round(38 * scale)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Noto Sans CJK SC,{size},&H00FFFFFF,&H00FFFFFF,&H00121218,&H80121218,-1,0,0,0,100,100,0,0,1,{3 * scale:g},{scale:g},2,{margin},{margin},{bottom},1
Style: Info,Noto Sans CJK SC,{round(22 * scale)},&H00FFFFFF,&H00FFFFFF,&H00121218,&H80121218,0,0,0,0,100,100,0,0,1,{2 * scale:g},0,7,{round(24 * scale)},{round(24 * scale)},{round(22 * scale)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines, srt, included_spoken = [], [], 0
    for e in spoken:
        # There are no word timestamps in chunk-ASR. Cropping a chunk and
        # redistributing its whole sentence would invent captions for cut audio.
        if e["start"] < start - .08 or e["end"] > end + .08:
            continue
        a, b = max(start, e["start"]) - start, min(end, e["end"]) - start
        if b <= a:
            continue
        included_spoken += 1
        parts = wrap_text(e["text"], 24 if width > height else 13)
        if (e.get("data") or {}).get("timing_basis") == "forced_alignment":
            # Long indivisible aligned words can wrap visually, but their
            # measured interval must never be divided by character count.
            srt_text = "\n".join(parts)
            srt.append(f"{len(srt)+1}\n{srt_time(a)} --> {srt_time(b)}\n{srt_text}\n")
            if captions:
                caption = r"\N".join(parts)
                lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,{caption}")
            continue
        # Split long chunks over their measured interval, not extra invented time.
        for index, text in enumerate(parts):
            x, y = a + (b-a)*index/len(parts), a + (b-a)*(index+1)/len(parts)
            srt.append(f"{len(srt)+1}\n{srt_time(x)} --> {srt_time(y)}\n{text}\n")
            if captions:
                lines.append(f"Dialogue: 0,{ass_time(x)},{ass_time(y)},Caption,,0,0,0,,{text}")
    # Chat emphasis now uses visually verified source pixels in chat_focus.py.
    # A WebSocket receive time must never create a synthetic caption/dialogue.
    (folder / "subtitles.ass").write_text(header + "\n".join(lines), encoding="utf-8")
    (folder / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")
    return included_spoken, 0


def cover_headline_layout(font_path, text, width, height, vertical=False):
    """Fit a complete headline into the upper safe area, above feed overlays."""
    text = str(text).strip()[:45]
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()] or [""]
    # More than three manual rows used to be silently dropped. Reflow them so
    # that the headline remains complete, including its final punchline.
    if len(paragraphs) > 3:
        paragraphs = [" ".join(paragraphs)]
    margin = 48
    top = round(height * (.10 if vertical else .055))
    bottom = round(height * (.47 if vertical else .55))
    stroke = 6
    for size in range(112 if vertical else 104, 39, -2):
        font = ImageFont.truetype(font_path, size)
        rows = []
        for paragraph in paragraphs:
            row = ""
            for character in paragraph:
                candidate = row + character
                if row and font.getlength(candidate) + stroke * 2 > width - margin * 2:
                    rows.append(row.rstrip())
                    row = character.lstrip()
                else:
                    row = candidate
            rows.append(row.rstrip())
        line_height = round(size * 1.12)
        if len(rows) <= 3 and line_height * len(rows) <= bottom - top:
            break
    positions = [((width - font.getlength(row)) / 2, top + index * line_height)
                 for index, row in enumerate(rows)]
    return {"font": font, "font_size": size, "rows": rows,
            "positions": positions, "stroke_width": stroke,
            "safe_box": (margin, top - stroke, width - margin, bottom + stroke)}


def make_cover(settings, frame, output, anchor, vertical=False):
    w, h = (1080, 1440) if vertical else (1280, 720)
    raw = Image.open(frame).convert("RGB")
    # Fit the full source into the design; faces need not be guessed or fabricated.
    backdrop = ImageOps.fit(raw, (w, h))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(.66).convert("RGBA")
    foreground = ImageOps.contain(raw, (w, h))
    backdrop.alpha_composite(foreground.convert("RGBA"), ((w-foreground.width)//2, (h-foreground.height)//2))
    shade = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(shade)
    for y in range(h):
        # Text and contrast now sit above the platform's bottom overlays.
        # Leave the lower source image clear instead of darkening it needlessly.
        alpha = int(175 * max(0, 1 - y / (h * .65)))
        d.line([(0,y),(w,y)], fill=(12,12,20,alpha))
    backdrop = Image.alpha_composite(backdrop, shade)
    draw = ImageDraw.Draw(backdrop)
    color = next((x["color"] for x in STYLES if x["id"] == anchor.get("style")), "#70eed1")
    layout = cover_headline_layout(settings.font,
        anchor.get("cover_text") or anchor["title"], w, h, vertical)
    for i, (text, position) in enumerate(zip(layout["rows"], layout["positions"])):
        draw.text(position, text, font=layout["font"], anchor="lt",
                  fill=color if i == len(layout["rows"])-1 else "#ffffff",
                  stroke_width=layout["stroke_width"], stroke_fill="#161620")
    backdrop.convert("RGB").save(output, quality=94)


def validate_range(anchor, segments):
    start, end = float(anchor["start"]), float(anchor["end"])
    if not math.isfinite(start + end) or start < 0 or end - start < 1 or end - start > 600:
        raise ValueError("切片长度需在 1–600 秒之间")
    if not segments:
        raise ValueError("选段尚无已保存的视频，等待录制完成后再导出")
    cursor = start
    for s in segments:
        if s["start"] > cursor + .35:
            raise ValueError(f"选段在 {cursor:.1f} 秒附近存在录像缺口，请调整进出点")
        cursor = max(cursor, s["end"])
    if cursor < end - .2:
        raise ValueError("选段结尾尚未录制完成，请稍后导出")


def validate_region(region):
    if not region:
        return None
    result = {k: float(region[k]) for k in ["x", "y", "w", "h", "start", "end"]}
    if not all(math.isfinite(x) for x in result.values()):
        raise ValueError("放大区域参数必须为有限数值")
    if (not 0 <= result["x"] <= .95 or not 0 <= result["y"] <= .95 or
            not .05 <= result["w"] <= 1 or not .05 <= result["h"] <= 1 or
            result["x"] + result["w"] > 1.001 or result["y"] + result["h"] > 1.001 or
            result["end"] <= result["start"]):
        raise ValueError("放大区域超出画面或时间范围")
    return result


def publication_evidence_snapshot(events, anchor):
    """Copy cited source text that is wholly present in the rendered interval."""
    selected = set(anchor.get("evidence_ids") or [])
    return [{key: event[key] for key in ("id", "kind", "start", "end", "text")}
            for event in events
            if event["id"] in selected
            and anchor["start"] <= event["start"] <= event["end"] <= anchor["end"]]


class Renderer:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.slot = asyncio.Semaphore(1)

    async def run(self, job_id):
        async with self.slot:
            job = self.store.job(job_id)
            # Snapshot the edit at enqueue time; subsequent UI edits cannot mutate a queued export.
            anchor = job["anchor"]
            folder = self.settings.data_dir / "exports" / job_id
            folder.mkdir(parents=True, exist_ok=True)
            try:
                self.store.update_job(job_id, status="rendering", progress=5, error=None)
                segments = self.store.segments(anchor["session_id"], anchor["start"], anchor["end"])
                validate_range(anchor, segments)
                options = {"resolution": DEFAULT_RESOLUTION, **job.get("options", {})}
                self.store.update_job(job_id, options=options)
                vertical = options.get("aspect") == "vertical"
                w, h = (1920, 1080) if options["resolution"] == 1080 else (1280, 720)
                if vertical:
                    w, h = h, w
                events = self.store.events(anchor["session_id"], anchor["start"], anchor["end"], limit=100000)
                evidence_snapshot = publication_evidence_snapshot(events, anchor)
                omitted = sum(e["kind"] in {"transcript","audio_segment"} and
                              (e["start"] < anchor["start"]-.08 or e["end"] > anchor["end"]+.08)
                              for e in events)
                warning = ("选段截到了语音块中间；因无逐字时间，已省略边界语音块字幕，避免放入被剪掉的台词。"
                           if omitted and options.get("captions",True) else None)
                self.store.update_job(job_id,warning=warning)
                spoken, _ = write_subtitles(folder, events, anchor, w, h,
                                                options.get("captions", True), options.get("emphasis", True))
                # ffconcat file names are our own numeric indexes, never unescaped user input.
                source_dir = folder / "sources"
                source_dir.mkdir(exist_ok=True)
                concat = ["ffconcat version 1.0"]
                for index, s in enumerate(segments):
                    p = Path(s["path"])
                    if not p.is_file():
                        raise ValueError("源录像已移走，无法导出")
                    linked = source_dir / f"{index:06}{p.suffix}"
                    if not linked.exists():
                        linked.symlink_to(p.resolve())
                    concat.append(f"file 'sources/{linked.name}'")
                    concat.append(f"duration {s['end']-s['start']:.6f}")
                (folder / "input.ffconcat").write_text("\n".join(concat)+"\n")
                offset = anchor["start"] - segments[0]["start"]
                duration = anchor["end"] - anchor["start"]
                info = await probe(self.settings, segments[0]["path"])
                base = [self.settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "0", "-i", folder / "input.ffconcat"]
                from .subtitle_review import review_subtitles
                source = self.store.session(anchor["session_id"])
                scoped = self.settings.for_session(source)
                self.store.update_job(job_id, progress=8, render_stage="复听成片音频、校准字幕")
                caption_events, subtitle_report = await review_subtitles(
                    scoped, folder, events, anchor, w, h, offset=offset,
                    has_audio=info["audio"], source=source,
                    enabled=options.get("captions", True), command=command)
                if subtitle_report["applied"]:
                    spoken, _ = write_subtitles(folder, caption_events, anchor, w, h,
                                                options.get("captions", True), options.get("emphasis", True))
                    # Whole-clip aligned ASR recovers even the clipped boundary
                    # chunks. Their old omission warning no longer describes it.
                    warning = None
                if subtitle_report.get("warning"):
                    warning = " ".join(filter(None, [warning, subtitle_report["warning"]]))
                from .chat_focus import align_chat_focus, focus_filter_chain, MAGNIFICATION
                focus_report = {"version": 1, "status": "disabled", "plans": [], "omissions": []}
                if options.get("emphasis", True):
                    self.store.update_job(job_id, progress=10, render_stage="定位原画面弹幕")
                    focus_report = await align_chat_focus(
                        self.settings.ffmpeg, folder / "input.ffconcat", offset, duration,
                        events, anchor, folder)
                chats = len(focus_report["plans"])
                if focus_report.get("omissions"):
                    focus_warning = "部分关键弹幕未在原画面中可靠定位，已保留原画面并省略对应放大。"
                    warning = f"{warning} {focus_warning}" if warning else focus_warning
                self.store.update_job(job_id, warning=warning, render_stage="编码成片")
                (folder / "chat-focus.json").write_text(json.dumps(focus_report, ensure_ascii=False, indent=2))
                # Extract a real frame near the interesting moment for the cover.
                cover_t = max(offset, min(offset+duration-.1, offset+float(anchor.get("timestamp", anchor["start"]))-anchor["start"]))
                if options.get("cover",True):
                    await command(base + ["-ss", f"{cover_t:.3f}", "-frames:v", "1", folder / "frame.jpg"])
                self.store.update_job(job_id, progress=15)
                if options.get("cover",True):
                    if options.get("cover_mode", "designed") == "frame":
                        # Preserve the selected source frame, including any existing design.
                        await asyncio.to_thread(shutil.copyfile, folder / "frame.jpg", folder / "cover.jpg")
                    else:
                        await asyncio.to_thread(make_cover, self.settings, folder / "frame.jpg", folder / "cover.jpg", anchor, vertical)
                # Geometry preserves the whole source for portrait exports.
                filters = [f"[0:v]trim=start={offset:.6f}:duration={duration:.6f},setpts=PTS-STARTPTS,"
                           f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x11111b,setsar=1[base]"]
                label = "base"
                region = validate_region(options.get("focus_region") or anchor.get("focus_region"))
                focus_plans = list(focus_report["plans"])
                if region:
                    a, b = max(0, region["start"]-anchor["start"]), min(duration, region["end"]-anchor["start"])
                    if b > a:
                        # A user's region is explicit pixel/time evidence. Keep
                        # it in place, with the same restrained 1.3x enlargement.
                        focus_plans = [plan for plan in focus_plans
                                       if plan["end"] <= a or plan["start"] >= b]
                        focus_plans.append({"start": a, "end": b, "magnification": MAGNIFICATION,
                            "bbox": [region["x"]*info["width"], region["y"]*info["height"],
                                     (region["x"]+region["w"])*info["width"],
                                     (region["y"]+region["h"])*info["height"]],
                            "timing_basis": "manual_source_region"})
                focus_filters, label = focus_filter_chain(
                    focus_plans, w, h, info["width"], info["height"], label)
                filters.extend(focus_filters)
                # Run from job directory so ASS filter paths contain no special characters.
                font_dir = folder / "fonts"
                font_dir.mkdir(exist_ok=True)
                local_font = font_dir / "NotoSansCJKsc-Bold.otf"
                if not local_font.exists():
                    local_font.symlink_to(Path(self.settings.font).resolve())
                filters.append(f"[{label}]ass=filename=subtitles.ass:fontsdir=fonts,format=yuv420p[out]")
                (folder / "filter.txt").write_text(";\n".join(filters))
                args = [str(x) for x in base] + ["-filter_complex_script", "filter.txt", "-map", "[out]"]
                if info["audio"]:
                    args += ["-af", f"atrim=start={offset:.6f}:duration={duration:.6f},asetpts=PTS-STARTPTS",
                             "-map", "0:a:0", "-c:a", "aac", "-b:a", "160k"]
                args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-threads", "2",
                         "-filter_complex_threads", "1", "-r", "30", "-t", f"{duration:.6f}",
                         "-movflags", "+faststart", "clip.mp4"]
                proc = await asyncio.create_subprocess_exec(*args, cwd=folder,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                try:
                    async def wait():
                        while proc.returncode is None:
                            await asyncio.sleep(1)
                            self.store.update_job(job_id, progress=min(89, self.store.job(job_id)["progress"]+1))
                    ticker = asyncio.create_task(wait())
                    _, err = await asyncio.wait_for(proc.communicate(), max(180, duration*12))
                    if proc.returncode:
                        raise RuntimeError("视频编码失败：" + err.decode(errors="replace")[-1200:])
                finally:
                    ticker.cancel()
                    with suppress(asyncio.CancelledError):
                        await ticker
                    if proc.returncode is None:
                        with suppress(ProcessLookupError):
                            proc.kill()
                        await proc.wait()
                result = await probe(self.settings, folder / "clip.mp4")
                if abs(result["duration"]-duration) > max(1, duration*.015):
                    raise RuntimeError("成片时长验证未通过，请检查源录像时间轴")
                source = self.store.session(anchor["session_id"])
                from .publishing import metadata_for, publication_metadata_evidence
                publication_job = {**job, "evidence_snapshot": evidence_snapshot}
                publication_metadata = metadata_for(publication_job, source)
                publication_evidence = publication_metadata_evidence(publication_job, source)
                metadata = {"anchor": anchor, "options": options, "source_title": source.get("title"),
                            "model_profile": scoped.model_profile,
                            "source_url": source.get("source_url", ""), "duration": result["duration"],
                            "publication_metadata": publication_metadata,
                            "publication_metadata_evidence": publication_evidence,
                            "evidence_snapshot": evidence_snapshot,
                            "subtitles": spoken, "emphasized_chat": chats,
                            "chat_focus": focus_report,
                            "subtitle_review": subtitle_report,
                            "omitted_boundary_caption_chunks":0 if subtitle_report["applied"] else omitted,
                            "original_boundary_caption_chunks":omitted, "warning":warning,
                            "source_clock": "session_relative_seconds",
                            "rendered_at": time.time(), "title_variants": anchor.get("title_variants", [anchor["title"]]),
                            "source_segments": [{"start": s["start"], "end": s["end"],
                                                "timestamp_basis": s["metadata"].get("timestamp_basis")}
                                               for s in segments]}
                (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
                (folder / "titles.txt").write_text("\n".join(metadata["title_variants"]), encoding="utf-8")
                def pack():
                    with zipfile.ZipFile(folder / "package.zip", "w", zipfile.ZIP_STORED) as z:
                        names = ["clip.mp4", "subtitles.srt", "metadata.json", "titles.txt", "chat-focus.json",
                                 "subtitle-review.json"]
                        if options.get("cover",True):
                            names.append("cover.jpg")
                        for name in names:
                            z.write(folder / name, name)
                await asyncio.to_thread(pack)
                # /api/state returns every job repeatedly. Keep full transcripts
                # and word alignment in the report/metadata/package, not each row.
                subtitle_summary = {key: subtitle_report[key] for key in (
                    "version", "status", "applied", "model", "model_profile", "aligner", "timing_basis",
                    "cache_hit", "duration", "reason", "warning", "speaker_attribution")
                    if key in subtitle_report}
                subtitle_summary["word_count"] = len(subtitle_report.get("words", []))
                subtitle_summary["caption_count"] = len(subtitle_report.get("reviewed_subtitles", []))
                self.store.update_job(job_id, status="complete", progress=100, error=None,
                                      render_stage="完成", duration=result["duration"],
                                      evidence_snapshot=evidence_snapshot,
                                      publication_metadata=publication_metadata,
                                      publication_metadata_evidence=publication_evidence,
                                      chat_focus=focus_report,
                                      subtitle_review=subtitle_summary,
                                      video_url=f"/api/exports/{job_id}/clip.mp4",
                                      cover_url=f"/api/exports/{job_id}/cover.jpg" if options.get("cover",True) else None,
                                      package_url=f"/api/exports/{job_id}/package.zip",
                                      subtitles_url=f"/api/exports/{job_id}/subtitles.srt")
                self.store.update_anchor(anchor["id"], status="exported")
            except asyncio.CancelledError:
                self.store.update_job(job_id, status="failed", error="导出已中断，可重新提交")
                raise
            except Exception as exc:
                self.store.update_job(job_id, status="failed", error=str(exc)[:1800])
