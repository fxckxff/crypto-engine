"""Разбор сообщений Binance на реальных записанных payload'ах."""

from __future__ import annotations

import json
from itertools import pairwise

import pytest
from app.core.enums import Aggressor
from app.core.errors import MalformedPayload
from app.exchanges.binance.protocol import (
    aggressor_from_buyer_maker,
    parse_agg_trade,
    parse_depth_snapshot,
    parse_depth_update,
    parse_instruments,
    parse_order,
    parse_trading_rules,
)

from tests.support import FIXTURES, load_diffs, load_trades


def test_snapshot_from_live_fixture() -> None:
    payload = json.loads((FIXTURES / "BTCUSDT_snapshot.json").read_text(encoding="utf-8"))
    snapshot = parse_depth_snapshot(payload, "btcusdt", receive_ns=1)
    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.last_update_id == payload["lastUpdateId"]
    assert len(snapshot.bids) == len(payload["bids"])
    assert snapshot.bids[0][0] > snapshot.bids[-1][0]
    assert snapshot.asks[0][0] < snapshot.asks[-1][0]
    assert snapshot.exchange_ts_ms is None  # ограничение REST Binance


def test_depth_stream_is_ordered_and_contiguous() -> None:
    diffs = load_diffs()
    assert len(diffs) >= 50
    for previous, current in pairwise(diffs):
        assert current.first_update_id == previous.last_update_id + 1
        assert current.meta.exchange_ts_ms is not None
        assert current.meta.receive_wall_ns >= 0


def test_trade_aggressor_mapping() -> None:
    assert aggressor_from_buyer_maker(True) is Aggressor.SELL
    assert aggressor_from_buyer_maker(False) is Aggressor.BUY

    rows = load_trades()
    assert rows
    for row in rows:
        trade = parse_agg_trade(row["data"], receive_ns=1)
        expected = Aggressor.SELL if row["data"]["m"] else Aggressor.BUY
        assert trade.aggressor is expected
        assert trade.quantity > 0


def test_depth_update_without_ids_is_rejected() -> None:
    with pytest.raises(MalformedPayload):
        parse_depth_update({"e": "depthUpdate", "s": "BTCUSDT", "b": [], "a": []}, receive_ns=1)


def test_trade_without_aggressor_field_is_rejected() -> None:
    payload = {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "1.0", "q": "1.0", "T": 1}
    with pytest.raises(MalformedPayload):
        parse_agg_trade(payload, receive_ns=1)


def test_wrong_event_type_is_rejected() -> None:
    with pytest.raises(MalformedPayload):
        parse_depth_update({"e": "kline", "s": "BTCUSDT"}, receive_ns=1)


def test_trading_rules_from_live_exchange_info() -> None:
    payload = json.loads((FIXTURES / "BTCUSDT_exchange_info.json").read_text(encoding="utf-8"))
    instruments = parse_instruments(payload)
    assert "BTCUSDT" in instruments
    assert instruments["BTCUSDT"].trading_allowed is True

    rules = parse_trading_rules(payload["symbols"][0])
    assert rules.tick_size > 0
    assert rules.step_size > 0
    assert rules.min_notional > 0
    assert rules.min_quantity <= rules.max_quantity


def test_order_parsing_computes_average_price() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "status": "PARTIALLY_FILLED",
        "origQty": "1.00000000",
        "executedQty": "0.40000000",
        "cummulativeQuoteQty": "8000.00000000",
        "price": "20000.00000000",
        "orderId": 42,
        "clientOrderId": "abc",
        "transactTime": 1700000000000,
        "fills": [{"commission": "0.0004", "commissionAsset": "BTC"}],
    }
    order = parse_order(payload)
    assert order.filled_quantity == order.quantity - order.remaining_quantity
    assert order.average_price == 20000
    assert order.fee_paid > 0
    assert order.exchange_order_id == "42"


def test_bad_side_is_rejected() -> None:
    with pytest.raises(MalformedPayload):
        parse_order({"symbol": "BTCUSDT", "side": "HOLD", "type": "LIMIT", "status": "NEW"})
