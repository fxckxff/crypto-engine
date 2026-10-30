from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from app.config.schema import RiskSection
from app.exchanges.base import TradingRules
from app.paper.config import PaperSettings, StrategySettings
from app.paper.engine import PaperEngine
from app.paper.models import Market
from app.paper.runtime import PaperRuntime
from app.paper.signals import evaluate

D = Decimal
T = 1_790_000_000_000_000_000
MS = 1_000_000


def market(now=T, symbol="BTCUSDT", bid="100", ask="100.5", sequence=1, quantity="1"):
    return Market(
        symbol,
        now,
        sequence,
        True,
        ((D(bid), D(quantity)),),
        ((D(ask), D(quantity)),),
        TradingRules(symbol, D("0.01"), D("0.005"), D("0.005"), D("100"), D("1")),
        obi=0.5,
        ofi=1,
        microprice_bps=40,
        buy_volume=D(10),
        sell_volume=D(1),
        trades=20,
    )


def engine(tmp_path=None, **kwargs):
    settings = PaperSettings(database_path=str(tmp_path / "paper.sqlite") if tmp_path else ":memory:", **kwargs)
    settings = settings.model_copy(
        update={"strategies": {k: v.model_copy(update={"enabled": False}) for k, v in settings.strategies.items()}}
    )
    value = PaperEngine(settings, RiskSection(enabled=True), max_age_ms=500)
    value.market(market(), T)
    value.start(T)
    return value


def buy(value, now=T, quantity="0.1", client_id="test-order-1"):
    return value.submit("BTCUSDT", "BUY", D(quantity), D(100), now, client_id)


def filled_buy(value):
    order = buy(value)
    value.trade("BTCUSDT", 1, D(100), D("1.1"), "SELL", T + 100 * MS)
    return order


def test_reserved_cash_and_pending_position_count():
    value = engine()
    order = buy(value)
    assert order.status == "SUBMITTING"
    assert value.cash == 1000
    assert value.reserved_cash == D("10.01")
    duplicate = buy(value)
    assert duplicate is order
    assert len(value.active()) == 1
    assert buy(value, client_id="test-order-2").status == "REJECTED"


@pytest.mark.parametrize(
    "quantity,price",
    [("0.103", "100"), ("0.1", "100.001"), ("0.005", "100"), ("-1", "100"), ("NaN", "100"), ("0.1", "Infinity")],
)
def test_invalid_orders_never_reserve(quantity, price):
    value = engine()
    result = value.submit("BTCUSDT", "BUY", D(quantity), D(price), T, "bad-order")
    assert result.status == "REJECTED"
    assert value.reserved_cash == 0


def test_duplicate_command_id_different_parameters_rejected():
    value = engine()
    buy(value)
    with pytest.raises(ValueError, match="client_order_id"):
        buy(value, quantity="0.2")


def test_queue_latency_partial_and_duplicate_trade():
    value = engine()
    order = buy(value)
    value.trade("BTCUSDT", 1, D(100), D(20), "SELL", T + 50 * MS)
    assert order.filled == 0
    value.trade("BTCUSDT", 2, D(100), D("1.05"), "SELL", T + 100 * MS)
    assert order.filled == D("0.05")
    assert order.status == "PARTIALLY_FILLED"
    assert value.cash == D("994.995")
    value.trade("BTCUSDT", 2, D(100), D(100), "SELL", T + 110 * MS)
    assert order.filled == D("0.05")
    value.trade("BTCUSDT", 3, D(100), D("0.05"), "SELL", T + 120 * MS)
    assert order.status == "FILLED"
    assert value.positions["BTCUSDT"].cost == D("10.01")
    assert value.reserved_cash == 0


def test_wrong_aggressor_and_historical_exchange_trade_cannot_fill():
    value = engine()
    order = buy(value)
    value.trade("BTCUSDT", 1, D(100), D(100), "BUY", T + 100 * MS)
    value.trade("BTCUSDT", 2, D(100), D(100), "SELL", T + 110 * MS, T - MS)
    assert order.filled == 0


def test_cancel_before_ack_keeps_queue_and_can_fill_before_cancel_ack():
    value = engine()
    order = buy(value)
    value.cancel(order.id, T + 50 * MS)
    value.trade("BTCUSDT", 1, D(100), D("0.5"), "SELL", T + 100 * MS)
    assert order.filled == 0
    value.trade("BTCUSDT", 2, D(100), D("0.55"), "SELL", T + 120 * MS)
    assert order.filled == D("0.05")
    value.trade("BTCUSDT", 3, D(100), D(100), "SELL", T + 150 * MS)
    assert order.filled == D("0.05")
    assert order.status == "CANCELLED"
    assert value.reserved_cash == 0


def test_post_only_rechecked_after_latency():
    value = engine()
    order = buy(value)
    value.market(market(T + 100 * MS, bid="99", ask="100", sequence=2), T + 100 * MS)
    assert order.status == "REJECTED"
    assert value.cash == 1000


def test_round_trip_pnl_and_fees():
    value = engine()
    filled_buy(value)
    order = value.submit("BTCUSDT", "SELL", D("0.1"), D("100.5"), T + 100 * MS, "sell-order")
    value.trade("BTCUSDT", 2, D("100.5"), D("1.1"), "BUY", T + 200 * MS)
    assert order.status == "FILLED"
    assert not value.positions
    assert value.realized == D("0.02995")
    assert value.cash == D("1000.02995")
    assert value.fees == D("0.02005")


def test_cannot_sell_more_than_inventory():
    value = engine()
    assert value.submit("BTCUSDT", "SELL", D("0.1"), D("100.5"), T, "no-inventory").status == "REJECTED"
    filled_buy(value)
    assert value.submit("BTCUSDT", "SELL", D("0.2"), D("100.5"), T + 100 * MS, "too-large").status == "REJECTED"


def test_cash_includes_fees_and_pending_orders_in_exposure():
    value = engine(initial_cash=D("10"))
    assert buy(value).status == "REJECTED"
    value = engine()
    value.risk = RiskSection(enabled=True, max_position_usdt=50, max_total_exposure_usdt=50, max_open_positions=1)
    buy(value)
    value.market(market(symbol="ETHUSDT"), T)
    assert value.submit("ETHUSDT", "BUY", D("0.1"), D(100), T, "second-symbol").status == "REJECTED"


def test_stale_data_halts_and_releases_orders_without_fake_liquidation():
    value = engine()
    filled_buy(value)
    value.market(replace(market(), ready=False), T + 200 * MS)
    assert not value.running
    assert value.positions["BTCUSDT"].quantity == D("0.1")
    assert not value.active()
    with pytest.raises(ValueError):
        value.start(T + 200 * MS)


def test_time_exit_and_partial_ioc_remain_visible():
    value = engine(max_hold_seconds=1, cooldown_ms=100)
    filled_buy(value)
    later = T + 1200 * MS
    value.market(market(later, sequence=2, quantity="0.05"), later)
    order = value.active()[0]
    assert order.reason == "TIME_EXIT" and order.kind == "IOC"
    value.market(market(later + 100 * MS, sequence=2, quantity="0.05"), later + 100 * MS)
    assert value.positions["BTCUSDT"].quantity == D("0.05")
    # A second IOC cannot consume already used liquidity from the same book update.
    value.market(market(later + 200 * MS, sequence=2, quantity="0.05"), later + 200 * MS)
    assert value.positions["BTCUSDT"].quantity == D("0.05")


def test_daily_loss_stops_entries():
    value = engine()
    value.risk = RiskSection(enabled=True, max_daily_loss_usdt=0.1)
    filled_buy(value)
    value.market(market(T + 200 * MS, bid="98", ask="98.5", sequence=2), T + 200 * MS)
    assert not value.running
    assert value.flattening
    assert "Дневной" in value.halt_reason


@pytest.mark.parametrize("name", ["spread_capture", "liquidity_wall", "microstructure"])
def test_strategies_need_confirmation_and_costs(name):
    settings = StrategySettings(enabled=True)
    frame = replace(market(), wall_score=90, wall_absorbed=D(1), wall_price=D(100), wall_persistent=True)
    result = evaluate(name, settings, PaperSettings(), frame)
    assert result.allowed
    assert not evaluate(name, settings, PaperSettings(), replace(frame, trades=0)).allowed
    assert not evaluate(name, settings, PaperSettings(maker_fee_bps=D(100)), frame).allowed
    if name == "liquidity_wall":
        assert not evaluate(name, settings, PaperSettings(), replace(frame, wall_absorbed=D(0))).allowed


async def test_persistent_positions_and_no_automatic_restart(tmp_path):
    value = engine(tmp_path)
    runtime = PaperRuntime(value)
    await runtime.open(T)
    await runtime.apply("submit", "BTCUSDT", "BUY", D("0.1"), D(100), T, "persistent-buy")
    await runtime.apply("trade", "BTCUSDT", 1, D(100), D("1.05"), "SELL", T + 100 * MS)
    # Simulate process exit without a graceful engine.stop; database contains an active partial order.
    runtime.store.close()
    restored = PaperRuntime(engine(tmp_path))
    await restored.open(T + 200 * MS)
    assert restored.engine.cash == D("994.995")
    assert restored.engine.positions["BTCUSDT"].quantity == D("0.05")
    assert not restored.engine.running
    assert restored.engine.orders["persistent-buy"].status == "CANCELLED"
    assert len(restored.store.history(kind="FILL")) == 1
    await restored.close(T + 300 * MS)


async def test_storage_failure_rolls_back_and_blocks_new_commands(tmp_path, monkeypatch):
    runtime = PaperRuntime(engine(tmp_path))
    await runtime.open(T)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.store, "save", fail)
    with pytest.raises(RuntimeError):
        await runtime.apply("submit", "BTCUSDT", "BUY", D("0.1"), D(100), T, "failed-order")
    assert not runtime.engine.running
    assert runtime.engine.cash == 1000
    assert not runtime.engine.active()
    with pytest.raises(RuntimeError):
        await runtime.apply("start", T)
    await runtime.close(T)


async def test_one_writer_per_account(tmp_path):
    first = PaperRuntime(engine(tmp_path))
    second = PaperRuntime(engine(tmp_path))
    await first.open(T)
    with pytest.raises(BlockingIOError):
        await second.open(T)
    await first.close(T)


async def test_backtest_uses_same_engine_deterministically(tmp_path):
    from scripts.paper_backtest import run_paper_backtest

    from tests.unit.test_reliability import dataset, event

    events = [event(1, "depth_snapshot", {"lastUpdateId": 1, "b": [["100", "1"]], "a": [["100.5", "1"]]})]
    # Keep book fresh and supply enough real-shaped aggressor events for a research signal.
    for i in range(2, 30):
        events.append(event(i, "depth_update", {"U": i, "u": i, "b": [["100", "1"]], "a": []}))
        events.append(event(i, "agg_trade", {"id": i, "p": "100", "q": "0.1", "aggressor": "BUY"}))
    path = dataset(tmp_path, events)
    first = await run_paper_backtest(path, "BTCUSDT", market().rules)
    second = await run_paper_backtest(path, "BTCUSDT", market().rules)
    assert first == second
    assert first["engine"] == "PaperEngine"
    assert first["events_processed"] == 57
    assert first["gaps"] == 0


def test_flatten_does_not_wait_for_previous_quote_cooldown():
    value = engine()
    filled_buy(value)
    value.market(market(T + 200 * MS), T + 200 * MS)
    assert value.active()[0].kind == "POST_ONLY"
    value.stop(T + 250 * MS, flatten=True)
    value.market(market(T + 300 * MS), T + 300 * MS)
    assert value.active()[0].kind == "IOC"
    value.market(market(T + 400 * MS), T + 400 * MS)
    assert not value.positions


def test_recovered_unmonitored_position_blocks_start():
    value = engine()
    filled_buy(value)
    value.stop(T + 200 * MS)
    value.markets.clear()
    value.market(market(T + 300 * MS, symbol="ETHUSDT"), T + 300 * MS)
    with pytest.raises(ValueError, match="Восстановленные"):
        value.start(T + 300 * MS)


def test_flatten_completion_is_visible():
    value = engine()
    filled_buy(value)
    value.stop(T + 200 * MS, flatten=True)
    value.market(market(T + 200 * MS), T + 200 * MS)
    value.market(market(T + 300 * MS), T + 300 * MS)
    assert not value.positions
    assert not value.flattening
    assert not value.running
    assert any(e["kind"] == "FLATTEN_COMPLETE" for e in value.events)


def test_dust_is_reported_once_instead_of_endless_exit_orders():
    value = engine()
    filled_buy(value)
    value.positions["BTCUSDT"].quantity = D("0.005")
    value.positions["BTCUSDT"].cost = D("0.5005")
    for i in range(2, 5):
        value.market(market(T + i * 100 * MS), T + i * 100 * MS)
    assert not value.active()
    assert len([e for e in value.events if e["kind"] == "EXIT_BLOCKED"]) == 1


def test_stale_altcoin_isolated_without_filling_or_stopping_other_markets():
    value = engine(isolate_stale_symbols=True)
    order = buy(value)
    value.market(market(symbol="ETHUSDT"), T)
    value.market(market(now=T - 1000 * MS), T)
    assert value.running
    assert order.status == "CANCELLED"
    assert value.cash == 1000
    assert value.fresh(value.markets["ETHUSDT"], T)
    value.trade("BTCUSDT", 1, D(100), D(20), "SELL", T + 100 * MS)
    assert order.filled == 0


def test_session_deadline_cancels_entries_and_cannot_restart():
    value = engine(session_end_ns=T + 200 * MS)
    order = buy(value)
    value.trade("BTCUSDT", 1, D(100), D(20), "SELL", T + 200 * MS)
    assert not value.running
    assert order.status == "CANCELLED"
    assert order.filled == 0
    with pytest.raises(ValueError, match="истекло"):
        value.start(T + 200 * MS)


def test_session_deadline_closes_inventory():
    value = engine(session_end_ns=T + 200 * MS)
    filled_buy(value)
    value.market(market(now=T + 200 * MS), T + 200 * MS)
    assert not value.running
    assert value.flattening
    assert any(o.side == "SELL" for o in value.active())


def test_order_history_is_bounded_and_keeps_active_orders():
    value = engine(order_history_limit=50)
    active = buy(value)
    for index in range(200):
        value.submit("BTCUSDT", "BUY", D("-1"), D(100), T, f"bad-{index}")
    assert len(value.orders) <= 51
    assert active.id in value.orders
    assert value.reserved_cash == D("10.01")
    assert len(value.state()["orders"]) <= 51
