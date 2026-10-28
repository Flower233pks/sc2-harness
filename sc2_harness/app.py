"""Private, same-origin HTTP/WS observatory. Bind to loopback by default."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from sc2_harness import __version__
from sc2_harness.models import Control, RunConfig
from sc2_harness.runtime import RunConflict, RunManager
from sc2_harness.settings import Settings

EXPORTS = {"run.json", "config.json", "metrics.json", "events.jsonl", "snapshots.jsonl"}
MAX_FRAME_BYTES = 16 * 1024 * 1024


def same_origin(origin: str | None, host: str) -> bool:
    if origin is None:
        return True  # CLI clients; browsers supply Origin for cross-origin mutation/WS.
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc == host


def create_app(settings: Settings | None = None, *, backend_factory=None, policy_factory=None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = RunManager(settings, backend_factory, policy_factory)
        yield
        await app.state.manager.shutdown()

    app = FastAPI(title="SC2 Harness", version=__version__, lifespan=lifespan)
    web = Path(__file__).parent / "web"
    web.mkdir(exist_ok=True)
    app.mount("/assets", StaticFiles(directory=web), name="assets")

    @app.middleware("http")
    async def private_observatory(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not same_origin(
                request.headers.get("origin"), request.headers.get("host", "")):
            return JSONResponse({"detail": "Cross-origin experiment control is not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def manager() -> RunManager:
        return app.state.manager

    def get_run(run_id: str) -> dict:
        try:
            return manager().get(run_id)
        except KeyError:
            raise HTTPException(404, "Run not found") from None

    @app.get("/")
    def index():
        index_path = web / "index.html"
        if not index_path.is_file():
            return JSONResponse({"detail": "Observatory assets are not installed"}, status_code=503)
        return FileResponse(index_path)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "capabilities": manager().capabilities(),
                "model": settings.public_model()}

    @app.get("/api/capabilities")
    def capabilities():
        return manager().capabilities()

    @app.get("/api/runs")
    def runs():
        fields = ("id", "name", "status", "created_at", "updated_at", "round", "backend", "policy", "scenario",
                  "map_name", "result", "error")
        return {"runs": [{key: run.get(key) for key in fields} for run in manager().store.list()]}

    @app.post("/api/runs", status_code=201)
    async def create(config: RunConfig):
        try:
            return await manager().create(config)
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/runs/{run_id}")
    def detail(run_id: str):
        return get_run(run_id)

    @app.post("/api/runs/{run_id}/control")
    async def control(run_id: str, body: Control):
        get_run(run_id)
        try:
            return await manager().control(run_id, body.action)
        except RunConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        get_run(run_id)
        return manager().store.page("events", run_id, after, limit)

    @app.get("/api/runs/{run_id}/snapshots")
    def snapshots(run_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        get_run(run_id)
        return manager().store.page("snapshots", run_id, after, limit)

    @app.get("/api/runs/{run_id}/frame")
    def frame(run_id: str):
        run = get_run(run_id)
        if run.get("backend") != "sc2" or run.get("observation", {}).get("rendering") != "rgb" or not run.get("frame_path"):
            raise HTTPException(404, "No native RGB frame is available; tactical view is separate")
        root = manager().store.run_dir(run_id).resolve()
        target = (root / run["frame_path"]).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise HTTPException(404, "Frame not found")
        # Native publication atomically replaces this path. FileResponse stats it
        # before opening it, so its Content-Length can describe a different frame.
        # Open once and freeze bounded bytes before generating response headers.
        try:
            with target.open("rb") as source:
                image = source.read(MAX_FRAME_BYTES + 1)
        except OSError:
            raise HTTPException(404, "Frame not found") from None
        if not image:
            raise HTTPException(404, "Frame not found")
        if len(image) > MAX_FRAME_BYTES:
            raise HTTPException(413, "Native RGB frame exceeds the size limit")
        return Response(image, media_type="image/png" if target.suffix.lower() == ".png" else "image/jpeg")

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str):
        get_run(run_id)
        manager().export(run_id)
        root = manager().store.run_dir(run_id).resolve()
        files = [item for item in root.iterdir() if item.is_file() and item.resolve().is_relative_to(root)
                 and (item.name in EXPORTS or item.suffix == ".SC2Replay")]
        return {"artifacts": [{"name": item.name, "size": item.stat().st_size,
                 "url": f"/api/runs/{run_id}/artifacts/{quote(item.name)}"} for item in sorted(files)]}

    @app.get("/api/runs/{run_id}/artifacts/{name}")
    def artifact(run_id: str, name: str):
        get_run(run_id)
        if "/" in name or "\\" in name or ".." in name or not (name in EXPORTS or name.endswith(".SC2Replay")):
            raise HTTPException(404, "Artifact not found")
        root = manager().store.run_dir(run_id).resolve()
        target = (root / name).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(404, "Artifact not found")
        return FileResponse(target, filename=name, media_type="application/octet-stream")

    @app.websocket("/api/runs/{run_id}/stream")
    async def stream(websocket: WebSocket, run_id: str):
        if not same_origin(websocket.headers.get("origin"), websocket.headers.get("host", "")):
            await websocket.close(code=1008)
            return
        try:
            run = get_run(run_id)
        except HTTPException:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        manager().subscribers[run_id].add(queue)
        try:
            await websocket.send_json({"type": "state", "data": run})
            while True:
                try:
                    value = await asyncio.wait_for(queue.get(), 10)
                except asyncio.TimeoutError:
                    value = {"type": "state", "data": get_run(run_id)}
                await websocket.send_json(value)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            manager().subscribers[run_id].discard(queue)

    return app
