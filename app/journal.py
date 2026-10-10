"""Rotating local records of observations and question workflow events."""
import json
from pathlib import Path
import time


class SessionJournal:
    def __init__(self, directory: Path, max_bytes=10 * 1024 * 1024, backups=4):
        self.path = directory / "events.jsonl"
        self.max_bytes = max_bytes
        self.backups = backups

    def append(self, kind, data):
        line = json.dumps({"time": time.time(), "type": kind, "data": data}, ensure_ascii=False) + "\n"
        if self.path.exists() and self.path.stat().st_size + len(line.encode()) > self.max_bytes:
            for index in range(self.backups, 0, -1):
                previous = self.path if index == 1 else self.path.with_suffix(f".jsonl.{index - 1}")
                target = self.path.with_suffix(f".jsonl.{index}")
                if previous.exists():
                    previous.replace(target)
        with self.path.open("a", encoding="utf-8") as output:
            output.write(line)
