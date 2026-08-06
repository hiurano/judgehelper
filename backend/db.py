"""
SQLite persistence module for Judge Helper jobs.
Provides thread-safe dict-like storage for job states.
"""
import asyncio
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from backend.config import DB_PATH, log


class JobStore:
    """Thread-safe SQLite key-value store. API-compatible with `dict[str, dict]`
    for the subset of operations used here: `store[k] = v`, `store.get(k, default)`,
    `len(store)`, `k in store`. Each value is JSON-serialised."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """Initialise database schema and PRAGMA settings once on start."""
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)"
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, job_id: str, default=None):
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            log.exception(f"Corrupt JSON for job {job_id} — treating as missing")
            return default

    def __getitem__(self, job_id: str):
        v = self.get(job_id)
        if v is None:
            raise KeyError(job_id)
        return v

    def __setitem__(self, job_id: str, data: dict):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        ts = int(time.time())
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs (id, data, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       data = excluded.data,
                       updated_at = excluded.updated_at""",
                (job_id, payload, ts),
            )

    def __contains__(self, job_id: str) -> bool:
        return self.get(job_id) is not None

    def __len__(self) -> int:
        with self._lock, self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def cleanup_old(self, max_age_days: int) -> int:
        cutoff = int(time.time()) - max_age_days * 86400
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE updated_at < ?", (cutoff,))
            deleted = cur.rowcount

        # Clean locks for jobs no longer in DB
        for k in list(locks.keys()):
            if k not in self:
                locks.pop(k, None)

        return deleted


jobs = JobStore(DB_PATH)
log.info(f"JobStore initialised at {DB_PATH} ({len(jobs)} existing entries)")

locks: dict[str, asyncio.Lock] = {}


def get_lock(job_id: str) -> asyncio.Lock:
    if job_id not in locks:
        locks[job_id] = asyncio.Lock()
    return locks[job_id]
