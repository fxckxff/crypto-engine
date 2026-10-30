"""Локальный стакан: снимок, дельты, разрывы, целостность."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.enums import BookIntegrity, Side
from app.core.errors import SequenceGap, SnapshotRequired
from app.core.time import FakeClock
from app.market_data.orderbook import LocalOrderBook

D = Decimal


def make_book(clock: FakeClock | None = None, depth: int = 50) -> LocalOrderBook:
    return LocalOrderBook("BTCUSDT", depth=depth, max_levels=depth, stale_after_ms=1000, clock=clock or FakeClock())


def snapshot(book: LocalOrderBook, last_id: int = 100, now: int = 0) -> None:
    book.apply_snapshot(
        bids=[(D("100"), D("1")), (D("99"), D("2")), (D("98"), D("3"))],
        asks=[(D("101"), D("1")), (D("102"), D("2")), (D("103"), D("3"))],
        last_update_id=last_id,
        now_ns=now,
    )


def test_snapshot_sets_ready_state() -> None:
    book = make_book()
    snapshot(book)
    assert book.ready
    assert book.levels_loaded == 6
    assert book.best_bid().price == D("100")
    assert book.best_ask().price == D("101")
    assert book.spread() == D("1")
    assert book.validate() == []


def test_diff_inserts_updates_and_removes_levels() -> None:
    book = make_book()
    snapshot(book)
    book.apply_diff(101, 101, [(D("100"), D("5"))], [(D("101"), D("0"))], now_ns=1)
    assert book.level_at(Side.BID, D("100")).quantity == D("5")
    assert book.best_ask().price == D("102")  # уровень с нулём удалён
    assert book.validate() == []

    book.apply_diff(102, 102, [(D("99.5"), D("7"))], [], now_ns=2)
    assert book.best_bid().price == D("100")
    assert book.level_at(Side.BID, D("99.5")).quantity == D("7")


def test_duplicate_diff_is_ignored() -> None:
    book = make_book()
    snapshot(book, last_id=100)
    book.apply_diff(101, 110, [(D("100"), D("4"))], [], now_ns=1)
    result = book.apply_diff(105, 110, [(D("100"), D("9"))], [], now_ns=2)
    assert result.applied is False
    assert result.reason == "already_applied"
    assert book.level_at(Side.BID, D("100")).quantity == D("4")


def test_sequence_gap_invalidates_book() -> None:
    book = make_book()
    snapshot(book, last_id=100)
    book.apply_diff(101, 110, [], [], now_ns=1)
    with pytest.raises(SequenceGap) as exc:
        book.apply_diff(120, 130, [], [], now_ns=2)
    assert exc.value.expected == 111
    assert exc.value.received == 120
    assert book.state is BookIntegrity.INVALID
    assert book.gap_count == 1


def test_first_diff_must_cover_snapshot_id() -> None:
    book = make_book()
    snapshot(book, last_id=100)
    # Дельта целиком старше снимка — это не разрыв, а устаревшее событие.
    assert book.apply_diff(90, 95, [], [], now_ns=1).applied is False
    # Дельта начинается позже снимка — именно разрыв.
    with pytest.raises(SequenceGap):
        book.apply_diff(105, 110, [], [], now_ns=2)


def test_first_diff_may_overlap_snapshot() -> None:
    book = make_book()
    snapshot(book, last_id=100)
    result = book.apply_diff(95, 105, [(D("100"), D("8"))], [], now_ns=1)
    assert result.applied is True
    assert book.ready


@pytest.mark.parametrize(
    ("bids", "asks"),
    [
        ([(D("101"), D("1"))], [(D("101"), D("1"))]),
        ([(D("102"), D("1"))], [(D("101"), D("1"))]),
    ],
)
def test_crossed_snapshot_invalidates_book(bids, asks) -> None:
    book = make_book()
    book.apply_snapshot(bids, asks, last_update_id=1, now_ns=0)
    assert book.state is BookIntegrity.INVALID
    assert not book.ready


def test_crossed_after_diff_is_detected() -> None:
    book = make_book()
    snapshot(book)
    book.apply_diff(101, 101, [(D("101.5"), D("1"))], [], now_ns=1)
    assert book.state is BookIntegrity.INVALID


def test_diff_without_snapshot_is_rejected() -> None:
    book = make_book()
    with pytest.raises(SnapshotRequired):
        book.apply_diff(1, 2, [], [], now_ns=0)


def test_book_is_trimmed_to_max_levels() -> None:
    book = make_book(depth=2)
    book.apply_snapshot(
        bids=[(D("100"), D("1")), (D("99"), D("1")), (D("98"), D("1"))],
        asks=[(D("101"), D("1")), (D("102"), D("1")), (D("103"), D("1"))],
        last_update_id=5,
        now_ns=0,
    )
    assert book.levels_loaded == 4
    assert book.best_bid().price == D("100")
    assert book.best_ask().price == D("101")


def test_stale_state_after_silence_and_recovery() -> None:
    clock = FakeClock()
    book = make_book(clock)
    snapshot(book, now=clock.monotonic_ns())
    clock.advance_ms(500)
    assert book.refresh_state() is BookIntegrity.READY
    clock.advance_ms(700)
    assert book.refresh_state() is BookIntegrity.STALE

    book.apply_diff(101, 101, [(D("100"), D("2"))], [], now_ns=clock.monotonic_ns())
    assert book.state is BookIntegrity.READY


def test_level_statistics_are_estimated_not_verified() -> None:
    book = make_book()
    snapshot(book)
    book.apply_diff(101, 101, [(D("100"), D("0.5"))], [], now_ns=1, executed_by_price={D("100"): D("0.2")})
    level = book.level_at(Side.BID, D("100"))
    assert level.removed_quantity == D("0.5")
    assert level.estimated_executed_quantity == D("0.2")
    assert level.estimated_cancelled_quantity == D("0.3")
    assert level.lifetime_confidence.value == "ESTIMATED"
