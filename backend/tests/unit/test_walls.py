"""Детектор стен: условия, жизненный цикл, оценка, причины переходов."""

from __future__ import annotations

from decimal import Decimal

from app.analytics.flow import Trade
from app.analytics.walls import WallConfig, WallDetector, reference_notional
from app.core.enums import Aggressor, Side, WallState
from app.core.time import FakeClock
from app.market_data.orderbook import LocalOrderBook

D = Decimal
MS = 1_000_000


def build_book(clock: FakeClock, wall_qty: str = "100") -> LocalOrderBook:
    book = LocalOrderBook("BTCUSDT", depth=50, max_levels=50, stale_after_ms=60_000, clock=clock)
    book.apply_snapshot(
        bids=[
            (D("100.00"), D("1")),
            (D("99.99"), D(wall_qty)),  # кандидат в стену
            (D("99.98"), D("1")),
            (D("99.97"), D("1")),
            (D("99.96"), D("1")),
        ],
        asks=[
            (D("100.01"), D("1")),
            (D("100.02"), D("1")),
            (D("100.03"), D("1")),
        ],
        last_update_id=100,
        now_ns=clock.monotonic_ns(),
    )
    return book


def config(**overrides: object) -> WallConfig:
    base = {
        "min_wall_notional": D("500"),
        "wall_size_ratio": 3.0,
        "max_wall_distance_pct": 1.0,
        "observe_ms": 1000,
        "persist_ms": 5000,
        "expire_ms": 60_000,
    }
    base.update(overrides)
    return WallConfig(**base)  # type: ignore[arg-type]


def test_reference_notional_ignores_zero_levels() -> None:
    notionals = [D("0"), D("100"), D("0"), D("300"), D("0")]
    # Нулевые уровни не попадают в медиану: остаётся только 100.
    assert reference_notional(notionals, index=0, k=2) == D("100")
    # Без положительных соседей падаем на все ненулевые уровни стакана.
    assert reference_notional([D("0"), D("50"), D("0")], index=0, k=1) == D("50")


def test_wall_is_detected_by_size_and_distance() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())
    walls = detector.active_walls()
    assert len(walls) == 1
    wall = walls[0]
    assert wall.side is Side.BID
    assert wall.price == D("99.99")
    assert wall.state is WallState.DETECTED
    assert 0 <= wall.score <= 100


def test_small_level_is_not_a_wall() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock, wall_qty="1")
    detector.update(book, clock.monotonic_ns())
    assert detector.active_walls() == []


def test_far_level_is_not_a_wall() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config(max_wall_distance_pct=0.005))
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())
    assert detector.active_walls() == []


def test_lifecycle_observe_then_persist() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())

    clock.advance_ms(1100)
    transitions = detector.update(book, clock.monotonic_ns())
    assert [t.to_state for t in transitions] == [WallState.OBSERVING]
    assert transitions[0].reason

    clock.advance_ms(4000)
    transitions = detector.update(book, clock.monotonic_ns())
    assert [t.to_state for t in transitions] == [WallState.PERSISTENT]


def test_trade_near_wall_marks_tested_and_absorbs() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())
    clock.advance_ms(1100)
    detector.update(book, clock.monotonic_ns())

    trade = Trade("BTCUSDT", D("99.99"), D("40"), Aggressor.SELL, 1, clock.wall_ns())
    transitions = detector.update(book, clock.monotonic_ns(), trades=[trade])
    states = [t.to_state for t in transitions]
    assert WallState.TESTED in states

    wall = detector.active_walls()[0]
    assert wall.absorbed_quantity == D("40")
    assert wall.state is WallState.ABSORBING  # 40 % от 100 > порога 30 %


def test_weakening_when_size_drops() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())
    clock.advance_ms(6000)
    detector.update(book, clock.monotonic_ns())

    book.apply_diff(101, 101, [(D("99.99"), D("10"))], [], now_ns=clock.monotonic_ns())
    transitions = detector.update(book, clock.monotonic_ns())
    assert WallState.WEAKENING in [t.to_state for t in transitions]


def test_removed_wall_is_marked_removed() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())

    book.apply_diff(101, 101, [(D("99.99"), D("0"))], [], now_ns=clock.monotonic_ns())
    transitions = detector.update(book, clock.monotonic_ns())
    assert [t.to_state for t in transitions] == [WallState.REMOVED]
    assert "removed" in transitions[0].reason
    assert detector.active_walls() == []


def test_broken_wall_when_traded_through() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())

    # Сделка прошла ниже уровня и объём снят: стена сметена, а не отменена.
    book.apply_diff(101, 101, [(D("99.99"), D("0"))], [], now_ns=clock.monotonic_ns())
    trade = Trade("BTCUSDT", D("99.90"), D("5"), Aggressor.SELL, 2, clock.wall_ns())
    transitions = detector.update(book, clock.monotonic_ns(), trades=[trade])
    assert [t.to_state for t in transitions] == [WallState.BROKEN]
    assert "through" in transitions[0].reason


def test_no_detection_on_invalid_book() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = LocalOrderBook("BTCUSDT", clock=clock)
    assert detector.update(book, clock.monotonic_ns()) == []


def test_wall_history_keeps_reasons() -> None:
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())
    clock.advance_ms(1100)
    detector.update(book, clock.monotonic_ns())
    clock.advance_ms(4000)
    detector.update(book, clock.monotonic_ns())

    wall = detector.active_walls()[0]
    assert len(wall.transitions) == 2
    assert all(t.reason for t in wall.transitions)
    assert wall.transitions[0].from_state is WallState.DETECTED
    assert wall.transitions[-1].to_state is WallState.PERSISTENT


def test_buy_trade_does_not_count_as_bid_wall_absorption():
    clock = FakeClock()
    detector = WallDetector("BTCUSDT", config())
    book = build_book(clock)
    detector.update(book, clock.monotonic_ns())
    trade = Trade("BTCUSDT", D("99.99"), D("40"), Aggressor.BUY, 1, clock.wall_ns())
    detector.update(book, clock.monotonic_ns(), trades=[trade])
    assert detector.active_walls()[0].absorbed_quantity == 0
