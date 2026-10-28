"""Build the pinned CUDA server privately when a release requires a newer host glibc."""

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

COMMIT = "620d2225cc436ab0b001e3200e590e3b286ef9ab"
URL = f"https://codeload.github.com/ggml-org/llama.cpp/tar.gz/{COMMIT}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/sc2-harness/model")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_relative_to(Path.home().resolve()) or not (root / "manifest.json").exists():
        raise SystemExit("Install the personal base model first")
    nvcc = Path("/usr/local/cuda-12.9/bin/nvcc")
    if not nvcc.is_file() or not shutil.which("cmake") or not shutil.which("g++"):
        raise SystemExit(
            "Existing nvcc/CMake/C++ prerequisites are unavailable; do not install system packages"
        )
    if shutil.disk_usage(root).free < 22 * 1024**3:
        raise SystemExit("Need at least 22GiB available before the private build")
    archive = root / f"llama-source-{COMMIT}.tar.gz"
    if not archive.exists():
        part = archive.with_suffix(".part")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        count = 0
        with opener.open(URL, timeout=60) as source, part.open("wb") as output:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > 128 * 1024**2:
                    raise RuntimeError("Pinned source archive exceeded the approved small-source budget")
                output.write(block)
        part.replace(archive)
    source_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    stage = root / "cuda-source"
    stage.mkdir(exist_ok=True)
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        if len(members) > 100000 or sum(member.size for member in members) > 512 * 1024**2:
            raise RuntimeError("Unexpected source archive size")
        for member in members:
            candidate = (stage / member.name).resolve()
            if not candidate.is_relative_to(stage) or member.isdev():
                raise RuntimeError("Unsafe source archive path")
            if member.issym() and (
                Path(member.linkname).is_absolute()
                or not (candidate.parent / member.linkname).resolve().is_relative_to(stage)
            ):
                raise RuntimeError("Unsafe source symlink")
            if member.islnk() and not (stage / member.linkname).resolve().is_relative_to(stage):
                raise RuntimeError("Unsafe source hardlink")
        package.extractall(stage)
    sources = [path for path in stage.iterdir() if path.is_dir() and (path / "CMakeLists.txt").is_file()]
    if len(sources) != 1:
        raise RuntimeError("Missing/ambiguous pinned source tree")
    build = root / "cuda-build-sm80"
    environment = {
        **os.environ,
        "CUDACXX": str(nvcc),
        "CUDA_VISIBLE_DEVICES": "",
        "CMAKE_BUILD_PARALLEL_LEVEL": "8",
        "NVCC_THREADS": "1",
    }
    configure = [
        "cmake",
        "-S",
        str(sources[0]),
        "-B",
        str(build),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DGGML_CUDA=ON",
        "-DCMAKE_CUDA_ARCHITECTURES=80",
        "-DCMAKE_CUDA_FLAGS=--threads=1",
        f"-DCMAKE_CUDA_COMPILER={nvcc}",
        "-DGGML_NATIVE=OFF",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_EXAMPLES=OFF",
        "-DLLAMA_BUILD_SERVER=ON",
        "-DLLAMA_CURL=OFF",
    ]
    print(
        json.dumps(
            {
                "source_commit": COMMIT,
                "source_bytes": archive.stat().st_size,
                "source_sha256_locally_recorded": source_sha,
                "configure": configure,
            }
        ),
        flush=True,
    )
    with (root / "gpu-build.log").open("a") as log:
        subprocess.run(configure, check=True, env=environment, stdout=log, stderr=log)
        subprocess.run(
            ["cmake", "--build", str(build), "--parallel", "8", "--target", "llama-server"],
            check=True,
            env=environment,
            stdout=log,
            stderr=log,
        )
    binary = build / "bin/llama-server"
    dependencies = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=True)
    if "not found" in dependencies.stdout + dependencies.stderr:
        raise RuntimeError("Private CUDA build still has unmet runtime dependencies")
    version = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "CUDA_VISIBLE_DEVICES": "1"},
    )
    manifest = {
        "binary": str(binary),
        "source_url": URL,
        "source_commit": COMMIT,
        "source_archive_sha256_locally_recorded": source_sha,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "build_parallel": 8,
        "cuda_architecture": "80",
        "cuda_compiler": str(nvcc),
        "dependencies": dependencies.stdout,
        "version": (version.stdout + version.stderr).strip(),
        "physical_gpu": 1,
        "build_log": str(root / "gpu-build.log"),
    }
    (root / "gpu-release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps({"built": manifest, "free_gib": round(shutil.disk_usage(root).free / 1024**3, 2)}),
        flush=True,
    )


if __name__ == "__main__":
    main()
