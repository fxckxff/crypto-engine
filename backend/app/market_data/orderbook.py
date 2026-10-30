"""Локальная копия стакана с валидацией последовательности (ТЗ §4.2).

Инварианты:
    * торговые признаки запрещены, пока state != READY;
    * разрыв последовательности переводит стакан в INVALID до пересинхронизации;
    * уровни с нулевым объёмом удаляются;
    * лучший bid всегда строго меньше лучшего ask.
"""

from __future__ import annotations

from bisect import bisect_left, insort
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import BookIntegrity, Side
from app.core.errors import BookNotReady, SequenceGap, SnapshotRequired
from app.core.time import NS_MS, Clock
from app.market_data.level import OrderBookLevel

LevelTuple = tuple[Decimal, Decimal]


@dataclass(frozen=True, slots=True)
class DiffResult:
    applied: bool
    reason: str
    skipped_levels: int = 0


class LocalOrderBook:
    def __init__(
        self,
        symbol: str,
        depth: int = 200,
        max_levels: int = 200,
        stale_after_ms: float = 2000.0,
        clock: Clock | None = None,
    ) -> None:
        if depth <= 0:
            raise ValueError("depth must be positive")
        self.symbol = symbol
        self.depth = depth
        self.max_levels = max_levels
        self.stale_after_ms = stale_after_ms
        self.clock = clock or Clock()

        self.bids: dict[Decimal, OrderBookLevel] = {}
        self.asks: dict[Decimal, OrderBookLevel] = {}
        self._bid_prices: list[Decimal] = []
        self._ask_prices: list[Decimal] = []

        self.last_update_id: int | None = None
        self._awaiting_first_diff = False
        self.last_update_ns: int = 0
        self.last_exchange_ts_ms: int | None = None
        self.state: BookIntegrity = BookIntegrity.EMPTY
        self.reason: str | None = None
        self.resync_count: int = 0
        self.gap_count: int = 0

    # ------------------------------------------------------------------ состояние

    @property
    def ready(self) -> bool:
        return self.state is BookIntegrity.READY

    @property
    def levels_loaded(self) -> int:
        return len(self._bid_prices) + len(self._ask_prices)

    def require_ready(self) -> None:
        if not self.ready:
            raise BookNotReady(f"{self.symbol}: book state={self.state.value} reason={self.reason}")

    def age_ms(self, now_ns: int | None = None) -> float:
        now = self.clock.monotonic_ns() if now_ns is None else now_ns
        return (now - self.last_update_ns) / NS_MS

    def refresh_state(self, now_ns: int | None = None) -> BookIntegrity:
        """Пометить стакан STALE, если обновлений давно не было (ТЗ §4.1 п.6)."""
        if self.state is BookIntegrity.READY and self.age_ms(now_ns) > self.stale_after_ms:
            self.state = BookIntegrity.STALE
            self.reason = f"no updates for {self.age_ms(now_ns):.0f} ms"
        elif self.state is BookIntegrity.STALE and self.age_ms(now_ns) <= self.stale_after_ms:
            self.state = BookIntegrity.READY
            self.reason = None
        return self.state

    def mark_invalid(self, reason: str) -> None:
        self.state = BookIntegrity.INVALID
        self.reason = reason

    def invalidate(self, reason: str) -> None:
        """Полный сброс после нарушения целостности или разрыва соединения."""
        self.mark_invalid(reason)
        self.bids.clear()
        self.asks.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()
        self.last_update_id = None
        self.resync_count += 1

    # ------------------------------------------------------------------ снапшот

    def apply_snapshot(
        self,
        bids: Sequence[LevelTuple],
        asks: Sequence[LevelTuple],
        last_update_id: int,
        now_ns: int,
        exchange_ts_ms: int | None = None,
    ) -> None:
        self.bids.clear()
        self.asks.clear()
        self._bid_prices.clear()
        self._ask_prices.clear()

        for price, qty in bids:
            if qty > 0:
                self.bids[price] = OrderBookLevel(price, qty, now_ns, now_ns)
        for price, qty in asks:
            if qty > 0:
                self.asks[price] = OrderBookLevel(price, qty, now_ns, now_ns)

        self._bid_prices = sorted(self.bids)
        self._ask_prices = sorted(self.asks)
        self._trim()

        self.last_update_id = last_update_id
        self.last_update_ns = now_ns
        self.last_exchange_ts_ms = exchange_ts_ms
        # После снимка первая дельта может перекрывать его: правило Binance
        # U <= lastUpdateId + 1 <= u, а не строгая непрерывность.
        self._awaiting_first_diff = True
        self.state = BookIntegrity.READY
        self.reason = None

        if not self.bids or not self.asks:
            self.invalidate("snapshot has an empty side")
        elif self.is_crossed():
            self.invalidate("snapshot produced crossed book")

    # ------------------------------------------------------------------ дельты

    def apply_diff(
        self,
        first_update_id: int,
        last_update_id: int,
        bids: Sequence[LevelTuple],
        asks: Sequence[LevelTuple],
        now_ns: int,
        exchange_ts_ms: int | None = None,
        executed_by_price: dict[Decimal, Decimal] | None = None,
    ) -> DiffResult:
        if self.last_update_id is None or self.state in (
            BookIntegrity.EMPTY,
            BookIntegrity.SYNCING,
            BookIntegrity.INVALID,
        ):
            raise SnapshotRequired(f"{self.symbol}: diff without snapshot")

        if last_update_id <= self.last_update_id:
            return DiffResult(False, "already_applied")

        expected = self.last_update_id + 1
        if self._awaiting_first_diff:
            if not (first_update_id <= expected <= last_update_id):
                self.gap_count += 1
                self.mark_invalid(
                    f"diff {first_update_id}..{last_update_id} does not cover snapshot id {self.last_update_id}"
                )
                raise SequenceGap(expected, first_update_id)
            self._awaiting_first_diff = False
        elif first_update_id > expected:
            self.gap_count += 1
            self.mark_invalid(f"sequence gap: expected U={expected}, got U={first_update_id}")
            raise SequenceGap(expected, first_update_id)

        skipped = 0
        for price, qty in bids:
            self._apply_level(Side.BID, price, qty, now_ns, executed_by_price)
        for price, qty in asks:
            self._apply_level(Side.ASK, price, qty, now_ns, executed_by_price)

        self.last_update_id = last_update_id
        self.last_update_ns = now_ns
        if exchange_ts_ms is not None:
            self.last_exchange_ts_ms = exchange_ts_ms
        self._trim()

        if not self.bids or not self.asks:
            self.mark_invalid("empty side after diff")
        elif self.is_crossed():
            self.mark_invalid("crossed book after diff")

        if self.state is BookIntegrity.STALE:
            self.state = BookIntegrity.READY
            self.reason = None

        return DiffResult(True, "ok", skipped)

    def _apply_level(
        self,
        side: Side,
        price: Decimal,
        qty: Decimal,
        now_ns: int,
        executed_by_price: dict[Decimal, Decimal] | None,
    ) -> None:
        book = self.bids if side is Side.BID else self.asks
        prices = self._bid_prices if side is Side.BID else self._ask_prices
        level = book.get(price)

        if qty <= 0:
            if level is not None:
                del book[price]
                idx = bisect_left(prices, price)
                if idx < len(prices) and prices[idx] == price:
                    prices.pop(idx)
            return

        if level is None:
            book[price] = OrderBookLevel(price, qty, now_ns, now_ns, added_quantity=qty)
            insort(prices, price)
            return

        hint = Decimal(0)
        if executed_by_price:
            hint = executed_by_price.get(price, Decimal(0))
        level.update_quantity(qty, now_ns, hint)

    # ------------------------------------------------------------------ обслуживание

    def _trim(self) -> None:
        """Держать только верхние max_levels уровней по каждой стороне."""
        while len(self._bid_prices) > self.max_levels:
            price = self._bid_prices.pop(0)
            self.bids.pop(price, None)
        while len(self._ask_prices) > self.max_levels:
            price = self._ask_prices.pop()
            self.asks.pop(price, None)

    def _update_time_now(self) -> int:
        return self.clock.monotonic_ns()

    # ------------------------------------------------------------------ чтение

    def best_bid(self) -> OrderBookLevel | None:
        if not self._bid_prices:
            return None
        return self.bids[self._bid_prices[-1]]

    def best_ask(self) -> OrderBookLevel | None:
        if not self._ask_prices:
            return None
        return self.asks[self._ask_prices[0]]

    def mid_price(self) -> Decimal | None:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return (bid.price + ask.price) / 2

    def spread(self) -> Decimal | None:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return None
        return ask.price - bid.price

    def is_crossed(self) -> bool:
        bid, ask = self.best_bid(), self.best_ask()
        if bid is None or ask is None:
            return False
        return bid.price >= ask.price

    def side_levels(self, side: Side, n: int | None = None) -> list[OrderBookLevel]:
        """Уровни от лучшей цены вглубь стакана."""
        if side is Side.BID:
            prices: Iterable[Decimal] = reversed(self._bid_prices)
            book = self.bids
        else:
            prices = iter(self._ask_prices)
            book = self.asks
        out: list[OrderBookLevel] = []
        for i, price in enumerate(prices):
            if n is not None and i >= n:
                break
            out.append(book[price])
        return out

    def level_at(self, side: Side, price: Decimal) -> OrderBookLevel | None:
        return (self.bids if side is Side.BID else self.asks).get(price)

    def total_quantity(self, side: Side, n: int | None = None) -> Decimal:
        return sum((lv.quantity for lv in self.side_levels(side, n)), Decimal(0))

    def total_notional(self, side: Side, n: int | None = None) -> Decimal:
        return sum((lv.notional_usdt for lv in self.side_levels(side, n)), Decimal(0))

    def snapshot_levels(self, n: int | None = None) -> tuple[list[LevelTuple], list[LevelTuple]]:
        bids = [(lv.price, lv.quantity) for lv in self.side_levels(Side.BID, n)]
        asks = [(lv.price, lv.quantity) for lv in self.side_levels(Side.ASK, n)]
        return bids, asks

    def validate(self) -> list[str]:
        """Проверка отсортированности и пересечения. Возвращает список проблем."""
        problems: list[str] = []
        if self._bid_prices != sorted(self._bid_prices):
            problems.append("bid prices not ascending")
        if self._ask_prices != sorted(self._ask_prices):
            problems.append("ask prices not ascending")
        if set(self._bid_prices) != set(self.bids):
            problems.append("bid price index out of sync with levels")
        if set(self._ask_prices) != set(self.asks):
            problems.append("ask price index out of sync with levels")
        for price in self._bid_prices:
            lv = self.bids.get(price)
            if lv is None or lv.quantity <= 0:
                problems.append(f"invalid bid level at {price}")
        for price in self._ask_prices:
            lv = self.asks.get(price)
            if lv is None or lv.quantity <= 0:
                problems.append(f"invalid ask level at {price}")
        if self.is_crossed():
            problems.append("crossed book")
        return problems

    def __repr__(self) -> str:  # pragma: no cover - отладочное представление
        bid, ask = self.best_bid(), self.best_ask()
        return (
            f"LocalOrderBook({self.symbol}, state={self.state.value}, "
            f"bid={bid.price if bid else None}, ask={ask.price if ask else None}, "
            f"last_id={self.last_update_id})"
        )
