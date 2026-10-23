#!/usr/bin/env python3
"""Refresh the local clip gallery served by the configured workbench."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipper.library import refresh_library
from clipper.library_manifest import atomic_json, read_json
from clipper.config import Settings, local_workbench_base_url


def health(run_dir):
    state = read_json(run_dir / "status.json", {}) or {}
    running = False
    pid = state.get("pid")
    if isinstance(pid, int) and pid > 0:
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            running = str(Path(__file__).resolve()).encode() in argv or b"scripts/run_local_library.py" in argv
        except OSError:
            pass
    return {**state, "process_running": running}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["once", "run", "health", "stop"])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "static/clips/library.json")
    parser.add_argument("--base-url")
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args(argv)
    if args.interval < 1:
        parser.error("interval must be at least 1 second")
    if args.data_dir is None:
        args.data_dir = Settings.from_env().data_dir
    if args.base_url is None:
        args.base_url = local_workbench_base_url()
    return args


def main():
    args = parse_args()
    run_dir = args.data_dir / "local-library"
    if args.command == "health":
        print(json.dumps(health(run_dir), ensure_ascii=False))
        return 0
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "stop":
        (run_dir / "STOP").touch(mode=0o600)
        print("已请求停止本地切片列表更新，录制与轮巡继续运行。")
        return 0
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    with (run_dir / "refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("本地切片列表更新服务已运行。")
            return 1
        (run_dir / "STOP").unlink(missing_ok=True)
        state = {"status": "starting", "pid": os.getpid(), "started_at": time.time()}
        failed = False
        try:
            while not stopping.is_set() and not (run_dir / "STOP").exists():
                try:
                    manifest = asyncio.run(refresh_library(args.data_dir, args.output, args.base_url))
                    state.update(status="ready", clip_count=len(manifest["clips"]),
                                 last_success_at=time.time(), error=None)
                    failed = False
                except Exception:
                    # Never write raw upstream responses, credentials, or private paths to the UI/log.
                    state.update(status="retrying", error="local_library_refresh_failed")
                    failed = True
                state["updated_at"] = time.time()
                atomic_json(run_dir / "status.json", state)
                print(json.dumps({key: state.get(key) for key in ("status", "clip_count", "error")}), flush=True)
                if args.command == "once":
                    break
                deadline = time.monotonic() + args.interval
                while time.monotonic() < deadline and not (run_dir / "STOP").exists():
                    if stopping.wait(min(1, max(0, deadline - time.monotonic()))):
                        break
        finally:
            state.update(status="stopped", pid=None, updated_at=time.time())
            atomic_json(run_dir / "status.json", state)
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
