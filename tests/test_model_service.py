"""Offline launcher contracts: no subprocess, socket, API or GPU is accessed."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[1] / "scripts" / "model-service.py"
    spec = importlib.util.spec_from_file_location("sc2_test_model_service", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_launcher(launcher, tmp_path, monkeypatch):
    root = tmp_path / "model"
    root.mkdir()
    (root / "candidates").mkdir()
    cpu_binary, gpu_binary = root / "cpu" / "llama-server", root / "gpu" / "llama-server"
    for name, value in (
        ("manifest.json", {"binary": str(cpu_binary), "model_path": str(root / "4b.gguf")}),
        ("gpu-release.json", {"binary": str(gpu_binary)}),
        ("candidates/Qwen3-8B.json", {"model_path": str(root / "8b.gguf")}),
    ):
        (root / name).write_text(json.dumps(value), encoding="utf-8")
    state = {"pid": 4321, "alive": True, "owned": True, "popen": [], "signals": [],
             "affinity": [], "nvidia": [], "queries": [], "verified": [], "vram": 4096}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def connect_ex(self, address):
            state["queries"].append(address)
            return 111

    def popen(command, **kwargs):
        state["popen"].append((command, kwargs))
        return SimpleNamespace(pid=state["pid"], poll=lambda: None)

    def nvidia(command, **kwargs):
        assert command[0] == "nvidia-smi" and "--id=1" in command
        state["nvidia"].append((command, kwargs))
        return SimpleNamespace(stdout="30, 0\n")

    def kill(pid, sig):
        state["signals"].append((pid, sig))
        state["alive"] = False

    def verify(path, profile):
        state["verified"].append((path, profile))
        return True

    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(launcher.socket, "socket", FakeSocket)
    monkeypatch.setattr(launcher.shutil, "disk_usage", lambda _path: SimpleNamespace(free=30 * 1024**3))
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher.subprocess, "run", nvidia)
    monkeypatch.setattr(launcher.os, "sched_getaffinity", lambda _pid: set(range(32)), raising=False)
    monkeypatch.setattr(launcher.os, "sched_setaffinity",
                        lambda pid, cpus: state["affinity"].append((pid, set(cpus))), raising=False)
    monkeypatch.setattr(launcher.os, "kill", kill)
    monkeypatch.setattr(launcher, "ticks", lambda _pid: "test-start-ticks")
    monkeypatch.setattr(launcher, "verified_model", verify)
    monkeypatch.setattr(launcher, "api_status", lambda: {"ready": True, "models": {"data": []}})
    monkeypatch.setattr(launcher, "gpu_memory_mib", lambda pid: state["vram"] if pid == state["pid"] else 0)
    monkeypatch.setattr(launcher, "owned", lambda identity, _root: bool(
        state["owned"] and state["alive"] and identity.get("pid") == state["pid"]))
    return launcher, root, state


@pytest.mark.parametrize("device,profile,context,budget", [
    ("cpu", "4b", 8192, 12288), ("gpu1", "8b", 16384, 16384),
])
def test_start_persists_actual_context_and_confines_device_and_affinity(
    isolated_launcher, capsys, device, profile, context, budget,
):
    module, root, state = isolated_launcher
    module.main(["start", "--root", str(root), "--device", device, "--model-profile", profile,
                 "--context-per-slot", str(context), "--vram-budget-mib", str(budget)])
    output = json.loads(capsys.readouterr().out)
    identity = json.loads((root / "service.json").read_text())
    command, kwargs = state["popen"][0]
    assert output["started"] is True and output["ready"] is True
    assert identity == output["identity"]
    assert identity["context_per_slot"] == context
    assert identity["context_total"] == 3 * context
    assert command[command.index("--ctx-size") + 1] == str(3 * context)
    assert command[command.index("--parallel") + 1] == "3"
    assert identity["vram_budget_mib"] == (budget if device == "gpu1" else None)
    assert identity["physical_gpu"] == (1 if device == "gpu1" else None)
    assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ("1" if device == "gpu1" else "")
    assert kwargs["cwd"] == root and kwargs["start_new_session"] is True
    assert state["affinity"] == [(0, set(range(16, 32))), (0, set(range(32)))]
    assert state["queries"] == [("127.0.0.1", 18891)]
    assert len(state["nvidia"]) == (1 if device == "gpu1" else 0)
    assert state["verified"][0][1] is module.MODEL_PROFILES[profile]
    assert state["signals"] == []


def live_identity(module, root, state, **changes):
    identity = {"service": "sc2-harness-qwen-cpu", "pid": state["pid"], "start_ticks": "test-start-ticks",
                "binary": str(root / "gpu" / "llama-server"), "model": "Qwen3-8B", "gpu": True,
                "port": 18891, "context_total": 24576, "context_per_slot": 8192, "vram_budget_mib": 12288}
    identity.update(changes)
    (root / "service.json").write_text(json.dumps(identity), encoding="utf-8")
    return identity


@pytest.mark.parametrize("context,budget,message", [
    (16384, 12288, "context size"), (8192, 16384, "GPU budget"),
])
def test_reuse_with_changed_context_or_gpu_budget_requires_explicit_stop(
    isolated_launcher, context, budget, message,
):
    module, root, state = isolated_launcher
    before = live_identity(module, root, state)
    with pytest.raises(SystemExit, match=message):
        module.main(["start", "--root", str(root), "--device", "gpu1", "--model-profile", "8b",
                     "--context-per-slot", str(context), "--vram-budget-mib", str(budget)])
    assert state["popen"] == state["signals"] == state["nvidia"] == []
    assert json.loads((root / "service.json").read_text()) == before


def test_reuse_identical_context_does_not_start_or_signal_another_process(isolated_launcher, capsys):
    module, root, state = isolated_launcher
    before = live_identity(module, root, state)
    module.main(["start", "--root", str(root), "--device", "gpu1", "--model-profile", "8b"])
    output = json.loads(capsys.readouterr().out)
    assert output["reused_existing_service"] is True
    assert output["identity"] == before
    assert state["popen"] == state["signals"] == state["nvidia"] == []


@pytest.mark.parametrize("option,value", [
    ("--context-per-slot", "4096"), ("--context-per-slot", "32768"),
    ("--vram-budget-mib", "10000"), ("--vram-budget-mib", "20000"),
])
def test_unsupported_context_or_vram_arguments_never_launch(isolated_launcher, option, value):
    module, root, state = isolated_launcher
    with pytest.raises(SystemExit) as failure:
        module.main(["start", "--root", str(root), option, value])
    assert failure.value.code == 2
    assert state["popen"] == state["signals"] == []
    assert not (root / "service.json").exists()


@pytest.mark.parametrize("still_owned", [False, True])
def test_vram_cap_only_signals_the_exact_process_while_ownership_is_still_verified(
    isolated_launcher, still_owned,
):
    module, root, state = isolated_launcher
    state["vram"], state["owned"] = 16385, still_owned
    with pytest.raises(SystemExit, match="exceeded its configured GPU budget") as failure:
        module.main(["start", "--root", str(root), "--device", "gpu1", "--model-profile", "8b",
                     "--context-per-slot", "16384", "--vram-budget-mib", "16384"])
    assert len(state["popen"]) == 1
    assert state["signals"] == ([(state["pid"], module.signal.SIGTERM)] if still_owned else [])
    assert "was stopped" not in str(failure.value)


def test_stop_refuses_a_stale_or_foreign_identity_without_signaling(isolated_launcher):
    module, root, state = isolated_launcher
    live_identity(module, root, state)
    state["owned"] = False
    with pytest.raises(SystemExit, match="no process was signaled"):
        module.main(["stop", "--root", str(root)])
    assert state["signals"] == state["popen"] == []


@pytest.mark.parametrize("mismatch", [None, "uid", "ticks", "exe", "outside_root", "service", "port"])
def test_real_ownership_predicate_requires_uid_start_ticks_executable_and_cache_scope(
    launcher, tmp_path, monkeypatch, mismatch,
):
    module, pid, real_path = launcher, 4321, Path
    root = tmp_path / "cache"
    binary = root / "bin" / "llama-server"
    actual_binary = binary
    if mismatch == "outside_root":
        binary = actual_binary = tmp_path / "unrelated" / "llama-server"
    if mismatch == "exe":
        actual_binary = root / "different" / "llama-server"
    identity = {"pid": pid, "start_ticks": "expected", "binary": str(binary),
                "service": "another-project" if mismatch == "service" else "sc2-harness-qwen-cpu",
                "port": 12345 if mismatch == "port" else 18891}

    def proc_path(value):
        if str(value) == f"/proc/{pid}":
            return SimpleNamespace(stat=lambda: SimpleNamespace(st_uid=2000 if mismatch == "uid" else 1000))
        if str(value) == f"/proc/{pid}/exe":
            return SimpleNamespace(resolve=lambda: actual_binary.resolve())
        return real_path(value)

    monkeypatch.setattr(module, "Path", proc_path)
    monkeypatch.setattr(module.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(module, "ticks", lambda _pid: "reused-pid" if mismatch == "ticks" else "expected")
    assert module.owned(identity, root) is (mismatch is None)
