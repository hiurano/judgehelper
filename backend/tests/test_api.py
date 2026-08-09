"""
Integration tests for FastAPI endpoints.
"""
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.auth import SESSION_COOKIE, make_session_token

client = TestClient(app)


@pytest.fixture
def auth_client(monkeypatch):
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "ASSEMBLYAI_KEY", "test-aai-key-12345")
    test_client = TestClient(app)
    test_client.cookies.set(SESSION_COOKIE, make_session_token("elena"))
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
    assert data["username"] == "elena"
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
        "user_id": "elena",
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
    token = make_session_token("elena")
    username = verify_session_token(token)
    assert username == "elena"


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


