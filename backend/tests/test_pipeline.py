"""
Tests for the transcript processing pipeline.

The pipeline runs long after the request that started it has returned, so the
job it is working on can change underneath it. These cover what happens then.
"""
import asyncio

import pytest

import backend.services.pipeline as pipeline
from backend.db import jobs
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

    async def _fake_llm(client, user_msg, log_prefix):
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

    async def _fake_llm(client, user_msg, log_prefix):
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

    async def _fake_llm(client, user_msg, log_prefix):
        # The owner deletes the protocol while the model is still drafting.
        assert jobs.delete(job_id) is True
        return Draft("Текст удалённого протокола", "test-model", {})

    monkeypatch.setattr(pipeline, "call_llm_with_fallback", _fake_llm)

    asyncio.run(pipeline.process_transcript(job_id))

    assert jobs.get(job_id) is None


def test_process_transcript_skips_a_job_that_is_already_gone(fake_aai, monkeypatch):
    called = False

    async def _fake_llm(client, user_msg, log_prefix):
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
