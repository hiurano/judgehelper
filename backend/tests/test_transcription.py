"""Tests for the AssemblyAI polling pass.

This is what moves a job forward when the webhook does not arrive, and what
decides a job can never finish. Both were previously untested.
"""
import asyncio

import pytest

import backend.services.transcription as transcription
from backend.db import jobs


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status on {self.status_code}")

    def json(self):
        return self._body


class _Client:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    async def get(self, *args, **kwargs):
        self.calls += 1
        return self._response


def _poll(client, job_id):
    row = jobs.get(job_id)
    row["id"] = job_id
    return asyncio.run(
        transcription.poll_one_job(client, row, asyncio.Semaphore(10))
    )


@pytest.fixture
def spawned(monkeypatch):
    started = []
    monkeypatch.setattr(
        transcription, "spawn", lambda coro, name: (coro.close(), started.append(name))
    )
    return started


def test_a_vanished_transcript_fails_the_job(spawned):
    """AssemblyAI drops transcripts past its retention window.

    Polling could only repeat the 404, so the job stayed 'processing' and held
    one of the account's three slots until the retention pass removed it."""
    job_id = "job-transcript-vanished"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-vanished",
    }

    _poll(_Client(_Resp(404)), job_id)

    failed = jobs.get(job_id)
    assert failed["status"] == "error"
    assert failed["phase"] == "error"
    assert "загрузите запись повторно" in failed["error"]
    assert job_id not in {job["id"] for job in jobs.get_pending_jobs()}
    jobs.delete(job_id)


def test_a_completed_transcript_starts_the_drafting(spawned):
    job_id = "job-poll-completed"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-completed",
    }

    _poll(_Client(_Resp(200, {"status": "completed", "audio_duration": 300})), job_id)

    row = jobs.get(job_id)
    assert row["phase"] == "drafting"
    assert row["audio_duration_sec"] == 300
    assert f"process:{job_id}" in spawned
    jobs.delete(job_id)


def test_a_failed_transcription_is_reported(spawned):
    job_id = "job-poll-aai-error"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-broken",
    }

    _poll(_Client(_Resp(200, {"status": "error"})), job_id)

    row = jobs.get(job_id)
    assert row["status"] == "error"
    assert "не смог обработать" in row["error"]
    assert spawned == []
    jobs.delete(job_id)


def test_a_transcript_still_running_is_only_noted(spawned):
    job_id = "job-poll-in-progress"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-running",
    }

    _poll(_Client(_Resp(200, {"status": "processing"})), job_id)

    row = jobs.get(job_id)
    assert row["status"] == "processing"
    assert row["phase"] == "transcribing"
    assert row["aai_status"] == "processing"
    assert spawned == []
    jobs.delete(job_id)


def test_a_job_a_worker_holds_is_skipped(spawned):
    """The webhook may have started drafting a moment earlier."""
    job_id = "job-poll-locked"
    jobs[job_id] = {
        "status": "processing",
        "phase": "transcribing",
        "user_id": "test",
        "aai_transcript_id": "aai-locked",
    }
    client = _Client(_Resp(200, {"status": "completed"}))

    async def poll_while_locked():
        from backend.db import get_lock
        async with get_lock(job_id):
            row = jobs.get(job_id)
            row["id"] = job_id
            await transcription.poll_one_job(client, row, asyncio.Semaphore(10))

    asyncio.run(poll_while_locked())

    assert client.calls == 0
    assert jobs.get(job_id)["phase"] == "transcribing"
    assert spawned == []
    jobs.delete(job_id)


def test_a_job_without_a_transcript_id_is_skipped(spawned):
    client = _Client(_Resp(200, {"status": "completed"}))

    asyncio.run(
        transcription.poll_one_job(
            client,
            {"id": "job-no-aai", "phase": "transcribing"},
            asyncio.Semaphore(10),
        )
    )

    assert client.calls == 0
