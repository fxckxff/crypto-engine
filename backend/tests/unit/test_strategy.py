"""Тесты для стратегий и менеджера рисков (PHASE 5-6)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.core.enums import Side
from app.core.time import FakeClock
from app.market_data.orderbook import LocalOrderBook
from app.strategy.base import RiskContext, Signal
from app.strategy.market_making import MarketMakingStrategy
from app.strategy.risk import RiskEngine

D = Decimal


@pytest.fixture
def mock_book() -> LocalOrderBook:
    clock = FakeClock()
    book = LocalOrderBook("BTCUSDT", depth=50, clock=clock)
    book.apply_snapshot(
        bids=[(D("100.00"), D("1.0"))],
        asks=[(D("100.10"), D("1.0"))],
        last_update_id=100,
        now_ns=clock.monotonic_ns(),
    )
    return book


def test_market_making_strategy_signal(mock_book: LocalOrderBook) -> None:
    strategy = MarketMakingStrategy("BTCUSDT", min_spread_pct=0.05, max_notional=D("500"))
    
    # Спред (100.10 - 100.00)/100 = 0.1% >= 0.05% -> должен дать сигнал
    snapshot = None  # Не используется в логике этой стратегии
    signal = strategy.on_snapshot(snapshot, mock_book, None)
    
    assert signal is not None
    assert signal.symbol == "BTCUSDT"
    assert signal.side is Side.BID
    assert signal.intent == "OPEN"
    assert signal.price == D("100.00")


def test_risk_engine_limits() -> None:
    risk = RiskEngine(max_position_usdt=50.0, max_total_exposure_usdt=100.0, max_open_positions=2)
    
    signal = Signal("BTCUSDT", Side.BID, "OPEN", size=D("0.1"), price=D("100.00"))  # Notional: 10 USDT
    context = RiskContext(
        positions={},
        total_exposure=D("0"),
        max_exposure=D("100"),
        max_position=D("50"),
        balances_usdt=D("1000"),
    )
    
    assert risk.approve(signal, context) is True
    
    # Сигнал превышает лимит позиции (60 USDT > 50 USDT)
    large_signal = Signal("BTCUSDT", Side.BID, "OPEN", size=D("0.6"), price=D("100.00"))
    assert risk.approve(large_signal, context) is False
