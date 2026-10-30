"""Запись рыночных событий в Parquet (ТЗ §16).

Правила:
    * запись идёт через очередь, горячий путь не блокируется диском;
    * переполнение очереди фиксируется как потеря данных, а не замалчивается;
    * у каждого файла есть версия схемы;
    * ротация по размеру.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"


def raw_event_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.string()),
            ("kind", pa.string()),
            ("symbol", pa.string()),
            ("stream", pa.string()),
            ("receive_ns", pa.int64()),
            ("receive_mono_ns", pa.int64()),
            ("exchange_ts_ms", pa.int64()),
            ("sequence_first", pa.int64()),
            ("sequence_last", pa.int64()),
            ("ingest_ns", pa.int64()),
            ("payload", pa.string()),
        ]
    )


def feature_schema() -> pa.Schema:
    return pa.schema(
        [
            ("schema_version", pa.string()),
            ("symbol", pa.string()),
            ("ts_ns", pa.int64()),
            ("book_state", pa.string()),
            ("book_age_ms", pa.float64()),
            ("tradable", pa.bool_()),
            ("block_reason", pa.string()),
            ("best_bid", pa.string()),
            ("best_ask", pa.string()),
            ("mid", pa.string()),
            ("spread", pa.float64()),
            ("spread_pct", pa.float64()),
            ("spread_bps", pa.float64()),
            ("spread_ticks", pa.float64()),
            ("spread_percentile", pa.float64()),
            ("spread_median", pa.float64()),
            ("obi_1", pa.float64()),
            ("obi_5", pa.float64()),
            ("obi_10", pa.float64()),
            ("obi_20", pa.float64()),
            ("obi_50", pa.float64()),
            ("obi_weighted", pa.float64()),
            ("microprice", pa.float64()),
            ("microprice_dev_bps", pa.float64()),
            ("ofi_top", pa.float64()),
            ("ofi_multi", pa.float64()),
            ("ofi_rolling", pa.float64()),
            ("buy_volume", pa.string()),
            ("sell_volume", pa.string()),
            ("cvd", pa.string()),
            ("trade_count", pa.int64()),
            ("wall_count", pa.int64()),
            ("top_wall_score", pa.float64()),
        ]
    )


@dataclass(slots=True)
class RecorderStats:
    recorded: int = 0
    dropped: int = 0
    files: int = 0
    bytes_written: int = 0
    last_error: str | None = None
    flush_lag_ms: float = 0.0

    @property
    def data_loss(self) -> bool:
        return self.dropped > 0 or self.last_error is not None


class ParquetRecorder:
    def __init__(
        self,
        dataset: str,
        directory: str | Path,
        schema: pa.Schema,
        rotation_mb: int = 256,
        queue_max: int = 100_000,
        batch_size: int = 2_000,
    ) -> None:
        self.dataset = dataset
        self.directory = Path(directory) / dataset
        self.schema = schema
        self.rotation_bytes = rotation_mb * 1024 * 1024
        self.batch_size = batch_size
        self.stats = RecorderStats()
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_max)
        self._task: asyncio.Task[None] | None = None
        self._buffer: list[dict] = []
        self._buffer_bytes = 0
        self._file_index = 0
        self._closing = False
        self._session_id = uuid4().hex[:12]
        self._current: pq.ParquetWriter | None = None
        self._current_path: Path | None = None

    # ------------------------------------------------------------------ публичный API

    def record(self, row: dict) -> None:
        """Неблокирующая постановка в очередь. Потеря видна в stats.dropped."""
        if self._closing or self.stats.last_error is not None:
            self.stats.dropped += 1
            return
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self.stats.dropped += 1

    async def start(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain(), name=f"recorder-{self.dataset}")

    async def close(self) -> None:
        # Never cancel to_thread(_flush): cancellation does not stop its writer thread.
        self._closing = True
        if self._task is not None:
            await self._task
        self._task = None
        try:
            if self.stats.last_error is None:
                while not self._queue.empty():
                    self._buffer.append(self._queue.get_nowait())
                await asyncio.to_thread(self._flush)
            else:
                self.stats.dropped += self._queue.qsize() + len(self._buffer)
                self._buffer.clear()
                while not self._queue.empty():
                    self._queue.get_nowait()
        finally:
            await asyncio.to_thread(self._close_writer)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------ внутреннее

    async def _drain(self) -> None:
        try:
            while not self._closing or not self._queue.empty():
                try:
                    row = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except TimeoutError:
                    continue
                self._buffer.append(row)
                deadline = asyncio.get_running_loop().time() + 0.05
                while len(self._buffer) < self.batch_size:
                    try:
                        self._buffer.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if self._closing or remaining <= 0:
                            break
                        try:
                            self._buffer.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                        except TimeoutError:
                            break
                started = time.perf_counter()
                await asyncio.to_thread(self._flush)
                self.stats.flush_lag_ms = (time.perf_counter() - started) * 1000
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("recorder crashed", extra={"extra_fields": {"dataset": self.dataset}})

    def _flush(self) -> None:
        if not self._buffer:
            return
        rows, self._buffer = self._buffer, []
        try:
            table = pa.Table.from_pylist(rows, schema=self.schema)
            if self._current is None:
                self._open_writer()
            assert self._current is not None
            self._current.write_table(table)
        except Exception as exc:
            self.stats.dropped += len(rows)
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            raise
        size = table.nbytes
        self.stats.recorded += table.num_rows
        self.stats.bytes_written += size
        self._buffer_bytes += size
        if self._buffer_bytes >= self.rotation_bytes:
            self._close_writer()

    def _open_writer(self) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self._file_index += 1
        path = self.directory / f"{self.dataset}-{stamp}-{self._session_id}-{self._file_index:05d}.parquet.partial"
        self._current = pq.ParquetWriter(path, self.schema, compression="zstd")
        self._current_path = path
        self._buffer_bytes = 0
        self.stats.files += 1
        logger.info("opened parquet file", extra={"extra_fields": {"path": str(path)}})

    def _close_writer(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None
            if self._current_path is not None and self.stats.last_error is None:
                self._current_path.rename(self._current_path.with_suffix(""))
            self._current_path = None

    # ------------------------------------------------------------------ обслуживание

    def apply_retention(self, retention_days: int) -> int:
        """Удалить файлы старше retention_days. Возвращает число удалённых."""
        if not self.directory.exists():
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        removed = 0
        for path in self.directory.glob("*.parquet"):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed


def depth_diff_row(diff, ingest_ns: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "depth_diff",
        "symbol": diff.symbol,
        "stream": "depth",
        "receive_ns": diff.meta.receive_wall_ns,
        "receive_mono_ns": diff.meta.receive_mono_ns,
        "exchange_ts_ms": diff.meta.exchange_ts_ms,
        "sequence_first": diff.first_update_id,
        "sequence_last": diff.last_update_id,
        "ingest_ns": ingest_ns,
        "payload": json.dumps(
            {
                "b": [[str(p), str(q)] for p, q in diff.bids],
                "a": [[str(p), str(q)] for p, q in diff.asks],
            },
            separators=(",", ":"),
        ),
    }


def trade_row(trade, ingest_ns: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "agg_trade",
        "symbol": trade.symbol,
        "stream": "aggTrade",
        "receive_ns": trade.receive_ns,
        "receive_mono_ns": trade.meta.receive_mono_ns,
        "exchange_ts_ms": trade.exchange_ts_ms,
        "sequence_first": trade.meta.sequence_first,
        "sequence_last": trade.meta.sequence_last,
        "ingest_ns": ingest_ns,
        "payload": json.dumps(
            {
                "id": trade.trade_id,
                "p": str(trade.price),
                "q": str(trade.quantity),
                "aggressor": trade.aggressor.value,
            },
            separators=(",", ":"),
        ),
    }


def ticker_row(ticker, ingest_ns: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "book_ticker",
        "symbol": ticker.symbol,
        "stream": "bookTicker",
        "receive_ns": ticker.receive_ns,
        "receive_mono_ns": ticker.receive_ns,
        "exchange_ts_ms": ticker.exchange_ts_ms,
        "sequence_first": None,
        "sequence_last": None,
        "ingest_ns": ingest_ns,
        "payload": json.dumps(
            {
                "b": str(ticker.bid_price),
                "B": str(ticker.bid_quantity),
                "a": str(ticker.ask_price),
                "A": str(ticker.ask_quantity),
            },
            separators=(",", ":"),
        ),
    }


def snapshot_row(snapshot, ingest_ns: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "depth_snapshot",
        "symbol": snapshot.symbol,
        "stream": "depthSnapshot",
        "receive_ns": snapshot.receive_ns,
        "receive_mono_ns": ingest_ns,
        "exchange_ts_ms": snapshot.exchange_ts_ms,
        "sequence_first": None,
        "sequence_last": snapshot.last_update_id,
        "ingest_ns": ingest_ns,
        "payload": json.dumps(
            {
                "lastUpdateId": snapshot.last_update_id,
                "b": [[str(p), str(q)] for p, q in snapshot.bids],
                "a": [[str(p), str(q)] for p, q in snapshot.asks],
            },
            separators=(",", ":"),
        ),
    }
