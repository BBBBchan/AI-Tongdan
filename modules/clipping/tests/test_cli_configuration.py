import asyncio
import json
import os
from pathlib import Path

import pytest

from clipper import config
from scripts import run_following_patrol as patrol
from scripts import run_recording_retention as retention


@pytest.fixture
def roots(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    module = repository / "modules/clipping"
    module.mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT", module)
    monkeypatch.setattr(config, "REPOSITORY_ROOT", repository)
    for name in list(os.environ):
        if name in {"HOST", "PORT"} or name.startswith(("CLIPPER_", "MODEL_", "AUDIO_", "BILIBILI_")):
            monkeypatch.delenv(name)
    other = tmp_path / "unrelated-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    return repository, module


@pytest.fixture(params=["patrol", "retention", "library"])
def cli(request):
    if request.param == "patrol":
        return patrol, "health", "run_dir", "--run-dir", Path("following-patrol")
    if request.param == "retention":
        return retention, "plan", "data_dir", "--data-dir", Path()
    from scripts import run_local_library as library
    return library, "health", "data_dir", "--data-dir", Path()


def test_independent_cli_uses_repository_relative_data_and_listener(roots, cli):
    repository, _ = roots
    (repository / ".env").write_text(
        "CLIPPER_DATA_DIR=storage/clips\nCLIPPER_HOST=0.0.0.0\nCLIPPER_PORT=7011\n")
    script, command, field, _, suffix = cli
    args = script.parse_args([command])
    assert getattr(args, field) == repository / "storage/clips" / suffix
    assert args.base_url == "http://127.0.0.1:7011"
    assert not (repository / "storage").exists()


def test_independent_cli_module_config_overrides_repository(roots, cli):
    repository, module = roots
    (repository / ".env").write_text("CLIPPER_DATA_DIR=common\nCLIPPER_PORT=7011\n")
    (module / ".env").write_text("CLIPPER_DATA_DIR=local\nCLIPPER_HOST=::\nCLIPPER_PORT=7012\n")
    script, command, field, _, suffix = cli
    args = script.parse_args([command])
    assert getattr(args, field) == module / "local" / suffix
    assert args.base_url == "http://[::1]:7012"


def test_independent_cli_explicit_flags_override_environment(roots, cli, monkeypatch):
    repository, _ = roots
    # Explicit flags must still work when an unrelated default is unusable.
    monkeypatch.setenv("CLIPPER_PORT", "invalid-port")
    monkeypatch.setenv("BILIBILI_COOKIE_FILE", str(repository / "missing-cookie"))
    script, command, field, flag, _ = cli
    data = repository / "chosen"
    args = script.parse_args([command, flag, str(data), "--base-url", "http://localhost:7033"])
    assert getattr(args, field) == data
    assert args.base_url == "http://localhost:7033"


@pytest.mark.parametrize("host, expected", [
    ("0.0.0.0", "127.0.0.1"), ("::", "[::1]"),
    ("2001:db8::2", "[2001:db8::2]"), ("localhost", "localhost"),
])
def test_worker_url_shares_host_but_not_companion_port(roots, monkeypatch, host, expected):
    monkeypatch.setenv("HOST", host)
    monkeypatch.setenv("PORT", "7044")
    assert config.local_workbench_base_url() == f"http://{expected}:6880"


def test_patrol_health_reads_the_configured_state_directory(roots, capsys):
    repository, _ = roots
    (repository / ".env").write_text("CLIPPER_DATA_DIR=custom-clips\n")
    run_dir = repository / "custom-clips/following-patrol"
    run_dir.mkdir(parents=True)
    (run_dir / "status.json").write_text(json.dumps({"status": "fixture-running"}))
    asyncio.run(patrol.main(patrol.parse_args(["health"])))
    assert json.loads(capsys.readouterr().out)["status"] == "fixture-running"


def test_retention_plan_uses_configured_data_without_applying_cleanup(roots, monkeypatch, capsys):
    repository, _ = roots
    (repository / ".env").write_text("CLIPPER_DATA_DIR=custom-clips\n")
    calls = []

    def plan(data_dir, **kwargs):
        calls.append(data_dir)
        return {"status": "planned"}

    monkeypatch.setattr(retention, "plan", plan)
    retention.main(["plan"])
    assert calls == [repository / "custom-clips"]
    assert json.loads(capsys.readouterr().out) == {"status": "planned"}
    assert not (repository / "custom-clips").exists()
