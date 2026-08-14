"""
Pipeline orchestration and background job recovery for Judge Helper.
"""
import asyncio
import time

from backend.config import (
    ASSEMBLYAI_KEY,
    DEFAULT_USER,
    log,
)
from backend.db import get_lock, jobs, remove_lock
from backend.services.http_client import async_retry, get_shared_client
from backend.services.llm import call_llm_with_fallback, split_transcript_into_chunks
from backend.services.text_cleaner import clean_transcript, format_metadata_block


async def process_transcript(job_id: str):
    """Process transcript from AssemblyAI, format text, and run LLM drafting."""
    lock = get_lock(job_id)
    try:
        async with lock:
            existing = jobs.get(job_id, {})
            if existing.get("status") == "done":
                return
            metadata = existing.get("metadata", {})
            user_id = existing.get("user_id", DEFAULT_USER)
            filename = existing.get("filename", "")
            created_at = existing.get("created_at", int(time.time()))
            aai_started_at = existing.get("aai_started_at", created_at)
            audio_duration_sec = existing.get("audio_duration_sec")
            aai_transcript_id = existing.get("aai_transcript_id") or job_id

            existing.update({
                "status": "processing",
                "phase": existing.get("phase", "processing"),
                "aai_transcript_id": aai_transcript_id,
                "audio_duration_sec": audio_duration_sec,
            })
            jobs[job_id] = existing

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
                    existing.update({
                        "status": "processing",
                        "phase": "transcribing",
                        "audio_duration_sec": audio_duration_sec,
                    })
                    jobs[job_id] = existing
                    return

                utterances = transcript.get("utterances") or []
                if utterances:
                    formatted = "\n\n".join(
                        f"[Спикер {u['speaker']}]: {u['text']}" for u in utterances
                    )
                else:
                    formatted = transcript.get("text", "")

                speakers = set(u.get("speaker") for u in utterances if u.get("speaker"))
                speakers_count = len(speakers) if speakers else (1 if utterances else 0)
                utterances_count = len(utterances)
                formatted = clean_transcript(formatted)
                duration_min = round((audio_duration_sec or 0) / 60, 1)

                log.info(
                    f"Transcript {job_id}: {duration_min} min, "
                    f"{utterances_count} utterances, {speakers_count} speakers, {len(formatted)} chars. Calling LLM..."
                )

                meta_block = format_metadata_block(metadata)
                chunks = split_transcript_into_chunks(formatted, max_chunk_chars=12000)
                total_chunks = len(chunks)

                drafting_started_at = int(time.time())
                existing.update({
                    "status": "processing",
                    "phase": "drafting",
                    "audio_duration_sec": audio_duration_sec,
                    "duration_min": duration_min,
                    "speakers_count": speakers_count,
                    "utterances_count": utterances_count,
                    "total_chunks": total_chunks,
                    "current_chunk": 1,
                    "drafting_started_at": drafting_started_at,
                    "phase_detail": f"Составление протокола нейросетью (часть 1 из {total_chunks})..." if total_chunks > 1 else "Составление протокола нейросетью...",
                })
                jobs[job_id] = existing

                if len(chunks) == 1:
                    user_msg = (
                        f"{meta_block}"
                        "Составь черновик протокола судебного заседания на основе "
                        f"следующей размеченной стенограммы аудиозаписи:\n\n{formatted}"
                    )
                    draft, used_model, usage = await call_llm_with_fallback(
                        client, user_msg, job_id
                    )
                else:
                    log.info(f"[{job_id}] Transcript split into {len(chunks)} chunks for sequential drafting.")
                    drafts = []
                    used_model = None
                    total_prompt_tokens = 0
                    total_completion_tokens = 0

                    last_context = ""

                    for idx, chunk_text in enumerate(chunks):
                        existing_chunk = jobs.get(job_id, {})
                        existing_chunk.update({
                            "current_chunk": idx + 1,
                            "total_chunks": total_chunks,
                            "phase_detail": f"Составление протокола нейросетью (часть {idx + 1} из {total_chunks})...",
                        })
                        jobs[job_id] = existing_chunk
                        if idx == 0:
                            prompt = (
                                f"{meta_block}"
                                f"Это ЧАСТЬ 1 из {len(chunks)} стенограммы судебного заседания.\n"
                                "ВАЖНО: В САМОЙ ПЕРВОЙ СТРОКЕ своего ответа ОБЯЗАТЕЛЬНО напиши строгий маппинг в формате: [КЛЮЧ РОЛЕЙ: Спикер А = Судья, Спикер B = Защитник]\n"
                                "Со второй строки сформируй вводную часть протокола (шапку, состав суда, наименование дела) и оформи начальные реплики в официальном стиле.\n\n"
                                f"{chunk_text}"
                            )
                        elif idx == len(chunks) - 1:
                            prompt = (
                                f"Это ФИНАЛЬНАЯ ЧАСТЬ {idx + 1} из {len(chunks)} стенограммы судебного заседания.\n"
                                f"Контекст для сохранения ролей (конец предыдущей части):\n{last_context}\n\n"
                                "ОБЯЗАТЕЛЬНО преобразуй ВСЕ метки [Спикер A/B/C/D]: в официальные судебные роли (Председательствующий:, Защитник:, Государственный обвинитель:, Подсудимый:, Свидетель:). Запрещено оставлять сырые метки [Спикер X]!\n"
                                "Продолжи дословное оформление реплик и судебных действий в официальном стиле, а в конце сформируй итоговый блок подписей (председательствующий судья, секретарь):\n\n"
                                f"{chunk_text}"
                            )
                        else:
                            prompt = (
                                f"Это ЧАСТЬ {idx + 1} из {len(chunks)} стенограммы судебного заседания.\n"
                                f"Контекст для сохранения ролей (конец предыдущей части):\n{last_context}\n\n"
                                "ОБЯЗАТЕЛЬНО преобразуй ВСЕ метки [Спикер A/B/C/D]: в официальные судебные роли (Председательствующий:, Защитник:, Государственный обвинитель:, Подсудимый:, Свидетель:). Запрещено оставлять сырые метки [Спикер X]!\n"
                                "ВАЖНО: В САМОЙ ПЕРВОЙ СТРОКЕ своего ответа ОБЯЗАТЕЛЬНО напиши строгий маппинг в формате: [КЛЮЧ РОЛЕЙ: Спикер А = Судья, Спикер B = Защитник]\n"
                                "Со второй строки оформи содержательную часть реплик и действий участников процесса в официальном стиле.\n\n"
                                f"{chunk_text}"
                            )

                        c_draft, c_model, c_usage = await call_llm_with_fallback(
                            client, prompt, f"{job_id}-chunk-{idx + 1}"
                        )

                        # Robustly extract and remove role key lines
                        role_lines = []
                        clean_lines = []
                        for line in c_draft.split('\n'):
                            if 'КЛЮЧ РОЛЕЙ' in line.upper() or 'МАППИНГ' in line.upper():
                                role_lines.append(line.strip())
                            else:
                                clean_lines.append(line)
                                
                        role_key = "\n".join(role_lines)
                        c_draft_clean = "\n".join(clean_lines).strip()

                        drafts.append(c_draft_clean)
                        
                        # Save last 1500 chars + role key to maintain character identities across chunks
                        last_text = c_draft_clean[-1500:] if len(c_draft_clean) > 1500 else c_draft_clean
                        last_context = f"{last_text}\n\nСОХРАНЕННЫЙ МАППИНГ РОЛЕЙ ИЗ ПРЕДЫДУЩЕЙ ЧАСТИ:\n{role_key}"
                        
                        if not used_model:
                            used_model = c_model
                        total_prompt_tokens += (c_usage or {}).get("prompt_tokens", 0)
                        total_completion_tokens += (c_usage or {}).get("completion_tokens", 0)

                    usage = {
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                    }
                    draft = "\n\n".join(drafts)

                log.info(
                    f"[{job_id}] Draft via {used_model} ({len(draft)} chars, "
                    f"chunks={len(chunks)}, in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')})"
                )

                existing_done = jobs.get(job_id, {})
                existing_done.update({
                    "status": "done",
                    "draft": draft,
                    "transcript": formatted,
                    "duration_min": duration_min,
                    "speakers_count": speakers_count,
                    "utterances_count": utterances_count,
                    "total_chunks": total_chunks,
                    "current_chunk": total_chunks,
                    "model": used_model,
                    "phase": "done",
                    "phase_detail": "Протокол сформирован",
                })
                jobs[job_id] = existing_done
            except Exception as e:
                log.exception(f"Processing failed for {job_id}")
                existing.update({
                    "status": "error",
                    "error": str(e),
                })
                jobs[job_id] = existing
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
    except Exception as e:
        log.exception(f"Error recovering pending jobs: {e}")
