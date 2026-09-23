"""
Tests for the transcript processing pipeline.

The pipeline runs long after the request that started it has returned, so the
job it is working on can change underneath it. These cover what happens then.
"""
import asyncio
import time

import pytest

import backend.services.pipeline as pipeline
from backend.config import JOB_MAX_LIFETIME_HOURS
from backend.db import get_lock, jobs
from backend.services.llm import Draft


class _FakeTranscriptResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakeAaiClient:
    """Stands in for the shared httpx client: only GET /transcript is used."""

    def __init__(self, body):
        self._body = body

    async def get(self, *args, **kwargs):
        return _FakeTranscriptResponse(self._body)


COMPLETED_TRANSCRIPT = {
    "status": "completed",
    "audio_duration": 120,
    "utterances": [
        {"speaker": "A", "text": "Судебное заседание объявляется открытым."},
        {"speaker": "B", "text": "Права разъяснены и понятны."},
    ],
}


@pytest.fixture
def fake_aai(monkeypatch):
    monkeypatch.setattr(
        pipeline, "get_shared_client", lambda: _FakeAaiClient(COMPLETED_TRANSCRIPT)
    )


def test_process_transcript_writes_the_draft(fake_aai, monkeypatch):
    jobs["job-happy-path"] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-happy-path",
    }

    async def _fake_llm(client, user_msg, log_prefix, **kwargs):
        return Draft("ПРОТОКОЛ судебного заседания", "test-model", {"prompt_tokens": 10})

    monkeypatch.setattr(pipeline, "call_llm_with_fallback", _fake_llm)

    asyncio.run(pipeline.process_transcript("job-happy-path"))

    stored = jobs.get("job-happy-path")
    assert stored["status"] == "done"
    assert stored["draft"] == "ПРОТОКОЛ судебного заседания"
    assert stored["speakers_count"] == 2
    assert stored["user_id"] == "test"
    assert stored["truncated"] is False


def test_a_truncated_draft_is_saved_and_flagged(fake_aai, monkeypatch):
    jobs["job-truncated"] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-truncated",
    }

    async def _fake_llm(client, user_msg, log_prefix, **kwargs):
        return Draft("Протокол обрывается на середине", "test-model", {}, truncated=True)

    monkeypatch.setattr(pipeline, "call_llm_with_fallback", _fake_llm)

    asyncio.run(pipeline.process_transcript("job-truncated"))

    stored = jobs.get("job-truncated")
    assert stored["status"] == "done"
    assert stored["draft"] == "Протокол обрывается на середине"
    # The operator has to be told, or they file an unfinished protocol.
    assert stored["truncated"] is True
    assert "неполным" in stored["phase_detail"]


def test_process_transcript_does_not_resurrect_a_deleted_job(fake_aai, monkeypatch):
    """A protocol deleted while it was being drafted must stay deleted."""
    job_id = "job-deleted-midflight"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-midflight",
    }

    async def _fake_llm(client, user_msg, log_prefix, **kwargs):
        # The owner deletes the protocol while the model is still drafting.
        assert jobs.delete(job_id) is True
        return Draft("Текст удалённого протокола", "test-model", {})

    monkeypatch.setattr(pipeline, "call_llm_with_fallback", _fake_llm)

    asyncio.run(pipeline.process_transcript(job_id))

    assert jobs.get(job_id) is None


def test_process_transcript_skips_a_job_that_is_already_gone(fake_aai, monkeypatch):
    called = False

    async def _fake_llm(client, user_msg, log_prefix, **kwargs):
        nonlocal called
        called = True
        return Draft("draft", "test-model", {})

    monkeypatch.setattr(pipeline, "call_llm_with_fallback", _fake_llm)

    asyncio.run(pipeline.process_transcript("job-never-existed"))

    assert called is False
    assert jobs.get("job-never-existed") is None


def test_failure_does_not_recreate_a_deleted_job(monkeypatch):
    job_id = "job-deleted-then-failed"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-deleted-then-failed",
    }

    class _ExplodingClient:
        async def get(self, *args, **kwargs):
            jobs.delete(job_id)
            raise RuntimeError("AssemblyAI is unreachable")

    async def _no_retry(coro_fn, **kwargs):
        return await coro_fn()

    monkeypatch.setattr(pipeline, "get_shared_client", lambda: _ExplodingClient())
    monkeypatch.setattr(pipeline, "async_retry", _no_retry)

    asyncio.run(pipeline.process_transcript(job_id))

    # The error branch must not write the job back either.
    assert jobs.get(job_id) is None


def test_recover_marks_a_job_interrupted_before_transcription(monkeypatch):
    """A crash while the upload was still streaming leaves no transcript id."""
    resumed = []
    # Keep recovery from firing real background work for unrelated rows that
    # other tests left in this database.
    monkeypatch.setattr(
        pipeline, "spawn", lambda coro, name: (coro.close(), resumed.append(name))
    )

    # This is the phase /upload sets while writing the file to disk.
    jobs["job-interrupted-receiving"] = {
        "status": "processing",
        "phase": "receiving_upload",
        "filename": "zasedanie.mp3",
        "user_id": "test",
    }

    asyncio.run(pipeline.recover_pending_jobs())

    recovered = jobs.get("job-interrupted-receiving")
    assert recovered["status"] == "error"
    assert recovered["phase"] == "error"
    assert "загрузите файл повторно" in recovered["error"]
    # No transcript id means nothing to resume, so no task should be started.
    assert "recover:job-interrupted-receiving" not in resumed


def test_recover_resumes_a_job_that_reached_transcription(monkeypatch):
    resumed = []
    monkeypatch.setattr(
        pipeline, "spawn", lambda coro, name: (coro.close(), resumed.append(name))
    )

    jobs["job-mid-transcription"] = {
        "status": "processing",
        "phase": "transcribing",
        "filename": "zasedanie.mp3",
        "user_id": "test",
        "aai_transcript_id": "aai-mid-transcription",
    }

    asyncio.run(pipeline.recover_pending_jobs())

    assert "recover:job-mid-transcription" in resumed
    assert jobs.get("job-mid-transcription")["status"] == "processing"


# --- Chunked drafting -----------------------------------------------------
#
# A hearing long enough to split is the normal case, not the exception, and
# the pieces have to come back as one protocol with the speakers still
# identified the same way throughout.

LONG_TRANSCRIPT = {
    "status": "completed",
    "audio_duration": 7200,
    "utterances": [
        {"speaker": "A" if i % 2 else "B", "text": f"Реплика номер {i}. " * 60}
        for i in range(30)
    ],
}


def test_a_long_transcript_is_drafted_in_parts_and_joined(monkeypatch):
    monkeypatch.setattr(
        pipeline, "get_shared_client", lambda: _FakeAaiClient(LONG_TRANSCRIPT)
    )
    prompts = []
    structures = []

    async def _fake_llm(client, user_msg, log_prefix, **kwargs):
        prompts.append(user_msg)
        structures.append(kwargs["structure_instruction"])
        part = len(prompts)
        return Draft(
            f"[КЛЮЧ РОЛЕЙ: Спикер A = Судья]\nЧасть {part} протокола.",
            "test-model",
            {"prompt_tokens": 10, "completion_tokens": 20},
        )

    monkeypatch.setattr(pipeline, "call_llm_with_fallback", _fake_llm)

    jobs["job-long-hearing"] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-long-hearing",
    }
    asyncio.run(pipeline.process_transcript("job-long-hearing"))

    stored = jobs.get("job-long-hearing")
    assert stored["status"] == "done"
    assert stored["total_chunks"] > 1
    assert len(prompts) == stored["total_chunks"]
    assert stored["current_chunk"] == stored["total_chunks"]

    assert "Шапку протокола и вводную часть оформи только здесь" in structures[0]
    assert all("Не повторяй шапку" in item for item in structures[1:])
    assert all("Не добавляй заключительный шаблон" in item for item in structures[:-1])
    assert "подписи оформи один раз" in structures[-1]

    # Every part is in the protocol, in order...
    for part in range(1, len(prompts) + 1):
        assert f"Часть {part} протокола." in stored["draft"]
    positions = [stored["draft"].index(f"Часть {p} протокола.")
                 for p in range(1, len(prompts) + 1)]
    assert positions == sorted(positions)

    # ...and the model's bookkeeping line is not part of what the judge reads.
    assert "КЛЮЧ РОЛЕЙ" not in stored["draft"]

    # The role mapping is carried into the next part so the speakers keep
    # their identities across the seam.
    assert "СОХРАНЕННЫЙ МАППИНГ РОЛЕЙ" in prompts[1]
    assert "Спикер A = Судья" in prompts[1]

    # Usage is summed over the parts, all of which are paid for.
    assert stored["truncated"] is False
    jobs.delete("job-long-hearing")


def test_one_truncated_part_flags_the_whole_protocol(monkeypatch):
    monkeypatch.setattr(
        pipeline, "get_shared_client", lambda: _FakeAaiClient(LONG_TRANSCRIPT)
    )
    calls = {"n": 0}

    async def _fake_llm(client, user_msg, log_prefix, **kwargs):
        calls["n"] += 1
        # Only the second part runs out of room.
        return Draft(f"Часть {calls['n']}.", "test-model", {}, truncated=calls["n"] == 2)

    monkeypatch.setattr(pipeline, "call_llm_with_fallback", _fake_llm)

    jobs["job-partly-truncated"] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-partly-truncated",
    }
    asyncio.run(pipeline.process_transcript("job-partly-truncated"))

    stored = jobs.get("job-partly-truncated")
    assert stored["truncated"] is True
    assert "может быть неполным" in stored["phase_detail"]
    jobs.delete("job-partly-truncated")


# --- The stall watchdog ---------------------------------------------------
#
# Nothing else moves a job off `processing` once the step that owned it is
# gone, and every such job holds one of the account's three active slots.

def _stale_timestamp() -> int:
    return int(time.time()) - (JOB_MAX_LIFETIME_HOURS * 3600) - 60


def test_a_stalled_job_is_failed_so_its_slot_comes_back():
    job_id = "job-stalled-in-transcription"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-vanished",
        "created_at": _stale_timestamp(),
    }

    assert pipeline.fail_stalled_jobs() >= 1

    failed = jobs.get(job_id)
    assert failed["status"] == "error"
    assert failed["phase"] == "error"
    assert "загрузите запись повторно" in failed["error"]
    # No longer 'processing', so it no longer occupies one of the user's slots.
    assert job_id not in {job["id"] for job in jobs.get_pending_jobs()}


def test_a_job_still_within_the_limit_is_left_alone():
    job_id = "job-still-working"
    jobs[job_id] = {
        "status": "processing",
        "phase": "drafting",
        "user_id": "test",
        "created_at": int(time.time()) - 60,
    }

    pipeline.fail_stalled_jobs()

    assert jobs.get(job_id)["status"] == "processing"
    jobs.delete(job_id)


def test_a_job_a_worker_still_holds_is_not_failed():
    """A held lock proves someone is on it; failing it would race their write."""
    job_id = "job-held-by-a-worker"
    jobs[job_id] = {
        "status": "processing",
        "phase": "drafting",
        "user_id": "test",
        "created_at": _stale_timestamp(),
    }

    async def with_the_lock_held():
        async with get_lock(job_id):
            return pipeline.fail_stalled_jobs()

    asyncio.run(with_the_lock_held())

    assert jobs.get(job_id)["status"] == "processing"
    jobs.delete(job_id)


def test_the_polling_status_write_does_not_keep_a_job_looking_fresh():
    """Age is counted from the upload, not from `updated_at`.

    The polling loop writes the recognition service's status back on every
    pass, so a job wedged on that service's side is touched every minute."""
    job_id = "job-touched-but-stalled"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "created_at": _stale_timestamp(),
    }
    # Exactly what poll_one does each cycle: rewrite the row, bumping updated_at.
    touched = jobs.get(job_id)
    touched["aai_status"] = "processing"
    assert jobs.update_if_exists(job_id, touched)

    pipeline.fail_stalled_jobs()

    assert jobs.get(job_id)["status"] == "error"


def test_recover_gives_up_on_a_job_older_than_the_limit(monkeypatch):
    resumed = []
    monkeypatch.setattr(
        pipeline, "spawn", lambda coro, name: (coro.close(), resumed.append(name))
    )

    job_id = "job-stale-across-a-restart"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-stale",
        "created_at": _stale_timestamp(),
    }

    asyncio.run(pipeline.recover_pending_jobs())

    assert f"recover:{job_id}" not in resumed
    assert jobs.get(job_id)["status"] == "error"
