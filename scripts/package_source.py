"""Create a source-only handoff ZIP; runtime state, secrets and game/model assets are excluded."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/sc2-harness-source.zip"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    paths = [root / name for name in ("README.md", "AGENTS.md", "pyproject.toml", ".gitignore", ".gitattributes", ".env.example")]
    for folder in ("sc2_harness", "sc2_viewer", "tests", "scripts", "docs", "deploy"):
        paths.extend(path for path in (root / folder).rglob("*") if path.is_file())
    allowed = {".py", ".md", ".html", ".css", ".js", ".cjs", ".sh", ".json", ".toml", ".service", ".png", ".jpg"}
    files = sorted({path for path in paths if path.is_file()
                    and not path.is_symlink()
                    and "__pycache__" not in path.parts
                    and (path.suffix in allowed or path.name in {".gitignore", ".gitattributes", ".env.example"})})
    manifest = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            data = path.read_bytes()
            name = path.relative_to(root).as_posix()
            archive.writestr(name, data)
            manifest.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        archive.writestr("SOURCE_MANIFEST.json", json.dumps(manifest, indent=2))
    print(json.dumps({"archive": str(args.output), "files": len(files), "bytes": args.output.stat().st_size,
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
