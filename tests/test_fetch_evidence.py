import hashlib
import json

import httpx
import pytest

from scripts import fetch_evidence as fetch

RUN_ID = "a" * 32
ORIGIN = "http://workbench.test:18890"
FILES = {"config.json": b'{"seed":7}', "run.json": b'{"status":"failed"}', "metrics.json": b'{"model_calls":24}',
         "events.jsonl": b'{"seq":1}\n', "snapshots.jsonl": b'{"seq":1}\n', "match.SC2Replay": b"native-replay"}


def handler_for(files, requests, *, bad_name=None, frame=None):
    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == "workbench.test"
        assert "authorization" not in request.headers
        if request.url.path.endswith("/artifacts"):
            artifacts = [{"name": name, "url": "https://user:password@unrelated.test/file?token=secret"}
                         for name in files]
            if bad_name:
                artifacts.append({"name": bad_name})
            return httpx.Response(200, json={"artifacts": artifacts})
        if request.url.path.endswith("/frame"):
            return httpx.Response(404) if frame is None else httpx.Response(
                200, content=frame, headers={"content-type": "image/png"})
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=files[name])
    return handler


def test_downloads_allowlisted_bytes_and_hashes_without_trusting_advertised_urls(tmp_path):
    requests = []
    directory = fetch.fetch_evidence(ORIGIN, RUN_ID, tmp_path / "capture", include_frame=True,
        transport=httpx.MockTransport(handler_for(FILES, requests)))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is True and manifest["frame"]["status"] == "unavailable"
    assert len(manifest["files"]) == len(FILES)
    for item in manifest["files"]:
        assert (directory / item["name"]).read_bytes() == FILES[item["name"]]
        assert item["sha256"] == hashlib.sha256(FILES[item["name"]]).hexdigest()
        assert item["size"] == len(FILES[item["name"]])
        assert item["source"].startswith(ORIGIN + "/api/")
        assert "?" not in item["source"] and "@" not in item["source"]
    assert all("/control" not in request.url.path for request in requests)
    assert not list(directory.glob(".*.tmp"))


@pytest.mark.parametrize("url", ["http://user:pass@workbench.test", "http://workbench.test?token=secret",
                                 "http://workbench.test/path", "http://workbench.test/#fragment"])
def test_rejects_credentialed_or_ambiguous_source_before_download(tmp_path, url):
    with pytest.raises(ValueError):
        fetch.fetch_evidence(url, RUN_ID, tmp_path / "capture")
    assert not (tmp_path / "capture").exists()


@pytest.mark.parametrize("run_id", ["../outside", "a" * 31, "a" * 33, "g" * 32])
def test_rejects_invalid_run_identity(tmp_path, run_id):
    with pytest.raises(ValueError):
        fetch.fetch_evidence(ORIGIN, run_id, tmp_path / "capture")
    assert not (tmp_path / "capture").exists()


@pytest.mark.parametrize("name", ["../outside.json", "nested/run.json", ".env", "CON.SC2Replay", "run.json "])
def test_rejects_unapproved_artifact_names(tmp_path, name):
    requests = []
    with pytest.raises(fetch.EvidenceError):
        fetch.fetch_evidence(ORIGIN, RUN_ID, tmp_path / "capture",
            transport=httpx.MockTransport(handler_for({}, requests, bad_name=name)))
    assert len(requests) == 1
    manifest = json.loads((tmp_path / "capture/manifest.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False and manifest["errors"]
    assert not (tmp_path / "outside.json").exists()


def test_streaming_limit_preserves_existing_file_and_removes_temporary(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_FILE_BYTES", 16)
    directory = tmp_path / "capture"
    directory.mkdir()
    (directory / "events.jsonl").write_bytes(b"old-evidence")

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"a" * 10
            yield b"b" * 10

    def handler(request):
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json={"artifacts": [{"name": "events.jsonl"}]})
        return httpx.Response(200, stream=Chunks())

    with pytest.raises(fetch.EvidenceError):
        fetch.fetch_evidence(ORIGIN, RUN_ID, directory, transport=httpx.MockTransport(handler))
    assert (directory / "events.jsonl").read_bytes() == b"old-evidence"
    assert not list(directory.glob(".*.tmp"))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert not manifest["complete"] and manifest["errors"]


def test_destination_file_symlink_cannot_overwrite_outside(tmp_path):
    directory = tmp_path / "capture"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"unchanged")
    try:
        (directory / "run.json").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires additional permission on this Windows host")
    requests = []
    with pytest.raises(fetch.EvidenceError):
        fetch.fetch_evidence(ORIGIN, RUN_ID, directory,
            transport=httpx.MockTransport(handler_for(FILES, requests)))
    assert outside.read_bytes() == b"unchanged"
    assert not list(directory.glob(".*.tmp"))


def test_optional_frame_is_saved_with_its_actual_type(tmp_path):
    png = bytes.fromhex("89504e470d0a1a0a") + b"fixture-frame"
    directory = fetch.fetch_evidence(ORIGIN, RUN_ID, tmp_path / "capture", include_frame=True,
        transport=httpx.MockTransport(handler_for(FILES, [], frame=png)))
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["frame"]["status"] == "downloaded"
    assert (directory / "native-frame.png").read_bytes() == png
    assert next(item for item in manifest["files"] if item["name"] == "native-frame.png")["sha256"] == (
        hashlib.sha256(png).hexdigest())


def test_redirect_is_not_followed_or_copied_with_credentials_to_manifest(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://user:password@unrelated.test/?token=secret"})

    with pytest.raises(fetch.EvidenceError):
        fetch.fetch_evidence(ORIGIN, RUN_ID, tmp_path / "capture", transport=httpx.MockTransport(handler))
    assert len(requests) == 1 and requests[0].url.host == "workbench.test"
    text = (tmp_path / "capture/manifest.json").read_text(encoding="utf-8")
    assert "password" not in text and "token=secret" not in text and "unrelated.test" not in text
