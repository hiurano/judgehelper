"""
Judge Helper backend — audio file -> court protocol draft.

Main FastAPI application module.
Imports modular services for configuration, database, authentication,
transcription (AssemblyAI), LLM drafting (OpenRouter), and DOCX generation.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
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
    BACKEND_DIR,
    BASE_URL,
    DEFAULT_USER,
    JOB_TTL_DAYS,
    MAX_UPLOAD_BYTES,
    MODEL,
    OPENROUTER_KEY,
    STATIC_DIR,
    SYSTEM_PROMPT,
    WEBHOOK_SECRET,
    log,
)
from backend.db import get_lock, jobs, user_store
from backend.services.ai_service import (
    async_retry,
    close_shared_client,
    get_shared_client,
    process_transcript,
    recover_pending_jobs,
    submit_to_assemblyai,
)
from backend.services.docx_generator import render_docx


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Ensure default users exist (create_user is idempotent — skips if exists)
        seeded = []
        if user_store.create_user("elena", "protocol2026", "Елена"):
            seeded.append("elena")
        if user_store.create_user("test", "Test-2026", "Тест"):
            seeded.append("test")
        if AUTH_USERNAME and AUTH_PASSWORD:
            if user_store.create_user(AUTH_USERNAME, AUTH_PASSWORD):
                seeded.append(AUTH_USERNAME)
        if seeded:
            log.info(f"Seeded user accounts: {', '.join(seeded)}")

        n = jobs.cleanup_old(JOB_TTL_DAYS)
        if n:
            log.info(f"Startup: pruned {n} job entries older than {JOB_TTL_DAYS} days")
        await recover_pending_jobs()
    except Exception:
        log.exception("Startup cleanup/recovery failed (non-fatal)")
    yield
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
    allowed_exts = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".webm", ".opus", ".mp4"}
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    if not ext or ext not in allowed_exts:
        raise HTTPException(
            400,
            f"Неподдерживаемый формат файла ({ext or 'нет расширения'}). "
            "Разрешены аудиофайлы: MP3, WAV, M4A, OGG, FLAC, AAC, WMA, WEBM.",
        )

    # Pre-check Content-Length to reject oversized uploads before reading
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "Файл слишком большой. Максимальный допустимый размер: 1 ГБ")

    user_id = getattr(request.state, "user", DEFAULT_USER)
    
    job_id = "job-" + uuid.uuid4().hex[:24]
    upload_dir = BACKEND_DIR / "data" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / f"{job_id}{ext}"

    # Stream the uploaded file directly to disk to avoid RAM OOM
    size_bytes = 0
    with open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1 MB chunks
            f.write(chunk)
            size_bytes += len(chunk)
            if size_bytes > MAX_UPLOAD_BYTES:
                file_path.unlink()
                raise HTTPException(400, "Файл слишком большой. Максимальный допустимый размер: 1 ГБ")

    if size_bytes == 0:
        file_path.unlink()
        raise HTTPException(400, "Загруженный файл пуст")

    size_mb = size_bytes / 1024 / 1024

    if not ASSEMBLYAI_KEY:
        file_path.unlink()
        raise HTTPException(500, "AssemblyAI key not configured on server")
        
    metadata = {
        "defendant": defendant.strip(),
    }
    log.info(f"Received {filename} ({size_mb:.1f} MB) for user {user_id}; defendant: {defendant}")

    jobs[job_id] = {
        "status": "processing",
        "phase": "uploading_to_aai",
        "metadata": metadata,
        "size_mb": round(size_mb, 1),
        "filename": filename,
        "created_at": int(time.time()),
        "user_id": user_id,
    }

    asyncio.create_task(submit_to_assemblyai(job_id, file_path, filename or "audio"))
    return {"job_id": job_id}


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

    job_id, existing = jobs.get_by_aai_id(transcript_id)
    if not job_id or not existing:
        log.warning(f"Webhook received for unknown transcript_id: {transcript_id}")
        return {"ok": False, "reason": "job not found"}

    if status_val == "error":
        existing.update({"status": "error", "error": "AssemblyAI transcription failed"})
        jobs[job_id] = existing
        return {"ok": True}

    if status_val != "completed":
        return {"ok": True}

    asyncio.create_task(process_transcript(job_id))
    return {"ok": True}


@app.get("/status/{job_id}")
async def status(job_id: str):
    found_id, cached = jobs.get_by_aai_id(job_id)
    if found_id:
        job_id = found_id

    if not cached:
        raise HTTPException(404, "Job not found")

    if cached.get("status") in ("done", "error"):
        return cached

    aai_transcript_id = cached.get("aai_transcript_id")
    if not aai_transcript_id:
        return cached

    client = get_shared_client()

    async def _fetch_status():
        tx_resp = await client.get(
            f"https://api.assemblyai.com/v2/transcript/{aai_transcript_id}",
            headers={"authorization": ASSEMBLYAI_KEY},
        )
        if tx_resp.status_code == 404:
            raise HTTPException(404, "Job not found on AssemblyAI")
        tx_resp.raise_for_status()
        return tx_resp.json()

    aai_body = await async_retry(_fetch_status, retries=2, delay=0.5)
    aai_status = aai_body.get("status")
    aai_audio_duration = aai_body.get("audio_duration")

    if aai_audio_duration and not cached.get("audio_duration_sec"):
        cached["audio_duration_sec"] = aai_audio_duration

    if aai_status == "error":
        cached.update({"status": "error", "error": aai_body.get("error", "AssemblyAI error")})
        jobs[job_id] = cached
        return cached

    if aai_status != "completed":
        cached.update({"status": "processing", "phase": "transcribing", "aai_status": aai_status})
        jobs[job_id] = cached
        return cached

    lock = get_lock(job_id)
    cached.update({
        "status": "processing",
        "phase": "drafting",
        "drafting_started_at": cached.get("drafting_started_at") or int(time.time()),
    })
    jobs[job_id] = cached
    if not lock.locked():
        asyncio.create_task(process_transcript(job_id))
    return cached


@app.get("/jobs")
async def list_jobs(request: Request):
    user_id = getattr(request.state, "user", DEFAULT_USER)
    recent = jobs.list_recent(user_id=user_id, limit=30)
    cleaned = []
    for item in recent:
        job_id = item.get("id")
        if not job_id:
            continue
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


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    user_id = getattr(request.state, "user", DEFAULT_USER)
    job_data = jobs.get(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found")
    # Verify owner if user is not None
    job_owner = job_data.get("user_id")
    if job_owner and user_id and job_owner != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this protocol")
        
    success = jobs.delete(job_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete job")
    return {"ok": True}


@app.get("/api/me")
async def get_me(request: Request):
    user_id = getattr(request.state, "user", DEFAULT_USER)
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
    docx_bytes = await asyncio.to_thread(render_docx, text)
    filename = payload.get("filename") or "protokol.docx"
    if not filename.endswith(".docx"):
        filename = f"{filename}.docx"
    quoted_filename = urllib.parse.quote(filename)
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quoted_filename}"},
    )
