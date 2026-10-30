"""Синхронизация локального стакана со снимком и потоком дельт (ТЗ §4.2).

Порядок, который гарантирует корректность:
    1. дельты начинают буферизоваться до запроса снимка;
    2. снимок применяется как база;
    3. буфер отбрасывает всё, что старше снимка, остаток применяется по порядку;
    4. при разрыве последовательности стакан пересоздаётся, а не «доклеивается».

Важная деталь: при неудачном выравнивании дельта НЕ выбрасывается из буфера.
Иначе повторный снимок уже не сойдётся с потоком и пересинхронизация зациклится.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable

from app.core.enums import BookIntegrity
from app.core.errors import SequenceGap, SnapshotRequired
from app.core.time import Clock
from app.market_data.events import DepthDiff
from app.market_data.orderbook import LocalOrderBook
from app.recorder.parquet_recorder import (
    SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)

UpdateCallback = Callable[[LocalOrderBook, DepthDiff], Awaitable[None] | None]
TradeHint = Callable[[int, int], dict]


class BookSynchronizer:
    def __init__(
        self,
        adapter,
        symbol: str,
        depth: int = 200,
        clock: Clock | None = None,
        stale_after_ms: float = 2000.0,
        queue_max: int = 20_000,
        trade_hint: TradeHint | None = None,
        recorder = None,
    ) -> None:
        self.adapter = adapter
        self.symbol = symbol.upper()
        self.depth = depth
        self.clock = clock or Clock()
        self.book = LocalOrderBook(
            symbol=self.symbol,
            depth=depth,
            max_levels=depth,
            stale_after_ms=stale_after_ms,
            clock=self.clock,
        )
        self._buffer: deque[DepthDiff] = deque()
        self._queue_max = queue_max
        self._available = asyncio.Event()
        self._reader: asyncio.Task[None] | None = None
        self._first_event = asyncio.Event()
        self.trade_hint = trade_hint
        self.recorder = recorder
        self.dropped_diffs = 0
        self.gaps = 0
        self.resyncs = 0
        self.applied_diffs = 0
        self._last_diff_ns = 0
        self._reader_error: Exception | None = None

    # ------------------------------------------------------------------ буфер

    async def start(self) -> None:
        if self._reader is None or self._reader.done():
            self._reader_error = None
            self._first_event.clear()
            self._reader = asyncio.create_task(self._read_diffs(), name=f"diff-reader-{self.symbol}")

    async def stop(self) -> None:
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
        self._reader = None
        self._buffer.clear()

    async def _read_diffs(self) -> None:
        try:
            async for diff in self.adapter.stream_order_book(self.symbol, self.depth):
                if diff.symbol and diff.symbol.upper() != self.symbol:
                    continue
                if len(self._buffer) >= self._queue_max:
                    self._buffer.popleft()
                    self.dropped_diffs += 1
                    self.book.invalidate("diff buffer overflow")
                    logger.error(
                        "diff buffer overflow: local data loss",
                        extra={"extra_fields": {"symbol": self.symbol, "dropped": self.dropped_diffs}},
                    )
                self._buffer.append(diff)
                self._available.set()
                self._first_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error = exc
            self.book.invalidate("depth stream failed")
            logger.exception("diff reader crashed", extra={"extra_fields": {"symbol": self.symbol}})
        finally:
            self._available.set()
            self._first_event.set()

    async def _next_diff(self) -> DepthDiff:
        while True:
            if self._buffer:
                return self._buffer.popleft()
            if self._reader is not None and self._reader.done():
                if self._reader_error is not None:
                    raise self._reader_error
                raise EOFError("depth stream ended")
            self._available.clear()
            await self._available.wait()

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    # ------------------------------------------------------------------ синхронизация

    async def synchronize(self, max_attempts: int = 5) -> LocalOrderBook:
        """Снимок + выравнивание буфера. Возвращает стакан в состоянии READY или INVALID."""
        for attempt in range(1, max_attempts + 1):
            snapshot = await self.adapter.get_order_book_snapshot(self.symbol, self.depth)
            now = self.clock.monotonic_ns()
            ingest_ns = self.clock.wall_ns()
            self.book.apply_snapshot(
                bids=snapshot.bids,
                asks=snapshot.asks,
                last_update_id=snapshot.last_update_id,
                now_ns=now,
                exchange_ts_ms=snapshot.exchange_ts_ms,
            )
            
            # Record depth snapshot
            if self.recorder is not None:
                payload = json.dumps(
                    {
                        "lastUpdateId": snapshot.last_update_id,
                        "b": [[str(p), str(q)] for p, q in snapshot.bids],
                        "a": [[str(p), str(q)] for p, q in snapshot.asks],
                    },
                    separators=(",", ":"),
                )
                row = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "depth_snapshot",
                    "symbol": self.symbol,
                    "stream": "depthSnapshot",
                    "receive_ns": snapshot.receive_ns,
                    "receive_mono_ns": now,
                    "exchange_ts_ms": snapshot.exchange_ts_ms,
                    "sequence_first": None,
                    "sequence_last": snapshot.last_update_id,
                    "ingest_ns": ingest_ns,
                    "payload": payload,
                }
                self.recorder.record(row)
            
            self.resyncs += 1
            logger.info(
                "book snapshot applied",
                extra={
                    "extra_fields": {
                        "symbol": self.symbol,
                        "last_update_id": snapshot.last_update_id,
                        "levels": self.book.levels_loaded,
                        "buffered": len(self._buffer),
                        "attempt": attempt,
                    }
                },
            )

            if self.book.ready and self._drain_buffer() and self.book.ready:
                return self.book

            logger.warning(
                "buffer ahead of snapshot: refetching",
                extra={"extra_fields": {"symbol": self.symbol, "attempt": attempt}},
            )
            await asyncio.sleep(0.05 * attempt)

        self.book.mark_invalid("cannot align snapshot with diff stream")
        return self.book

    def _drain_buffer(self) -> bool:
        """Применить накопленные дельты. False — буфер начинается позже ожидаемого."""
        applied = 0
        while self._buffer:
            diff = self._buffer[0]
            if diff.last_update_id <= (self.book.last_update_id or 0):
                self._buffer.popleft()
                continue
            try:
                self.book.apply_diff(
                    first_update_id=diff.first_update_id,
                    last_update_id=diff.last_update_id,
                    bids=diff.bids,
                    asks=diff.asks,
                    now_ns=diff.meta.receive_mono_ns,
                    exchange_ts_ms=diff.meta.exchange_ts_ms,
                    executed_by_price=self._hints(diff),
                )
            except (SequenceGap, SnapshotRequired):
                # Дельта остаётся в буфере: следующий снимок может её закрыть.
                self.gaps += 1
                return False
            
            # Record depth diff
            if self.recorder is not None:
                ingest_ns = self.clock.wall_ns()
                payload = json.dumps(
                    {
                        "U": diff.first_update_id,
                        "u": diff.last_update_id,
                        "b": [[str(p), str(q)] for p, q in diff.bids],
                        "a": [[str(p), str(q)] for p, q in diff.asks],
                    },
                    separators=(",", ":"),
                )
                row = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "depth_update",
                    "symbol": self.symbol,
                    "stream": "depthUpdate",
                    "receive_ns": diff.meta.receive_wall_ns,
                    "receive_mono_ns": diff.meta.receive_mono_ns,
                    "exchange_ts_ms": diff.meta.exchange_ts_ms,
                    "sequence_first": diff.first_update_id,
                    "sequence_last": diff.last_update_id,
                    "ingest_ns": ingest_ns,
                    "payload": payload,
                }
                self.recorder.record(row)
            
            self._buffer.popleft()
            applied += 1
            self.applied_diffs += 1
        return True

    async def run(self, on_update: UpdateCallback | None = None) -> None:
        await self.start()
        # Снимок берём только когда поток дельт уже жив, иначе первый же diff даёт разрыв.
        try:
            await asyncio.wait_for(self._first_event.wait(), timeout=15.0)
            if self._reader_error is not None:
                raise self._reader_error
        except TimeoutError:
            logger.error(
                "no depth data within 15s, retrying connection",
                extra={"extra_fields": {"symbol": self.symbol}},
            )
            self.book.invalidate("depth stream timeout")
            raise
        await self.synchronize()

        while True:
            try:
                diff = await self._next_diff()
            except EOFError:
                return
            if diff.last_update_id <= (self.book.last_update_id or 0):
                continue
            try:
                self.book.apply_diff(
                    first_update_id=diff.first_update_id,
                    last_update_id=diff.last_update_id,
                    bids=diff.bids,
                    asks=diff.asks,
                    now_ns=diff.meta.receive_mono_ns,
                    exchange_ts_ms=diff.meta.exchange_ts_ms,
                    executed_by_price=self._hints(diff),
                )
            except SequenceGap as exc:
                self.gaps += 1
                logger.warning(
                    "sequence gap detected, resyncing",
                    extra={"extra_fields": {"symbol": self.symbol, "expected": exc.expected, "got": exc.received}},
                )
                self.book.invalidate(f"sequence gap {exc.expected}->{exc.received}")
                self._buffer.appendleft(diff)
                await self.synchronize()
                continue
            except SnapshotRequired:
                self._buffer.appendleft(diff)
                await self.synchronize()
                continue
            
            # Record depth diff
            if self.recorder is not None:
                ingest_ns = self.clock.wall_ns()
                payload = json.dumps(
                    {
                        "U": diff.first_update_id,
                        "u": diff.last_update_id,
                        "b": [[str(p), str(q)] for p, q in diff.bids],
                        "a": [[str(p), str(q)] for p, q in diff.asks],
                    },
                    separators=(",", ":"),
                )
                row = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "depth_update",
                    "symbol": self.symbol,
                    "stream": "depthUpdate",
                    "receive_ns": diff.meta.receive_wall_ns,
                    "receive_mono_ns": diff.meta.receive_mono_ns,
                    "exchange_ts_ms": diff.meta.exchange_ts_ms,
                    "sequence_first": diff.first_update_id,
                    "sequence_last": diff.last_update_id,
                    "ingest_ns": ingest_ns,
                    "payload": payload,
                }
                self.recorder.record(row)
            
            self.applied_diffs += 1
            if not self.book.ready:
                await self.synchronize()
                continue
            if on_update is not None:
                result = on_update(self.book, diff)
                if asyncio.iscoroutine(result):
                    await result

    # ------------------------------------------------------------------ служебное

    def _hints(self, diff: DepthDiff) -> dict | None:
        """Оценка исполненного объёма по публичным сделкам внутри окна дельты."""
        if self.trade_hint is None:
            return None
        since = self._last_diff_ns
        until = diff.meta.receive_mono_ns
        self._last_diff_ns = until
        if since <= 0 or until <= since:
            return None
        return self.trade_hint(since, until) or None

    @property
    def state(self) -> BookIntegrity:
        return self.book.state
