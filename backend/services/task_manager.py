"""Lifecycle management for in-process background tasks."""
import asyncio
from collections.abc import Coroutine
from typing import Any

from backend.config import log


_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """Start and retain a task until completion so it cannot be orphaned."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)

    def _completed(done: asyncio.Task):
        _tasks.discard(done)
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.error("Background task %s failed: %r", done.get_name(), exc)

    task.add_done_callback(_completed)
    return task


async def cancel_all() -> None:
    """Cancel and await every retained background task during shutdown."""
    current = asyncio.current_task()
    pending = [task for task in _tasks if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _tasks.clear()


async def cancel_job(job_id: str) -> None:
    """Stop local submission/drafting after the owner deletes its job row."""
    names = {f'{kind}:{job_id}' for kind in ('aai-submit', 'process', 'recover')}
    pending = [task for task in _tasks if task.get_name() in names and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
