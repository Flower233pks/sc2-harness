"""Subprocess faults must fail closed, without relying on an installed game."""

from __future__ import annotations

import asyncio
import sys

import pytest

from sc2_harness.backends.native import NativeBackend, NativeBackendError


def worker(monkeypatch, code):
    monkeypatch.setattr(NativeBackend, "_command", staticmethod(lambda: [sys.executable, "-u", "-c", code]))


@pytest.mark.asyncio
async def test_response_identity_mismatch_terminates_worker(monkeypatch, tmp_path):
    worker(
        monkeypatch,
        "import sys,json,time; q=json.loads(input()); print(json.dumps({'id':999,'result':{}}),flush=True); time.sleep(60)",
    )
    backend = NativeBackend()
    with pytest.raises(NativeBackendError, match="identity"):
        await backend.start({}, tmp_path)
    assert backend._process.returncode is not None
    assert backend._log is None


@pytest.mark.asyncio
async def test_silent_step_timeout_terminates_worker(monkeypatch, tmp_path):
    worker(
        monkeypatch,
        "import json,time; q=json.loads(input()); print(json.dumps({'id':q['id'],'result':{}}),flush=True); input(); time.sleep(60)",
    )
    backend = NativeBackend()
    await backend.start({}, tmp_path)
    backend._timeout = 0.1
    with pytest.raises(asyncio.TimeoutError):
        await backend.step([], 8)
    assert backend._process.returncode is not None
    with pytest.raises(NativeBackendError, match="not running"):
        await backend.step([], 8)


@pytest.mark.asyncio
async def test_worker_cannot_publish_frame_outside_run(monkeypatch, tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"not a real frame")
    code = (
        "import json,time; q=json.loads(input()); "
        f"print(json.dumps({{'id':q['id'],'result':{{'frame_path':{str(outside)!r}}}}}),flush=True); "
        "time.sleep(60)"
    )
    worker(monkeypatch, code)
    backend = NativeBackend()
    with pytest.raises(NativeBackendError, match="frame artifact"):
        await backend.start({}, tmp_path / "run")
    assert backend._process.returncode is not None


@pytest.mark.asyncio
async def test_close_delivers_metadata_and_waits_for_exit(monkeypatch, tmp_path):
    worker(
        monkeypatch,
        "import json; q=json.loads(input()); print(json.dumps({'id':q['id'],'result':{}}),flush=True); q=json.loads(input()); print(json.dumps({'id':q['id'],'result':{'status':'closed','replay':None}}),flush=True)",
    )
    backend = NativeBackend()
    await backend.start({}, tmp_path)
    result = await backend.close()
    assert result == {"status": "closed", "replay": None}
    assert backend._process.returncode == 0
    assert await backend.close() == result


def test_real_worker_probe_never_claims_engine_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("SC2PATH", str(tmp_path))
    monkeypatch.setenv("SC2H_GAME_PYTHON", sys.executable)
    result = NativeBackend.probe()
    assert result["available"] is False
    assert result["native_verified"] is False
    assert result["reason"] == "sc2_executable_missing"
    assert result["sdk"] == "6.6.0"
    assert result["python"] == sys.executable


@pytest.mark.asyncio
async def test_real_worker_missing_engine_returns_protocol_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SC2PATH", str(tmp_path / "no-game"))
    backend = NativeBackend()
    with pytest.raises(NativeBackendError, match="sc2_executable_missing"):
        await backend.start({}, tmp_path / "run")
    assert backend._process.returncode is not None
    log = (tmp_path / "run" / "native-worker.log").read_text(encoding="utf-8")
    assert "Native SC2 unavailable" in log
