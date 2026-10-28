"""Private JSONL subprocess entry point; never import a model client here."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback

from sc2_harness.native_worker import NativeSession, probe


async def serve(output) -> None:
    session = NativeSession()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            identity = None
            close = False
            try:
                if len(line) > 1024 * 1024:
                    raise ValueError("Request exceeds protocol limit")
                request = json.loads(line)
                identity = request["id"]
                operation = request["op"]
                if operation == "start":
                    result = await session.start(request["config"], request["run_dir"])
                elif operation == "step":
                    result = await session.step(request["commands"], request["advance_loops"])
                elif operation == "close":
                    result = await session.close()
                    close = True
                else:
                    raise ValueError("Unknown operation")
                response = {"id": identity, "result": result}
            except Exception as exc:  # noqa: BLE001 - private process RPC error boundary
                traceback.print_exc(file=sys.stderr)
                response = {"id": identity, "error": {"type": type(exc).__name__, "message": str(exc)}}
            output.write(json.dumps(response, allow_nan=False) + "\n")
            output.flush()
            if close:
                break
    finally:
        await session.close()


def main() -> None:
    if "--probe" in sys.argv:
        print(json.dumps(probe(), allow_nan=False))
        return
    # Keep one private pipe fd for JSON. Native SC2 inherits fd 1, so redirect the OS
    # fd as well as Python stdout before any SDK/game process is imported or launched.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1) as protocol_output:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr
        asyncio.run(serve(protocol_output))


if __name__ == "__main__":
    main()
