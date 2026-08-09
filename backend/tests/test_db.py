"""
Unit tests for SQLite JobStore database module.
"""
import time
import pytest
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

    data = {"status": "processing", "user_id": "elena"}
    store["job1"] = data

    assert len(store) == 1
    assert "job1" in store
    retrieved = store["job1"]
    assert retrieved["status"] == "processing"
    assert retrieved["user_id"] == "elena"


def test_job_store_aai_id_lookup_and_stats(temp_job_store):
    store = temp_job_store
    store["job-123"] = {"status": "done", "aai_transcript_id": "aai-abc", "duration_min": 10.5, "user_id": "elena"}

    found_id, data = store.get_by_aai_id("aai-abc")
    assert found_id == "job-123"
    assert data["status"] == "done"

    found_id_direct, data_direct = store.get_by_aai_id("job-123")
    assert found_id_direct == "job-123"
    assert data_direct["status"] == "done"

    stats = store.get_user_stats("elena")
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
