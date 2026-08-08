"""
Judge Helper backend — audio file -> court protocol draft.

Main FastAPI application module.
Imports modular services for configuration, database, authentication,
transcription (AssemblyAI), LLM drafting (OpenRouter), and DOCX generation.
"""
import asyncio
from contextlib import asynccontextmanager
import time
import urllib.parse
import uuid
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.auth import (
    login_page_handler,
    login_submit_handler,
    logout_handler,
    session_auth_middleware,
)
from backend.config import (
    ALLOWED_ORIGINS,
    ASSEMBLYAI_KEY,
    AUTH_PASSWORD,
    AUTH_USERNAME,
    BASE_URL,
    JOB_TTL_DAYS,
    MODEL,
    OPENROUTER_KEY,
    STATIC_DIR,
    SYSTEM_PROMPT,
    WEBHOOK_SECRET,
    log,
)
from backend.db import get_lock, jobs
from backend.services.ai_service import (
    close_shared_client,
    process_transcript,
    submit_to_assemblyai,
)
from backend.services.docx_generator import render_docx

http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0)
    try:
        n = jobs.cleanup_old(JOB_TTL_DAYS)
        if n:
            log.info(f"Startup: pruned {n} job entries older than {JOB_TTL_DAYS} days")
    except Exception:
        log.exception("Startup cleanup failed (non-fatal)")
    yield
    await http_client.aclose()
    await close_shared_client()


app = FastAPI(title="Judge Helper", docs_url="/api/docs", lifespan=lifespan)


# --- Middleware ---------------------------------------------------------
@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.middleware("http")(session_auth_middleware)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Login / Logout Routes ----------------------------------------------
@app.get("/login")
async def login_page(request: Request):
    return await login_page_handler(request)


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    return await login_submit_handler(username, password)


@app.get("/logout")
@app.post("/logout")
async def logout():
    return await logout_handler()


# --- Static frontend & PWA assets ---------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    f = STATIC_DIR / "index.html"
    if not f.exists():
        return JSONResponse({"error": "frontend not found", "expected_at": str(f)}, status_code=500)
    return FileResponse(f)


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")


@app.get("/apple-touch-icon-precomposed.png")
async def apple_touch_icon_precomposed():
    return FileResponse(STATIC_DIR / "apple-touch-icon.png", media_type="image/png")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


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


# --- API Endpoints ------------------------------------------------------
@app.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    defendant: str = Form(""),
):
    if not ASSEMBLYAI_KEY:
        raise HTTPException(500, "AssemblyAI key not configured on server")

    user_id = getattr(request.state, "user", "elena")
    audio = await file.read()
    size_mb = len(audio) / 1024 / 1024
    metadata = {
        "defendant": defendant.strip(),
    }
    log.info(f"Received {file.filename} ({size_mb:.1f} MB) for user {user_id}; defendant: {defendant}")

    temp_id = "tmp-" + uuid.uuid4().hex[:24]
    jobs[temp_id] = {
        "status": "processing",
        "phase": "uploading_to_aai",
        "metadata": metadata,
        "size_mb": round(size_mb, 1),
        "filename": file.filename or "",
        "created_at": int(time.time()),
        "user_id": user_id,
    }

    asyncio.create_task(submit_to_assemblyai(temp_id, audio, file.filename or "audio"))
    return {"job_id": temp_id}


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

    asyncio.create_task(process_transcript(transcript_id))
    return {"ok": True}


@app.get("/status/{job_id}")
async def status(job_id: str):
    cached = jobs.get(job_id)

    if job_id.startswith("tmp-") and cached:
        if cached.get("status") == "error":
            return cached
        real_id = cached.get("real_job_id")
        if not real_id:
            return cached
        real_cached = jobs.get(real_id)
        if real_cached and real_cached.get("status") in ("done", "error"):
            return real_cached
        job_id = real_id
        cached = real_cached

    if cached and cached.get("status") in ("done", "error"):
        return cached

    client = http_client or httpx.AsyncClient(timeout=30.0)
    tx_resp = await client.get(
        f"https://api.assemblyai.com/v2/transcript/{job_id}",
        headers={"authorization": ASSEMBLYAI_KEY},
    )
    if tx_resp.status_code == 404:
        raise HTTPException(404, "Job not found")
    tx_resp.raise_for_status()
    aai_body = tx_resp.json()
    aai_status = aai_body.get("status")
    aai_audio_duration = aai_body.get("audio_duration")

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


@app.get("/jobs")
async def list_jobs(request: Request):
    user_id = getattr(request.state, "user", "elena")
    recent = jobs.list_recent(user_id=user_id, limit=30)
    seen_ids = set()
    cleaned = []
    for item in recent:
        job_id = item.get("id")
        if not job_id or job_id in seen_ids:
            continue
        real_id = item.get("real_job_id")
        if real_id and any(r.get("id") == real_id for r in recent):
            continue
        seen_ids.add(job_id)
        cleaned.append({
            "id": job_id,
            "status": item.get("status"),
            "phase": item.get("phase"),
            "filename": item.get("filename"),
            "duration_min": item.get("duration_min"),
            "metadata": item.get("metadata", {}),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "draft": item.get("draft") if item.get("status") == "done" else None,
            "error": item.get("error") if item.get("status") == "error" else None,
        })
    return {"jobs": cleaned}


@app.get("/api/me")
async def get_me(request: Request):
    user_id = getattr(request.state, "user", "elena")
    stats = jobs.get_user_stats(user_id)
    display_name = user_id.capitalize()
    return {
        "username": user_id,
        "display_name": display_name,
        "plan": "Персональный",
        "total_protocols": stats["total_protocols"],
        "total_duration_min": stats["total_duration_min"],
    }


@app.post("/render-docx")
async def render_docx_endpoint(payload: dict):
    text = payload.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "text field is required and must be non-empty")
    docx_bytes = render_docx(text)
    filename = payload.get("filename") or "protokol.docx"
    if not filename.endswith(".docx"):
        filename = f"{filename}.docx"
    quoted_filename = urllib.parse.quote(filename)
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quoted_filename}"},
    )
