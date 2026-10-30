from __future__ import annotations

import asyncio
import json
import threading
from decimal import Decimal
from unittest.mock import AsyncMock

import pyarrow.parquet as pq
import pytest
from app.config.schema import Config
from app.core.enums import BookIntegrity
from app.core.errors import SnapshotRequired
from app.core.time import FakeClock
from app.exchanges.binance.adapter import BinanceSpotAdapter
from app.exchanges.binance.sign import Credentials
from app.market_data.orderbook import LocalOrderBook
from app.market_data.synchronizer import BookSynchronizer
from app.market_data.websocket_manager import ReconnectingStream, ReconnectPolicy
from app.recorder.parquet_reader import ParquetReader
from app.recorder.parquet_recorder import ParquetRecorder, raw_event_schema
from app.services.market_service import MarketService
from scripts.backtest import run_backtest
from scripts.replay import run_replay

from tests.support import ReplayAdapter, load_diffs, load_snapshot
from tests.unit.test_recorder import row


@pytest.mark.parametrize("mode", ["research", "backtest", "paper", "live"])
def test_execution_cannot_bypass_live_guard(mode):
    with pytest.raises(ValueError):
        Config.model_validate({"app": {"mode": mode}, "execution": {"enabled": True}})


async def test_service_stop_before_start():
    adapter = AsyncMock()
    config = Config.model_validate({"market_data": {"record_raw_events": False, "record_feature_snapshots": False}})
    service = MarketService(config, adapter)
    await service.stop()
    adapter.close.assert_awaited_once()


async def test_reader_failure_propagates_without_hanging():
    class BrokenAdapter(ReplayAdapter):
        async def stream_order_book(self, symbol, depth=200):
            raise RuntimeError("broken stream")
            yield  # pragma: no cover
    sync = BookSynchronizer(BrokenAdapter(load_snapshot(), []), "BTCUSDT")
    try:
        with pytest.raises(RuntimeError, match="broken stream"):
            await asyncio.wait_for(sync.run(), timeout=1)
        assert sync.book.state == BookIntegrity.INVALID
    finally:
        await sync.stop()


def test_invalid_book_requires_new_snapshot():
    book = LocalOrderBook("BTCUSDT", clock=FakeClock())
    snap = load_snapshot()
    book.apply_snapshot(snap.bids, snap.asks, snap.last_update_id, 0)
    book.mark_invalid("data loss")
    with pytest.raises(SnapshotRequired):
        book.apply_diff(snap.last_update_id + 1, snap.last_update_id + 1, [], [], 1)


def test_empty_snapshot_is_not_ready():
    book = LocalOrderBook("BTCUSDT")
    book.apply_snapshot([], [], 1, 1)
    assert not book.ready


async def test_buffer_overflow_invalidates_immediately():
    sync = BookSynchronizer(ReplayAdapter(load_snapshot(), load_diffs(), delay_s=0), "BTCUSDT", queue_max=1)
    await sync.start()
    await sync._reader
    assert sync.dropped_diffs > 0
    assert sync.book.state == BookIntegrity.INVALID
    await sync.stop()


def test_long_outage_backoff_does_not_overflow():
    assert ReconnectPolicy(jitter=0).delay_s(100000) == 30


async def test_clean_websocket_close_reconnects_with_backoff(monkeypatch):
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def __aiter__(self):
            if False:
                yield "unused"
    monkeypatch.setattr("app.market_data.websocket_manager.websockets.connect", lambda *a, **kw: Socket())
    stream = ReconnectingStream("test", "ws://localhost", max_attempts=1)
    assert [message async for message in stream.messages()] == []
    assert stream.stats.disconnects == 1
    assert not stream.stats.connected


async def test_completed_single_file_is_readable(tmp_path):
    rec = ParquetRecorder("raw_events", tmp_path, raw_event_schema())
    await rec.start()
    rec.record(row(1))
    await rec.close()
    assert ParquetReader(tmp_path).load_dataset("raw_events").count_rows() == 1
    assert not list(tmp_path.rglob("*.partial"))


async def test_shutdown_waits_for_inflight_write(tmp_path, monkeypatch):
    rec = ParquetRecorder("raw_events", tmp_path, raw_event_schema())
    entered, release = threading.Event(), threading.Event()
    original = rec._flush
    def slow_flush():
        entered.set()
        assert release.wait(3)
        original()
    monkeypatch.setattr(rec, "_flush", slow_flush)
    await rec.start()
    rec.record(row(1))
    assert await asyncio.to_thread(entered.wait, 2)
    rec.record(row(2))
    closing = asyncio.create_task(rec.close())
    await asyncio.sleep(0.02)
    assert not closing.done()
    release.set()
    await closing
    dataset = ParquetReader(tmp_path).load_dataset("raw_events")
    assert dataset.count_rows() == 2
    assert rec.stats.dropped == 0


async def test_disk_failure_visible_and_counted(tmp_path, monkeypatch):
    rec = ParquetRecorder("raw_events", tmp_path, raw_event_schema())
    def fail():
        raise OSError("disk full")
    monkeypatch.setattr(rec, "_open_writer", fail)
    await rec.start()
    rec.record(row(1))
    await asyncio.wait_for(rec._task, 2)
    rec.record(row(2))
    await rec.close()
    assert rec.stats.data_loss
    assert rec.stats.dropped == 2


async def test_commission_units_are_basis_points():
    adapter = BinanceSpotAdapter(["BTCUSDT"], credentials=Credentials("test", "test"))
    adapter.rest.account = AsyncMock(return_value={"makerCommission": 10, "takerCommission": 15})
    fees = await adapter.get_account_commissions()
    assert fees.maker_bps == Decimal(10)
    assert fees.taker_bps == Decimal(15)


def dataset(tmp_path, events):
    path = tmp_path / "raw_events"
    path.mkdir()
    pq.write_table(__import__("pyarrow").Table.from_pylist(events, schema=raw_event_schema()), path / "events.parquet")
    return tmp_path


def event(i, kind, payload):
    return {**row(i), "kind": kind, "ingest_ns": i * 1_000_000_000,
            "receive_ns": i * 1_000_000_000, "payload": json.dumps(payload)}


async def test_replay_range_warms_book_and_is_repeatable(tmp_path):
    path = dataset(tmp_path, [event(1, "depth_snapshot", {"lastUpdateId": 1, "b": [["100", "1"]],
                                                       "a": [["101", "1"]]}),
                              event(2, "depth_update", {"U": 2, "u": 2, "b": [["100", "2"]], "a": []}),
                              event(3, "agg_trade", {"p": "100", "q": "1", "aggressor": "SELL"})])
    first = await run_replay(path, "BTCUSDT", 2_000_000_000, 2_000_000_000)
    assert first == await run_replay(path, "BTCUSDT", 2_000_000_000, 2_000_000_000)
    assert first["events_processed"] == 1
    assert first["final_book_state"] == "READY"


async def test_backtest_queue_partial_fill_fees_no_short(tmp_path):
    # First sell consumes the existing bid queue; only excess volume can fill our buy.
    path = dataset(tmp_path, [event(1, "depth_snapshot", {"lastUpdateId": 1, "b": [["100", "1"]],
                                                       "a": [["101", "1"]]}),
                              event(2, "agg_trade", {"p": "100", "q": "1.0005", "aggressor": "SELL"})])
    result = await run_backtest(path, "BTCUSDT", "simple_market_making", {})
    assert result["total_trades"] == 1
    assert result["final_position"] == pytest.approx(0.0005)
    assert result["fees_paid"] == pytest.approx(0.00005)
    assert result["final_cash"] == pytest.approx(10000 - 0.05 - 0.00005)
    assert result == await run_backtest(path, "BTCUSDT", "simple_market_making", {})


async def test_backtest_does_not_fill_before_latency(tmp_path):
    path = dataset(tmp_path, [event(1, "depth_snapshot", {"lastUpdateId": 1, "b": [["100", "1"]],
                                                       "a": [["101", "1"]]}),
                              event(2, "agg_trade", {"p": "100", "q": "50", "aggressor": "SELL"})])
    result = await run_backtest(path, "BTCUSDT", "simple_market_making", {"latency_ms": 2000})
    assert result["total_trades"] == 0
