"""Process-isolated native SC2 adapter. Importing this module never starts a game."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


class NativeBackendError(RuntimeError):
    """The native process or its private protocol failed."""


class NativeBackend:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._run_dir: Path | None = None
        self._log: Any = None
        self._closed = False
        self._timeout = 45.0
        self._close_result: dict = {"status": "not_started", "replay": None}

    @staticmethod
    def _command() -> list[str]:
        python = os.environ.get("SC2H_GAME_PYTHON") or sys.executable
        return [python, "-u", "-m", "sc2_harness.game_worker"]

    @staticmethod
    def probe() -> dict:
        """Probe in the selected interpreter, without loading or starting SC2."""
        command = [*NativeBackend._command(), "--probe"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
            if result.returncode:
                return {"available": False, "reason": "native_probe_failed", "exit_code": result.returncode}
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise TypeError("Probe must return an object")
            return value
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as exc:
            return {"available": False, "reason": type(exc).__name__}

    async def start(self, config: dict, run_dir: Path) -> dict:
        if self._process is not None or self._closed:
            raise NativeBackendError("Native backend instances can only start once")
        self._run_dir = Path(run_dir).resolve()
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._log = (self._run_dir / "native-worker.log").open("ab", buffering=0)
        self._timeout = min(180.0, max(10.0, float(config.get("native_timeout", 45))))
        environment = {
            key: value
            for key, value in os.environ.items()
            if not any(part in key.upper() for part in ("API_KEY", "ACCESS_TOKEN", "SECRET", "PASSWORD"))
        }
        environment["PYTHONUNBUFFERED"] = "1"
        options: dict[str, Any] = (
            {"start_new_session": True}
            if os.name != "nt"
            else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            }
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self._log,
                env=environment,
                limit=4 * 1024 * 1024,
                **options,
            )
            return await self._request(
                {"op": "start", "config": config, "run_dir": str(self._run_dir)},
                timeout=180,
            )
        except BaseException:
            await self._terminate()
            raise

    async def step(self, commands: list[dict], advance_loops: int) -> dict:
        if (
            isinstance(advance_loops, bool)
            or not isinstance(advance_loops, int)
            or not 1 <= advance_loops <= 2240
        ):
            raise ValueError("advance_loops must be an integer from 1 to 2240")
        return await self._request({"op": "step", "commands": commands, "advance_loops": advance_loops})

    async def _request(self, payload: dict, timeout: float | None = None) -> dict:
        async with self._lock:
            process = self._process
            if process is None or process.returncode is not None or self._closed:
                raise NativeBackendError("Native worker is not running")
            self._seq += 1
            message = {"id": self._seq, **payload}
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write((json.dumps(message, allow_nan=False) + "\n").encode("utf-8"))
                await process.stdin.drain()
                raw = await asyncio.wait_for(process.stdout.readline(), timeout or self._timeout)
                if not raw:
                    raise NativeBackendError("Native worker exited before replying; see native-worker.log")
                response = json.loads(raw)
                if not isinstance(response, dict) or response.get("id") != self._seq:
                    raise NativeBackendError("Native worker returned an invalid response identity")
                if response.get("error"):
                    error = response["error"]
                    raise NativeBackendError(str(error.get("message", "Native game operation failed")))
                result = response.get("result")
                if not isinstance(result, dict):
                    raise NativeBackendError("Native worker returned an invalid result")
                frame_path = result.get("frame_path")
                if frame_path is not None:
                    candidate = Path(frame_path).resolve()
                    if (
                        self._run_dir is None
                        or not candidate.is_relative_to(self._run_dir)
                        or not candidate.is_file()
                    ):
                        raise NativeBackendError("Native worker returned an invalid frame artifact")
                return result
            except BaseException:
                await self._terminate()
                raise

    async def close(self) -> dict:
        if self._closed:
            return self._close_result
        try:
            if self._process is not None and self._process.returncode is None:
                self._close_result = await self._request({"op": "close"}, timeout=20)
                await asyncio.wait_for(self._process.wait(), 5)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - cleanup must survive SDK/protocol errors
            self._close_result = {"status": "forced_close", "replay": None, "error": type(exc).__name__}
        finally:
            await self._terminate()
        return self._close_result

    async def _terminate(self) -> None:
        process = self._process
        if process is not None:
            if os.name != "nt":
                # This worker is a new session. Its SC2 child belongs to this exact group.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    pass
                # The worker may exit before a stuck native descendant; reap the whole owned group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            if process.returncode is None:
                await process.wait()
        self._closed = True
        if self._log is not None:
            self._log.close()
            self._log = None
