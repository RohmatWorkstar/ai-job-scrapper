"""Bounded retry with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")


def backoff_delay(attempt: int, base: float = 1.5, cap: float = 30.0, retry_after: float | None = None) -> float:
    """Delay before retry number ``attempt`` (1-based). Honors Retry-After when given."""
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, cap)
    return min(cap, base * (2 ** (attempt - 1))) * (0.75 + random.random() * 0.5)


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    is_retryable: Callable[[BaseException], bool],
    base: float = 1.5,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``func`` with up to ``attempts`` retries for retryable exceptions only."""
    last: BaseException | None = None
    for attempt in range(attempts + 1):
        try:
            return await func()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            last = exc
            if attempt >= attempts or not is_retryable(exc):
                raise
            await sleep(backoff_delay(attempt + 1, base))
    assert last is not None
    raise last
