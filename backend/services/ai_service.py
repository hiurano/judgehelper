"""
AI service module for AssemblyAI transcription and OpenRouter LLM drafting.
"""
import asyncio
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
from backend.db import get_lock, jobs, remove_lock
from backend.services.text_cleaner import clean_transcript, format_metadata_block

from fastapi import HTTPException

_shared_client: Optional[httpx.AsyncClient] = None


async def async_retry(coro_fn, retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Retry an async operation on transient network failures or HTTP errors."""
    last_exc = None
    curr_delay = delay
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except HTTPException:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == retries:
                break
            log.warning(f"Network call failed (attempt {attempt}/{retries}): {exc}. Retrying in {curr_delay:.1f}s...")
            await asyncio.sleep(curr_delay)
            curr_delay *= backoff
    raise last_exc


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


async def submit_to_assemblyai(job_id: str, audio: bytes, filename: str):
    """Background task: upload bytes to AssemblyAI and update transcription job."""
    try:
        client = get_shared_client()

        async def _do_upload():
            up_resp = await client.post(
                "https://api.assemblyai.com/v2/upload",
                headers={"authorization": ASSEMBLYAI_KEY},
                content=audio,
            )
            if up_resp.status_code != 200:
                raise RuntimeError(
                    f"AssemblyAI upload {up_resp.status_code}: {up_resp.text[:300]}"
                )
            return up_resp.json()["upload_url"]

        audio_url = await async_retry(_do_upload, retries=3, delay=1.0)
        log.info(f"[{job_id}] Uploaded to AssemblyAI ({filename})")

        job_meta = jobs.get(job_id, {}).get("metadata", {})
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

        async def _do_submit():
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
            return submit_resp.json()["id"]

        aai_transcript_id = await async_retry(_do_submit, retries=3, delay=1.0)

        now = int(time.time())
        existing = jobs.get(job_id, {})
        existing.update({
            "status": "processing",
            "phase": "transcribing",
            "aai_transcript_id": aai_transcript_id,
            "aai_started_at": now,
        })
        jobs[job_id] = existing
        log.info(f"[{job_id}] AssemblyAI aai_transcript_id={aai_transcript_id}")
    except Exception as e:
        log.exception(f"[{job_id}] background submit to AssemblyAI failed")
        existing = jobs.get(job_id, {})
        existing.update({
            "status": "error",
            "error": f"Не удалось отправить файл на расшифровку: {e}",
        })
        jobs[job_id] = existing


async def call_llm_with_fallback(client: httpx.AsyncClient, user_msg: str, log_prefix: str):
    """Try each model in LLM_FALLBACK_CHAIN until one returns a valid draft.
    Handles finish_reason='length' by prompting the model to continue.
    Returns (draft, used_model, usage_dict). Raises if all models fail."""
    last_error: Optional[str] = None
    for model in LLM_FALLBACK_CHAIN:
        try:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ]
            full_draft = ""
            total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
            is_completed = False

            for loop_idx in range(5):
                async def _do_llm_call():
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
                            "messages": messages,
                        },
                    )
                    if llm_resp.status_code != 200:
                        raise RuntimeError(f"HTTP {llm_resp.status_code}: {llm_resp.text[:300]}")
                    return llm_resp

                llm_resp = await async_retry(_do_llm_call, retries=2, delay=0.5)
                if llm_resp.status_code != 200:
                    last_error = f"{model}: HTTP {llm_resp.status_code}: {llm_resp.text[:300]}"
                    log.warning(f"[{log_prefix}] {last_error}; trying next model")
                    break  # Break out of loop_idx, go to next model

                llm_data = llm_resp.json()

                if isinstance(llm_data, dict) and "error" in llm_data:
                    err = llm_data["error"]
                    err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    last_error = f"{model}: {err_msg}"
                    log.warning(f"[{log_prefix}] {last_error}; trying next model")
                    break

                choices = llm_data.get("choices") if isinstance(llm_data, dict) else None
                if not choices:
                    last_error = f"{model}: no choices in response — {str(llm_data)[:300]}"
                    log.warning(f"[{log_prefix}] {last_error}; trying next model")
                    break

                draft = choices[0].get("message", {}).get("content")
                if not draft:
                    last_error = f"{model}: empty content in choices[0]"
                    log.warning(f"[{log_prefix}] {last_error}; trying next model")
                    break

                full_draft += draft
                usage = llm_data.get("usage", {})
                total_usage["prompt_tokens"] = max(total_usage["prompt_tokens"], usage.get("prompt_tokens", 0))
                total_usage["completion_tokens"] += usage.get("completion_tokens", 0)

                finish_reason = choices[0].get("finish_reason")
                if finish_reason == "length" or len(draft) > 15000:
                    log.info(f"[{log_prefix}] Model hit token limit (length={len(draft)}, reason={finish_reason}). Continuing...")
                    messages.append({"role": "assistant", "content": draft})
                    messages.append({"role": "user", "content": "Твой предыдущий ответ оборвался из-за лимита токенов. Пожалуйста, продолжи строго с того места, где ты прервался, не повторяя уже написанное и ничего не пропуская."})
                    continue
                else:
                    is_completed = True
                    break

            if is_completed and full_draft:
                return full_draft, model, total_usage
            else:
                last_error = f"{model}: Failed to complete draft within iteration limits."
                continue

        except Exception as e:
            last_error = f"{model}: {e}"
            log.warning(f"[{log_prefix}] {last_error}; trying next model")
            continue

    raise RuntimeError(
        f"Все LLM-модели не отвечают. Попробуйте через несколько минут. Последняя ошибка: {last_error}"
    )


async def process_transcript(job_id: str):
    """Process transcript from AssemblyAI, format text, and run LLM drafting."""
    lock = get_lock(job_id)
    try:
        async with lock:
            existing = jobs.get(job_id, {})
            if existing.get("status") == "done":
                return
            metadata = existing.get("metadata", {})
            user_id = existing.get("user_id", "elena")
            filename = existing.get("filename", "")
            created_at = existing.get("created_at", int(time.time()))
            aai_started_at = existing.get("aai_started_at", created_at)
            audio_duration_sec = existing.get("audio_duration_sec")
            aai_transcript_id = existing.get("aai_transcript_id") or job_id

            jobs[job_id] = {
                "status": "processing",
                "phase": existing.get("phase", "processing"),
                "metadata": metadata,
                "filename": filename,
                "user_id": user_id,
                "created_at": created_at,
                "aai_started_at": aai_started_at,
                "aai_transcript_id": aai_transcript_id,
                "audio_duration_sec": audio_duration_sec,
            }

            try:
                client = get_shared_client()

                async def _do_fetch_transcript():
                    tx_resp = await client.get(
                        f"https://api.assemblyai.com/v2/transcript/{aai_transcript_id}",
                        headers={"authorization": ASSEMBLYAI_KEY},
                    )
                    tx_resp.raise_for_status()
                    return tx_resp.json()

                transcript = await async_retry(_do_fetch_transcript, retries=3, delay=1.0)

                audio_duration_sec = transcript.get("audio_duration") or audio_duration_sec

                if transcript.get("status") != "completed":
                    log.warning(f"process_transcript called for non-completed job {job_id} (AAI: {aai_transcript_id})")
                    jobs[job_id] = {
                        "status": "processing",
                        "phase": "transcribing",
                        "metadata": metadata,
                        "filename": filename,
                        "user_id": user_id,
                        "created_at": created_at,
                        "aai_started_at": aai_started_at,
                        "aai_transcript_id": aai_transcript_id,
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
                    f"Transcript {job_id}: {duration_min} min, "
                    f"{len(utterances)} utterances, {len(formatted)} chars. Calling LLM..."
                )

                drafting_started_at = int(time.time())
                jobs[job_id] = {
                    "status": "processing",
                    "phase": "drafting",
                    "metadata": metadata,
                    "filename": filename,
                    "user_id": user_id,
                    "created_at": created_at,
                    "aai_started_at": aai_started_at,
                    "aai_transcript_id": aai_transcript_id,
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
                    client, user_msg, job_id
                )
                log.info(
                    f"[{job_id}] Draft via {used_model} ({len(draft)} chars, "
                    f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')})"
                )

                jobs[job_id] = {
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
                    "aai_transcript_id": aai_transcript_id,
                    "audio_duration_sec": audio_duration_sec,
                }
            except Exception as e:
                log.exception(f"Processing failed for {job_id}")
                jobs[job_id] = {
                    "status": "error",
                    "error": str(e),
                    "metadata": metadata,
                    "filename": filename,
                    "user_id": user_id,
                    "created_at": created_at,
                    "aai_transcript_id": aai_transcript_id,
                }
    finally:
        remove_lock(job_id)


async def recover_pending_jobs():
    """Scan DB on startup for unfinished jobs and resume or clean them up."""
    try:
        pending = jobs.get_pending_jobs()
        if not pending:
            return
        log.info(f"Startup: found {len(pending)} pending jobs to recover")
        for item in pending:
            job_id = item.get("id")
            if not job_id:
                continue
            phase = item.get("phase")
            aai_transcript_id = item.get("aai_transcript_id")

            if phase == "uploading_to_aai" and not aai_transcript_id:
                item.update({
                    "status": "error",
                    "error": "Обработка прервана перезапуском сервера. Пожалуйста, загрузите файл повторно.",
                })
                jobs[job_id] = item
                log.warning(f"[{job_id}] Interrupted during initial upload — marked as error")
            else:
                log.info(f"[{job_id}] Resuming background processing (phase={phase}, aai_transcript_id={aai_transcript_id})")
                asyncio.create_task(process_transcript(job_id))
    except Exception:
        log.exception("Startup job recovery failed (non-fatal)")
