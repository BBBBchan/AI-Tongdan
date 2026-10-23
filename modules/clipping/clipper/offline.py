"""File imports use a media-relative clock, shared by ASR, frames and editing."""
import asyncio
import base64
import json
import io
import math
from pathlib import Path
import re
import wave

import httpx

from .render import command, probe


def parse_events(value):
    if isinstance(value, dict):
        value = value.get("events", value.get("segments", []))
    if not isinstance(value, list) or len(value) > 100000:
        raise ValueError("事件文件需为 JSON 数组或包含 events 的对象")
    result = []
    for row in value:
        if not isinstance(row, dict):
            continue
        kind = row.get("kind", row.get("type", "transcript"))
        if kind == "audio_segment":
            kind = "transcript"
        if kind not in {"transcript", "danmaku", "observation"}:
            continue
        a = float(row.get("start", row.get("timestamp", 0)))
        b = float(row["end"]) if row.get("end") is not None else a + .1
        if not math.isfinite(a+b) or a < 0 or b < a:
            raise ValueError("事件时间需为从视频起点开始的非负秒数")
        result.append({"kind": kind, "start": a, "end": b, "text": str(row.get("text", ""))[:5000],
                       "data": row.get("data", {}) if isinstance(row.get("data", {}), dict) else {}})
    return result


def read_event_file(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    if Path(path).suffix.lower() == ".srt":
        rows = []
        pattern = r"(\d\d):(\d\d):(\d\d)[,.](\d{3})"
        for part in re.split(r"\n\s*\n", text.strip()):
            lines = part.splitlines()
            line = next((i for i,x in enumerate(lines) if "-->" in x), None)
            if line is None:
                continue
            matches = re.findall(pattern, lines[line])
            if len(matches) != 2:
                continue
            def sec(m):
                return int(m[0])*3600+int(m[1])*60+int(m[2])+int(m[3])/1000
            rows.append({"kind":"transcript", "start":sec(matches[0]), "end":sec(matches[1]),
                         "text":" ".join(lines[line+1:])})
        return parse_events(rows)
    try:
        return parse_events(json.loads(text))
    except json.JSONDecodeError:
        return parse_events([json.loads(line) for line in text.splitlines() if line.strip()])


async def prepare_file(settings, store, sid, transcribe=True):
    session = store.session(sid)
    path = session["media_path"]
    duration = session["duration"]
    folder = settings.data_dir / "sessions" / sid
    folder.mkdir(parents=True, exist_ok=True)
    config = settings.audio_config()
    # A partially successful ASR pass must remain resumable; even silent chunks
    # get a completion checkpoint. External captions are intentionally reused.
    external_transcript = any(e["kind"] == "transcript" and e["data"].get("source") != "asr"
                              for e in store.events(sid, limit=100000))
    completed = set(session.get("asr_completed_chunks", []))
    store.update_session(sid, analysis_status="正在提取画面与语音", analysis_progress=0)
    async with httpx.AsyncClient(timeout=55, trust_env=False) as client:
        for index, start in enumerate(range(0, math.ceil(duration), 12)):
            length = min(12, duration-start)
            if length < .2:
                break
            frame = folder / f"frame-{start:07}.jpg"
            if not frame.exists():
                await command([settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                               "-ss", str(start), "-i", path, "-frames:v", "1", "-vf", "scale=640:-2",
                               frame], timeout=60)
            store.add_event(sid, "frame", start, start, "", {"path": str(frame)}, f"frame:{start}")
            if index == 0:
                store.update_session(sid, latest_frame=str(frame))
            if transcribe and not external_transcript and start not in completed and config.get("base_url"):
                pcm = await command([settings.ffmpeg, "-hide_banner", "-loglevel", "error",
                                     "-ss", str(start), "-i", path, "-t", str(length),
                                     "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"])
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as wavfile:
                    wavfile.setnchannels(1)
                    wavfile.setsampwidth(2)
                    wavfile.setframerate(16000)
                    wavfile.writeframes(pcm)
                wav = buffer.getvalue()
                headers = {"Authorization": f"Bearer {config['api_key']}"} if config.get("api_key") else {}
                try:
                    response = await client.post(config["base_url"].rstrip("/")+"/analyze", headers=headers,
                                json={"audio_b64": base64.b64encode(wav).decode("ascii")})
                    response.raise_for_status()
                    result = response.json()
                    parts = result.get("segments") or [{"start":0, "end":length, "text":result.get("text","")}]
                    for n, part in enumerate(parts[:48]):
                        text = str(part.get("text") or "").strip()
                        a, b = max(0, float(part.get("start", 0))), min(length, float(part.get("end", length)))
                        if text and math.isfinite(a+b) and b > a:
                            store.add_event(sid, "transcript", start+a, start+b, text,
                                {"source":"asr", "role":"unknown", "timing":"file_media_seconds", "chunk_start":start},
                                f"asr:{start}:{n}")
                    completed.add(start)
                    store.update_session(sid, asr_completed_chunks=sorted(completed))
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    store.update_session(sid, transcription_warning="部分语音转写失败，已保留可用内容，可补充字幕后重新分析")
            store.update_session(sid, analysis_progress=round(60*(start+length)/duration))
    if transcribe and not external_transcript and not config.get("base_url"):
        store.update_session(sid, transcription_warning="未配置语音模型，可导入带时间的字幕或弹幕后分析")


async def make_preview(settings, store, sid):
    session = store.session(sid)
    folder = settings.data_dir / "sessions" / sid
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / "preview.mp4"
    await command([settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-i", session["media_path"], "-vf", "scale=960:-2:force_original_aspect_ratio=decrease",
                   "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "28", "-threads", "2", "-c:a", "aac", "-movflags", "+faststart", output],
                  timeout=max(180, session["duration"]*5))
    store.update_session(sid, preview_path=str(output))
