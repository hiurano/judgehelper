"""
Unit tests for SQLite JobStore database module.
"""
import time
try:
    import pytest
except ImportError:
    class pytest:
        @staticmethod
        def fixture(fn):
            return fn
from backend.db import JobStore


@pytest.fixture
def temp_job_store(tmp_path):
    db_file = tmp_path / "test_jobs.db"
    return JobStore(str(db_file))


def test_job_store_basic_crud(temp_job_store):
    store = temp_job_store
    assert len(store) == 0
    assert "job1" not in store
    assert store.get("job1") is None

    data = {"status": "processing", "user_id": "test"}
    store["job1"] = data

    assert len(store) == 1
    assert "job1" in store
    retrieved = store["job1"]
    assert retrieved["status"] == "processing"
    assert retrieved["user_id"] == "test"


def test_job_store_aai_id_lookup_and_stats(temp_job_store):
    store = temp_job_store
    store["job-123"] = {"status": "done", "aai_transcript_id": "aai-abc", "duration_min": 10.5, "user_id": "test"}

    found_id, data = store.get_by_aai_id("aai-abc")
    assert found_id == "job-123"
    assert data["status"] == "done"

    found_id_direct, data_direct = store.get_by_aai_id("job-123")
    assert found_id_direct == "job-123"
    assert data_direct["status"] == "done"

    stats = store.get_user_stats("test")
    assert stats["total_protocols"] == 1
    assert stats["total_duration_min"] == 10.5


def test_job_store_cleanup_old(temp_job_store):
    store = temp_job_store
    store["old_job"] = {"status": "done"}
    # Force updated_at to 40 days ago
    with store._conn() as conn:
        conn.execute("UPDATE jobs SET updated_at = ? WHERE id = 'old_job'", (int(time.time()) - 40 * 86400,))

    store["new_job"] = {"status": "processing"}

    assert len(store) == 2
    deleted = store.cleanup_old(30)
    assert deleted == 1
    assert "old_job" not in store
    assert "new_job" in store


def test_job_store_get_pending_jobs(temp_job_store):
    store = temp_job_store
    store["job-done"] = {"status": "done"}
    store["job-processing"] = {"status": "processing", "phase": "transcribing"}

    pending = store.get_pending_jobs()
    assert len(pending) == 1
    assert pending[0]["id"] == "job-processing"
    assert pending[0]["phase"] == "transcribing"


def test_job_store_reserves_active_slots_atomically(temp_job_store):
    first = {"status": "processing", "user_id": "alice"}
    second = {"status": "processing", "user_id": "alice"}
    assert temp_job_store.create_if_under_active_limit("job-1", first, 1) is True
    assert temp_job_store.create_if_under_active_limit("job-2", second, 1) is False
    assert "job-2" not in temp_job_store


def test_password_hashing_and_legacy_upgrade(tmp_path):
    import hashlib
    from backend.db import UserStore, hash_password, verify_password

    # 1. PBKDF2 hash verification
    pwd = "SecretPassword123!"
    stored = hash_password(pwd)
    assert stored.startswith("pbkdf2:600000:")
    assert verify_password(pwd, stored) is True
    assert verify_password("WrongPassword", stored) is False

    # 2. Legacy SHA-256 (salt:hash) backward compatibility
    legacy_salt = "1234567890abcdef"
    legacy_hash = hashlib.sha256(f"{legacy_salt}:{pwd}".encode()).hexdigest()
    legacy_stored = f"{legacy_salt}:{legacy_hash}"
    assert verify_password(pwd, legacy_stored) is True
    assert verify_password("WrongPassword", legacy_stored) is False

    # 3. UserStore transparent upgrade from legacy hash to PBKDF2 on verify
    user_db = tmp_path / "test_users.db"
    store = UserStore(str(user_db))

    # Manually insert user with legacy hash
    with store._conn() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
            ("legacy_user", legacy_stored, "Legacy", int(time.time())),
        )

    # First verify should succeed and trigger upgrade
    assert store.verify("legacy_user", pwd) is True

    # Check that the hash was upgraded in SQLite
    with store._conn() as conn:
        new_row = conn.execute("SELECT password_hash FROM users WHERE username = 'legacy_user'").fetchone()
    assert new_row[0].startswith("pbkdf2:600000:")
    # Subsequent login works with upgraded hash
    assert store.verify("legacy_user", pwd) is True


def test_password_change_increments_session_version(tmp_path):
    from backend.db import UserStore

    store = UserStore(str(tmp_path / "users.db"))
    assert store.create_user("alice", "old-password")
    original = store.get_session_version("alice")
    assert store.change_password("alice", "new-password")
    assert store.get_session_version("alice") == original + 1
