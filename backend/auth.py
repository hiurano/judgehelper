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
    SECRET_KEY,
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


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Вход — Помощник секретаря</title>
    <link rel="apple-touch-icon" href="/apple-touch-icon.png">
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <meta name="theme-color" content="#111111">
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
            background: #111111;
            color: #E0E0E0;
            -webkit-text-size-adjust: 100%;
        }
        .login-wrapper {
            display: flex;
            align-items: stretch;
            justify-content: center;
            gap: 1.75rem;
            width: 100%;
            max-width: 820px;
        }
        @media (max-width: 768px) {
            .login-wrapper {
                flex-direction: column;
                max-width: 400px;
            }
            .changelog-card {
                display: none;
            }
        }
        .card {
            background: #191919; border: 1px solid #3C3C3C;
            border-radius: 14px; padding: 2.25rem 1.75rem;
            box-shadow: 0 16px 32px rgba(0,0,0,0.4);
            flex: 1;
            min-width: 320px;
        }
        .logo {
            width: 64px; height: 64px; margin: 0 auto 1.25rem;
            background: #AAAAAA; color: #111111;
            border-radius: 14px;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.5rem; font-weight: 700;
            letter-spacing: -0.03em;
        }
        h1 {
            text-align: center; margin: 0 0 0.35rem;
            font-size: 1.35rem; font-weight: 600;
            color: #E0E0E0;
        }
        .subtitle {
            text-align: center; color: #828282;
            margin: 0 0 1.75rem; font-size: 0.9rem;
        }
        .error {
            background: rgba(221, 221, 221, 0.1); border: 1px solid rgba(221, 221, 221, 0.3);
            color: #DDDDDD;
            border-radius: 8px; padding: 0.625rem 0.875rem;
            font-size: 0.85rem; margin-bottom: 1.25rem;
        }
        label {
            display: block; margin-bottom: 1rem;
            font-size: 0.85rem; color: #828282; font-weight: 500;
        }
        input {
            display: block; width: 100%;
            margin-top: 0.375rem;
            padding: 0.75rem 0.875rem;
            border: 1px solid #3C3C3C; border-radius: 8px;
            font-size: 0.95rem; font-family: inherit;
            min-height: 44px;
            background: #151515; color: #E0E0E0;
            -webkit-appearance: none;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        input:focus { outline: none; border-color: #AAAAAA; box-shadow: 0 0 0 2px rgba(170, 170, 170, 0.25); }
        input:-webkit-autofill,
        input:-webkit-autofill:hover, 
        input:-webkit-autofill:focus, 
        input:-webkit-autofill:active {
            -webkit-box-shadow: 0 0 0 1000px #151515 inset !important;
            -webkit-text-fill-color: #E0E0E0 !important;
            caret-color: #E0E0E0 !important;
            transition: background-color 50000s ease-in-out 0s;
        }
        button {
            width: 100%; min-height: 46px;
            background: #AAAAAA; color: #111111; border: none;
            border-radius: 8px; padding: 0.875rem 1.25rem;
            font-size: 0.975rem; font-family: inherit; font-weight: 600; cursor: pointer;
            margin-top: 0.5rem;
            transition: background-color 0.15s, transform 0.1s;
        }
        button:hover { background: #CCCCCC; }
        button:active { transform: scale(0.98); }

        /* Changelog Side Panel */
        .changelog-card {
            background: #191919;
            border: 1px solid #3C3C3C;
            border-radius: 14px;
            padding: 2.25rem 1.75rem;
            box-shadow: 0 16px 32px rgba(0,0,0,0.4);
            flex: 1;
            min-width: 320px;
            display: flex;
            flex-direction: column;
        }
        .changelog-header {
            display: flex;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 1.25rem;
            padding-bottom: 0.875rem;
            border-bottom: 1px solid #3C3C3C;
        }
        .changelog-icon {
            font-size: 1.25rem;
        }
        .changelog-title {
            font-size: 1.05rem;
            font-weight: 600;
            color: #E0E0E0;
        }
        .changelog-list {
            display: flex;
            flex-direction: column;
            gap: 1.15rem;
            max-height: 360px;
            overflow-y: auto;
            padding-right: 6px;
        }
        .changelog-list::-webkit-scrollbar {
            width: 5px;
        }
        .changelog-list::-webkit-scrollbar-thumb {
            background: #3C3C3C;
            border-radius: 3px;
        }
        .changelog-item {
            border-left: 2px solid #AAAAAA;
            padding-left: 0.875rem;
        }
        .changelog-date {
            font-size: 0.8rem;
            font-weight: 600;
            color: #AAAAAA;
            margin-bottom: 0.25rem;
        }
        .changelog-desc {
            font-size: 0.85rem;
            color: #828282;
            line-height: 1.45;
        }
    </style>
</head>
<body>
    <div class="login-wrapper">
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

        <div class="changelog-card">
            <div class="changelog-header">
                <div class="changelog-title">История обновлений</div>
            </div>

            <div class="changelog-list">
                <div class="changelog-item">
                    <div class="changelog-date">09 августа 2026</div>
                    <div class="changelog-desc">Архитектурный рефакторинг бэкенда: zero-RAM загрузка, гибридный вебхук/поллинг, турбо-база данных на индексах и динамическая смена промптов.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">07 августа 2026</div>
                    <div class="changelog-desc">Умная фоновая очередь файлов, расчёт сэкономленного времени и нативная монохромная тема Noctalia.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">06 августа 2026</div>
                    <div class="changelog-desc">Переход на GPT-4o-mini / Gemini Flash, ускорение генерации в 2.5 раза и таймер ETA.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">05 августа 2026</div>
                    <div class="changelog-desc">Автоматическое исправление ASR оговорок распознавания редких судебных фамилий.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">04 августа 2026</div>
                    <div class="changelog-desc">Анализ 58 эталонных судебных протоколов и нативная вёрстка Word (.docx) по стандартам ГОСТ.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">03 июля 2026</div>
                    <div class="changelog-desc">Точный режим дословного протоколирования («Анти-сокращение») и поддержка 16 000 токенов.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">02 июля 2026</div>
                    <div class="changelog-desc">Авто-разбор фамилий из названий файлов (`[Фамилия]_[Дата]_протокол.docx`) и пакетный drag-and-drop.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">01 июля 2026</div>
                    <div class="changelog-desc">Интеграция AssemblyAI Speech Models с динамическим бустом судебной терминологии.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">30 июня 2026</div>
                    <div class="changelog-desc">Унификация интерфейса под строгую монохромную тему и внедрение векторных иконок.</div>
                </div>
                <div class="changelog-item">
                    <div class="changelog-date">29 июня 2026</div>
                    <div class="changelog-desc">Первый релиз архитектуры Помощника Секретаря на базе FastAPI, SQLite и локальной ИИ-обработки.</div>
                </div>
            </div>
        </div>
    </div>
</body>
</html>"""


def _login_page(error: str = "") -> HTMLResponse:
    block = f'<div class="error">{error}</div>' if error else ""
    html = LOGIN_HTML.replace("__ERROR__", block)
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
):
    clean_user = (username or "").strip()
    if not verify_user_credentials(clean_user, password):
        log.warning(f"Failed login: username={clean_user!r}")
        return _login_page(error="Неверное имя пользователя или пароль")

    token = make_session_token(clean_user)
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
    log.info(f"Login successful for {clean_user!r}")
    return resp


async def logout_handler():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp
