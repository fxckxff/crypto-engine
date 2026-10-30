"""Скользящее окно выборок с метками времени."""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from statistics import median

from app.core.time import NS_MS


@dataclass(frozen=True, slots=True)
class Sample:
    ts_ns: int
    value: float


class RollingWindow:
    """Выборки за последние window_ms, с временным весом.

    Хранит только то, что нужно расчётам: пару (время, значение).
    """

    def __init__(self, window_ms: float, maxlen: int = 10_000) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be positive")
        self.window_ns = int(window_ms * NS_MS)
        self._samples: deque[Sample] = deque(maxlen=maxlen)

    def add(self, ts_ns: int, value: float) -> None:
        self._samples.append(Sample(ts_ns, value))
        self._prune(ts_ns)

    def _prune(self, now_ns: int) -> None:
        cutoff = now_ns - self.window_ns
        while self._samples and self._samples[0].ts_ns < cutoff:
            self._samples.popleft()

    def samples(self, now_ns: int | None = None) -> list[Sample]:
        if now_ns is not None:
            self._prune(now_ns)
        return list(self._samples)

    def values(self, now_ns: int | None = None) -> list[float]:
        return [s.value for s in self.samples(now_ns)]

    def __len__(self) -> int:
        return len(self._samples)

    def latest(self) -> Sample | None:
        return self._samples[-1] if self._samples else None

    def median(self, now_ns: int | None = None) -> float | None:
        vals = self.values(now_ns)
        return median(vals) if vals else None

    def percentile_rank(self, value: float, now_ns: int | None = None) -> float | None:
        """Доля выборок <= value. Показывает, насколько текущее значение редкое."""
        vals = self.values(now_ns)
        if not vals:
            return None
        ordered = sorted(vals)
        return bisect_left(ordered, value) / len(ordered)

    def stdev(self, now_ns: int | None = None) -> float | None:
        vals = self.values(now_ns)
        if len(vals) < 2:
            return None
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        return var**0.5

    def time_weighted_mean(self, now_ns: int) -> float | None:
        """Среднее, взвешенное временем жизни каждого значения (ТЗ §5.1)."""
        samples = self.samples(now_ns)
        if not samples:
            return None
        total = 0.0
        weighted = 0.0
        for i, sample in enumerate(samples):
            end = samples[i + 1].ts_ns if i + 1 < len(samples) else now_ns
            dwell = max(end - sample.ts_ns, 0)
            total += dwell
            weighted += sample.value * dwell
        return weighted / total if total > 0 else samples[-1].value

    def duration_above(self, threshold: float, now_ns: int) -> int:
        """Сколько нс подряд последние значения превышают порог (длительность широкого спреда)."""
        samples = self.samples(now_ns)
        if not samples:
            return 0
        start_ns = now_ns
        for i in range(len(samples) - 1, -1, -1):
            if samples[i].value <= threshold:
                break
            start_ns = samples[i].ts_ns
        return now_ns - start_ns
