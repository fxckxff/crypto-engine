"""Адаптер Binance Spot: единственная полностью рабочая биржа в MVP (ТЗ §3.1)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

from app.core.enums import Confidence, MarketType, OrderStatus, Side
from app.core.errors import CredentialsMissing, ExchangeError, MalformedPayload
from app.core.time import Clock
from app.exchanges.base import (
    Balance,
    ExchangeAdapter,
    Instrument,
    Order,
    OrderFill,
    OrderRequest,
    TradingFees,
    TradingRules,
)
from app.exchanges.binance.protocol import (
    parse_24h_volume,
    parse_agg_trade,
    parse_book_ticker,
    parse_depth_snapshot,
    parse_depth_update,
    parse_instruments,
    parse_my_trade,
    parse_order,
    parse_trading_rules,
)
from app.exchanges.binance.rest import BinanceRestClient
from app.exchanges.binance.sign import Credentials
from app.exchanges.binance.ws import BinanceWsClient, PrivateStream, WsMessage
from app.market_data.events import DepthDiff, DepthSnapshot, PrivateEvent, Quote, TradeTick
from app.market_data.websocket_manager import BoundedEventQueue, ReconnectPolicy

logger = logging.getLogger(__name__)

D = Decimal


class BinanceSpotAdapter(ExchangeAdapter):
    name = "binance_spot"
    market_type = MarketType.SPOT

    def __init__(
        self,
        symbols: list[str],
        rest_base_url: str = "https://api.binance.com",
        ws_base_url: str = "wss://stream.binance.com:9443",
        depth: int = 200,
        credentials: Credentials | None = None,
        requests_per_minute: int = 600,
        recv_window_ms: int = 5000,
        timeout_s: float = 10.0,
        clock: Clock | None = None,
        policy: ReconnectPolicy | None = None,
        default_maker_bps: Decimal = D("10"),
        default_taker_bps: Decimal = D("10"),
        on_disconnect: Callable[[BaseException], None] | None = None,
    ) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.depth = depth
        self.clock = clock or Clock()
        self.credentials = credentials or Credentials.from_env()
        self.rest = BinanceRestClient(
            base_url=rest_base_url,
            credentials=self.credentials,
            requests_per_minute=requests_per_minute,
            recv_window_ms=recv_window_ms,
            timeout_s=timeout_s,
        )
        self._ws = BinanceWsClient(
            ws_base_url,
            self.symbols,
            policy=policy,
            clock=self.clock,
            on_disconnect=on_disconnect,
        )
        self._depth_queues: dict[str, BoundedEventQueue] = {s: BoundedEventQueue(20_000) for s in self.symbols}
        self._trade_queues: dict[str, BoundedEventQueue] = {s: BoundedEventQueue(50_000) for s in self.symbols}
        self._ticker_queues: dict[str, BoundedEventQueue] = {s: BoundedEventQueue(2_000) for s in self.symbols}
        self._pump_task: asyncio.Task[None] | None = None
        self._private_task: asyncio.Task[None] | None = None
        self._private_keepalive: asyncio.Task[None] | None = None
        self._private_queue: BoundedEventQueue = BoundedEventQueue(10_000)
        self._listen_key: str | None = None
        self._exchange_info_raw: dict[str, Any] | None = None
        self._instruments: dict[str, Instrument] = {}
        self._default_maker_bps = default_maker_bps
        self._default_taker_bps = default_taker_bps
        self.parse_errors = 0

    # ------------------------------------------------------------------ жизненный цикл

    async def connect(self) -> None:
        await self.rest.start()
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump_public(), name="binance-public-pump")
        logger.info(
            "adapter connected",
            extra={"extra_fields": {"exchange": self.name, "symbols": self.symbols, "depth": self.depth}},
        )

    async def close(self) -> None:
        for task in (self._pump_task, self._private_task, self._private_keepalive):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await self.rest.close()

    @property
    def connection_stats(self):
        return self._ws.connection_stats

    @property
    def market_data_loss(self) -> bool:
        return self.parse_errors > 0 or any(
            queue.dropped > 0 for queue in (*self._depth_queues.values(), *self._trade_queues.values())
        )

    def set_on_disconnect(self, cb: Callable[[BaseException], None] | None) -> None:
        """Set callback for public WebSocket disconnects.

        Called after adapter creation so the market service can invalidate
        synchronizers immediately on disconnect instead of waiting for a
        sequence gap to be detected.
        """
        self._ws.set_on_disconnect(cb)

    # ------------------------------------------------------------------ публичный поток

    async def _pump_public(self) -> None:
        try:
            async for message in self._ws.messages():
                self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("public pump crashed")

    def _dispatch(self, message: WsMessage) -> None:
        data = message.data
        try:
            if message.stream.endswith("aggTrade"):
                trade = parse_agg_trade(data, message.receive_ns, message.receive_mono_ns)
                self._put(self._trade_queues.get(trade.symbol), trade)
            elif "depth" in message.stream:
                diff = parse_depth_update(data, message.receive_ns, message.receive_mono_ns)
                self._put(self._depth_queues.get(diff.symbol), diff)
            elif message.stream.endswith("bookTicker"):
                ticker = parse_book_ticker(data, message.receive_ns)
                self._put(self._ticker_queues.get(ticker.symbol), ticker)
            else:
                self._ws.stats.unknown_streams += 1
        except MalformedPayload as exc:
            self.parse_errors += 1
            logger.warning(
                "malformed ws payload",
                extra={"extra_fields": {"stream": message.stream, "error": str(exc)}},
            )

    @staticmethod
    def _put(queue: BoundedEventQueue | None, item: object) -> None:
        if queue is None:
            return
        try:
            queue._queue.put_nowait(item)
        except asyncio.QueueFull:
            queue.dropped += 1

    async def stream_order_book(self, symbol: str, depth: int = 200) -> AsyncIterator[DepthDiff]:
        queue = self._depth_queues.get(symbol.upper())
        if queue is None:
            raise ValueError(f"{symbol} is not subscribed by this adapter")
        while True:
            yield await queue.get()  # type: ignore[misc]

    async def stream_trades(self, symbol: str) -> AsyncIterator[TradeTick]:
        queue = self._trade_queues.get(symbol.upper())
        if queue is None:
            raise ValueError(f"{symbol} is not subscribed by this adapter")
        while True:
            yield await queue.get()  # type: ignore[misc]

    async def stream_quotes(self, symbol: str) -> AsyncIterator[object]:
        queue = self._ticker_queues.get(symbol.upper())
        if queue is None:
            raise ValueError(f"{symbol} is not subscribed by this adapter")
        while True:
            yield await queue.get()

    # ------------------------------------------------------------------ справочники

    async def get_instruments(self) -> dict[str, Instrument]:
        payload = await self.rest.exchange_info(self.symbols)
        self._exchange_info_raw = payload
        self._instruments = parse_instruments(payload)
        missing = [s for s in self.symbols if s not in self._instruments]
        if missing:
            raise ExchangeError(None, f"symbols not found on exchange: {missing}")
        return {s: self._instruments[s] for s in self.symbols}

    async def get_trading_rules(self, symbol: str) -> TradingRules:
        symbol = symbol.upper()
        if self._exchange_info_raw is None:
            await self.get_instruments()
        assert self._exchange_info_raw is not None
        entries: list[dict[str, Any]] = self._exchange_info_raw.get("symbols", [])
        for item in entries:
            if str(item.get("symbol", "")).upper() == symbol:
                return parse_trading_rules(item)
        raise ExchangeError(None, f"symbol {symbol} not found in exchangeInfo")

    async def get_account_commissions(self) -> TradingFees:
        """Spot-комиссии отдаёт приватный /api/v3/account (в единицах 1e-4 = bps)."""
        if not self.credentials.available:
            return TradingFees(
                maker_bps=self._default_maker_bps,
                taker_bps=self._default_taker_bps,
                confidence=Confidence.CONFIGURED,
                source="config:default (приватный API недоступен без ключей)",
            )
        account = await self.rest.account()
        maker = D(str(account.get("makerCommission", self._default_maker_bps)))
        taker = D(str(account.get("takerCommission", self._default_taker_bps)))
        return TradingFees(
            maker_bps=maker,
            taker_bps=taker,
            confidence=Confidence.VERIFIED,
            source="binance:/api/v3/account",
        )

    # ------------------------------------------------------------------ рыночные данные

    async def get_order_book_snapshot(self, symbol: str, limit: int = 200) -> DepthSnapshot:
        # REST принимает только 5/10/20/50/100/500/1000/5000: берём ближайший больший, локально обрезаем.
        payload = await self.rest.depth(symbol.upper(), rest_depth_limit(limit))
        return parse_depth_snapshot(payload, symbol.upper(), self.clock.wall_ns())

    async def get_quote(self, symbol: str) -> Quote:
        payload = await self.rest.book_ticker(symbol.upper())
        ticker = parse_book_ticker(payload, self.clock.wall_ns())
        return Quote(
            symbol=ticker.symbol,
            bid_price=ticker.bid_price,
            bid_quantity=ticker.bid_quantity,
            ask_price=ticker.ask_price,
            ask_quantity=ticker.ask_quantity,
            exchange_ts_ms=ticker.exchange_ts_ms,
            receive_ns=ticker.receive_ns,
        )

    async def get_24h_volumes(self, symbols: list[str]) -> dict[str, Decimal]:
        """Оборот за 24 часа (quoteVolume). Один запрос до 20 символов, вес 2."""
        result: dict[str, Decimal] = {}
        for start in range(0, len(symbols), 20):
            chunk = [s.upper() for s in symbols[start : start + 20]]
            result.update(parse_24h_volume(await self.rest.ticker_24hr(chunk)))
        return result

    # ------------------------------------------------------------------ приватный доступ

    async def get_balances(self) -> list[Balance]:
        account = await self.rest.account()
        balances: list[Balance] = []
        for item in account.get("balances", []):
            free = D(str(item.get("free", "0")))
            locked = D(str(item.get("locked", "0")))
            if free == 0 and locked == 0:
                continue
            balances.append(Balance(asset=str(item.get("asset", "")), free=free, locked=locked))
        return balances

    async def place_order(self, request: OrderRequest) -> Order:
        if request.order_type.value == "MARKET" and request.price is not None:
            raise ValueError("market order must not carry price")
        params = {
            "symbol": request.symbol.upper(),
            "side": "BUY" if request.side is Side.BID else "SELL",
            "type": request.order_type.value,
            "quantity": _fmt(request.quantity),
            "newOrderRespType": "FULL",
        }
        if request.price is not None:
            params["price"] = _fmt(request.price)
        if request.time_in_force and request.order_type.value == "LIMIT":
            params["timeInForce"] = request.time_in_force
        if request.client_order_id:
            params["newClientOrderId"] = request.client_order_id
        payload = await self.rest.place_order(params)
        return parse_order(payload, request.symbol)

    async def cancel_order(
        self, symbol: str, order_id: str | None = None, client_order_id: str | None = None
    ) -> Order:
        params: dict[str, object] = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        payload = await self.rest.cancel_order(params)
        return parse_order(payload, symbol)

    async def get_order(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> Order:
        params: dict[str, object] = {"symbol": symbol.upper()}
        if order_id:
            params["orderId"] = order_id
        elif client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            raise ValueError("order_id or client_order_id is required")
        payload = await self.rest.get_order(params)
        return parse_order(payload, symbol)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        payload = await self.rest.open_orders(symbol.upper() if symbol else None)
        return [parse_order(item) for item in payload]

    async def get_my_trades(self, symbol: str, limit: int = 100) -> list[OrderFill]:
        payload = await self.rest.my_trades(symbol.upper(), limit)
        return [parse_my_trade(item) for item in payload]

    async def stream_private(self) -> AsyncIterator[PrivateEvent]:
        if not self.credentials.available:
            raise CredentialsMissing("private stream requires BINANCE_API_KEY/SECRET")
        self._listen_key = await self.rest.create_listen_key()
        stream = PrivateStream(self._ws.base_url, self._listen_key)
        async for payload in stream.messages():
            kind = str(payload.get("e", "unknown"))
            symbol = str(payload.get("s", ""))
            yield PrivateEvent(
                kind=kind,
                symbol=symbol,
                payload=payload,
                receive_ns=self.clock.wall_ns(),
            )


def rest_depth_limit(limit: int) -> int:
    for allowed in (5, 10, 20, 50, 100, 500, 1000, 5000):
        if limit <= allowed:
            return allowed
    return 5000


def _fmt(value: Decimal) -> str:
    """Decimal → строка без экспоненты: биржа не принимает '1E+1'."""
    return format(value.normalize(), "f")


__all__ = ["BinanceSpotAdapter", "OrderStatus"]
