"""Тестовые дублёры адаптера: воспроизведение записанного потока без сети."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from app.exchanges.base import ExchangeAdapter, Order, OrderFill, OrderRequest, TradingFees, TradingRules
from app.exchanges.binance.protocol import parse_depth_snapshot, parse_depth_update
from app.market_data.events import DepthDiff, DepthSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "binance"


def load_snapshot(symbol: str = "BTCUSDT") -> DepthSnapshot:
    payload = json.loads((FIXTURES / f"{symbol}_snapshot.json").read_text(encoding="utf-8"))
    return parse_depth_snapshot(payload, symbol, receive_ns=1)


def load_diffs(symbol: str = "BTCUSDT") -> list[DepthDiff]:
    path = FIXTURES / f"{symbol}_depth_stream.jsonl"
    diffs: list[DepthDiff] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        envelope = json.loads(line)
        diffs.append(parse_depth_update(envelope["data"], receive_ns=i, receive_mono_ns=i))
    return diffs


def load_trades(symbol: str = "BTCUSDT") -> list[dict]:
    path = FIXTURES / f"{symbol}_trades.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class ReplayAdapter(ExchangeAdapter):
    """Отдаёт записанные события. Полностью повторяем, никакой случайности."""

    name = "replay"

    def __init__(
        self,
        snapshot: DepthSnapshot,
        diffs: list[DepthDiff],
        drop_ids: set[int] | None = None,
        delay_s: float = 0.0,
        snapshots: list[DepthSnapshot] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self._snapshots = list(snapshots or [])
        self.diffs = diffs
        self.drop_ids = drop_ids or set()
        self.delay_s = delay_s
        self.snapshot_calls = 0
        self.exhausted = False

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_order_book_snapshot(self, symbol: str, limit: int = 200) -> DepthSnapshot:
        self.snapshot_calls += 1
        if self._snapshots:
            # Биржа на повторный запрос отдаёт уже актуальный снимок.
            if len(self._snapshots) > 1:
                return self._snapshots.pop(0)
            return self._snapshots[0]
        return self.snapshot

    async def stream_order_book(self, symbol: str, depth: int = 200) -> AsyncIterator[DepthDiff]:
        for i, diff in enumerate(self.diffs):
            if i in self.drop_ids:
                continue
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield diff
        self.exhausted = True

    async def stream_trades(self, symbol: str):  # type: ignore[override]
        if False:  # pragma: no cover - генератор нужен только для типов
            yield None

    async def get_quote(self, symbol: str):  # type: ignore[override]
        raise NotImplementedError

    async def get_instruments(self):
        return {}

    async def get_trading_rules(self, symbol: str) -> TradingRules:
        return TradingRules(
            symbol=symbol,
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.00001"),
            min_quantity=Decimal("0.00001"),
            max_quantity=Decimal("9000"),
            min_notional=Decimal("5"),
        )

    async def get_account_commissions(self) -> TradingFees:
        raise NotImplementedError

    async def get_balances(self):  # type: ignore[override]
        return []

    async def place_order(self, request: OrderRequest) -> Order:  # pragma: no cover - реплей не торгует
        raise NotImplementedError("replay adapter does not place orders")

    async def cancel_order(self, symbol, order_id=None, client_order_id=None):  # type: ignore[override]
        raise NotImplementedError

    async def get_order(self, symbol, order_id=None, client_order_id=None):  # type: ignore[override]
        raise NotImplementedError

    async def get_open_orders(self, symbol=None):  # type: ignore[override]
        return []

    async def get_my_trades(self, symbol: str, limit: int = 100) -> list[OrderFill]:
        return []

    async def stream_private(self):  # type: ignore[override]
        if False:  # pragma: no cover
            yield None
