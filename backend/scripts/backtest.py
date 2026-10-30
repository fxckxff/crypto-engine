#!/usr/bin/env python3
"""Conservative L2 research baseline; no claim of exchange-grade execution fidelity."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.core.time import FakeClock
from app.market_data.orderbook import LocalOrderBook
from app.recorder.replay import apply_book_event, load_events

D = Decimal


@dataclass
class PassiveOrder:
    side: str
    price: Decimal
    remaining: Decimal
    queue_ahead: Decimal
    active_ns: int
    expires_ns: int


async def run_backtest(dataset_dir: Path, symbol: str, strategy_name: str, config: dict) -> dict:
    if strategy_name != "simple_market_making":
        raise ValueError(f"unsupported strategy: {strategy_name}")
    initial_cash = D(str(config.get("initial_cash", 10000)))
    size = D(str(config.get("order_size", "0.001")))
    fee_rate = D(str(config.get("maker_fee_bps", 10))) / 10000
    min_spread_bps = D(str(config.get("min_spread_bps", 25)))
    latency_ns = int(config.get("latency_ms", 100)) * 1_000_000
    ttl_ns = int(config.get("order_ttl_ms", 5000)) * 1_000_000
    max_position = D(str(config.get("max_position_usdt", 50)))
    if initial_cash <= 0 or size <= 0 or fee_rate < 0 or latency_ns < 0 or ttl_ns <= latency_ns or max_position <= 0:
        raise ValueError("invalid cash, size, fees, latency, lifetime or position limit")
    cash, position, fees = initial_cash, D(0), D(0)
    clock = FakeClock()
    book = LocalOrderBook(symbol, clock=clock)
    pending: PassiveOrder | None = None
    trades: list[dict] = []
    gaps = events = 0
    last_mid: Decimal | None = None
    peak = initial_cash
    max_drawdown = D(0)
    for row in load_events(dataset_dir, symbol):
        now = row["ingest_ns"]
        clock._wall = clock._mono = now
        events += 1
        if not apply_book_event(book, row, now):
            gaps += 1
        book.refresh_state(now)
        if not book.ready:
            pending = None
            continue
        if row["kind"] == "depth_snapshot":
            # A new snapshot ends continuity assumptions about queue position.
            pending = None
        if pending and now >= pending.expires_ns:
            pending = None
        # Fill only orders created on EARLIER events, after submission latency.
        if pending and row["kind"] == "agg_trade" and now >= pending.active_ns:
            payload = json.loads(row["payload"])
            price, volume = D(payload["p"]), D(payload["q"])
            matched = (pending.side == "BUY" and payload["aggressor"] == "SELL") or (
                pending.side == "SELL" and payload["aggressor"] == "BUY"
            )
            # Deliberately conservative: only executed volume at our price advances
            # the queue; cancellations and trades at other prices do not fill us.
            if matched and price == pending.price:
                consumed = min(volume, pending.queue_ahead)
                pending.queue_ahead -= consumed
                fill = min(pending.remaining, volume - consumed)
                if fill > 0:
                    notional = fill * pending.price
                    fee = notional * fee_rate
                    if pending.side == "BUY":
                        cash -= notional + fee
                        position += fill
                    else:
                        cash += notional - fee
                        position -= fill
                    fees += fee
                    pending.remaining -= fill
                    trades.append(
                        {
                            "side": pending.side,
                            "price": str(pending.price),
                            "size": str(fill),
                            "fee": str(fee),
                            "ts": now,
                        }
                    )
                    if pending.remaining == 0:
                        pending = None
        bid, ask = book.best_bid(), book.best_ask()
        assert bid is not None and ask is not None
        last_mid = (bid.price + ask.price) / 2
        equity = cash + position * bid.price  # executable-side mark, no forced liquidation
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        # New decisions are made after processing this event, never filled by it.
        if pending is None:
            if position > 0:
                pending = PassiveOrder(
                    "SELL", ask.price, min(size, position), ask.quantity, now + latency_ns, now + ttl_ns
                )
            elif (ask.price - bid.price) / last_mid * 10000 >= max(min_spread_bps, 2 * fee_rate * 10000):
                quantity = min(size, cash / (bid.price * (1 + fee_rate)), max_position / bid.price)
                if quantity > 0:
                    pending = PassiveOrder("BUY", bid.price, quantity, bid.quantity, now + latency_ns, now + ttl_ns)
    bid = book.best_bid()
    mark = bid.price if bid else last_mid
    final_equity = cash + position * (mark or D(0))
    valid = gaps == 0 and book.ready and events > 0
    return {
        "symbol": symbol,
        "strategy": strategy_name,
        "events_processed": events,
        "status": "research_only" if valid else "incomplete_data",
        "sequence_gaps": gaps,
        "total_trades": len(trades),
        "final_position": float(position),
        "final_cash": float(cash),
        "final_equity": float(final_equity),
        "fees_paid": float(fees),
        "return_pct": float((final_equity - initial_cash) / initial_cash * 100) if valid else None,
        "max_drawdown_pct": float(max_drawdown * 100),
        "trades": trades,
        "assumptions": {
            "maker_fee_bps": str(fee_rate * 10000),
            "latency_ms": latency_ns // 1_000_000,
            "queue_model": "displayed volume ahead; no cancellation credit; exact-price trades only",
            "valuation": "remaining inventory marked at last known bid; not liquidated",
        },
        "limitations": [
            "L2 does not reveal exact queue priority or hidden liquidity",
            "No historical instrument-filter validation or independent out-of-sample evaluation",
            "Research baseline only; not proof of profitability or live readiness",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Conservative L2 research backtest")
    parser.add_argument("--dataset", default="datasets/raw")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--strategy", default="simple_market_making")
    parser.add_argument("--cash", type=float, default=10000)
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(run_backtest(Path(args.dataset), args.symbol, args.strategy, {"initial_cash": args.cash})),
            indent=2,
        )
    )
