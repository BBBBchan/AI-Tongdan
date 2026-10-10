"""Create private keys and loopback endpoint files without replacing existing files."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parent.parent


def create_private(path: Path, text: str):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"Kept existing {path}")
        return
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)
    print(f"Created {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    directory = args.data_dir.expanduser().resolve()
    private = directory / "secrets"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    for kind, port, model in [("vision", 8020, "Qwen3.8-27B-FP8"), ("audio", 8021, "Qwen3-ASR-0.6B + CAM++")]:
        key_file = private / f"{kind}_api_key.txt"
        create_private(key_file, secrets.token_urlsafe(32) + "\n")
        if len(key_file.read_text().strip()) < 20:
            raise SystemExit(f"Existing key is too short: {key_file}; repair it manually")
        endpoint_name = "model_endpoint.json" if kind == "vision" else "audio_endpoint.json"
        endpoint = {"base_url": f"http://127.0.0.1:{port}" + ("/v1" if kind == "vision" else ""),
                    "model": model, "api_key_file": f"secrets/{kind}_api_key.txt"}
        create_private(directory / endpoint_name, json.dumps(endpoint, indent=2) + "\n")
    print("Configuration created, not connectivity-validated. Run the checks in docs/deployment.md.")


if __name__ == "__main__":
    main()
