"""Чистые функции разбора сообщений Binance Spot.

Здесь нет сети и состояния: на вход словарь, на выход типизированное событие.
Именно поэтому протокол полностью покрывается unit-тестами на записанных payload'ах.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from app.core.enums import Aggressor, Confidence, MarketType, OrderStatus, OrderType, Side
from app.core.errors import MalformedPayload
from app.core.time import EventMeta
from app.exchanges.base import Instrument, Order, OrderFill, TradingRules
from app.market_data.events import BookTicker, DepthDiff, DepthSnapshot, Quote, TradeTick

D = Decimal


def _get(payload: dict[str, Any], key: str) -> Any:
    if key not in payload:
        raise MalformedPayload(f"missing field {key!r}")
    return payload[key]


def _dec(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise MalformedPayload(f"field {field!r} is not decimal: {value!r}") from exc


def _int(value: Any, field: str) -> int:
    try:
        return int(value)
    except Exception as exc:
        raise MalformedPayload(f"field {field!r} is not int: {value!r}") from exc


def _levels(payload: Any, field: str) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(payload, list):
        raise MalformedPayload(f"field {field!r} must be a list of [price, qty]")
    result: list[tuple[Decimal, Decimal]] = []
    for row in payload:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise MalformedPayload(f"bad level in {field!r}: {row!r}")
        result.append((_dec(row[0], f"{field}.price"), _dec(row[1], f"{field}.qty")))
    return tuple(result)


# --------------------------------------------------------------------- стакан


def parse_depth_snapshot(payload: dict[str, Any], symbol: str, receive_ns: int | None = None) -> DepthSnapshot:
    """REST /api/v3/depth. Биржа не возвращает времени события — это известное ограничение."""
    if "lastUpdateId" not in payload:
        raise MalformedPayload("snapshot payload has no lastUpdateId")
    return DepthSnapshot(
        symbol=symbol.upper(),
        last_update_id=_int(payload["lastUpdateId"], "lastUpdateId"),
        bids=_levels(payload.get("bids", []), "bids"),
        asks=_levels(payload.get("asks", []), "asks"),
        exchange_ts_ms=None,
        receive_ns=receive_ns if receive_ns is not None else time.time_ns(),
    )


def parse_depth_update(
    payload: dict[str, Any],
    receive_ns: int,
    receive_mono_ns: int | None = None,
) -> DepthDiff:
    """WS stream <symbol>@depth. U/u — диапазон идентификаторов обновления."""
    if payload.get("e") != "depthUpdate":
        raise MalformedPayload(f"unexpected event type for depth: {payload.get('e')!r}")
    symbol = str(payload.get("s", "")).upper()
    if not symbol:
        raise MalformedPayload("depth update without symbol")
    first_id = _int(_get(payload, "U"), "U")
    last_id = _int(_get(payload, "u"), "u")
    exchange_ts = payload.get("E")
    meta = EventMeta(
        symbol=symbol,
        channel="depth",
        event_type="depthUpdate",
        exchange_ts_ms=_int(exchange_ts, "E") if exchange_ts is not None else None,
        receive_wall_ns=receive_ns,
        receive_mono_ns=receive_mono_ns if receive_mono_ns is not None else time.monotonic_ns(),
        sequence_first=first_id,
        sequence_last=last_id,
    )
    return DepthDiff(
        symbol=symbol,
        first_update_id=first_id,
        last_update_id=last_id,
        bids=_levels(payload.get("b", []), "b"),
        asks=_levels(payload.get("a", []), "a"),
        meta=meta,
    )


# --------------------------------------------------------------------- сделки


def aggressor_from_buyer_maker(is_buyer_maker: bool) -> Aggressor:
    """Binance aggTrade: m=true → покупатель был мейкером → агрессор продавал."""
    return Aggressor.SELL if is_buyer_maker else Aggressor.BUY


def parse_agg_trade(
    payload: dict[str, Any],
    receive_ns: int,
    receive_mono_ns: int | None = None,
) -> TradeTick:
    if payload.get("e") != "aggTrade":
        raise MalformedPayload(f"unexpected event type for trade: {payload.get('e')!r}")
    symbol = str(payload.get("s", "")).upper()
    if not symbol:
        raise MalformedPayload("aggTrade without symbol")
    if "m" not in payload:
        raise MalformedPayload("aggTrade without 'm' (aggressor side) — classification impossible")
    exchange_ts = payload.get("T", payload.get("E"))
    meta = EventMeta(
        symbol=symbol,
        channel="aggTrade",
        event_type="aggTrade",
        exchange_ts_ms=_int(exchange_ts, "T") if exchange_ts is not None else None,
        receive_wall_ns=receive_ns,
        receive_mono_ns=receive_mono_ns if receive_mono_ns is not None else time.monotonic_ns(),
        sequence_first=_int(payload["f"], "f") if "f" in payload else None,
        sequence_last=_int(payload["l"], "l") if "l" in payload else None,
    )
    return TradeTick(
        symbol=symbol,
        trade_id=_int(payload["a"], "a") if "a" in payload else None,
        price=_dec(_get(payload, "p"), "p"),
        quantity=_dec(_get(payload, "q"), "q"),
        aggressor=aggressor_from_buyer_maker(bool(payload["m"])),
        exchange_ts_ms=meta.exchange_ts_ms,
        receive_ns=receive_ns,
        meta=meta,
    )


def parse_24h_volume(payload: list[dict[str, Any]]) -> dict[str, Decimal]:
    """quoteVolume из /api/v3/ticker/24hr: объём в котируемой валюте за 24 часа."""
    volumes: dict[str, Decimal] = {}
    for item in payload:
        if not isinstance(item, dict):
            raise MalformedPayload(f"bad 24hr ticker entry: {item!r}")
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            raise MalformedPayload("24hr ticker entry without symbol")
        volumes[symbol] = _dec(item.get("quoteVolume", "0"), "quoteVolume")
    return volumes


# --------------------------------------------------------------------- котировки


def parse_book_ticker(payload: dict[str, Any], receive_ns: int) -> BookTicker:
    symbol = str(payload.get("s", "")).upper()
    if not symbol:
        raise MalformedPayload("bookTicker without symbol")
    exchange_ts = payload.get("E")
    return BookTicker(
        symbol=symbol,
        bid_price=_dec(_get(payload, "b"), "b"),
        bid_quantity=_dec(_get(payload, "B"), "B"),
        ask_price=_dec(_get(payload, "a"), "a"),
        ask_quantity=_dec(_get(payload, "A"), "A"),
        exchange_ts_ms=_int(exchange_ts, "E") if exchange_ts is not None else None,
        receive_ns=receive_ns,
    )


def quote_from_ticker(ticker: BookTicker) -> Quote:
    return Quote(
        symbol=ticker.symbol,
        bid_price=ticker.bid_price,
        bid_quantity=ticker.bid_quantity,
        ask_price=ticker.ask_price,
        ask_quantity=ticker.ask_quantity,
        exchange_ts_ms=ticker.exchange_ts_ms,
        receive_ns=ticker.receive_ns,
    )


# --------------------------------------------------------------------- справочники


def parse_instruments(payload: dict[str, Any]) -> dict[str, Instrument]:
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        raise MalformedPayload("exchangeInfo has no symbols list")
    result: dict[str, Instrument] = {}
    for item in symbols:
        if not isinstance(item, dict):
            raise MalformedPayload(f"bad symbol entry: {item!r}")
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            raise MalformedPayload("symbol entry without symbol")
        status = str(item.get("status", "UNKNOWN"))
        result[symbol] = Instrument(
            symbol=symbol,
            base_asset=str(item.get("baseAsset", "")),
            quote_asset=str(item.get("quoteAsset", "")),
            market_type=MarketType.SPOT,
            status=status,
            trading_allowed=status == "TRADING" and bool(item.get("isSpotTradingAllowed", True)),
        )
    return result


def _filter(symbol_payload: dict[str, Any], filter_type: str) -> dict[str, Any]:
    for item in symbol_payload.get("filters", []):
        if item.get("filterType") == filter_type:
            return item
    return {}


def parse_trading_rules(symbol_payload: dict[str, Any]) -> TradingRules:
    symbol = str(symbol_payload.get("symbol", "")).upper()
    if not symbol:
        raise MalformedPayload("symbol payload without symbol")
    price_filter = _filter(symbol_payload, "PRICE_FILTER")
    lot_filter = _filter(symbol_payload, "LOT_SIZE")
    notional_filter = _filter(symbol_payload, "NOTIONAL") or _filter(symbol_payload, "MIN_NOTIONAL")
    if not price_filter or not lot_filter:
        raise MalformedPayload(f"{symbol}: PRICE_FILTER or LOT_SIZE missing")
    return TradingRules(
        symbol=symbol,
        tick_size=_dec(price_filter["tickSize"], "tickSize"),
        step_size=_dec(lot_filter["stepSize"], "stepSize"),
        min_quantity=_dec(lot_filter["minQty"], "minQty"),
        max_quantity=_dec(lot_filter["maxQty"], "maxQty"),
        min_notional=_dec(notional_filter.get("minNotional", notional_filter.get("notional", "0")), "minNotional"),
        max_num_orders=(
            _int(symbol_payload["maxNumOrders"], "maxNumOrders") if "maxNumOrders" in symbol_payload else None
        ),
    )


# --------------------------------------------------------------------- ордера


_STATUS_MAP = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "PENDING_CANCEL": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
}


def parse_order(payload: dict[str, Any], symbol: str | None = None) -> Order:
    sym = str(payload.get("symbol", symbol or "")).upper()
    if not sym:
        raise MalformedPayload("order payload without symbol")
    status_raw = str(payload.get("status", "UNKNOWN"))
    side_raw = str(payload.get("side", "")).upper()
    type_raw = str(payload.get("type", "")).upper()
    if side_raw not in ("BUY", "SELL"):
        raise MalformedPayload(f"bad side {side_raw!r}")
    side = Side.BID if side_raw == "BUY" else Side.ASK
    order_type = {
        "LIMIT": OrderType.LIMIT,
        "MARKET": OrderType.MARKET,
        "LIMIT_MAKER": OrderType.LIMIT_MAKER,
    }.get(type_raw, OrderType.LIMIT)

    filled = _dec(payload.get("executedQty", "0"), "executedQty")
    quote_filled = _dec(payload.get("cummulativeQuoteQty", "0"), "cummulativeQuoteQty")
    avg_price = quote_filled / filled if filled > 0 and quote_filled > 0 else Decimal(0)
    fee = Decimal(0)
    for fill in payload.get("fills", []) or []:
        fee += _dec(fill.get("commission", "0"), "commission")

    return Order(
        symbol=sym,
        side=side,
        order_type=order_type,
        status=_STATUS_MAP.get(status_raw, OrderStatus.UNKNOWN),
        quantity=_dec(payload.get("origQty", "0"), "origQty"),
        price=_dec(payload["price"], "price") if payload.get("price") not in (None, "") else None,
        filled_quantity=filled,
        average_price=avg_price,
        fee_paid=fee,
        exchange_order_id=str(payload["orderId"]) if "orderId" in payload else None,
        client_order_id=str(payload["clientOrderId"]) if "clientOrderId" in payload else None,
        exchange_ts_ms=_int(payload["transactTime"], "transactTime") if "transactTime" in payload else None,
    )


def parse_my_trade(payload: dict[str, Any]) -> OrderFill:
    return OrderFill(
        price=_dec(payload["price"], "price"),
        quantity=_dec(payload["qty"], "qty"),
        commission=_dec(payload.get("commission", "0"), "commission"),
        commission_asset=str(payload.get("commissionAsset", "")),
        trade_id=_int(payload["id"], "id") if "id" in payload else None,
        exchange_ts_ms=_int(payload["time"], "time") if "time" in payload else None,
    )


AGGRESSOR_CONFIDENCE = Confidence.VERIFIED
