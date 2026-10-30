"""Менеджер рисков (PHASE 6)."""

from __future__ import annotations

from decimal import Decimal

from app.strategy.base import RiskContext, Signal


class RiskEngine:
    """Менеджер рисков: проверяет лимиты перед отправкой ордеров."""

    def __init__(
        self,
        max_position_usdt: float = 50.0,
        max_total_exposure_usdt: float = 100.0,
        max_open_positions: int = 2,
    ) -> None:
        self.max_position = Decimal(str(max_position_usdt))
        self.max_total_exposure = Decimal(str(max_total_exposure_usdt))
        self.max_open_positions = max_open_positions

    def approve(self, signal: Signal, context: RiskContext) -> bool:
        """Проверяет одобрение сигнала."""
        if signal.intent == "CLOSE":
            return True  # Закрытие всегда разрешено

        # 1. Лимит количества позиций
        if len(context.positions) >= self.max_open_positions and signal.symbol not in context.positions:
            return False

        # 2. Лимит на нотионал одной позиции
        signal_price = signal.price or Decimal("1")
        signal_notional = signal.size * signal_price
        current_pos = context.positions.get(signal.symbol)
        current_notional = current_pos.notional if current_pos else Decimal("0")

        if current_notional + signal_notional > self.max_position:
            return False

        # 3. Лимит на общую экспозицию
        if context.total_exposure + signal_notional > self.max_total_exposure:
            return False

        return True
