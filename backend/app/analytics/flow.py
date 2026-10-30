"""Агрессивный поток по публичным сделкам (ТЗ §5.4).

Сторона агрессора берётся из поля биржи, если оно документировано.
Для Binance aggTrade: ``m`` (isBuyerMaker) == True означает, что агрессор — продавец.
Если поле отсутствует, сделка идёт в UNKNOWN и в торговые метрики не попадает.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Aggressor
from app.core.time import NS_MS

_SECONDS_NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    price: Decimal
    quantity: Decimal
    aggressor: Aggressor
    exchange_ts_ms: int | None
    receive_ns: int
    receive_mono_ns: int | None = None
    trade_id: int | None = None

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    @property
    def window_ts_ns(self) -> int:
        """Метка для оконных расчётов.

        Окно режется по монотонному времени (ТЗ §4.1): wall-clock может прыгнуть
        при синхронизации NTP, и тогда окно либо не обрежется, либо обрежется целиком.
        """
        return self.receive_ns if self.receive_mono_ns is None else self.receive_mono_ns


@dataclass(frozen=True, slots=True)
class FlowSnapshot:
    window_ms: float
    buy_volume: Decimal
    sell_volume: Decimal
    signed_delta: Decimal
    cvd: Decimal
    trade_count: int
    trade_frequency: float
    average_trade_size: Decimal
    large_trade_count: int
    large_trade_volume: Decimal
    aggression_ratio: float | None
    unknown_volume: Decimal
    classified_share: float
    large_trade_threshold: Decimal


class AggressiveFlow:
    def __init__(
        self,
        window_ms: float = 60_000.0,
        large_trade_multiplier: Decimal = Decimal(10),
        min_large_notional: Decimal = Decimal(0),
    ) -> None:
        self.window_ns = int(window_ms * NS_MS)
        self.large_trade_multiplier = large_trade_multiplier
        self.min_large_notional = min_large_notional
        self._trades: deque[Trade] = deque(maxlen=200_000)
        self._cvd = Decimal(0)

    def add(self, trade: Trade) -> None:
        self._trades.append(trade)
        if trade.aggressor is Aggressor.BUY:
            self._cvd += trade.quantity
        elif trade.aggressor is Aggressor.SELL:
            self._cvd -= trade.quantity

    @property
    def cvd(self) -> Decimal:
        return self._cvd

    def _prune(self, now_ns: int) -> None:
        cutoff = now_ns - self.window_ns
        while self._trades and self._trades[0].window_ts_ns < cutoff:
            self._trades.popleft()

    def snapshot(self, now_ns: int) -> FlowSnapshot:
        self._prune(now_ns)
        trades = list(self._trades)

        buy = sum((t.quantity for t in trades if t.aggressor is Aggressor.BUY), Decimal(0))
        sell = sum((t.quantity for t in trades if t.aggressor is Aggressor.SELL), Decimal(0))
        unknown = sum((t.quantity for t in trades if t.aggressor is Aggressor.UNKNOWN), Decimal(0))

        notional = sorted(t.notional for t in trades)
        median_notional = notional[len(notional) // 2] if notional else Decimal(0)
        threshold = max(median_notional * self.large_trade_multiplier, self.min_large_notional)

        large = [t for t in trades if t.notional >= threshold and threshold > 0]

        total_volume = buy + sell + unknown
        classified = buy + sell
        ratio: float | None = None
        if sell > 0:
            ratio = float(buy / sell)
        elif buy > 0:
            ratio = float("inf")

        window_seconds = self.window_ns / _SECONDS_NS
        count = len(trades)

        return FlowSnapshot(
            window_ms=self.window_ns / NS_MS,
            buy_volume=buy,
            sell_volume=sell,
            signed_delta=buy - sell,
            cvd=self._cvd,
            trade_count=count,
            trade_frequency=count / window_seconds if window_seconds > 0 else 0.0,
            average_trade_size=(total_volume / count) if count else Decimal(0),
            large_trade_count=len(large),
            large_trade_volume=sum((t.quantity for t in large), Decimal(0)),
            aggression_ratio=ratio,
            unknown_volume=unknown,
            classified_share=float(classified / total_volume) if total_volume > 0 else 0.0,
            large_trade_threshold=threshold,
        )
