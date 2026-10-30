from decimal import Decimal as D

from app.config.schema import RiskSection
from app.paper.config import StrategySettings
from app.paper.models import PaperPosition
from app.scanner.universe import select_candidates

from tests.unit.test_paper import MS, T, engine, market


def test_dust_does_not_use_slot_but_keeps_cost_and_exposure():
    e = engine()
    e.risk = RiskSection(max_open_positions=1)
    e.positions["BTCUSDT"] = PaperPosition("BTCUSDT", "spread_capture", D("0.005"), D("0.5"), T)
    e.market(market(symbol="ETHUSDT"), T)
    order = e.submit("ETHUSDT", "BUY", D("0.1"), D(100), T, "new-symbol")
    assert order.status == "SUBMITTING"
    assert e.positions["BTCUSDT"].cost == D("0.5")
    e.risk = RiskSection(max_position_usdt=10, max_total_exposure_usdt=10, max_open_positions=0)
    e.market(market(symbol="SOLUSDT"), T)
    order = e.submit("SOLUSDT", "BUY", D("0.1"), D(100), T, "over-budget")
    assert order.status == "REJECTED"
    assert "экспозиции" in order.reason


def test_zero_count_limit_allows_multiple_symbols_with_budget():
    e = engine()
    e.risk = RiskSection(max_open_positions=0)
    for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        e.market(market(symbol=s), T)
        o = e.submit(s, "BUY", D("0.1"), D(100), T, "entry-" + s)
        assert o.status == "SUBMITTING"
    assert len(e.active()) == 3


def test_passive_exit_captures_available_spread_after_fees():
    e = engine()
    e.positions["BTCUSDT"] = PaperPosition("BTCUSDT", "spread_capture", D(1), D("100.1"), T)
    m = market(bid="100", ask="100.31")
    e.market(m, T)
    sell = e.active()[0]
    assert sell.side == "SELL" and sell.kind == "POST_ONLY"
    assert sell.price == D("100.31")
    assert sell.price * D("0.999") - D("100.1") > D("0.1")


def test_recovered_dust_is_no_longer_forced_exit():
    e = engine()
    p = PaperPosition("BTCUSDT", "spread_capture", D("0.02"), D("2.002"), T, exit_reason="DUST_BELOW_MIN_NOTIONAL")
    e.positions[p.symbol] = p
    e.market(market(), T)
    assert p.exit_reason is None
    assert e.active()[0].kind == "POST_ONLY"


def test_partial_buy_below_minimum_is_not_cancelled_by_exit():
    e = engine()
    o = e.submit("BTCUSDT", "BUY", D("0.1"), D(100), T, "partial-order")
    e.trade("BTCUSDT", 1, D(100), D("1.005"), "SELL", T + 100 * MS)
    e.market(market(now=T + 200 * MS), T + 200 * MS)
    assert o.status == "PARTIALLY_FILLED"
    assert o.cancel_ns is None
    assert e.is_dust(e.positions["BTCUSDT"])


def test_confirmed_microstructure_is_not_starved_by_spread_iteration():
    e = engine()
    settings = {k: StrategySettings(enabled=True) for k in e.settings.strategies}
    e.settings = e.settings.model_copy(update={"strategies": settings})
    e.market(market(), T)
    assert e.active()[0].strategy == "microstructure"
    assert len(e.decisions) == 3


def test_universe_candidates_exclude_inactive_thin_quotes_and_existing():
    rows = [
        dict(symbol=s, trades=n, volume_usdt=200000, spread_pct=0.4, top_depth_usdt=d)
        for s, n, d in [("A", 2000, 50), ("B", 3000, 10), ("C", 1, 1), ("D", 4000, 0)]
    ]
    assert select_candidates(rows, [], 10, True) == ["B", "A"]
    assert select_candidates(rows, ["B"], 10, True) == ["A"]


def test_optimized_snapshot_is_independent_and_keeps_command_idempotency():
    e = engine()
    e.last_trade_ids["BTCUSDT"] = 1
    e.cooldowns["BTCUSDT"] = T
    snapshot = e.state()
    e.last_trade_ids["BTCUSDT"] = 2
    e.cooldowns["BTCUSDT"] = T + MS
    e.order_times.append(T)
    assert snapshot["last_trade_ids"]["BTCUSDT"] == 1
    assert snapshot["cooldowns"]["BTCUSDT"] == T
    assert snapshot["order_times"] == []
    o = e.submit("BTCUSDT", "SELL", D(1), D(100), T, "terminal-id")
    assert o.status == "REJECTED"
    first = e.state()
    first["orders"][o.id]["reason"] = "changed externally"
    assert e.state()["orders"][o.id]["reason"] != "changed externally"
    assert e.submit("BTCUSDT", "SELL", D(1), D(100), T, "terminal-id") is o
