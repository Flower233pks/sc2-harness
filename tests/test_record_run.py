import base64
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from scripts import record_run as recorder

RUN_ID = "c" * 32
ORIGIN = "http://workbench.test:18890"
JPEG_HEADER = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcp"
    "LDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QA"
    "tRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
    "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QA"
    "tREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2"
    "Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6"
    "wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD"
)


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds

    def utc(self):
        return (datetime(2026, 10, 27, tzinfo=timezone.utc) + timedelta(seconds=self.value)).isoformat()


def jpeg(color="red"):
    # Real 2x2 JPEG fixtures keep all raw-recording tests independent of optional Pillow.
    return base64.b64decode(JPEG_HEADER + ("i6KKK+ZP3E//Z" if color == "red" else "xyiiiv3E8w//Z"))


def state(status="running", result=None, game_time=20, **changes):
    value = {"id": RUN_ID, "backend": "sc2", "status": status, "result": result, "round": 4,
             "observation": {"engine": "sc2", "game_time": game_time, "result": result},
             "metrics": {"wall_seconds": 500}}
    value.update(changes)
    return value


def record(tmp_path, handler, *, clock=None, **changes):
    timer = clock or Clock()
    options = {"interval": 2, "max_seconds": 20, "max_samples": 10,
               "clock": timer.now, "sleep": timer.sleep, "utc_now": timer.utc}
    options.update(changes)
    directory = recorder.record_run(ORIGIN, RUN_ID, tmp_path / "capture",
        transport=httpx.MockTransport(handler), **options)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return directory, manifest, timer


def test_samples_real_wall_times_deduplicates_exact_jpegs_and_preserves_terminal(tmp_path, monkeypatch):
    timer, requests = Clock(), []
    images = [jpeg(), jpeg(), jpeg("blue")]
    sample = -1
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@unreachable.invalid:1")

    def handler(request):
        nonlocal sample
        requests.append(request)
        assert request.method == "GET" and "authorization" not in request.headers
        timer.value += 0.05
        if request.url.path.endswith("/frame"):
            return httpx.Response(200, content=images[sample], headers={"content-type": "image/jpeg"})
        sample += 1
        return httpx.Response(200, json=state("completed" if sample == 2 else "running",
                             "Victory" if sample == 2 else None, game_time=sample * 10))

    directory, manifest, _ = record(tmp_path, handler, clock=timer)
    assert manifest["stop_reason"] == "terminal" and len(manifest["samples"]) == 3
    assert manifest["terminal_observation"]["engine_result"] == "Victory"
    assert manifest["terminal_observation"]["reported_wall_seconds"] == 500
    assert manifest["sampler_elapsed_wall_seconds"] == pytest.approx(4.1)
    assert manifest["coverage_start"]["elapsed_wall_seconds"] == pytest.approx(0.1)
    assert manifest["coverage_end"]["elapsed_wall_seconds"] == pytest.approx(4.1)
    assert [sample["elapsed_wall_seconds"] for sample in manifest["samples"]] == [0, 2, 4]
    assert len(manifest["unique_frames"]) == len(list((directory / "frames").iterdir())) == 2
    for sample in manifest["samples"]:
        raw = (directory / sample["frame"]["file"]).read_bytes()
        assert sample["frame"]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert sample["utc"].endswith("+00:00") and not sample["gaps"]
    assert manifest["samples"][0]["frame"]["sha256"] == manifest["samples"][1]["frame"]["sha256"]
    assert all(request.url.path in {f"/api/runs/{RUN_ID}", f"/api/runs/{RUN_ID}/frame"} for request in requests)
    assert not list(directory.rglob("*.tmp"))


@pytest.mark.parametrize("terminal,result", [("completed", "Defeat"), ("failed", "error"),
                                            ("stopped", "stopped"), ("interrupted", None)])
def test_terminal_is_preserved_even_without_frame_and_never_rewritten_as_victory(tmp_path, terminal, result):
    def handler(request):
        return httpx.Response(404) if request.url.path.endswith("/frame") else httpx.Response(
            200, json=state(terminal, result))
    _, manifest, _ = record(tmp_path, handler, encode_webp=True)
    assert manifest["stop_reason"] == "terminal"
    assert manifest["terminal_observation"]["status"] == terminal
    assert manifest["terminal_observation"]["engine_result"] == ("Defeat" if result == "Defeat" else None)
    assert manifest["samples"][0]["gaps"] == [{"source": "frame", "error": "http_404"}]
    assert manifest["animation"]["status"] == "skipped_no_frames"


def test_network_and_http_gaps_are_explicit_and_prevent_animation_across_internal_gap(tmp_path):
    sample = -1
    def handler(request):
        nonlocal sample
        if request.url.path.endswith("/frame"):
            if sample == 1:
                raise httpx.ReadTimeout("secret-provider-text-must-not-be-copied")
            return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})
        sample += 1
        return httpx.Response(200, json=state("completed" if sample == 2 else "running"))
    directory, manifest, _ = record(tmp_path, handler, encode_webp=True)
    assert manifest["samples"][1]["frame"] is None
    assert manifest["samples"][1]["gaps"] == [{"source": "frame", "error": "ReadTimeout"}]
    assert manifest["animation"]["status"] == "skipped_gaps"
    assert "secret-provider" not in (directory / "manifest.json").read_text(encoding="utf-8")
    assert len(manifest["unique_frames"]) == 1


def test_leading_startup_gap_is_omitted_from_optional_animation_and_coverage_is_explicit(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    features = pytest.importorskip("PIL.features")
    _webp = pytest.importorskip("PIL._webp")
    sample = -1
    def handler(request):
        nonlocal sample
        if request.url.path.endswith("/frame"):
            return httpx.Response(404) if sample == 0 else httpx.Response(
                200, content=jpeg("red" if sample == 1 else "blue"), headers={"content-type": "image/jpeg"})
        sample += 1
        return httpx.Response(200, json=state("starting" if sample == 0 else "completed" if sample == 2 else "running"))
    directory, manifest, _ = record(tmp_path, handler, encode_webp=True)
    assert manifest["samples"][0]["gaps"]
    assert manifest["coverage_start"]["elapsed_wall_seconds"] == 2
    assert manifest["coverage_end"]["elapsed_wall_seconds"] == 4
    if features.check("webp") and hasattr(_webp, "WebPAnimEncoder") and getattr(_webp, "HAVE_WEBPANIM", True):
        assert manifest["animation"]["status"] == "encoded"
        assert manifest["animation"]["omitted_leading_samples"] == 1
        assert manifest["animation"]["duration_ms"] == 2001
        with Image.open(directory / "recording.webp") as image:
            assert image.n_frames == 2
    else:
        assert manifest["animation"]["status"] == "webp_animation_unavailable"


def test_run_read_gap_never_uses_stale_state_or_fabricates_a_frame(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(404)
    _, manifest, _ = record(tmp_path, handler, max_samples=2)
    assert manifest["stop_reason"] == "sample_limit" and manifest["terminal_observation"] is None
    assert all(sample["state"] is None and sample["frame"] is None for sample in manifest["samples"])
    assert all(not request.url.path.endswith("/frame") for request in requests)


@pytest.mark.parametrize("value", ["http://user:secret@host", "http://host?token=secret", "http://host/path",
                                   "http://host/#x", "http://host\\other", "http://host space", "ftp://host"])
def test_unsafe_origin_rejected_before_creating_output(tmp_path, value):
    with pytest.raises(ValueError):
        recorder.record_run(value, RUN_ID, tmp_path / "capture")
    assert not (tmp_path / "capture").exists()


@pytest.mark.parametrize("run_id", ["../outside", "f" * 31, "g" * 32])
def test_bad_identity_rejected(tmp_path, run_id):
    with pytest.raises(ValueError):
        recorder.record_run(ORIGIN, run_id, tmp_path / "capture")
    assert not (tmp_path / "capture").exists()


@pytest.mark.parametrize("options", [{"interval": 0.5}, {"interval": float("nan")}, {"max_seconds": 21601},
                                     {"max_samples": 10001}, {"max_total_bytes": 2**31}])
def test_limits_rejected_before_network_or_output(tmp_path, options):
    with pytest.raises(ValueError):
        record(tmp_path, lambda _: pytest.fail("must not request"), **options)
    assert not (tmp_path / "capture").exists()


def test_existing_output_is_never_overwritten(tmp_path):
    target = tmp_path / "capture"
    target.mkdir()
    (target / "manifest.json").write_bytes(b"preserve")
    with pytest.raises(recorder.RecordingError, match="output_already_exists"):
        record(tmp_path, lambda _: pytest.fail("must not request"))
    assert (target / "manifest.json").read_bytes() == b"preserve"


def test_output_symlink_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("host does not allow creating directory symlinks")
    with pytest.raises(recorder.RecordingError, match="output_contains_link"):
        recorder.record_run(ORIGIN, RUN_ID, linked / "capture")
    assert not (outside / "capture").exists()


@pytest.mark.parametrize("reason,options", [("duration_limit", {"max_seconds": 3}),
                                            ("sample_limit", {"max_samples": 2}),
                                            ("byte_limit", {"max_total_bytes": 10})])
def test_capture_bounds_stop_only_the_recorder(tmp_path, reason, options):
    def handler(request):
        assert request.method == "GET" and "/control" not in request.url.path
        return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"}) if request.url.path.endswith(
            "/frame") else httpx.Response(200, json=state())
    _, manifest, _ = record(tmp_path, handler, **options)
    assert manifest["stop_reason"] == reason and manifest["terminal_observation"] is None


def test_oversized_frame_is_a_gap_without_partial_files(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "MAX_FRAME_BYTES", 32)
    def handler(request):
        return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"}) if request.url.path.endswith(
            "/frame") else httpx.Response(200, json=state("completed"))
    directory, manifest, _ = record(tmp_path, handler)
    assert manifest["samples"][0]["gaps"] == [{"source": "frame", "error": "response_size_limit"}]
    assert not list((directory / "frames").iterdir()) and not list(directory.rglob("*.tmp"))


def test_redirect_is_not_followed_or_copied_into_manifest(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://user:secret@elsewhere.invalid/?key=secret"})
    directory, manifest, _ = record(tmp_path, handler, max_samples=1)
    assert len(calls) == 1 and manifest["samples"][0]["gaps"][0]["error"] == "http_302"
    assert "secret" not in (directory / "manifest.json").read_text(encoding="utf-8")


def test_missing_pillow_keeps_frames_and_manifest_without_installation(tmp_path, monkeypatch):
    raw = jpeg()
    sample = -1
    def handler(request):
        nonlocal sample
        if request.url.path.endswith("/frame"):
            return httpx.Response(200, content=raw, headers={"content-type": "image/jpeg"})
        sample += 1
        return httpx.Response(200, json=state("completed" if sample else "running"))
    monkeypatch.setitem(sys.modules, "PIL", None)
    directory, manifest, _ = record(tmp_path, handler, encode_webp=True)
    assert manifest["animation"]["status"] == "pillow_unavailable"
    assert len(list((directory / "frames").iterdir())) == 1
