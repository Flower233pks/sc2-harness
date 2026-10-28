"""Explicit spectator allowlists; no model environment or runtime database access."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

RUN_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class ViewerSettings:
    allowed_run_ids: tuple[str, ...] = ()
    default_run_id: str | None = None
    upstream: str = "http://127.0.0.1:18890"
    recordings_root: Path | None = None
    recordings: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        parsed = urlsplit(self.upstream)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"} \
                or parsed.port != 18890 or parsed.username is not None or parsed.password is not None \
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Viewer upstream must be the loopback workbench on HTTP port 18890")
        if len(self.allowed_run_ids) > 64 or any(not RUN_ID.fullmatch(item) for item in self.allowed_run_ids):
            raise ValueError("Use at most 64 explicit 32-character run IDs")
        if len(set(self.allowed_run_ids)) != len(self.allowed_run_ids):
            raise ValueError("Duplicate allowed run ID")
        if self.default_run_id is not None and self.default_run_id not in self.allowed_run_ids:
            raise ValueError("The default run must be allowed")
        if self.recordings and self.recordings_root is None:
            raise ValueError("Recording mappings require an explicit recordings root")
        for run_id, folder in self.recordings.items():
            path = Path(folder)
            if run_id not in self.allowed_run_ids or not folder or folder in {".", ".."} \
                    or folder.startswith("/") or path.is_absolute() \
                    or any(part in {"..", "."} for part in path.parts) or "\\" in folder or ":" in folder:
                raise ValueError("Recording folders must be relative children for explicitly allowed runs")

    @property
    def default(self) -> str | None:
        return self.default_run_id or next(iter(self.allowed_run_ids), None)
