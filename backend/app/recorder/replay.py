"""Deterministic application-order replay of completed recording files."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.dataset as ds

from app.core.errors import SequenceGap, SnapshotRequired
from app.market_data.orderbook import LocalOrderBook
from app.recorder.parquet_reader import ParquetReader


def load_events(directory: Path, symbol: str) -> list[dict[str, Any]]:
    dataset = ParquetReader(directory).load_dataset("raw_events")
    rows = dataset.to_table(filter=ds.field("symbol") == symbol.upper()).to_pylist()
    # Stable sort by ingestion, NOT exchange time: buffered deltas were applied
    # after the snapshot, even when their network receive time preceded it.
    rows.sort(key=lambda row: row["ingest_ns"])
    return rows


def apply_book_event(book: LocalOrderBook, row: dict[str, Any], now_ns: int) -> bool:
    payload = json.loads(row["payload"])
    kind = row["kind"]
    def levels(key, fallback):
        return [(Decimal(p), Decimal(q)) for p, q in payload.get(key, payload.get(fallback, []))]
    if kind == "depth_snapshot":
        book.apply_snapshot(
            levels("bids", "b"), levels("asks", "a"), payload["lastUpdateId"], now_ns, row["exchange_ts_ms"]
        )
    elif kind in ("depth_update", "depth_diff"):
        try:
            book.apply_diff(
                payload.get("U", row["sequence_first"]),
                payload.get("u", row["sequence_last"]),
                levels("b", "bids"),
                levels("a", "asks"),
                now_ns,
                row["exchange_ts_ms"],
            )
        except (SequenceGap, SnapshotRequired):
            book.invalidate("recording sequence gap; waiting for next snapshot")
            return False
    return True
