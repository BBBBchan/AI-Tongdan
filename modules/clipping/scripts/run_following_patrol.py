#!/usr/bin/env python3
"""Run, inspect, or stop patrol; durable workbench policy controls publishing."""
import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clipper.config import Settings, local_workbench_base_url
from clipper.patrol import FollowingPatrol, PatrolConfig, health


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["run", "health", "stop", "inspect-following"])
    p.add_argument("--run-dir", type=Path, help="Default: configured clipper data directory / following-patrol")
    p.add_argument("--base-url", help="Default: configured clipper listener")
    p.add_argument("--dwell", type=float, help="固定停留秒数；指定后覆盖随机区间，另保留尾部")
    p.add_argument("--dwell-min", type=float, default=2700, help="每站最短秒数，含尾部，默认 2700")
    p.add_argument("--dwell-max", type=float, default=3720, help="每站最长秒数，含尾部，默认 3720")
    p.add_argument("--tail", type=float, default=25)
    p.add_argument("--poll", type=float, default=10)
    p.add_argument("--refresh", type=float, default=300)
    p.add_argument("--room-status", type=float, default=30, help="直播状态检查间隔秒数")
    p.add_argument("--minimum-score", type=int, default=88)
    p.add_argument("--max-recording-gib", type=float, default=20)
    p.add_argument("--minimum-free-gib", type=float, default=5)
    p.add_argument("--first-room", type=int, help="仅首站优先；必须仍在当前直播关注列表内")
    p.add_argument("--next-room", type=int, help="恢复时仅下一站优先，并解除该房冷却；保留历史")
    p.add_argument("--until-offline-room", type=int, help="指定这场直播看到确认下播，再恢复随机轮巡；重启保留一次性状态")
    return p


def parse_args(argv=None):
    args = parser().parse_args(argv)
    if args.run_dir is None:
        args.run_dir = Settings.from_env().data_dir / "following-patrol"
    if args.base_url is None:
        args.base_url = local_workbench_base_url()
    return args


async def main(args):
    if args.command == "health":
        print(json.dumps(health(args.run_dir), ensure_ascii=False, indent=2))
        return
    if args.command == "stop":
        args.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (args.run_dir / "STOP").touch(mode=0o600)
        print("已请求轮巡停止，现有素材和导出保留。")
        return
    cfg = PatrolConfig(base_url=args.base_url, dwell_seconds=args.dwell,
                       dwell_min_seconds=args.dwell_min, dwell_max_seconds=args.dwell_max,
                       tail_seconds=args.tail, poll_seconds=args.poll,
                       refresh_seconds=args.refresh, room_status_seconds=args.room_status,
                       minimum_score=args.minimum_score,
                       max_recording_gib=args.max_recording_gib,
                       minimum_free_gib=args.minimum_free_gib, first_room=args.first_room)
    cfg.next_room = args.next_room
    cfg.until_offline_room = args.until_offline_room
    patrol = FollowingPatrol(Settings.from_env(), args.run_dir, cfg)
    if args.command == "inspect-following":
        try:
            rooms = await patrol.follows(persist=False)
            print(json.dumps({"account_name": patrol.state["account_name"],
                              "live_following_count": len(rooms), "rooms": rooms},
                             ensure_ascii=False, indent=2))
        finally:
            await patrol.client.aclose()
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, patrol.stopping.set)
    await patrol.run()


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except (RuntimeError, ValueError) as exc:
        # These exceptions are deliberately fixed local messages.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
