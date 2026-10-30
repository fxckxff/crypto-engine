from dataclasses import replace
from decimal import Decimal as D

from app.analytics.flow import Trade
from app.analytics.walls import WallDetector
from app.core.enums import Aggressor
from app.core.time import FakeClock

from tests.unit.test_walls import build_book, config


def observed():
    clock = FakeClock()
    book = build_book(clock)
    detector = WallDetector("BTCUSDT", config())
    for _ in range(7):
        detector.update(book, clock.monotonic_ns())
        clock.advance_ms(1000)
    return clock, book, detector


def trade(clock, ident, price="99.99", quantity="20"):
    return Trade(
        "BTCUSDT",
        D(price),
        D(quantity),
        Aggressor.SELL,
        1,
        clock.wall_ns(),
        receive_mono_ns=clock.monotonic_ns(),
        trade_id=ident,
    )


def test_large_old_level_without_trades_is_only_candidate():
    _, _, detector = observed()
    wall = detector.active_walls()[0]
    assert not wall.qualified
    assert any("сделок" in reason for reason in wall.quality_reasons)


def test_quality_needs_persistence_and_two_exact_unique_trades():
    clock, book, detector = observed()
    one = trade(clock, 1)
    detector.update(book, clock.monotonic_ns(), [one, one])
    wall = detector.active_walls()[0]
    assert wall.absorbed_quantity == D(20)
    assert wall.exact_touches == 1
    assert not wall.qualified
    detector.update(book, clock.monotonic_ns(), [trade(clock, 2)])
    assert wall.qualified and wall.score >= 65
    assert sum(wall.score_components.values()) > 65


def test_nearby_trade_is_not_absorption():
    clock, book, detector = observed()
    detector.update(book, clock.monotonic_ns(), [trade(clock, 1, "100"), trade(clock, 2, "100")])
    wall = detector.active_walls()[0]
    assert wall.absorbed_quantity == 0 and not wall.qualified


def test_unexplained_withdrawal_downgrades_wall():
    clock, book, detector = observed()
    detector.update(book, clock.monotonic_ns(), [trade(clock, 1), trade(clock, 2)])
    wall = detector.active_walls()[0]
    assert wall.qualified
    book.apply_diff(101, 101, [(D("99.99"), D(30))], [], now_ns=clock.monotonic_ns())
    detector.update(book, clock.monotonic_ns())
    assert not wall.qualified
    assert wall.cancelled_quantity_estimate == D(70)
    assert wall.current_quantity == D(30)
    assert wall.score_components["withdrawal_penalty"] < 0


def test_pre_detection_level_changes_are_not_counted_as_withdrawals():
    clock = FakeClock()
    book = build_book(clock, wall_qty="200")
    book.apply_diff(101, 101, [(D("99.99"), D(100))], [], now_ns=clock.monotonic_ns())
    detector = WallDetector("BTCUSDT", config())
    detector.update(book, clock.monotonic_ns())
    assert detector.active_walls()[0].cancelled_quantity_estimate == 0


def test_data_gap_removes_quality_and_requires_new_evidence():
    clock, book, detector = observed()
    detector.update(book, clock.monotonic_ns(), [trade(clock, 1), trade(clock, 2)])
    wall = detector.active_walls()[0]
    assert wall.qualified
    detector.invalidate_observation(clock.monotonic_ns())
    assert not wall.qualified and wall.absorbed_quantity == 0
    clock.advance_ms(1000)
    detector.update(book, clock.monotonic_ns())
    assert not wall.qualified


def test_no_quality_from_wrong_symbol_or_pre_detection_trade():
    clock, book, detector = observed()
    other = replace(trade(clock, 1), symbol="OTHERUSDT")
    detector.update(book, clock.monotonic_ns(), [other])
    assert detector.active_walls()[0].absorbed_quantity == 0


def test_default_relative_detection_can_find_altcoin_wall_below_250k():
    clock = FakeClock()
    detector = WallDetector("BTCUSDT")
    detector.update(build_book(clock), clock.monotonic_ns())
    assert len(detector.active_walls()) == 1
    assert detector.active_walls()[0].current_notional < 250000
