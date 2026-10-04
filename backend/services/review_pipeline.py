"""Checkpointed analysis for the opt-in review pipeline."""
import time

from backend.config import ASSEMBLYAI_KEY, PROJECT_DIR
from backend.db import jobs
from backend.services.http_client import async_retry
from backend.services.protocol import (
    SCHEMA_VERSION, TEMPLATE_VERSION, batches_for_source, digest, document_blocks,
    document_text, normalize_source, role_registry,
)
from backend.services.review_llm import ReviewAnalysisError, propose_changes


class ReviewGone(Exception):
    pass


def initial_state():
    prompt = (PROJECT_DIR / "prompts" / "review-protocol.md").read_text(encoding="utf-8")
    return {"schema_version": SCHEMA_VERSION, "template_version": TEMPLATE_VERSION,
            "prompt_version": "review-1", "prompt_sha256": digest(prompt), "prompt": prompt,
            "stage": "processing", "batches": {}, "decisions": {}, "speakers": {},
            "utterances": {}, "fields": {}, "manual_text": {}, "resolved_concerns": [],
            "reviewed": False, "error": None, "failed_usage": {"prompt_tokens": 0, "completion_tokens": 0}}


def save(job_id, record, **job_updates):
    if not jobs.save_review(job_id, record["state"], record["revision"], job_updates):
        raise ReviewGone(job_id)
    record["revision"] += 1


async def process_review(job_id, job, client):
    record = jobs.get_review(job_id)
    if record is None:
        async def fetch_source():
            response = await client.get(
                f"https://api.assemblyai.com/v2/transcript/{job['aai_transcript_id']}",
                headers={"authorization": ASSEMBLYAI_KEY},
            )
            response.raise_for_status()
            return response.json()
        transcript = await async_retry(fetch_source, retries=3, delay=1.0)
        if transcript.get("status") != "completed":
            jobs.update_if_exists(job_id, {**job, "status": "processing", "phase": "transcribing"})
            return
        transcript.setdefault("id", job["aai_transcript_id"])
        source = normalize_source(transcript)
        if not jobs.create_review(job_id, source, initial_state()):
            return
        record = jobs.get_review(job_id)
        if record is None:
            return
    source, state = record["source"], record["state"]
    if state["stage"] in ("ready", "error"):
        # Webhook delivery is not permission to repeat a failed analysis or
        # overwrite human decisions. Only the retry endpoint restarts it.
        return
    batches = batches_for_source(source)
    try:
        save(job_id, record, status="processing", phase="drafting", has_review=True,
             review_status="processing", total_chunks=len(batches),
             utterances_count=len(source["utterances"]),
             speakers_count=len({u["speaker_id"] for u in source["utterances"]}),
             duration_min=round((source.get("audio_duration") or job.get("audio_duration_sec") or 0) / 60, 1),
             drafting_started_at=job.get("drafting_started_at") or int(time.time()))
        for index, segments in enumerate(batches):
            batch_id = str(index)
            if batch_id in state["batches"]:
                continue
            save(job_id, record, current_chunk=index + 1,
                 phase_detail=f"Проверка расшифровки: часть {index + 1} из {len(batches)}")
            # Registry grows across the entire source, never from the tail of
            # rewritten text. Conflicting candidates survive for human review.
            registry = [{"speaker_id": row["speaker_id"], "status": row["status"],
                         "candidates": [dict(role=role, name=name) for role, name in sorted({
                             (c["role"], c["name"]) for c in row["candidates"]})]}
                        for row in role_registry(source, state)]
            result = await propose_changes(client, source, segments, registry, state["prompt"])
            state["batches"][batch_id] = result
            save(job_id, record)
        state["stage"] = "ready"
        state["error"] = None
        usage = {key: state["failed_usage"][key] + sum(b.get("usage", {}).get(key, 0) for b in state["batches"].values())
                 for key in ("prompt_tokens", "completion_tokens")}
        save(job_id, record, status="done", phase="review", review_status="pending",
             draft=document_text(document_blocks(source, state)), truncated=False,
             phase_detail="Расшифровка готова к проверке", current_chunk=len(batches),
             error=None, model=", ".join(sorted({b["model"] for b in state["batches"].values()})),
             usage=usage)
    except ReviewGone:
        return
    except Exception as exc:
        state["stage"] = "error"
        state["error"] = "Анализ не завершён. Исходник и готовые части сохранены; можно повторить анализ."
        if isinstance(exc, ReviewAnalysisError):
            for key, count in exc.usage.items():
                state["failed_usage"][key] += count
        try:
            save(job_id, record, status="error", phase="review_error", review_status="error",
                 error=state["error"], draft=document_text(document_blocks(source, state)))
        except ReviewGone:
            pass
