"""
Transcription service for AssemblyAI speech-to-text integration and background polling.
"""
import asyncio
from pathlib import Path
import time
from backend.config import (
    ASSEMBLYAI_KEY,
    BASE_URL,
    WEBHOOK_SECRET,
    WORD_BOOST,
    log,
)
from backend.db import get_lock, jobs
from backend.services.http_client import async_retry, get_shared_client
from backend.services.task_manager import spawn


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
            up_resp.raise_for_status()
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
            submit_resp.raise_for_status()
            return submit_resp.json()["id"]

        aai_transcript_id = await async_retry(_do_submit, retries=3, delay=1.0)

        now = int(time.time())
        existing = jobs.get(job_id) or {}
        existing.update({
            "status": "processing",
            "phase": "transcribing",
            "phase_detail": "Распознавание речи и разделение спикеров...",
            "aai_transcript_id": aai_transcript_id,
            "aai_started_at": now,
        })
        if not jobs.update_if_exists(job_id, existing):
            log.info(f"[{job_id}] Job deleted during upload — not recreating it")
            return
        log.info(f"[{job_id}] AssemblyAI aai_transcript_id={aai_transcript_id}")
    except Exception:
        log.exception(f"[{job_id}] background submit to AssemblyAI failed")
        existing = jobs.get(job_id) or {}
        existing.update({
            "status": "error",
            "error": "Не удалось отправить файл на расшифровку. Повторите попытку позже.",
            "phase": "error",
        })
        jobs.update_if_exists(job_id, existing)
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
    from backend.services.pipeline import fail_stalled_jobs, process_transcript

    sleep_interval = 60 if (BASE_URL and WEBHOOK_SECRET) else 15
    while True:
        try:
            await asyncio.sleep(sleep_interval)

            # Before polling, retire anything that has outlived any plausible
            # hearing. This loop is the only thing that runs often enough to
            # free a wedged job's active slot while its owner is still waiting.
            stalled = await asyncio.to_thread(fail_stalled_jobs)
            if stalled:
                log.warning("Marked %s stalled job(s) as failed", stalled)

            pending = jobs.get_pending_jobs()
            if not pending:
                continue

            client = get_shared_client()
            semaphore = asyncio.Semaphore(10)

            async def poll_one(p_job):
                job_id = p_job.get("id")
                aai_id = p_job.get("aai_transcript_id")
                if not aai_id or p_job.get("phase") != "transcribing":
                    return

                # If someone is already processing this job (e.g. webhook just fired), skip
                lock = get_lock(job_id)
                if lock.locked():
                    return

                try:
                    async with semaphore:
                        tx_resp = await client.get(
                            f"https://api.assemblyai.com/v2/transcript/{aai_id}",
                            headers={"authorization": ASSEMBLYAI_KEY},
                        )
                    if tx_resp.status_code == 404:
                        # The transcript is gone from AssemblyAI — past its
                        # retention window, or removed. Polling can only repeat
                        # this 404, so stop instead of holding the slot until
                        # the stall watchdog notices hours later.
                        gone = jobs.get(job_id)
                        if gone and gone.get("status") not in ("done", "error"):
                            gone.update({
                                "status": "error",
                                "phase": "error",
                                "error": (
                                    "Расшифровка больше недоступна на сервисе распознавания. "
                                    "Пожалуйста, загрузите запись повторно."
                                ),
                            })
                            jobs.update_if_exists(job_id, gone)
                            log.warning(f"[{job_id}] AssemblyAI no longer has transcript {aai_id}")
                        return
                    tx_resp.raise_for_status()
                    aai_body = tx_resp.json()

                    aai_status = aai_body.get("status")
                    aai_audio_duration = aai_body.get("audio_duration")

                    cached = jobs.get(job_id, {})
                    if not cached or cached.get("status") in ("done", "error"):
                        return

                    if aai_audio_duration and not cached.get("audio_duration_sec"):
                        cached["audio_duration_sec"] = aai_audio_duration

                    if aai_status == "error":
                        cached.update({
                            "status": "error",
                            "phase": "error",
                            "error": "Сервис распознавания не смог обработать запись.",
                        })
                        jobs.update_if_exists(job_id, cached)
                    elif aai_status == "completed":
                        cached.update({
                            "status": "processing",
                            "phase": "drafting",
                            "drafting_started_at": cached.get("drafting_started_at") or int(time.time()),
                        })
                        if not jobs.update_if_exists(job_id, cached):
                            return
                        if not lock.locked():
                            spawn(process_transcript(job_id), name=f"process:{job_id}")
                    else:
                        cached.update({"aai_status": aai_status})
                        jobs.update_if_exists(job_id, cached)

                except Exception as inner_e:
                    log.error(f"[{job_id}] Polling error: {inner_e}")

            await asyncio.gather(*(poll_one(p_job) for p_job in pending))

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception(f"Error in aai_polling_loop: {e}")
