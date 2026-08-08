"""
AI service module for AssemblyAI transcription and OpenRouter LLM drafting.
"""
import time
from typing import Optional

import httpx

from backend.config import (
    ASSEMBLYAI_KEY,
    BASE_URL,
    LLM_FALLBACK_CHAIN,
    OPENROUTER_KEY,
    SYSTEM_PROMPT,
    WEBHOOK_SECRET,
    WORD_BOOST,
    log,
)
from backend.db import get_lock, jobs
from backend.services.text_cleaner import clean_transcript, format_metadata_block

_shared_client: Optional[httpx.AsyncClient] = None


def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=600.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _shared_client


async def close_shared_client():
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None


async def submit_to_assemblyai(temp_id: str, audio: bytes, filename: str):
    """Background task: upload bytes to AssemblyAI and create a transcription job."""
    try:
        client = get_shared_client()
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
            dynamic_boost.append(defendant_name.split()[0])

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

        now = int(time.time())
        existing = jobs.get(temp_id, {})
        metadata = existing.get("metadata", {})
        user_id = existing.get("user_id", "elena")
        filename = existing.get("filename", "")

        existing["real_job_id"] = transcript_id
        existing["phase"] = "transcribing"
        existing["aai_started_at"] = now
        jobs[temp_id] = existing

        jobs[transcript_id] = {
            "status": "processing",
            "phase": "transcribing",
            "metadata": metadata,
            "filename": filename,
            "user_id": user_id,
            "created_at": existing.get("created_at", now),
            "aai_started_at": now,
        }
        log.info(f"[{temp_id}] AssemblyAI transcript_id={transcript_id} (user: {user_id})")
    except Exception as e:
        log.exception(f"[{temp_id}] background submit to AssemblyAI failed")
        existing = jobs.get(temp_id, {})
        jobs[temp_id] = {
            "status": "error",
            "error": f"Не удалось отправить файл на расшифровку: {e}",
            "metadata": existing.get("metadata", {}),
            "user_id": existing.get("user_id", "elena"),
            "filename": existing.get("filename", ""),
            "created_at": existing.get("created_at", int(time.time())),
        }


async def call_llm_with_fallback(client: httpx.AsyncClient, user_msg: str, log_prefix: str):
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
                    "max_tokens": 16000,
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


def _sync_temp_jobs(transcript_id: str, final_status: str):
    """Mark any temp_id job referencing transcript_id as complete/error so it stops lingering in active queue."""
    try:
        for job_id in list(jobs._conn().execute("SELECT id FROM jobs WHERE id LIKE 'tmp-%'").fetchall()):
            tid = job_id[0]
            item = jobs.get(tid)
            if item and item.get("real_job_id") == transcript_id:
                item["status"] = final_status
                item["phase"] = None
                jobs[tid] = item
    except Exception:
        pass


async def process_transcript(transcript_id: str):
    """Process transcript from AssemblyAI, format text, and run LLM drafting."""
    lock = get_lock(transcript_id)
    async with lock:
        existing = jobs.get(transcript_id, {})
        if existing.get("status") == "done":
            return
        metadata = existing.get("metadata", {})
        user_id = existing.get("user_id", "elena")
        filename = existing.get("filename", "")
        created_at = existing.get("created_at", int(time.time()))
        aai_started_at = existing.get("aai_started_at", created_at)
        audio_duration_sec = existing.get("audio_duration_sec")
        jobs[transcript_id] = {
            "status": "processing",
            "phase": existing.get("phase", "processing"),
            "metadata": metadata,
            "filename": filename,
            "user_id": user_id,
            "created_at": created_at,
            "aai_started_at": aai_started_at,
            "audio_duration_sec": audio_duration_sec,
        }

        try:
            client = get_shared_client()
            tx_resp = await client.get(
                f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
                headers={"authorization": ASSEMBLYAI_KEY},
            )
            tx_resp.raise_for_status()
            transcript = tx_resp.json()

            audio_duration_sec = transcript.get("audio_duration") or audio_duration_sec

            if transcript.get("status") != "completed":
                log.warning(f"process_transcript called for non-completed job {transcript_id}")
                jobs[transcript_id] = {
                    "status": "processing",
                    "phase": "transcribing",
                    "metadata": metadata,
                    "filename": filename,
                    "user_id": user_id,
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

            formatted = clean_transcript(formatted)
            duration_min = round((audio_duration_sec or 0) / 60, 1)

            log.info(
                f"Transcript {transcript_id}: {duration_min} min, "
                f"{len(utterances)} utterances, {len(formatted)} chars. Calling LLM..."
            )

            drafting_started_at = int(time.time())
            jobs[transcript_id] = {
                "status": "processing",
                "phase": "drafting",
                "metadata": metadata,
                "filename": filename,
                "user_id": user_id,
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
            draft, used_model, usage = await call_llm_with_fallback(
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
                "filename": filename,
                "user_id": user_id,
                "created_at": created_at,
                "aai_started_at": aai_started_at,
                "audio_duration_sec": audio_duration_sec,
            }
            _sync_temp_jobs(transcript_id, "done")
        except Exception as e:
            log.exception(f"Processing failed for {transcript_id}")
            jobs[transcript_id] = {
                "status": "error",
                "error": str(e),
                "metadata": metadata,
                "filename": filename,
                "user_id": user_id,
                "created_at": created_at,
            }
            _sync_temp_jobs(transcript_id, "error")
