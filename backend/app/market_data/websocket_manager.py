"""Переподключение WebSocket с экспоненциальной паузой (ТЗ §4.1 п.1, §3.1).

Модуль не знает про Binance: он умеет держать соединение и сообщать о разрывах.
Разбор сообщений — задача адаптера.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import websockets
from websockets.exceptions import ConnectionClosed

from app.core.time import Clock
from app.core.tls import build_ssl_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    initial_ms: int = 500
    max_ms: int = 30_000
    factor: float = 2.0
    jitter: float = 0.2

    def delay_s(self, attempt: int) -> float:
        cap = math.ceil(math.log(max(self.max_ms / self.initial_ms, 1), self.factor))
        base = min(self.initial_ms * (self.factor ** min(max(attempt - 1, 0), cap)), self.max_ms) / 1000.0
        if self.jitter <= 0:
            return base
        return base * (1.0 - self.jitter + random.random() * 2 * self.jitter)


@dataclass(slots=True)
class ConnectionStats:
    connects: int = 0
    disconnects: int = 0
    last_connected_ns: int = 0
    last_disconnect_ns: int = 0
    last_error: str | None = None
    reconnect_attempts: int = 0

    @property
    def connected(self) -> bool:
        return self.last_connected_ns > self.last_disconnect_ns


class ReconnectingStream:
    """Асинхронный поток сообщений с автоматическим восстановлением соединения."""

    def __init__(
        self,
        name: str,
        url_factory: Callable[[], str] | str,
        policy: ReconnectPolicy | None = None,
        clock: Clock | None = None,
        on_disconnect: Callable[[str, BaseException], None] | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.name = name
        self._url_factory = url_factory if callable(url_factory) else (lambda: url_factory)
        self.policy = policy or ReconnectPolicy()
        self.clock = clock or Clock()
        self.on_disconnect = on_disconnect
        self.max_attempts = max_attempts
        self.stats = ConnectionStats()

    async def messages(self) -> AsyncIterator[str]:
        attempt = 0
        while True:
            url = self._url_factory()
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=4096,
                    open_timeout=15,
                    ssl=build_ssl_context() if url.startswith("wss:") else None,
                ) as socket:
                    attempt = 0
                    self.stats.connects += 1
                    self.stats.last_connected_ns = self.clock.monotonic_ns()
                    logger.info("ws connected", extra={"extra_fields": {"stream": self.name}})
                    async for message in socket:
                        if isinstance(message, bytes):
                            message = message.decode("utf-8", errors="replace")
                        yield message
                    raise ConnectionError("WebSocket closed normally; reconnect required")
            except asyncio.CancelledError:
                self.stats.last_disconnect_ns = self.clock.monotonic_ns()
                raise
            except (ConnectionClosed, OSError, TimeoutError) as exc:
                attempt += 1
                self.stats.disconnects += 1
                self.stats.last_disconnect_ns = self.clock.monotonic_ns()
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                self.stats.reconnect_attempts = attempt
                if self.on_disconnect is not None:
                    self.on_disconnect(self.name, exc)
                logger.warning(
                    "ws disconnected",
                    extra={
                        "extra_fields": {
                            "stream": self.name,
                            "attempt": attempt,
                            "error": self.stats.last_error,
                        }
                    },
                )
                if self.max_attempts is not None and attempt >= self.max_attempts:
                    logger.error("ws reconnect attempts exhausted", extra={"extra_fields": {"stream": self.name}})
                    return
                await asyncio.sleep(self.policy.delay_s(attempt))
            except Exception as exc:
                attempt += 1
                self.stats.disconnects += 1
                self.stats.last_disconnect_ns = self.clock.monotonic_ns()
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "ws unexpected failure",
                    extra={"extra_fields": {"stream": self.name, "attempt": attempt}},
                )
                if self.max_attempts is not None and attempt >= self.max_attempts:
                    return
                await asyncio.sleep(self.policy.delay_s(attempt))


class BoundedEventQueue:
    """Очередь событий с регистрацией потерь вместо бесконечного роста памяти."""

    def __init__(self, maxsize: int = 50_000) -> None:
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    async def put(self, item: object) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def get(self) -> object:
        return await self._queue.get()

    @property
    def qsize(self) -> int:
        return self._queue.qsize()


def now_ms() -> float:
    return time.monotonic() * 1000.0
