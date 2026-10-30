"""Пример стратегии: маркет-мейкинг на широком спреде (PHASE 5)."""

from __future__ import annotations

from decimal import Decimal

from app.core.enums import Side
from app.strategy.base import Signal, Strategy


class MarketMakingStrategy(Strategy):
    """Стратегия маркет-мейкинга: размещаем лимитные ордера на best bid/ask.

    Требования:
    - Спред >= min_spread_pct
    - Нет открытой позиции (или хотим нейтрализовать)
    - Размер позиции в рамках лимитов риска
    """

    name = "market_making"

    def __init__(
        self,
        symbol: str,
        min_spread_pct: float = 0.05,
        max_notional: Decimal = Decimal("500"),
        tick_size: Decimal = Decimal("0.01"),
    ) -> None:
        self.symbol = symbol
        self.min_spread_pct = min_spread_pct
        self.max_notional = max_notional
        self.tick_size = tick_size

    def on_snapshot(self, snapshot, book, position):
        """Генерируем сигнал на открытие/закрытие позиции."""

        if not book.ready:
            return None

        best_bid = book.best_bid()
        best_ask = book.best_ask()
        if not best_bid or not best_ask:
            return None

        spread_pct = (float(best_ask.price) - float(best_bid.price)) / float(best_bid.price) * 100
        if spread_pct < self.min_spread_pct:
            return None

        # Если уже есть позиция — закрываем
        if position and position.quantity > 0:
            return Signal(
                symbol=self.symbol,
                side=Side.ASK,
                intent="CLOSE",
                size=position.quantity,
                price=best_bid.price,
                metadata={"reason": "position_close"},
            )

        # Открываем позицию на бокупку у лучшего bid
        size = (self.max_notional / best_bid.price).quantize(Decimal("0.001"))
        if size <= 0:
            return None

        return Signal(
            symbol=self.symbol,
            side=Side.BID,
            intent="OPEN",
            size=size,
            price=best_bid.price,
            metadata={"reason": "market_making_entry", "spread_pct": spread_pct},
        )

    def on_private_update(self, event):
        """Обрабатываем события ордеров."""
        kind = event.kind
        if kind == "executionReport" and event.payload.get("status") in ("FILLED", "PARTIALLY_FILLED"):
            return Signal(
                symbol=self.symbol,
                side=Side[event.payload.get("S", "BID")],
                intent="FILLED",
                size=Decimal(str(event.payload.get("l", "0"))),
                metadata={"order_id": event.payload.get("orderId")},
            )
        return None
