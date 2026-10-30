"""Часы и метки времени.

ТЗ §4.1: локальное время приёма не эквивалентно времени события на бирже,
поэтому оба значения хранятся раздельно, вместе с монотонной меткой.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

NS_MS = 1_000_000


class Clock:
    """Системные часы. Все внутренние расчёты — в наносекундах."""

    def wall_ns(self) -> int:
        """Время UNIX, нс. Может прыгать при синхронизации NTP."""
        return time.time_ns()

    def monotonic_ns(self) -> int:
        """Монотонное время, нс. Используется для всех длительностей."""
        return time.monotonic_ns()


class FakeClock(Clock):
    """Управляемые часы для тестов."""

    def __init__(self, wall_ns: int = 1_700_000_000 * 10**9, monotonic_ns: int = 0) -> None:
        self._wall = wall_ns
        self._mono = monotonic_ns

    def wall_ns(self) -> int:
        return self._wall

    def monotonic_ns(self) -> int:
        return self._mono

    def advance_ms(self, ms: float) -> None:
        delta = int(ms * NS_MS)
        self._wall += delta
        self._mono += delta


@dataclass(frozen=True, slots=True)
class EventMeta:
    """Метаданные события, обязательные по ТЗ §4.1."""

    symbol: str
    channel: str
    event_type: str
    exchange_ts_ms: int | None
    receive_wall_ns: int
    receive_mono_ns: int
    sequence_first: int | None = None
    sequence_last: int | None = None

    @property
    def latency_ms(self) -> float | None:
        """Задержка доставки: приём минус время события. None, если биржа не дала время."""
        if self.exchange_ts_ms is None:
            return None
        return (self.receive_wall_ns - self.exchange_ts_ms * NS_MS) / NS_MS
