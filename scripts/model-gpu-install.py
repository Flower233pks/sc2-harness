"""Install an optional pinned CUDA 12.8 llama.cpp binary, reusing existing GGUF weights."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import tarfile
from pathlib import Path

URL = "https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-ubuntu-cuda-12.8-x64.tar.gz"
SHA = "4703ed1a3cd6e8504affb6cb9866f501e96d634d68a002dc9a8251c9e2819b49"
SIZE = 172525113


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/sc2-harness/model")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_relative_to(Path.home().resolve()) or not (root / "manifest.json").exists():
        raise SystemExit("Install the personal base model first")
    common = runpy.run_path(str(Path(__file__).with_name("model-install.py")))
    archive = root / "llama-b11669-bin-ubuntu-cuda-12.8-x64.tar.gz"
    common["download"](URL, archive, SIZE, SHA)
    destination = root / "llama-b11669-cuda12"
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        if len(members) > 5000 or sum(member.size for member in members) > 1024**3:
            raise RuntimeError("Unexpected CUDA release archive size")
        for member in members:
            candidate = (destination / member.name).resolve()
            if not candidate.is_relative_to(destination) or member.isdev():
                raise RuntimeError("Unsafe archive path")
            if member.issym() and (
                Path(member.linkname).is_absolute()
                or not (candidate.parent / member.linkname).resolve().is_relative_to(destination)
            ):
                raise RuntimeError("Unsafe archive symlink")
            if member.islnk() and not (destination / member.linkname).resolve().is_relative_to(destination):
                raise RuntimeError("Unsafe archive hardlink")
        package.extractall(destination)
    servers = list(destination.rglob("llama-server"))
    if len(servers) != 1:
        raise RuntimeError("Missing/ambiguous CUDA llama-server")
    binary = servers[0]
    dependencies = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=True)
    if "not found" in dependencies.stdout:
        raise RuntimeError("CUDA runtime dependencies are missing; do not alter system CUDA or drivers")
    version = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": "1"},
    )
    manifest = {
        "binary": str(binary),
        "release_url": URL,
        "release_sha256": SHA,
        "release_bytes": SIZE,
        "dependencies": dependencies.stdout,
        "version": (version.stdout + version.stderr).strip(),
        "physical_gpu": 1,
    }
    (root / "gpu-release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    main()
