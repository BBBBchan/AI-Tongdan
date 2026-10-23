"""Locate chat in decoded source pixels before adding a modest local magnifier.

WebSocket timestamps deliberately do not participate in matching or display
timing. A message must be readable in adjacent source frames at a stable
location. Missing OCR, unreadable/ambiguous text, movement and absent messages
all fail closed: the original video is preserved without emphasis.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import math
from pathlib import Path
import re
import unicodedata


SAMPLE_INTERVAL = .5
# Cover the full ten-minute automatic/manual export limit while keeping
# a bounded decoder budget and source-frame timestamp verification.
MAX_SAMPLES = 1201
MAX_FOCUS_SECONDS = 1.5
MIN_CONFIDENCE = .88
MAGNIFICATION = 1.30


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", str(text))
                   if c.isalnum())


def selected_chats(events, anchor):
    # Citation explains why a clip is interesting; it does not request an edit.
    # Missing and explicitly empty selections both preserve the source pixels.
    ids = set(anchor.get("focus_event_ids") or [])
    seen, result = set(), []
    for event in events:
        value = normalized(event.get("text", ""))
        # Very short phrases cannot distinguish a chat bubble from scene text.
        if (event.get("kind") != "danmaku" or event.get("id") not in ids
                or len(value) < 6 or value in seen):
            continue
        seen.add(value)
        result.append(event)
    return result[:4]


def _box(row):
    points = row[0]
    xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def match_text(rows, text, width, height):
    """Require an exact complete OCR string, including wrapped bubble lines."""
    target = normalized(text)
    if len(target) < 6:
        return None
    lines = []
    for row in rows or []:
        try:
            box = _box(row)
            score = float(row[2])
            value = normalized(row[1])
        except (IndexError, TypeError, ValueError):
            continue
        if score >= MIN_CONFIDENCE and value and all(math.isfinite(x) for x in box):
            lines.append({"box": box, "score": score, "text": value})
    lines.sort(key=lambda line: (line["box"][1], line["box"][0]))
    candidates = []
    for first in lines:
        group = [first]
        # Wraps normally keep the same left edge. Never concatenate arbitrary
        # neighboring bubbles or omit an unrecognized suffix.
        for _ in range(2):
            value = "".join(line["text"] for line in group)
            if target == value:
                box = [min(x["box"][0] for x in group), min(x["box"][1] for x in group),
                       max(x["box"][2] for x in group), max(x["box"][3] for x in group)]
                x1, y1, x2, y2 = box
                # Restrict automatic effects to a small side-panel message;
                # a central subtitle/title is not sufficient chat evidence.
                side = x2 <= width * .48 or x1 >= width * .52
                if (side and 4 < x2-x1 <= width*.42 and 4 < y2-y1 <= height*.16):
                    candidates.append({"bbox": box, "confidence": min(x["score"] for x in group),
                                       "recognized_text": value})
                break
            if not target.startswith(value):
                break
            last = group[-1]["box"]
            line_height = max(1, last[3]-last[1])
            following = [line for line in lines if line not in group
                         and 0 <= line["box"][1]-last[3] <= line_height*.9
                         and abs(line["box"][0]-first["box"][0]) <= line_height*.65]
            if not following:
                break
            group.append(min(following, key=lambda line: line["box"][1]))
        else:
            # Three-line messages are intentionally omitted; they are too large
            # for the requested unobtrusive magnifier.
            continue
    return candidates[0] if len(candidates) == 1 else None


def stable_runs(samples, text, width, height):
    """Return confirmed intervals; never extrapolate past a verified frame."""
    runs, active = [], []
    for sample in samples:
        match = match_text(sample.get("rows"), text, width, height)
        if match:
            entry = {"time": float(sample["time"]), **match}
            if active:
                delta = max(abs(a-b) for a, b in zip(active[-1]["bbox"], entry["bbox"]))
                gap = entry["time"]-active[-1]["time"]
                if delta > max(6, width*.005) or not 0 < gap <= SAMPLE_INTERVAL + .12:
                    if len(active) >= 2:
                        runs.append(active)
                    active = []
            active.append(entry)
        else:
            if len(active) >= 2:
                runs.append(active)
            active = []
    if len(active) >= 2:
        runs.append(active)
    return runs


def plans_from_samples(samples, chats, width, height, duration):
    plans, omissions = [], []
    for event in chats:
        runs = stable_runs(samples, event["text"], width, height)
        chosen = next((run for run in runs if run[-1]["time"]-run[0]["time"] >= .45), None)
        if not chosen:
            omissions.append({"event_id": event["id"], "reason": "no_stable_complete_visual_match"})
            continue
        start = chosen[0]["time"]
        end = min(duration, start+MAX_FOCUS_SECONDS, chosen[-1]["time"])
        confirmed = [item for item in chosen if item["time"] <= end+.01]
        # Cover the actual recognized glyphs, with only a small pixel margin.
        pad = max(6, height*.008)
        box = [max(0, min(x["bbox"][0] for x in confirmed)-pad),
               max(0, min(x["bbox"][1] for x in confirmed)-pad),
               min(width, max(x["bbox"][2] for x in confirmed)+pad),
               min(height, max(x["bbox"][3] for x in confirmed)+pad)]
        plans.append({"event_id": event["id"], "text": event["text"],
                      "start": round(start, 6), "end": round(end, 6),
                      "bbox": [round(value, 2) for value in box],
                      "source_width": width, "source_height": height,
                      "magnification": MAGNIFICATION,
                      "confidence": round(min(item["confidence"] for item in confirmed), 6),
                      "confirmed_frame_times": [item["time"] for item in confirmed],
                      "timing_basis": "decoded_source_frame_pts",
                      "matching": "complete_exact_text_in_adjacent_stable_frames"})
    # At most one bubble grows at a time; source text and ordering remain intact.
    plans.sort(key=lambda plan: plan["start"])
    accepted = []
    for plan in plans:
        if accepted and plan["start"] < accepted[-1]["end"]:
            omissions.append({"event_id": plan["event_id"], "reason": "overlaps_previous_focus"})
            continue
        accepted.append(plan)
    return accepted, omissions


async def _extract_frames(ffmpeg, concat, offset, duration, folder):
    # showinfo is BEFORE the output encoder and records the selected source PTS.
    # No fps resampling or guessed WebSocket-to-video latency is involved.
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "info", "-y",
               "-f", "concat", "-safe", "0", "-i", str(concat),
               "-an", "-vf", f"trim=start={offset:.6f}:duration={duration:.6f},"
               "setpts=PTS-STARTPTS,"
               f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{SAMPLE_INTERVAL})',showinfo",
               "-fps_mode", "vfr", "-frames:v", str(MAX_SAMPLES),
               "-q:v", "2", str(folder / "frame-%04d.jpg")]
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                  stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await asyncio.wait_for(process.communicate(), max(60, duration*2))
    except BaseException:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError("source_frame_decode_failed")
    times = [float(x) for x in re.findall(r"\bn:\s*\d+.*?\bpts_time:([0-9.eE+-]+)", error.decode(errors="replace"))]
    paths = sorted(folder.glob("frame-*.jpg"))
    # FFmpeg may run one extra filter frame before -frames:v stops its encoder.
    # Only emitted image files are evidence; discard the unencoded log entry.
    if len(times) < len(paths):
        raise RuntimeError("source_frame_timestamp_count_mismatch")
    return list(zip(times[:len(paths)], paths))


def _recognize(paths, chats):
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    samples = []
    width = height = 0
    for timestamp, path in paths:
        with Image.open(path) as frame:
            width, height = frame.size
        rows, _ = engine(str(path), use_cls=False)
        samples.append({"time": timestamp, "rows": rows or []})
        # Once every selected message has a verified short stable interval,
        # later frames cannot change its first appearance or selected effect.
        if all(any(run[-1]["time"]-run[0]["time"] >= MAX_FOCUS_SECONDS
                   for run in stable_runs(samples, event["text"], width, height))
               for event in chats):
            break
    return samples, width, height


async def align_chat_focus(ffmpeg, concat, offset, duration, events, anchor, folder):
    chats = selected_chats(events, anchor)
    report = {"version": 1, "method": "local_ocr_source_pixels",
              "timing_basis": "decoded_source_frame_pts", "plans": [], "omissions": [],
              "sample_interval_seconds": SAMPLE_INTERVAL}
    if not chats:
        report["status"] = "no_eligible_selected_chat"
        return report
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:
        report.update(status="ocr_unavailable", omissions=[
            {"event_id": event["id"], "reason": "local_ocr_not_installed"} for event in chats])
        return report
    frames = folder / "chat-focus-frames"
    frames.mkdir(exist_ok=True)
    try:
        paths = await _extract_frames(ffmpeg, concat, offset, duration, frames)
        samples, width, height = await asyncio.to_thread(_recognize, paths, chats)
        plans, omissions = plans_from_samples(samples, chats, width, height, duration)
        report.update(status="matched" if plans else "no_visual_match", plans=plans, omissions=omissions,
                      scanned_frames=len(samples), source_width=width, source_height=height,
                      scanned_until_seconds=samples[-1]["time"] if samples else 0,
                      frame_budget=MAX_SAMPLES,
                      scan_budget_exhausted=len(paths) >= MAX_SAMPLES and
                          paths[-1][0] < duration-SAMPLE_INTERVAL)
        # Retain only the exact evidence frames that justify applied effects.
        evidence_times = {t for plan in plans for t in plan["confirmed_frame_times"]}
        for t, path in paths:
            if t not in evidence_times:
                path.unlink(missing_ok=True)
        (folder / "chat-focus-ocr.json").write_text(json.dumps(
            [sample for sample in samples if sample["time"] in evidence_times],
            ensure_ascii=False, indent=2))
    except asyncio.CancelledError:
        raise
    except Exception:
        # Never leak backend paths or produce a guessed replacement effect.
        report.update(status="alignment_failed", omissions=[
            {"event_id": event["id"], "reason": "visual_alignment_failed"} for event in chats])
    return report


def focus_filter_chain(plans, output_width, output_height, source_width, source_height,
                       input_label="base"):
    """Magnify source pixels in place, respecting letterboxing in portrait."""
    scale = min(output_width/source_width, output_height/source_height)
    pad_x = (output_width-source_width*scale)/2
    pad_y = (output_height-source_height*scale)/2
    filters, prepared = [], []
    for plan in plans:
        x1, y1, x2, y2 = plan["bbox"]
        x, y = int(x1*scale+pad_x)//2*2, int(y1*scale+pad_y)//2*2
        width, height = max(2, round((x2-x1)*scale)//2*2), max(2, round((y2-y1)*scale)//2*2)
        width, height = min(width, output_width-x), min(height, output_height-y)
        zoom = min(1.4, max(1.25, plan.get("magnification", MAGNIFICATION)))
        zoom_w, zoom_h = round(width*zoom)//2*2, round(height*zoom)//2*2
        overlay_x = max(0, min(output_width-zoom_w, round(x+width/2-zoom_w/2)))
        overlay_y = max(0, min(output_height-zoom_h, round(y+height/2-zoom_h/2)))
        prepared.append((plan, x, y, width, height, zoom_w, zoom_h, overlay_x, overlay_y))
    if not prepared:
        return filters, input_label
    split_outputs = "[focus_base]" + "".join(f"[focus_source_{i}]" for i in range(len(prepared)))
    filters.append(f"[{input_label}]split={len(prepared)+1}{split_outputs}")
    previous = "focus_base"
    for i, (plan, x, y, width, height, zoom_w, zoom_h, px, py) in enumerate(prepared):
        filters.append(f"[focus_source_{i}]crop={width}:{height}:{x}:{y},"
                       f"scale={zoom_w}:{zoom_h}:flags=lanczos,"
                       "drawbox=x=0:y=0:w=iw:h=ih:color=white@0.65:t=1"
                       f"[chat_zoom_{i}]")
        label = f"chat_focused_{i}"
        filters.append(f"[{previous}][chat_zoom_{i}]overlay={px}:{py}:"
                       f"enable='gte(t,{plan['start']:.6f})*lt(t,{plan['end']:.6f})'[{label}]")
        previous = label
    return filters, previous
