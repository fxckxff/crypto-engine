"""Единый интерфейс биржи (ТЗ §3.1).

Адаптер отвечает только за транспорт и нормализацию данных. Он не хранит стакан,
не считает признаки и не принимает торговых решений.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

from app.core.enums import Aggressor, Confidence, MarketType, OrderStatus, OrderType, Side
from app.market_data.events import DepthDiff, DepthSnapshot, PrivateEvent, Quote, TradeTick


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    base_asset: str
    quote_asset: str
    market_type: MarketType
    status: str
    trading_allowed: bool


@dataclass(frozen=True, slots=True)
class TradingRules:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    min_notional: Decimal
    max_num_orders: int | None = None
    allowed_order_types: tuple[str, ...] = ("LIMIT", "MARKET", "LIMIT_MAKER")


@dataclass(frozen=True, slots=True)
class TradingFees:
    """Комиссии аккаунта. Если биржа не отдаёт их публично — source=config."""

    maker_bps: Decimal
    taker_bps: Decimal
    confidence: Confidence
    source: str


@dataclass(frozen=True, slots=True)
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None = None
    client_order_id: str | None = None
    time_in_force: str | None = "GTC"


@dataclass(frozen=True, slots=True)
class OrderFill:
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str
    trade_id: int | None
    exchange_ts_ms: int | None


@dataclass(slots=True)
class Order:
    symbol: str
    side: Side
    order_type: OrderType
    status: OrderStatus
    quantity: Decimal
    price: Decimal | None
    filled_quantity: Decimal = Decimal(0)
    average_price: Decimal = Decimal(0)
    fee_paid: Decimal = Decimal(0)
    exchange_order_id: str | None = None
    client_order_id: str | None = None
    exchange_ts_ms: int | None = None
    fills: list[OrderFill] = field(default_factory=list)

    @property
    def remaining_quantity(self) -> Decimal:
        return max(self.quantity - self.filled_quantity, Decimal(0))


class ExchangeAdapter(ABC):
    """Контракт адаптера. Все методы асинхронные, ошибки — app.core.errors."""

    name: str
    market_type: MarketType = MarketType.SPOT

    # --- жизненный цикл -----------------------------------------------------
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # --- справочники --------------------------------------------------------
    @abstractmethod
    async def get_instruments(self) -> dict[str, Instrument]: ...

    @abstractmethod
    async def get_trading_rules(self, symbol: str) -> TradingRules: ...

    @abstractmethod
    async def get_account_commissions(self) -> TradingFees: ...

    # --- рыночные данные ----------------------------------------------------
    @abstractmethod
    async def get_order_book_snapshot(self, symbol: str, limit: int = 200) -> DepthSnapshot: ...

    @abstractmethod
    def stream_order_book(self, symbol: str, depth: int = 200) -> AsyncIterator[DepthDiff]: ...

    @abstractmethod
    def stream_trades(self, symbol: str) -> AsyncIterator[TradeTick]: ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    async def get_24h_volumes(self, symbols: list[str]) -> dict[str, Decimal]:
        """Оборот за 24 часа в котируемой валюте.

        Нужен сканеру для фильтра минимального объёма. Адаптер, у которого такого
        эндпоинта нет, возвращает пустой словарь: сканер отметит объём как неизвестный.
        """
        return {}

    # --- приватный доступ ---------------------------------------------------
    @abstractmethod
    async def get_balances(self) -> list[Balance]: ...

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel_order(
        self,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> Order: ...

    @abstractmethod
    async def get_order(
        self,
        symbol: str,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> Order: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]: ...

    @abstractmethod
    async def get_my_trades(self, symbol: str, limit: int = 100) -> list[OrderFill]: ...

    @abstractmethod
    def stream_private(self) -> AsyncIterator[PrivateEvent]: ...


@dataclass(frozen=True, slots=True)
class AggressorClassification:
    """Результат классификации агрессора по полям биржи."""

    aggressor: Aggressor
    confidence: Confidence
