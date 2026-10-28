"""Read-only, bounded extraction of a workbench run's allowlisted evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 2 * 1024 * 1024
REQUIRED_FILES = frozenset({"config.json", "run.json", "metrics.json", "events.jsonl", "snapshots.jsonl"})


class EvidenceError(RuntimeError):
    pass


def public_error(error: BaseException) -> str:
    # HTTPX redirect errors can contain an untrusted Location with query credentials.
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code} from the requested workbench API"
    if isinstance(error, EvidenceError):
        return str(error)
    return type(error).__name__


def source_origin(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "?" in value or "#" in value or parsed.path not in {"", "/"}
            or any(ord(character) < 32 for character in value)):
        raise ValueError("--url must be an HTTP(S) origin without userinfo, path, query or fragment")
    # Accessing port validates malformed/out-of-range ports without retaining credentials.
    _ = parsed.port
    return f"{parsed.scheme}://{parsed.netloc}"


def checked_run_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise ValueError("--run-id must contain exactly 32 hexadecimal characters")
    return value.lower()


def allowed_artifact(name: object) -> bool:
    if (not isinstance(name, str) or not name or len(name) > 180 or name in {".", ".."}
            or ".." in name or name[-1:] in {" ", "."}
            or any(character in name for character in '<>:"/\\|?*\0')
            or any(ord(character) < 32 for character in name)
            or name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL",
                                                 *(f"COM{i}" for i in range(1, 10)),
                                                 *(f"LPT{i}" for i in range(1, 10))}):
        return False
    return name in REQUIRED_FILES or name.endswith(".SC2Replay")


def reject_links(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise EvidenceError("Evidence destination must not contain symlinks or junctions")


def output_directory(value: Path) -> Path:
    path = Path(os.path.abspath(value.expanduser()))
    reject_links(path)
    path.mkdir(parents=True, exist_ok=True)
    reject_links(path)
    return path.resolve(strict=True)


def safe_target(directory: Path, name: str) -> Path:
    target = directory / name
    reject_links(target)
    if target.parent.resolve(strict=True) != directory or (target.exists() and not target.is_file()):
        raise EvidenceError("Evidence target is not a regular file inside the output directory")
    return target


def temporary_stream(directory: Path, name: str):
    temporary = directory / f".{name}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return temporary, os.fdopen(descriptor, "wb")


def publish_manifest(directory: Path, manifest: dict) -> None:
    target = safe_target(directory, "manifest.json")
    temporary, stream = temporary_stream(directory, "manifest.json")
    try:
        with stream:
            stream.write((json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        safe_target(directory, target.name)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def content_length(response: httpx.Response, limit: int) -> None:
    try:
        size = int(response.headers.get("content-length", "0"))
    except ValueError:
        return
    if size > limit:
        raise EvidenceError(f"Response exceeds the {limit}-byte limit")


def artifact_index(client: httpx.Client, url: str) -> list[dict]:
    body = bytearray()
    with client.stream("GET", url) as response:
        response.raise_for_status()
        content_length(response, MAX_INDEX_BYTES)
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > MAX_INDEX_BYTES:
                raise EvidenceError("Artifact index exceeds its size limit")
            body.extend(chunk)
    value = json.loads(body)
    artifacts = value.get("artifacts") if isinstance(value, dict) else None
    if not isinstance(artifacts, list) or len(artifacts) > 128:
        raise EvidenceError("Invalid artifact index")
    return artifacts


def download(client: httpx.Client, url: str, directory: Path, name: str | None = None) -> dict | None:
    temporary = None
    with client.stream("GET", url) as response:
        if name is None and response.status_code == 404:
            return None
        response.raise_for_status()
        if name is None:
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type not in {"image/png", "image/jpeg"}:
                raise EvidenceError("Native frame endpoint did not return a PNG or JPEG")
            name = "native-frame.png" if media_type == "image/png" else "native-frame.jpg"
        target = safe_target(directory, name)
        content_length(response, MAX_FILE_BYTES)
        digest, size = hashlib.sha256(), 0
        try:
            temporary, stream = temporary_stream(directory, name)
            with stream:
                for chunk in response.iter_bytes():
                    if size + len(chunk) > MAX_FILE_BYTES:
                        raise EvidenceError(f"{name} exceeds the 64 MiB single-file limit")
                    stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            safe_target(directory, name)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {"name": name, "size": size, "sha256": digest.hexdigest(), "source": url}


def fetch_evidence(url: str, run_id: str, output: Path | None = None, *, include_frame: bool = False,
                   transport: httpx.BaseTransport | None = None) -> Path:
    origin, run_id = source_origin(url), checked_run_id(run_id)
    directory = output_directory(output or Path("runtime") / "evidence" / run_id)
    # Fail before any network request if a pre-existing manifest redirects outside this directory.
    safe_target(directory, "manifest.json")
    prefix = f"{origin}/api/runs/{run_id}"
    manifest = {"run_id": run_id, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "source": f"{prefix}/artifacts", "complete": False, "files": [], "errors": [],
                "missing": [], "frame": {"requested": include_frame, "status": "not_requested"}}
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False, transport=transport,
                      headers={"Accept-Encoding": "identity"}) as client:
        try:
            artifacts = artifact_index(client, f"{prefix}/artifacts")
            seen = set()
            for item in artifacts:
                name = item.get("name") if isinstance(item, dict) else None
                if not allowed_artifact(name):
                    raise EvidenceError("Artifact index contains a non-allowlisted basename")
                if name in seen:
                    continue
                seen.add(name)
                # Never follow a URL supplied in the index, including cross-origin or credentialed URLs.
                source = f"{prefix}/artifacts/{quote(name, safe='')}"
                try:
                    manifest["files"].append(download(client, source, directory, name))
                except (httpx.HTTPError, EvidenceError, OSError) as exc:
                    manifest["errors"].append({"name": name, "error": public_error(exc)})
            downloaded = {item["name"] for item in manifest["files"]}
            manifest["missing"] = sorted(REQUIRED_FILES - downloaded)
            if include_frame:
                try:
                    frame = download(client, f"{prefix}/frame", directory)
                    manifest["frame"]["status"] = "downloaded" if frame else "unavailable"
                    if frame:
                        manifest["files"].append(frame)
                except (httpx.HTTPError, EvidenceError, OSError) as exc:
                    manifest["frame"].update(status="error", error=public_error(exc))
                    manifest["errors"].append({"name": "native-frame", "error": public_error(exc)})
            manifest["complete"] = not manifest["errors"] and not manifest["missing"]
        except (httpx.HTTPError, EvidenceError, OSError, ValueError) as exc:
            manifest["errors"].append({"name": "artifact-index", "error": public_error(exc)})
    publish_manifest(directory, manifest)
    if not manifest["complete"]:
        raise EvidenceError(f"Evidence extraction is incomplete; inspect {directory / 'manifest.json'}")
    return directory


def main() -> None:
    parser = argparse.ArgumentParser(description="只读下载实验允许导出的证据及 SHA-256 清单，不控制实验")
    parser.add_argument("--url", default="http://127.0.0.1:18890", help="工作台 origin；不允许用户名、密码、路径或 query")
    parser.add_argument("--run-id", required=True, help="32 位十六进制实验 ID")
    parser.add_argument("--output", type=Path, help="默认 runtime/evidence/{run-id}")
    parser.add_argument("--frame", action="store_true", help="额外取得当前原生帧；404 会标记为 unavailable")
    args = parser.parse_args()
    try:
        directory = fetch_evidence(args.url, args.run_id, args.output, include_frame=args.frame)
    except (EvidenceError, ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Evidence directory: {directory}")


if __name__ == "__main__":
    main()
