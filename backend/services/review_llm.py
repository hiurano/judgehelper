"""Bounded structured proposals; invalid/partial replies never become a draft."""
import json
import time

from backend.config import LLM_FALLBACK_CHAIN, OPENROUTER_KEY, log
from backend.services.http_client import async_retry
from backend.services.protocol import ProposalBatch, validate_proposals


class ReviewAnalysisError(RuntimeError):
    def __init__(self, usage):
        super().__init__("Не удалось получить проверяемые предложения ИИ. Исходная расшифровка сохранена.")
        self.usage = usage


async def propose_changes(client, source, segments, registry, prompt):
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    started = time.monotonic()
    for model in LLM_FALLBACK_CHAIN:
        # One retry for malformed JSON or invalid source positions. Transport
        # retries remain bounded independently. No free-text continuation.
        for attempt in range(2):
            try:
                async def send():
                    response = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {OPENROUTER_KEY}",
                                 "HTTP-Referer": "https://github.com/hiurano/judgehelper",
                                 "X-OpenRouter-Title": "Judge Helper"},
                        json={"model": model, "temperature": 0, "max_completion_tokens": 8000,
                              "provider": {"require_parameters": True},
                              "response_format": {"type": "json_schema", "json_schema": {
                                  "name": "protocol_review", "strict": True,
                                  "schema": ProposalBatch.model_json_schema()}},
                              "messages": [{"role": "system", "content": prompt},
                                           {"role": "user", "content": json.dumps({
                                               "segments": segments, "registry": registry,
                                           }, ensure_ascii=False)}]},
                    )
                    response.raise_for_status()
                    return response.json()

                body = await async_retry(send, retries=2, delay=1.0)
                if not isinstance(body, dict) or body.get("error"):
                    raise ValueError("Provider error")
                for key in usage:
                    count = (body.get("usage") or {}).get(key, 0)
                    if type(count) is int and count >= 0:
                        usage[key] += count
                choice = body["choices"][0]
                if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                    raise ValueError("Incomplete or refused response")
                content = choice["message"]["content"]
                if not isinstance(content, str) or len(content) > 150000:
                    raise ValueError("Invalid response size")
                result = validate_proposals(json.loads(content), source, segments)
                return {"result": result, "model": model, "usage": usage,
                        "elapsed_seconds": round(time.monotonic() - started, 2)}
            except Exception as exc:
                # Validation errors can contain fragments of court testimony.
                log.warning("Review model=%s attempt=%s failed (%s)", model, attempt + 1, type(exc).__name__)
    raise ReviewAnalysisError(usage)
