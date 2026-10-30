"""REST-клиент Binance Spot.

Отвечает за транспорт, подпись, лимиты и разбор ошибок. Бизнес-логики здесь нет.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from app.core.errors import ExchangeError, MalformedPayload, RateLimitExceeded
from app.core.tls import build_ssl_context
from app.exchanges.binance.ratelimit import RateLimiter
from app.exchanges.binance.sign import Credentials, signed_params

logger = logging.getLogger(__name__)

# Веса запросов — по актуальной документации Binance Spot
# (rest-api.md, «Order book»: limit 1-100 → 5, 101-500 → 25, 501-1000 → 50, 1001-5000 → 250).
DEPTH_WEIGHTS = {5: 5, 10: 5, 20: 5, 50: 5, 100: 5, 500: 25, 1000: 50, 5000: 250}


class BinanceRestClient:
    def __init__(
        self,
        base_url: str,
        credentials: Credentials | None = None,
        requests_per_minute: int = 600,
        recv_window_ms: int = 5000,
        timeout_s: float = 10.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials or Credentials.from_env()
        self.limiter = RateLimiter(requests_per_minute)
        self.recv_window_ms = recv_window_ms
        self.timeout_s = timeout_s
        self._session = session
        self._owns_session = session is None
        self._time_offset_ms = 0
        self.request_count = 0
        self.error_count = 0

    async def __aenter__(self) -> BinanceRestClient:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                headers={"User-Agent": "crypto-liquidity-engine/0.2"},
                connector=aiohttp.TCPConnector(ssl=build_ssl_context()),
            )
            self._owns_session = True
        await self.sync_time()

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ транспорт

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        weight: int = 1,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("REST client is not started: call await client.start()")

        await self.limiter.acquire(weight)
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}

        if signed:
            api_key, api_secret = self.credentials.require()
            query = signed_params(params or {}, api_secret, self.recv_window_ms, self._now_ms())
            headers["X-MBX-APIKEY"] = api_key
            url = f"{url}?{query}"
            body = None
        else:
            body = None

        self.request_count += 1
        started = time.perf_counter()
        try:
            async with self._session.request(
                method, url, params=None if signed else params, headers=headers, json=body
            ) as response:
                text = await response.text()
                status = response.status
        except TimeoutError as exc:
            self.error_count += 1
            raise ExchangeError(None, f"timeout after {self.timeout_s}s on {path}") from exc
        except aiohttp.ClientError as exc:
            self.error_count += 1
            raise ExchangeError(None, f"transport error on {path}: {exc}") from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "rest request",
                extra={"extra_fields": {"path": path, "status": status, "elapsed_ms": round(elapsed_ms, 2)}},
            )
        if status == 429:
            self.error_count += 1
            raise RateLimitExceeded(
                f"exchange rate limit hit on {path} (local wait total {self.limiter.waited_seconds:.1f}s)"
            )
        if status == 418:
            self.error_count += 1
            raise RateLimitExceeded(f"IP banned by exchange (418) on {path}")
        if status >= 400:
            self.error_count += 1
            code = None
            message = text
            try:
                payload = await_maybe_json(text)
                code = payload.get("code")
                message = payload.get("msg", text)
            except MalformedPayload:
                pass
            raise ExchangeError(code, str(message), status)

        try:
            return await_maybe_json(text)
        except MalformedPayload as exc:
            raise MalformedPayload(f"invalid JSON from {path}: {exc}") from exc

    def _now_ms(self) -> int:
        return int(time.time() * 1000 + self._time_offset_ms)

    async def sync_time(self) -> int:
        """Синхронизация часов с биржей: без неё подпись ломается (ошибка -1021)."""
        for attempt in range(3):
            try:
                payload = await self._request("GET", "/api/v3/time")
                server_time = int(payload["serverTime"])
                self._time_offset_ms = server_time - int(time.time() * 1000)
                return self._time_offset_ms
            except Exception as exc:
                if attempt == 2:
                    logger.warning("time sync failed after 3 attempts, using local time offset 0: %s", exc)
                    self._time_offset_ms = 0
                    return 0
                await asyncio.sleep(1.0 * (attempt + 1))
        return 0

    @property
    def time_offset_ms(self) -> int:
        return self._time_offset_ms

    # ------------------------------------------------------------------ публичные

    async def server_time(self) -> int:
        payload = await self._request("GET", "/api/v3/time")
        return int(payload["serverTime"])

    async def exchange_info(self, symbols: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if symbols:
            params["symbols"] = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        return await self._request("GET", "/api/v3/exchangeInfo", params, weight=20)

    async def depth(self, symbol: str, limit: int = 200) -> dict[str, Any]:
        weight = DEPTH_WEIGHTS.get(limit, 50)
        return await self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit}, weight=weight)

    async def book_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol}, weight=2)

    async def trades(self, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/v3/trades", {"symbol": symbol, "limit": limit}, weight=25)

    async def ticker_24hr(self, symbols: list[str]) -> list[dict[str, Any]]:
        """24h статистика. Вес 2 для 1-20 символов, 40 для 21-100 (документация Binance Spot)."""
        if not symbols:
            raise ValueError("symbols must not be empty")
        if len(symbols) > 20:
            raise ValueError("ticker_24hr accepts at most 20 symbols per request")
        payload = await self._request(
            "GET",
            "/api/v3/ticker/24hr",
            {"symbols": "[" + ",".join(f'"{s}"' for s in symbols) + "]"},
            weight=2,
        )
        if not isinstance(payload, list):
            raise MalformedPayload("ticker/24hr with symbols must return a list")
        return payload

    # ------------------------------------------------------------------ приватные

    async def account(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v3/account", signed=True, weight=20)

    async def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/v3/order", params, signed=True, weight=1)

    async def cancel_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("DELETE", "/api/v3/order", params, signed=True, weight=1)

    async def get_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("GET", "/api/v3/order", params, signed=True, weight=4)

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        # Вес: 6 по одному символу, 80 без символа (документация Binance Spot).
        return await self._request("GET", "/api/v3/openOrders", params, signed=True, weight=6 if symbol else 80)

    async def my_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self._request(
            "GET", "/api/v3/myTrades", {"symbol": symbol, "limit": limit}, signed=True, weight=20
        )

    async def create_listen_key(self) -> str:
        payload = await self._request("POST", "/api/v3/userDataStream", signed=False, weight=2)
        return str(payload["listenKey"])

    async def keepalive_listen_key(self, listen_key: str) -> None:
        await self._request("PUT", "/api/v3/userDataStream", {"listenKey": listen_key}, weight=2)

    async def close_listen_key(self, listen_key: str) -> None:
        await self._request("DELETE", "/api/v3/userDataStream", {"listenKey": listen_key}, weight=2)


def await_maybe_json(text: str) -> Any:
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedPayload(f"not JSON: {text[:200]!r}") from exc
