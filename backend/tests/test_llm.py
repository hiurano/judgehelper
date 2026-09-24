"""
Tests for the OpenRouter drafting layer: model fallback and длинные ответы.
"""
import asyncio

import pytest

import backend.services.llm as llm


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _FakeClient:
    """Returns the queued bodies in order and records what was asked for."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append(kwargs.get("json", {}))
        return _FakeResponse(self._bodies.pop(0))


def _completion(content: str, finish_reason: str = "stop", **usage):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
    }


@pytest.fixture
def two_model_chain(monkeypatch):
    monkeypatch.setattr(llm, "LLM_FALLBACK_CHAIN", ["primary/model", "backup/model"])


def test_uses_the_first_model_that_answers(two_model_chain):
    client = _FakeClient([_completion("ПРОТОКОЛ", prompt_tokens=11, completion_tokens=22)])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-1"))

    assert result.text == "ПРОТОКОЛ"
    assert result.model == "primary/model"
    assert result.usage == {"prompt_tokens": 11, "completion_tokens": 22}
    assert result.truncated is False
    assert len(client.requests) == 1


def test_falls_back_to_the_next_model_on_a_provider_error(two_model_chain):
    client = _FakeClient([
        {"error": {"message": "model is overloaded"}},
        _completion("ПРОТОКОЛ"),
    ])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-2"))

    assert result.text == "ПРОТОКОЛ"
    assert result.model == "backup/model"
    assert [req["model"] for req in client.requests] == ["primary/model", "backup/model"]


def test_falls_back_when_a_model_returns_no_choices(two_model_chain):
    client = _FakeClient([{"choices": []}, _completion("ПРОТОКОЛ")])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-3"))

    assert result.text == "ПРОТОКОЛ"
    assert result.model == "backup/model"


def test_continues_a_reply_cut_off_by_the_token_limit(two_model_chain):
    client = _FakeClient([
        _completion("Первая половина. ", finish_reason="length",
                    prompt_tokens=100, completion_tokens=200),
        _completion("Вторая половина.", prompt_tokens=300, completion_tokens=50),
    ])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-4"))

    assert result.text == "Первая половина. Вторая половина."
    assert result.model == "primary/model"
    assert result.truncated is False
    # Both calls are paid for, so both must be counted.
    assert result.usage == {"prompt_tokens": 400, "completion_tokens": 250}
    # The continuation must carry the truncated answer back as context.
    assert client.requests[1]["messages"][-2]["content"] == "Первая половина. "


def test_a_long_but_finished_reply_is_not_continued(two_model_chain):
    """A real protocol runs to tens of thousands of characters.

    Asking a model that already said "stop" to continue makes it restate the
    whole protocol, so the draft came back duplicated once per continuation and
    flagged as possibly incomplete."""
    client = _FakeClient([_completion("А" * 16000)])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-8"))

    assert len(client.requests) == 1
    assert result.text == "А" * 16000
    assert result.truncated is False


def test_a_provider_specific_truncation_reason_is_honoured(two_model_chain):
    """Not every provider calls a token-limit stop "length"."""
    client = _FakeClient([
        _completion("Первая половина. ", finish_reason="max_tokens"),
        _completion("Вторая половина."),
    ])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-9"))

    assert result.text == "Первая половина. Вторая половина."
    assert result.truncated is False


def test_an_unknown_finish_reason_counts_as_finished(two_model_chain):
    """Without a reason to think otherwise, stop: continuing duplicates work."""
    client = _FakeClient([_completion("ПРОТОКОЛ", finish_reason=None)])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-10"))

    assert len(client.requests) == 1
    assert result.text == "ПРОТОКОЛ"
    assert result.truncated is False


def test_keeps_the_longest_partial_when_no_model_finishes(two_model_chain):
    """A protocol cut off by the token limit still beats losing everything."""
    short = [_completion(f"A{i}", finish_reason="length")
             for i in range(llm.MAX_CONTINUATIONS)]
    long = [_completion(f"Длинная часть {i}. ", finish_reason="length")
            for i in range(llm.MAX_CONTINUATIONS)]
    client = _FakeClient(short + long)

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-6"))

    assert result.truncated is True
    assert result.model == "backup/model"
    assert result.text.startswith("Длинная часть 0. ")
    # Every continuation it managed is kept, not just the last one.
    assert result.text.count("Длинная часть") == llm.MAX_CONTINUATIONS


def test_prefers_a_complete_answer_over_a_partial_one(two_model_chain):
    truncated_attempts = [_completion("Очень длинный обрывок. ", finish_reason="length")
                          for _ in range(llm.MAX_CONTINUATIONS)]
    client = _FakeClient(truncated_attempts + [_completion("Короткий но целый")])

    result = asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-7"))

    assert result.text == "Короткий но целый"
    assert result.model == "backup/model"
    assert result.truncated is False


def test_raises_when_every_model_fails(two_model_chain):
    client = _FakeClient([
        {"error": {"message": "first is down"}},
        {"error": {"message": "second is down"}},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-5"))

    assert "second is down" in str(excinfo.value)


def test_chunk_structure_survives_continuation_and_fallback(two_model_chain, monkeypatch):
    monkeypatch.setattr(llm, "get_system_prompt", lambda: "Полный шаблон с шапкой и подписями.")
    client = _FakeClient([
        _completion("Начало реплики", finish_reason="length"),
        {"error": {"message": "unavailable"}},
        _completion("Продолжение протокола"),
    ])
    instruction = llm.chunk_structure_instruction(1, 3)
    result = asyncio.run(llm.call_llm_with_fallback(
        client, "Текущий фрагмент", "job-chunk", structure_instruction=instruction,
    ))
    assert result.text == "Продолжение протокола"
    assert len(client.requests) == 3
    for request in client.requests:
        system = request["messages"][0]
        assert system["role"] == "system"
        assert system["content"].endswith(instruction)
        assert "Не повторяй шапку" in system["content"]
        assert "Не добавляй заключительный шаблон" in system["content"]


@pytest.mark.parametrize('fallback', [False, True])
def test_continuation_exception_preserves_partial_or_uses_complete_fallback(monkeypatch, fallback):
    import httpx
    monkeypatch.setattr(llm, 'LLM_FALLBACK_CHAIN', ['first', 'second'] if fallback else ['first'])
    async def no_delays(operation, **kwargs):
        return await operation()
    monkeypatch.setattr(llm, 'async_retry', no_delays)

    class Client:
        calls = 0
        async def post(self, url, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise httpx.ReadTimeout('continuation failed')
            return _FakeResponse(_completion(
                'complete' if self.calls == 3 else 'saved partial',
                'stop' if self.calls == 3 else 'length',
                prompt_tokens=10, completion_tokens=20,
            ))

    result = asyncio.run(llm.call_llm_with_fallback(Client(), 'transcript', 'job'))
    assert result.text == ('complete' if fallback else 'saved partial')
    assert result.truncated is not fallback
    assert result.usage == {'prompt_tokens': 10, 'completion_tokens': 20}
