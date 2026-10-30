"""Сканер: фильтры исключения и диапазон оценки."""

from __future__ import annotations

from decimal import Decimal

from app.analytics.engine import FeatureEngine
from app.config.schema import ScannerSection
from app.core.enums import MarketType
from app.core.time import FakeClock
from app.exchanges.base import Instrument, TradingRules
from app.market_data.orderbook import LocalOrderBook
from app.scanner.scanner import SymbolScanner

D = Decimal


def make_env(spread_ask: str = "100.5", bid_depth: str = "50", ask_depth: str = "50"):
    clock = FakeClock()
    book = LocalOrderBook("BTCUSDT", depth=50, max_levels=50, stale_after_ms=10_000, clock=clock)
    book.apply_snapshot(
        bids=[(D("100"), D(bid_depth)), (D("99.9"), D("10"))],
        asks=[(D(spread_ask), D(ask_depth)), (D("101"), D("10"))],
        last_update_id=1,
        now_ns=clock.monotonic_ns(),
    )
    engine = FeatureEngine("BTCUSDT", tick_size=D("0.01"))
    snapshot = engine.update(book, clock.monotonic_ns())
    instrument = Instrument("BTCUSDT", "BTC", "USDT", MarketType.SPOT, "TRADING", True)
    rules = TradingRules(
        symbol="BTCUSDT",
        tick_size=D("0.01"),
        step_size=D("0.00001"),
        min_quantity=D("0.00001"),
        max_quantity=D("1000"),
        min_notional=D("5"),
    )
    config = ScannerSection(min_spread_pct=0.30, max_spread_pct=5.0, min_orderbook_depth_usdt=500)
    scanner = SymbolScanner(config, {"BTCUSDT": instrument}, {"BTCUSDT": rules}, max_position_usdt=50)
    return scanner, snapshot, book, instrument, rules


def test_eligible_symbol_gets_score() -> None:
    scanner, snapshot, book, _, _ = make_env()
    row = scanner.evaluate(snapshot, book)
    assert row.eligible is True
    assert row.exclude_reason is None
    assert 0 <= row.opportunity_score <= 100
    assert row.components is not None
    assert row.spread_pct is not None and row.spread_pct > 0.3


def test_narrow_spread_is_excluded() -> None:
    scanner, snapshot, book, _, _ = make_env(spread_ask="100.05")
    row = scanner.evaluate(snapshot, book)
    assert row.eligible is False
    assert "below min" in (row.exclude_reason or "")


def test_thin_depth_is_excluded() -> None:
    _scanner, snapshot, book, instruments, rules = make_env()
    strict = SymbolScanner(
        ScannerSection(min_spread_pct=0.30, max_spread_pct=5.0, min_orderbook_depth_usdt=1_000_000),
        {"BTCUSDT": instruments},
        {"BTCUSDT": rules},
        max_position_usdt=50,
    )
    row = strict.evaluate(snapshot, book)
    assert row.eligible is False
    assert "depth" in (row.exclude_reason or "")


def test_suspended_instrument_is_excluded() -> None:
    scanner, snapshot, book, _instrument, rules = make_env()
    suspended = Instrument("BTCUSDT", "BTC", "USDT", MarketType.SPOT, "BREAK", False)
    scanner = SymbolScanner(scanner.config, {"BTCUSDT": suspended}, {"BTCUSDT": rules}, max_position_usdt=50)
    row = scanner.evaluate(snapshot, book)
    assert row.eligible is False
    assert "trading not allowed" in (row.exclude_reason or "")


def test_min_notional_above_position_is_excluded() -> None:
    scanner, snapshot, book, instrument, _ = make_env()
    rules = TradingRules(
        symbol="BTCUSDT",
        tick_size=D("0.01"),
        step_size=D("0.00001"),
        min_quantity=D("0.00001"),
        max_quantity=D("1000"),
        min_notional=D("500"),
    )
    scanner = SymbolScanner(scanner.config, {"BTCUSDT": instrument}, {"BTCUSDT": rules}, max_position_usdt=50)
    row = scanner.evaluate(snapshot, book)
    assert row.eligible is False
    assert "min order notional" in (row.exclude_reason or "")


def test_low_24h_volume_is_excluded() -> None:
    scanner, snapshot, book, _instruments, _rules = make_env()
    scanner.volume_24h_usdt["BTCUSDT"] = D("1000")
    row = scanner.evaluate(snapshot, book)
    assert row.eligible is False
    assert "24h volume" in (row.exclude_reason or "")
    assert row.volume_24h_usdt == D("1000")


def test_unknown_volume_is_noted_but_not_excluded() -> None:
    scanner, snapshot, book, _instruments, _rules = make_env()
    row = scanner.evaluate(snapshot, book)
    assert row.eligible is True
    assert row.volume_24h_usdt is None
    assert any("24h" in note for note in row.notes)
