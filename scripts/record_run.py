"""Read-only native JPEG sampling on the wall clock; not native-FPS video or a browser recording."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

TERMINAL = frozenset({"completed", "stopped", "failed", "interrupted"})
MAX_RUN_BYTES = 2 * 1024 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_ANIMATION_BYTES = 128 * 1024 * 1024
MAX_ANIMATION_FRAMES = 240
MAX_ANIMATION_PIXELS = 80_000_000


class RecordingError(RuntimeError):
    """A safe, local error code; never copy untrusted response bodies into error messages."""


class RecordingLimit(RecordingError):
    pass


def source_origin(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or "?" in value or "#" in value
            or parsed.path not in {"", "/"} or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)):
        raise ValueError("--url must be an HTTP(S) origin without userinfo, path, query or fragment")
    _ = parsed.port
    return f"{parsed.scheme}://{parsed.netloc}"


def checked_run_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise ValueError("--run-id must contain exactly 32 hexadecimal characters")
    return value.lower()


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _reject_links(path: Path) -> None:
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise RecordingError("output_contains_link")


def _new_directory(value: Path) -> Path:
    path = Path(os.path.abspath(value.expanduser()))
    _reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_links(path)
    try:
        path.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise RecordingError("output_already_exists") from exc
    _reject_links(path)
    return path.resolve(strict=True)


def _target(directory: Path, name: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", name) or name in {".", ".."}:
        raise RecordingError("invalid_output_basename")
    target = directory / name
    _reject_links(target)
    if directory.resolve(strict=True) != directory or (target.exists() and not target.is_file()):
        raise RecordingError("unsafe_output_target")
    return target


def _write(directory: Path, name: str, body: bytes) -> None:
    target = _target(directory, name)
    temporary = directory / f".{name}.{uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _target(directory, name)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _publish(directory: Path, manifest: dict) -> None:
    _write(directory, "manifest.json", (json.dumps(manifest, ensure_ascii=False, allow_nan=False,
                                                 indent=2) + "\n").encode("utf-8"))


def _error(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"http_{error.response.status_code}"
    return str(error) if isinstance(error, RecordingError) else type(error).__name__


def _read(client: httpx.Client, url: str, limit: int, budget: dict, deadline: float, clock) -> tuple[bytes, str]:
    remaining = deadline - clock()
    if remaining <= 0:
        raise RecordingLimit("duration_limit")
    body = bytearray()
    with client.stream("GET", url, timeout=min(10, remaining)) as response:
        response.raise_for_status()
        try:
            declared = int(response.headers.get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > limit:
            raise RecordingError("response_size_limit")
        for chunk in response.iter_bytes(chunk_size=65536):
            budget["received"] += len(chunk)
            if budget["received"] > budget["limit"]:
                raise RecordingLimit("byte_limit")
            if clock() >= deadline:
                raise RecordingLimit("duration_limit")
            if len(body) + len(chunk) > limit:
                raise RecordingError("response_size_limit")
            body.extend(chunk)
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return bytes(body), media_type


def _run_state(body: bytes, run_id: str) -> dict:
    value = json.loads(body)
    if not isinstance(value, dict) or value.get("id") != run_id:
        raise RecordingError("run_identity_mismatch")
    if value.get("backend") != "sc2":
        raise RecordingError("native_backend_required")
    observation = value.get("observation") or {}
    metrics = value.get("metrics") or {}
    if not isinstance(observation, dict) or not isinstance(metrics, dict):
        raise RecordingError("invalid_run_state")
    if observation.get("engine") is not None and observation.get("engine") != "sc2":
        raise RecordingError("native_identity_mismatch")
    status, result = value.get("status"), value.get("result")
    if not isinstance(status, str) or status not in {"starting", "running", "deciding", "paused", "stopping", *TERMINAL}:
        raise RecordingError("invalid_run_status")
    engine_result = observation.get("result")
    return {"id": run_id, "backend": "sc2", "status": status,
            "result": result if isinstance(result, str) and result in {
                "success", "failure", "incomplete", "stopped", "error"} else None,
            "engine_result": engine_result if isinstance(engine_result, str)
            and engine_result in {"Victory", "Defeat", "Tie"} else None,
            "game_time": _number(observation.get("game_time")), "round": _number(value.get("round")),
            "reported_wall_seconds": _number(metrics.get("wall_seconds"))}


def _animation(directory: Path, manifest: dict) -> dict:
    first = next((index for index, sample in enumerate(manifest["samples"]) if sample.get("frame")), None)
    if first is None:
        return {"requested": True, "status": "skipped_no_frames"}
    samples = manifest["samples"][first:]
    if any(sample["gaps"] or not sample.get("frame") for sample in samples):
        return {"requested": True, "status": "skipped_gaps"}
    if len(samples) < 2:
        return {"requested": True, "status": "skipped_single_sample"}
    try:
        from PIL import Image, features
    except ImportError:
        return {"requested": True, "status": "pillow_unavailable"}
    if not features.check("webp"):
        return {"requested": True, "status": "webp_animation_unavailable"}
    try:
        from PIL import _webp
    except ImportError:
        return {"requested": True, "status": "webp_animation_unavailable"}
    # Pillow 12 removed the separate webp_anim feature name, but retains the native animation encoder.
    if not hasattr(_webp, "WebPAnimEncoder") or getattr(_webp, "HAVE_WEBPANIM", True) is False:
        return {"requested": True, "status": "webp_animation_unavailable"}
    segments = []
    for index, sample in enumerate(samples):
        frame = sample["frame"]
        end = samples[index + 1]["frame"]["elapsed_wall_seconds"] if index + 1 < len(samples) \
            else manifest["sampler_elapsed_wall_seconds"]
        duration = max(1, round((end - frame["elapsed_wall_seconds"]) * 1000))
        if segments and segments[-1]["sha256"] == frame["sha256"]:
            segments[-1]["duration_ms"] += duration
        else:
            segments.append({**frame, "duration_ms": duration})
    if len(segments) > MAX_ANIMATION_FRAMES:
        return {"requested": True, "status": "skipped_encoding_limit"}
    images, pixels, dimensions = [], 0, None
    temporary = directory / f".recording.{uuid4().hex}.webp"
    try:
        for segment in segments:
            frame = _target(directory / "frames", Path(segment["file"]).name)
            with Image.open(frame) as source:
                width, height = source.size
                pixels += width * height
                if not 0 < width <= 4096 or not 0 < height <= 4096 or pixels > MAX_ANIMATION_PIXELS:
                    return {"requested": True, "status": "skipped_encoding_limit"}
                if dimensions is not None and dimensions != source.size:
                    return {"requested": True, "status": "inconsistent_dimensions"}
                dimensions = source.size
                images.append(source.convert("RGB"))
        _reject_links(directory)
        images[0].save(temporary, format="WEBP", save_all=True, append_images=images[1:], lossless=True,
                       duration=[segment["duration_ms"] for segment in segments], loop=1, method=3)
        if temporary.stat().st_size > MAX_ANIMATION_BYTES:
            return {"requested": True, "status": "skipped_encoding_limit"}
        target = _target(directory, "recording.webp")
        if target.exists():
            raise RecordingError("animation_target_already_exists")
        os.replace(temporary, target)
        return {"requested": True, "status": "encoded", "file": target.name,
                "size": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "duration_ms": sum(segment["duration_ms"] for segment in segments),
                "segments": len(segments), "timing_resolution_ms": 1,
                "start_elapsed_wall_seconds": samples[0]["frame"]["elapsed_wall_seconds"],
                "omitted_leading_samples": first}
    except (OSError, ValueError, RecordingError) as exc:
        return {"requested": True, "status": "encoding_error", "error": _error(exc)}
    finally:
        for image in images:
            image.close()
        temporary.unlink(missing_ok=True)


def record_run(url: str, run_id: str, output: Path | None = None, *, interval: float = 2,
               max_seconds: float = 3600, max_samples: int = 1800, max_total_bytes: int = 512 * 1024 * 1024,
               encode_webp: bool = False, transport: httpx.BaseTransport | None = None,
               clock=time.monotonic, sleep=time.sleep,
               utc_now=lambda: datetime.now(timezone.utc).isoformat()) -> Path:
    """Sample an existing native run. Never POST, stop a game, infer a missing frame or overwrite a directory."""
    origin, run_id = source_origin(url), checked_run_id(run_id)
    if (_number(interval) is None or not 1 <= interval <= 300
            or _number(max_seconds) is None or not 1 <= max_seconds <= 21600
            or isinstance(max_samples, bool) or not isinstance(max_samples, int) or not 1 <= max_samples <= 10000
            or isinstance(max_total_bytes, bool) or not isinstance(max_total_bytes, int)
            or not 1 <= max_total_bytes <= 1024 * 1024 * 1024):
        raise ValueError("invalid recording limits (interval 1..300s, duration 1..21600s, samples 1..10000, bytes <=1GiB)")
    directory = _new_directory(output or Path("runtime") / "recordings" / run_id)
    frames = directory / "frames"
    frames.mkdir(mode=0o700)
    prefix = f"{origin}/api/runs/{run_id}"
    started = clock()
    deadline = started + max_seconds
    budget = {"received": 0, "limit": max_total_bytes}
    manifest = {"format_version": 1, "recording_type": "native_rgb_wall_sampling", "run_id": run_id,
                "source": {"run": prefix, "frame": f"{prefix}/frame"}, "started_utc": utc_now(),
                "ended_utc": None, "sampler_elapsed_wall_seconds": 0, "interval_seconds": interval,
                "limits": {"max_seconds": max_seconds, "max_samples": max_samples,
                           "max_total_bytes": max_total_bytes, "max_run_bytes": MAX_RUN_BYTES,
                           "max_frame_bytes": MAX_FRAME_BYTES}, "samples": [], "unique_frames": [],
                "terminal_observation": None, "stop_reason": "recording", "received_bytes": 0,
                "coverage_start": None, "coverage_end": None,
                "animation": {"requested": encode_webp, "status": "pending" if encode_webp else "not_requested"},
                "notes": ["Native RGB sampled on the wall clock, including model waits; not native FPS or browser video.",
                          ("Run state and frame are separate reads; game_time/round belong to the state read, "
                           "not a guaranteed timestamp for the JPEG."),
                          "Recording starts now; previously overwritten native frames cannot be recovered.",
                          ("Gaps are not interpolated. Leading gaps are omitted from optional animation; "
                           "a gap after its first frame prevents encoding. Coverage is sampler time, not whole-run wall time. "
                           "Animation durations use actual sample intervals, rounded to at least one millisecond.")]}
    _publish(directory, manifest)
    known = {}
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, transport=transport,
                          headers={"Accept-Encoding": "identity"}) as client:
            for index in range(max_samples):
                if clock() >= deadline:
                    manifest["stop_reason"] = "duration_limit"
                    break
                sampled_at = clock()
                sample = {"index": index, "utc": utc_now(), "elapsed_wall_seconds": sampled_at - started,
                          "game_time": None, "round": None, "state": None, "frame": None, "gaps": []}
                limit_hit = None
                try:
                    body, _ = _read(client, prefix, MAX_RUN_BYTES, budget, deadline, clock)
                    sample["state"] = _run_state(body, run_id)
                    sample["state_utc"] = utc_now()
                    sample["game_time"], sample["round"] = sample["state"]["game_time"], sample["state"]["round"]
                except (httpx.HTTPError, OSError, ValueError, RecordingError) as exc:
                    sample["gaps"].append({"source": "run", "error": _error(exc)})
                    if isinstance(exc, RecordingLimit) or isinstance(exc, RecordingError) and str(exc) in {
                            "run_identity_mismatch", "native_backend_required", "native_identity_mismatch"}:
                        limit_hit = str(exc)
                if sample["state"] and not limit_hit:
                    try:
                        body, media = _read(client, f"{prefix}/frame", MAX_FRAME_BYTES, budget, deadline, clock)
                        if media != "image/jpeg" or not body.startswith(b"\xff\xd8") or not body.endswith(b"\xff\xd9"):
                            raise RecordingError("native_jpeg_required")
                        digest = hashlib.sha256(body).hexdigest()
                        target = _target(frames, f"{digest}.jpg")
                        if digest not in known:
                            if target.exists():
                                raise RecordingError("unexpected_frame_file")
                            _write(frames, target.name, body)
                            known[digest] = {"file": f"frames/{target.name}", "sha256": digest, "size": len(body)}
                            manifest["unique_frames"].append(known[digest])
                        elif not target.is_file() or target.stat().st_size != known[digest]["size"] \
                                or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                            raise RecordingError("stored_frame_changed")
                        sample["frame"] = {**known[digest], "utc": utc_now(),
                                           "elapsed_wall_seconds": clock() - started}
                        coverage = {"utc": sample["frame"]["utc"],
                                    "elapsed_wall_seconds": sample["frame"]["elapsed_wall_seconds"],
                                    "game_time": sample["game_time"], "round": sample["round"]}
                        if manifest["coverage_start"] is None:
                            manifest["coverage_start"] = coverage
                        manifest["coverage_end"] = coverage
                    except (httpx.HTTPError, OSError, ValueError, RecordingError) as exc:
                        sample["gaps"].append({"source": "frame", "error": _error(exc)})
                        if isinstance(exc, RecordingLimit):
                            limit_hit = str(exc)
                else:
                    sample["gaps"].append({"source": "frame", "error": "unverified_run_state"})
                manifest["samples"].append(sample)
                manifest["received_bytes"] = budget["received"]
                if sample["state"] and sample["state"]["status"] in TERMINAL:
                    manifest["terminal_observation"] = {**sample["state"], "utc": sample["state_utc"],
                                                        "elapsed_wall_seconds": clock() - started}
                    manifest["stop_reason"] = "terminal"
                elif limit_hit:
                    manifest["stop_reason"] = limit_hit
                _publish(directory, manifest)
                if manifest["stop_reason"] != "recording":
                    break
                pause = min(max(0, sampled_at + interval - clock()), max(0, deadline - clock()))
                if pause:
                    sleep(pause)
            else:
                manifest["stop_reason"] = "sample_limit"
    except KeyboardInterrupt:
        manifest["stop_reason"] = "recorder_interrupted"
    except (OSError, httpx.HTTPError, RecordingError) as exc:
        manifest["stop_reason"] = "recorder_error"
        manifest["recorder_error"] = _error(exc)
    finally:
        manifest["ended_utc"] = utc_now()
        manifest["sampler_elapsed_wall_seconds"] = max(0, clock() - started)
        manifest["received_bytes"] = budget["received"]
        _publish(directory, manifest)
    if encode_webp:
        manifest["animation"] = _animation(directory, manifest)
        _publish(directory, manifest)
    return directory


def main() -> None:
    parser = argparse.ArgumentParser(description="只读采样原生 RGB；不是原生 FPS 视频或网页录屏，不控制游戏")
    parser.add_argument("--url", required=True, help="显式工作台 HTTP(S) origin；不允许凭据、路径或 query")
    parser.add_argument("--run-id", required=True, help="已存在的 32 位十六进制运行 ID")
    parser.add_argument("--output", type=Path, help="新目录；默认 runtime/recordings/{run-id}，拒绝覆盖")
    parser.add_argument("--interval", type=float, default=2, help="wall 采样间隔，至少 1 秒")
    parser.add_argument("--max-seconds", type=float, default=3600)
    parser.add_argument("--max-samples", type=int, default=1800)
    parser.add_argument("--max-total-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--webp", action="store_true", help="可选现有 Pillow 动画；缺帧或超编码限额则跳过")
    args = parser.parse_args()
    try:
        directory = record_run(args.url, args.run_id, args.output, interval=args.interval,
                               max_seconds=args.max_seconds, max_samples=args.max_samples,
                               max_total_bytes=args.max_total_bytes, encode_webp=args.webp)
    except (ValueError, RecordingError, OSError) as exc:
        parser.exit(2, f"Recording failed: {_error(exc)}\n")
    print(directory / "manifest.json")


if __name__ == "__main__":
    main()
