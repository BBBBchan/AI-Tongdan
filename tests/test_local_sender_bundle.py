from io import BytesIO
import stat
from zipfile import ZipFile

import pytest

from app import local_sender_bundle as bundle


def test_bundle_is_fixed_source_whitelist_with_licenses():
    with ZipFile(BytesIO(bundle.build_local_sender_bundle())) as archive:
        assert set(archive.namelist()) == set(bundle.BUNDLE_FILES)
        assert {"LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore"} <= set(archive.namelist())
        assert all(not name.endswith(".json") for name in archive.namelist())
        assert "data/" not in " ".join(archive.namelist())
        assert ".aitongdan-local/" in archive.read(".gitignore").decode()
        assert "bilibili_cookie*.txt" in archive.read(".gitignore").decode()
        for name in ("start-local-sender.sh", "start-local-sender.command"):
            assert (archive.getinfo(name).external_attr >> 16) & stat.S_IXUSR
        for name in ("local_sender.py", "app/danmaku_send.py", "app/bilibili.py"):
            compile(archive.read(name), name, "exec")


def test_bundle_does_not_traverse_or_include_extra_private_files(tmp_path, monkeypatch):
    for name in bundle.BUNDLE_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source placeholder")
    for name in ("bilibili_cookie.txt", "data/bilibili_send_credentials.private.json",
                 ".aitongdan-local/jobs/private.json", "danmakus_style/real-messages.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private-material-must-not-appear")
    monkeypatch.setattr(bundle, "ROOT", tmp_path)
    with ZipFile(BytesIO(bundle.build_local_sender_bundle())) as archive:
        assert set(archive.namelist()) == set(bundle.BUNDLE_FILES)
        assert all(b"private-material" not in archive.read(name) for name in archive.namelist())


def test_bundle_rejects_symlink_in_whitelist(tmp_path, monkeypatch):
    (tmp_path / "private").write_text("private-material")
    (tmp_path / "local_sender.py").symlink_to(tmp_path / "private")
    monkeypatch.setattr(bundle, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        bundle.build_local_sender_bundle()
