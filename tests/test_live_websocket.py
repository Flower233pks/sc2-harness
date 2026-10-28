"""Exercise the real ASGI server transport, not only the in-process TestClient."""
import asyncio
import json
import socket

import httpx
import uvicorn
from websockets.asyncio.client import connect

from sc2_harness.app import create_app
from sc2_harness.settings import Settings


async def test_uvicorn_transports_initial_state_and_persisted_events(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(Settings(tmp_path)), log_level="error",
                                          lifespan="on", ws="websockets-sansio"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async def started():
            while not server.started:
                await asyncio.sleep(.01)
        await asyncio.wait_for(started(), 5)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            response = await client.post("/api/runs", json={"backend": "sandbox", "max_rounds": 30})
            assert response.status_code == 201
            run_id = response.json()["id"]
            async with connect(f"ws://127.0.0.1:{port}/api/runs/{run_id}/stream",
                               origin=f"http://127.0.0.1:{port}") as stream:
                first = json.loads(await asyncio.wait_for(stream.recv(), 3))
                assert first["type"] == "state" and first["data"]["id"] == run_id
                await client.post(f"/api/runs/{run_id}/control", json={"action": "pause"})
                async def pause_event():
                    while True:
                        value = json.loads(await stream.recv())
                        if value["type"] == "event" and value["data"]["type"] == "run.pause_requested":
                            return
                await asyncio.wait_for(pause_event(), 3)
                await client.post(f"/api/runs/{run_id}/control", json={"action": "stop"})
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()
