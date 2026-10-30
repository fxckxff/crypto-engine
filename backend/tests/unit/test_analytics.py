"""Признаки: спред, OBI, microprice, OFI, поток сделок, гейт tradable."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.analytics.engine import FeatureEngine
from app.analytics.flow import AggressiveFlow, Trade
from app.analytics.imbalance import distance_weighted_imbalance, order_book_imbalance
from app.analytics.microprice import MicropriceTracker, microprice
from app.analytics.order_flow import OrderFlowImbalance
from app.analytics.spread import SpreadTracker
from app.core.enums import Aggressor, BookIntegrity
from app.core.time import FakeClock
from app.market_data.orderbook import LocalOrderBook

D = Decimal

# Wall-clock epoch (ns) — имитирует реальное receive_ns из time.time_ns().
# Окна в тестах резятся по монотонному времени, поэтому receive_ns и receive_mono_ns
# задаются раздельно (см. Trade.window_ts_ns).
_WALL_NS = 1_700_000_000_000_000_000


def book_with(
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    clock: FakeClock | None = None,
    last_id: int = 100,
) -> LocalOrderBook:
    clock = clock or FakeClock()
    book = LocalOrderBook("BTCUSDT", depth=50, max_levels=50, stale_after_ms=1000, clock=clock)
    book.apply_snapshot(
        bids=[(D(p), D(q)) for p, q in bids],
        asks=[(D(p), D(q)) for p, q in asks],
        last_update_id=last_id,
        now_ns=clock.monotonic_ns(),
    )
    return book


# --------------------------------------------------------------------- spread


def test_spread_math_matches_spec() -> None:
    book = book_with([("100", "1")], [("100.5", "1")])
    tracker = SpreadTracker(tick_size=D("0.5"))
    state = tracker.update(book, 1_000_000_000)
    assert state is not None
    assert state.spread == pytest.approx(0.5)
    assert state.mid == pytest.approx(100.25)
    assert state.spread_pct == pytest.approx(0.5 / 100.25 * 100)
    assert state.spread_bps == pytest.approx(0.5 / 100.25 * 100 * 100)
    assert state.spread_ticks == pytest.approx(1.0)


def test_spread_percentile_and_median() -> None:
    tracker = SpreadTracker(window_ms=60_000)
    book = book_with([("100", "1")], [("100.1", "1")])
    for i in range(10):
        tracker.update(book, i * 1_000_000_000)
    wide = book_with([("100", "1")], [("101", "1")])
    state = tracker.update(wide, 10_000_000_000)
    assert state is not None
    assert state.rolling_median == pytest.approx(0.1)
    # percentile_rank = доля выборок <= текущего значения: 10 из 11, т.к. прошлых было 10
    assert state.percentile == pytest.approx(10 / 11)
    assert state.wide_duration_ms >= 0


# --------------------------------------------------------------------- OBI


def test_obi_formula_and_undefined() -> None:
    book = book_with([("100", "3")], [("101", "1")])
    assert order_book_imbalance(book, 1) == pytest.approx((3 - 1) / 4)

    empty = LocalOrderBook("BTCUSDT")
    empty.apply_snapshot([], [], last_update_id=1, now_ns=0)
    assert order_book_imbalance(empty, 1) is None


def test_obi_depth_levels_differ() -> None:
    book = book_with([("100", "10"), ("99", "0.1")], [("101", "1"), ("102", "10")])
    obi1 = order_book_imbalance(book, 1)
    obi2 = order_book_imbalance(book, 2)
    assert obi1 is not None and obi2 is not None
    assert obi1 > obi2  # глубина добавляет объём на стороне ask


def test_weighted_imbalance_ignores_far_levels() -> None:
    near = book_with([("100", "5")], [("100.01", "1")])
    far = book_with([("90", "5")], [("100.01", "1")])
    near_value = distance_weighted_imbalance(near, n=1, half_life_pct=0.01)
    far_value = distance_weighted_imbalance(far, n=1, half_life_pct=0.01)
    assert near_value is not None and far_value is not None
    assert near_value > far_value


# --------------------------------------------------------------------- microprice


def test_microprice_formula() -> None:
    # (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)
    value = microprice(D("100"), D("101"), D("10"), D("1"))
    assert value == pytest.approx((101 * 10 + 100 * 1) / 11)


def test_microprice_velocity_sign() -> None:
    tracker = MicropriceTracker(window_ms=10_000)
    clock = FakeClock()
    up = book_with([("100", "10")], [("100.2", "1")], clock)
    state = tracker.update(up, 0)
    assert state is not None
    assert state.deviation_bps > 0

    # bid_qty растёт → microprice идёт к ask
    clock.advance_ms(100)
    up.apply_diff(101, 101, [(D("100"), D("50"))], [], now_ns=clock.monotonic_ns())
    state = tracker.update(up, clock.monotonic_ns())
    assert state is not None
    assert state.velocity_bps_s is not None and state.velocity_bps_s > 0


# --------------------------------------------------------------------- OFI


def test_ofi_increases_when_bid_grows() -> None:
    clock = FakeClock()
    book = book_with([("100", "1")], [("101", "1")], clock)
    ofi = OrderFlowImbalance(levels=1, window_ms=10_000)
    ofi.update(book, 0)
    book.apply_diff(101, 101, [(D("100"), D("5"))], [], now_ns=1)
    state = ofi.update(book, 1)
    assert state.top_of_book == pytest.approx(4.0)
    assert state.multi_level == pytest.approx(4.0)


def test_ofi_decreases_when_bid_shrinks() -> None:
    clock = FakeClock()
    book = book_with([("100", "5")], [("101", "1")], clock)
    ofi = OrderFlowImbalance(levels=1, window_ms=10_000)
    ofi.update(book, 0)
    book.apply_diff(101, 101, [(D("100"), D("2"))], [], now_ns=1)
    state = ofi.update(book, 1)
    assert state.top_of_book == pytest.approx(-3.0)


# --------------------------------------------------------------------- flow


def test_aggressive_flow_metrics() -> None:
    flow = AggressiveFlow(window_ms=60_000, large_trade_multiplier=D("2"))
    flow.add(Trade("BTCUSDT", D("100"), D("10"), Aggressor.BUY, 1, _WALL_NS, 1_000_000_000_000))
    flow.add(Trade("BTCUSDT", D("100"), D("4"), Aggressor.SELL, 2, _WALL_NS, 1_000_000_001_100))
    flow.add(Trade("BTCUSDT", D("100"), D("1"), Aggressor.UNKNOWN, 3, _WALL_NS, 1_000_000_001_200))
    snapshot = flow.snapshot(1_000_000_001_300)
    assert snapshot.buy_volume == D("10")
    assert snapshot.sell_volume == D("4")
    assert snapshot.signed_delta == D("6")
    assert snapshot.cvd == D("6")
    assert snapshot.unknown_volume == D("1")
    assert snapshot.classified_share == pytest.approx(14 / 15)
    assert snapshot.large_trade_count == 1
    assert snapshot.aggression_ratio == pytest.approx(2.5)


def test_flow_window_drops_old_trades() -> None:
    flow = AggressiveFlow(window_ms=1_000, large_trade_multiplier=D("5"))
    flow.add(Trade("BTCUSDT", D("100"), D("10"), Aggressor.BUY, 1, _WALL_NS, 0))
    flow.add(Trade("BTCUSDT", D("100"), D("1"), Aggressor.SELL, 2, _WALL_NS, 2_000_000_000))
    snapshot = flow.snapshot(2_000_000_000)
    assert snapshot.trade_count == 1
    assert snapshot.buy_volume == D("0")
    assert flow.cvd == D("9")  # CVD накопительный, окно его не откатывает


def test_flow_window_ignores_wall_clock() -> None:
    """Регрессия: окно режется по монотонному времени, а не по wall-clock.

    Раньше receive_ns был wall-clock (≈1.7e18 нс), а snapshot() получал monotonic
    (≈1e14 нс), поэтому cutoff всегда был меньше меток и окно не обрезалось вовсе.
    """
    flow = AggressiveFlow(window_ms=1_000, large_trade_multiplier=D("5"))
    flow.add(Trade("BTCUSDT", D("100"), D("10"), Aggressor.BUY, 1, _WALL_NS, 0))
    snapshot = flow.snapshot(5_000_000_000)
    assert snapshot.trade_count == 0
    assert snapshot.buy_volume == D("0")


# --------------------------------------------------------------------- гейт


def test_features_are_blocked_without_ready_book() -> None:
    clock = FakeClock()
    engine = FeatureEngine("BTCUSDT")
    empty = LocalOrderBook("BTCUSDT", clock=clock)
    snapshot = engine.update(empty, clock.monotonic_ns())
    assert snapshot.tradable is False
    assert snapshot.block_reason is not None
    assert snapshot.spread is None
    assert all(value is None for value in snapshot.obi.values())
    assert engine.blocked_snapshots == 1


def test_features_are_blocked_when_book_is_stale() -> None:
    clock = FakeClock()
    book = book_with([("100", "1")], [("101", "1")], clock)
    engine = FeatureEngine("BTCUSDT")
    assert engine.update(book, clock.monotonic_ns()).tradable is True

    clock.advance_ms(5000)
    stale = engine.update(book, clock.monotonic_ns())
    assert stale.book_state is BookIntegrity.STALE
    assert stale.tradable is False


def test_ready_book_produces_complete_snapshot() -> None:
    clock = FakeClock()
    book = book_with([("100", "3"), ("99", "2")], [("101", "1"), ("102", "2")], clock)
    engine = FeatureEngine("BTCUSDT", tick_size=D("0.01"))
    snapshot = engine.update(book, clock.monotonic_ns())
    assert snapshot.tradable is True
    assert snapshot.spread is not None
    assert snapshot.obi[1] is not None
    assert snapshot.obi[50] is not None
    assert snapshot.microprice is not None
    assert snapshot.ofi is not None
