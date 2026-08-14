"""
HTTP client management and retry utilities for Judge Helper services.
"""
import asyncio
from typing import Optional

try:
    from fastapi import HTTPException
except ImportError:
    class HTTPException(Exception):
        pass

try:
    import httpx
except ImportError:
    httpx = None

from backend.config import log

_shared_client = None


async def async_retry(coro_fn, retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Retry an async operation on transient network failures or HTTP errors."""
    last_exc = None
    curr_delay = delay
    for attempt in range(1, retries + 1):
        try:
            return await coro_fn()
        except HTTPException:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt == retries:
                break
            log.warning(f"Network call failed (attempt {attempt}/{retries}): {exc}. Retrying in {curr_delay:.1f}s...")
            await asyncio.sleep(curr_delay)
            curr_delay *= backoff
    raise last_exc


def get_shared_client():
    """Get or instantiate the global shared httpx.AsyncClient."""
    global _shared_client
    if httpx is None:
        raise RuntimeError("httpx is required to use get_shared_client")
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=600.0,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _shared_client


async def close_shared_client():
    """Gracefully close the global shared httpx.AsyncClient."""
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None
