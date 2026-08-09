"""
SQLite persistence module for Judge Helper.
Provides thread-safe storage for jobs and users.
"""
import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from backend.config import DB_PATH, DEFAULT_USER, log


# --- Password hashing utilities -------------------------------------------

def hash_password(password: str) -> str:
    """Create a salted SHA-256 hash for storage."""
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored salted hash."""
    try:
        salt, expected = stored_hash.split(":", 1)
    except ValueError:
        return False
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return secrets.compare_digest(h, expected)


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
                    updated_at INTEGER NOT NULL,
                    user_id TEXT DEFAULT 'elena'
                )"""
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
            if "user_id" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT DEFAULT 'elena'")
                conn.execute("UPDATE jobs SET user_id = 'elena' WHERE user_id IS NULL")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)"
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
        user_id = data.get("user_id", DEFAULT_USER)
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs (id, data, updated_at, user_id) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       data = excluded.data,
                       updated_at = excluded.updated_at,
                       user_id = COALESCE(excluded.user_id, jobs.user_id)""",
                (job_id, payload, ts, user_id),
            )

    def __contains__(self, job_id: str) -> bool:
        return self.get(job_id) is not None

    def __len__(self) -> int:
        with self._lock, self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def delete(self, job_id: str) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return cur.rowcount > 0

    def cleanup_old(self, max_age_days: int) -> int:
        cutoff = int(time.time()) - max_age_days * 86400
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE updated_at < ?", (cutoff,))
            deleted = cur.rowcount

        # Clean locks for jobs that are unlocked or no longer in DB
        cleanup_unused_locks()

        return deleted

    def get_by_aai_id(self, key_or_aai_id: str) -> tuple[Optional[str], Optional[dict]]:
        """Find job by primary job_id or embedded aai_transcript_id.
        Returns (job_id, job_data) or (None, None)."""
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT id, data FROM jobs WHERE id = ?", (key_or_aai_id,)).fetchone()
            if row:
                try:
                    return row[0], json.loads(row[1])
                except Exception:
                    return None, None

            rows = conn.execute("SELECT id, data FROM jobs WHERE data LIKE ?", (f'%"{key_or_aai_id}"%',)).fetchall()
            for r_id, r_data in rows:
                try:
                    d = json.loads(r_data)
                    if d.get("aai_transcript_id") == key_or_aai_id:
                        return r_id, d
                except Exception:
                    continue
        return None, None

    def get_pending_jobs(self) -> list[dict]:
        """Fetch all jobs currently in 'processing' status."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT id, data FROM jobs WHERE data LIKE '%\"status\": \"processing\"%'"
            ).fetchall()
        results = []
        for job_id, data_str in rows:
            try:
                data = json.loads(data_str)
                if data.get("status") == "processing":
                    data["id"] = job_id
                    results.append(data)
            except Exception:
                continue
        return results

    def list_recent(self, user_id: str = None, limit: int = 30) -> list:
        with self._lock, self._conn() as conn:
            if user_id:
                rows = conn.execute(
                    "SELECT id, data, updated_at FROM jobs WHERE (user_id = ? OR user_id IS NULL) ORDER BY updated_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, data, updated_at FROM jobs ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        results = []
        for job_id, data_str, updated_at in rows:
            try:
                data = json.loads(data_str)
                data["id"] = job_id
                data["updated_at"] = updated_at
                results.append(data)
            except Exception:
                continue
        return results

    def get_user_stats(self, user_id: str = DEFAULT_USER) -> dict:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM jobs WHERE (user_id = ? OR user_id IS NULL)", (user_id,)
            ).fetchall()
        total_count = 0
        total_sec = 0.0
        for (data_str,) in rows:
            try:
                data = json.loads(data_str)
                if data.get("status") == "done":
                    total_count += 1
                    dur = data.get("duration_min") or (data.get("audio_duration_sec", 0) / 60.0)
                    total_sec += (dur * 60.0)
            except Exception:
                continue
        total_min = round(total_sec / 60.0, 1)
        return {
            "total_protocols": total_count,
            "total_duration_min": total_min,
        }


# --- UserStore -------------------------------------------------------------

class UserStore:
    """Thread-safe SQLite store for user accounts with hashed passwords."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    display_name TEXT,
                    created_at INTEGER NOT NULL
                )"""
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

    def create_user(self, username: str, password: str, display_name: str = "") -> bool:
        """Create a new user. Returns False if username already exists."""
        pw_hash = hash_password(password)
        try:
            with self._lock, self._conn() as conn:
                conn.execute(
                    "INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                    (username, pw_hash, display_name or username.capitalize(), int(time.time())),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def verify(self, username: str, password: str) -> bool:
        """Verify login credentials against stored hash."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not row:
            return False
        return verify_password(password, row[0])

    def exists(self, username: str) -> bool:
        """Check if a user account exists."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
        return row is not None

    def delete_user(self, username: str) -> bool:
        """Delete a user account."""
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
            return cur.rowcount > 0

    def list_users(self) -> list[dict]:
        """List all users (without passwords)."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT username, display_name, created_at FROM users"
            ).fetchall()
        return [{"username": r[0], "display_name": r[1], "created_at": r[2]} for r in rows]

    def change_password(self, username: str, new_password: str) -> bool:
        """Change a user's password."""
        pw_hash = hash_password(new_password)
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (pw_hash, username),
            )
            return cur.rowcount > 0

    def is_empty(self) -> bool:
        """Check if the users table has no entries."""
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        return row[0] == 0


jobs = JobStore(DB_PATH)
log.info(f"JobStore initialised at {DB_PATH} ({len(jobs)} existing entries)")

user_store = UserStore(DB_PATH)
log.info(f"UserStore initialised at {DB_PATH}")

_locks_guard = threading.Lock()
locks: dict[str, asyncio.Lock] = {}


def get_lock(job_id: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a job in a thread-safe manner."""
    with _locks_guard:
        if job_id not in locks:
            locks[job_id] = asyncio.Lock()
        return locks[job_id]


def remove_lock(job_id: str):
    """Safely remove a job's lock from memory if it is unlocked."""
    with _locks_guard:
        lock = locks.get(job_id)
        if lock and not lock.locked():
            locks.pop(job_id, None)


def cleanup_unused_locks():
    """Remove locks for jobs that are unlocked."""
    with _locks_guard:
        for k in list(locks.keys()):
            lock = locks[k]
            if not lock.locked():
                locks.pop(k, None)
