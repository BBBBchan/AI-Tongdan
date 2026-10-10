"""Local configuration; credentials never enter public state."""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path
    ffmpeg_bin: str
    sample_interval: float = 1
    room_poll_seconds: float = 20
    model_timeout: float = 90
    bilibili_cookie: str = ""
    observation_frames: int = 4
    observation_stride: int = 3
    observation_concurrency: int = 2
    audio_enabled: bool = True
    audio_alignment_wait_seconds: float = 4

    def __post_init__(self):
        if self.observation_frames not in (2, 4):
            raise ValueError("OBSERVATION_FRAMES must be 2 or 4")

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        directory = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
        if not directory.is_absolute():
            directory = ROOT / directory
        return cls(
            directory.resolve(), os.getenv("FFMPEG_BIN", "ffmpeg"),
            max(0.5, float(os.getenv("SAMPLE_INTERVAL", "1"))),
            max(5, float(os.getenv("ROOM_POLL_SECONDS", "20"))),
            max(5, float(os.getenv("MODEL_TIMEOUT", "90"))),
            os.getenv("BILIBILI_COOKIE", ""),
            int(os.getenv("OBSERVATION_FRAMES", "4")),
            max(1, min(4, int(os.getenv("OBSERVATION_STRIDE", "3")))),
            max(1, min(2, int(os.getenv("OBSERVATION_CONCURRENCY", "2")))),
            os.getenv("AUDIO_ENABLED", "1").strip().lower() not in {"0", "false", "no"},
            max(0, min(8, float(os.getenv("AUDIO_ALIGNMENT_WAIT_SECONDS", "4")))),
        )

    @property
    def ffmpeg_available(self):
        return bool(shutil.which(self.ffmpeg_bin))

    def model_config(self):
        self.model_error = None
        config = {
            "base_url": os.getenv("MODEL_BASE_URL", "").strip(),
            "model": os.getenv("MODEL_NAME", "Qwen3.8-27B-FP8"),
            "api_key": os.getenv("MODEL_API_KEY", ""),
            "timeout": self.model_timeout,
        }
        if config["base_url"]:
            return config
        path = Path(os.getenv("MODEL_ENDPOINT_FILE", str(self.data_dir / "model_endpoint.json")))
        if not path.is_absolute():
            path = ROOT / path
        if path.is_file():
            try:
                value = json.loads(path.read_text())
                base_url = value.get("base_url", value.get("api_base_url", ""))
                name = value.get("model", config["model"])
                if not isinstance(base_url, str) or not isinstance(name, str):
                    raise ValueError("Invalid model endpoint")
                key_path = value.get("api_key_file")
                if key_path:
                    key_file = Path(key_path)
                    if not key_file.is_absolute():
                        key_file = path.parent / key_file
                    config["api_key"] = key_file.read_text().strip()
                config.update(base_url=base_url.rstrip("/"), model=name)
            except (OSError, ValueError, TypeError, AttributeError):
                self.model_error = "模型配置文件无法读取，请检查 endpoint JSON 与密钥文件。"
        return config

    def audio_config(self):
        """A separate endpoint keeps audio inference independent of vision."""
        config = {"base_url": os.getenv("AUDIO_BASE_URL", "").strip(),
                  "api_key": os.getenv("AUDIO_API_KEY", ""),
                  "model": "Qwen3-ASR-0.6B + CAM++"}
        if config["base_url"]:
            return config
        path = Path(os.getenv("AUDIO_ENDPOINT_FILE", str(self.data_dir / "audio_endpoint.json")))
        if not path.is_absolute():
            path = ROOT / path
        try:
            value = json.loads(path.read_text())
            address = value.get("base_url", "")
            if not isinstance(address, str):
                return config
            key_path = value.get("api_key_file")
            if key_path:
                key_path = Path(key_path)
                if not key_path.is_absolute():
                    key_path = path.parent / key_path
                config["api_key"] = key_path.read_text().strip()
            config.update(base_url=address.rstrip("/"), model=value.get("model", config["model"]))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return config
