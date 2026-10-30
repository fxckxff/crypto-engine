"""Типы событий рыночных данных, общие для всех адаптеров."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Aggressor, Side
from app.core.time import EventMeta


@dataclass(frozen=True, slots=True)
class DepthDiff:
    """Инкрементальное обновление стакана с идентификаторами последовательности."""

    symbol: str
    first_update_id: int
    last_update_id: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    meta: EventMeta


@dataclass(frozen=True, slots=True)
class DepthSnapshot:
    symbol: str
    last_update_id: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    exchange_ts_ms: int | None
    receive_ns: int


@dataclass(frozen=True, slots=True)
class TradeTick:
    symbol: str
    trade_id: int | None
    price: Decimal
    quantity: Decimal
    aggressor: Aggressor
    exchange_ts_ms: int | None
    receive_ns: int
    meta: EventMeta

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    exchange_ts_ms: int | None
    receive_ns: int


@dataclass(frozen=True, slots=True)
class BookTicker:
    symbol: str
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    exchange_ts_ms: int | None
    receive_ns: int


@dataclass(frozen=True, slots=True)
class PrivateEvent:
    """Событие приватного потока: обновление ордера или исполнение."""

    kind: str
    symbol: str
    payload: dict[str, object]
    receive_ns: int


@dataclass(frozen=True, slots=True)
class Level:
    side: Side
    price: Decimal
    quantity: Decimal
