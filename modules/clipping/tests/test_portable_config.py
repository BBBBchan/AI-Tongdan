import json
import runpy

import pytest

from clipper import config
from clipper.config import Settings, session_model_profile


@pytest.fixture
def config_roots(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    module = repository / "modules/clipping"
    module.mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT", module)
    monkeypatch.setattr(config, "REPOSITORY_ROOT", repository)
    for name in list(config.os.environ):
        if name.startswith(("MODEL_", "AUDIO_", "CLIPPER_", "BILIBILI_")):
            monkeypatch.delenv(name)
    other = tmp_path / "other-cwd"
    other.mkdir()
    monkeypatch.chdir(other)
    return repository, module


def make_endpoint(root, name="model", *, base="https://model.example", key="file-test-key"):
    folder = root / "private"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.key").write_text(key)
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"base_url": base, "model": "test-model", "api_key_file": f"{name}.key"}))
    return path


def test_repository_paths_are_not_relative_to_module_or_process_cwd(config_roots):
    repository, module = config_roots
    make_endpoint(repository)
    (repository / ".env").write_text("MODEL_ENDPOINT_FILE=private/model.json\nDATA_DIR=companion-data\nBILIBILI_COOKIE=read-only-cookie\n")
    settings = Settings.from_env()
    assert settings.model_config()["api_key"] == "file-test-key"
    assert settings.model_endpoint == str(repository / "private/model.json")
    assert settings.data_dir == module / "data"
    assert settings.cookie == "read-only-cookie"
    assert settings.upload_cookie() == ""


def test_module_file_overrides_root_file_and_has_its_own_path_origin(config_roots):
    repository, module = config_roots
    make_endpoint(repository, base="https://shared.example")
    make_endpoint(module, base="https://clipping.example")
    (repository / ".env").write_text("MODEL_ENDPOINT_FILE=private/model.json\n")
    (module / ".env").write_text("MODEL_ENDPOINT_FILE=private/model.json\nCLIPPER_DATA_DIR=local-data\n")
    settings = Settings.from_env()
    assert settings.model_config()["base_url"] == "https://clipping.example"
    assert settings.data_dir == module / "local-data"


def test_explicit_environment_wins_over_both_dotenv_files(config_roots, monkeypatch):
    repository, module = config_roots
    (repository / ".env").write_text("MODEL_BASE_URL=https://shared.example\n")
    (module / ".env").write_text("MODEL_BASE_URL=https://module.example\n")
    monkeypatch.setenv("MODEL_BASE_URL", "https://shell.example")
    assert Settings.from_env().model_config()["base_url"] == "https://shell.example"


@pytest.mark.parametrize("kind", ["MODEL", "AUDIO"])
def test_dedicated_endpoint_file_ignores_shared_environment(config_roots, monkeypatch, kind):
    _, module = config_roots
    file = make_endpoint(module, kind.lower(), base="https://dedicated.example")
    monkeypatch.setenv(kind + "_BASE_URL", "https://shared.example")
    monkeypatch.setenv(kind + "_API_KEY", "shared-test-key")
    monkeypatch.setenv("CLIPPER_" + kind + "_ENDPOINT_FILE", str(file))
    settings = Settings.from_env()
    actual = settings.model_config() if kind == "MODEL" else settings.audio_config()
    assert actual["base_url"] == "https://dedicated.example"
    assert actual["api_key"] == "file-test-key"


def test_broken_dedicated_file_does_not_silently_fall_back(config_roots, monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://shared.example")
    monkeypatch.setenv("CLIPPER_MODEL_ENDPOINT_FILE", "missing.json")
    value = Settings.from_env().model_config()
    assert not value["base_url"] and value["error"]


def test_explicit_dedicated_base_never_uses_shared_credentials(config_roots, monkeypatch):
    monkeypatch.setenv("MODEL_BASE_URL", "https://shared.example")
    monkeypatch.setenv("MODEL_API_KEY", "shared-test-key")
    monkeypatch.setenv("CLIPPER_MODEL_BASE_URL", "https://dedicated.example")
    value = Settings.from_env().model_config()
    assert value["base_url"] == "https://dedicated.example" and value["api_key"] == ""


def test_dedicated_fields_override_file_without_discarding_its_key(config_roots, monkeypatch):
    _, module = config_roots
    file = make_endpoint(module)
    monkeypatch.setenv("CLIPPER_MODEL_ENDPOINT_FILE", str(file))
    monkeypatch.setenv("CLIPPER_MODEL_BASE_URL", "https://dedicated.example")
    monkeypatch.setenv("MODEL_API_KEY", "shared-test-key")
    value = Settings.from_env().model_config()
    assert value["base_url"] == "https://dedicated.example"
    assert value["api_key"] == "file-test-key"


def test_validated_endpoint_file_can_be_updated_after_start(config_roots):
    repository, _ = config_roots
    endpoint = make_endpoint(repository)
    (repository / ".env").write_text("MODEL_ENDPOINT_FILE=private/model.json\n")
    settings = Settings.from_env()
    assert settings.model_config()["base_url"] == "https://model.example"
    value = json.loads(endpoint.read_text())
    value["base_url"] = "https://new-validated.example"
    endpoint.write_text(json.dumps(value))
    assert settings.model_config()["base_url"] == "https://new-validated.example"


def test_clipper_name_override_can_share_explicit_common_service(config_roots, monkeypatch):
    monkeypatch.setenv("AUDIO_BASE_URL", "https://shared.example")
    monkeypatch.setenv("AUDIO_API_KEY", "shared-test-key")
    monkeypatch.setenv("CLIPPER_AUDIO_NAME", "dedicated-asr-model")
    assert Settings.from_env().audio_config() == {
        "base_url": "https://shared.example", "api_key": "shared-test-key", "model": "dedicated-asr-model"}


def test_upload_cookie_file_path_keeps_its_dotenv_origin(config_roots):
    repository, _ = config_roots
    cookie = repository / "private/upload.cookie"
    cookie.parent.mkdir()
    cookie.write_text("upload-cookie-test")
    (repository / ".env").write_text("BILIBILI_UPLOAD_COOKIE_FILE=private/upload.cookie\n")
    assert Settings.from_env().upload_cookie() == "upload-cookie-test"


@pytest.mark.parametrize("name", ["../extra", "UPPER", "space name", "/absolute", "a" * 49, 23, False, 0])
def test_profile_name_cannot_be_a_path_or_arbitrary_value(name):
    with pytest.raises(ValueError, match="session_model_profile_unknown"):
        session_model_profile({"model_profile": name})


def test_named_profile_uses_relative_endpoint_paths_and_optional_identity(tmp_path):
    settings = Settings(data_dir=tmp_path)
    profiles = tmp_path / "model-profiles"
    profiles.mkdir()
    model = make_endpoint(profiles, "model")
    audio = make_endpoint(profiles, "audio")
    profile = profiles / "stream-two.json"
    profile.write_text(json.dumps({"version": 1, "profile": "stream-two",
                                  "model_endpoint": "private/model.json", "audio_endpoint": "private/audio.json",
                                  "owner_uid": 13579, "room_id": 24680}))
    scoped = settings.for_session({"model_profile": "stream-two"})
    assert scoped.model_endpoint == str(model) and scoped.audio_endpoint == str(audio)
    scoped.validate_profile_identity(room_id=24680)
    scoped.validate_profile_identity(room_id=24680, owner_uid=13579, observed=True)
    for values in ({"room_id": 24681}, {"owner_uid": 2}, {"room_id": 24680, "observed": True}):
        with pytest.raises(ValueError, match="session_model_profile_identity_mismatch"):
            scoped.validate_profile_identity(**values)
    value = json.loads(profile.read_text()); value.pop("owner_uid"); value.pop("room_id")
    profile.write_text(json.dumps(value))
    settings.for_session({"model_profile": "stream-two"}).validate_profile_identity(room_id=1, owner_uid=2, observed=True)


def test_cli_uses_explicit_clipper_binding_over_common_ports(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "configuration_environment", lambda: ({"HOST": "127.0.0.2", "PORT": "7999",
        "CLIPPER_HOST": "127.0.0.3", "CLIPPER_PORT": "6999"}, {}))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append((args, kwargs)))
    runpy.run_module("clipper.__main__", run_name="__main__")
    assert calls[0][1]["host"] == "127.0.0.3" and calls[0][1]["port"] == 6999


def test_standalone_clipper_does_not_bind_the_companion_port(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "configuration_environment", lambda: ({"PORT": "7862"}, {}))
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append(kwargs))
    runpy.run_module("clipper.__main__", run_name="__main__")
    assert calls[0]["port"] == 6880
