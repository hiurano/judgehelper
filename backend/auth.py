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

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from backend.config import (
    AUTH_PASSWORD,
    AUTH_USERNAME,
    WEBHOOK_SECRET,
    log,
)

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


def _session_secret() -> str:
    return WEBHOOK_SECRET or "fallback-dev-secret-do-not-use-in-prod"


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
    if not (AUTH_USERNAME and AUTH_PASSWORD):
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE)
    username = verify_session_token(token)
    if username == AUTH_USERNAME:
        return await call_next(request)

    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse({"detail": "Не авторизованы"}, status_code=401)


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Вход — Помощник секретаря</title>
    <link rel="apple-touch-icon" href="/apple-touch-icon.png">
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <meta name="theme-color" content="#232A2E">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0; min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
            padding: 1.5rem;
            background: #232A2E;
            color: #D3C6AA;
            -webkit-text-size-adjust: 100%;
        }
        .card {
            background: #2D353B; border: 1px solid #475258;
            border-radius: 14px; padding: 2.25rem 1.75rem;
            box-shadow: 0 16px 32px rgba(0,0,0,0.25);
            width: 100%; max-width: 380px;
        }
        .logo {
            width: 64px; height: 64px; margin: 0 auto 1.25rem;
            background: #A7C080; color: #1E2326;
            border-radius: 14px;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.5rem; font-weight: 700;
            letter-spacing: -0.03em;
        }
        h1 {
            text-align: center; margin: 0 0 0.35rem;
            font-size: 1.35rem; font-weight: 600;
            color: #D3C6AA;
        }
        .subtitle {
            text-align: center; color: #859289;
            margin: 0 0 1.75rem; font-size: 0.9rem;
        }
        .error {
            background: rgba(230, 126, 128, 0.08); border: 1px solid rgba(230, 126, 128, 0.3);
            color: #E67E80;
            border-radius: 8px; padding: 0.625rem 0.875rem;
            font-size: 0.85rem; margin-bottom: 1.25rem;
        }
        label {
            display: block; margin-bottom: 1rem;
            font-size: 0.85rem; color: #859289; font-weight: 500;
        }
        input {
            display: block; width: 100%;
            margin-top: 0.375rem;
            padding: 0.75rem 0.875rem;
            border: 1px solid #475258; border-radius: 8px;
            font-size: 0.95rem; font-family: inherit;
            min-height: 44px;
            background: #1E2326; color: #D3C6AA;
            -webkit-appearance: none;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        input:focus { outline: none; border-color: #A7C080; box-shadow: 0 0 0 2px rgba(167, 192, 128, 0.2); }
        input:-webkit-autofill,
        input:-webkit-autofill:hover, 
        input:-webkit-autofill:focus, 
        input:-webkit-autofill:active {
            -webkit-box-shadow: 0 0 0 1000px #1E2326 inset !important;
            -webkit-text-fill-color: #D3C6AA !important;
            caret-color: #D3C6AA !important;
            transition: background-color 50000s ease-in-out 0s;
        }
        button {
            width: 100%; min-height: 46px;
            background: #A7C080; color: #1E2326; border: none;
            border-radius: 8px; padding: 0.875rem 1.25rem;
            font-size: 0.975rem; font-family: inherit; font-weight: 600; cursor: pointer;
            margin-top: 0.5rem;
            transition: background-color 0.15s, transform 0.1s;
        }
        button:hover { background: #B8D191; }
        button:active { transform: scale(0.98); }
    </style>
</head>
<body>
    <form class="card" method="POST" action="/login" autocomplete="on">
        <div class="logo">ПС</div>
        <h1>Помощник секретаря</h1>
        <p class="subtitle">Войдите, чтобы продолжить</p>
        __ERROR__
        <label>
            Имя пользователя
            <input type="text" name="username" required autocomplete="username" autofocus
                   autocapitalize="off" autocorrect="off" spellcheck="false">
        </label>
        <label>
            Пароль
            <input type="password" name="password" required autocomplete="current-password">
        </label>
        <button type="submit">Войти</button>
    </form>
</body>
</html>"""


def _login_page(error: str = "") -> HTMLResponse:
    block = f'<div class="error">{error}</div>' if error else ""
    html = LOGIN_HTML.replace("__ERROR__", block)
    return HTMLResponse(content=html)


async def login_page_handler(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if AUTH_USERNAME and verify_session_token(token) == AUTH_USERNAME:
        return RedirectResponse(url="/", status_code=303)
    return _login_page()


async def login_submit_handler(
    username: str,
    password: str,
):
    if not (AUTH_USERNAME and AUTH_PASSWORD):
        return RedirectResponse(url="/", status_code=303)

    ok_user = secrets.compare_digest(username, AUTH_USERNAME)
    ok_pass = secrets.compare_digest(password, AUTH_PASSWORD)
    if not (ok_user and ok_pass):
        log.warning(f"Failed login: username={username!r}")
        return _login_page(error="Неверное имя пользователя или пароль")

    token = make_session_token(AUTH_USERNAME)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DURATION,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    log.info(f"Login successful for {username!r}")
    return resp


async def logout_handler():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
