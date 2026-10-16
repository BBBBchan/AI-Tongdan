#!/usr/bin/env python3
"""Export local patrol media only, without network access or session mutation.

Each visit gets an archived thumbnail, at most a 30-second MP4 preview, and
hardlinks to explicitly closed MKV parts. No credentials, event logs, live
recordings, directory symlinks, or copies of large original recordings are
exported. Relative recording times are not precise live wall-clock times.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


VISIT_ID = re.compile(r"\d{3,}-\d+")
RECORDING = re.compile(r"recordings/(part-\d{3,}\.mkv)")
FRAME = re.compile(r"evidence/frames/[0-9a-f]{32}\.jpg")
PREVIEW_SECONDS = 30


def number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def load_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".media-manifest-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def source_file(visit_dir, relative, pattern):
    """Allow only existing regular files in this visit's explicit media paths."""
    if not isinstance(relative, str) or not pattern.fullmatch(relative):
        return None
    source = visit_dir / relative
    if source.is_symlink() or not source.is_file():
        return None
    try:
        source.resolve().relative_to(visit_dir.resolve())
    except ValueError:
        return None
    # Do not accept a media directory that redirects to a different location.
    if any(parent.is_symlink() for parent in source.parents if parent != visit_dir and visit_dir in parent.parents):
        return None
    return source


def target_directory(output, visit_id):
    if not VISIT_ID.fullmatch(visit_id):
        raise ValueError("Invalid visit identifier")
    media = output / "media"
    directory = media / visit_id
    if media.is_symlink() or directory.is_symlink():
        raise ValueError("Media output directories must not be symlinks")
    directory.mkdir(parents=True, exist_ok=True)
    directory.resolve().relative_to(output.resolve())
    return directory


def archived_thumbnail(visit_dir):
    index = visit_dir / "frames.jsonl"
    if index.is_file() and not index.is_symlink():
        with index.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                source = source_file(visit_dir, row.get("file"), FRAME)
                if source:
                    return source
    for source in sorted((visit_dir / "evidence/frames").glob("*.jpg")):
        valid = source_file(visit_dir, source.relative_to(visit_dir).as_posix(), FRAME)
        if valid:
            return valid
    return None


def closed_recordings(visit_dir, visit):
    result = []
    for recording in visit.get("recordings", []):
        if not isinstance(recording, dict):
            continue
        start, end = number(recording.get("started_at")), number(recording.get("ended_at"))
        # An in-progress process has no ended_at/exit_code. Never export it.
        if start is None or end is None or end < start or type(recording.get("exit_code")) is not int:
            continue
        source = source_file(visit_dir, recording.get("file"), RECORDING)
        if source and source.stat().st_size:
            result.append((recording, source))
    return result


def hardlink_recording(source, target):
    """Never fall back to copying an original or following a destination link."""
    if target.is_symlink():
        raise ValueError("Refusing a symlink at an original-recording destination")
    if target.exists():
        if not os.path.samefile(source, target):
            raise ValueError("An unrelated file already occupies the recording destination")
        return
    os.link(source, target, follow_symlinks=False)


def tool_path(name):
    variable = f"{name.upper()}_BIN"
    configured = os.environ.get(variable, "").strip()
    if configured:
        value = shutil.which(str(Path(configured).expanduser()))
        if value:
            return value
    value = shutil.which(name)
    if value:
        return value
    raise FileNotFoundError(f"Required media tool is unavailable: {name}; install it on PATH or set {variable} to an executable")


def probe(path, executable):
    completed = subprocess.run(
        [executable, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=30,
    )
    if completed.returncode:
        raise RuntimeError("ffprobe could not read the media container")
    value = json.loads(completed.stdout)
    duration = float(value.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Media duration is unavailable")
    if not any(stream.get("codec_type") == "video" for stream in value.get("streams", [])):
        raise ValueError("Media has no video stream")
    return duration


def create_preview(source, target, ffmpeg, ffprobe):
    fd, temporary = tempfile.mkstemp(prefix=".preview-", suffix=".mp4", dir=target.parent)
    os.close(fd)
    try:
        # Start at recording-relative zero. Stream copying after a seek could
        # move the first displayed frame to an earlier keyframe, so do not seek.
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
             "-t", str(PREVIEW_SECONDS), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "128k", "-threads", "1", "-avoid_negative_ts", "make_zero",
             "-movflags", "+faststart", temporary],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180,
        )
        if completed.returncode:
            raise RuntimeError("ffmpeg could not create the preview")
        duration = probe(Path(temporary), ffprobe)
        os.replace(temporary, target)
        return duration
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_visit(run_dir, output, visit_id, previous, ffmpeg, ffprobe, probe_raw=False):
    warnings = []
    result = {"thumbnail": None, "preview": None, "recordings": []}
    directory = target_directory(output, visit_id)
    visit_dir = run_dir / "visits" / visit_id
    if (run_dir / "visits").is_symlink() or visit_dir.is_symlink():
        raise ValueError("Visit directory must not be a symlink")
    visit_dir.resolve().relative_to(run_dir.resolve())
    if (visit_dir / "visit.json").is_symlink():
        raise ValueError("Visit metadata must not be a symlink")
    visit = load_json(visit_dir / "visit.json")
    source = archived_thumbnail(visit_dir)
    if source:
        target = directory / "thumbnail.jpg"
        if target.is_symlink():
            raise ValueError("Thumbnail destination must not be a symlink")
        # Archived frames are independent of the rolling live frame cache.
        shutil.copyfile(source, target)
        result["thumbnail"] = f"media/{visit_id}/thumbnail.jpg"
    closed = closed_recordings(visit_dir, visit)
    for recording, source in closed:
        try:
            hardlink_recording(source, directory / source.name)
            result["recordings"].append({
                "name": source.name, "url": f"media/{visit_id}/{source.name}",
                "bytes": source.stat().st_size, "duration_seconds": None,
                "started_at": recording["started_at"], "ended_at": recording["ended_at"],
            })
        except (OSError, ValueError) as exc:
            warnings.append(f"{visit_id}/{source.name}: original unavailable ({type(exc).__name__}); no copy fallback")
    if closed:
        # Select one real closed part; longest recorded process interval is only
        # a selection heuristic, never published as the media duration.
        _, source = max(closed, key=lambda item: item[0]["ended_at"] - item[0]["started_at"])
        prior_preview = previous.get("preview") or {}
        prior_part = next((r for r in previous.get("recordings", []) if r.get("name") == source.name), {})
        target = directory / "preview.mp4"
        try:
            if target.is_symlink():
                raise ValueError("Preview destination must not be a symlink")
            if (target.is_file() and prior_preview.get("source_file") == source.name
                    and prior_preview.get("start_seconds") == 0 and prior_part.get("bytes") == source.stat().st_size):
                duration = probe(target, ffprobe)
            else:
                duration = create_preview(source, target, ffmpeg, ffprobe)
            result["preview"] = {"url": f"media/{visit_id}/preview.mp4", "source_file": source.name,
                "start_seconds": 0, "duration_seconds": duration, "timestamp_basis": "recording_relative_seconds"}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            warnings.append(f"{visit_id}: preview unavailable ({type(exc).__name__}); original links retained")
    if probe_raw:
        sources = {source.name: source for _, source in closed}
        for recording in result["recordings"]:
            try:
                recording["duration_seconds"] = probe(sources[recording["name"]], ffprobe)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                warnings.append(f"{visit_id}/{recording['name']}: original duration unavailable ({type(exc).__name__})")
    return result, warnings


def build(run_dir, output, *, workers=2, probe_raw_visits=()):
    run_dir, output = Path(run_dir).resolve(), Path(output).absolute()
    if output.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    report = load_json(run_dir / "report.json")
    visit_ids = list(dict.fromkeys(v["visit_id"] for v in report.get("visits", [])
                                  if isinstance(v, dict) and VISIT_ID.fullmatch(v.get("visit_id", ""))))
    manifest_path = output / "media-manifest.json"
    prior_manifest = load_json(manifest_path) if manifest_path.exists() else {}
    if prior_manifest and prior_manifest.get("run_id") != run_dir.name:
        raise ValueError("Output already belongs to a different patrol run")
    previous = prior_manifest.get("visits", {})
    manifest = {"run_id": run_dir.name, "generated_at": time.time(), "visits": {}, "warnings": []}
    ffmpeg, ffprobe = tool_path("ffmpeg"), tool_path("ffprobe")
    with ThreadPoolExecutor(max_workers=min(2, max(1, workers))) as pool:
        pending = {pool.submit(export_visit, run_dir, output, vid, previous.get(vid, {}), ffmpeg, ffprobe,
                               vid in probe_raw_visits): vid for vid in visit_ids}
        for future in as_completed(pending):
            vid = pending[future]
            try:
                value, warnings = future.result()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                value = {"thumbnail": None, "preview": None, "recordings": []}
                warnings = [f"{vid}: media export unavailable ({type(exc).__name__})"]
            manifest["visits"][vid] = value
            manifest["warnings"].extend(warnings)
            print(json.dumps({"visit_id": vid, "thumbnail": bool(value["thumbnail"]),
                              "preview": bool(value["preview"]), "closed_originals": len(value["recordings"]),
                              "warnings": len(warnings)}, ensure_ascii=False), flush=True)
    manifest["visits"] = {vid: manifest["visits"][vid] for vid in visit_ids}
    manifest["generated_at"] = time.time()
    atomic_json(manifest_path, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description="生成仅限本机复盘的缩略图、短预览与已结束录像硬链接")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--probe-raw-visits", default="", help="逗号分隔的 visit_id；其他原片时长保持 null")
    args = parser.parse_args()
    value = build(args.run_dir, args.output, workers=args.workers,
                  probe_raw_visits=set(filter(None, args.probe_raw_visits.split(","))))
    print(json.dumps({"manifest": str(args.output / "media-manifest.json"), "visits": len(value["visits"]),
                      "previews": sum(bool(v["preview"]) for v in value["visits"].values()),
                      "warnings": len(value["warnings"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
