from io import BytesIO

import pytest
from fastapi import HTTPException
from PIL import Image

from sc2_harness.app import create_app
from sc2_harness.runtime import RunManager
from sc2_harness.settings import Settings


def jpeg(size):
    output = BytesIO()
    Image.new("RGB", size, "blue").save(output, format="JPEG")
    return output.getvalue()


def native_frame_fixture(tmp_path):
    app = create_app(Settings(tmp_path))
    manager = app.state.manager = RunManager(Settings(tmp_path))
    run_id = "a" * 32
    root = manager.store.run_dir(run_id)
    target = root / "native-frame.jpg"
    manager.store.put({"id": run_id, "backend": "sc2", "observation": {"rendering": "rgb"},
                       "frame_path": target.name})
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/runs/{run_id}/frame")
    return endpoint, run_id, target


@pytest.mark.parametrize("old_size,new_size", [((16, 16), (512, 512)), ((512, 512), (16, 16))])
async def test_native_frame_publication_after_headers_cannot_change_body_or_length(tmp_path, old_size, new_size):
    endpoint, run_id, target = native_frame_fixture(tmp_path)
    original, replacement = jpeg(old_size), jpeg(new_size)
    assert len(original) != len(replacement)
    target.write_bytes(original)
    response = endpoint(run_id)
    messages = []
    publications = []

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            # This is exactly between FileResponse's stat/header publication and
            # its later open. A mutable FileResponse sends the replacement frame
            # with the old length; the bounded Response keeps its original bytes.
            staging = target.with_suffix(".next.jpg")
            staging.write_bytes(replacement)
            staging.replace(target)
            publications.append(True)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await response({"type": "http", "method": "GET"}, receive, send)
    headers = dict(next(message["headers"] for message in messages if message["type"] == "http.response.start"))
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    assert publications == [True] and target.read_bytes() == replacement
    assert body == original
    assert int(headers[b"content-length"]) == len(body) == len(original)
    assert headers[b"content-type"] == b"image/jpeg"


def test_native_frame_read_is_bounded_and_empty_frame_is_not_published(tmp_path, monkeypatch):
    endpoint, run_id, target = native_frame_fixture(tmp_path)
    target.write_bytes(jpeg((16, 16)))
    monkeypatch.setattr("sc2_harness.app.MAX_FRAME_BYTES", 32)
    with pytest.raises(HTTPException) as exc:
        endpoint(run_id)
    assert exc.value.status_code == 413
    target.write_bytes(b"")
    with pytest.raises(HTTPException) as exc:
        endpoint(run_id)
    assert exc.value.status_code == 404


def test_native_frame_disappearance_before_open_returns_404_without_a_path_leak(tmp_path, monkeypatch):
    endpoint, run_id, target = native_frame_fixture(tmp_path)
    target.write_bytes(jpeg((16, 16)))
    original_open = type(target).open

    def missing_at_open(path, *args, **kwargs):
        if path == target:
            raise FileNotFoundError(str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "open", missing_at_open)
    with pytest.raises(HTTPException) as exc:
        endpoint(run_id)
    assert exc.value.status_code == 404 and str(target) not in str(exc.value.detail)
