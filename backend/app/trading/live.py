"""Live trading manager: kill switch, reconciliation, order lifecycle (PHASE 7)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.enums import OrderStatus, OrderType, Side
from app.exchanges.base import ExchangeAdapter, Order, OrderRequest
from app.execution.engine import ExecutionEngine
from app.storage.postgres import PostgresCatalog
from app.strategy.base import Position, RiskContext, Signal
from app.strategy.risk import RiskEngine

logger = logging.getLogger(__name__)

D = Decimal


@dataclass(frozen=True, slots=True)
class TradeIntent:
    """Намерение от стратегии, готовое к исполнению."""

    signal: Signal
    request: OrderRequest
    risk_approved: bool = False


@dataclass(slots=True)
class LiveState:
    """Состояние live-торговли для одного символа."""

    symbol: str
    position: Position | None = None
    open_orders: dict[str, Order] = field(default_factory=dict)  # exchange_order_id -> Order
    daily_pnl: Decimal = Decimal("0")
    daily_volume: Decimal = Decimal("0")
    daily_trades: int = 0
    last_update_ns: int = 0
    emergency_stop: bool = False


class LiveTradingManager:
    """Менеджер live-торговли: от сигнала до исполнения, контроль рисков, kill switch."""

    def __init__(
        self,
        adapter: ExchangeAdapter,
        catalog: PostgresCatalog | None,
        execution_engine: ExecutionEngine,
        risk_engine: RiskEngine,
        clock,
        max_daily_loss: Decimal,
        max_slippage_pct: Decimal,
    ) -> None:
        self.adapter = adapter
        self.catalog = catalog
        self.execution = execution_engine
        self.risk = risk_engine
        self.clock = clock
        self.max_daily_loss = max_daily_loss
        self.max_slippage_pct = max_slippage_pct

        self.states: dict[str, LiveState] = {}
        self._running = False
        self._reconcile_task: asyncio.Task | None = None
        self._risk_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self, symbols: list[str]) -> None:
        if self._running:
            return
        self._running = True
        # Инициализация состояний
        for symbol in symbols:
            self.states[symbol] = LiveState(symbol=symbol)
        # Загрузка открытых ордеров с биржи
        await self._reconcile_all()
        # Фоновые задачи
        self._reconcile_task = asyncio.create_task(self._reconcile_loop(), name="live-reconcile")
        self._risk_task = asyncio.create_task(self._risk_loop(), name="live-risk")
        logger.info("live trading started", extra={"extra_fields": {"symbols": symbols}})

    async def stop(self, emergency: bool = False) -> None:
        """Остановка live-торговли. emergency=True — отмена всех ордеров."""
        logger.warning("live trading stopping", extra={"extra_fields": {"emergency": emergency}})
        self._running = False
        if emergency:
            await self._emergency_close_all()
        for task in (self._reconcile_task, self._risk_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ------------------------------------------------------------------ signal handling

    async def on_signal(self, intent: TradeIntent) -> Order | None:
        """Обработка сигнала от стратегии: проверка рисков -> исполнение."""
        state = self.states.get(intent.signal.symbol)
        if not state:
            logger.warning("unknown symbol", extra={"extra_fields": {"symbol": intent.signal.symbol}})
            return None
        if state.emergency_stop:
            logger.warning("emergency stop active, rejecting signal")
            return None

        # 1. Риск-проверка
        if not self._check_risk(state, intent):
            return None

        # 2. Исполнение
        rules = await self.adapter.get_trading_rules(intent.signal.symbol)
        result = await self.execution.submit(intent.request, rules)

        if result.rejected:
            logger.warning(
                "order rejected",
                extra={"extra_fields": {"symbol": intent.signal.symbol, "reason": result.reason}},
            )
            return None

        order = result.order
        if not order:
            return None

        # 3. Сохранение
        if order.exchange_order_id:
            state.open_orders[order.exchange_order_id] = order
        if self.catalog:
            db_order_id = await self.catalog.record_order(order)
            if order.fills:
                await self.catalog.record_fills(db_order_id, order.fills)

        # 4. Обновление позиции
        self._update_position(state, order)

        return order

    def _check_risk(self, state: LiveState, intent: TradeIntent) -> bool:
        # Daily loss limit
        if state.daily_pnl <= -self.max_daily_loss:
            logger.error(
                "daily loss limit reached",
                extra={"extra_fields": {"symbol": state.symbol, "pnl": str(state.daily_pnl)}},
            )
            state.emergency_stop = True
            return False

        # RiskEngine approval
        context = RiskContext(
            positions={s: st.position for s, st in self.states.items() if st.position},
            total_exposure=sum((st.position.notional for st in self.states.values() if st.position), D("0")),
            max_exposure=self.risk.max_total_exposure,
            max_position=self.risk.max_position,
            balances_usdt=D("0"),
        )
        if not self.risk.approve(intent.signal, context):
            logger.warning("risk rejected signal", extra={"extra_fields": {"symbol": intent.signal.symbol}})
            return False

        return True

    def _update_position(self, state: LiveState, order: Order) -> None:
        if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
            # Упрощённо: считаем, что позиция обновилась
            for fill in order.fills:
                if state.position is None:
                    state.position = Position(
                        symbol=order.symbol,
                        side=order.side,
                        quantity=fill.quantity,
                        entry_price=fill.price,
                        entry_ns=self.clock.monotonic_ns(),
                        fills=[fill],
                    )
                else:
                    state.position.fills.append(fill)
                    state.position.quantity += fill.quantity if order.side == state.position.side else -fill.quantity

            state.daily_volume += sum((f.quantity * f.price for f in order.fills), D("0"))
            state.daily_trades += len(order.fills)

        if (
            order.status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)
            and order.exchange_order_id
        ):
            state.open_orders.pop(order.exchange_order_id, None)

    # ------------------------------------------------------------------ reconciliation

    async def _reconcile_all(self) -> None:
        for _symbol, state in self.states.items():
            await self._reconcile_symbol(state)

    async def _reconcile_symbol(self, state: LiveState) -> None:
        """Сверим открытые ордера с биржей."""
        try:
            remote_orders = await self.adapter.get_open_orders(state.symbol)
            remote_ids = {o.exchange_order_id for o in remote_orders}
            local_ids = set(state.open_orders.keys())

            # Ордера, которых нет на бирже — считаем закрытыми/отменёнными
            for missing_id in local_ids - remote_ids:
                order = state.open_orders.pop(missing_id, None)
                if order:
                    order.status = OrderStatus.CANCELED
                    logger.info(
                        "reconcile: order missing on exchange, marked CANCELED",
                        extra={"extra_fields": {"symbol": state.symbol, "order_id": missing_id}},
                    )

            # Обновляем статусы существующих
            for remote_order in remote_orders:
                exchange_oid = remote_order.exchange_order_id
                if not exchange_oid:
                    continue
                local_order = state.open_orders.get(exchange_oid)
                if local_order and local_order.status != remote_order.status:
                    local_order.status = remote_order.status
                    local_order.filled_quantity = remote_order.filled_quantity
                    local_order.average_price = remote_order.average_price
                    local_order.fills = remote_order.fills
                    if self.catalog and local_order.exchange_order_id:
                        await self.catalog.record_order(local_order)
                        if remote_order.fills:
                            order_db_id = await self.catalog.record_order(local_order)
                            await self.catalog.record_fills(order_db_id, remote_order.fills)

        except Exception:
            logger.exception("reconcile failed", extra={"extra_fields": {"symbol": state.symbol}})

    async def _reconcile_loop(self, interval_s: float = 10.0) -> None:
        try:
            while self._running:
                await asyncio.sleep(interval_s)
                if not self._running:
                    break
                await self._reconcile_all()
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ risk monitoring

    async def _risk_loop(self, interval_s: float = 5.0) -> None:
        try:
            while self._running:
                await asyncio.sleep(interval_s)
                if not self._running:
                    break
                await self._check_daily_limits()
        except asyncio.CancelledError:
            raise

    async def _check_daily_limits(self) -> None:
        for state in self.states.values():
            if state.daily_pnl <= -self.max_daily_loss and not state.emergency_stop:
                logger.critical(
                    "DAILY LOSS LIMIT EXCEEDED — EMERGENCY STOP",
                    extra={"extra_fields": {"symbol": state.symbol, "pnl": str(state.daily_pnl)}},
                )
                state.emergency_stop = True
                await self._emergency_close_symbol(state.symbol)

    # ------------------------------------------------------------------ emergency

    async def _emergency_close_all(self) -> None:
        for symbol in list(self.states.keys()):
            await self._emergency_close_symbol(symbol)

    async def _emergency_close_symbol(self, symbol: str) -> None:
        """Отмена всех ордеров + закрытие позиции по рынку."""
        state = self.states.get(symbol)
        if not state:
            return

        # 1. Отмена открытых ордеров
        for order_id in list(state.open_orders.keys()):
            try:
                await self.adapter.cancel_order(symbol, order_id=order_id)
                logger.warning(
                    "emergency: order cancelled",
                    extra={"extra_fields": {"symbol": symbol, "order_id": order_id}},
                )
            except Exception:
                logger.exception(
                    "emergency cancel failed",
                    extra={"extra_fields": {"symbol": symbol, "order_id": order_id}},
                )

        # 2. Закрытие позиции по рынку (если есть)
        if state.position and state.position.quantity > 0:
            try:
                close_request = OrderRequest(
                    symbol=symbol,
                    side=Side.ASK if state.position.side == Side.BID else Side.BID,
                    order_type=OrderType.MARKET,
                    quantity=state.position.quantity,
                )
                rules = await self.adapter.get_trading_rules(symbol)
                result = await self.execution.submit(close_request, rules)
                if result.order:
                    logger.critical(
                        "emergency: position closed",
                        extra={"extra_fields": {"symbol": symbol, "qty": str(result.order.quantity)}},
                    )
            except Exception:
                logger.exception("emergency close position failed", extra={"extra_fields": {"symbol": symbol}})
