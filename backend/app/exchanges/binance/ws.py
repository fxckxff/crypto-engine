"""WebSocket Binance Spot: комбинированный поток публичных данных."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from app.core.time import Clock
from app.market_data.websocket_manager import ReconnectingStream, ReconnectPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WsMessage:
    stream: str
    data: dict[str, object]
    receive_ns: int
    receive_mono_ns: int


@dataclass(slots=True)
class WsStats:
    messages: int = 0
    decode_errors: int = 0
    unknown_streams: int = 0


def stream_names(symbols: list[str], depth_speed_ms: int = 100) -> list[str]:
    """Имена стримов Binance: depth, aggTrade, bookTicker."""
    speed = "" if depth_speed_ms >= 1000 else f"@{depth_speed_ms}ms"
    names: list[str] = []
    for symbol in symbols:
        low = symbol.lower()
        names.append(f"{low}@depth{speed}")
        names.append(f"{low}@aggTrade")
        names.append(f"{low}@bookTicker")
    return names


class BinanceWsClient:
    """Одно соединение на все символы и каналы (ТЗ §4.1 п.8)."""

    def __init__(
        self,
        base_url: str,
        symbols: list[str],
        depth_speed_ms: int = 100,
        policy: ReconnectPolicy | None = None,
        clock: Clock | None = None,
        on_disconnect: Callable[[BaseException], None] | None = None,
    ) -> None:
        if not symbols:
            raise ValueError("at least one symbol is required")
        self.base_url = base_url.rstrip("/")
        self.symbols = [s.upper() for s in symbols]
        self.depth_speed_ms = depth_speed_ms
        self.clock = clock or Clock()
        self.streams = stream_names(self.symbols, depth_speed_ms)
        self.stats = WsStats()
        self._on_disconnect: Callable[[BaseException], None] | None = on_disconnect

        def _on_disconnect(name: str, exc: BaseException) -> None:
            cb = self._on_disconnect
            if cb is not None:
                cb(exc)

        self._stream = ReconnectingStream(
            name="binance_public",
            url_factory=self._url,
            policy=policy,
            clock=self.clock,
            on_disconnect=_on_disconnect,
        )

    def set_on_disconnect(self, cb: Callable[[BaseException], None] | None) -> None:
        self._on_disconnect = cb

    def _url(self) -> str:
        return f"{self.base_url}/stream?streams={'/'.join(self.streams)}"

    @property
    def connection_stats(self):
        return self._stream.stats

    async def messages(self) -> AsyncIterator[WsMessage]:
        async for raw in self._stream.messages():
            receive_ns = time.time_ns()
            receive_mono_ns = time.monotonic_ns()
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                self.stats.decode_errors += 1
                logger.warning("ws decode error", extra={"extra_fields": {"raw": raw[:300]}})
                continue
            if not isinstance(envelope, dict):
                self.stats.decode_errors += 1
                continue
            stream = str(envelope.get("stream", ""))
            data = envelope.get("data")
            if not isinstance(data, dict):
                # Одиночный стрим без обёртки — допускается для тестов и listenKey.
                if "e" in envelope or "u" in envelope:
                    data = envelope
                else:
                    self.stats.decode_errors += 1
                    continue
            self.stats.messages += 1
            yield WsMessage(stream=stream, data=data, receive_ns=receive_ns, receive_mono_ns=receive_mono_ns)


@dataclass(slots=True)
class PrivateStream:
    """Поток приватных событий по listenKey. Переподключение — на уровне адаптера."""

    base_url: str
    listen_key: str
    policy: ReconnectPolicy = field(default_factory=ReconnectPolicy)

    async def messages(self) -> AsyncIterator[dict[str, object]]:
        url = f"{self.base_url}/ws/{self.listen_key}"
        stream = ReconnectingStream(name="binance_private", url_factory=url, policy=self.policy)
        async for raw in stream.messages():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("private stream decode error")
                continue
            if isinstance(payload, dict):
                yield payload
