"""Приватные эндпоинты Binance Spot: аккаунт, ордера, сделки, userDataStream."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any

import aiohttp

from app.exchanges.binance.rest import BinanceRestClient
from app.exchanges.binance.sign import sign_query

logger = logging.getLogger(__name__)


class BinancePrivateClient:
    """Клиент для приватных REST эндпоинтов (требует API ключи)."""

    def __init__(
        self,
        rest: BinanceRestClient,
        api_key: str,
        api_secret: str,
    ) -> None:
        self.rest = rest
        self.api_key = api_key
        self.api_secret = api_secret

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Выполняет подписанный запрос с timestamp и signature."""
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        query_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        params["signature"] = sign_query(query_string, self.api_secret)
        return await self.rest._request(method, path, params, signed=True)

    # ------------------------------------------------------------------ Account

    async def get_account(self) -> dict[str, Any]:
        """GET /api/v3/account — балансы и права."""
        return await self._signed_request("GET", "/api/v3/account")

    async def get_account_commission(self, symbol: str) -> dict[str, Any]:
        """GET /api/v3/account/commission — комиссии для символа."""
        return await self._signed_request("GET", "/api/v3/account/commission", {"symbol": symbol})

    # ------------------------------------------------------------------ Orders

    async def place_order(
        self,
        symbol: str,
        side: str,
        type: str,
        quantity: Decimal | None = None,
        quote_order_qty: Decimal | None = None,
        price: Decimal | None = None,
        time_in_force: str | None = None,
        client_order_id: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """POST /api/v3/order — размещение ордера."""
        params = {"symbol": symbol, "side": side, "type": type}
        if quantity is not None:
            params["quantity"] = str(quantity)
        if quote_order_qty is not None:
            params["quoteOrderQty"] = str(quote_order_qty)
        if price is not None:
            params["price"] = str(price)
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        if client_order_id is not None:
            params["newClientOrderId"] = client_order_id
        params.update(extra)
        return await self._signed_request("POST", "/api/v3/order", params)

    async def cancel_order(
        self,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """DELETE /api/v3/order — отмена ордера."""
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return await self._signed_request("DELETE", "/api/v3/order", params)

    async def get_order(
        self,
        symbol: str,
        order_id: int | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/v3/order — статус ордера."""
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return await self._signed_request("GET", "/api/v3/order", params)

    async def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """GET /api/v3/openOrders — открытые ордера."""
        params = {"symbol": symbol} if symbol else {}
        return await self._signed_request("GET", "/api/v3/openOrders", params)

    async def get_my_trades(self, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
        """GET /api/v3/myTrades — история сделок по аккаунту."""
        return await self._signed_request("GET", "/api/v3/myTrades", {"symbol": symbol, "limit": limit})

    # ------------------------------------------------------------------ User Data Stream

    async def start_user_stream(self) -> str:
        """POST /api/v3/userDataStream — получить listenKey."""
        resp = await self._signed_request("POST", "/api/v3/userDataStream")
        return str(resp["listenKey"])

    async def keepalive_user_stream(self, listen_key: str) -> None:
        """PUT /api/v3/userDataStream — продлить listenKey."""
        await self._signed_request("PUT", "/api/v3/userDataStream", {"listenKey": listen_key})

    async def close_user_stream(self, listen_key: str) -> None:
        """DELETE /api/v3/userDataStream — закрыть stream."""
        await self._signed_request("DELETE", "/api/v3/userDataStream", {"listenKey": listen_key})


class UserDataStream:
    """WebSocket для userDataStream (balance, order, execution updates)."""

    def __init__(
        self,
        ws_base_url: str,
        listen_key: str,
    ) -> None:
        self.url = f"{ws_base_url}/{listen_key}"
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10000)
        self._running = False

    async def connect(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="user-data-stream")

    async def _run(self) -> None:
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url) as ws:
                        self._ws = ws
                        logger.info("user data stream connected")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    await self._queue.put(msg.json())
                                except Exception:
                                    logger.exception("failed to parse user data message")
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                logger.error("user data stream error: %s", ws.exception())
                                break
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("user data stream crashed, reconnecting in 5s")
            finally:
                self._ws = None
            if self._running:
                await asyncio.sleep(5)

    async def get(self, timeout_s: float = 1.0) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_s)
        except TimeoutError:
            return None

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass