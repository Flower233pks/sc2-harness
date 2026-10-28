"""Prestage the official 8B GGUF without modifying or querying the active model service."""

from __future__ import annotations

import argparse
import json
import runpy
import shutil
from pathlib import Path

NAME = "Qwen3-8B-Q4_K_M.gguf"
SIZE = 5027783488
SHA = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
PRIMARY = "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/6a569868d07d3bd59e8b97fb001bf8c0b254bb20/" + NAME
MIRROR = "https://modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/" + NAME


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/sc2-harness/model")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_relative_to(Path.home().resolve()) or not root.is_dir():
        raise SystemExit("Use the existing personal SC2 model cache")
    candidate = root / "candidates"
    candidate.mkdir(exist_ok=True)
    common = runpy.run_path(str(Path(__file__).with_name("model-install.py")))
    common["download"](MIRROR, candidate / NAME, SIZE, SHA)
    manifest = {
        "model": "Qwen3-8B",
        "upstream": "Qwen/Qwen3-8B-GGUF",
        "quantization": "Q4_K_M",
        "model_path": str(candidate / NAME),
        "bytes": SIZE,
        "sha256": SHA,
        "primary_url": PRIMARY,
        "download_url": MIRROR,
        "status": "downloaded-not-loaded",
        "thinking": "hybrid; explicitly disable for experiment",
    }
    (candidate / "Qwen3-8B.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps({"candidate": manifest, "free_gib": round(shutil.disk_usage(root).free / 1024**3, 2)}),
        flush=True,
    )


if __name__ == "__main__":
    main()
