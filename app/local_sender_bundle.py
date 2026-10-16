"""Source-only desktop helper download. Never traverse a workspace or user data."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_FILES = (
    "local_sender.py", "app/__init__.py", "app/bilibili.py", "app/danmaku_send.py",
    "requirements-local-sender.txt", "start-local-sender.bat", "start-local-sender.command",
    "start-local-sender.sh", "get_bilibili_cookie.py", "docs/local-sender.md",
    "LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore",
)


def build_local_sender_bundle() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for relative in BUNDLE_FILES:
            source = ROOT / relative
            if source.is_symlink() or not source.is_file():
                raise FileNotFoundError("本机助手发布文件缺失，请检查部署是否完整。")
            info = ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            executable = relative.endswith((".sh", ".command"))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            archive.writestr(info, source.read_bytes())
    return output.getvalue()
