"""
AI service facade module for Judge Helper.
Re-exports components from modular subservices for full backward compatibility:
- http_client: Shared HTTP client management and retry utilities.
- transcription: AssemblyAI speech-to-text upload and polling.
- llm: Text chunking and OpenRouter LLM drafting.
- pipeline: End-to-end processing pipeline and recovery.
"""

from backend.services.http_client import (
    async_retry,
    close_shared_client,
    get_shared_client,
)
from backend.services.llm import (
    call_llm_with_fallback,
    split_transcript_into_chunks,
)
from backend.services.pipeline import (
    process_transcript,
    recover_pending_jobs,
)
from backend.services.transcription import (
    aai_polling_loop,
    submit_to_assemblyai,
)

__all__ = [
    "async_retry",
    "get_shared_client",
    "close_shared_client",
    "submit_to_assemblyai",
    "aai_polling_loop",
    "split_transcript_into_chunks",
    "call_llm_with_fallback",
    "process_transcript",
    "recover_pending_jobs",
]
