import asyncio
import json
import sqlite3
import time

import httpx
import pytest

from backend.db import JobStore
from backend.services import review_llm, review_pipeline
from backend.services.protocol import batches_for_source, normalize_source
from backend.services.review_pipeline import initial_state


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path / 'jobs.db'))
    monkeypatch.setattr(review_pipeline, "jobs", store)
    store["job"] = {"user_id": "test", "status": "processing", "protocol_mode": "review", "aai_transcript_id": "aai-id"}
    return store


def empty_response(segments):
    return {"reviewed_segments": [s["id"] for s in segments], "corrections": [], "roles": [], "concerns": []}


def test_checkpoint_resume_no_second_asr_or_successful_llm_calls(store, monkeypatch):
    transcript = {"status": "completed", "text": "Исходная речь. " * 2500}
    source_calls, llm_calls = [], []

    async def handle(request):
        source_calls.append(request.url)
        return httpx.Response(200, json=transcript)

    async def propose(client, source, segments, registry, prompt):
        llm_calls.append(segments[0]["id"])
        assert store.get_review('job')["source"]["utterances"][0]["text"] == transcript["text"]
        if len(llm_calls) == 2:
            raise review_llm.ReviewAnalysisError({"prompt_tokens": 20, "completion_tokens": 5})
        return {"result": empty_response(segments), "model": "test", "usage": {}}

    monkeypatch.setattr(review_pipeline, "propose_changes", propose)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await review_pipeline.process_review('job', store.get('job'), client)
            record = store.get_review('job')
            assert record["state"]["stage"] == "error"
            assert len(record["state"]["batches"]) == 1
            assert record["state"]["failed_usage"]["prompt_tokens"] == 20
            assert store.get('job')["status"] == "error"
            await review_pipeline.process_review('job', store.get('job'), client)
            assert len(llm_calls) == 2  # late webhook cannot restart an error
            record["state"]["stage"] = "processing"
            assert store.save_review('job', record["state"], record["revision"])
            await review_pipeline.process_review('job', store.get('job'), client)
    asyncio.run(run())
    assert len(source_calls) == 1
    assert llm_calls.count(llm_calls[0]) == 1
    assert store.get('job')["status"] == "done"
    assert store.get_review('job')["state"]["stage"] == "ready"
    assert store.get('job')["draft"].count(transcript["text"]) == 1


def test_delete_wins_over_pending_model_reply(store, monkeypatch):
    source = normalize_source({"text": "Синтетическая запись."})
    store.create_review('job', source, initial_state())

    async def propose(client, source, segments, registry, prompt):
        store.delete('job')
        return {"result": empty_response(segments)}
    monkeypatch.setattr(review_pipeline, "propose_changes", propose)
    asyncio.run(review_pipeline.process_review('job', store.get('job'), None))
    assert store.get('job') is None
    assert store.get_review('job') is None


@pytest.mark.parametrize("remove", ["job", "account", "ttl"])
def test_reviews_share_the_job_lifecycle_and_source_is_immutable(store, remove):
    source = normalize_source({"text": "Исходник"})
    assert store.create_review('job', source, initial_state())
    with pytest.raises(ValueError):
        store.create_review('job', normalize_source({"text": "Другой текст"}), initial_state())
    record = store.get_review('job')
    assert store.save_review('job', record["state"], 0, {"draft": "new"})
    assert not store.save_review('job', record["state"], 0, {"draft": "stale"})
    assert store.get('job')["draft"] == "new"
    assert 'source' not in store.list_recent()[0]
    if remove == "job":
        store.delete('job')
    elif remove == "account":
        store.delete_for_user('test')
    else:
        with sqlite3.connect(store.path) as conn:
            conn.execute('UPDATE jobs SET updated_at = ?', (int(time.time()) - 40 * 86400,))
        store.cleanup_old(30)
    assert store.get_review('job') is None
    assert not store.save_review('job', record["state"], 1)
    assert not store.create_review('job', source, initial_state())


@pytest.mark.parametrize("invalid", ["truncated", "missing", "invalid_json", "refusal", "invalid_position"])
def test_structured_reply_validation_and_fallback_usage(monkeypatch, invalid):
    source = normalize_source({"text": "Тестовая речь."})
    segments = batches_for_source(source)[0]
    calls = []
    monkeypatch.setattr(review_llm, 'LLM_FALLBACK_CHAIN', ['first', 'second'])

    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        content = json.dumps(empty_response(segments))
        reason, refusal = 'stop', None
        if payload['model'] == 'first':
            if invalid == 'truncated': reason = 'length'
            elif invalid == 'refusal': refusal = 'refused'
            elif invalid == 'missing': content = '{}'
            elif invalid == 'invalid_json': content = '{'
            else:
                body = empty_response(segments)
                body['corrections'] = [{'utterance_id': 'u000001', 'start': 0, 'end': 3,
                    'original': 'нет', 'replacement': 'да', 'category': 'recognition', 'reason': 'test'}]
                content = json.dumps(body)
        return httpx.Response(200, json={'choices': [{'finish_reason': reason, 'message': {'content': content, 'refusal': refusal}}],
                                        'usage': {'prompt_tokens': 5, 'completion_tokens': 2}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await review_llm.propose_changes(client, source, segments, [], 'test prompt')
    result = asyncio.run(run())
    assert result['model'] == 'second'
    assert result['usage'] == {'prompt_tokens': 15, 'completion_tokens': 6}
    assert len(calls) == 3
    assert all(c['response_format']['type'] == 'json_schema' for c in calls)
    assert all(c['provider']['require_parameters'] for c in calls)


def test_all_invalid_answers_raise_instead_of_returning_partial_draft(monkeypatch):
    monkeypatch.setattr(review_llm, 'LLM_FALLBACK_CHAIN', ['test'])
    source = normalize_source({'text': 'Запись'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            'choices': [{'finish_reason': 'length', 'message': {'content': '{'}}], 'usage': {'prompt_tokens': 1},
        }))) as client:
            await review_llm.propose_changes(client, source, batches_for_source(source)[0], [], 'test')
    with pytest.raises(review_llm.ReviewAnalysisError) as error:
        asyncio.run(run())
    assert error.value.usage['prompt_tokens'] == 2
