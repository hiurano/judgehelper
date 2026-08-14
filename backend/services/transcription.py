"""
Transcription service for AssemblyAI speech-to-text integration and background polling.
"""
import asyncio
from pathlib import Path
import time
from typing import TYPE_CHECKING

from backend.config import (
    ASSEMBLYAI_KEY,
    BASE_URL,
    WEBHOOK_SECRET,
    WORD_BOOST,
    log,
)
from backend.db import get_lock, jobs
from backend.services.http_client import async_retry, get_shared_client


async def submit_to_assemblyai(job_id: str, file_path: Path, filename: str):
    """Background task: stream file from disk to AssemblyAI and update transcription job."""
    try:
        client = get_shared_client()

        async def file_streamer():
            def read_chunk(f):
                return f.read(64 * 1024)
            with open(file_path, "rb") as f:
                while True:
                    chunk = await asyncio.to_thread(read_chunk, f)
                    if not chunk:
                        break
                    yield chunk

        async def _do_upload():
            up_resp = await client.post(
                "https://api.assemblyai.com/v2/upload",
                headers={"authorization": ASSEMBLYAI_KEY},
                content=file_streamer(),
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
            "phase_detail": "Распознавание речи и разделение спикеров...",
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
    finally:
        # Clean up the temporary file from disk
        if isinstance(file_path, Path):
            file_path.unlink(missing_ok=True)


async def aai_polling_loop():
    """
    Background loop that polls AssemblyAI for pending jobs.
    If webhooks are configured, it runs less frequently as a fallback.
    If webhooks are NOT configured, it polls every 15 seconds.
    """
    from backend.services.pipeline import process_transcript

    sleep_interval = 60 if (BASE_URL and WEBHOOK_SECRET) else 15
    while True:
        try:
            await asyncio.sleep(sleep_interval)
            pending = jobs.get_pending_jobs()
            if not pending:
                continue

            client = get_shared_client()
            for p_job in pending:
                job_id = p_job.get("id")
                aai_id = p_job.get("aai_transcript_id")
                
                if not aai_id or p_job.get("phase") != "transcribing":
                    continue
                
                # If someone is already processing this job (e.g. webhook just fired), skip
                lock = get_lock(job_id)
                if lock.locked():
                    continue

                try:
                    tx_resp = await client.get(
                        f"https://api.assemblyai.com/v2/transcript/{aai_id}",
                        headers={"authorization": ASSEMBLYAI_KEY},
                    )
                    if tx_resp.status_code == 404:
                        continue
                    tx_resp.raise_for_status()
                    aai_body = tx_resp.json()
                    
                    aai_status = aai_body.get("status")
                    aai_audio_duration = aai_body.get("audio_duration")
                    
                    cached = jobs.get(job_id, {})
                    if not cached or cached.get("status") in ("done", "error"):
                        continue
                        
                    if aai_audio_duration and not cached.get("audio_duration_sec"):
                        cached["audio_duration_sec"] = aai_audio_duration

                    if aai_status == "error":
                        cached.update({"status": "error", "error": aai_body.get("error", "AssemblyAI error")})
                        jobs[job_id] = cached
                    elif aai_status == "completed":
                        cached.update({
                            "status": "processing",
                            "phase": "drafting",
                            "drafting_started_at": cached.get("drafting_started_at") or int(time.time()),
                        })
                        jobs[job_id] = cached
                        if not lock.locked():
                            asyncio.create_task(process_transcript(job_id))
                    else:
                        cached.update({"aai_status": aai_status})
                        jobs[job_id] = cached

                except Exception as inner_e:
                    log.error(f"[{job_id}] Polling error: {inner_e}")
                    
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception(f"Error in aai_polling_loop: {e}")
