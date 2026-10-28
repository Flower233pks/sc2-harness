"""The LAN spectator must never become an alternate experiment-control or export API."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sc2_viewer.app import create_app
from sc2_viewer.settings import ViewerSettings

RUN = "a" * 32
OTHER = "b" * 32


@pytest.fixture
def sample():
    return {"id": RUN, "name": "真实演示", "status": "completed", "backend": "sc2", "policy": "llm",
            "map_name": "Simple96", "model_name": "Qwen3-8B", "result": "success", "round": 43,
            "frame_path": "/home/private/latest.jpg", "error": "/home/private/error.log",
            "api_key": "DO_NOT_PUBLISH", "config": {"seed": 7, "policy": "llm", "model_api_key": "DO_NOT_PUBLISH"},
            "metrics": {"wall_seconds": 645.95, "engine_result": "Victory", "api_key": "DO_NOT_PUBLISH"},
            "observation": {"engine": "sc2", "game_time": 421.4, "rendering": "rgb", "minerals": 375,
                "engine_identity": {"native_pid": 123, "game_path": "/home/private/StarCraftII",
                                    "game_version": "4.10.0.75689", "clock": "non_realtime_manual_step"},
                "map": {"width": 96, "height": 96, "start": [1, 2], "enemy_start": [90, 90]},
                "ready_counts": {"MARINE": 27}, "api_key": "DO_NOT_PUBLISH",
                "units": [{"tag": 1, "type": "MARINE", "owner": "self", "x": 4, "y": 5,
                           "health": 45, "internal_path": "/home/private"},
                          {"tag": 2, "type": "SCV", "owner": "enemy", "x": 6, "y": 7, "display_type": 2}]},
            "agents": [{"id": "tactics", "name": "侦察作战", "goal": "攻击已见目标",
                        "last_reason": "attack; log=/home/private/a.log Authorization=Bearer-private",
                        "prompt_messages": [{"content": "DO_NOT_PUBLISH"}],
                        "last_actions": [{"tool": "attack", "args": {"x": 12, "y": 15,
                                                                       "api_key": "DO_NOT_PUBLISH"}}],
                        "stats": {"calls": 43, "provider_secret": "DO_NOT_PUBLISH"}}]}


def client_for(handler, **settings):
    return TestClient(create_app(ViewerSettings(allowed_run_ids=(RUN,), **settings),
                                 transport=httpx.MockTransport(handler)))


def test_detail_projects_actual_battle_and_removes_private_data(sample):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=sample)

    with client_for(upstream) as client:
        response = client.get(f"/api/runs/{RUN}", headers={"Authorization": "Bearer visitor-secret"})
    assert response.status_code == 200
    public = response.json()
    assert public["observation"]["ready_counts"] == {"MARINE": 27}
    assert public["observation"]["units"] == [{"tag": 1, "type": "MARINE", "owner": "self", "x": 4, "y": 5,
                                               "health": 45, "orders": []}]
    assert public["metrics"]["engine_result"] == "Victory"
    assert public["agents"][0]["last_actions"] == [{"tool": "attack", "args": {"x": 12, "y": 15}}]
    assert public["has_error"] is True
    serialized = json.dumps(public)
    for secret in ("DO_NOT_PUBLISH", "/home/private", "Authorization=Bearer-private", "native_pid",
                   "prompt_messages", "frame_path", "game_path", "visitor-secret"):
        assert secret not in serialized
    assert len(calls) == 1 and calls[0].method == "GET"
    assert "authorization" not in calls[0].headers


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "options"])
@pytest.mark.parametrize("path", ["/api/runs", f"/api/runs/{RUN}/control", f"/api/runs/{RUN}/artifacts"])
def test_mutations_never_reach_workbench(method, path):
    def forbidden(_):
        pytest.fail("Read-only viewer forwarded a mutation")

    with client_for(forbidden) as client:
        response = getattr(client, method)(path)
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


@pytest.mark.parametrize("path", [f"/api/runs/{OTHER}", f"/api/runs/{OTHER}/frame",
                                  f"/api/runs/{RUN}/artifacts", f"/api/runs/{RUN}/artifacts/run.json",
                                  "/api/capabilities", "/api/runs/%2e%2e/config", "/assets/../pyproject.toml"])
def test_unpublished_or_unregistered_paths_never_reach_workbench(path):
    def forbidden(_):
        pytest.fail("A private or traversal path reached the workbench")

    with client_for(forbidden) as client:
        response = client.get(path)
    assert response.status_code == 404


def test_list_only_explicit_ids_and_empty_defaults_reveal_nothing(sample):
    def upstream(request):
        assert request.url.path == "/api/runs"
        return httpx.Response(200, json={"runs": [sample, {"id": OTHER, "name": "unpublished"}]})

    with client_for(upstream, default_run_id=RUN) as client:
        assert [row["id"] for row in client.get("/api/runs").json()["runs"]] == [RUN]
        assert client.get("/api/viewer/config").json()["default_run_id"] == RUN
    with TestClient(create_app(transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected call")))) as client:
        assert client.get("/api/runs").json() == {"runs": []}


def test_events_and_snapshots_have_independent_field_whitelists(sample):
    def upstream(request):
        assert dict(request.url.params) == {"after": "1", "limit": "2"}
        assert request.headers.get("cookie") is None
        if request.url.path.endswith("events"):
            return httpx.Response(200, json={"events": [{"seq": 2, "type": "agent.decision", "round": 2,
                "actor_id": "tactics", "message": "进攻", "data": {"goal": "goal", "reason": "reason",
                "actions": [{"tool": "attack", "args": {"x": 2, "y": 3}}],
                "prompt_messages": [{"content": "PRIVATE-PROMPT"}], "provider_diagnostics": {"a": "SECRET"},
                "model_attempts": [{"http_body": "SECRET"}]}}], "cursor": 2})
        return httpx.Response(200, json={"snapshots": [{"seq": 2, "round": 2, "observation": sample["observation"],
                                                       "provider": "SECRET"}], "cursor": 2})

    with client_for(upstream) as client:
        events = client.get(f"/api/runs/{RUN}/events?after=1&limit=2&url=http://evil/",
                            headers={"Cookie": "secret=yes"}).json()
        snapshots = client.get(f"/api/runs/{RUN}/snapshots?after=1&limit=2").json()
    assert events["cursor"] == snapshots["cursor"] == 2
    assert events["events"][0]["data"]["actions"][0]["args"] == {"x": 2, "y": 3}
    assert snapshots["snapshots"][0]["observation"]["minerals"] == 375
    serialized = json.dumps([events, snapshots])
    assert "SECRET" not in serialized and "PRIVATE-PROMPT" not in serialized and "/home/private" not in serialized


@pytest.mark.parametrize("failure", ["offline", "redirect", "non_json", "wrong_type", "oversize"])
def test_upstream_errors_are_bounded_and_do_not_leak_addresses(failure, monkeypatch):
    import sc2_viewer.app as module

    monkeypatch.setattr(module, "MAX_JSON", 80)
    # Function defaults are bound when create_app is defined, so simulate a
    # server-advertised large result rather than allocating huge input.
    def upstream(request):
        if failure == "offline":
            raise httpx.ConnectError("http://127.0.0.1:18890 /home/private", request=request)
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "http://evil.example/secret"})
        if failure == "non_json":
            return httpx.Response(200, text="private server error /home/private")
        if failure == "wrong_type":
            return httpx.Response(200, json=["private"])
        return httpx.Response(200, headers={"content-length": "999999999"}, text="{}")

    with client_for(upstream) as client:
        response = client.get(f"/api/runs/{RUN}")
    assert response.status_code in {502, 503}
    assert "127.0.0.1" not in response.text and "/home/private" not in response.text
    assert "evil.example" not in response.text


def test_frame_preserves_native_bytes_but_rejects_html():
    png = b"\x89PNG\r\n\x1a\nreal-frame"
    replies = iter([httpx.Response(200, content=png, headers={"content-type": "image/png"}),
                    httpx.Response(200, text="<script>secret</script>", headers={"content-type": "image/jpeg"})])
    with client_for(lambda _: next(replies)) as client:
        response = client.get(f"/api/runs/{RUN}/frame")
        assert response.content == png and response.headers["content-type"] == "image/png"
        assert client.get(f"/api/runs/{RUN}/frame").status_code == 502


@pytest.mark.parametrize("upstream", ["http://example.com:18890", "http://127.0.0.1:18891",
    "https://127.0.0.1:18890", "http://user:secret@localhost:18890", "http://localhost:18890/private",
    "http://localhost:18890/?url=evil"])
def test_settings_rejects_upstream_ssrf_or_model_endpoint(upstream):
    with pytest.raises(ValueError, match="loopback"):
        ViewerSettings(upstream=upstream)


@pytest.mark.parametrize("folder", ["../runtime", "/etc", "C:/private", "folder\\..\\runtime", "safe/../private"])
def test_recording_mappings_reject_path_escape(tmp_path, folder):
    with pytest.raises(ValueError, match="relative"):
        ViewerSettings((RUN,), recordings_root=tmp_path, recordings={RUN: folder})


def test_only_explicit_media_files_and_safe_manifest_are_served(tmp_path):
    media = tmp_path / "published"
    media.mkdir()
    Image.new("RGB", (4, 4), "red").save(media / "recording.webp")
    (media / "manifest.json").write_text(json.dumps({"run_id": RUN, "unique_frames": 2,
        "recording_type": "native_wall_clock_samples", "source": "http://127.0.0.1:18890/private",
        "samples": [{"path": "/home/private/frame.jpg"}], "api_key": "SECRET"}), encoding="utf-8")
    (media / "native.SC2Replay").write_bytes(b"private replay")
    with client_for(lambda _: pytest.fail("Media must not call upstream"), recordings_root=tmp_path,
                    recordings={RUN: "published"}) as client:
        response = client.get(f"/api/runs/{RUN}/recording.webp")
        assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
        assert client.get(f"/api/runs/{RUN}/recording-manifest").json() == {
            "run_id": RUN, "unique_frames": 2, "recording_type": "native_wall_clock_samples"}
        assert client.get(f"/api/runs/{RUN}/native.SC2Replay").status_code == 404
        assert client.get(f"/api/runs/{OTHER}/recording.webp").status_code == 404


def test_media_symlink_escape_is_denied(tmp_path):
    private = tmp_path / "private.webp"
    private.write_bytes(b"RIFF0000WEBPsecret")
    media = tmp_path / "published"
    media.mkdir()
    try:
        (media / "recording.webp").symlink_to(private)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this user")
    with client_for(lambda _: pytest.fail("Unexpected upstream"), recordings_root=tmp_path,
                    recordings={RUN: "published"}) as client:
        assert client.get(f"/api/runs/{RUN}/recording.webp").status_code == 404


def test_local_health_does_not_probe_game_or_model_and_static_assets_are_own():
    with client_for(lambda _: pytest.fail("Health/assets must not call upstream")) as client:
        assert client.get("/api/health").json()["readonly"] is True
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        js = client.get("/assets/app.js")
        assert js.status_code == 200
        assert "RunManager" not in js.text
        assert client.get("/assets/../../.env").status_code == 404


def test_cli_maps_explicit_flags_without_loading_admin_runtime(tmp_path, monkeypatch):
    module = importlib.import_module("sc2_viewer.__main__")
    called = {}
    monkeypatch.setattr(module, "create_app", lambda settings: called.setdefault("settings", settings))
    monkeypatch.setattr(module.uvicorn, "run", lambda app, **kwargs: called.update(kwargs))
    module.main(["--allow-run-id", RUN, "--default-run-id", RUN, "--host", "127.0.0.1", "--port", "8903",
                 "--recordings-root", str(tmp_path), "--recording", f"{RUN}=published"])
    assert called["host"] == "127.0.0.1" and called["port"] == 8903
    assert called["settings"].recordings == {RUN: "published"}
    source = (Path(__file__).parents[1] / "sc2_viewer" / "app.py").read_text(encoding="utf-8")
    assert "from sc2_harness" not in source and "import sc2_harness" not in source


def mp4_header(*, brand=b"isom", extended=False, padding=False):
    # Header-only fixture: serving validates the container type, not decoding.
    body = brand + b"\0\0\0\1" + b"isommp42"
    ftyp = (b"\0\0\0\1ftyp" + (len(body) + 16).to_bytes(8, "big") if extended
            else (len(body) + 8).to_bytes(4, "big") + b"ftyp") + body
    prefix = b"\0\0\0\x0cfree\0\0\0\0" if padding else b""
    return prefix + ftyp + b"\0\0\0\x0cmoovdata\0\0\0\x0cmdatdata"


@pytest.mark.parametrize("extended,padding", [(False, False), (True, False), (False, True)])
def test_mp4_native_file_response_supports_range_seeking(tmp_path, extended, padding):
    folder = tmp_path / "published"
    folder.mkdir()
    data = mp4_header(extended=extended, padding=padding)
    (folder / "recording.mp4").write_bytes(data)
    with client_for(lambda _: pytest.fail("MP4 must not request workbench data"), recordings_root=tmp_path,
                    recordings={RUN: "published"}) as client:
        full = client.get(f"/api/runs/{RUN}/recording.mp4")
        assert full.status_code == 200 and full.content == data
        assert full.headers["content-type"] == "video/mp4"
        assert "media-src 'self'" in full.headers["content-security-policy"]
        seek = client.get(f"/api/runs/{RUN}/recording.mp4", headers={"Range": "bytes=16-27"})
        assert seek.status_code == 206 and seek.content == data[16:28]
        assert seek.headers["content-range"] == f"bytes 16-27/{len(data)}"
        suffix = client.get(f"/api/runs/{RUN}/recording.mp4", headers={"Range": "bytes=-8"})
        assert suffix.status_code == 206 and suffix.content == data[-8:]
        assert client.get(f"/api/runs/{RUN}/recording.mp4", headers={"Range": "bytes=9999-"}).status_code == 416


def test_video_available_requires_an_actual_validated_file(tmp_path, sample):
    folder = tmp_path / "published"
    folder.mkdir()
    target = folder / "recording.mp4"
    with client_for(lambda _: httpx.Response(200, json=sample), recordings_root=tmp_path,
                    recordings={RUN: "published"}) as client:
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is False
        assert client.get(f"/api/runs/{RUN}/recording.mp4").status_code == 404
        target.write_bytes(b"not a video")
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is False
        target.write_bytes(mp4_header())
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is True
        target.unlink()
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is False
    with client_for(lambda _: httpx.Response(200, json=sample)) as client:
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is False


@pytest.mark.parametrize("invalid", [b"<html>private</html>", b"\0\0\0\x04ftypisom",
    b"\0\0\x10\x00ftypisom\0\0\0\0", b"\0\0\0\x10ftypavif\0\0\0\0",
    b"\0\0\0\x08ftyp", b"\0\0\0\1ftyp\0\0\0\0"])
def test_mp4_bad_headers_are_never_published(tmp_path, sample, invalid):
    folder = tmp_path / "published"
    folder.mkdir()
    (folder / "recording.mp4").write_bytes(invalid)
    with client_for(lambda _: httpx.Response(200, json=sample), recordings_root=tmp_path,
                    recordings={RUN: "published"}) as client:
        assert client.get(f"/api/runs/{RUN}/recording.mp4").status_code == 404
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is False


def test_mp4_size_bound_and_unpublished_run_are_enforced(tmp_path, monkeypatch, sample):
    import sc2_viewer.app as module

    folder = tmp_path / "published"
    folder.mkdir()
    data = mp4_header()
    (folder / "recording.mp4").write_bytes(data)
    monkeypatch.setattr(module, "MAX_RECORDING", len(data) - 1)
    with client_for(lambda _: httpx.Response(200, json=sample), recordings_root=tmp_path,
                    recordings={RUN: "published"}) as client:
        assert client.get(f"/api/runs/{RUN}/recording.mp4").status_code == 413
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is False
        assert client.get(f"/api/runs/{OTHER}/recording.mp4").status_code == 404
        assert client.get(f"/api/runs/{RUN}/../recording.mp4").status_code == 404


def test_mp4_symlink_is_unavailable_in_both_detail_and_download(tmp_path, sample):
    private = tmp_path / "private.mp4"
    private.write_bytes(mp4_header())
    folder = tmp_path / "published"
    folder.mkdir()
    try:
        (folder / "recording.mp4").symlink_to(private)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this user")
    with client_for(lambda _: httpx.Response(200, json=sample), recordings_root=tmp_path,
                    recordings={RUN: "published"}) as client:
        assert client.get(f"/api/runs/{RUN}/recording.mp4").status_code == 404
        assert client.get(f"/api/runs/{RUN}").json()["video_available"] is False
