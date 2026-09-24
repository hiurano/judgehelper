"""
Authentication and session management module for Judge Helper.
Provides cookie-based session verification, HMAC tokens, and login/logout routes.
"""
import asyncio
import base64
import hashlib
import hmac
import secrets
import time
import threading
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
LOGIN_WINDOW_SECONDS = 15 * 60
# Counted per (username, address). Keying on the username alone let anyone who
# knew it lock the real user out by failing five times from anywhere.
LOGIN_MAX_FAILURES = 5
# And per address, so one caller cannot work through a list of usernames with
# five free guesses each.
LOGIN_MAX_FAILURES_PER_IP = 20

PUBLIC_PATHS = {
    "/health",
    "/ready",
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
_login_attempts: dict[str, list[float]] = {}
_login_attempts_lock = threading.Lock()


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
    session_identity = user_store.get_session_identity(username)
    if session_identity is None:
        raise ValueError("Cannot create a session for an unknown user")
    expiry = int(time.time()) + SESSION_DURATION
    payload = f"{username}|{session_identity}|{expiry}"
    sig = hmac.new(_session_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def verify_session_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        username, identity, expiry_str, sig = decoded.rsplit("|", 3)
        if int(expiry_str) < int(time.time()):
            return None
        expected = hmac.new(
            _session_secret().encode(),
            f"{username}|{identity}|{expiry_str}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        current_identity = user_store.get_session_identity(username)
        if current_identity is None or identity != current_identity:
            return None
        return username
    except Exception:
        return None


class SessionAuthMiddleware:
    """Pure ASGI cookie authentication middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        username = verify_session_token(request.cookies.get(SESSION_COOKIE))
        if username:
            scope.setdefault("state", {})["user"] = username
            await self.app(scope, receive, send)
            return

        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            response = RedirectResponse(url="/login", status_code=303)
        else:
            response = JSONResponse({"detail": "Не авторизованы"}, status_code=401)
        await response(scope, receive, send)


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


def _login_page(error: str = "", status_code: int = 200) -> HTMLResponse:
    block = f'<div class="error">{error}</div>' if error else ""
    html = _get_login_template().replace("__ERROR__", block)
    return HTMLResponse(content=html, status_code=status_code)


def _client_address(request: Optional[Request]) -> str:
    """The caller's address as uvicorn resolved it behind the proxy.

    Unknown addresses all share one bucket rather than each getting their own
    unlimited allowance."""
    client = getattr(request, "client", None) if request is not None else None
    return getattr(client, "host", None) or "<unknown>"


def _recent_failures(key: str, now: float) -> list[float]:
    """Failures inside the window, pruning what has aged out. Caller holds the lock."""
    kept = [
        ts for ts in _login_attempts.get(key, [])
        if now - ts < LOGIN_WINDOW_SECONDS
    ]
    if kept:
        _login_attempts[key] = kept
    else:
        _login_attempts.pop(key, None)
    return kept


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
    if len(clean_user) > 128 or len(password or "") > 1024:
        return _login_page(error="Неверное имя пользователя или пароль")
    address = _client_address(request)
    user_key = f"user:{clean_user.casefold() or '<empty>'}|{address}"
    ip_key = f"ip:{address}"
    now = time.monotonic()
    with _login_attempts_lock:
        if len(_login_attempts) > 10_000:
            cutoff = now - LOGIN_WINDOW_SECONDS
            for key in list(_login_attempts):
                kept = [ts for ts in _login_attempts[key] if ts >= cutoff]
                if kept:
                    _login_attempts[key] = kept
                else:
                    _login_attempts.pop(key, None)
        user_failures = len(_recent_failures(user_key, now))
        ip_failures = len(_recent_failures(ip_key, now))
        limited = user_failures >= LOGIN_MAX_FAILURES or ip_failures >= LOGIN_MAX_FAILURES_PER_IP
        if not limited:
            # Reserve before yielding to password verification. In-flight
            # attempts count too, including ones whose caller disconnects.
            _login_attempts.setdefault(user_key, []).append(now)
            _login_attempts.setdefault(ip_key, []).append(now)
    if limited:
        log.warning(
            "Login rate limit reached: username=%r address=%s (%s for this pair, %s from this address)",
            clean_user, address, user_failures, ip_failures,
        )
        # A form post deserves the form back, not raw JSON in the viewport.
        return _login_page(
            error="Слишком много попыток входа. Повторите через 15 минут.",
            status_code=429,
        )

    valid = await asyncio.to_thread(verify_user_credentials, clean_user, password)
    if not valid:
        log.warning(f"Failed login: username={clean_user!r} address={address}")
        return _login_page(error="Неверное имя пользователя или пароль")

    with _login_attempts_lock:
        # Release only this successful attempt, preserving other failures and
        # reservations made concurrently for the same pair/address.
        for key in (user_key, ip_key):
            entries = _login_attempts.get(key, [])
            if now in entries:
                entries.remove(now)
            if not entries:
                _login_attempts.pop(key, None)

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
