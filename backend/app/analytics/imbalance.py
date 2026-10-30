"""Дисбаланс стакана OBI (ТЗ §5.2).

При нулевом знаменателе возвращается None — это UNDEFINED, значение нельзя
использовать как подтверждённый сигнал.
"""

from __future__ import annotations

import math
from decimal import Decimal

from app.core.enums import Side
from app.market_data.orderbook import LocalOrderBook

DEFAULT_DEPTHS = (1, 5, 10, 20, 50)


def order_book_imbalance(book: LocalOrderBook, n: int) -> float | None:
    if n <= 0:
        raise ValueError("n must be positive")
    bid = book.total_quantity(Side.BID, n)
    ask = book.total_quantity(Side.ASK, n)
    denom = bid + ask
    if denom <= 0:
        return None
    return float((bid - ask) / denom)


def imbalance_at_levels(bids: list[Decimal], asks: list[Decimal]) -> float | None:
    denom = sum(bids) + sum(asks)
    if denom <= 0:
        return None
    return float((sum(bids) - sum(asks)) / denom)


def distance_weighted_imbalance(
    book: LocalOrderBook,
    n: int = 10,
    half_life_pct: float = 0.05,
) -> float | None:
    """OBI с весом exp(-ln2 * dist/half_life), где dist — расстояние от mid в процентах."""
    mid = book.mid_price()
    if mid is None or mid <= 0:
        return None
    decay = math.log(2) / half_life_pct if half_life_pct > 0 else 0.0

    weighted_bid = 0.0
    weighted_ask = 0.0
    for side in (Side.BID, Side.ASK):
        for level in book.side_levels(side, n):
            dist_pct = abs(float(level.price - mid)) / float(mid) * 100.0
            weight = math.exp(-decay * dist_pct)
            notional = float(level.notional_usdt)
            if side is Side.BID:
                weighted_bid += notional * weight
            else:
                weighted_ask += notional * weight

    denom = weighted_bid + weighted_ask
    if denom <= 0:
        return None
    return (weighted_bid - weighted_ask) / denom


def imbalance_vector(
    book: LocalOrderBook,
    depths: tuple[int, ...] = DEFAULT_DEPTHS,
) -> dict[int, float | None]:
    return {n: order_book_imbalance(book, n) for n in depths}
