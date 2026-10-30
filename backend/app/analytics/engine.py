"""Feature Engine: сводка признаков инструмента с гейтом по целостности данных.

Правило ТЗ §4.2: пока стакан не READY и не свежий, признаки не считаются,
а снимок помечается tradable=False с указанием причины.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.analytics.flow import AggressiveFlow, FlowSnapshot, Trade
from app.analytics.imbalance import DEFAULT_DEPTHS, distance_weighted_imbalance, imbalance_vector
from app.analytics.microprice import MicropriceState, MicropriceTracker
from app.analytics.order_flow import OfiState, OrderFlowImbalance
from app.analytics.spread import SpreadState, SpreadTracker
from app.core.enums import BookIntegrity
from app.market_data.orderbook import LocalOrderBook


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    symbol: str
    now_ns: int
    book_state: BookIntegrity
    book_age_ms: float
    tradable: bool
    block_reason: str | None
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    spread: SpreadState | None
    obi: dict[int, float | None]
    obi_weighted: float | None
    microprice: MicropriceState | None
    ofi: OfiState | None
    flow: FlowSnapshot


class FeatureEngine:
    def __init__(
        self,
        symbol: str,
        tick_size: Decimal | None = None,
        spread_window_ms: float = 60_000.0,
        flow_window_ms: float = 60_000.0,
        ofi_levels: int = 5,
        ofi_window_ms: float = 5_000.0,
        large_trade_multiplier: Decimal = Decimal(10),
    ) -> None:
        self.symbol = symbol
        self.spread = SpreadTracker(window_ms=spread_window_ms, tick_size=tick_size)
        self.flow = AggressiveFlow(window_ms=flow_window_ms, large_trade_multiplier=large_trade_multiplier)
        self.ofi = OrderFlowImbalance(levels=ofi_levels, window_ms=ofi_window_ms)
        self.microprice = MicropriceTracker()
        self._blocked_snapshots = 0

    @property
    def blocked_snapshots(self) -> int:
        return self._blocked_snapshots

    def on_trade(self, trade: Trade) -> None:
        self.flow.add(trade)

    def update(self, book: LocalOrderBook, now_ns: int) -> FeatureSnapshot:
        state = book.refresh_state(now_ns)
        age = book.age_ms(now_ns)
        flow = self.flow.snapshot(now_ns)

        if state is not BookIntegrity.READY:
            self._blocked_snapshots += 1
            return FeatureSnapshot(
                symbol=self.symbol,
                now_ns=now_ns,
                book_state=state,
                book_age_ms=age,
                tradable=False,
                block_reason=book.reason or f"book state {state.value}",
                best_bid=None,
                best_ask=None,
                mid=None,
                spread=None,
                obi={n: None for n in DEFAULT_DEPTHS},
                obi_weighted=None,
                microprice=None,
                ofi=None,
                flow=flow,
            )

        spread_state = self.spread.update(book, now_ns)
        if spread_state is None:
            self._blocked_snapshots += 1
            return FeatureSnapshot(
                symbol=self.symbol,
                now_ns=now_ns,
                book_state=state,
                book_age_ms=age,
                tradable=False,
                block_reason="no best bid/ask",
                best_bid=None,
                best_ask=None,
                mid=None,
                spread=None,
                obi={n: None for n in DEFAULT_DEPTHS},
                obi_weighted=None,
                microprice=None,
                ofi=None,
                flow=flow,
            )

        bid, ask = book.best_bid(), book.best_ask()
        assert bid is not None and ask is not None

        return FeatureSnapshot(
            symbol=self.symbol,
            now_ns=now_ns,
            book_state=state,
            book_age_ms=age,
            tradable=True,
            block_reason=None,
            best_bid=bid.price,
            best_ask=ask.price,
            mid=book.mid_price(),
            spread=spread_state,
            obi=imbalance_vector(book),
            obi_weighted=distance_weighted_imbalance(book, n=10),
            microprice=self.microprice.update(book, now_ns),
            ofi=self.ofi.update(book, now_ns),
            flow=flow,
        )


__all__ = ["FeatureEngine", "FeatureSnapshot", "FlowSnapshot", "MicropriceState", "OfiState", "SpreadState"]
