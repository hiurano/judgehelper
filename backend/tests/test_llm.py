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
