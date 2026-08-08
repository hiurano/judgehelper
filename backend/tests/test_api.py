"""
Integration tests for FastAPI endpoints.
"""
from fastapi.testclient import TestClient
from backend.main import app
from backend.auth import SESSION_COOKIE, make_session_token

client = TestClient(app)

# Helper token for authenticated requests
auth_cookie = {SESSION_COOKIE: make_session_token("elena")}


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


def test_api_me_endpoint_authorized():
    response = client.get("/api/me", cookies=auth_cookie)
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "elena"
    assert "display_name" in data


def test_render_docx_endpoint_validation():
    # Empty payload should return 400
    response = client.post("/render-docx", json={"text": ""}, cookies=auth_cookie)
    assert response.status_code == 400


def test_render_docx_endpoint_success():
    payload = {"text": "ПРОТОКОЛ\nсудебного заседания", "filename": "test.docx"}
    response = client.post("/render-docx", json=payload, cookies=auth_cookie)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert response.content.startswith(b"PK\x03\x04")
