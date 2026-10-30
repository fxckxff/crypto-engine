"""Microprice и его производные (ТЗ §5.3).

microprice = (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.core.time import NS_MS
from app.market_data.orderbook import LocalOrderBook


@dataclass(frozen=True, slots=True)
class MicropriceState:
    value: float
    deviation_bps: float
    velocity_bps_s: float | None
    acceleration_bps_s2: float | None
    direction_stability: float | None


def microprice(bid: Decimal, ask: Decimal, bid_qty: Decimal, ask_qty: Decimal) -> float | None:
    total = bid_qty + ask_qty
    if total <= 0:
        return None
    return float((ask * bid_qty + bid * ask_qty) / total)


class MicropriceTracker:
    def __init__(self, window_ms: float = 5_000.0, stability_window: int = 20) -> None:
        self.window_ns = int(window_ms * NS_MS)
        self.stability_window = stability_window
        self._history: deque[tuple[int, float, float]] = deque(maxlen=2_000)
        self._deviations: deque[float] = deque(maxlen=stability_window)

    def update(self, book: LocalOrderBook, now_ns: int) -> MicropriceState | None:
        bid, ask = book.best_bid(), book.best_ask()
        if bid is None or ask is None:
            return None
        value = microprice(bid.price, ask.price, bid.quantity, ask.quantity)
        if value is None:
            return None
        mid = float((bid.price + ask.price) / 2)
        if mid <= 0:
            return None
        deviation_bps = (value - mid) / mid * 10_000
        self._history.append((now_ns, value, mid))
        self._deviations.append(deviation_bps)
        self._prune(now_ns)

        velocity = self._velocity(now_ns)
        accel = self._acceleration(now_ns)
        return MicropriceState(
            value=value,
            deviation_bps=deviation_bps,
            velocity_bps_s=velocity,
            acceleration_bps_s2=accel,
            direction_stability=self.direction_stability(),
        )

    def _prune(self, now_ns: int) -> None:
        cutoff = now_ns - self.window_ns
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

    def _velocity(self, now_ns: int) -> float | None:
        if len(self._history) < 2:
            return None
        first_ts, first_val, first_mid = self._history[0]
        last_ts, last_val, _ = self._history[-1]
        dt = (last_ts - first_ts) / 1e9
        if dt <= 0 or first_mid <= 0:
            return None
        return (last_val - first_val) / first_mid * 10_000 / dt

    def _acceleration(self, now_ns: int) -> float | None:
        if len(self._history) < 3:
            return None
        ts = [h[0] for h in self._history]
        vals = [h[1] for h in self._history]
        mid = self._history[-1][2]
        if mid <= 0:
            return None
        mid_idx = len(ts) // 2
        v1 = _rate(ts[0], vals[0], ts[mid_idx], vals[mid_idx])
        v2 = _rate(ts[mid_idx], vals[mid_idx], ts[-1], vals[-1])
        if v1 is None or v2 is None:
            return None
        dt = (ts[-1] - ts[mid_idx]) / 1e9
        if dt <= 0:
            return None
        return (v2 - v1) / dt

    def direction_stability(self) -> float | None:
        """Доля последних отклонений с тем же знаком, что и последнее."""
        if len(self._deviations) < 3:
            return None
        last = self._deviations[-1]
        if last == 0:
            return 0.0
        same = sum(1 for d in self._deviations if d * last > 0)
        return same / len(self._deviations)


def _rate(t0: int, v0: float, t1: int, v1: float) -> float | None:
    dt = (t1 - t0) / 1e9
    if dt <= 0:
        return None
    return (v1 - v0) / dt
