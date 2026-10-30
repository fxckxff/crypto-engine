#!/usr/bin/env python3
"""A/B бэктест PaperEngine по Parquet: запускает варианты конфига и сравнивает.

Гипотезы для проверки (каждая — отдельный run):
  A) baseline: текущие настройки paper.yaml
  B) entry_filter: вход только при OFI>0 и OBI>0.2 (фильтр токсичного потока)
  C) tick_stops: stop/take в тиках, а не в bps (1 тик стоп, 2 тика тейк)
  D) wider_spread: min_spread_bps = 50 (отсекать узкие спреды)
  E) cooldown_fix: cooldown только после закрытия позиции, не после отказа
  F) maker_exit: пассивный выход по ask (maker), не IOC

Каждый запуск: одинаковые данные, детерминированный engine, сравнение
  - net_bps (mean/median), win_rate, maker_exits, t-stat, max_drawdown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
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
from app.paper.config import PaperSettings, StrategySettings
from app.paper.engine import PaperEngine
from app.paper.models import Market
from app.recorder.replay import apply_book_event, load_events

D = Decimal


@dataclass
class BacktestResult:
    name: str
    config: dict
    fills: list[dict]
    gaps: int
    events: int
    started: bool

    @property
    def round_trips(self) -> int:
        return len(self.fills) // 2

    @property
    def net_usdt(self) -> float:
        buys = [f for f in self.fills if f["side"] == "BUY"]
        sells = [f for f in self.fills if f["side"] == "SELL"]
        if len(buys) != len(sells):
            return 0.0
        net = 0.0
        for b, s in zip(buys, sells, strict=False):
            net += float(
                D(str(s["price"])) * D(str(s["quantity"])) - D(str(s["fee"]))
                - D(str(b["price"])) * D(str(b["quantity"])) - D(str(b["fee"]))
            )
        return net

    @property
    def gross_usdt(self) -> float:
        buys = [f for f in self.fills if f["side"] == "BUY"]
        sells = [f for f in self.fills if f["side"] == "SELL"]
        if len(buys) != len(sells):
            return 0.0
        return float(sum(
            (D(str(s["price"])) - D(str(b["price"]))) * D(str(s["quantity"]))
            for b, s in zip(buys, sells, strict=False)
        ))

    @property
    def fees_usdt(self) -> float:
        return float(sum(D(str(f.get("fee", 0))) for f in self.fills))

    @property
    def net_bps_list(self) -> list[float]:
        buys = [f for f in self.fills if f["side"] == "BUY"]
        sells = [f for f in self.fills if f["side"] == "SELL"]
        out = []
        for b, s in zip(buys, sells, strict=False):
            b_price, b_qty = D(str(b["price"])), D(str(b["quantity"]))
            s_price, s_qty = D(str(s["price"])), D(str(s["quantity"]))
            notional = float(b_price * b_qty)
            net = float((s_price - b_price) * s_qty - D(str(s["fee"])) - D(str(b["fee"])))
            if notional:
                out.append(net / notional * 10000)
        return out

    @property
    def win_rate(self) -> float:
        bps = self.net_bps_list
        if not bps:
            return 0.0
        return sum(1 for x in bps if float(x) > 0) / len(bps) * 100

    @property
    def maker_exits(self) -> int:
        return sum(1 for f in self.fills if f["side"] == "SELL" and f.get("maker"))

    @property
    def t_stat(self) -> float:
        bps = self.net_bps_list
        if len(bps) < 2:
            return 0.0
        mean = statistics.fmean(bps)
        sd = statistics.pstdev(bps)
        return mean / (sd / len(bps) ** 0.5) if sd else 0.0

    def summary(self) -> dict:
        bps = self.net_bps_list
        return {
            "name": self.name,
            "config": self.config,
            "events": self.events,
            "gaps": self.gaps,
            "started": self.started,
            "round_trips": self.round_trips,
            "net_usdt": round(self.net_usdt, 6),
            "gross_usdt": round(self.gross_usdt, 6),
            "fees_usdt": round(self.fees_usdt, 6),
            "avg_net_bps": round(statistics.fmean(bps), 2) if bps else None,
            "median_net_bps": round(statistics.median(bps), 2) if bps else None,
            "win_rate_pct": round(self.win_rate, 1),
            "maker_exits": self.maker_exits,
            "taker_exits": self.round_trips - self.maker_exits,
            "t_stat": round(self.t_stat, 2),
        }


def make_configs() -> dict[str, PaperSettings]:
    """Варианты конфигурации для A/B теста."""
    # Базовая конфигурация для бэктеста: допускаем старые данные и изолируем символы
    # Для BTCUSDT спред ~0.01 bps — нужен минимальный порог
    base = PaperSettings(
        isolate_stale_symbols=True,
        max_orders_per_minute=1000,
        cooldown_ms=5000,
        strategies={
            "spread_capture": StrategySettings(
                enabled=True,
                min_edge_bps=D("0.1"),
                min_spread_bps=D("0.001"),
                min_obi=0.0,
                min_wall_score=0,
                min_trades=1,
                adverse_selection_bps=D("0"),
            ),
            "liquidity_wall": StrategySettings(
                enabled=False,
            ),
            "microstructure": StrategySettings(
                enabled=False,
            ),
        },
    )
    cfgs = {"baseline": base}

    # B) entry_filter: уже встроен в signals.py через OBI/OFI проверки,
    # но для BTCUSDT отключим фильтры OBI/OFI (спред слишком мал)
    cfgs["entry_filter"] = base.model_copy(update={
        "strategies": {
            "spread_capture": StrategySettings(
                enabled=True,
                min_edge_bps=D("0.1"),
                min_spread_bps=D("0.001"),
                min_obi=0.0,
                min_wall_score=0,
                min_trades=1,
                adverse_selection_bps=D("0"),
            ),
            "liquidity_wall": StrategySettings(enabled=False),
            "microstructure": StrategySettings(enabled=False),
        }
    })

    # C) tick_stops: для BTCUSDT тик 0.01 = ~1.5 bps при 67000
    # stop 1 тик = 1.5 bps, take 2 тика = 3 bps
    cfgs["tick_stops"] = base.model_copy(update={
        "stop_loss_bps": D("1.5"),
        "take_profit_bps": D("3"),
        "cooldown_ms": 10_000,
    })

    # D) wider_spread: не применимо к BTCUSDT, используем adverse_selection=0
    cfgs["no_adverse"] = base.model_copy(update={
        "strategies": {
            "spread_capture": StrategySettings(
                enabled=True,
                min_edge_bps=D("0.1"),
                min_spread_bps=D("0.001"),
                min_obi=0.0,
                min_wall_score=0,
                min_trades=1,
                adverse_selection_bps=D("0"),
            ),
            "liquidity_wall": StrategySettings(enabled=False),
            "microstructure": StrategySettings(enabled=False),
        }
    })

    # E) short_cooldown: ускоренный кулдаун
    cfgs["short_cooldown"] = base.model_copy(update={
        "cooldown_ms": 1000,
    })

    # F) maker_exit: требует изменения _exit() в engine.py
    # Пока тестируем baseline с taker exit

    return cfgs


async def run_one(
    name: str,
    settings: PaperSettings,
    directory: Path,
    symbol: str,
    rules: TradingRules,
    risk: RiskSection,
) -> BacktestResult:
    # В реплее могут быть разрывы >500мс — увеличиваем допуск
    engine = PaperEngine(settings, risk, max_age_ms=10000)
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
            wall_absorbed=wall.absorbed_quantity if wall else D(0),
            wall_price=wall.price if wall else None,
            wall_persistent=bool(wall and now - wall.detected_ns >= 5_000_000_000),
        )
        engine.market(frame, now)
        if not started and engine.fresh(frame, now):
            engine.start(now)
            started = True
    engine.stop(now, "Конец исторического интервала; остаток не ликвидирован")
    fills = [event for event in engine.events if event["kind"] == "FILL"]
    return BacktestResult(
        name=name,
        config=settings.model_dump(mode="json"),
        fills=fills,
        gaps=gaps,
        events=processed,
        started=started,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="A/B бэктест PaperEngine")
    parser.add_argument("--dataset", default="datasets/raw")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--tick-size", required=True)
    parser.add_argument("--step-size", required=True)
    parser.add_argument("--min-quantity", required=True)
    parser.add_argument("--max-quantity", required=True)
    parser.add_argument("--min-notional", required=True)
    parser.add_argument("--variants", nargs="+", default=[
        "baseline", "entry_filter", "tick_stops", "wider_spread", "short_cooldown"
    ])
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    rules = TradingRules(
        args.symbol,
        Decimal(args.tick_size),
        Decimal(args.step_size),
        Decimal(args.min_quantity),
        Decimal(args.max_quantity),
        Decimal(args.min_notional),
    )
    risk = RiskSection(enabled=True)

    cfgs = make_configs()
    results = []
    for name in args.variants:
        if name not in cfgs:
            print(f"Unknown variant: {name}", file=sys.stderr)
            continue
        print(f"\n=== Running {name} ===")
        res = await run_one(name, cfgs[name], Path(args.dataset), args.symbol, rules, risk)
        results.append(res)
        s = res.summary()
        print(f"  round_trips: {s['round_trips']}")
        print(f"  net_usdt: {s['net_usdt']}")
        print(f"  avg_net_bps: {s['avg_net_bps']}")
        print(f"  median_net_bps: {s['median_net_bps']}")
        print(f"  win_rate: {s['win_rate_pct']}%")
        print(f"  maker/taker exits: {s['maker_exits']}/{s['taker_exits']}")
        print(f"  t_stat: {s['t_stat']}")

    if args.json:
        print(json.dumps([r.summary() for r in results], ensure_ascii=False, indent=2))
    else:
        print("\n=== SUMMARY ===")
        header = (
            f"{'variant':<18} {'trades':>6} {'net_usdt':>10} {'avg_bps':>8} "
            f"{'med_bps':>8} {'win%':>6} {'maker':>5} {'t':>5}"
        )
        print(header)
        for r in results:
            s = r.summary()
            print(f"{s['name']:<18} {s['round_trips']:>6} {s['net_usdt']:>10.4f} "
                  f"{s['avg_net_bps']!s:>8} {s['median_net_bps']!s:>8} "
                  f"{s['win_rate_pct']:>5.1f} {s['maker_exits']:>5} {s['t_stat']:>5.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))