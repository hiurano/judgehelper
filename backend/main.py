"""
Judge Helper backend — audio file -> court protocol draft.

Flow:
  1. POST /upload (multipart)         -> backend uploads to AssemblyAI,
                                         starts transcription with our webhook URL,
                                         returns job_id (= AssemblyAI transcript id)
  2. POST /webhook/aai                -> AssemblyAI calls this when transcription done.
                                         Handler fetches transcript, formats with speaker
                                         labels, calls OpenRouter to draft the protocol,
                                         caches result in memory.
  3. GET  /status/{job_id}            -> frontend polls. Returns "processing" / "done".
                                         If we lost in-memory state (cold restart) and
                                         AssemblyAI says completed, triggers reprocessing.

State is in-memory. Resets on cold restart, but /status falls back to AssemblyAI's
own status so jobs in flight survive — they just re-run the LLM step.

All endpoints async via httpx + asyncio for concurrent file uploads.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from io import BytesIO
from pathlib import Path
from typing import Optional

import httpx
from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("judge-helper")


# --- Config -------------------------------------------------------------
ASSEMBLYAI_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
BASE_URL       = os.environ.get("BASE_URL", "").rstrip("/")
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
MODEL          = os.environ.get("LLM_MODEL", "google/gemini-1.5-flash")
AUTH_USERNAME  = os.environ.get("AUTH_USERNAME", "")
AUTH_PASSWORD  = os.environ.get("AUTH_PASSWORD", "")

# Models tried in order; first success wins. Lets us survive a flaky free tier
# (e.g. Owl Alpha rate-limited or temporarily down).
LLM_FALLBACK_CHAIN: list[str] = [
    MODEL,
    "openai/gpt-4o-mini",
    "anthropic/claude-3-haiku",
]
# dedupe while preserving order
_seen = set()
LLM_FALLBACK_CHAIN = [m for m in LLM_FALLBACK_CHAIN if not (m in _seen or _seen.add(m))]

BACKEND_DIR = Path(__file__).parent
PROJECT_DIR = BACKEND_DIR.parent
SYSTEM_PROMPT_PATH = PROJECT_DIR / "prompts" / "system-protocol.md"
STATIC_DIR = BACKEND_DIR / "static"

for name, val in [
    ("ASSEMBLYAI_API_KEY", ASSEMBLYAI_KEY),
    ("OPENROUTER_API_KEY", OPENROUTER_KEY),
]:
    if not val:
        log.warning(f"{name} not set — endpoint calls will fail")
if not BASE_URL:
    log.warning("BASE_URL not set — AssemblyAI webhooks disabled, /status will poll instead")

SYSTEM_PROMPT = ""
if SYSTEM_PROMPT_PATH.exists():
    SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    log.info(f"Loaded system prompt: {len(SYSTEM_PROMPT)} chars from {SYSTEM_PROMPT_PATH}")
else:
    log.error(f"System prompt not found at {SYSTEM_PROMPT_PATH}")

WORD_BOOST = [
    "ходатайство", "определение суда", "прения сторон", "последнее слово",
    "мера пресечения", "подсудимый", "потерпевший", "защитник",
    "государственный обвинитель", "приговор", "судебное заседание",
    "разъяснение прав", "примирение сторон", "процессуальные издержки",
    "Нижневартовск", "Нижневартовский городской суд",
    "Югра", "Ханты-Мансийский автономный округ",
    "УК РФ", "УПК РФ", "статья", "часть", "пункт",
    "явка с повинной", "вещественные доказательства", "апелляционная жалоба",
]


# Pre-LLM regex cleanup — deterministic fixes for typical AssemblyAI mistakes.
# Order matters (longer phrases first). Each rule (pattern, replacement, flags).
TRANSCRIPT_FIXES: list[tuple[str, str, int]] = [
    # Court / geography
    (r"\bНижневатовск(ий|ого|ому|им|ом)?\b",      r"Нижневартовск\1", re.IGNORECASE),
    (r"\bНижневарковск(ий|ого|ому|им|ом)?\b",     r"Нижневартовск\1", re.IGNORECASE),
    (r"\bНижнего\s+Арктика\b",                     "Нижневартовска",    re.IGNORECASE),
    (r"\bНижневальтовск(ая|ой|ий|ого|ому|им|ом)\b",r"Нижневартовск\1", re.IGNORECASE),
    (r"\bНижний\s+Артефакт\b",                     "Нижневартовск",     re.IGNORECASE),
    (r"\bНижний\s+Арктический\b",                  "Нижневартовск",     re.IGNORECASE),
    (r"\bКонференцийск(ого|ому|им|ом|ий)\b",       r"Ханты-Мансийск\1",  re.IGNORECASE),
    (r"\bсуд\s+Игорьевича\b",                      "суд Югры",          re.IGNORECASE),
    # Codes
    (r"\b10-28\b",                                 "228",               re.IGNORECASE),
    (r"\bкровного\s+кодекса\b",                    "Уголовного кодекса",re.IGNORECASE),
    (r"\bУ\s*КРС\b",                               "УК РФ",             re.IGNORECASE),
    (r"\bУ\s*ПКРС\b",                              "УПК РФ",            re.IGNORECASE),
    # Common mishearings
    (r"\bнеподмение\b",                            "не позднее",        re.IGNORECASE),
    (r"\bпрещени(я|е|ю|ем|и)\b",                   r"пресечени\1",      re.IGNORECASE),
    (r"\bпрофессиональн(ые|ых|ым|ыми)\s+издержк",   r"процессуальн\1 издержк", re.IGNORECASE),
    # Product names
    (r"\bПалларис\b",                              "Polaris",           re.IGNORECASE),
    (r"\bПоларис\b",                               "Polaris",           re.IGNORECASE),
]


def clean_transcript(text: str) -> str:
    """Run deterministic regex fixes before passing to LLM. Faster + more
    reliable than asking the model to correct each instance."""
    if not text:
        return text
    fixed = text
    applied: list[str] = []
    for pattern, replacement, flags in TRANSCRIPT_FIXES:
        new_fixed, n = re.subn(pattern, replacement, fixed, flags=flags)
        if n > 0:
            applied.append(f"{pattern} -> {replacement} (x{n})")
            fixed = new_fixed
    if applied:
        log.info(f"Applied {len(applied)} transcript fixes: {applied[:5]}{'...' if len(applied) > 5 else ''}")
    return fixed


def format_metadata_block(meta: dict) -> str:
    """Convert metadata fields to a prompt prefix telling the LLM these are
    pre-confirmed values that must appear verbatim in the протокол header."""
    if not meta:
        return ""
    parts: list[str] = []
    if meta.get("case_number"):
        parts.append(f"- Номер дела: {meta['case_number']}")
    if meta.get("defendant"):
        parts.append(f"- ФИО подсудимого: {meta['defendant']}")
    if meta.get("statute"):
        parts.append(f"- Статья УК: {meta['statute']}")
    if meta.get("judge"):
        parts.append(f"- Председательствующий: {meta['judge']}")
    if not parts:
        return ""
    return (
        "ИЗВЕСТНЫЕ ДАННЫЕ ДЕЛА (вписать в шапку как есть, **не помечать [УТОЧНИТЬ]**):\n"
        + "\n".join(parts)
        + "\n\n"
    )


# --- App ---------------------------------------------------------------
app = FastAPI(title="Judge Helper", docs_url="/api/docs")

# Routes that stay public even when AUTH is enabled.
# /login itself is obviously public. /static and PWA assets are public so
# the login page can show icons / install as PWA before authentication.
PUBLIC_PATHS = {
    "/health",
    "/webhook/aai",
    "/login",
    "/logout",
    "/manifest.json",
    "/sw.js",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static/",)

SESSION_COOKIE = "judge_helper_session"
SESSION_DURATION = 60 * 60 * 24 * 30  # 30 days


def _session_secret() -> str:
    """Use WEBHOOK_SECRET as HMAC key for session cookies — already
    auto-generated by Render, no extra env var needed."""
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


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    """Cookie-based session auth. Unauthenticated requests:
    - GET / → 303 redirect to /login
    - other / API requests → 401 JSON, frontend handles by reloading"""
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
        return await call_next(request)
    if not (AUTH_USERNAME and AUTH_PASSWORD):
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE)
    username = verify_session_token(token)
    if username == AUTH_USERNAME:
        return await call_next(request)

    # Not authenticated
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse({"detail": "Не авторизованы"}, status_code=401)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Login / logout pages ---------------------------------------
LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>Вход — Помощник секретаря</title>
    <link rel="apple-touch-icon" href="/apple-touch-icon.png">
    <link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
    <meta name="theme-color" content="#212121">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <style>
        * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
            margin: 0; min-height: 100vh;
            display: flex; align-items: center; justify-content: center;
            padding: 1.5rem;
            background: #212121;
            color: #ececec;
            -webkit-text-size-adjust: 100%;
        }
        .card {
            background: #2f2f2f; border: 1px solid transparent;
            border-radius: 16px; padding: 2rem 1.5rem;
            box-shadow: 0 20px 40px -10px rgba(0,0,0,0.5);
            width: 100%; max-width: 380px;
        }
        .logo {
            width: 72px; height: 72px; margin: 0 auto 1rem;
            background: #ffffff; color: #212121;
            border-radius: 18px;
            display: flex; align-items: center; justify-content: center;
            font-size: 1.75rem; font-weight: 700;
            letter-spacing: -0.05em;
        }
        h1 {
            text-align: center; margin: 0 0 0.25rem;
            font-size: 1.375rem;
            color: #ececec;
        }
        .subtitle {
            text-align: center; color: #b4b4b4;
            margin: 0 0 1.5rem; font-size: 0.95rem;
        }
        .error {
            background: rgba(239, 68, 68, 0.05); border: 1px solid rgba(239, 68, 68, 0.2);
            color: #ef4444;
            border-radius: 8px; padding: 0.625rem 0.875rem;
            font-size: 0.875rem; margin-bottom: 1rem;
        }
        label {
            display: block; margin-bottom: 0.875rem;
            font-size: 0.85rem; color: #b4b4b4;
        }
        input {
            display: block; width: 100%;
            margin-top: 0.375rem;
            padding: 0.75rem 0.875rem;
            border: 1px solid rgba(255, 255, 255, 0.15); border-radius: 8px;
            font-size: 1rem; font-family: inherit;
            min-height: 48px;
            background: rgba(0, 0, 0, 0.2); color: #ececec;
            -webkit-appearance: none;
        }
        input:focus { outline: none; border-color: #ffffff; box-shadow: none; }
        button {
            width: 100%; min-height: 48px;
            background: #ffffff; color: #212121; border: none;
            border-radius: 8px; padding: 0.875rem 1.25rem;
            font-size: 1rem; font-weight: 600; cursor: pointer;
            margin-top: 0.5rem;
            transition: background-color 0.15s, transform 0.05s;
        }
        button:hover { background: #e5e5e5; }
        button:active { transform: scale(0.98); }
        .hint {
            text-align: center; color: #b4b4b4; font-size: 0.8rem;
            margin: 1.25rem 0 0;
        }
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
    block = (
        f'<div class="error">{error}</div>' if error else ""
    )
    html = LOGIN_HTML.replace("__ERROR__", block)
    return HTMLResponse(content=html)


@app.get("/login")
async def login_page(request: Request):
    # Already authenticated? Go to main page.
    token = request.cookies.get(SESSION_COOKIE)
    if AUTH_USERNAME and verify_session_token(token) == AUTH_USERNAME:
        return RedirectResponse(url="/", status_code=303)
    return _login_page()


@app.post("/login")
async def login_submit(
    username: str = Form(...),
    password: str = Form(...),
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


@app.get("/logout")
@app.post("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

# --- Persistence --------------------------------------------------------
# SQLite-backed dict-like store for job state. Replaces the previous in-memory
# dict so soft restarts (redeploys, OOM, code reloads) no longer lose
# in-flight transcripts or completed drafts.
#
# IMPORTANT — Render free tier disk: the filesystem is ephemeral, so on full
# cold start (after the 15-min idle spin-down) the .db file IS wiped. The
# keep-alive workflow at .github/workflows/keep-alive.yml prevents spin-down
# during business hours, so within a working day persistence works. For full
# 24/7 durability, mount a persistent disk (Render Starter+ has $1/mo 1GB)
# and point DB_PATH at it — or use an external SQLite over HTTP service
# (Turso, Cloudflare D1, etc).
DB_PATH = os.environ.get("DB_PATH") or str(BACKEND_DIR / "data" / "jobs.db")
JOB_TTL_DAYS = int(os.environ.get("JOB_TTL_DAYS", "30"))


class JobStore:
    """Thread-safe SQLite key-value store. API-compatible with `dict[str, dict]`
    for the subset of operations used here: `store[k] = v`, `store.get(k, default)`,
    `len(store)`, `k in store`. Each value is JSON-serialised; non-JSON-safe
    payloads will raise on write.

    Locking: a single threading.Lock serialises Python access; SQLite itself
    uses WAL mode so readers don't block. Low-volume app, so per-op connections
    are fine (a few µs overhead per call)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = threading.Lock()
        # Touch the DB once on startup so a brand-new install has the
        # schema ready before the first request lands.
        with self._lock, self._conn():
            pass

    @contextmanager
    def _conn(self):
        # Every connect ensures: dir exists, PRAGMAs set, schema exists.
        # All steps are idempotent (~50µs total), so we get resilience for
        # free against runtime disk wipes (rm -rf, container restart with
        # remount, etc) without separate health-check logic. journal_mode
        # and synchronous are per-connection PRAGMAs — reset to defaults
        # on each connection, so we set them every time.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)"
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, job_id: str, default=None):
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            log.exception(f"Corrupt JSON for job {job_id} — treating as missing")
            return default

    def __getitem__(self, job_id: str):
        v = self.get(job_id)
        if v is None:
            raise KeyError(job_id)
        return v

    def __setitem__(self, job_id: str, data: dict):
        payload = json.dumps(data, ensure_ascii=False, default=str)
        ts = int(time.time())
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs (id, data, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       data = excluded.data,
                       updated_at = excluded.updated_at""",
                (job_id, payload, ts),
            )

    def __contains__(self, job_id: str) -> bool:
        return self.get(job_id) is not None

    def __len__(self) -> int:
        with self._lock, self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def cleanup_old(self, max_age_days: int) -> int:
        cutoff = int(time.time()) - max_age_days * 86400
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE updated_at < ?", (cutoff,))
            return cur.rowcount


jobs = JobStore(DB_PATH)
log.info(f"JobStore initialised at {DB_PATH} ({len(jobs)} existing entries)")


@app.on_event("startup")
async def _startup_cleanup():
    """Prune entries older than JOB_TTL_DAYS on each startup. Keeps the DB
    small and limits how long personal data (defendant names etc.) lingers
    on disk."""
    try:
        n = jobs.cleanup_old(JOB_TTL_DAYS)
        if n:
            log.info(f"Startup: pruned {n} job entries older than {JOB_TTL_DAYS} days")
    except Exception:
        log.exception("Startup cleanup failed (non-fatal)")


# locks stays in-memory: asyncio.Lock is process-local and only needs to
# coordinate concurrent process_transcript() calls within the same worker.
# After restart, locks are gone — worst case is a duplicate LLM call.
locks: dict[str, asyncio.Lock] = {}


def get_lock(job_id: str) -> asyncio.Lock:
    if job_id not in locks:
        locks[job_id] = asyncio.Lock()
    return locks[job_id]


# --- Static frontend ----------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    f = STATIC_DIR / "index.html"
    if not f.exists():
        return JSONResponse({"error": "frontend not found", "expected_at": str(f)}, status_code=500)
    return FileResponse(f)


# PWA-related files served from root (manifest, service worker, iOS icon)
# need to be at root URL so service-worker scope covers the whole site.
@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")


@app.get("/apple-touch-icon-precomposed.png")
async def apple_touch_icon_precomposed():
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC_DIR / "icon-192.png", media_type="image/png")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "model": MODEL,
        "has_assemblyai_key": bool(ASSEMBLYAI_KEY),
        "has_openrouter_key": bool(OPENROUTER_KEY),
        "webhook_configured": bool(BASE_URL and WEBHOOK_SECRET),
        "system_prompt_loaded": bool(SYSTEM_PROMPT),
        "auth_enabled": bool(AUTH_USERNAME and AUTH_PASSWORD),
        "active_jobs": len(jobs),
    }


# --- Upload endpoint ----------------------------------------------------
# Two-stage upload:
#   1. /upload accepts the audio bytes from the browser, buffers in memory,
#      generates a temporary job_id, returns immediately.
#   2. Background task uploads the bytes to AssemblyAI and submits a
#      transcription job. Result is linked to the temp_id via real_job_id.
# Why: when /upload also did the AssemblyAI roundtrip, total request time
# (phone -> backend + backend -> AssemblyAI) could exceed Render/Cloudflare
# request timeout (~100s) for large files (multi-hour court sessions).
# Now the client-facing /upload returns as soon as the file is buffered.

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    case_number: str = Form(""),
    defendant: str = Form(""),
    statute: str = Form(""),
    judge: str = Form(""),
    is_part: str = Form(""),  # "true" if this file is one part of a multi-part session
):
    if not ASSEMBLYAI_KEY:
        raise HTTPException(500, "AssemblyAI key not configured on server")

    audio = await file.read()
    size_mb = len(audio) / 1024 / 1024
    metadata = {
        "case_number": case_number.strip(),
        "defendant":   defendant.strip(),
        "statute":     statute.strip(),
        "judge":       judge.strip(),
    }
    has_meta = any(metadata.values())
    is_part_flag = is_part.lower() in ("true", "1", "yes")
    log.info(
        f"Received {file.filename} ({size_mb:.1f} MB)"
        + (" [PART]" if is_part_flag else "")
        + (f"; metadata: {metadata}" if has_meta else "")
    )

    # Create a temp job_id and respond immediately. AssemblyAI upload runs in
    # background — client polls /status/{temp_id} which redirects to the real
    # AAI transcript_id once available.
    temp_id = "tmp-" + uuid.uuid4().hex[:24]
    jobs[temp_id] = {
        "status": "processing",
        "phase": "uploading_to_aai",
        "metadata": metadata,
        "size_mb": round(size_mb, 1),
        "filename": file.filename or "",
        "is_part": is_part_flag,
        "created_at": int(time.time()),
    }
    asyncio.create_task(_submit_to_assemblyai(temp_id, audio, file.filename or "audio"))
    return {"job_id": temp_id}


async def _submit_to_assemblyai(temp_id: str, audio: bytes, filename: str):
    """Background task: upload bytes to AssemblyAI and create a transcription
    job. Links the temp_id to the real AssemblyAI transcript_id."""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            up_resp = await client.post(
                "https://api.assemblyai.com/v2/upload",
                headers={"authorization": ASSEMBLYAI_KEY},
                content=audio,
            )
            if up_resp.status_code != 200:
                raise RuntimeError(
                    f"AssemblyAI upload {up_resp.status_code}: {up_resp.text[:300]}"
                )
            audio_url = up_resp.json()["upload_url"]
            log.info(f"[{temp_id}] Uploaded to AssemblyAI ({filename})")

            job_meta = jobs.get(temp_id, {}).get("metadata", {})
            dynamic_boost = list(WORD_BOOST)
            if job_meta.get("defendant"):
                defendant_name = job_meta["defendant"]
                dynamic_boost.append(defendant_name)
                # Boost surname alone
                dynamic_boost.append(defendant_name.split()[0])
            if job_meta.get("judge"):
                dynamic_boost.append(job_meta["judge"])

            body = {
                "audio_url": audio_url,
                "language_code": "ru",
                "speaker_labels": True,
                "speakers_expected": 3,
                "speech_models": ["universal-2"],
                "punctuate": True,
                "format_text": True,
                "word_boost": dynamic_boost,
                "boost_param": "high",
            }
            if BASE_URL and WEBHOOK_SECRET:
                body["webhook_url"] = f"{BASE_URL}/webhook/aai"
                body["webhook_auth_header_name"] = "x-webhook-secret"
                body["webhook_auth_header_value"] = WEBHOOK_SECRET

            submit_resp = await client.post(
                "https://api.assemblyai.com/v2/transcript",
                headers={
                    "authorization": ASSEMBLYAI_KEY,
                    "content-type": "application/json",
                },
                json=body,
            )
            if submit_resp.status_code != 200:
                raise RuntimeError(
                    f"AssemblyAI submit {submit_resp.status_code}: {submit_resp.text[:300]}"
                )
            transcript_id = submit_resp.json()["id"]

        # Carry metadata + part flag forward to the real job entry. Webhook
        # handler reads is_part to decide whether to trigger LLM drafting.
        # aai_started_at is the timestamp from which the frontend computes
        # transcription ETA (AssemblyAI processing time, not upload time).
        now = int(time.time())
        existing = jobs.get(temp_id, {})
        metadata = existing.get("metadata", {})
        is_part_flag = existing.get("is_part", False)
        existing["real_job_id"] = transcript_id
        existing["phase"] = "transcribing"
        existing["aai_started_at"] = now
        jobs[temp_id] = existing
        jobs[transcript_id] = {
            "status": "processing",
            "phase": "transcribing",
            "metadata": metadata,
            "is_part": is_part_flag,
            "created_at": existing.get("created_at", now),
            "aai_started_at": now,
        }
        log.info(f"[{temp_id}] AssemblyAI transcript_id={transcript_id}{' [PART]' if is_part_flag else ''}")
    except Exception as e:
        log.exception(f"[{temp_id}] background submit to AssemblyAI failed")
        existing = jobs.get(temp_id, {})
        jobs[temp_id] = {
            "status": "error",
            "error": f"Не удалось отправить файл на расшифровку: {e}",
            "metadata": existing.get("metadata", {}),
            "created_at": existing.get("created_at", int(time.time())),
        }


# --- AssemblyAI webhook -------------------------------------------------
@app.post("/webhook/aai")
async def aai_webhook(payload: dict, x_webhook_secret: Optional[str] = Header(None)):
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        log.warning("Webhook called with bad/missing secret")
        raise HTTPException(401, "bad secret")

    transcript_id = payload.get("transcript_id")
    status_val = payload.get("status")
    log.info(f"Webhook: {transcript_id} -> {status_val}")

    if not transcript_id:
        return {"ok": False, "reason": "no transcript_id"}

    if status_val == "error":
        existing = jobs.get(transcript_id, {})
        existing.update({"status": "error", "error": "AssemblyAI transcription failed"})
        jobs[transcript_id] = existing
        return {"ok": True}

    if status_val != "completed":
        return {"ok": True}

    # Don't await — let AssemblyAI get fast ack
    asyncio.create_task(process_transcript(transcript_id))
    return {"ok": True}


# --- LLM call with model fallback chain ---------------------------------
async def _call_llm_with_fallback(client, user_msg: str, log_prefix: str):
    """Try each model in LLM_FALLBACK_CHAIN until one returns a valid draft.
    Returns (draft, used_model, usage_dict). Raises if all models fail."""
    last_error: Optional[str] = None
    for model in LLM_FALLBACK_CHAIN:
        try:
            llm_resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/judge-helper",
                    "X-Title": "Judge Helper",
                },
                json={
                    "model": model,
                    "max_tokens": 8000,
                    "temperature": 0.3,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                },
            )
            if llm_resp.status_code != 200:
                last_error = f"{model}: HTTP {llm_resp.status_code}: {llm_resp.text[:300]}"
                log.warning(f"[{log_prefix}] {last_error}; trying next model")
                continue

            llm_data = llm_resp.json()

            # OpenRouter can return 200 with an error object instead of choices
            if isinstance(llm_data, dict) and "error" in llm_data:
                err = llm_data["error"]
                err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                last_error = f"{model}: {err_msg}"
                log.warning(f"[{log_prefix}] {last_error}; trying next model")
                continue

            choices = llm_data.get("choices") if isinstance(llm_data, dict) else None
            if not choices:
                last_error = f"{model}: no choices in response — {str(llm_data)[:300]}"
                log.warning(f"[{log_prefix}] {last_error}; trying next model")
                continue

            draft = choices[0].get("message", {}).get("content")
            if not draft:
                last_error = f"{model}: empty content in choices[0]"
                log.warning(f"[{log_prefix}] {last_error}; trying next model")
                continue

            return draft, model, llm_data.get("usage", {})

        except Exception as e:
            last_error = f"{model}: {e}"
            log.warning(f"[{log_prefix}] {last_error}; trying next model")
            continue

    raise RuntimeError(
        f"Все LLM-модели не отвечают. Попробуйте через несколько минут. Последняя ошибка: {last_error}"
    )


# --- The actual transcript -> LLM step ----------------------------------
async def process_transcript(transcript_id: str):
    lock = get_lock(transcript_id)
    async with lock:
        existing = jobs.get(transcript_id, {})
        if existing.get("status") == "done":
            return  # already done by a parallel run
        # preserve metadata + part flag + timestamps across status transitions
        metadata = existing.get("metadata", {})
        is_part = existing.get("is_part", False)
        created_at = existing.get("created_at", int(time.time()))
        aai_started_at = existing.get("aai_started_at", created_at)
        audio_duration_sec = existing.get("audio_duration_sec")
        jobs[transcript_id] = {
            "status": "processing",
            "phase": "transcribing" if is_part else "drafting",
            "metadata": metadata,
            "is_part": is_part,
            "created_at": created_at,
            "aai_started_at": aai_started_at,
            "audio_duration_sec": audio_duration_sec,
            "drafting_started_at": int(time.time()) if not is_part else None,
        }

        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                tx_resp = await client.get(
                    f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
                    headers={"authorization": ASSEMBLYAI_KEY},
                )
                tx_resp.raise_for_status()
                transcript = tx_resp.json()

                # Persist audio_duration as soon as AAI tells us — frontend
                # uses it to compute the transcription ETA.
                audio_duration_sec = transcript.get("audio_duration") or audio_duration_sec

                if transcript.get("status") != "completed":
                    log.warning(f"process_transcript called for non-completed job {transcript_id}")
                    jobs[transcript_id] = {
                        "status": "processing",
                        "phase": "transcribing",
                        "is_part": is_part,
                        "metadata": metadata,
                        "created_at": created_at,
                        "aai_started_at": aai_started_at,
                        "audio_duration_sec": audio_duration_sec,
                    }
                    return

                utterances = transcript.get("utterances") or []
                if utterances:
                    formatted = "\n\n".join(
                        f"[Спикер {u['speaker']}]: {u['text']}" for u in utterances
                    )
                else:
                    formatted = transcript.get("text", "")

                # Pre-LLM deterministic regex cleanup of common AssemblyAI mistakes
                formatted = clean_transcript(formatted)

                duration_min = round((audio_duration_sec or 0) / 60, 1)

                # PART of a multi-part session — store transcript and STOP.
                # The /combine-and-draft endpoint will merge with other parts
                # and run the LLM step once on the combined text.
                if is_part:
                    log.info(
                        f"[{transcript_id}] PART transcribed ({duration_min} min, "
                        f"{len(formatted)} chars). Awaiting combine call."
                    )
                    jobs[transcript_id] = {
                        "status": "transcribed",  # not 'done' — no draft yet
                        "transcript": formatted,
                        "duration_min": duration_min,
                        "metadata": metadata,
                        "is_part": True,
                        "created_at": created_at,
                        "aai_started_at": aai_started_at,
                        "audio_duration_sec": audio_duration_sec,
                    }
                    return

                log.info(
                    f"Transcript {transcript_id}: {duration_min} min, "
                    f"{len(utterances)} utterances, {len(formatted)} chars. Calling LLM..."
                )

                # Drafting starts now — let frontend distinguish phases.
                drafting_started_at = int(time.time())
                jobs[transcript_id] = {
                    "status": "processing",
                    "phase": "drafting",
                    "metadata": metadata,
                    "is_part": False,
                    "created_at": created_at,
                    "aai_started_at": aai_started_at,
                    "audio_duration_sec": audio_duration_sec,
                    "drafting_started_at": drafting_started_at,
                }

                meta_block = format_metadata_block(metadata)
                user_msg = (
                    f"{meta_block}"
                    "Составь черновик протокола судебного заседания на основе "
                    f"следующей размеченной стенограммы аудиозаписи:\n\n{formatted}"
                )
                draft, used_model, usage = await _call_llm_with_fallback(
                    client, user_msg, transcript_id
                )
                log.info(
                    f"[{transcript_id}] Draft via {used_model} ({len(draft)} chars, "
                    f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')})"
                )

            jobs[transcript_id] = {
                "status": "done",
                "draft": draft,
                "transcript": formatted,
                "duration_min": duration_min,
                "model": used_model,
                "metadata": metadata,
                "created_at": created_at,
                "aai_started_at": aai_started_at,
                "audio_duration_sec": audio_duration_sec,
            }
        except Exception as e:
            log.exception(f"Processing failed for {transcript_id}")
            jobs[transcript_id] = {
                "status": "error",
                "error": str(e),
                "metadata": metadata,
                "created_at": created_at,
            }


# --- Status polling -----------------------------------------------------
def render_docx(text: str) -> bytes:
    """
    Render protocol text as a .docx with Russian court document defaults:
    Times New Roman 14pt, A4-ish margins, runs of 5+ spaces converted to
    right-aligned tab stops so the shapka name alignment looks correct.
    """
    doc = Document()

    # Normal style font. python-docx's font.name only sets the 'ascii' slot;
    # for Cyrillic to render in Times New Roman in Word we must also set the
    # complex-script and east-asian font slots via raw XML. Otherwise Word
    # falls back to Calibri for Russian letters.
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(14)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for slot in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(slot), "Times New Roman")

    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(3)
        section.right_margin = Cm(1.5)

    for line in text.split("\n"):
        compact = re.sub(r" {5,}", "\t", line)
        p = doc.add_paragraph()
        run = p.add_run(compact)
        # Explicit per-run font set too — belt and suspenders for Cyrillic
        run.font.name = "Times New Roman"
        run.font.size = Pt(14)
        run_rpr = run._r.get_or_add_rPr()
        run_rfonts = run_rpr.find(qn("w:rFonts"))
        if run_rfonts is None:
            run_rfonts = OxmlElement("w:rFonts")
            run_rpr.insert(0, run_rfonts)
        for slot in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            run_rfonts.set(qn(slot), "Times New Roman")
        if "\t" in compact:
            p.paragraph_format.tab_stops.add_tab_stop(Cm(15), WD_TAB_ALIGNMENT.RIGHT)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


@app.get("/download/{job_id}")
async def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        raise HTTPException(404, "Draft not ready or job not found")

    docx_bytes = render_docx(job["draft"])
    filename = f"protokol_{job_id[:8]}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/render-docx")
async def render_docx_endpoint(payload: dict):
    """Render arbitrary text as .docx. Used by frontend when mom has edited
    the draft in the textarea before downloading — we render her edits, not
    the original LLM output."""
    text = payload.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "text field is required and must be non-empty")
    docx_bytes = render_docx(text)
    filename = payload.get("filename") or "protokol.docx"
    if not filename.endswith(".docx"):
        filename = f"{filename}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/status/{job_id}")
async def status(job_id: str):
    cached = jobs.get(job_id)

    # Session ID (multi-part combined draft) — lives entirely in jobs dict,
    # never hits AssemblyAI directly. Just return what we have.
    if job_id.startswith("ses-"):
        if not cached:
            raise HTTPException(404, "Session not found")
        return cached

    # Temp ID flow: client polls this while we're uploading to AssemblyAI in
    # background. Once we have the real transcript_id, we forward the rest of
    # this handler to that id.
    if job_id.startswith("tmp-") and cached:
        if cached.get("status") == "error":
            return cached
        real_id = cached.get("real_job_id")
        if not real_id:
            # Still uploading to AssemblyAI
            return cached
        # Switch to real id for the rest of the logic
        real_cached = jobs.get(real_id)
        if real_cached and real_cached.get("status") in ("done", "error"):
            return real_cached
        job_id = real_id
        cached = real_cached

    if cached and cached.get("status") in ("done", "error", "transcribed"):
        return cached

    if not ASSEMBLYAI_KEY:
        raise HTTPException(500, "AssemblyAI key not configured")

    async with httpx.AsyncClient(timeout=30.0) as client:
        tx_resp = await client.get(
            f"https://api.assemblyai.com/v2/transcript/{job_id}",
            headers={"authorization": ASSEMBLYAI_KEY},
        )
        if tx_resp.status_code == 404:
            raise HTTPException(404, "Job not found")
        tx_resp.raise_for_status()
        aai_body = tx_resp.json()
        aai_status = aai_body.get("status")
        # AAI returns audio_duration as soon as it has the file (very early).
        # Persist so frontend ETA works even when our own cache was wiped
        # (cold restart) and we're rebuilding state from AAI alone.
        aai_audio_duration = aai_body.get("audio_duration")

    # Refresh cache for the existing entry so timestamps + audio_duration
    # survive even when we hit the AAI fallback. If no cache exists at all
    # (cold restart, job state lost), synthesise a minimal entry so the
    # ETA logic still has timestamps to work with.
    cached_now = jobs.get(job_id) or {}
    if aai_audio_duration and not cached_now.get("audio_duration_sec"):
        cached_now["audio_duration_sec"] = aai_audio_duration
    if not cached_now.get("created_at"):
        cached_now["created_at"] = int(time.time())
    if not cached_now.get("aai_started_at"):
        cached_now["aai_started_at"] = cached_now["created_at"]

    if aai_status == "error":
        cached_now.update({"status": "error", "error": aai_body.get("error", "AssemblyAI error")})
        jobs[job_id] = cached_now
        return cached_now

    if aai_status != "completed":
        cached_now.update({"status": "processing", "phase": "transcribing", "aai_status": aai_status})
        jobs[job_id] = cached_now
        return cached_now

    # Completed but no draft cached — trigger LLM step (handles missed webhook / restart)
    lock = get_lock(job_id)
    cached_now.update({
        "status": "processing",
        "phase": "drafting",
        "drafting_started_at": cached_now.get("drafting_started_at") or int(time.time()),
    })
    jobs[job_id] = cached_now
    if not lock.locked():
        asyncio.create_task(process_transcript(job_id))
    return cached_now


# --- Multi-part session: combine transcripts and draft as one ----------
@app.post("/combine-and-draft")
async def combine_and_draft(payload: dict):
    """Combine transcripts from multiple parts of a single court session
    (recorded in pieces around a break) into one protocol draft.

    Body: {"transcript_ids": ["abc...", "def..."], "metadata": {...}}
    Returns: {"job_id": "ses-..."} — frontend polls /status/{ses-id}.
    """
    transcript_ids = payload.get("transcript_ids") or []
    metadata = payload.get("metadata") or {}
    if not transcript_ids:
        raise HTTPException(400, "transcript_ids must be a non-empty list")
    if len(transcript_ids) < 2:
        raise HTTPException(400, "session needs at least 2 parts to combine")

    # Verify all parts have a stored transcript. Frontend may pass either the
    # real AAI transcript_id OR our internal temp-id (which wraps it); resolve
    # temp-ids to their real_job_id transparently.
    parts: list[dict] = []
    for tid in transcript_ids:
        entry = jobs.get(tid, {})
        # Resolve temp -> real if needed
        if entry.get("real_job_id"):
            real_tid = entry["real_job_id"]
            real_entry = jobs.get(real_tid, {})
            if real_entry:
                tid, entry = real_tid, real_entry
        text = entry.get("transcript")
        if not text:
            raise HTTPException(
                425,  # Too Early
                f"Часть {tid} ещё не расшифрована — подождите и попробуйте снова.",
            )
        parts.append({
            "id": tid,
            "text": text,
            "duration_min": entry.get("duration_min", 0),
        })

    combined = "\n\n[ПЕРЕРЫВ — продолжение заседания]\n\n".join(p["text"] for p in parts)
    total_duration = round(sum(p["duration_min"] or 0 for p in parts), 1)

    session_id = "ses-" + uuid.uuid4().hex[:24]
    now = int(time.time())
    jobs[session_id] = {
        "status": "processing",
        "phase": "drafting",
        "metadata": metadata,
        "is_session": True,
        "part_ids": list(transcript_ids),
        "duration_min": total_duration,
        "created_at": now,
        "drafting_started_at": now,
    }
    asyncio.create_task(_draft_session(session_id, combined, metadata, total_duration))
    log.info(
        f"[{session_id}] Combining {len(parts)} parts "
        f"({total_duration} min total, {len(combined)} chars)"
    )
    return {"job_id": session_id}


async def _draft_session(session_id: str, combined_text: str, metadata: dict, total_duration: float):
    """Background: run LLM on the combined transcript and store draft."""
    existing = jobs.get(session_id, {})
    created_at = existing.get("created_at", int(time.time()))
    drafting_started_at = existing.get("drafting_started_at", created_at)
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            meta_block = format_metadata_block(metadata)
            user_msg = (
                f"{meta_block}"
                "Составь черновик протокола судебного заседания на основе "
                "следующей размеченной стенограммы.\n\n"
                "⚠️ ВАЖНО: заседание состояло из НЕСКОЛЬКИХ ЧАСТЕЙ (запись прерывалась, "
                "например, во время перерыва или окончания первой сессии). Части "
                "объединены в один текст, между ними стоит маркер "
                "`[ПЕРЕРЫВ — продолжение заседания]`.\n\n"
                "Сделай **ОДИН ЦЕЛЬНЫЙ протокол**, в котором все события и реплики "
                "идут последовательно — состав суда, стороны и роли участников **те же** "
                "что и до перерыва, не дублируй блок «явка», шапка одна. После маркера "
                "перерыва можешь написать одну строку «После перерыва судебное заседание "
                "продолжено» (если это уместно по контексту) и продолжить.\n\n"
                f"Стенограмма:\n\n{combined_text}"
            )
            draft, used_model, usage = await _call_llm_with_fallback(
                client, user_msg, session_id
            )
            log.info(
                f"[{session_id}] Session draft via {used_model} "
                f"({len(draft)} chars, in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')})"
            )
        jobs[session_id] = {
            "status": "done",
            "draft": draft,
            "transcript": combined_text,
            "duration_min": total_duration,
            "model": used_model,
            "metadata": metadata,
            "is_session": True,
            "created_at": created_at,
            "drafting_started_at": drafting_started_at,
        }
    except Exception as e:
        log.exception(f"[{session_id}] session draft failed")
        jobs[session_id] = {
            "status": "error",
            "error": str(e),
            "metadata": metadata,
            "is_session": True,
            "created_at": created_at,
        }
