"""Broad USDT spot screen; depth trading remains limited to a selected working set."""

from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from app.exchanges.binance.rest import BinanceRestClient


async def fetch_universe(rest: BinanceRestClient) -> list[dict]:
    info = await rest.exchange_info()
    allowed = {
        s["symbol"]
        for s in info["symbols"]
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING" and s.get("isSpotTradingAllowed", False)
    }
    ticks = await rest._request("GET", "/api/v3/ticker/24hr", weight=80)
    now = time.time_ns()
    rows = []
    for t in ticks:
        if t["symbol"] not in allowed:
            continue
        bid, ask = Decimal(t["bidPrice"]), Decimal(t["askPrice"])
        spread = float((ask - bid) / ((ask + bid) / 2) * 100) if ask >= bid > 0 else None
        rows.append(
            {
                "symbol": t["symbol"],
                "price": t["lastPrice"],
                "change_pct": float(t["priceChangePercent"]),
                "volume_usdt": float(t["quoteVolume"]),
                "trades": int(t["count"]),
                "spread_pct": spread,
                "top_depth_usdt": float(min(bid * Decimal(t["bidQty"]), ask * Decimal(t["askQty"]))),
                "updated_ns": now,
            }
        )
    return sorted(rows, key=lambda row: row["trades"], reverse=True)


def select_candidates(rows: list[dict], existing: list[str], count: int, low: bool) -> list[str]:
    candidates = [
        r
        for r in rows
        if r["symbol"] not in existing
        and r["trades"] >= 1000
        and r["volume_usdt"] >= 100000
        and r["spread_pct"] is not None
        and 0.05 <= r["spread_pct"] <= 1.0
        and r["top_depth_usdt"] > 0
    ]
    # Best quote size is a screening proxy, not full +/-1% order-book depth.
    candidates.sort(key=lambda r: (r["top_depth_usdt"] if low else -r["spread_pct"], -r["trades"]))
    return [r["symbol"] for r in candidates[:count]]


async def discover_config(config):
    if not config.scanner.universe_enabled or not config.scanner.discovery_slots:
        return config
    # Keep every held symbol across discovery changes and service restarts.
    existing = list(config.exchange.symbols)
    database = Path(config.paper.database_path)
    if config.app.mode == "paper" and database.exists():
        conn = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT payload FROM state WHERE id=1").fetchone()
            if row:
                for symbol in json.loads(row[0])["positions"]:
                    if symbol not in existing:
                        existing.append(symbol)
        finally:
            conn.close()
    rest = BinanceRestClient(config.exchange.rest_base_url, requests_per_minute=config.exchange.max_requests_per_minute)
    try:
        await rest.start()
        rows = await fetch_universe(rest)
        selected = select_candidates(
            rows, existing, config.scanner.discovery_slots, config.scanner.discovery_low_liquidity
        )
        exchange = config.exchange.model_copy(update={"symbols": [*existing, *selected]})
        return config.model_copy(update={"exchange": exchange})
    finally:
        await rest.close()
