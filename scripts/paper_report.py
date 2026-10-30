#!/usr/bin/env python3
"""Атрибуция paper-сессии: что стратегия обещала и что счёт получил.

Читает SQLite-файл PaperStore (таблицы state/events) только на чтение и считает
реализованный результат по FIFO-парам fill'ов: валовый результат, комиссии,
время удержания, доля maker/taker, разбивка по стратегии и символу.

Пример:
    PYTHONPATH=backend .venv/bin/python -m scripts.paper_report \
        --account datasets/low-liquidity/session-20261030-1121.sqlite3
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

D = Decimal


@dataclass
class Lot:
    quantity: Decimal
    price: Decimal
    fee: Decimal
    ts_ns: int


@dataclass
class RoundTrip:
    symbol: str
    strategy: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    entry_ns: int
    exit_ns: int
    maker_exit: bool

    @property
    def notional(self) -> Decimal:
        return self.entry_price * self.quantity

    @property
    def gross(self) -> Decimal:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def fees(self) -> Decimal:
        return self.entry_fee + self.exit_fee

    @property
    def net(self) -> Decimal:
        return self.gross - self.fees

    @property
    def net_bps(self) -> float:
        return float(self.net / self.notional * 10000) if self.notional else 0.0

    @property
    def gross_bps(self) -> float:
        return float(self.gross / self.notional * 10000) if self.notional else 0.0

    @property
    def hold_seconds(self) -> float:
        return (self.exit_ns - self.entry_ns) / 1e9


@dataclass
class Report:
    path: str
    events: int
    round_trips: list[RoundTrip] = field(default_factory=list)
    exit_reasons: Counter = field(default_factory=Counter)
    rejected: Counter = field(default_factory=Counter)
    decisions_allowed: int = 0
    decisions_total: int = 0
    claimed_edge_bps: list[float] = field(default_factory=list)
    open_lots: int = 0

    @property
    def net(self) -> Decimal:
        return sum((t.net for t in self.round_trips), D(0))

    @property
    def gross(self) -> Decimal:
        return sum((t.gross for t in self.round_trips), D(0))

    @property
    def fees(self) -> Decimal:
        return sum((t.fees for t in self.round_trips), D(0))

    def summary(self) -> dict:
        trips = self.round_trips
        wins = [t for t in trips if t.net > 0]
        return {
            "file": self.path,
            "events": self.events,
            "round_trips": len(trips),
            "open_lots": self.open_lots,
            "net_usdt": str(round(self.net, 6)),
            "gross_usdt": str(round(self.gross, 6)),
            "fees_usdt": str(round(self.fees, 6)),
            "fees_share_of_gross_pct": (
                str(round(float(self.fees / abs(self.gross) * 100), 1)) if self.gross else None
            ),
            "net_per_trade_usdt": str(round(self.net / len(trips), 6)) if trips else None,
            "avg_net_bps": round(statistics.fmean(t.net_bps for t in trips), 2) if trips else None,
            "avg_gross_bps": round(statistics.fmean(t.gross_bps for t in trips), 2) if trips else None,
            "median_net_bps": round(statistics.median(t.net_bps for t in trips), 2) if trips else None,
            "win_rate_pct": round(len(wins) / len(trips) * 100, 1) if trips else None,
            "median_hold_seconds": round(statistics.median(t.hold_seconds for t in trips), 1) if trips else None,
            "maker_exits": sum(1 for t in trips if t.maker_exit),
            "taker_exits": sum(1 for t in trips if not t.maker_exit),
            "exit_reasons": dict(self.exit_reasons),
            "by_strategy": self._group(lambda t: t.strategy),
            "by_symbol": self._group(lambda t: t.symbol),
            "decisions": {"total": self.decisions_total, "allowed": self.decisions_allowed},
            "claimed_edge_bps": (
                round(statistics.fmean(self.claimed_edge_bps), 2) if self.claimed_edge_bps else None
            ),
            "rejections": dict(self.rejected),
        }

    def _group(self, key) -> list[dict]:
        groups: dict[str, list[RoundTrip]] = defaultdict(list)
        for trip in self.round_trips:
            groups[key(trip)].append(trip)
        rows = []
        for name, trips in sorted(groups.items(), key=lambda item: sum((t.net for t in item[1]), D(0))):
            rows.append(
                {
                    "name": name,
                    "trades": len(trips),
                    "net_usdt": str(round(sum((t.net for t in trips), D(0)), 6)),
                    "avg_net_bps": round(statistics.fmean(t.net_bps for t in trips), 2),
                }
            )
        return rows


def pair_fills(fill_events: list[dict]) -> tuple[list[RoundTrip], int]:
    """FIFO-склейка fill'ов в круглые сделки: вход и выход с раздельными комиссиями."""
    lots: dict[str, list[Lot]] = defaultdict(list)
    trips: list[RoundTrip] = []
    open_lots = 0
    for event in fill_events:
        symbol = event["symbol"]
        quantity, price = D(event["quantity"]), D(event["price"])
        fee = D(event["fee"])
        if event["side"] == "BUY":
            lots[symbol].append(Lot(quantity, price, fee, event["ts_ns"]))
            continue
        remaining = quantity
        while remaining > 0 and lots[symbol]:
            lot = lots[symbol][0]
            take = min(remaining, lot.quantity)
            share = take / lot.quantity
            entry_fee = lot.fee * share
            exit_fee = fee * (take / quantity) if quantity else D(0)
            trips.append(
                RoundTrip(
                    symbol=symbol,
                    strategy=event.get("strategy", "unknown"),
                    quantity=take,
                    entry_price=lot.price,
                    exit_price=price,
                    entry_fee=entry_fee,
                    exit_fee=exit_fee,
                    entry_ns=lot.ts_ns,
                    exit_ns=event["ts_ns"],
                    maker_exit=bool(event.get("maker")),
                )
            )
            lot.quantity -= take
            lot.fee -= entry_fee
            remaining -= take
            if lot.quantity <= 0:
                lots[symbol].pop(0)
        if remaining > 0:
            # Sell without recorded inventory: the session was restored mid-position.
            open_lots += 1
    open_lots += sum(len(symbol_lots) for symbol_lots in lots.values())
    return trips, open_lots


def load_report(path: Path) -> Report:
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        (events,) = conn.execute("select count(*) from events").fetchone()
        report = Report(str(path), events)
        # Aggregates stay in SQL: long sessions hold millions of DECISION rows.
        report.decisions_total, report.decisions_allowed, edge_sum = conn.execute(
            "select count(*), coalesce(sum(json_extract(payload, '$.allowed') = 1), 0), "
            "coalesce(sum(case when json_extract(payload, '$.allowed') then "
            "cast(json_extract(payload, '$.edge_bps') as real) else 0 end), 0) "
            "from events where kind='DECISION'"
        ).fetchone()
        report.claimed_edge_bps = [edge_sum / report.decisions_allowed] if report.decisions_allowed else []
        rejected_sql = (
            "select coalesce(json_extract(payload, '$.order.reason'), "
            "json_extract(payload, '$.reason')) as reason, count(*) "
            "from events where kind='REJECTED' group by reason"
        )
        report.rejected = Counter(dict(conn.execute(rejected_sql).fetchall()))
        exit_sql = (
            "select json_extract(payload, '$.reason') as reason, count(*) "
            "from events where kind='POSITION_CLOSED' group by reason"
        )
        report.exit_reasons = Counter(dict(conn.execute(exit_sql).fetchall()))
        rows = [
            json.loads(payload)
            for (payload,) in conn.execute(
                "select payload from events where kind='FILL' order by sequence"
            )
        ]
    finally:
        conn.close()
    trips, open_lots = pair_fills(rows)
    report.round_trips = trips
    report.open_lots = open_lots
    return report


def render(summary: dict) -> str:
    def usdt(value) -> str:
        return "—" if value is None else f"{float(value):+.4f}"

    def _row(row: dict) -> str:
        return (
            f"  {row['name']:<18} {row['trades']:>4} сделок"
            f"  {usdt(row['net_usdt'])}  {row['avg_net_bps']:>8} bps"
        )

    lines = [
        f"Файл: {summary['file']}",
        f"Событий: {summary['events']}",
        "",
        f"Круглых сделок: {summary['round_trips']} (незакрытых лотов: {summary['open_lots']})",
        f"Результат: {usdt(summary['net_usdt'])} USDT "
        f"(валовый {usdt(summary['gross_usdt'])}, комиссии {float(summary['fees_usdt']):.4f})",
        f"Средняя сделка: {summary['avg_net_bps']} bps чистыми, {summary['avg_gross_bps']} bps валовыми",
        f"Win rate: {summary['win_rate_pct']}% | медиана удержания: {summary['median_hold_seconds']} с",
        f"Выходы: maker {summary['maker_exits']}, taker {summary['taker_exits']}",
        f"Причины выхода: {summary['exit_reasons']}",
        "",
        f"Решения: {summary['decisions']['allowed']} разрешено из {summary['decisions']['total']} "
        f"(заявленный edge: {summary['claimed_edge_bps']} bps)",
        f"Отказы: {summary['rejections']}",
        "",
        "По стратегиям:",
    ]
    for row in summary["by_strategy"]:
        lines.append(_row(row))
    lines.append("")
    lines.append("По символам:")
    for row in summary["by_symbol"]:
        lines.append(_row(row))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Атрибуция paper-сессии (только чтение)")
    parser.add_argument("--account", default="datasets/paper/account.sqlite3", help="SQLite-файл PaperStore")
    parser.add_argument("--json", action="store_true", help="Вывести машинночитаемый отчёт")
    args = parser.parse_args()
    path = Path(args.account)
    if not path.exists():
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 2
    report = load_report(path)
    if args.json:
        print(json.dumps(report.summary(), ensure_ascii=False, indent=2))
    else:
        print(render(report.summary()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
