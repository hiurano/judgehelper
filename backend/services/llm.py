"""
LLM service module for OpenRouter interaction, chunking, and fallback logic.
"""
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import httpx

from backend.config import (
    LLM_FALLBACK_CHAIN,
    OPENROUTER_KEY,
    get_system_prompt,
    log,
)
from backend.services.http_client import async_retry


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


async def call_llm_with_fallback(client: "httpx.AsyncClient", user_msg: str, log_prefix: str):
    """Try each model in LLM_FALLBACK_CHAIN until one returns a valid draft.
    Handles finish_reason='length' by prompting the model to continue.
    Returns (draft, used_model, usage_dict). Raises if all models fail."""
    last_error: Optional[str] = None
    for model in LLM_FALLBACK_CHAIN:
        try:
            messages = [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": user_msg},
            ]
            full_draft = ""
            total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
            is_completed = False

            for loop_idx in range(5):
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
                    last_error = f"{model}: {err_msg}"
                    log.warning(f"[{log_prefix}] {last_error}; trying next model")
                    break

                choices = llm_data.get("choices") if isinstance(llm_data, dict) else None
                if not choices:
                    last_error = f"{model}: no choices in response — {str(llm_data)[:300]}"
                    log.warning(f"[{log_prefix}] {last_error}; trying next model")
                    break

                draft = choices[0].get("message", {}).get("content")
                if not draft:
                    last_error = f"{model}: empty content in choices[0]"
                    log.warning(f"[{log_prefix}] {last_error}; trying next model")
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
                return full_draft, model, total_usage
            else:
                last_error = f"{model}: Failed to complete draft within iteration limits."
                continue

        except Exception as e:
            last_error = f"{model}: {e}"
            log.warning(f"[{log_prefix}] {last_error}; trying next model")
            continue

    raise RuntimeError(
        f"Все LLM-модели не отвечают. Попробуйте через несколько минут. Последняя ошибка: {last_error}"
    )
