"""Owner-only review, revision-safe editing, retry and structured Word export."""
import asyncio
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from backend.config import MAX_ACTIVE_JOBS_PER_USER, MAX_RENDER_TEXT_CHARS
from backend.db import get_lock, jobs, remove_lock
from backend.services.docx_generator import render_review_docx
from backend.services.pipeline import process_transcript
from backend.services.protocol import (
    ROLES, ReviewUpdate, apply_review, corrected_utterances, document_blocks,
    document_text, proposals, role_registry, sensitive_change,
)
from backend.services.task_manager import spawn


router = APIRouter()
_render_slots = asyncio.Semaphore(2)


def owned_review(job_id, request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Не авторизованы")
    job = jobs.get(job_id)
    if not job or job.get("user_id") != user:
        raise HTTPException(404, "Задача не найдена")
    record = jobs.get_review(job_id)
    if record is None:
        raise HTTPException(404, "Исходная расшифровка для проверки ещё не сохранена")
    return record


def view(record):
    source, state = record["source"], record["state"]
    return {"source": source, "revision": record["revision"], "role_options": ROLES,
            **{key: state[key] for key in ("stage", "error", "decisions", "speakers", "utterances",
                                          "fields", "manual_text", "resolved_concerns", "reviewed")},
            "corrections": [{**c, "sensitive": sensitive_change(c)} for c in proposals(state, "corrections")],
            "concerns": proposals(state, "concerns"),
            "registry": role_registry(source, state), "rows": corrected_utterances(source, state),
            "blocks": document_blocks(source, state)}


@router.get("/jobs/{job_id}/review")
def read_review(job_id: str, request: Request):
    return view(owned_review(job_id, request))


@router.put("/jobs/{job_id}/review")
def write_review(job_id: str, payload: ReviewUpdate, request: Request):
    record = owned_review(job_id, request)
    if record["revision"] != payload.revision:
        raise HTTPException(409, "Документ изменён в другой вкладке. Обновите его перед сохранением.")
    try:
        state = apply_review(record["source"], record["state"], payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    draft = document_text(document_blocks(record["source"], state))
    if len(draft) > MAX_RENDER_TEXT_CHARS:
        raise HTTPException(413, "Документ превышает допустимый размер")
    state["reviewed_by"] = request.state.user
    state["reviewed_at"] = int(time.time())
    updates = {"draft": draft, "review_status": "reviewed" if state["reviewed"] else state["stage"]}
    if not jobs.save_review(job_id, state, payload.revision, updates):
        raise HTTPException(409, "Документ изменён или удалён. Обновите страницу.")
    return view({**record, "state": state, "revision": payload.revision + 1})


@router.post("/jobs/{job_id}/review/retry")
async def retry_review(job_id: str, request: Request):
    record = owned_review(job_id, request)
    lock = get_lock(job_id)
    if lock.locked() or record["state"]["stage"] != "error":
        raise HTTPException(409, "Повтор доступен только после ошибки анализа")
    try:
        async with lock:
            state = record["state"]
            state.update(stage="processing", error=None, reviewed=False)
            try:
                saved = jobs.save_review(job_id, state, record["revision"], {
                    "status": "processing", "phase": "drafting", "error": None,
                    "review_status": "processing", "review_attempt_started_at": int(time.time()),
                }, max_active=MAX_ACTIVE_JOBS_PER_USER)
            except ValueError as exc:
                raise HTTPException(429, str(exc)) from None
            if not saved:
                raise HTTPException(409, "Документ изменён или удалён")
        spawn(process_transcript(job_id), name=f"process:{job_id}")
    finally:
        remove_lock(job_id)
    return {"ok": True}


@router.get("/jobs/{job_id}/review/docx")
async def export_review(job_id: str, request: Request):
    record = owned_review(job_id, request)
    blocks = document_blocks(record["source"], record["state"])
    if len(document_text(blocks)) > MAX_RENDER_TEXT_CHARS:
        raise HTTPException(413, "Документ превышает допустимый размер")
    async with _render_slots:
        data = await asyncio.to_thread(render_review_docx, blocks)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": 'attachment; filename="protocol.docx"',
                             "Cache-Control": "no-store"})
