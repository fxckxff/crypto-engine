"""Уровень локального стакана (ТЗ §4.3).

Поля, описывающие исполнение и отмену, имеют статус ESTIMATED: на агрегированном
L2 нельзя достоверно отделить снятие объёма от исполнения.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Confidence
from app.core.time import NS_MS


@dataclass(slots=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal
    first_seen_ns: int
    last_update_ns: int
    size_change_count: int = 0
    added_quantity: Decimal = Decimal(0)
    removed_quantity: Decimal = Decimal(0)
    estimated_executed_quantity: Decimal = Decimal(0)
    estimated_cancelled_quantity: Decimal = Decimal(0)
    lifetime_confidence: Confidence = Confidence.ESTIMATED

    @property
    def notional_usdt(self) -> Decimal:
        return self.price * self.quantity

    def observed_lifetime_ms(self, now_ns: int) -> float:
        return (now_ns - self.first_seen_ns) / NS_MS

    def update_quantity(
        self,
        new_quantity: Decimal,
        now_ns: int,
        executed_hint: Decimal = Decimal(0),
    ) -> None:
        """Применить новое значение объёма уровня.

        executed_hint — объём агрессивных сделок по этой цене, попавший в окно
        между предыдущим и текущим обновлением. Всё, что не объяснено сделками,
        считается отменой, но остаётся ESTIMATED.
        """
        if new_quantity == self.quantity:
            return

        self.size_change_count += 1
        self.last_update_ns = now_ns

        if new_quantity > self.quantity:
            self.added_quantity += new_quantity - self.quantity
        else:
            removed = self.quantity - new_quantity
            self.removed_quantity += removed
            executed = min(removed, max(executed_hint, Decimal(0)))
            self.estimated_executed_quantity += executed
            self.estimated_cancelled_quantity += removed - executed

        self.quantity = new_quantity

    def decayed_notional(self, now_ns: int) -> Decimal:
        """Номинал, уменьшенный на объём, который уже был съеден (оценка)."""
        remaining = self.quantity - self.estimated_executed_quantity
        if remaining <= 0:
            return Decimal(0)
        return self.price * remaining
