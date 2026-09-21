"""
LLM service module for OpenRouter interaction, chunking, and fallback logic.
"""
from typing import TYPE_CHECKING, NamedTuple, Optional

if TYPE_CHECKING:
    import httpx

from backend.config import (
    LLM_FALLBACK_CHAIN,
    OPENROUTER_KEY,
    get_system_prompt,
    log,
)
from backend.services.http_client import async_retry


MAX_CONTINUATIONS = 5


class Draft(NamedTuple):
    """A model's answer. `truncated` means it never reached a natural end."""
    text: str
    model: str
    usage: dict
    truncated: bool = False


def split_transcript_into_chunks(formatted_text: str, max_chunk_chars: int = 12000) -> list[str]:
    """Split transcript text into chunks of at most max_chunk_chars,
    splitting only on utterance boundaries (double newlines) to avoid breaking sentences."""
    if len(formatted_text) <= max_chunk_chars:
        return [formatted_text]

    paragraphs = formatted_text.split("\n\n")
    chunks = []
    current_chunk = []
    current_length = 0

    for para in paragraphs:
        if len(para) > max_chunk_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            # A single unusually long utterance must not bypass the model limit.
            for start in range(0, len(para), max_chunk_chars):
                chunks.append(para[start:start + max_chunk_chars])
            continue
        para_len = len(para) + 2  # account for \n\n
        if current_length + para_len > max_chunk_chars and current_chunk:
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [para]
            current_length = para_len
        else:
            current_chunk.append(para)
            current_length += para_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


async def call_llm_with_fallback(client: "httpx.AsyncClient", user_msg: str, log_prefix: str) -> Draft:
    """Try each model in LLM_FALLBACK_CHAIN until one returns a valid draft.
    Handles finish_reason='length' by prompting the model to continue.
    Returns a Draft. Raises only if no model produced any text at all."""
    last_error: Optional[str] = None
    # A model that keeps hitting the token limit still wrote a real protocol.
    # Prefer a complete answer from a later model, but never throw the work
    # away: hours of hearing and every token spent on it are in here.
    best_partial: Optional[Draft] = None
    for model in LLM_FALLBACK_CHAIN:
        try:
            messages = [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": user_msg},
            ]
            full_draft = ""
            total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
            is_completed = False
            # Kept apart from last_error: the generic "gave up" message below
            # must not overwrite what the provider actually said went wrong.
            model_error: Optional[str] = None

            for loop_idx in range(MAX_CONTINUATIONS):
                async def _do_llm_call(current_messages):
                    llm_resp = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {OPENROUTER_KEY}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://github.com/hiurano/judge-helper",
                            "X-OpenRouter-Title": "Judge Helper",
                        },
                        json={
                            "model": model,
                            "max_completion_tokens": 16000,
                            "temperature": 0.3,
                            "messages": current_messages,
                        },
                    )
                    llm_resp.raise_for_status()
                    return llm_resp

                # We use a lambda to cleanly pass the current state of messages
                llm_resp = await async_retry(lambda: _do_llm_call(messages), retries=4, delay=1.5, backoff=2.0)

                llm_data = llm_resp.json()

                if isinstance(llm_data, dict) and "error" in llm_data:
                    err = llm_data["error"]
                    err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    model_error = f"{model}: {err_msg}"
                    log.warning(f"[{log_prefix}] {model_error}; trying next model")
                    break

                choices = llm_data.get("choices") if isinstance(llm_data, dict) else None
                if not choices:
                    model_error = f"{model}: no choices in response — {str(llm_data)[:300]}"
                    log.warning(f"[{log_prefix}] {model_error}; trying next model")
                    break

                draft = choices[0].get("message", {}).get("content")
                if not draft:
                    model_error = f"{model}: empty content in choices[0]"
                    log.warning(f"[{log_prefix}] {model_error}; trying next model")
                    break

                full_draft += draft
                usage = llm_data.get("usage", {})
                total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
                total_usage["completion_tokens"] += usage.get("completion_tokens", 0)

                finish_reason = choices[0].get("finish_reason")
                if finish_reason == "length" or len(draft) > 15000:
                    log.info(f"[{log_prefix}] Model hit token limit (length={len(draft)}, reason={finish_reason}). Continuing...")
                    messages.append({"role": "assistant", "content": draft})
                    messages.append({"role": "user", "content": "Твой предыдущий ответ оборвался из-за лимита токенов. Пожалуйста, продолжи строго с того места, где ты прервался, не повторяя уже написанное и ничего не пропуская."})
                    continue
                else:
                    is_completed = True
                    break

            if is_completed and full_draft:
                return Draft(full_draft, model, total_usage)

            last_error = model_error or (
                f"{model}: Failed to complete draft within iteration limits."
            )
            if full_draft and (best_partial is None or len(full_draft) > len(best_partial.text)):
                best_partial = Draft(full_draft, model, total_usage, truncated=True)
            continue

        except Exception as e:
            last_error = f"{model}: {e}"
            log.warning(f"[{log_prefix}] {last_error}; trying next model")
            continue

    if best_partial is not None:
        log.warning(
            "[%s] No model finished cleanly; keeping the longest partial draft "
            "from %s (%s chars). Last error: %s",
            log_prefix, best_partial.model, len(best_partial.text), last_error,
        )
        return best_partial

    raise RuntimeError(
        f"Все LLM-модели не отвечают. Попробуйте через несколько минут. Последняя ошибка: {last_error}"
    )
