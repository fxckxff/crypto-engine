"""Order Flow Imbalance (ТЗ §5.5).

Не путать с Order Book Imbalance: OBI — состояние стакана, OFI — поток его изменений.
Формула для верхнего уровня (Cont, Kukanov, Stoikov, 2014):

    e = I[pb >= pb_prev] * qb  -  I[pb <= pb_prev] * qb_prev
      - I[pa <= pa_prev] * qa  +  I[pa >= pa_prev] * qa_prev
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Side
from app.core.time import NS_MS
from app.market_data.orderbook import LocalOrderBook


@dataclass(frozen=True, slots=True)
class OfiState:
    top_of_book: float | None
    multi_level: float | None
    rolling: float | None
    normalized: float | None
    samples: int


class OrderFlowImbalance:
    def __init__(self, levels: int = 5, window_ms: float = 5_000.0) -> None:
        if levels <= 0:
            raise ValueError("levels must be positive")
        self.levels = levels
        self.window_ns = int(window_ms * NS_MS)
        self._prev_bids: list[tuple[Decimal, Decimal]] = []
        self._prev_asks: list[tuple[Decimal, Decimal]] = []
        self._events: deque[tuple[int, float]] = deque(maxlen=5_000)
        self._top_of_book: float | None = None
        self._multi_level: float | None = None

    def update(self, book: LocalOrderBook, now_ns: int) -> OfiState:
        bids = [(lv.price, lv.quantity) for lv in book.side_levels(Side.BID, self.levels)]
        asks = [(lv.price, lv.quantity) for lv in book.side_levels(Side.ASK, self.levels)]

        if self._prev_bids and self._prev_asks:
            self._top_of_book = self._level_ofi(self._prev_bids[0], bids[0], self._prev_asks[0], asks[0])
            total = 0.0
            for i in range(self.levels):
                prev_bid = self._prev_bids[i] if i < len(self._prev_bids) else (Decimal(0), Decimal(0))
                prev_ask = self._prev_asks[i] if i < len(self._prev_asks) else (Decimal(0), Decimal(0))
                cur_bid = bids[i] if i < len(bids) else (Decimal(0), Decimal(0))
                cur_ask = asks[i] if i < len(asks) else (Decimal(0), Decimal(0))
                total += self._level_ofi(prev_bid, cur_bid, prev_ask, cur_ask)
            self._multi_level = total
            if total != 0.0:
                self._events.append((now_ns, total))

        self._prev_bids = bids
        self._prev_asks = asks

        rolling = self._rolling(now_ns)
        return OfiState(
            top_of_book=self._top_of_book,
            multi_level=self._multi_level,
            rolling=rolling,
            normalized=self._normalized(rolling),
            samples=len(self._events),
        )

    @staticmethod
    def _level_ofi(
        prev_bid: tuple[Decimal, Decimal],
        cur_bid: tuple[Decimal, Decimal],
        prev_ask: tuple[Decimal, Decimal],
        cur_ask: tuple[Decimal, Decimal],
    ) -> float:
        pb, qb = prev_bid
        pb2, qb2 = cur_bid
        pa, qa = prev_ask
        pa2, qa2 = cur_ask

        bid_term = 0.0
        if pb2 >= pb:
            bid_term += float(qb2)
        if pb2 <= pb:
            bid_term -= float(qb)

        ask_term = 0.0
        if pa2 <= pa:
            ask_term -= float(qa2)
        if pa2 >= pa:
            ask_term += float(qa)

        return bid_term + ask_term

    def _rolling(self, now_ns: int) -> float | None:
        if not self._events:
            return None
        cutoff = now_ns - self.window_ns
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        if not self._events:
            return None
        return sum(v for _, v in self._events)

    def _normalized(self, rolling: float | None) -> float | None:
        if rolling is None or len(self._events) < 3:
            return None
        vals = [v for _, v in self._events]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        if var <= 0:
            return None
        return rolling / (var**0.5)
