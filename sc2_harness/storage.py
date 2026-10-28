"""Transactional run metadata and append-only event/snapshot evidence."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "experiments.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
                CREATE TABLE IF NOT EXISTS snapshots(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS snapshots_run ON snapshots(run_id,seq);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise KeyError(run_id)
        target = self.root / "runs" / run_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def put(self, run: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                       (run["id"], encode(run)))

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def list(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM runs ORDER BY rowid DESC LIMIT 200").fetchall()
        return [json.loads(row[0]) for row in rows]

    def append(self, table: str, run_id: str, payload: dict) -> dict:
        if table not in {"events", "snapshots"}:
            raise ValueError("Invalid evidence table")
        with self.connect() as db:
            cur = db.execute(f"INSERT INTO {table}(run_id,payload) VALUES(?,?)", (run_id, encode(payload)))
            result = {"seq": cur.lastrowid, **payload}
        return result

    def page(self, table: str, run_id: str, after: int = 0, limit: int = 200) -> dict:
        if table not in {"events", "snapshots"}:
            raise ValueError("Invalid evidence table")
        with self.connect() as db:
            rows = db.execute(f"SELECT seq,payload FROM {table} WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                              (run_id, after, min(max(limit, 1), 500))).fetchall()
        records = [{"seq": row[0], **json.loads(row[1])} for row in rows]
        return {table: records, "cursor": records[-1]["seq"] if records else after}

    def all_evidence(self, table: str, run_id: str):
        cursor = 0
        while True:
            page = self.page(table, run_id, cursor, 500)
            yield from page[table]
            if not page[table]:
                return
            cursor = page["cursor"]

    def interrupt_stale(self) -> None:
        active = {"starting", "running", "deciding", "paused", "stopping"}
        for run in self.list():
            if run["status"] in active:
                run.update(status="interrupted", result="incomplete", updated_at=utcnow(),
                           error="服务重启；未自动重放动作。请查看历史证据后新建实验。")
                self.put(run)
                self.append("events", run["id"], {"run_id": run["id"], "time": utcnow(),
                    "game_loop": None, "round": run.get("round", 0), "type": "run.interrupted",
                    "actor_id": "system", "message": run["error"], "data": {}})
