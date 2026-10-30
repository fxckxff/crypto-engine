"""Тесты для скриптов Replay и Backtest (PHASE 3-4)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scripts.backtest import run_backtest
from scripts.replay import run_replay


@pytest.fixture
def sample_parquet_dataset(tmp_path: Path) -> Path:
    """Создает временный датасет с несколькими событиями для тестов."""
    dataset_dir = tmp_path / "raw"
    events_dir = dataset_dir / "raw_events"
    events_dir.mkdir(parents=True)

    # Схема совпадает с ParquetRecorder (см. raw_event_schema)
    schema = pa.schema([
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
    ])

    rows = [
        # 1. Snapshot
        {
            "schema_version": "1",
            "kind": "depth_snapshot",
            "symbol": "BTCUSDT",
            "stream": "depth",
            "receive_ns": 1000000000,
            "receive_mono_ns": 1000000,
            "exchange_ts_ms": 1000,
            "sequence_first": 100,
            "sequence_last": 100,
            "ingest_ns": 1000000000,
            "payload": json.dumps({
                "lastUpdateId": 100,
                "bids": [["100.0", "1.0"]],
                "asks": [["101.0", "1.0"]],
            }),
        },
        # 2. Trade
        {
            "schema_version": "1",
            "kind": "agg_trade",
            "symbol": "BTCUSDT",
            "stream": "aggTrade",
            "receive_ns": 2000000000,
            "receive_mono_ns": 2000000,
            "exchange_ts_ms": 2000,
            "sequence_first": 101,
            "sequence_last": 101,
            "ingest_ns": 2000000000,
            "payload": json.dumps({
                "p": "100.5",
                "q": "0.1",
                "aggressor": "BUY",
            }),
        },
    ]

    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(table, events_dir / "raw_events-20260101T000000-00001.parquet")
    
    # Добавляем второй "пустой" файл для проверки исключения активных файлов
    pq.write_table(table.slice(0, 0), events_dir / "raw_events-20260101T000001-00002.parquet")

    return dataset_dir


@pytest.mark.asyncio
async def test_replay_script_execution(sample_parquet_dataset: Path) -> None:
    result = await run_replay(sample_parquet_dataset, "BTCUSDT")
    assert result["symbol"] == "BTCUSDT"
    assert result["events_processed"] == 2
    assert result["final_book_state"] == "READY"


@pytest.mark.asyncio
async def test_backtest_script_execution(sample_parquet_dataset: Path) -> None:
    config = {"initial_cash": 10000}
    result = await run_backtest(sample_parquet_dataset, "BTCUSDT", "simple_market_making", config)
    assert result["symbol"] == "BTCUSDT"
    assert result["strategy"] == "simple_market_making"
    assert "return_pct" in result
