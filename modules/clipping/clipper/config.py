from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import shutil

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = ROOT.parent.parent
PROFILE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")


def session_model_profile(session):
    profile = (session or {}).get("model_profile")
    if profile is None or profile == "":
        profile = "default"
    if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile):
        raise ValueError("session_model_profile_unknown")
    return profile


def configuration_environment():
    """Read repository and module settings without changing another app's env.

    Module .env overrides repository .env; explicit process values win. Paths
    remember the file that supplied them, so launching from another cwd does
    not silently choose a different endpoint or cookie file.
    """
    values, origins = {}, {}
    def relevant(key):
        return key in {"HOST", "PORT"} or key.startswith(("MODEL_", "AUDIO_", "CLIPPER_", "BILIBILI_"))
    for root in (REPOSITORY_ROOT, ROOT):
        for key, value in dotenv_values(root / ".env").items():
            if not relevant(key):
                continue
            values[key] = value or ""
            origins[key] = root
    for key, value in os.environ.items():
        if not relevant(key):
            continue
        # A parent launcher may already have loaded the same .env. Preserve its
        # known file origin when the value has not been changed by the caller.
        if values.get(key) != value:
            origins[key] = Path.cwd()
        values[key] = value
    return values, origins


def configured_path(values, origins, name, default=""):
    value = values.get(name) or default
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = origins.get(name, ROOT) / path
    return str(path.resolve())


def local_workbench_base_url():
    """Connect local workers to the configured listener, including wildcards."""
    values, _ = configuration_environment()
    host = values.get("CLIPPER_HOST") or values.get("HOST") or "127.0.0.1"
    # PORT belongs to the companion; a standalone worker must never call its API.
    port = int(values.get("CLIPPER_PORT") or "6880")
    if not 1 <= port <= 65535:
        raise ValueError("切片服务端口必须在 1–65535 之间")
    host = host.strip("[]")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def endpoint(path, prefix, *, use_environment=True, values=None):
    values = os.environ if values is None else values
    result = {"base_url": values.get(prefix + "_BASE_URL", "") if use_environment else "",
              "api_key": values.get(prefix + "_API_KEY", "") if use_environment else "",
              "model": values.get(prefix + "_NAME", "Qwen3.8-27B-FP8") if use_environment else "Qwen3.8-27B-FP8"}
    if result["base_url"] or not path:
        return result
    try:
        p = Path(path).expanduser().resolve()
        value = json.loads(p.read_text())
        result["base_url"] = value.get("base_url", value.get("api_base_url", "")).rstrip("/")
        result["model"] = value.get("model", result["model"])
        if value.get("api_key_file"):
            key = Path(value["api_key_file"]).expanduser()
            if not key.is_absolute():
                key = p.parent / key
            result["api_key"] = key.read_text().strip()
    except (OSError, ValueError, TypeError, AttributeError):
        result["error"] = "端点配置无法读取"
    return result


def _model_settings(values, origins, kind):
    specific = "CLIPPER_" + kind
    explicit_file = bool(values.get(specific + "_ENDPOINT_FILE"))
    explicit_base = bool(values.get(specific + "_BASE_URL"))
    path_key = (specific if explicit_file else kind) + "_ENDPOINT_FILE"
    path = configured_path(values, origins, path_key)
    if explicit_file or explicit_base:
        # A dedicated clipper endpoint must never inherit the companion's base
        # URL or credentials. Explicit dedicated fields may override its file.
        config = endpoint(path if explicit_file else "", specific,
                          use_environment=False, values=values)
        for field, suffix in (("base_url", "_BASE_URL"), ("model", "_NAME"), ("api_key", "_API_KEY")):
            if values.get(specific + suffix):
                config[field] = values[specific + suffix]
    else:
        config = endpoint(path, kind, values=values)
        for field, suffix in (("model", "_NAME"), ("api_key", "_API_KEY")):
            if values.get(specific + suffix):
                config[field] = values[specific + suffix]
    return path, config


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    ffmpeg: str = ""
    capture_ffmpeg: str = ""
    ffprobe: str = ""
    font: str = ""
    cookie: str = ""
    model_endpoint: str = ""
    audio_endpoint: str = ""
    analyze_interval: float = 15
    minimum_score: int = 88
    max_recording_gib: float = 30
    import_roots: tuple = ()
    upload_cookie_file: str = ""
    model_profile: str = "default"
    endpoint_environment: bool = True
    expected_owner_uid: int | None = None
    expected_room_id: int | None = None
    _configuration_values: dict | None = field(default=None, repr=False)
    _configuration_origins: dict | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls):
        values, origins = configuration_environment()
        data_dir = Path(configured_path(values, origins, "CLIPPER_DATA_DIR", str(ROOT / "data")))
        ffmpeg = values.get("CLIPPER_FFMPEG", "") or shutil.which("ffmpeg") or ""
        if not ffmpeg:
            try:
                import imageio_ffmpeg
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                pass
        import_base = origins.get("CLIPPER_IMPORT_ROOTS", ROOT)
        roots = tuple((Path(x).expanduser() if Path(x).expanduser().is_absolute()
                       else import_base / Path(x).expanduser()).resolve()
                      for x in (values.get("CLIPPER_IMPORT_ROOTS") or str(data_dir)).split(os.pathsep) if x)
        cookie = values.get("BILIBILI_COOKIE", "")
        if not cookie and values.get("BILIBILI_COOKIE_FILE"):
            cookie = Path(configured_path(values, origins, "BILIBILI_COOKIE_FILE")).read_text().strip()
        if not cookie and values.get("BILIBILI_COOKIE_JSON"):
            cookie = json.loads(Path(configured_path(values, origins, "BILIBILI_COOKIE_JSON")).read_text()).get("cookie", "")
        model_path, _ = _model_settings(values, origins, "MODEL")
        audio_path, _ = _model_settings(values, origins, "AUDIO")
        return cls(data_dir=data_dir,
                   ffmpeg=ffmpeg, ffprobe=values.get("CLIPPER_FFPROBE", "") or shutil.which("ffprobe") or "",
                   capture_ffmpeg=values.get("CLIPPER_CAPTURE_FFMPEG", ""),
                   font=configured_path(values, origins, "CLIPPER_FONT", str(ROOT / "static/fonts/NotoSansCJKsc-Bold.otf")),
                   cookie=cookie,
                   upload_cookie_file=configured_path(values, origins, "BILIBILI_UPLOAD_COOKIE_FILE"),
                   model_endpoint=model_path, audio_endpoint=audio_path,
                   _configuration_values=values, _configuration_origins=origins,
                   max_recording_gib=float(values.get("CLIPPER_RECORDING_GIB") or "30"),
                   import_roots=roots)

    def model_config(self):
        if self._configuration_values is not None:
            return _model_settings(self._configuration_values, self._configuration_origins, "MODEL")[1]
        return endpoint(self.model_endpoint, "MODEL", use_environment=self.endpoint_environment)

    def audio_config(self):
        if self._configuration_values is not None:
            return _model_settings(self._configuration_values, self._configuration_origins, "AUDIO")[1]
        return endpoint(self.audio_endpoint, "AUDIO", use_environment=self.endpoint_environment)

    def for_session(self, session):
        """Resolve a locally configured profile without exposing endpoints.

        A profile has its own named file and fails closed if unavailable. It
        cannot silently fall back to the default model pool or credentials.
        """
        profile = session_model_profile(session)
        if profile == "default":
            if self.model_profile != "default":
                raise ValueError("session_model_profile_mismatch")
            return self
        try:
            path = self.data_dir / "model-profiles" / (profile + ".json")
            value = json.loads(path.read_text())
            if value.get("version") != 1 or value.get("profile") != profile:
                raise ValueError()
            paths = []
            for name in ("model_endpoint", "audio_endpoint"):
                raw = value[name]
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError()
                target = Path(raw).expanduser()
                if not target.is_absolute():
                    target = path.parent / target
                if not target.is_file():
                    raise ValueError()
                paths.append(str(target.resolve()))
            owner, room = value.get("owner_uid"), value.get("room_id")
            if any(v is not None and (type(v) is not int or v <= 0) for v in (owner, room)):
                raise ValueError()
            scoped = replace(self, model_endpoint=paths[0], audio_endpoint=paths[1],
                             model_profile=profile, endpoint_environment=False,
                             _configuration_values=None, _configuration_origins=None,
                             expected_owner_uid=owner, expected_room_id=room)
            if any(c.get("error") or not c.get("base_url") or not c.get("api_key")
                   for c in (scoped.model_config(), scoped.audio_config())):
                raise ValueError()
            return scoped
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("session_model_profile_unavailable") from None

    def validate_profile_identity(self, *, room_id=None, owner_uid=None, observed=False):
        for expected, actual in ((self.expected_room_id, room_id), (self.expected_owner_uid, owner_uid)):
            if expected is not None and (observed or actual is not None) and str(actual) != str(expected):
                raise ValueError("session_model_profile_identity_mismatch")

    def upload_cookie(self):
        path = Path(self.upload_cookie_file) if self.upload_cookie_file else self.data_dir / "private/bilibili-upload.cookie"
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    def import_path(self, value):
        p = Path(value).expanduser().resolve()
        if not p.is_file() or not any(p.is_relative_to(root) for root in self.import_roots):
            raise ValueError("文件不存在，或不在已配置的素材目录内。也可以直接上传文件。")
        return p
