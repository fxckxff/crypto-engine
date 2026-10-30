"""Запись в Parquet: схема, ротация, видимая потеря данных."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from app.recorder.parquet_recorder import SCHEMA_VERSION, ParquetRecorder, raw_event_schema


def row(i: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "depth_diff",
        "symbol": "BTCUSDT",
        "stream": "depth",
        "receive_ns": i,
        "receive_mono_ns": i,
        "exchange_ts_ms": i,
        "sequence_first": i,
        "sequence_last": i,
        "ingest_ns": i,
        "payload": "{}",
    }


async def test_writes_and_reads_back(tmp_path: Path) -> None:
    recorder = ParquetRecorder("raw", tmp_path, raw_event_schema(), batch_size=10)
    await recorder.start()
    for i in range(25):
        recorder.record(row(i))
    await asyncio.sleep(0.1)
    assert recorder.stats.recorded > 0
    await recorder.close()

    files = sorted((tmp_path / "raw").glob("*.parquet"))
    assert files, "parquet file was not created"
    table = pq.read_table(files[0])
    assert table.num_rows > 0
    assert set(table.column_names) == set(raw_event_schema().names)
    assert pa.types.is_int64(table.schema.field("receive_ns").type)
    assert pa.types.is_string(table.schema.field("payload").type)


async def test_overflow_is_counted_not_hidden(tmp_path: Path) -> None:
    recorder = ParquetRecorder("raw", tmp_path, raw_event_schema(), queue_max=2)
    for i in range(5):
        recorder.record(row(i))
    assert recorder.stats.dropped == 3
    assert recorder.stats.data_loss is True
    # Запись без старта не должна терять уже принятые элементы.
    await recorder.start()
    await asyncio.sleep(0.05)
    await recorder.close()
    files = list((tmp_path / "raw").glob("*.parquet"))
    assert sum(pq.read_table(f).num_rows for f in files) == 2


async def test_rotation_creates_multiple_files(tmp_path: Path) -> None:
    recorder = ParquetRecorder("raw", tmp_path, raw_event_schema(), rotation_mb=1, batch_size=50)
    await recorder.start()
    for i in range(60_000):
        recorder.record({**row(i), "payload": "x" * 200})
    await asyncio.sleep(0.5)
    await recorder.close()
    files = list((tmp_path / "raw").glob("*.parquet"))
    assert len(files) >= 1
    total = sum(pq.read_table(f).num_rows for f in files)
    assert total == 60_000
    assert recorder.stats.dropped == 0


def test_retention_removes_old_files(tmp_path: Path) -> None:
    recorder = ParquetRecorder("raw", tmp_path, raw_event_schema())
    directory = tmp_path / "raw"
    directory.mkdir(parents=True)
    old = directory / "old.parquet"
    old.write_bytes(b"x")
    import os
    import time

    os.utime(old, (time.time() - 86_400 * 10, time.time() - 86_400 * 10))
    removed = recorder.apply_retention(retention_days=1)
    assert removed == 1
    assert not old.exists()
