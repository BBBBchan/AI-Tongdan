"""Launch a local authenticated vLLM process with configurable paths and limits."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print the command without reading credentials or starting a model")
    args = parser.parse_args()
    model = Path(os.environ.get("VISION_MODEL_DIR", str(ROOT / "data/models/Qwen3.8-27B-FP8"))).expanduser().resolve()
    command = [sys.executable, "-m", "vllm.entrypoints.openai.api_server",
               "--model", str(model), "--served-model-name", os.environ.get("VISION_MODEL_NAME", "Qwen3.8-27B-FP8"),
               "--host", os.environ.get("VISION_HOST", "127.0.0.1"),
               "--port", os.environ.get("VISION_PORT", "8020"),
               "--tensor-parallel-size", os.environ.get("VISION_TENSOR_PARALLEL_SIZE", "1"),
               "--dtype", "bfloat16", "--max-model-len", os.environ.get("VISION_MAX_MODEL_LEN", "32768"),
               "--max-num-seqs", os.environ.get("VISION_MAX_NUM_SEQS", "4"),
               "--limit-mm-per-prompt", '{"image":8}',
               "--gpu-memory-utilization", os.environ.get("VISION_GPU_MEMORY_UTILIZATION", "0.65"),
               "--gdn-prefill-backend", "triton", "--no-enable-log-requests"]
    if args.dry_run:
        print(json.dumps(command, indent=2))
        return
    if not (model / "config.json").is_file():
        raise SystemExit("Model missing; run deploy/download_models.py --component vision or set VISION_MODEL_DIR")
    environment = dict(os.environ)
    key_file = Path(environment.get("VISION_API_KEY_FILE", str(ROOT / "data/secrets/vision_api_key.txt"))).expanduser()
    key = environment.get("VLLM_API_KEY", "").strip() or (key_file.read_text().strip() if key_file.is_file() else "")
    if len(key) < 20:
        raise SystemExit("Set VLLM_API_KEY or create a private key with deploy/configure_local.py")
    environment.update(VLLM_API_KEY=key, PYTHONUNBUFFERED="1")
    environment.setdefault("CUDA_VISIBLE_DEVICES", "0")
    # Local snapshots are required, so model loading never silently fetches a new revision.
    environment.setdefault("HF_HUB_OFFLINE", "1")
    environment.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.execvpe(sys.executable, command, environment)


if __name__ == "__main__":
    main()
