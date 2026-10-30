"""Replay: синхронизация стакана на записанном потоке Binance.

Проверяет то, что нельзя проверить на синтетике: реальный порядок U/u, перекрытие
снимка и восстановление после потери дельт.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from app.core.enums import BookIntegrity
from app.core.time import FakeClock
from app.market_data.events import DepthSnapshot
from app.market_data.synchronizer import BookSynchronizer

from tests.support import ReplayAdapter, load_diffs, load_snapshot

D = Decimal


async def run_until_eof(sync: BookSynchronizer, adapter, timeout_s: float = 5.0) -> None:
    """Крутит run() пока поток фикстур не закончится и буфер не опустеет."""
    task = asyncio.create_task(sync.run())
    try:
        for _ in range(int(timeout_s / 0.02)):
            await asyncio.sleep(0.02)
            if adapter.exhausted and sync.buffered == 0:
                break
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await sync.stop()


async def test_replay_produces_ready_book_without_gaps() -> None:
    adapter = ReplayAdapter(load_snapshot(), load_diffs(), delay_s=0.001)
    sync = BookSynchronizer(adapter, "BTCUSDT", depth=200, clock=FakeClock(), stale_after_ms=60_000)
    await run_until_eof(sync, adapter)

    assert sync.book.state is BookIntegrity.READY
    assert sync.gaps == 0
    assert sync.applied_diffs >= 50
    assert sync.book.validate() == []
    assert sync.book.best_bid() is not None
    assert sync.book.best_ask() is not None
    assert sync.book.best_bid().price < sync.book.best_ask().price
    assert sync.book.levels_loaded <= 400


async def test_replayed_book_matches_stream_state() -> None:
    """Итоговый стакан должен совпасть с последовательным применением всех дельт."""
    from app.market_data.orderbook import LocalOrderBook

    snapshot = load_snapshot()
    diffs = load_diffs()
    reference = LocalOrderBook("BTCUSDT", depth=200, max_levels=200, stale_after_ms=60_000, clock=FakeClock())
    reference.apply_snapshot(snapshot.bids, snapshot.asks, snapshot.last_update_id, 0)
    for diff in diffs:
        reference.apply_diff(
            diff.first_update_id, diff.last_update_id, diff.bids, diff.asks, diff.meta.receive_mono_ns
        )

    adapter = ReplayAdapter(snapshot, diffs, delay_s=0.0)
    sync = BookSynchronizer(adapter, "BTCUSDT", depth=200, clock=FakeClock(), stale_after_ms=60_000)
    await run_until_eof(sync, adapter)

    assert sync.book.best_bid().price == reference.best_bid().price
    assert sync.book.best_ask().price == reference.best_ask().price
    assert sync.book.levels_loaded == reference.levels_loaded


async def test_gap_in_stream_triggers_resync() -> None:
    from app.market_data.orderbook import LocalOrderBook

    snapshot = load_snapshot()
    diffs = load_diffs()

    def snapshot_at(index: int) -> DepthSnapshot:
        """Состояние стакана после применения дельт до index включительно."""
        book = LocalOrderBook("BTCUSDT", depth=200, max_levels=200, clock=FakeClock())
        book.apply_snapshot(snapshot.bids, snapshot.asks, snapshot.last_update_id, 0)
        for diff in diffs[: index + 1]:
            book.apply_diff(
                diff.first_update_id, diff.last_update_id, diff.bids, diff.asks, diff.meta.receive_mono_ns
            )
        bids, asks = book.snapshot_levels(200)
        return DepthSnapshot(
            symbol="BTCUSDT",
            last_update_id=diffs[index].last_update_id,
            bids=tuple(bids),
            asks=tuple(asks),
            exchange_ts_ms=None,
            receive_ns=0,
        )

    drop_start = 20
    dropped = {0, 1, 2}
    stream = diffs[drop_start + 1 :]
    fresh_after_gap = snapshot_at(drop_start + len(dropped))

    adapter = ReplayAdapter(
        snapshot,
        stream,
        drop_ids=dropped,
        delay_s=0.001,
        snapshots=[snapshot_at(drop_start), fresh_after_gap],
    )
    sync = BookSynchronizer(adapter, "BTCUSDT", depth=200, clock=FakeClock(), stale_after_ms=60_000)
    await run_until_eof(sync, adapter)

    assert sync.gaps >= 1
    assert adapter.snapshot_calls >= 2
    assert sync.book.state is BookIntegrity.READY
    assert sync.book.validate() == []


async def test_book_is_invalid_and_unusable_after_total_desync() -> None:
    """Если поток начался далеко впереди снимка — стакан не становится READY молча."""
    adapter = ReplayAdapter(load_snapshot(), load_diffs()[40:], delay_s=0.0)
    sync = BookSynchronizer(adapter, "BTCUSDT", depth=200, clock=FakeClock(), stale_after_ms=60_000)
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await sync.stop()

    assert sync.book.state is BookIntegrity.INVALID
    assert sync.book.ready is False
