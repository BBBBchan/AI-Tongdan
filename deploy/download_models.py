"""Download pinned public snapshots; weights and receipts stay under data/."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = Path(__file__).with_name("models.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("vision", "audio", "all"), default="all")
    parser.add_argument("--model-dir", type=Path, default=ROOT / "data/models")
    parser.add_argument("--dry-run", action="store_true", help="Print sources without downloading")
    args = parser.parse_args()
    registry = json.loads(REGISTRY.read_text())
    names = {"vision": ["vision"], "audio": ["asr", "speaker"], "all": ["vision", "asr", "speaker"]}[args.component]
    destination = args.model_dir.expanduser().resolve()
    selected = {name: {**registry[name], "path": str(destination / registry[name]["directory"])} for name in names}
    if args.dry_run:
        print(json.dumps(selected, indent=2))
        return
    from huggingface_hub import snapshot_download

    started = time.time()
    for name, source in selected.items():
        print(f"Downloading {source['repo']} @ {source['revision']}", flush=True)
        snapshot_download(repo_id=source["repo"], revision=source["revision"],
                          local_dir=source["path"], max_workers=4)
    receipt = destination / f"download-{args.component}.json"
    receipt.write_text(json.dumps({"sources": selected, "elapsed_seconds": time.time() - started}, indent=2) + "\n")
    print(f"Downloads complete; receipt: {receipt}")


if __name__ == "__main__":
    main()
