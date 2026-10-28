"""Convert a saved animated WebP to an offline H.264 MP4, retaining wall-clock timing.

Requires Pillow and a local ffmpeg with libx264 and the setts bitstream filter.
No game, model, network, ffprobe, frame interpolation, resize or speed-up is used.
The MP4 is published only after a full decode verifies its frames and timestamps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_TEMP_BYTES = 1024 * 1024 * 1024
MAX_FRAMES = 2000
MAX_PIXELS = 1920 * 1080
MAX_TOTAL_PIXELS = 1_500_000_000
MAX_DURATION_MS = 3_600_000
PROCESS_TIMEOUT_SECONDS = 900


def regular_path(path: Path, *, must_exist: bool) -> Path:
    """Refuse links/junctions in the complete path, including an output parent."""
    path = Path(os.path.abspath(path.expanduser()))
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ValueError("Symbolic links and junctions are not accepted")
    if must_exist and not path.is_file():
        raise ValueError("Input must be an existing regular file")
    if not path.parent.is_dir():
        raise ValueError("Create the destination parent directory before conversion")
    return path


def executable(value: str) -> str:
    result = shutil.which(value)
    if result is None:
        raise ValueError("ffmpeg was not found; install it separately or provide --ffmpeg")
    return result


def decode_frames(source: Path, directory: Path) -> tuple[list[int], tuple[int, int]]:
    durations: list[int] = []
    total_bytes = 0
    with Image.open(source) as image:
        if image.format != "WEBP":
            raise ValueError("Expected a WebP recording")
        width, height = image.size
        count = image.n_frames
        if count < 1 or count > MAX_FRAMES:
            raise ValueError("Expected a WebP containing 1 to 2000 frames")
        if width <= 0 or height <= 0 or width > 1920 or height > 1080 or width * height > MAX_PIXELS:
            raise ValueError("Source pixel limit exceeded")
        if width % 2 or height % 2:
            raise ValueError("H.264 yuv420p requires even dimensions; automatic resizing is disabled")
        if count * width * height > MAX_TOTAL_PIXELS:
            raise ValueError("Total decoded pixel limit exceeded")
        for index in range(count):
            image.seek(index)
            image.load()  # Pillow exposes the animated WebP duration after loading the frame.
            duration = image.info.get("duration")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) \
                    or not math.isfinite(duration) or duration <= 0 or int(duration) != duration:
                raise ValueError(f"Frame {index} has no positive integer millisecond duration")
            durations.append(int(duration))
            if sum(durations) > MAX_DURATION_MS:
                raise ValueError("Recording duration exceeds the one-hour limit")
            if image.size != (width, height):
                raise ValueError("Frame dimensions changed")
            # Pillow yields the composited original frame; do not synthesize intermediate frames.
            frame = image.convert("RGB")
            target = directory / f"frame-{index:06d}.png"
            frame.save(target, format="PNG")
            total_bytes += target.stat().st_size
            if total_bytes > MAX_TEMP_BYTES:
                raise ValueError("Temporary decoded-frame size limit exceeded")
    lines = ["ffconcat version 1.0"]
    for index, duration in enumerate(durations):
        lines.extend((f"file 'frame-{index:06d}.png'", "option framerate 1000",
                      f"duration {duration / 1000:.3f}"))
    (directory / "frames.ffconcat").write_text("\n".join(lines) + "\n", encoding="ascii")
    return durations, (width, height)


def run_ffmpeg(command: list[str], directory: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=directory, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=PROCESS_TIMEOUT_SECONDS,
                            check=False)
    if result.returncode:
        # Generated local media only; never include environment contents or model configuration.
        tail = "\n".join(result.stderr.splitlines()[-12:])[-4000:]
        raise ValueError(f"ffmpeg failed ({result.returncode}): {tail}")
    return result


def faststart(path: Path) -> bool:
    positions = {}
    size = path.stat().st_size
    with path.open("rb") as source:
        offset = 0
        while offset + 8 <= size:
            source.seek(offset)
            length, kind = struct.unpack(">I4s", source.read(8))
            header = 8
            if length == 1:
                extra = source.read(8)
                if len(extra) != 8:
                    return False
                length = struct.unpack(">Q", extra)[0]
                header = 16
            elif length == 0:
                length = size - offset
            if length < header or offset + length > size:
                return False
            positions.setdefault(kind, offset)
            offset += length
    return b"moov" in positions and b"mdat" in positions and positions[b"moov"] < positions[b"mdat"]


def verify_video(ffmpeg: str, output: Path, durations: list[int], size: tuple[int, int]) -> dict:
    """Use ffmpeg itself to decode every frame; ffprobe is deliberately not required."""
    result = run_ffmpeg([ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
                         "-threads", "2", "-i", str(output), "-map", "0:v:0", "-an",
                         "-vf", "showinfo", "-filter_threads", "1", "-vsync", "0",
                         "-f", "null", "-"], output.parent)
    stamps = []
    dimensions = []
    for line in result.stderr.splitlines():
        if "showinfo" not in line:
            continue
        match = re.search(r"\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:([-\d.]+)", line)
        if match:
            if int(match.group(1)) != len(stamps):
                raise ValueError("Decoded frame sequence is incomplete")
            stamps.append(float(match.group(2)))
            shape = re.search(r"\bs:(\d+)x(\d+)\b", line)
            dimensions.append(tuple(map(int, shape.groups())) if shape else None)
    if len(stamps) != len(durations) or any(shape != size for shape in dimensions):
        raise ValueError("MP4 decode frame count or dimensions differ from the source")
    expected = []
    elapsed = 0
    for duration in durations:
        expected.append(elapsed / 1000)
        elapsed += duration
    if any(abs(actual - wanted) > 0.002 for actual, wanted in zip(stamps, expected)):
        raise ValueError("MP4 presentation timestamps differ from source frame timing")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        raise ValueError("ffmpeg did not report the MP4 duration")
    hours, minutes, seconds = match.groups()
    actual_duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if abs(actual_duration - elapsed / 1000) > 0.02:
        raise ValueError("MP4 total duration differs from source timing")
    if not re.search(r"Video:\s*h264\b[^\r\n]*\byuv420p\b", result.stderr):
        raise ValueError("Output is not H.264 yuv420p")
    if not faststart(output):
        raise ValueError("Output lacks a faststart MP4 layout")
    return {"decoded_frames": len(stamps), "frame_timestamps_verified": True,
            "reported_duration_seconds": actual_duration, "duration_tolerance_seconds": 0.02,
            "faststart_verified": True, "codec": "h264", "pixel_format": "yuv420p"}


def convert(source: Path, output: Path, ffmpeg: str = "ffmpeg") -> dict:
    source = regular_path(source, must_exist=True)
    output = regular_path(output, must_exist=False)
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; conversion never overwrites files")
    if output.suffix.lower() != ".mp4" or source == output:
        raise ValueError("Choose a new .mp4 output path")
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Input exceeds the 64 MiB limit")
    encoder = executable(ffmpeg)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix=".sc2-video-", dir=output.parent) as temporary:
        directory = Path(temporary)
        if directory.resolve().parent != output.parent.resolve():
            raise ValueError("Temporary directory escaped the selected output parent")
        durations, size = decode_frames(source, directory)
        candidate = directory / "video.mp4"
        # MP4 derives intermediate sample durations from consecutive DTS values.
        # Explicitly give the last packet its real duration; no duplicate tail frame is added.
        last = len(durations) - 1
        timing = f"setts=duration=if(eq(N\\,{last})\\,{durations[-1]}\\,DURATION):time_base=1/1000"
        run_ffmpeg([encoder, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-n",
                    "-protocol_whitelist", "file,pipe", "-threads", "2", "-f", "concat", "-safe", "0",
                    "-i", "frames.ffconcat", "-map", "0:v:0", "-an", "-vsync", "0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-x264-params", "fps=25/1", "-profile:v", "high", "-level:v", "4.1",
                    "-bf", "0", "-threads", "2", "-filter_threads", "1", "-enc_time_base", "1:1000",
                    "-bsf:v", timing, "-video_track_timescale", "1000", "-movflags", "+faststart",
                    "-fs", str(MAX_OUTPUT_BYTES), str(candidate)], directory)
        if not candidate.is_file() or not 0 < candidate.stat().st_size <= MAX_OUTPUT_BYTES:
            raise ValueError("Output is empty or exceeds the 64 MiB limit")
        verification = verify_video(encoder, candidate, durations, size)
        if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
            raise ValueError("Source changed during conversion")
        regular_path(output, must_exist=False)
        # Atomic no-clobber publication on the same filesystem, including POSIX.
        # A plain rename/replace could overwrite a destination created during encoding.
        os.link(candidate, output)
        candidate.unlink()
    return {"input": str(source), "input_sha256": source_sha, "output": str(output),
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "output_bytes": output.stat().st_size, "source_frames": len(durations),
            "source_duration_ms": sum(durations), "width": size[0], "height": size[1],
            "verification": verification,
            "note": "Offline native-RGB sampling conversion; original waits retained, no new gameplay or frames."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    try:
        result = convert(args.input, args.output, args.ffmpeg)
    except (OSError, ValueError, subprocess.TimeoutExpired, Image.DecompressionBombError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
