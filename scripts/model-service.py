"""Start/status/stop a personal loopback Qwen service on bounded CPU or physical GPU1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

MODEL_PROFILES = {
    "4b": {
        "alias": "Qwen3-4B",
        "manifest": "manifest.json",
        "bytes": 2497280256,
        "sha256": "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5",
    },
    "8b": {
        "alias": "Qwen3-8B",
        "manifest": "candidates/Qwen3-8B.json",
        "bytes": 5027783488,
        "sha256": "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
    },
}


def verified_model(path: Path, profile: dict) -> bool:
    if path.stat().st_size != profile["bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest() == profile["sha256"]


def ticks(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def owned(identity: dict, root: Path) -> bool:
    try:
        pid = int(identity["pid"])
        process = Path(f"/proc/{pid}")
        return (
            process.stat().st_uid == os.getuid()
            and ticks(pid) == identity["start_ticks"]
            and Path(f"/proc/{pid}/exe").resolve() == Path(identity["binary"]).resolve()
            and Path(identity["binary"]).resolve().is_relative_to(root)
            and identity.get("service") == "sc2-harness-qwen-cpu"
            and identity.get("port") == 18891
        )
    except (OSError, ValueError, KeyError):
        return False


def api_status() -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:18891/v1/models", timeout=3) as response:
            return {"ready": True, "models": json.loads(response.read(64000))}
    except (OSError, ValueError):
        return {"ready": False}


def gpu_memory_mib(pid: int) -> int:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--id=1",
            "--query-compute-apps=pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return sum(
        int(row.split(",")[1].strip())
        for row in result.stdout.splitlines()
        if row.split(",")[0].strip() == str(pid)
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "status", "stop"))
    parser.add_argument("--root", type=Path, default=Path.home() / ".cache/sc2-harness/model")
    parser.add_argument("--device", choices=("cpu", "gpu1"), default="cpu")
    parser.add_argument("--model-profile", choices=tuple(MODEL_PROFILES), default="4b")
    parser.add_argument("--context-per-slot", type=int, choices=(8192, 16384), default=8192)
    parser.add_argument("--vram-budget-mib", type=int, choices=(12288, 16384), default=12288)
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.is_relative_to(Path.home().resolve()):
        raise SystemExit("Use the current personal home only")
    identity_path = root / "service.json"
    identity = json.loads(identity_path.read_text()) if identity_path.exists() else {}
    if args.action == "status":
        print(
            json.dumps({"owned_process_alive": owned(identity, root), "identity": identity, **api_status()})
        )
        return
    if args.action == "stop":
        if not identity:
            print(json.dumps({"stopped": False, "reason": "No personal service identity"}))
            return
        if not owned(identity, root):
            raise SystemExit("Service identity no longer matches; no process was signaled")
        pid = int(identity["pid"])
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and owned(identity, root):
            time.sleep(0.2)
        print(json.dumps({"stopped": not owned(identity, root), "pid": pid}))
        return
    if owned(identity, root):
        if bool(identity.get("gpu")) != (args.device == "gpu1"):
            raise SystemExit("Stop the owned model service before changing its CPU/GPU backend")
        if identity.get("model") != MODEL_PROFILES[args.model_profile]["alias"]:
            raise SystemExit("Stop the owned model service before changing its model profile")
        if identity.get("context_total") != 3 * args.context_per_slot:
            raise SystemExit("Stop the owned model service before changing its context size")
        if args.device == "gpu1" and identity.get("vram_budget_mib") != args.vram_budget_mib:
            raise SystemExit("Stop the owned model service before changing its GPU budget")
        print(json.dumps({"reused_existing_service": True, "identity": identity, **api_status()}))
        return
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 18891)) == 0:
            raise SystemExit("Port 18891 is occupied; no existing service was changed")
    if shutil.disk_usage(root).free < 20 * 1024**3:
        raise SystemExit("At least 20 GiB must remain available")
    base_manifest = json.loads((root / "manifest.json").read_text())
    profile = MODEL_PROFILES[args.model_profile]
    manifest = json.loads((root / profile["manifest"]).read_text())
    gpu = args.device == "gpu1"
    binary_manifest = json.loads((root / "gpu-release.json").read_text()) if gpu else base_manifest
    binary = Path(binary_manifest["binary"]).resolve()
    model = Path(manifest["model_path"]).resolve()
    if not binary.is_relative_to(root) or not model.is_relative_to(root):
        raise SystemExit("Manifest paths must stay in the personal model cache")
    if not verified_model(model, profile):
        raise SystemExit("Model size/SHA-256 does not match the pinned official profile")
    if gpu:
        fresh = subprocess.run(
            [
                "nvidia-smi",
                "--id=1",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        used, utilization = [int(value.strip()) for value in fresh.stdout.strip().split(",")]
        if used > 512 or utilization > 10:
            raise SystemExit("GPU1 has active work; no model service was started")
    command = [
        str(binary),
        "--model",
        str(model),
        "--alias",
        profile["alias"],
        "--host",
        "127.0.0.1",
        "--port",
        "18891",
        "--threads",
        "16",
        "--threads-batch",
        "16",
        "--threads-http",
        "3",
        "--parallel",
        "3",
        "--ctx-size",
        str(3 * args.context_per_slot),
        "--n-gpu-layers",
        "all" if gpu else "0",
        "--jinja",
        "--reasoning",
        "off",
        "--chat-template-kwargs",
        '{"enable_thinking":false}',
        "--timeout",
        "120",
        "--no-slots",
        "--metrics",
    ]
    if gpu:
        command.extend(
            [
                "--split-mode",
                "none",
                "--main-gpu",
                "0",
                "--device",
                "CUDA0",
                "--fit",
                "off",
                "--batch-size",
                "512",
                "--ubatch-size",
                "256",
            ]
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            (
                "ROS_",
                "AMENT_",
                "CONDA_",
                "LLAMA_ARG_",
            )
        )
        and key not in {"PYTHONPATH", "LD_LIBRARY_PATH"}
    }
    environment.update(
        {
            "CUDA_VISIBLE_DEVICES": "1" if gpu else "",
            "OMP_NUM_THREADS": "16",
            "OPENBLAS_NUM_THREADS": "16",
            "MKL_NUM_THREADS": "16",
        }
    )
    original_cpus = os.sched_getaffinity(0)
    cpus = sorted(original_cpus)[-16:]
    with (root / "service.log").open("ab") as log:
        log.write(
            (
                "\nSC2_MODEL_LAUNCH "
                + json.dumps({"time": time.time(), "device": args.device, "model": profile["alias"]})
                + "\n"
            ).encode()
        )
        os.sched_setaffinity(0, cpus)
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        finally:
            os.sched_setaffinity(0, original_cpus)
    identity = {
        "service": "sc2-harness-qwen-cpu",
        "pid": process.pid,
        "start_ticks": ticks(process.pid),
        "binary": str(binary),
        "model": profile["alias"],
        "model_profile": args.model_profile,
        "model_path": str(model),
        "model_sha256": profile["sha256"],
        "port": 18891,
        "host": "127.0.0.1",
        "threads": 16,
        "parallel": 3,
        "context_total": 3 * args.context_per_slot,
        "context_per_slot": args.context_per_slot,
        "cpus": cpus,
        "gpu": gpu,
        "physical_gpu": 1 if gpu else None,
        "vram_budget_mib": args.vram_budget_mib if gpu else None,
        "started_at": time.time(),
        "command": command,
    }
    if identity_path.exists():
        previous = json.loads(identity_path.read_text())
        (root / f"service-previous-{previous.get('pid', 'unknown')}.json").write_text(
            json.dumps(previous, indent=2)
        )
    identity_path.write_text(json.dumps(identity, indent=2) + "\n")
    deadline = time.monotonic() + 60
    peak_vram = 0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("Model server exited during startup; inspect the personal service.log")
        if gpu:
            peak_vram = max(peak_vram, gpu_memory_mib(process.pid))
        if peak_vram > args.vram_budget_mib:
            if owned(identity, root):
                os.kill(process.pid, signal.SIGTERM)
            raise SystemExit("Model startup exceeded its configured GPU budget")
        status = api_status()
        if status["ready"]:
            identity["startup_vram_peak_mib"] = peak_vram if gpu else None
            identity_path.write_text(json.dumps(identity, indent=2) + "\n")
            print(json.dumps({"started": True, "identity": identity, **status}), flush=True)
            return
        time.sleep(1)
    print(json.dumps({"started": True, "identity": identity, "ready": False, "reason": "Startup pending"}))


if __name__ == "__main__":
    main()
