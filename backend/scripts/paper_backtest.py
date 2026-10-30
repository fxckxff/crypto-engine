"""Historical runner using exactly the PaperEngine strategies, risk and execution model."""

from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path

from app.analytics.engine import FeatureEngine
from app.analytics.flow import Trade
from app.analytics.walls import WallDetector
from app.config.schema import RiskSection
from app.core.enums import Aggressor, Side
from app.core.time import FakeClock
from app.exchanges.base import TradingRules
from app.market_data.orderbook import LocalOrderBook
from app.paper.config import PaperSettings
from app.paper.engine import PaperEngine
from app.paper.models import Market
from app.recorder.replay import apply_book_event, load_events


async def run_paper_backtest(
    directory: Path,
    symbol: str,
    rules: TradingRules,
    settings: PaperSettings | None = None,
    risk: RiskSection | None = None,
) -> dict:
    engine = PaperEngine(settings or PaperSettings(), risk or RiskSection(enabled=True))
    clock = FakeClock()
    book = LocalOrderBook(symbol, clock=clock)
    features = FeatureEngine(symbol, tick_size=rules.tick_size)
    walls = WallDetector(symbol)
    started = False
    gaps = processed = 0
    now = 0
    for row in load_events(directory, symbol):
        now = row["ingest_ns"]
        clock._mono = clock._wall = now
        processed += 1
        if not apply_book_event(book, row, now):
            gaps += 1
        if row["kind"] == "depth_snapshot":
            walls = WallDetector(symbol)
        if row["kind"] == "agg_trade":
            payload = json.loads(row["payload"])
            trade = Trade(
                symbol,
                Decimal(payload["p"]),
                Decimal(payload["q"]),
                Aggressor(payload["aggressor"]),
                row["exchange_ts_ms"],
                row["receive_ns"],
                now,
                payload.get("id"),
            )
            features.on_trade(trade)
            walls._apply_trades([trade], now)
            if trade.trade_id is not None:
                engine.trade(
                    symbol,
                    trade.trade_id,
                    trade.price,
                    trade.quantity,
                    trade.aggressor.value,
                    now,
                    trade.exchange_ts_ms * 1_000_000 if trade.exchange_ts_ms else None,
                )
            elif engine.active(symbol):
                engine.stop(now, "Recorded trade has no ID; cannot deduplicate execution")
                gaps += 1
        snapshot = features.update(book, now)
        walls.update(book, now)
        candidates = [w for w in walls.active_walls() if w.side == Side.BID]
        wall = max(candidates, key=lambda w: w.score) if candidates else None
        frame = Market(
            symbol,
            book.last_update_ns,
            book.last_update_id or 0,
            snapshot.tradable,
            tuple(book.snapshot_levels(50)[0]),
            tuple(book.snapshot_levels(50)[1]),
            rules,
            obi=snapshot.obi.get(5) or 0,
            ofi=(snapshot.ofi.rolling or 0) if snapshot.ofi else 0,
            microprice_bps=snapshot.microprice.deviation_bps if snapshot.microprice else 0,
            buy_volume=snapshot.flow.buy_volume,
            sell_volume=snapshot.flow.sell_volume,
            trades=snapshot.flow.trade_count,
            wall_score=wall.score if wall else 0,
            wall_absorbed=wall.absorbed_quantity if wall else Decimal(0),
            wall_price=wall.price if wall else None,
            wall_persistent=bool(wall and now - wall.detected_ns >= 5_000_000_000),
        )
        engine.market(frame, now)
        if not started and engine.fresh(frame, now):
            engine.start(now)
            started = True
    engine.stop(now, "Конец исторического интервала; остаток не ликвидирован")
    result = engine.summary(now)
    fills = [event for event in engine.events if event["kind"] == "FILL"]
    return {
        "symbol": symbol,
        "engine": "PaperEngine",
        "events_processed": processed,
        "gaps": gaps,
        "status": "research_only" if started and not gaps else "incomplete_data",
        "fills": fills,
        "fill_count": len(fills),
        "account": result,
        "assumptions": [
            "Instrument filters are explicit caller inputs, not verified historical rules",
            "All unclosed inventory remains marked; final liquidation is not assumed",
            "Historical ingestion time and L2 execution estimates; no profitability validation",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest with the same engine as Paper Trading")
    parser.add_argument("--dataset", default="datasets/raw")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--tick-size", required=True)
    parser.add_argument("--step-size", required=True)
    parser.add_argument("--min-quantity", required=True)
    parser.add_argument("--max-quantity", required=True)
    parser.add_argument("--min-notional", required=True)
    args = parser.parse_args()
    rules = TradingRules(
        args.symbol,
        Decimal(args.tick_size),
        Decimal(args.step_size),
        Decimal(args.min_quantity),
        Decimal(args.max_quantity),
        Decimal(args.min_notional),
    )
    print(
        json.dumps(
            asyncio.run(run_paper_backtest(Path(args.dataset), args.symbol, rules)), indent=2, ensure_ascii=False
        )
    )
