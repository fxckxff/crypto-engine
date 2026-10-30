"""Исполнение ордеров: smart routing, TWAP, iceberg (PHASE 6)."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal

from app.exchanges.base import Order, OrderFill, OrderRequest, TradingRules

logger = logging.getLogger(__name__)

D = Decimal


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Результат исполнения ордера."""
    order: Order | None = None
    fills: list[OrderFill] = field(default_factory=list)
    rejected: bool = False
    reason: str = ""
    latency_ms: float = 0.0


class ExecutionEngine(ABC):
    """Базовый движок исполнения."""

    @abstractmethod
    async def submit(self, request: OrderRequest, rules: TradingRules) -> ExecutionResult:
        ...


class DirectExecution(ExecutionEngine):
    """Прямое исполнение через адаптер (один ордер)."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter

    async def submit(self, request: OrderRequest, rules: TradingRules) -> ExecutionResult:
        start = time.monotonic_ns()
        try:
            order = await self.adapter.place_order(request)
            return ExecutionResult(
                order=order,
                latency_ms=(time.monotonic_ns() - start) / 1e6,
            )
        except Exception as exc:
            logger.warning("order rejected", extra={"extra_fields": {"symbol": request.symbol, "error": str(exc)}})
            return ExecutionResult(rejected=True, reason=str(exc))


class TwapExecution(ExecutionEngine):
    """TWAP: разбивает ордер на части по времени (для крупных объёмов)."""

    def __init__(
        self,
        adapter,
        slices: int = 4,
        interval_s: float = 30.0,
    ) -> None:
        self.adapter = adapter
        self.slices = max(1, slices)
        self.interval_s = interval_s

    async def submit(self, request: OrderRequest, rules: TradingRules) -> ExecutionResult:
        start = time.monotonic_ns()
        total_quantity = request.quantity
        slice_qty = (total_quantity / self.slices).quantize(rules.step_size)
        fills: list[OrderFill] = []
        order: Order | None = None

        for i in range(self.slices):
            if i == self.slices - 1:
                # Последний слой — остаток
                remaining = total_quantity - sum((f.quantity for f in fills), D("0"))
                slice_qty = remaining
            if slice_qty <= 0:
                break

            sub_request = OrderRequest(
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                quantity=slice_qty,
                price=request.price,
                time_in_force=request.time_in_force,
                client_order_id=f"{request.client_order_id or 'twap'}-{i}",
            )
            result = await DirectExecution(self.adapter).submit(sub_request, rules)
            if result.order:
                order = result.order
                fills.extend(result.order.fills)
            await asyncio.sleep(self.interval_s)

        return ExecutionResult(
            order=order,
            fills=fills,
            latency_ms=(time.monotonic_ns() - start) / 1e6,
        )


class IcebergExecution(ExecutionEngine):
    """Iceberg: показывает только часть ордера, остальное скрыто."""

    def __init__(
        self,
        adapter,
        display_qty_ratio: float = 0.25,
        refresh_ms: float = 500.0,
    ) -> None:
        self.adapter = adapter
        self.display_qty_ratio = display_qty_ratio
        self.refresh_ms = refresh_ms

    async def submit(self, request: OrderRequest, rules: TradingRules) -> ExecutionResult:
        start = time.monotonic_ns()
        total_quantity = request.quantity
        remaining = total_quantity
        fills: list[OrderFill] = []
        order: Order | None = None

        while remaining > 0:
            display_qty = (remaining * D(str(self.display_qty_ratio))).quantize(rules.step_size)
            if display_qty > remaining:
                display_qty = remaining

            sub_request = OrderRequest(
                symbol=request.symbol,
                side=request.side,
                order_type=request.order_type,
                quantity=display_qty,
                price=request.price,
                time_in_force=request.time_in_force,
                client_order_id=f"{request.client_order_id or 'ice'}-{int(time.time() * 1000)}",
            )
            result = await DirectExecution(self.adapter).submit(sub_request, rules)
            if result.order:
                order = result.order
                fills.extend(result.order.fills)
            remaining -= display_qty
            await asyncio.sleep(self.refresh_ms / 1000.0)

        return ExecutionResult(
            order=order,
            fills=fills,
            latency_ms=(time.monotonic_ns() - start) / 1e6,
        )


def choose_execution(
    adapter,
    request: OrderRequest,
    rules: TradingRules,
    min_qty_for_twap: Decimal = D("10"),
) -> ExecutionEngine:
    """Выбирает тип исполнения по объёму."""
    if request.quantity >= min_qty_for_twap:
        return TwapExecution(adapter, slices=4, interval_s=30.0)
    return DirectExecution(adapter)