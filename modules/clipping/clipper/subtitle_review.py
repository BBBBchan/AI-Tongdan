"""Re-listen to the exact rendered audio; keep evidence and live ASR untouched."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit
import wave

import httpx


REVIEW_VERSION = 1
MODEL = "Qwen3-ASR-1.7B"
ALIGNER = "Qwen3-ForcedAligner-0.6B"
MAX_SECONDS = 600
MAX_WAV_BYTES = MAX_SECONDS * 16000 * 2 + 65536
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TEXT = 30000
MAX_WORDS = 20000


class ReviewError(ValueError):
    """Only fixed, non-sensitive reason codes may leave this module."""


def identity_context(source):
    """Use platform identity only, never a title, transcript or generated summary."""
    room = source.get("room") if isinstance(source.get("room"), dict) else {}
    name = room.get("uname") or source.get("streamer") or ""
    if not isinstance(name, str):
        name = ""
    name = "".join(c for c in name[:80] if c.isalnum() or c in " _-·").strip()
    return ("B站直播。" + (f"主播名：{name}。" if name else ""))[:2000]


def _normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold()
                   if unicodedata.category(c)[0] in "LN")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ReviewError("invalid_word_timing")
    return float(value)


def validate_response(value, duration):
    if not isinstance(value, dict):
        raise ReviewError("invalid_response")
    if value.get("model") != MODEL or value.get("aligner") != ALIGNER:
        raise ReviewError("unexpected_model")
    if value.get("timing_basis") != "forced_alignment":
        raise ReviewError("missing_forced_alignment")
    reported_duration = _number(value.get("duration"))
    if not 0 < reported_duration <= MAX_SECONDS or abs(reported_duration - duration) > .25:
        raise ReviewError("audio_duration_mismatch")
    text, words = value.get("text"), value.get("words")
    if not isinstance(text, str) or len(text) > MAX_TEXT or not isinstance(words, list) or len(words) > MAX_WORDS:
        raise ReviewError("invalid_transcript")
    if not words:
        if _normalized(text):
            raise ReviewError("missing_word_alignment")
        return {"text": "", "words": [], "duration": reported_duration,
                "model": MODEL, "aligner": ALIGNER, "timing_basis": "forced_alignment"}
    clean, previous = [], 0.
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("text"), str):
            raise ReviewError("invalid_word_text")
        token = word["text"].strip()
        if not token or len(token) > 1000 or not _normalized(token):
            raise ReviewError("invalid_word_text")
        start, end = _number(word.get("start")), _number(word.get("end"))
        if start < previous or not 0 <= start <= end <= min(duration, reported_duration):
            raise ReviewError("invalid_word_timing")
        clean.append({"start": start, "end": end, "text": token})
        previous = start
    if not any(word["end"] > word["start"] for word in clean):
        raise ReviewError("zero_duration_alignment")
    if _normalized("".join(word["text"] for word in clean)) != _normalized(text):
        raise ReviewError("word_text_mismatch")
    return {"text": text, "words": clean, "duration": reported_duration,
            "model": MODEL, "aligner": ALIGNER, "timing_basis": "forced_alignment"}


def _punctuated_words(result):
    """Restore ASR punctuation around aligned words without inventing timestamps."""
    text = result["text"]
    positions = []
    for index, char in enumerate(text):
        positions.extend([index] * len(_normalized(char)))
    cursor, output = 0, []
    for index, word in enumerate(result["words"]):
        next_cursor = cursor + len(_normalized(word["text"]))
        start = positions[cursor] if index else 0
        end = positions[next_cursor] if next_cursor < len(positions) else len(text)
        output.append({**word, "text": text[start:end]})
        cursor = next_cursor
    return output


def caption_events(result, anchor_start, line_limit):
    """Cue boundaries always come from first/last aligned words, never ratios."""
    # Forced-aligner quantization can produce zero-duration and overlapping
    # tokens. Keep those inseparable neighbors in a single true time interval.
    units = []
    for word in _punctuated_words(result):
        if units and (word["start"] < units[-1]["end"] or word["start"] == word["end"]
                      or units[-1]["start"] == units[-1]["end"]):
            units[-1]["end"] = max(units[-1]["end"], word["end"])
            units[-1]["text"] += word["text"]
        else:
            units.append(dict(word))
    groups, current = [], []
    for word in units:
        if current and (word["start"] - current[-1]["end"] >= .45
                        or len("".join(w["text"] for w in current)) + len(word["text"]) > line_limit
                        or word["end"] - current[0]["start"] > 6):
            groups.append(current)
            current = []
        current.append(word)
        if re.search(r"[，。！？；：,.!?;:][\s\"'”’）)]*$", word["text"]):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return [{"id": f"subtitle_review_{index:05}", "kind": "transcript",
             "start": anchor_start + words[0]["start"], "end": anchor_start + words[-1]["end"],
             "text": "".join(word["text"] for word in words).strip(),
             "data": {"timing_basis": "forced_alignment", "speaker": "unknown", "model": MODEL}}
            for index, words in enumerate(groups)]


def _comparison(events, anchor):
    rows, remaining = [], MAX_TEXT
    for event in events:
        if (event.get("kind") not in {"transcript", "audio_segment"}
                or event.get("start", -1) < anchor["start"] - .08
                or event.get("end", math.inf) > anchor["end"] + .08):
            continue
        text = str(event.get("text") or "")
        if len(text) > remaining or len(rows) >= MAX_WORDS:
            return rows, True
        remaining -= len(text)
        rows.append({"start": max(0, event["start"] - anchor["start"]),
                     "end": min(anchor["end"] - anchor["start"], event["end"] - anchor["start"]),
                     "text": text})
    return rows, False


def _read_wav(path, expected_duration):
    if path.stat().st_size > MAX_WAV_BYTES:
        raise ReviewError("audio_too_large")
    try:
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, 16000, 2, "NONE"):
                raise ReviewError("invalid_audio_format")
            frames = wav.getnframes()
            duration = frames / 16000
            if not 0 < duration <= MAX_SECONDS or abs(duration - expected_duration) > .25:
                raise ReviewError("audio_duration_mismatch")
            pcm = wav.readframes(frames)
            if len(pcm) != frames * 2:
                raise ReviewError("truncated_audio")
    except (wave.Error, EOFError):
        raise ReviewError("invalid_audio_format") from None
    return duration, not any(pcm)


async def request_review(client, config, wav_bytes, context, duration, *, sleep=asyncio.sleep):
    payload = {"audio_base64": base64.b64encode(wav_bytes).decode("ascii"),
               "context": context, "language": "Chinese"}
    headers = {"Authorization": "Bearer " + config["api_key"]}
    endpoint = config["base_url"].rstrip("/") + "/transcribe_clip"
    timeout = min(1200, max(180, duration * 2))
    async def request_with_retries():
        for attempt in range(3):
            async with client.stream("POST", endpoint, headers=headers, json=payload,
                                     timeout=httpx.Timeout(timeout, connect=15, write=60, pool=15)) as response:
                if response.status_code == 429 and attempt < 2:
                    try:
                        delay = min(10, max(.25, float(response.headers.get("Retry-After", 2 ** attempt))))
                    except ValueError:
                        delay = 2 ** attempt
                elif response.status_code != 200:
                    raise ReviewError("http_" + str(response.status_code))
                else:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ReviewError("response_too_large")
                        body.extend(chunk)
                    try:
                        value = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        raise ReviewError("invalid_json") from None
                    return validate_response(value, duration)
            await sleep(delay)

    try:
        return await asyncio.wait_for(request_with_retries(), timeout=timeout + 45)
    except (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError):
        raise ReviewError("request_timeout") from None
    except httpx.HTTPError:
        raise ReviewError("request_failed") from None


async def review_subtitles(settings, folder, events, anchor, width, height, *,
                           offset, has_audio, source, enabled, command, client=None, sleep=asyncio.sleep):
    """Return isolated caption events plus an auditable report, or explicit fallback."""
    original, truncated = _comparison(events, anchor)
    report = {"version": REVIEW_VERSION, "status": "disabled", "applied": False,
              "model": MODEL, "aligner": ALIGNER, "timing_basis": "original_chunk_timing",
              "source_clock": "clip_relative_seconds", "original_subtitles": original,
              "original_comparison_truncated": truncated, "reviewed_subtitles": [],
              "cache_hit": False, "speaker_attribution": "unknown", "warning": None}
    output = events
    folder = Path(folder)
    try:
        if not enabled:
            return output, report
        if not has_audio:
            report["status"] = "no_audio"
            return output, report
        from .config import session_model_profile
        if session_model_profile(source) != "default":
            settings = settings.for_session(source)
        config = settings.audio_config()
        report["model_profile"] = getattr(settings, "model_profile", "default")
        parsed = urlsplit(config.get("base_url", ""))
        if (config.get("error") or parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment or not config.get("api_key")):
            report.update(status="not_configured", warning="成片字幕未复听：未配置可用音频服务，已保留原字幕。")
            return output, report
        duration = anchor["end"] - anchor["start"]
        if not 0 < duration <= MAX_SECONDS:
            raise ReviewError("invalid_clip_duration")
        wav = folder / "review.wav"
        await command([settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "concat", "-safe", "0", "-i", folder / "input.ffconcat",
                       "-map", "0:a:0", "-vn", "-af",
                       f"atrim=start={offset:.6f}:duration={duration:.6f},asetpts=PTS-STARTPTS",
                       "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                       "-t", f"{duration:.6f}", "-fs", str(MAX_WAV_BYTES), wav],
                      timeout=min(600, max(90, duration * 2)))
        audio_duration, silent = await asyncio.to_thread(_read_wav, wav, duration)
        wav_bytes = await asyncio.to_thread(wav.read_bytes)
        audio_hash = hashlib.sha256(wav_bytes).hexdigest()
        context = identity_context(source)
        report.update(audio_sha256=audio_hash, duration=audio_duration, context=context)
        if silent:
            report.update(status="silent_audio", applied=True, timing_basis="digital_silence")
            return [], report
        key = hashlib.sha256(json.dumps({"audio_sha256": audio_hash, "context": context,
                                       "model_profile": report["model_profile"],
                                       "model": MODEL, "aligner": ALIGNER, "version": REVIEW_VERSION},
                                      ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cache = folder / "subtitle-review-cache.json"
        result = None
        if cache.is_file() and cache.stat().st_size <= MAX_RESPONSE_BYTES:
            try:
                cached = json.loads(cache.read_text())
                if cached.get("key") == key:
                    result = validate_response(cached.get("result"), audio_duration)
                    report["cache_hit"] = True
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if result is None:
            if client is None:
                async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                             limits=httpx.Limits(max_connections=1)) as owned:
                    result = await request_review(owned, config, wav_bytes, context, audio_duration, sleep=sleep)
            else:
                result = await request_review(client, config, wav_bytes, context, audio_duration, sleep=sleep)
            # Cache only validated output; the cache contains no credentials or endpoints.
            temporary = cache.with_suffix(".json.tmp")
            temporary.write_text(json.dumps({"key": key, "result": result}, ensure_ascii=False))
            temporary.replace(cache)
        if not result["words"]:
            report.update(status="empty_result", warning="成片复听未返回可核验语音，已保留原字幕。")
            return output, report
        output = caption_events(result, anchor["start"], 24 if width > height else 13)
        reviewed, _ = _comparison(output, anchor)
        report.update(status="complete", applied=True, timing_basis="forced_alignment",
                      text=result["text"], words=result["words"], reviewed_subtitles=reviewed)
        return output, report
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ReviewError) else "local_review_failed"
        report.update(status="fallback", reason=reason,
                      warning=f"成片字幕复听未完成（{reason}），已回退原字幕。")
        return events, report
    finally:
        (folder / "subtitle-review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
