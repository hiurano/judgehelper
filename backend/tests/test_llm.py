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

    draft, used_model, usage = asyncio.run(
        llm.call_llm_with_fallback(client, "стенограмма", "job-1")
    )

    assert draft == "ПРОТОКОЛ"
    assert used_model == "primary/model"
    assert usage == {"prompt_tokens": 11, "completion_tokens": 22}
    assert len(client.requests) == 1


def test_falls_back_to_the_next_model_on_a_provider_error(two_model_chain):
    client = _FakeClient([
        {"error": {"message": "model is overloaded"}},
        _completion("ПРОТОКОЛ"),
    ])

    draft, used_model, _ = asyncio.run(
        llm.call_llm_with_fallback(client, "стенограмма", "job-2")
    )

    assert draft == "ПРОТОКОЛ"
    assert used_model == "backup/model"
    assert [req["model"] for req in client.requests] == ["primary/model", "backup/model"]


def test_falls_back_when_a_model_returns_no_choices(two_model_chain):
    client = _FakeClient([{"choices": []}, _completion("ПРОТОКОЛ")])

    draft, used_model, _ = asyncio.run(
        llm.call_llm_with_fallback(client, "стенограмма", "job-3")
    )

    assert draft == "ПРОТОКОЛ"
    assert used_model == "backup/model"


def test_continues_a_reply_cut_off_by_the_token_limit(two_model_chain):
    client = _FakeClient([
        _completion("Первая половина. ", finish_reason="length",
                    prompt_tokens=100, completion_tokens=200),
        _completion("Вторая половина.", prompt_tokens=300, completion_tokens=50),
    ])

    draft, used_model, usage = asyncio.run(
        llm.call_llm_with_fallback(client, "стенограмма", "job-4")
    )

    assert draft == "Первая половина. Вторая половина."
    assert used_model == "primary/model"
    # Both calls are paid for, so both must be counted.
    assert usage == {"prompt_tokens": 400, "completion_tokens": 250}
    # The continuation must carry the truncated answer back as context.
    assert client.requests[1]["messages"][-2]["content"] == "Первая половина. "


def test_raises_when_every_model_fails(two_model_chain):
    client = _FakeClient([
        {"error": {"message": "first is down"}},
        {"error": {"message": "second is down"}},
    ])

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(llm.call_llm_with_fallback(client, "стенограмма", "job-5"))

    assert "second is down" in str(excinfo.value)
