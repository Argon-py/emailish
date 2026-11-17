"""SQLite persistence for jobs (verification history)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,            -- uploaded | queued | running | done | failed | interrupted
    phase TEXT DEFAULT '',
    total_rows INTEGER DEFAULT 0,
    unique_emails INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    email_column TEXT,
    columns_json TEXT,
    summary_json TEXT,
    engine_json TEXT,
    error TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            # Jobs that were running when the process stopped cannot resume.
            self.conn.execute(
                "UPDATE jobs SET status='interrupted', error='Server stopped while the job was running; "
                "start it again.' WHERE status IN ('running','queued')")
            self.conn.commit()

    def insert_job(self, **f) -> None:
        keys = ",".join(f)
        with self.lock:
            self.conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({','.join('?' * len(f))})", tuple(f.values()))
            self.conn.commit()

    def update_job(self, job_id: str, **f) -> None:
        if not f:
            return
        sets = ",".join(f"{k}=?" for k in f)
        with self.lock:
            self.conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*f.values(), job_id))
            self.conn.commit()

    def get_job(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in ("columns_json", "summary_json", "engine_json"):
            v = d.pop(k, None)
            d[k.replace("_json", "")] = json.loads(v) if v else None
        return d
