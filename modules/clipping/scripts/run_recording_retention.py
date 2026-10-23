#!/usr/bin/env python3
"""Plan or release stopped live recording buffers; never removes exports."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipper.config import Settings, local_workbench_base_url
from clipper.recording_retention import VERSION, plan, policy, purge_session


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply", "run"])
    parser.add_argument("--data-dir", type=Path, help="Default: configured clipper data directory")
    parser.add_argument("--base-url", help="Default: configured clipper listener")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--grace-seconds", type=float, default=120)
    parser.add_argument("--discard-terminal-analysis-failures", action="store_true", default=None,
                        help="plan only: preview explicit policy to abandon terminated analysis retries")
    args = parser.parse_args(argv)
    if args.grace_seconds < 0 or args.interval < 10:
        parser.error("grace-seconds must be nonnegative; interval must be at least 10")
    if args.command != "plan" and args.discard_terminal_analysis_failures:
        parser.error("For apply/run set discard_terminal_analysis_failures explicitly in policy.json")
    if args.data_dir is None:
        args.data_dir = Settings.from_env().data_dir
    if args.base_url is None:
        args.base_url = local_workbench_base_url()
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.command == "plan":
        print(json.dumps(plan(args.data_dir, grace_seconds=args.grace_seconds,
            discard_terminal_analysis_failures=args.discard_terminal_analysis_failures), ensure_ascii=False, indent=2))
        return
    directory = args.data_dir / "recording-retention"
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / "worker.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    with httpx.Client(trust_env=False, timeout=15) as client:
        while not stopping.is_set():
            result = {"version": VERSION, "pid": os.getpid(), "checked_at": time.time(),
                      "deleted_bytes": 0, "deleted_files": 0, "sessions": []}
            try:
                if policy(args.data_dir).get("enabled") is not True:
                    result["status"] = "disabled"
                else:
                    # The live service must expose the atomic source-admission
                    # guard before an independent worker may release its files.
                    health = client.get(args.base_url.rstrip("/") + "/api/health")
                    health.raise_for_status()
                    if health.json().get("recording_retention_version") != VERSION:
                        raise RuntimeError("workbench_retention_guard_not_loaded")
                    current = plan(args.data_dir, grace_seconds=args.grace_seconds)
                    result["held_reasons"] = current["held_reasons"]
                    for row in current["eligible"]:
                        if stopping.is_set():
                            break
                        deleted = purge_session(args.data_dir, row["session_id"], grace_seconds=args.grace_seconds)
                        result["sessions"].append(deleted)
                        result["deleted_bytes"] += deleted["bytes"]
                        result["deleted_files"] += deleted["files"]
                    result["status"] = "complete"
            except Exception as exc:
                result.update(status="error", error=str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
            temp = directory / "status.json.tmp"
            temp.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            temp.replace(directory / "status.json")
            with (directory / "events.jsonl").open("a") as log:
                log.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.command == "apply":
                if result["status"] == "error":
                    raise SystemExit(1)
                return
            stopping.wait(args.interval)


if __name__ == "__main__":
    main()
