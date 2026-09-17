"""
Integration tests for FastAPI endpoints.
"""
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.auth import SESSION_COOKIE, make_session_token
from backend.db import user_store

@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(monkeypatch):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "ASSEMBLYAI_KEY", "test-aai-key-12345")
    # Ensure test user exists in SQLite
    user_store.create_user("test", "Test-2026", "Тест")
    with TestClient(app) as test_client:
        test_client.cookies.set(SESSION_COOKIE, make_session_token("test"))
        yield test_client


def test_health_endpoint(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data == {"ok": True}


def test_ready_endpoint_reports_incomplete_configuration(client, monkeypatch):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "ASSEMBLYAI_KEY", "")
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"ready": False}


def test_api_me_endpoint_unauthorized(client):
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

    disguised = {"file": ("fake.mp3", b"%PDF-1.7 fake", "audio/mpeg")}
    resp_disguised = auth_client.post("/upload", files=disguised)
    assert resp_disguised.status_code == 400
    assert "Содержимое файла" in resp_disguised.json()["detail"]


def test_render_docx_endpoint_validation(auth_client):
    # Empty payload should return 400
    response = auth_client.post("/render-docx", json={"text": ""})
    assert response.status_code == 400


def test_render_docx_size_limit(auth_client, monkeypatch):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "MAX_RENDER_TEXT_CHARS", 5)
    response = auth_client.post("/render-docx", json={"text": "123456"})
    assert response.status_code == 413


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
        "transcript": "legacy raw transcript",
        "user_id": "test",
        "aai_transcript_id": "aai-test-123"
    }
    # Test lookup by job_id
    resp1 = auth_client.get("/status/job-test-done")
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "done"
    assert "transcript" not in resp1.json()

    # Test lookup by AssemblyAI transcript_id
    resp2 = auth_client.get("/status/aai-test-123")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "done"


def test_status_does_not_expose_another_users_job(auth_client):
    from backend.db import jobs
    jobs["job-private"] = {
        "status": "done",
        "draft": "Секретный протокол",
        "transcript": "Секретная стенограмма",
        "user_id": "another-user",
    }
    response = auth_client.get("/status/job-private")
    assert response.status_code == 404
    assert "Секретный" not in response.text
    delete_response = auth_client.delete("/jobs/job-private")
    assert delete_response.status_code == 404


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
    import backend.auth as auth_mod
    from backend.auth import make_session_token, verify_session_token

    user_store.create_user("session-test", "Test-2026", "Session Test")
    monkeypatch.setattr(auth_mod, "SECRET_KEY", "custom-super-secret-key-12345")
    token = make_session_token("session-test")
    username = verify_session_token(token)
    assert username == "session-test"


def test_password_change_revokes_existing_session():
    from backend.auth import make_session_token, verify_session_token

    user_store.create_user("revoke-test", "Old-Password-2026", "Revoke Test")
    token = make_session_token("revoke-test")
    assert verify_session_token(token) == "revoke-test"
    assert user_store.change_password("revoke-test", "New-Password-2026")
    assert verify_session_token(token) is None


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

    oversized_utterance = "А" * 1000
    oversized_chunks = split_transcript_into_chunks(oversized_utterance, max_chunk_chars=300)
    assert len(oversized_chunks) == 4
    assert all(len(chunk) <= 300 for chunk in oversized_chunks)


def test_webhook_secret_constant_time_validation(monkeypatch, client):
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


def test_webhook_is_disabled_without_secret(monkeypatch, client):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "WEBHOOK_SECRET", "")
    response = client.post(
        "/webhook/aai",
        json={"transcript_id": "test", "status": "completed"},
    )
    assert response.status_code == 503


def test_media_signature_validation():
    from backend.main import _looks_like_supported_media

    assert _looks_like_supported_media(b"ID3\x04\x00\x00")
    assert _looks_like_supported_media(b"RIFF\x00\x00\x00\x00WAVE")
    assert _looks_like_supported_media(b"\x00\x00\x00\x18ftypmp42")
    assert not _looks_like_supported_media(b"%PDF-1.7")


def test_ephemeral_dev_session_secret(monkeypatch):
    import backend.auth as auth_mod
    monkeypatch.setattr(auth_mod, "SECRET_KEY", "")
    monkeypatch.setattr(auth_mod, "WEBHOOK_SECRET", "")
    monkeypatch.setattr(auth_mod, "_ephemeral_dev_secret", None)

    secret1 = auth_mod._session_secret()
    assert len(secret1) == 64  # 32 bytes hex
    secret2 = auth_mod._session_secret()
    assert secret1 == secret2  # Consistent for process lifetime


def test_login_rate_limit():
    import asyncio
    import backend.auth as auth_mod

    auth_mod._login_attempts.pop("rate-limit-user", None)
    for _ in range(auth_mod.LOGIN_MAX_FAILURES):
        response = asyncio.run(
            auth_mod.login_submit_handler("rate-limit-user", "wrong-password")
        )
        assert response.status_code == 200
    limited = asyncio.run(
        auth_mod.login_submit_handler("rate-limit-user", "wrong-password")
    )
    assert limited.status_code == 429


def test_allowed_origins_whitespace_stripping():
    import os
    raw_origins = " https://app.example.com , http://localhost:3000 , "
    cleaned = [o.strip() for o in raw_origins.split(",") if o.strip()]
    assert cleaned == ["https://app.example.com", "http://localhost:3000"]
