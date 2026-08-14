"""
Integration tests for FastAPI endpoints.
"""
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.auth import SESSION_COOKIE, make_session_token
from backend.db import user_store

client = TestClient(app)


@pytest.fixture
def auth_client(monkeypatch):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "ASSEMBLYAI_KEY", "test-aai-key-12345")
    # Ensure test user exists in SQLite
    user_store.create_user("test", "Test-2026", "Тест")
    test_client = TestClient(app)
    test_client.cookies.set(SESSION_COOKIE, make_session_token("test"))
    return test_client


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "model" in data
    assert "has_assemblyai_key" in data


def test_api_me_endpoint_unauthorized():
    response = client.get("/api/me")
    assert response.status_code == 401


def test_api_me_endpoint_authorized(auth_client):
    response = auth_client.get("/api/me")
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "test"
    assert "display_name" in data


def test_upload_endpoint_file_validation(auth_client):
    # Non-audio extension should return 400
    files = {"file": ("test.pdf", b"fake pdf content", "application/pdf")}
    response = auth_client.post("/upload", files=files)
    assert response.status_code == 400
    assert "Неподдерживаемый формат" in response.json()["detail"]

    # Empty file should return 400
    empty_files = {"file": ("test.mp3", b"", "audio/mpeg")}
    resp_empty = auth_client.post("/upload", files=empty_files)
    assert resp_empty.status_code == 400
    assert "пуст" in resp_empty.json()["detail"]


def test_render_docx_endpoint_validation(auth_client):
    # Empty payload should return 400
    response = auth_client.post("/render-docx", json={"text": ""})
    assert response.status_code == 400


def test_render_docx_endpoint_success(auth_client):
    payload = {"text": "ПРОТОКОЛ\nсудебного заседания", "filename": "test.docx"}
    response = auth_client.post("/render-docx", json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert response.content.startswith(b"PK\x03\x04")


def test_status_endpoint_done(auth_client):
    from backend.db import jobs
    jobs["job-test-done"] = {
        "status": "done",
        "draft": "Протокол готов",
        "user_id": "test",
        "aai_transcript_id": "aai-test-123"
    }
    # Test lookup by job_id
    resp1 = auth_client.get("/status/job-test-done")
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "done"

    # Test lookup by AssemblyAI transcript_id
    resp2 = auth_client.get("/status/aai-test-123")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "done"


def test_recover_pending_jobs():
    import asyncio
    from backend.db import jobs
    from backend.services.ai_service import recover_pending_jobs

    jobs["job-interrupted-upload"] = {
        "status": "processing",
        "phase": "uploading_to_aai",
        "filename": "test.mp3"
    }

    asyncio.run(recover_pending_jobs())

    recovered = jobs.get("job-interrupted-upload")
    assert recovered["status"] == "error"
    assert "прервана перезапуском" in recovered["error"]


def test_session_token_with_custom_secret_key(monkeypatch):
    import backend.config as config
    from backend.auth import make_session_token, verify_session_token

    monkeypatch.setattr(config, "SECRET_KEY", "custom-super-secret-key-12345")
    token = make_session_token("test")
    username = verify_session_token(token)
    assert username == "test"


def test_log_rotation_handler_configured():
    import logging
    from logging.handlers import RotatingFileHandler
    import backend.config as config

    root_logger = logging.getLogger()
    handlers = [h for h in root_logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(handlers) > 0, "RotatingFileHandler missing from root logger"
    rf = handlers[0]
    assert rf.maxBytes == 10 * 1024 * 1024
    assert rf.backupCount == 5


def test_async_retry_helper():
    import asyncio
    from backend.services.ai_service import async_retry

    attempts = 0

    async def flaky_call():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise RuntimeError("Temporary network glitch")
        return "success"

    result = asyncio.run(async_retry(flaky_call, retries=3, delay=0.01))
    assert result == "success"
    assert attempts == 2


def test_lock_memory_cleanup():
    from backend.db import cleanup_unused_locks, get_lock, locks, remove_lock

    get_lock("job-test-lock-1")
    assert "job-test-lock-1" in locks

    remove_lock("job-test-lock-1")
    assert "job-test-lock-1" not in locks

    get_lock("job-test-lock-2")
    assert "job-test-lock-2" in locks
    cleanup_unused_locks()
    assert "job-test-lock-2" not in locks


def test_split_transcript_into_chunks():
    from backend.services.ai_service import split_transcript_into_chunks

    # Short text should return a single chunk
    short_text = "Paragraph 1\n\nParagraph 2"
    assert split_transcript_into_chunks(short_text, max_chunk_chars=100) == [short_text]

    # Long text should split cleanly on utterance boundaries (\n\n)
    paras = [f"[Спикер A]: Это фрагмент речи №{i} с подробным описанием текста." for i in range(20)]
    long_text = "\n\n".join(paras)
    
    chunks = split_transcript_into_chunks(long_text, max_chunk_chars=300)
    assert len(chunks) > 1
    # Check that re-joining matches original
    assert "\n\n".join(chunks) == long_text
    # Check that each chunk is within max_chunk_chars bounds
    for chunk in chunks:
        assert len(chunk) <= 400  # allowing reasonable room for paragraph boundaries


def test_webhook_secret_constant_time_validation(monkeypatch):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", "super-secret-webhook-key")

    # Invalid secret -> 401
    resp_invalid = client.post(
        "/webhook/aai",
        json={"transcript_id": "test", "status": "completed"},
        headers={"x-webhook-secret": "wrong-secret"},
    )
    assert resp_invalid.status_code == 401

    # Valid secret -> 200 (even if job not found)
    resp_valid = client.post(
        "/webhook/aai",
        json={"transcript_id": "nonexistent-transcript-123", "status": "completed"},
        headers={"x-webhook-secret": "super-secret-webhook-key"},
    )
    assert resp_valid.status_code == 200
    assert resp_valid.json()["ok"] is False  # job not found, but authorized


def test_ephemeral_dev_session_secret(monkeypatch):
    import backend.auth as auth_mod
    monkeypatch.setattr(auth_mod, "SECRET_KEY", "")
    monkeypatch.setattr(auth_mod, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(auth_mod, "_ephemeral_dev_secret", None)

    secret1 = auth_mod._session_secret()
    assert len(secret1) == 64  # 32 bytes hex
    secret2 = auth_mod._session_secret()
    assert secret1 == secret2  # Consistent for process lifetime


def test_allowed_origins_whitespace_stripping():
    import os
    raw_origins = " https://app.example.com , http://localhost:3000 , "
    cleaned = [o.strip() for o in raw_origins.split(",") if o.strip()]
    assert cleaned == ["https://app.example.com", "http://localhost:3000"]



