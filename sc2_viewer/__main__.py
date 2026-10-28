"""Launch a separate spectator service, never the experiment manager."""
from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from sc2_viewer.app import create_app
from sc2_viewer.settings import ViewerSettings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only SC2 spectator; only explicitly published runs are visible")
    parser.add_argument("--host", default="127.0.0.1", help="Viewer bind address; default is loopback")
    parser.add_argument("--port", type=int, default=8903)
    parser.add_argument("--upstream", default="http://127.0.0.1:18890", help="Private loopback workbench on port 18890")
    parser.add_argument("--allow-run-id", action="append", default=[], help="Publish this run; repeat for each run")
    parser.add_argument("--default-run-id", help="Initially selected published run")
    parser.add_argument("--recordings-root", type=Path, help="Independent directory of explicitly published media")
    parser.add_argument("--recording", action="append", default=[], metavar="RUN_ID=RELATIVE_FOLDER",
                        help="Publish RELATIVE_FOLDER/recording.webp (and optional manifest.json); repeat as needed")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    mappings = {}
    for item in args.recording:
        if "=" not in item:
            parser.error("recording must be RUN_ID=RELATIVE_FOLDER")
        run_id, folder = item.split("=", 1)
        if run_id in mappings:
            parser.error("duplicate recording mapping")
        mappings[run_id] = folder
    try:
        settings = ViewerSettings(tuple(args.allow_run_id), args.default_run_id, args.upstream,
                                  args.recordings_root, mappings)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
