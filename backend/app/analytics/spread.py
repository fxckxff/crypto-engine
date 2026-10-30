"""Спред и его статистика (ТЗ §5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.analytics.window import RollingWindow
from app.core.time import NS_MS
from app.market_data.orderbook import LocalOrderBook


@dataclass(frozen=True, slots=True)
class SpreadState:
    spread: float
    spread_pct: float
    spread_bps: float
    spread_ticks: float | None
    mid: float
    rolling_median: float | None
    percentile: float | None
    volatility: float | None
    time_weighted: float | None
    wide_duration_ms: float


def spread_pct(best_bid: Decimal, best_ask: Decimal, mid: Decimal) -> float:
    if mid <= 0:
        return 0.0
    return float((best_ask - best_bid) / mid * 100)


class SpreadTracker:
    def __init__(
        self,
        window_ms: float = 60_000.0,
        tick_size: Decimal | None = None,
        wide_multiplier: float = 2.0,
    ) -> None:
        self.window = RollingWindow(window_ms)
        self.tick_size = tick_size
        self.wide_multiplier = wide_multiplier
        self._wide_threshold: float | None = None

    def update(self, book: LocalOrderBook, now_ns: int) -> SpreadState | None:
        bid, ask = book.best_bid(), book.best_ask()
        if bid is None or ask is None:
            return None
        mid = (bid.price + ask.price) / 2
        if mid <= 0:
            return None

        spread = float(ask.price - bid.price)
        pct = spread_pct(bid.price, ask.price, mid)
        self.window.add(now_ns, spread)

        median = self.window.median(now_ns)
        if median is not None and median > 0:
            self._wide_threshold = median * self.wide_multiplier

        wide_duration = 0
        if self._wide_threshold is not None:
            wide_duration = self.window.duration_above(self._wide_threshold, now_ns)

        return SpreadState(
            spread=spread,
            spread_pct=pct,
            spread_bps=pct * 100.0,
            spread_ticks=spread / float(self.tick_size) if self.tick_size else None,
            mid=float(mid),
            rolling_median=median,
            percentile=self.window.percentile_rank(spread, now_ns),
            volatility=self.window.stdev(now_ns),
            time_weighted=self.window.time_weighted_mean(now_ns),
            wide_duration_ms=wide_duration / NS_MS,
        )
