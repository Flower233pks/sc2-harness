"""A GET-only, bounded proxy over explicitly published experiment IDs."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from sc2_viewer import __version__, projection
from sc2_viewer.settings import ViewerSettings

MAX_JSON = 12 * 1024 * 1024
MAX_FRAME = 4 * 1024 * 1024
MAX_RECORDING = 64 * 1024 * 1024
MAX_MANIFEST = 256 * 1024


def _media_file(settings: ViewerSettings, run_id: str, filename: str) -> Path:
    folder = settings.recordings.get(run_id)
    if settings.recordings_root is None or folder is None:
        raise HTTPException(404, "No recording is published for this run")
    root = settings.recordings_root.absolute()
    target = root / folder / filename
    # Refuse links in every path component, including a linked root; publishing
    # a folder must never grant access outside the explicit media tree.
    for item in (target, *target.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise HTTPException(404, "Recording not found")
        if item == root.parent:
            break
    try:
        resolved_root, resolved = root.resolve(strict=True), target.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise HTTPException(404, "Recording not found")
    except (OSError, RuntimeError):
        raise HTTPException(404, "Recording not found") from None
    return resolved


def create_app(settings: ViewerSettings | None = None, *, transport=None) -> FastAPI:
    """No database, model, subprocess or experiment manager is constructed."""
    settings = settings or ViewerSettings()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url=settings.upstream.rstrip("/"), timeout=8,
                                    trust_env=False, follow_redirects=False, transport=transport,
                                    limits=httpx.Limits(max_connections=12, max_keepalive_connections=6)) as client:
            app.state.upstream = client
            yield

    app = FastAPI(title="SC2 Harness Viewer", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    web = Path(__file__).parent / "web"
    if web.is_dir():
        app.mount("/assets", StaticFiles(directory=web, follow_symlink=False), name="assets")

    @app.middleware("http")
    async def readonly(request: Request, call_next):
        if request.method not in {"GET", "HEAD"}:
            response = JSONResponse({"detail": "This spectator service is read-only"}, status_code=405,
                                    headers={"Allow": "GET, HEAD"})
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    def allowed(run_id: str):
        if run_id not in settings.allowed_run_ids:
            raise HTTPException(404, "Published run not found")

    async def upstream(path: str, *, params=None, limit=MAX_JSON, image=False):
        # All callers construct paths from fixed route names and validated IDs.
        # Do not propagate headers, cookies, Authorization, user-supplied URLs or
        # arbitrary query parameters to the private workbench.
        try:
            async with app.state.upstream.stream("GET", path, params=params) as response:
                if response.status_code == 404:
                    raise HTTPException(404, "Published data is unavailable")
                if response.status_code != 200:
                    raise HTTPException(503, "The experiment workbench is temporarily unavailable")
                length = response.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > limit):
                    raise HTTPException(502, "Published data exceeds the viewer size limit")
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > limit:
                        raise HTTPException(502, "Published data exceeds the viewer size limit")
                if image:
                    kind = response.headers.get("content-type", "").split(";", 1)[0]
                    valid = (kind == "image/jpeg" and data.startswith(b"\xff\xd8\xff")) or \
                        (kind == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                    if not valid:
                        raise HTTPException(502, "No valid native image is available")
                    return bytes(data), kind
                try:
                    result = json.loads(data)
                except (ValueError, UnicodeError):
                    raise HTTPException(502, "The experiment workbench returned invalid data") from None
                if not isinstance(result, dict):
                    raise HTTPException(502, "The experiment workbench returned invalid data")
                return result
        except httpx.HTTPError:
            raise HTTPException(503, "The experiment workbench is temporarily unavailable") from None

    @app.get("/")
    def index():
        target = web / "index.html"
        if not target.is_file():
            raise HTTPException(503, "Spectator assets are not installed")
        return FileResponse(target)

    @app.get("/api/viewer/config")
    def config():
        return {"readonly": True, "default_run_id": settings.default, "run_ids": list(settings.allowed_run_ids),
                "poll_seconds": 2, "recording_run_ids": sorted(settings.recordings)}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "readonly": True, "version": __version__}

    @app.get("/api/runs")
    async def runs():
        if not settings.allowed_run_ids:
            return {"runs": []}
        value = await upstream("/api/runs")
        rows = value.get("runs")
        return {"runs": [projection.run(row) for row in rows[:10000]
                         if isinstance(row, dict) and row.get("id") in settings.allowed_run_ids]
                if isinstance(rows, list) else []}

    @app.get("/api/runs/{run_id}")
    async def detail(run_id: str):
        allowed(run_id)
        value = await upstream(f"/api/runs/{run_id}")
        if value.get("id") != run_id:
            raise HTTPException(502, "The experiment workbench returned a different run")
        result = projection.run(value)
        result["recording_available"] = run_id in settings.recordings
        return result

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        allowed(run_id)
        value = await upstream(f"/api/runs/{run_id}/events", params={"after": after, "limit": limit})
        rows = value.get("events")
        return {"events": [projection.event(item) for item in rows[:limit] if isinstance(item, dict)]
                if isinstance(rows, list) else [], "cursor": _cursor(value.get("cursor"), after)}

    @app.get("/api/runs/{run_id}/snapshots")
    async def snapshots(run_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        allowed(run_id)
        value = await upstream(f"/api/runs/{run_id}/snapshots", params={"after": after, "limit": limit})
        rows = value.get("snapshots")
        return {"snapshots": [projection.snapshot(item) for item in rows[:limit] if isinstance(item, dict)]
                if isinstance(rows, list) else [], "cursor": _cursor(value.get("cursor"), after)}

    @app.get("/api/runs/{run_id}/frame")
    async def frame(run_id: str):
        allowed(run_id)
        data, kind = await upstream(f"/api/runs/{run_id}/frame", limit=MAX_FRAME, image=True)
        return Response(data, media_type=kind)

    @app.get("/api/runs/{run_id}/recording.webp")
    def recording(run_id: str):
        allowed(run_id)
        target = _media_file(settings, run_id, "recording.webp")
        if target.stat().st_size > MAX_RECORDING:
            raise HTTPException(413, "Recording exceeds the viewer size limit")
        with target.open("rb") as source:
            signature = source.read(12)
        if len(signature) != 12 or signature[:4] != b"RIFF" or signature[8:] != b"WEBP":
            raise HTTPException(404, "Recording is not a WebP image")
        return FileResponse(target, media_type="image/webp", filename="recording.webp",
                            content_disposition_type="inline")

    @app.get("/api/runs/{run_id}/recording-manifest")
    def recording_manifest(run_id: str):
        allowed(run_id)
        target = _media_file(settings, run_id, "manifest.json")
        if target.stat().st_size > MAX_MANIFEST:
            raise HTTPException(413, "Recording metadata exceeds the viewer size limit")
        try:
            with target.open("rb") as source:
                raw = source.read(MAX_MANIFEST + 1)
            if len(raw) > MAX_MANIFEST:
                raise ValueError("Too large")
            value = json.loads(raw)
        except (OSError, ValueError, UnicodeError):
            raise HTTPException(404, "Recording metadata is unavailable") from None
        if not isinstance(value, dict):
            raise HTTPException(404, "Recording metadata is unavailable")
        return projection.recording_manifest(value)

    return app


def _cursor(value, fallback: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= fallback else fallback
