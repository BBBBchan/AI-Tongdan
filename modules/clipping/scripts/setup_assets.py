#!/usr/bin/env python3
"""Install pinned playback/font assets with their licenses and SHA-256 checks.

Reuse the companion app's bundled HLS files when present. Downloads happen only
when this script is explicitly run; importing it never changes the filesystem.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent.parent
NOTO_REVISION = "f8d157532fbfaeda587e826d4cd5b21a49186f7c"
NOTO_BASE = f"https://raw.githubusercontent.com/notofonts/noto-cjk/{NOTO_REVISION}/Sans"


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    sha256: str
    alternate_sha256: str | None = None


ASSETS = (
    Asset("vendor/hls.min.js", "https://cdn.jsdelivr.net/npm/hls.js@1.6.13/dist/hls.min.js",
          "7c47cd97d7a6e7b98d9623dd8ed9a6d45af4be4085e0c2001cd7175c2b4cfb07"),
    Asset("vendor/hls.LICENSE", "https://cdn.jsdelivr.net/npm/hls.js@1.6.13/LICENSE",
          "ca8773cf798c7ed997d4dd7c8e23c348699f8d5b7462636694cc14de6cda12db"),
    Asset("vendor/APACHE-2.0.txt", "https://www.apache.org/licenses/LICENSE-2.0.txt",
          "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
          # The repository's copy differs only by one blank line.
          "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"),
    Asset("fonts/NotoSansCJKsc-Bold.otf", f"{NOTO_BASE}/OTF/SimplifiedChinese/NotoSansCJKsc-Bold.otf",
          "b5f0d1a190a7f9b43c310a8850630af12553df32c4c050543f9059732d9b4c0a"),
    Asset("fonts/OFL.txt", f"{NOTO_BASE}/LICENSE",
          "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"),
)


def valid_file(path: Path, expected: str, alternate: str | None = None) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() in {expected, alternate}


def install_asset(asset: Asset, asset_root: Path, *, offline=False, verify_only=False) -> str:
    destination = asset_root / asset.name
    if valid_file(destination, asset.sha256, asset.alternate_sha256):
        return "verified"
    if verify_only:
        raise ValueError("missing file or SHA-256 mismatch")
    bundled = REPOSITORY / "static" / asset.name
    source = bundled if valid_file(bundled, asset.sha256, asset.alternate_sha256) else None
    if source is None and offline:
        raise ValueError("not available offline")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=destination.name + ".",
                                         suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            if source:
                input_stream = source.open("rb")
            else:
                request = urllib.request.Request(asset.url, headers={"User-Agent": "AI-Tongdan-Asset-Setup/1"})
                input_stream = urllib.request.urlopen(request, timeout=90)
            with input_stream:
                for chunk in iter(lambda: input_stream.read(1024 * 1024), b""):
                    output.write(chunk)
        if not valid_file(temporary, asset.sha256, asset.alternate_sha256):
            raise ValueError("download SHA-256 mismatch; existing file was preserved")
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return "copied from repository" if source else "downloaded and verified"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Verify all selected checksums; do not write or download.")
    parser.add_argument("--offline", action="store_true", help="Use existing or bundled files only; do not download.")
    parser.add_argument("--skip-font", action="store_true", help="Install only the browser HLS player and its licenses.")
    parser.add_argument("--asset-root", type=Path, default=ROOT / "static", help="Destination static directory.")
    args = parser.parse_args(argv)
    failures = 0
    for asset in ASSETS:
        if args.skip_font and asset.name.startswith("fonts/"):
            continue
        try:
            result = install_asset(asset, args.asset_root, offline=args.offline, verify_only=args.verify_only)
            print(f"{asset.name}: {result}")
        except (OSError, ValueError) as exc:
            failures += 1
            print(f"{asset.name}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
