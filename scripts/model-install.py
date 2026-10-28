"""Install one pinned CPU-only llama.cpp release and an official Qwen GGUF in a personal cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

MODEL_SHA = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
MODEL_SIZE = 2497280256
MODEL_NAME = "Qwen3-4B-Q4_K_M.gguf"
MODEL_ORIGIN = (
    "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/a9a60d009fa7ff9606305047c2bf77ac25dbec49/" + MODEL_NAME
)
MODEL_MIRROR = "https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF/resolve/master/" + MODEL_NAME
RELEASE_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/b11669/llama-b11669-bin-ubuntu-x64.tar.gz"
)
RELEASE_SHA = "cf07c1c112d709bd67e522017471de57ed0ec8bea031d732c5422351c07f65ac"
RELEASE_SIZE = 17851611


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def download(url: str, target: Path, expected_size: int, expected_sha: str) -> None:
    if target.exists():
        if target.stat().st_size == expected_size and digest(target) == expected_sha:
            print(json.dumps({"reused": target.name}), flush=True)
            return
        raise RuntimeError(
            f"Existing {target.name} does not match the pinned release; preserve for inspection"
        )
    part = target.with_suffix(target.suffix + ".part")
    if part.exists() and part.stat().st_size == expected_size and digest(part) == expected_sha:
        part.replace(target)
        return
    if shutil.disk_usage(target.parent).free - expected_size < 20 * 1024**3:
        raise RuntimeError("Provisioning would leave less than 20 GiB available")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "sc2-harness-personal-model/0.1"})
    downloaded = 0
    next_report = 64 * 1024 * 1024
    with opener.open(request, timeout=60) as source, part.open("wb") as output:
        first = source.read(1024 * 1024)
        if target.suffix == ".gguf" and not first.startswith(b"GGUF"):
            raise RuntimeError("The model URL did not return a GGUF file")
        output.write(first)
        downloaded += len(first)
        while True:
            block = source.read(4 * 1024 * 1024)
            if not block:
                break
            downloaded += len(block)
            if downloaded > expected_size:
                raise RuntimeError("Download exceeds the pinned official size")
            output.write(block)
            if downloaded >= next_report:
                print(
                    json.dumps({"downloading": target.name, "bytes": downloaded, "total": expected_size}),
                    flush=True,
                )
                next_report += 64 * 1024 * 1024
    if downloaded != expected_size or digest(part) != expected_sha:
        raise RuntimeError("Download size/SHA-256 does not match the official published identity")
    part.replace(target)
    print(json.dumps({"verified": target.name, "bytes": downloaded, "sha256": expected_sha}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/sc2-harness/model")
    parser.add_argument("--model-source", choices=("modelscope", "huggingface"), default="modelscope")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_relative_to(Path.home().resolve()):
        raise SystemExit("Model files must stay in the current personal home")
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "llama-b11669-bin-ubuntu-x64.tar.gz"
    # The release was verified before implementation; keep this installer reusable without repeating downloads.
    legacy = root / "llama-b11669-bin-ubuntu-x64.tar.part"
    if legacy.exists() and not archive.exists() and digest(legacy) == RELEASE_SHA:
        legacy.replace(archive)
    download(RELEASE_URL, archive, RELEASE_SIZE, RELEASE_SHA)
    destination = root / "llama-b11669-cpu"
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        if len(members) > 5000 or sum(member.size for member in members) > 300 * 1024**2:
            raise RuntimeError("Unexpected CPU release archive size")
        for member in members:
            candidate = (destination / member.name).resolve()
            if not candidate.is_relative_to(destination.resolve()) or member.isdev():
                raise RuntimeError("Unsafe release archive path")
            if member.issym() and (
                Path(member.linkname).is_absolute()
                or not (candidate.parent / member.linkname).resolve().is_relative_to(destination.resolve())
            ):
                raise RuntimeError("Unsafe release symlink")
            if member.islnk() and not (destination / member.linkname).resolve().is_relative_to(
                destination.resolve()
            ):
                raise RuntimeError("Unsafe release hardlink")
        package.extractall(destination)
    servers = list(destination.rglob("llama-server"))
    if len(servers) != 1:
        raise RuntimeError("Missing/ambiguous CPU llama-server")
    version = subprocess.run(
        [str(servers[0]), "--version"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": ""},
    )
    model_url = MODEL_MIRROR if args.model_source == "modelscope" else MODEL_ORIGIN
    download(model_url, root / MODEL_NAME, MODEL_SIZE, MODEL_SHA)
    manifest = {
        "model": "Qwen3-4B",
        "upstream_model": "Qwen/Qwen3-4B-GGUF",
        "quantization": "Q4_K_M",
        "model_path": str(root / MODEL_NAME),
        "model_sha256": MODEL_SHA,
        "model_bytes": MODEL_SIZE,
        "model_primary_url": MODEL_ORIGIN,
        "model_download_url": model_url,
        "binary": str(servers[0]),
        "release_url": RELEASE_URL,
        "release_sha256": RELEASE_SHA,
        "release_version": (version.stdout + version.stderr).strip(),
        "gpu": False,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps({"installed": manifest, "free_gib": round(shutil.disk_usage(root).free / 1024**3, 2)}),
        flush=True,
    )


if __name__ == "__main__":
    main()
