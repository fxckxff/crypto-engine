"""Базовые интерфейсы для стратегий и риска."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.analytics.engine import FeatureSnapshot
from app.core.enums import Side
from app.exchanges.base import OrderFill


@dataclass(frozen=True, slots=True)
class Signal:
    """Сигнал стратегии для одного символа."""
    symbol: str
    side: Side
    intent: str  # "OPEN", "CLOSE", "ADJUST", "CANCEL"
    size: Decimal
    price: Decimal | None = None
    client_order_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    """Позиция по символу."""
    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal
    entry_ns: int
    fills: list[OrderFill] = field(default_factory=list)

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.entry_price

    @property
    def unrealized_pnl(self) -> Decimal:
        raise NotImplementedError("unrealized_pnl требует текущей цены")


@dataclass(slots=True)
class RiskContext:
    """Контекст риска для стратегии."""
    positions: dict[str, Position]
    total_exposure: Decimal
    max_exposure: Decimal
    max_position: Decimal
    balances_usdt: Decimal


class Strategy(ABC):
    """Базовый класс стратегии. PHASE 5."""

    name: str = "base"

    @abstractmethod
    def on_snapshot(self, snapshot: FeatureSnapshot, book_state: Any, position: Position | None) -> Signal | None:
        """Сгенерировать сигнал на основе признаков и состояния позиции."""
        ...

    @abstractmethod
    def on_private_update(self, event: Any) -> Signal | list[Signal] | None:
        """Обратный вызов на событие приватного потока."""
        ...


class RiskManager(ABC):
    """Базовый менеджер риска. PHASE 6."""

    @abstractmethod
    def approve(self, signal: Signal, context: RiskContext) -> bool:
        """Одобрить сигнал для исполнения."""
        ...
