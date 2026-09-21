"""
Judge Helper backend — audio file -> court protocol draft.

Main FastAPI application module.
Imports modular services for configuration, database, authentication,
transcription (AssemblyAI), LLM drafting (OpenRouter), and DOCX generation.
"""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import secrets
import time
import urllib.parse
import uuid
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.auth import (
    login_page_handler,
    login_submit_handler,
    logout_handler,
    SessionAuthMiddleware,
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
    MAX_ACTIVE_JOBS_PER_USER,
    MAX_RENDER_TEXT_CHARS,
    MAX_UPLOAD_BYTES,
    OPENROUTER_KEY,
    SECRET_KEY,
    STATIC_DIR,
    WEBHOOK_SECRET,
    get_system_prompt,
    log,
)
from backend.db import get_lock, jobs, user_store
from backend.services.docx_generator import render_docx
from backend.services.http_client import close_shared_client
from backend.services.pipeline import process_transcript, recover_pending_jobs
from backend.services.transcription import aai_polling_loop, submit_to_assemblyai
from backend.services.task_manager import cancel_all, spawn


def _looks_like_supported_media(header: bytes) -> bool:
    """Conservative signature check for the media containers accepted by the UI."""
    return any((
        header.startswith(b"ID3"),                         # MP3 with ID3
        len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0,
        header.startswith(b"RIFF"),                        # WAV
        header.startswith(b"fLaC"),                        # FLAC
        header.startswith(b"OggS"),                        # OGG/Opus
        header.startswith(bytes.fromhex("1a45dfa3")),       # WebM/Matroska
        header.startswith(bytes.fromhex("3026b2758e66cf11")),  # WMA/ASF
        len(header) >= 8 and header[4:8] == b"ftyp",       # M4A/MP4
    ))


UPLOAD_DIR = BACKEND_DIR / "data" / "uploads"


def _too_large_message() -> str:
    """Quote the limit that is actually configured, not a hardcoded 1 GB."""
    return (
        "Файл слишком большой. Максимальный допустимый размер: "
        f"{MAX_UPLOAD_BYTES // (1024 * 1024)} МБ"
    )


def prune_orphan_uploads() -> int:
    """Delete audio left on disk by a process that died mid-upload.

    A file is in use for exactly as long as a job row claims it, so anything
    whose job is no longer active was abandoned: the task that would have
    deleted it never reached its `finally`. Left alone these are gigabyte-sized
    files sharing a volume with the database."""
    if not UPLOAD_DIR.exists():
        return 0
    active = {job.get("id") for job in jobs.get_pending_jobs()}
    removed = 0
    for path in UPLOAD_DIR.iterdir():
        if not path.is_file() or path.stem in active:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            log.warning("Could not remove orphaned upload %s", path.name)
    return removed


async def _maintenance_loop():
    """Periodically enforce retention even when the service is never restarted."""
    while True:
        await asyncio.sleep(24 * 60 * 60)
        deleted = await asyncio.to_thread(jobs.cleanup_old, JOB_TTL_DAYS)
        if deleted:
            log.info("Maintenance: pruned %s expired job entries", deleted)
        orphans = await asyncio.to_thread(prune_orphan_uploads)
        if orphans:
            log.info("Maintenance: removed %s orphaned upload file(s)", orphans)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Account setup is security-critical: configuration errors must abort startup.
    if len(SECRET_KEY) < 32:
        raise RuntimeError("SECRET_KEY must contain at least 32 characters")
    if AUTH_PASSWORD and (len(AUTH_PASSWORD) < 12 or AUTH_PASSWORD == AUTH_USERNAME):
        raise RuntimeError("AUTH_PASSWORD must be at least 12 characters and differ from AUTH_USERNAME")

    seeded = []
    if AUTH_USERNAME and AUTH_PASSWORD:
        if user_store.create_user(AUTH_USERNAME, AUTH_PASSWORD, AUTH_USERNAME.capitalize()):
            seeded.append(AUTH_USERNAME)
    if seeded:
        log.info(f"Seeded user accounts: {', '.join(seeded)}")
    if user_store.exists("admin") and user_store.verify("admin", "admin"):
        if AUTH_USERNAME == "admin" and AUTH_PASSWORD and AUTH_PASSWORD != "admin":
            user_store.change_password("admin", AUTH_PASSWORD)
            log.warning("Replaced legacy admin/admin credentials with AUTH_PASSWORD")
        else:
            raise RuntimeError(
                "Insecure legacy admin/admin account detected. Set AUTH_USERNAME=admin "
                "and a strong AUTH_PASSWORD before starting."
            )
    if user_store.is_empty():
        raise RuntimeError(
            "No user accounts exist. Set AUTH_USERNAME and AUTH_PASSWORD "
            "or create an account with backend.cli before starting the service."
        )

    try:
        n = jobs.cleanup_old(JOB_TTL_DAYS)
        if n:
            log.info(f"Startup: pruned {n} job entries older than {JOB_TTL_DAYS} days")

        # Start background tasks
        spawn(aai_polling_loop(), name="aai-polling-loop")
        spawn(_maintenance_loop(), name="maintenance-loop")

        # After recovery, so that jobs it has just given up on release their
        # audio too rather than waiting a day for the maintenance pass.
        await recover_pending_jobs()
        orphans = prune_orphan_uploads()
        if orphans:
            log.info(f"Startup: removed {orphans} orphaned upload file(s)")
    except Exception:
        log.exception("Startup cleanup/recovery failed (non-fatal)")
    yield
    await cancel_all()
    await close_shared_client()


app = FastAPI(title="Judge Helper", docs_url="/api/docs", lifespan=lifespan)


# --- Middleware ---------------------------------------------------------
class NoCacheMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        no_cache = scope["type"] == "http" and (
            scope.get("path", "").startswith("/static/") or scope.get("path") == "/"
        )

        async def send_with_headers(message):
            if no_cache and message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"cache-control", b"no-cache, no-store, must-revalidate"),
                    (b"pragma", b"no-cache"),
                    (b"expires", b"0"),
                ])
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(NoCacheMiddleware)
app.add_middleware(SessionAuthMiddleware)

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
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    return await login_submit_handler(username, password, request=request)


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
    return {"ok": True}


@app.get("/ready")
async def ready():
    ready_now = bool(
        ASSEMBLYAI_KEY
        and OPENROUTER_KEY
        and SECRET_KEY
        and get_system_prompt()
        and not user_store.is_empty()
    )
    return JSONResponse({"ready": ready_now}, status_code=200 if ready_now else 503)


# --- API Endpoints ------------------------------------------------------
@app.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    defendant: str = Form(""),
):
    allowed_exts = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".webm", ".opus", ".mp4"}
    filename = file.filename or ""
    if len(filename) > 255:
        raise HTTPException(400, "Имя файла слишком длинное")
    if len(defendant) > 300:
        raise HTTPException(400, "Поле с данными подсудимого слишком длинное")
    ext = Path(filename).suffix.lower()
    if not ext or ext not in allowed_exts:
        raise HTTPException(
            400,
            f"Неподдерживаемый формат файла ({ext or 'нет расширения'}). "
            "Разрешены аудиофайлы: MP3, WAV, M4A, OGG, FLAC, AAC, WMA, WEBM.",
        )

    # Pre-check Content-Length to reject oversized uploads before reading
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_UPLOAD_BYTES + 1024 * 1024:
                raise HTTPException(413, _too_large_message())
        except ValueError:
            raise HTTPException(400, "Некорректный Content-Length")

    if not ASSEMBLYAI_KEY:
        raise HTTPException(500, "AssemblyAI key not configured on server")

    user_id = getattr(request.state, "user", DEFAULT_USER)
    job_id = "job-" + uuid.uuid4().hex[:24]
    metadata = {"defendant": defendant.strip()}
    initial_job = {
        "status": "processing",
        "phase": "receiving_upload",
        "metadata": metadata,
        "filename": filename,
        "created_at": int(time.time()),
        "user_id": user_id,
    }
    if not jobs.create_if_under_active_limit(
        job_id, initial_job, MAX_ACTIVE_JOBS_PER_USER
    ):
        raise HTTPException(
            429,
            f"Достигнут лимит активных задач ({MAX_ACTIVE_JOBS_PER_USER}). Дождитесь завершения обработки.",
        )

    file_path = UPLOAD_DIR / f"{job_id}{ext}"

    # Stream the uploaded file directly to disk to avoid RAM OOM
    size_bytes = 0
    header = b""
    try:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        with open(file_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):  # 1 MB chunks
                if not header:
                    # Judge the format from the first chunk: a file that is not
                    # audio should be refused now, not after it has all landed.
                    header = chunk[:16]
                    if not _looks_like_supported_media(header):
                        raise HTTPException(
                            400,
                            "Содержимое файла не соответствует поддерживаемому аудио/видео формату",
                        )
                await asyncio.to_thread(f.write, chunk)
                size_bytes += len(chunk)
                if size_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, _too_large_message())

        if size_bytes == 0:
            raise HTTPException(400, "Загруженный файл пуст")
    except BaseException:
        file_path.unlink(missing_ok=True)
        jobs.delete(job_id)
        raise

    size_mb = size_bytes / 1024 / 1024
        
    log.info("Received media file (%s MB) for user %s", f"{size_mb:.1f}", user_id)

    initial_job.update({
        "phase": "uploading_to_aai",
        "size_mb": round(size_mb, 1),
    })
    if not jobs.update_if_exists(job_id, initial_job):
        # Cancelled from another tab while the bytes were still arriving.
        file_path.unlink(missing_ok=True)
        raise HTTPException(404, "Задача была удалена во время загрузки")

    spawn(
        submit_to_assemblyai(job_id, file_path, filename or "audio"),
        name=f"aai-submit:{job_id}",
    )
    return {"job_id": job_id}


@app.post("/webhook/aai")
async def aai_webhook(payload: dict, x_webhook_secret: Optional[str] = Header(None)):
    if not WEBHOOK_SECRET:
        raise HTTPException(503, "webhook is not configured")
    if not secrets.compare_digest(x_webhook_secret or "", WEBHOOK_SECRET):
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
        jobs.update_if_exists(job_id, existing)
        return {"ok": True}

    if status_val != "completed":
        return {"ok": True}

    spawn(process_transcript(job_id), name=f"process:{job_id}")
    return {"ok": True}


@app.get("/status/{job_id}")
async def status(job_id: str, request: Request):
    found_id, cached = jobs.get_by_aai_id(job_id)
    if found_id:
        job_id = found_id

    if not cached:
        raise HTTPException(404, "Job not found")

    user_id = getattr(request.state, "user", DEFAULT_USER)
    if cached.get("user_id") != user_id:
        # Do not disclose whether another user's job exists.
        raise HTTPException(404, "Job not found")

    response_data = dict(cached)
    # Legacy rows may still contain raw transcripts; they are never returned.
    response_data.pop("transcript", None)
    return response_data


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
            "phase_detail": item.get("phase_detail"),
            "filename": item.get("filename"),
            "duration_min": item.get("duration_min"),
            "speakers_count": item.get("speakers_count"),
            "utterances_count": item.get("utterances_count"),
            "total_chunks": item.get("total_chunks"),
            "current_chunk": item.get("current_chunk"),
            "metadata": item.get("metadata", {}),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            # The draft itself is fetched from /status/{job_id} on demand: it is
            # the whole protocol, and this list is reloaded on every tab focus.
            "has_draft": bool(item.get("draft")),
            "truncated": bool(item.get("truncated")),
            "error": item.get("error") if item.get("status") == "error" else None,
        })
    return {"jobs": cleaned}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    user_id = getattr(request.state, "user", DEFAULT_USER)
    job_data = jobs.get(job_id)
    if not job_data:
        raise HTTPException(status_code=404, detail="Job not found")
    if job_data.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Job not found")
        
    success = jobs.delete(job_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete job")
    return {"ok": True}


@app.get("/api/me")
async def get_me(request: Request):
    user_id = getattr(request.state, "user", DEFAULT_USER)
    stats = jobs.get_user_stats(user_id)
    display_name = user_store.get_display_name(user_id) or user_id.capitalize()
    return {
        "username": user_id,
        "display_name": display_name,
        "plan": "Персональный",
        "total_protocols": stats["total_protocols"],
        "total_duration_min": stats["total_duration_min"],
        "max_active_jobs": MAX_ACTIVE_JOBS_PER_USER,
    }


@app.post("/render-docx")
async def render_docx_endpoint(payload: dict):
    text = payload.get("text", "")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(400, "text field is required and must be non-empty")
    if len(text) > MAX_RENDER_TEXT_CHARS:
        raise HTTPException(413, "text field is too large")
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
