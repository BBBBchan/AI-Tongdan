import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import scripts.run_workspace as launcher
from scripts.run_workspace import configuration, parser, service_plan, check_listeners, run_services, Service


def config(tmp_path, *arguments, environment=None):
    return configuration(parser().parse_args(arguments), tmp_path, environment or {})


def test_default_suite_is_isolated_and_does_not_start_patrol(tmp_path):
    cfg = config(tmp_path)
    services = service_plan(cfg)
    assert [s.name for s in services] == ["companion", "clipping", "clip-library", "recording-retention"]
    assert cfg["clip_data"] == tmp_path / "modules/clipping/data"
    assert cfg["environment"]["CLIPPER_ENABLED"] == "1"
    assert all("--data-dir" in s.command and str(cfg["clip_data"]) in s.command for s in services[2:])
    assert all("publish" not in arg for service in services for arg in service.command)


def test_companion_only_has_no_clipping_dependency(tmp_path):
    cfg = config(tmp_path, "--companion-only", "--port", "8765")
    assert not cfg["module"].exists()
    services = service_plan(cfg)
    assert len(services) == 1 and services[0].name == "companion"
    assert services[0].health_url == "http://127.0.0.1:8765/api/health"
    assert cfg["environment"]["CLIPPER_ENABLED"] == "0"


def test_explicit_patrol_uses_same_data_and_api(tmp_path):
    cfg = config(tmp_path, "--patrol", "--clipper-port", "8766")
    services = service_plan(cfg)
    assert services[-1].name == "clipping-patrol"
    assert "http://127.0.0.1:8766" in services[-1].command
    assert str(cfg["clip_data"] / "following-patrol") in services[-1].command


def test_environment_overrides_files_without_exporting_model_secrets(tmp_path):
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=8765\nMODEL_API_KEY=private-example\nCLIPPER_DATA_DIR=data/clipping\n")
    module = tmp_path / "modules/clipping"
    module.mkdir(parents=True)
    (module / ".env").write_text("CLIPPER_PORT=8766\nCLIPPER_DATA_DIR=runtime\n")
    cfg = config(tmp_path, environment={"PORT": "8767", "CLIPPER_PUBLIC_URL": "https://clips.example.org:6880/"})
    assert cfg["host"] == "0.0.0.0" and cfg["port"] == 8767 and cfg["clip_port"] == 8766
    assert cfg["clip_data"] == module / "runtime"
    assert "MODEL_API_KEY" not in cfg["environment"]
    assert cfg["environment"]["CLIPPER_PUBLIC_URL"] == "https://clips.example.org:6880/"
    assert service_plan(cfg)[0].health_url == "http://127.0.0.1:8767/api/health"


@pytest.mark.parametrize("arguments", [("--port", "0"), ("--clipper-port", "65536"),
                                      ("--port", "6880"), ("--companion-only", "--patrol")])
def test_invalid_listener_or_mode_is_rejected(tmp_path, arguments):
    with pytest.raises(ValueError):
        config(tmp_path, *arguments)


def test_shared_companion_and_clipper_data_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="数据目录"):
        config(tmp_path, environment={"CLIPPER_DATA_DIR": "data"})


def test_existing_listener_is_not_adopted(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); sock.listen()
        cfg = config(tmp_path, "--companion-only", "--port", str(sock.getsockname()[1]))
        with pytest.raises(ValueError, match="不可用"):
            check_listeners(cfg)


def process_running(pid):
    path = Path(f"/proc/{pid}/stat")
    if path.exists():
        return path.read_text().split()[2] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(os.name != "posix", reason="The suite launcher targets Linux/macOS/WSL")
def test_child_failure_stops_healthy_sibling_and_its_descendants(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    grandchild = f"import os,time;from pathlib import Path;Path({str(pid_file)!r}).write_text(str(os.getpid()));time.sleep(30)"
    parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{grandchild!r}]);time.sleep(30)"
    services = [Service("healthy", (sys.executable, "-c", parent), tmp_path),
                Service("failed", (sys.executable, "-c", "import time;time.sleep(.5);raise SystemExit(3)"), tmp_path)]
    logs = tmp_path / "logs"
    assert run_services(services, dict(os.environ), logs, threading.Event(), shutdown_timeout=2) == 1
    assert pid_file.exists()
    descendant = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while process_running(descendant) and time.monotonic() < deadline:
        time.sleep(.01)
    assert not process_running(descendant)
    state = json.loads((logs / "status.json").read_text())
    assert state["status"] == "failed"
    assert all(not process_running(s["pid"]) for s in state["services"])


@pytest.mark.skipif(os.name != "posix", reason="The suite launcher targets Linux/macOS/WSL")
def test_spawn_failure_cleans_previously_started_service(tmp_path):
    services = [Service("first", (sys.executable, "-c", "import time;time.sleep(30)"), tmp_path),
                Service("missing", (str(tmp_path / "no-such-command"),), tmp_path)]
    logs = tmp_path / "logs"
    assert run_services(services, dict(os.environ), logs, threading.Event(), shutdown_timeout=2) == 1
    state = json.loads((logs / "status.json").read_text())
    assert len(state["services"]) == 1
    assert not process_running(state["services"][0]["pid"])


@pytest.mark.skipif(os.name != "posix", reason="The suite launcher targets Linux/macOS/WSL")
def test_normal_stop_reaps_all_children(tmp_path):
    stopped = threading.Event()
    timer = threading.Timer(.4, stopped.set)
    timer.start()
    try:
        services = [Service("service", (sys.executable, "-c", "import time;time.sleep(30)"), tmp_path)]
        logs = tmp_path / "logs"
        assert run_services(services, dict(os.environ), logs, stopped, shutdown_timeout=2) == 0
        state = json.loads((logs / "status.json").read_text())
        assert state["status"] == "stopped"
        assert not process_running(state["services"][0]["pid"])
    finally:
        timer.cancel()


def test_shutdown_waits_for_tail_analysis_and_exports_before_api_exit(tmp_path):
    calls = []
    draining_reads = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal draining_reads
            calls.append(("GET", self.path))
            if not any(method == "POST" for method, _ in calls):
                state = {"sessions": [{"id": "test-session", "status": "live"}]}
            elif draining_reads == 0:
                draining_reads += 1
                state = {"sessions": [{"id": "test-session", "status": "stopped", "stop_phase": "analyzing"}],
                         "jobs": [{"status": "rendering"}]}
            else:
                state = {"sessions": [{"id": "test-session", "status": "stopped", "stop_phase": "complete"}],
                         "jobs": [{"status": "ready"}]}
            self.respond(state)

        def do_POST(self):
            calls.append(("POST", self.path))
            self.respond({"status": "stopped"})

        def respond(self, state):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        service = Service("clipping", (), tmp_path, f"http://127.0.0.1:{server.server_port}/api/health")
        launcher.drain_clipping(service, SimpleNamespace(poll=lambda: None), timeout=2)
        assert calls.count(("POST", "/api/sessions/test-session/stop")) == 1
        assert calls.count(("GET", "/api/state")) == 3
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="The suite launcher targets Linux/macOS/WSL")
def test_status_file_failure_still_stops_started_children(tmp_path, monkeypatch):
    children = []
    real_popen = launcher.subprocess.Popen
    real_replace = Path.replace

    def launch(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    def fail_status_write(path, destination):
        if destination.name == "status.json":
            raise OSError("test status write failure")
        return real_replace(path, destination)

    monkeypatch.setattr(launcher.subprocess, "Popen", launch)
    monkeypatch.setattr(Path, "replace", fail_status_write)
    services = [Service("service", (sys.executable, "-c", "import time;time.sleep(30)"), tmp_path)]
    assert run_services(services, dict(os.environ), tmp_path / "logs", threading.Event(), shutdown_timeout=2) == 1
    assert children and all(child.poll() is not None for child in children)
