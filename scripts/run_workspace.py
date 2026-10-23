#!/usr/bin/env python3
"""Run the companion and clipping tools as one supervised local workspace."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Service:
    name: str
    command: tuple[str, ...]
    cwd: Path
    health_url: str | None = None


def _port(value):
    value = int(value)
    if not 1 <= value <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    return value


def _loopback_url(host, port):
    hostname = "127.0.0.1" if host in {"0.0.0.0", "localhost", ""} else "::1" if host == "::" else host
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"http://{hostname}:{port}"


def configuration(args, root=ROOT, environment=None):
    """Resolve launcher settings without exporting model keys from .env files."""
    environment = dict(os.environ if environment is None else environment)
    module = root / "modules/clipping"
    common = dotenv_values(root / ".env")
    clipping = dotenv_values(module / ".env") if not args.companion_only else {}

    def setting(name, default, *, clip=False):
        sources = [(environment, root)]
        if clip:
            sources.append((clipping, module))
        sources.append((common, root))
        for values, base in sources:
            if values.get(name) not in (None, ""):
                return str(values[name]), base
        return default, root

    host = args.host or setting("HOST", "127.0.0.1")[0]
    port = _port(args.port if args.port is not None else setting("PORT", "7862")[0])
    clip_host = args.clipper_host or setting("CLIPPER_HOST", host, clip=True)[0]
    clip_port = _port(args.clipper_port if args.clipper_port is not None else setting("CLIPPER_PORT", "6880", clip=True)[0])
    if not args.companion_only and port == clip_port:
        raise ValueError("伴看与切片必须使用不同端口")
    value, base = setting("CLIPPER_DATA_DIR", str(module / "data"), clip=True)
    clip_data = Path(value).expanduser()
    if not clip_data.is_absolute():
        clip_data = base / clip_data
    companion_value, companion_base = setting("DATA_DIR", str(root / "data"))
    companion_data = Path(companion_value).expanduser()
    if not companion_data.is_absolute():
        companion_data = companion_base / companion_data
    if not args.companion_only and clip_data.resolve() == companion_data.resolve():
        raise ValueError("切片与伴看必须使用不同的数据目录")
    if args.patrol and args.companion_only:
        raise ValueError("--patrol 需要启用切片服务")
    public_url = setting("CLIPPER_PUBLIC_URL", "", clip=True)[0]
    env = {**environment, "HOST": host, "PORT": str(port),
           "CLIPPER_ENABLED": "0" if args.companion_only else "1",
           "CLIPPER_HOST": clip_host, "CLIPPER_PORT": str(clip_port),
           "CLIPPER_PUBLIC_URL": public_url,
           "CLIPPER_DATA_DIR": str(clip_data.resolve())}
    return {"root": root, "module": module, "host": host, "port": port,
            "clip_host": clip_host, "clip_port": clip_port, "clip_data": clip_data.resolve(),
            "companion_only": args.companion_only, "patrol": args.patrol, "environment": env}


def service_plan(config, python=sys.executable):
    root, module = config["root"], config["module"]
    result = [Service("companion", (python, "-m", "uvicorn", "app.server:app", "--host", config["host"],
                "--port", str(config["port"]), "--workers", "1", "--timeout-graceful-shutdown", "120"),
                root, _loopback_url(config["host"], config["port"]) + "/api/health")]
    if config["companion_only"]:
        return result
    base = _loopback_url(config["clip_host"], config["clip_port"])
    data = str(config["clip_data"])
    result.extend([
        Service("clipping", (python, "-m", "clipper"), module, base + "/api/health"),
        Service("clip-library", (python, "-u", "scripts/run_local_library.py", "run", "--base-url", base,
                "--data-dir", data), module),
        Service("recording-retention", (python, "-u", "scripts/run_recording_retention.py", "run",
                "--base-url", base, "--data-dir", data), module),
    ])
    if config["patrol"]:
        result.append(Service("clipping-patrol", (python, "-u", "scripts/run_following_patrol.py", "run",
                      "--base-url", base, "--run-dir", str(config["clip_data"] / "following-patrol"),
                      "--dwell-min", "2700", "--dwell-max", "3720", "--minimum-score", "88"), module))
    return result


def check_listeners(config):
    endpoints = [(config["host"], config["port"])]
    if not config["companion_only"]:
        endpoints.append((config["clip_host"], config["clip_port"]))
    for host, port in endpoints:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            try:
                listener.bind((host, port))
            except OSError as exc:
                raise ValueError(f"监听地址 {host}:{port} 不可用；请更换端口或停止原服务") from exc


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def stop_process(process, timeout):
    # First let the service drain its own capture/tasks. Kill the process group
    # only if it outlives the grace period, and reap any orphan descendants.
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
                process.wait(timeout=5)
    _signal_group(process, signal.SIGKILL)


def drain_clipping(service, process, timeout):
    """Finish capture tails and in-flight exports before stopping our own API."""
    if not service.health_url or process.poll() is not None:
        return
    base = service.health_url.removesuffix("/api/health")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout

    def request(path, *, stop=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        query = urllib.request.Request(base + path, data=b"{}" if stop else None,
                    headers={"Content-Type": "application/json"} if stop else {})
        with opener.open(query, timeout=min(remaining, 5)) as response:
            return json.load(response)

    active = {"live", "connecting", "reconnecting", "waiting", "recording"}
    try:
        snapshot = request("/api/state")
        for session in snapshot.get("sessions", []):
            if session.get("status") in active:
                try:
                    request(f"/api/sessions/{session['id']}/stop", stop=True)
                except (OSError, ValueError, urllib.error.HTTPError):
                    # The stop operation can outlast one HTTP request. Poll its
                    # durable state rather than invoking the stop a second time.
                    pass
        while process.poll() is None and time.monotonic() < deadline:
            snapshot = request("/api/state")
            busy = (any(s.get("status") in active or s.get("stop_phase") in {"draining", "analyzing"}
                        or s.get("analysis_source_reserved") is True
                        or s.get("analysis_status") in {"running", "transcribing", "pending", "analyzing",
                                                         "分析中", "正在提取画面与语音"}
                        for s in snapshot.get("sessions", []))
                    or any(j.get("status") in {"queued", "rendering"}
                           or (j.get("live_publication_review") or {}).get("status") in {"reviewing", "queued"}
                           for j in snapshot.get("jobs", []))
                    or any(p.get("status") in {"queued", "uploading", "submitting"}
                           for p in snapshot.get("publications", [])))
            if not busy:
                return
            time.sleep(.2)
    except (OSError, ValueError, urllib.error.HTTPError):
        pass
    print("切片收尾达到等待上限，将停止服务并保留任务恢复记录。", file=sys.stderr, flush=True)


def run_services(services, environment, log_dir, stopping, *, startup_timeout=60, shutdown_timeout=120):
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    processes = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    status_path = log_dir / "status.json"

    def save(status):
        payload = {"status": status, "pid": os.getpid(), "updated_at": time.time(),
                   "services": [{"name": spec.name, "pid": child.pid, "exit_code": child.poll()}
                                for spec, child in processes]}
        temporary = status_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        temporary.replace(status_path)

    def check_children():
        for spec, child in processes:
            if child.poll() is not None:
                raise RuntimeError(f"{spec.name} 已退出，请查看 {log_dir / (spec.name + '.log')}")

    outcome = 0
    try:
        for spec in services:
            if stopping.is_set():
                break
            check_children()
            descriptor = os.open(log_dir / (spec.name + ".log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "ab") as log:
                child = subprocess.Popen(spec.command, cwd=spec.cwd, env=environment,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append((spec, child))
            save("starting")
            deadline = time.monotonic() + startup_timeout
            while spec.health_url and not stopping.is_set():
                check_children()
                try:
                    with opener.open(spec.health_url, timeout=2) as response:
                        if response.status == 200 and json.load(response).get("ok") is True:
                            break
                except (OSError, ValueError, urllib.error.HTTPError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{spec.name} 启动超时，请查看 {log_dir / (spec.name + '.log')}")
                stopping.wait(.2)
        if not stopping.is_set():
            save("running")
            print("AI同担工作区已启动；按 Ctrl+C 停止。日志：" + str(log_dir), flush=True)
        while not stopping.wait(.2):
            check_children()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        outcome = 1
    finally:
        try:
            save("stopping")
        except OSError:
            outcome = 1
        # Patrol finishes room-tail analysis while its API still exists.
        for spec, child in reversed(processes):
            try:
                if outcome == 0 and spec.name == "clipping":
                    drain_clipping(spec, child, shutdown_timeout)
                stop_process(child, shutdown_timeout)
            except (OSError, subprocess.TimeoutExpired):
                outcome = 1
                print(f"{spec.name} 结束超时，已请求终止其进程组。", file=sys.stderr)
        try:
            save("failed" if outcome else "stopped")
        except OSError:
            outcome = 1
            print("无法写入工作区状态；子进程已停止。", file=sys.stderr)
    return outcome


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--companion-only", action="store_true", help="只启动原直播陪伴网页")
    result.add_argument("--patrol", action="store_true", help="额外开启关注列表切片轮巡；不会开启自动投稿")
    result.add_argument("--host")
    result.add_argument("--port", type=int)
    result.add_argument("--clipper-host")
    result.add_argument("--clipper-port", type=int)
    return result


def main():
    args = parser().parse_args()
    try:
        config = configuration(args)
        check_listeners(config)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    return run_services(service_plan(config), config["environment"], ROOT / "data/workspace", stopping)


if __name__ == "__main__":
    raise SystemExit(main())
