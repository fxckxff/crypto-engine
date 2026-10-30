#!/usr/bin/env python3
"""Replay completed recordings in application order, with event-time clocks."""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path

from app.analytics.engine import FeatureEngine
from app.analytics.flow import Trade
from app.analytics.walls import WallConfig, WallDetector
from app.core.enums import Aggressor
from app.core.time import FakeClock
from app.market_data.orderbook import LocalOrderBook
from app.recorder.replay import apply_book_event, load_events


async def run_replay(
    dataset_dir: Path,
    symbol: str,
    start_ns: int | None = None,
    end_ns: int | None = None,
    tick_size: Decimal = Decimal("0.01"),
) -> dict:
    if start_ns is not None and end_ns is not None and start_ns > end_ns:
        raise ValueError("start must be <= end")
    clock = FakeClock(monotonic_ns=0)
    book = LocalOrderBook(symbol, depth=200, max_levels=200, stale_after_ms=60000, clock=clock)
    engine = FeatureEngine(symbol=symbol, tick_size=tick_size)
    walls = WallDetector(symbol, WallConfig())
    processed = snapshots = wall_events = gaps = 0
    last_feature_ns = 0
    for row in load_events(dataset_dir, symbol):
        ts = row["ingest_ns"]
        if end_ns is not None and ts > end_ns:
            break
        # UNIX ingestion time provides a monotonic replay axis across recording sessions.
        clock._wall = clock._mono = ts
        if not apply_book_event(book, row, ts):
            gaps += 1
        if row["kind"] == "agg_trade":
            payload = json.loads(row["payload"])
            engine.on_trade(
                Trade(
                    symbol=symbol,
                    price=Decimal(payload["p"]),
                    quantity=Decimal(payload["q"]),
                    aggressor=Aggressor(payload["aggressor"]),
                    exchange_ts_ms=row["exchange_ts_ms"],
                    receive_ns=row["receive_ns"],
                    receive_mono_ns=ts,
                )
            )
        # Match the default live feature cadence without dropping source events.
        computed = ts - last_feature_ns >= 100_000_000
        transitions = []
        if computed:
            snapshot = engine.update(book, ts)
            last_feature_ns = ts
            transitions = walls.update(book, ts) if snapshot.tradable else []
        # Events before start warm up state, but aren't counted in the requested interval.
        if start_ns is None or ts >= start_ns:
            processed += 1
            snapshots += int(computed and snapshot.tradable)
            wall_events += len(transitions)
    return {
        "symbol": symbol,
        "events_processed": processed,
        "snapshots": snapshots,
        "wall_transitions": wall_events,
        "sequence_gaps": gaps,
        "final_book_state": book.state.value,
        "final_book_levels": book.levels_loaded,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay completed Parquet recordings")
    parser.add_argument("--dataset", default="datasets/raw")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", type=int, help="Ingestion UNIX timestamp, ns (inclusive)")
    parser.add_argument("--end", type=int, help="Ingestion UNIX timestamp, ns (inclusive)")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_replay(Path(args.dataset), args.symbol, args.start, args.end)), indent=2))
