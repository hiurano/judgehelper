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


PBKDF2_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    """Create a salted PBKDF2-HMAC-SHA256 hash for storage."""
    salt = secrets.token_bytes(16)
    kdf = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2:{PBKDF2_ITERATIONS}:{salt.hex()}:{kdf.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against stored hash (supports PBKDF2 and legacy SHA-256)."""
    if not stored_hash:
        return False
    try:
        if stored_hash.startswith("pbkdf2:"):
            _, iters_str, salt_hex, expected_hex = stored_hash.split(":", 3)
            salt = bytes.fromhex(salt_hex)
            kdf = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters_str))
            return secrets.compare_digest(kdf.hex(), expected_hex)
        else:
            # Legacy single-iteration SHA-256 fallback (salt:hash)
            salt, expected = stored_hash.split(":", 1)
            h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
            return secrets.compare_digest(h, expected)
    except Exception:
        return False


def _split_draft(data: dict) -> tuple[dict, Optional[str]]:
    """Separate the protocol text from the rest of a job's state.

    The text is by far the largest thing a job carries — tens of kilobytes —
    and almost nothing that reads a job wants it. Keeping it out of the JSON
    blob means the job list no longer parses megabytes to show thirty rows,
    and each progress write during drafting no longer rewrites the whole
    protocol. Returns a copy: callers keep using their own dict afterwards."""
    if "draft" not in data:
        return data, None
    rest = {key: value for key, value in data.items() if key != "draft"}
    draft = data.get("draft")
    return rest, draft if isinstance(draft, str) else None


def duration_seconds(data: dict) -> Optional[float]:
    """Audio length in seconds, from whichever field the pipeline recorded.

    `duration_min` is what the pipeline writes once it knows; before that only
    the raw `audio_duration_sec` from the recognition service is there."""
    for key, scale in (("duration_min", 60.0), ("audio_duration_sec", 1.0)):
        value = data.get(key)
        if value is None:
            continue
        try:
            return float(value) * scale
        except (TypeError, ValueError):
            continue
    return None


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
                    user_id TEXT DEFAULT 'test'
                )"""
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()]
            if "user_id" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN user_id TEXT DEFAULT 'test'")
                conn.execute("UPDATE jobs SET user_id = 'test' WHERE user_id IS NULL")
            if "status" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN status TEXT")
            if "aai_transcript_id" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN aai_transcript_id TEXT")
            if "draft" not in cols:
                conn.execute("ALTER TABLE jobs ADD COLUMN draft TEXT")
                # Lift the text out of the blobs written by earlier versions.
                for job_id, data_str in conn.execute(
                    "SELECT id, data FROM jobs WHERE data LIKE '%\"draft\"%'"
                ).fetchall():
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    rest, draft = _split_draft(data)
                    if draft is None and "draft" not in data:
                        continue
                    conn.execute(
                        "UPDATE jobs SET data = ?, draft = ? WHERE id = ?",
                        (json.dumps(rest, ensure_ascii=False, default=str), draft, job_id),
                    )
            if "duration_sec" not in cols:
                # Kept beside the blob so the per-user totals never have to read
                # it. Backfilled once here; the rows are rewritten from then on.
                conn.execute("ALTER TABLE jobs ADD COLUMN duration_sec REAL")
                for job_id, data_str in conn.execute("SELECT id, data FROM jobs").fetchall():
                    try:
                        seconds = duration_seconds(json.loads(data_str))
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if seconds is not None:
                        conn.execute(
                            "UPDATE jobs SET duration_sec = ? WHERE id = ?",
                            (seconds, job_id),
                        )

            # Also repair databases already opened by the old migration,
            # which added these columns without copying their JSON values.
            for job_id, data_str in conn.execute(
                "SELECT id, data FROM jobs WHERE status IS NULL OR "
                "(aai_transcript_id IS NULL AND data LIKE '%aai_transcript_id%')"
            ).fetchall():
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                status = data.get("status")
                transcript_id = data.get("aai_transcript_id")
                conn.execute(
                    "UPDATE jobs SET status = COALESCE(status, ?), "
                    "aai_transcript_id = COALESCE(aai_transcript_id, ?) WHERE id = ?",
                    (status if isinstance(status, str) else None,
                     transcript_id if isinstance(transcript_id, str) else None, job_id),
                )

            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_id ON jobs(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_aai_id ON jobs(aai_transcript_id)")
            # Covering index for the per-user totals: with duration_sec in the
            # index itself, /api/me is answered without touching the table,
            # whose rows carry the protocol text.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_user_status "
                "ON jobs(user_id, status, duration_sec)"
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

    @staticmethod
    def _rejoin(data_str: str, draft: Optional[str]) -> dict:
        """Put the protocol text back where every caller expects it."""
        data = json.loads(data_str)
        if draft is not None:
            data["draft"] = draft
        return data

    def get(self, job_id: str, default=None):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data, draft FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return default
        try:
            return self._rejoin(row[0], row[1])
        except json.JSONDecodeError:
            log.exception(f"Corrupt JSON for job {job_id} — treating as missing")
            return default

    def __getitem__(self, job_id: str):
        v = self.get(job_id)
        if v is None:
            raise KeyError(job_id)
        return v

    def __setitem__(self, job_id: str, data: dict):
        """Insert or overwrite a job outright.

        This creates the row if it is missing, so it is for code that owns the
        job's existence. Anything writing back state it read earlier wants
        `update_if_exists` instead."""
        rest, draft = _split_draft(data)
        payload = json.dumps(rest, ensure_ascii=False, default=str)
        ts = int(time.time())
        user_id = data.get("user_id", DEFAULT_USER)
        status = data.get("status")
        aai_transcript_id = data.get("aai_transcript_id")
        duration = duration_seconds(data)

        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs
                   (id, data, updated_at, user_id, status, aai_transcript_id,
                    duration_sec, draft)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       data = excluded.data,
                       updated_at = excluded.updated_at,
                       user_id = COALESCE(excluded.user_id, jobs.user_id),
                       status = excluded.status,
                       aai_transcript_id = excluded.aai_transcript_id,
                       duration_sec = excluded.duration_sec,
                       draft = excluded.draft""",
                (job_id, payload, ts, user_id, status, aai_transcript_id, duration, draft),
            )

    def update_if_exists(self, job_id: str, data: dict) -> bool:
        """Persist changes to a row that still exists, and only then.

        Background work outlives the request that started it, so a job can be
        deleted by its owner while a task is still holding a copy of it in
        memory. `__setitem__` would insert that copy straight back, reviving a
        deleted protocol under the default account; this refuses instead and
        lets the caller stop."""
        rest, draft = _split_draft(data)
        payload = json.dumps(rest, ensure_ascii=False, default=str)
        ts = int(time.time())
        user_id = data.get("user_id")
        status = data.get("status")
        aai_transcript_id = data.get("aai_transcript_id")
        duration = duration_seconds(data)

        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """UPDATE jobs SET
                       data = ?,
                       updated_at = ?,
                       user_id = COALESCE(?, user_id),
                       status = ?,
                       aai_transcript_id = ?,
                       duration_sec = ?,
                       draft = ?
                   WHERE id = ?""",
                (payload, ts, user_id, status, aai_transcript_id, duration, draft, job_id),
            )
            updated = cur.rowcount > 0
        return updated

    def __contains__(self, job_id: str) -> bool:
        return self.get(job_id) is not None

    def __len__(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def delete(self, job_id: str) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            return cur.rowcount > 0

    def claim_upload(self, job_id: str, user_id: str) -> bool:
        """Consume a reservation once; deletion always wins over a late upload."""
        with self._lock, self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
            if not row:
                return False
            data = json.loads(row[0])
            if data.get("status") != "processing" or data.get("phase") != "awaiting_upload":
                return False
            data["phase"] = "receiving_upload"
            conn.execute("UPDATE jobs SET data = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(data, ensure_ascii=False), int(time.time()), job_id))
            return True

    def count_for_user(self, user_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row[0])

    def delete_for_user(self, user_id: str) -> int:
        """Remove every job belonging to a user. Used when the account goes.

        Jobs are owned by username, so leaving them behind would hand them to
        the next account created with the same name."""
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE user_id = ?", (user_id,))
            deleted = cur.rowcount
        return deleted

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
        with self._conn() as conn:
            # Check aai_transcript_id column first (fast indexed lookup)
            row = conn.execute(
                "SELECT id, data, draft FROM jobs WHERE aai_transcript_id = ?",
                (key_or_aai_id,),
            ).fetchone()
            if row:
                try:
                    return row[0], self._rejoin(row[1], row[2])
                except Exception:
                    pass

            # Fallback to checking primary id
            row = conn.execute(
                "SELECT data, draft FROM jobs WHERE id = ?", (key_or_aai_id,)
            ).fetchone()
            if row:
                try:
                    return key_or_aai_id, self._rejoin(row[0], row[1])
                except Exception:
                    pass
        return None, None

    def get_pending_jobs(self) -> list[dict]:
        """Fetch all jobs currently in 'processing' status."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, data FROM jobs WHERE status = 'processing'"
            ).fetchall()
        
        results = []
        for job_id, data_str in rows:
            try:
                data = json.loads(data_str)
                data["id"] = job_id
                results.append(data)
            except Exception:
                continue
        return results

    def list_recent(self, user_id: str = None, limit: int = 30) -> list:
        """Rows for the job list, deliberately without the protocol text.

        Selecting the text here meant parsing megabytes on every tab focus for
        a list that shows none of it, so `has_draft` is read from the column
        instead and the text is fetched per job from /status when wanted."""
        with self._conn() as conn:
            if user_id:
                rows = conn.execute(
                    """SELECT id, data, updated_at, draft IS NOT NULL
                       FROM jobs WHERE user_id = ?
                       ORDER BY updated_at DESC LIMIT ?""",
                    (user_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, data, updated_at, draft IS NOT NULL
                       FROM jobs ORDER BY updated_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        results = []
        for job_id, data_str, updated_at, has_draft in rows:
            try:
                data = json.loads(data_str)
                data["id"] = job_id
                data["updated_at"] = updated_at
                data["has_draft"] = bool(has_draft)
                results.append(data)
            except Exception:
                continue
        return results

    def get_user_stats(self, user_id: str = DEFAULT_USER) -> dict:
        """Totals for /api/me, read straight from the indexed columns.

        This used to select every row the user owns and JSON-parse it — whole
        protocols, tens of kilobytes each — only to count the finished ones and
        add up their durations. /api/me runs on every page load and after every
        delete, and it all happened on the event loop."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(duration_sec), 0)
                   FROM jobs WHERE user_id = ? AND status = 'done'""",
                (user_id,),
            ).fetchone()
        return {
            "total_protocols": int(row[0]),
            "total_duration_min": round(float(row[1]) / 60.0, 1),
        }

    def count_active_for_user(self, user_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'processing'",
                (user_id,),
            ).fetchone()
        return int(row[0])

    def create_if_under_active_limit(
        self, job_id: str, data: dict, max_active: int
    ) -> bool:
        """Atomically reserve a processing slot for a user in this process."""
        rest, draft = _split_draft(data)
        payload = json.dumps(rest, ensure_ascii=False, default=str)
        ts = int(time.time())
        user_id = data.get("user_id", DEFAULT_USER)
        with self._lock, self._conn() as conn:
            active = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND status = 'processing'",
                (user_id,),
            ).fetchone()[0]
            if active >= max_active:
                return False
            conn.execute(
                """INSERT INTO jobs
                   (id, data, updated_at, user_id, status, aai_transcript_id,
                    duration_sec, draft)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, payload, ts, user_id, data.get("status"), None,
                 duration_seconds(data), draft),
            )
        return True


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
                    created_at INTEGER NOT NULL,
                    session_version INTEGER NOT NULL DEFAULT 1
                )"""
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
            if "session_version" not in cols:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"
                )

            # A username can be reused after deletion; its identity cannot.
            # Existing accounts get an ID once. Pre-migration cookies have no
            # ID and are intentionally revoked by the new token verifier.
            if "account_id" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN account_id TEXT")
            for (username,) in conn.execute(
                "SELECT username FROM users WHERE account_id IS NULL"
            ).fetchall():
                conn.execute(
                    "UPDATE users SET account_id = ? WHERE username = ?",
                    (secrets.token_hex(16), username),
                )

    def get_account_id(self, username: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT account_id FROM users WHERE username = ?", (username,)).fetchone()
        return row[0] if row else None

    def get_session_identity(self, username: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT account_id, session_version FROM users WHERE username = ?", (username,)).fetchone()
        return f"{row[0]}:{row[1]}" if row and row[0] else None

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
                    "INSERT INTO users (username, password_hash, display_name, created_at, account_id) VALUES (?, ?, ?, ?, ?)",
                    (username, pw_hash, display_name or username.capitalize(), int(time.time()), secrets.token_hex(16)),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def verify(self, username: str, password: str) -> bool:
        """Verify login credentials against stored hash, upgrading legacy hashes on success."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        if not row:
            return False
        stored_hash = row[0]
        if not verify_password(password, stored_hash):
            return False

        # If stored hash is legacy SHA-256 format, silently upgrade to PBKDF2
        if not stored_hash.startswith("pbkdf2:"):
            new_hash = hash_password(password)
            try:
                with self._lock, self._conn() as conn:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE username = ?",
                        (new_hash, username),
                    )
                log.info(f"Upgraded password hash to PBKDF2 for user {username!r}")
            except Exception as e:
                log.warning(f"Failed to upgrade password hash for {username!r}: {e}")

        return True

    def exists(self, username: str) -> bool:
        """Check if a user account exists."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
        return row is not None

    def get_session_version(self, username: str) -> Optional[int]:
        """Return the token generation for a user, or None for an unknown user."""
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT session_version FROM users WHERE username = ?", (username,)
            ).fetchone()
        return int(row[0]) if row else None

    def get_display_name(self, username: str) -> Optional[str]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT display_name FROM users WHERE username = ?", (username,)
            ).fetchone()
        return row[0] if row else None

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
        """Change a password and revoke all existing sessions for the user."""
        pw_hash = hash_password(new_password)
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """UPDATE users
                   SET password_hash = ?, session_version = session_version + 1
                   WHERE username = ?""",
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
