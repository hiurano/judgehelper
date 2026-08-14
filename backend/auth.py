"""
Authentication and session management module for Judge Helper.
Provides cookie-based session verification, HMAC tokens, and login/logout routes.
"""
import base64
import hashlib
import hmac
import secrets
import time
from typing import Optional

try:
    from fastapi import Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
except ImportError:
    Request = None
    class HTMLResponse:
        def __init__(self, content="", **kwargs):
            self.content = content
    class JSONResponse:
        def __init__(self, content=None, status_code=200, **kwargs):
            self.content = content
            self.status_code = status_code
    class RedirectResponse:
        def __init__(self, url="", status_code=307, **kwargs):
            self.url = url
            self.status_code = status_code
        def set_cookie(self, *args, **kwargs): pass
        def delete_cookie(self, *args, **kwargs): pass

from backend.config import (
    SECRET_KEY,
    STATIC_DIR,
    WEBHOOK_SECRET,
    log,
)
from backend.db import user_store

SESSION_COOKIE = "judge_helper_session"
SESSION_DURATION = 60 * 60 * 24 * 30  # 30 days

PUBLIC_PATHS = {
    "/health",
    "/webhook/aai",
    "/login",
    "/logout",
    "/manifest.json",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static/",)


def verify_user_credentials(username: str, password: str) -> bool:
    return user_store.verify(username, password)


_ephemeral_dev_secret: Optional[str] = None


def _session_secret() -> str:
    global _ephemeral_dev_secret
    if SECRET_KEY:
        return SECRET_KEY
    if WEBHOOK_SECRET:
        return WEBHOOK_SECRET
    if _ephemeral_dev_secret is None:
        _ephemeral_dev_secret = secrets.token_hex(32)
        log.warning(
            "Neither SECRET_KEY nor WEBHOOK_SECRET is set in environment. "
            "Generated an ephemeral in-memory session secret for this process."
        )
    return _ephemeral_dev_secret


def make_session_token(username: str) -> str:
    expiry = int(time.time()) + SESSION_DURATION
    payload = f"{username}|{expiry}"
    sig = hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_session_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        username, expiry_str, sig = decoded.rsplit("|", 2)
        if int(expiry_str) < int(time.time()):
            return None
        expected = hmac.new(
            _session_secret().encode(),
            f"{username}|{expiry_str}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return username
    except Exception:
        return None


async def session_auth_middleware(request: Request, call_next):
    """Cookie-based session auth middleware."""
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE)
    username = verify_session_token(token)
    if username and user_store.exists(username):
        request.state.user = username
        return await call_next(request)

    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse({"detail": "Не авторизованы"}, status_code=401)


_login_template_cache: Optional[str] = None


def _get_login_template() -> str:
    """Load and cache the login page HTML template from static/login.html."""
    global _login_template_cache
    if _login_template_cache is None:
        template_file = STATIC_DIR / "login.html"
        if template_file.exists():
            _login_template_cache = template_file.read_text(encoding="utf-8")
        else:
            _login_template_cache = (
                "<!DOCTYPE html><html><head><title>Вход</title></head>"
                "<body><h1>Вход</h1>__ERROR__"
                "<form method='POST' action='/login'>"
                "<input name='username' required/><input type='password' name='password' required/>"
                "<button type='submit'>Войти</button></form></body></html>"
            )
    return _login_template_cache


def _login_page(error: str = "") -> HTMLResponse:
    block = f'<div class="error">{error}</div>' if error else ""
    html = _get_login_template().replace("__ERROR__", block)
    return HTMLResponse(content=html)


async def login_page_handler(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    user = verify_session_token(token)
    if user and user_store.exists(user):
        return RedirectResponse(url="/", status_code=303)
    return _login_page()


async def login_submit_handler(
    username: str,
    password: str,
    request: Optional[Request] = None,
):
    clean_user = (username or "").strip()
    if not verify_user_credentials(clean_user, password):
        log.warning(f"Failed login: username={clean_user!r}")
        return _login_page(error="Неверное имя пользователя или пароль")

    token = make_session_token(clean_user)
    resp = RedirectResponse(url="/", status_code=303)

    is_https = False
    if request is not None and hasattr(request, "headers"):
        proto = request.headers.get("x-forwarded-proto", getattr(request.url, "scheme", "http"))
        is_https = (proto == "https")

    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DURATION,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )
    log.info(f"Login successful for {clean_user!r}")
    return resp


async def logout_handler():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
