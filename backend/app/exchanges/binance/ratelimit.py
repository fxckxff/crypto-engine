"""Локальный лимит запросов (token bucket) — часть адаптера, а не обёртка над ошибками."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, tokens_per_minute: int, burst: int | None = None) -> None:
        if tokens_per_minute <= 0:
            raise ValueError("tokens_per_minute must be positive")
        self.capacity = float(burst if burst is not None else max(tokens_per_minute // 6, 1))
        self.rate_per_second = tokens_per_minute / 60.0
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self.waited_seconds = 0.0

    async def acquire(self, weight: int = 1) -> None:
        if weight <= 0:
            raise ValueError("weight must be positive")
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate_per_second)
                self._updated = now
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                needed = (weight - self._tokens) / self.rate_per_second
                self.waited_seconds += needed
                await asyncio.sleep(needed)

    @property
    def available(self) -> float:
        return self._tokens
