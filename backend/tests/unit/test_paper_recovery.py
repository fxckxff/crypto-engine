import asyncio
from types import SimpleNamespace

from app.core.time import FakeClock
from app.paper.runtime import PaperRuntime
from app.services.market_service import MarketService

from tests.unit.test_paper import MS, T, engine, market


def test_data_pause_never_resumes_manual_or_risk_stop():
    e = engine()
    e.pause_data(T, "disconnect")
    assert e.data_paused and not e.running
    e.stop(T, "manual")
    e.resume_data(T)
    assert not e.running and not e.data_paused
    e.start(T)
    e.pause_data(T, "disconnect")
    e.consecutive_losses = e.settings.max_consecutive_losses
    e.resume_data(T)
    assert not e.running and not e.data_paused
    assert e.halt_reason == "Лимит убыточных сделок подряд"


def test_expired_session_not_resumed():
    e = engine(session_end_ns=T + 100 * MS)
    e.pause_data(T, "disconnect")
    e.resume_data(T + 101 * MS)
    assert not e.running and not e.data_paused
    assert e.halt_reason == "Время Paper-сессии истекло"


async def test_recovery_requires_continuous_health_and_fresh_positions():
    e = engine()
    e.pause_data(T, "disconnect")
    clock = FakeClock(T, 0)
    status = {
        "running": True,
        "connection": {"connected": True},
        "data_loss": False,
        "books": {"BTCUSDT": {"state": "READY"}},
    }

    async def apply(method, **kwargs):
        getattr(e, method)(clock.wall_ns())

    s = SimpleNamespace(
        paper=SimpleNamespace(engine=e, error=None, apply=apply),
        clock=clock,
        status=lambda: status,
        _paper_recovery_since_ns=None,
    )

    async def tick(ms):
        clock.advance_ms(ms)
        e.market(market(clock.wall_ns()), clock.wall_ns())
        await MarketService._recover_paper(s)

    await tick(0)
    await tick(9000)
    assert not e.running
    # Connection flap without data loss should NOT reset the timer.
    status["connection"]["connected"] = False
    await tick(1000)
    status["connection"]["connected"] = True
    await tick(0)
    # Timer has been running continuously: 9s + 1s + 0s = 10s, should resume now.
    assert e.running and not e.data_paused
    assert any(x["kind"] == "DATA_RESUMED" for x in e.events)

    # Now test that actual data loss DOES reset the timer.
    e.pause_data(T + 20_000 * MS, "data loss")
    status["data_loss"] = True
    await tick(5000)
    status["data_loss"] = False
    # First tick starts the timer, second tick after 10s expires it.
    await tick(1)
    await tick(10000)
    assert e.running and not e.data_paused


async def test_start_clock_sampled_after_lock(tmp_path, monkeypatch):
    e = engine(tmp_path)
    p = PaperRuntime(e)
    await p.open(T)
    e.stop(T)
    await p.lock.acquire()
    task = asyncio.create_task(p.apply("start", current_time=True))
    await asyncio.sleep(0)
    later = T + 1000 * MS
    e.market(market(later), later)
    monkeypatch.setattr("app.paper.runtime.time.time_ns", lambda: later)
    p.lock.release()
    await task
    assert e.running
    await p.close(later)


async def test_idle_trade_fast_path_keeps_deadline_and_dedup(tmp_path):
    e = engine(tmp_path, session_end_ns=T + 200 * MS)
    p = PaperRuntime(e)
    await p.open(T)
    e.start(T)
    await p.apply("trade", "BTCUSDT", 10, market().bid, market().bid, "SELL", T + 100 * MS)
    assert e.last_trade_ids["BTCUSDT"] == 10
    await p.apply("trade", "BTCUSDT", 9, market().bid, market().bid, "SELL", T + 101 * MS)
    assert e.last_trade_ids["BTCUSDT"] == 10
    await p.apply("trade", "BTCUSDT", 11, market().bid, market().bid, "SELL", T + 201 * MS)
    assert not e.running
    await p.close(T + 202 * MS)


async def test_quiet_isolated_symbols_do_not_pause_entire_account():
    e = engine(isolate_stale_symbols=True)
    clock = FakeClock(T + 3000 * MS, 3000 * MS)
    status = {
        "running": True,
        "connection": {"connected": True},
        "data_loss": False,
        "books": {"BTCUSDT": {"state": "STALE"}},
    }

    async def apply(method, *args, **kwargs):
        getattr(e, method)(*(args or (clock.wall_ns(),)))

    s = SimpleNamespace(
        paper=SimpleNamespace(engine=e, error=None, apply=apply),
        clock=clock,
        status=lambda: status,
        _paper_recovery_since_ns=None,
    )
    await MarketService._recover_paper(s)
    assert e.running
    e.pause_data(clock.wall_ns(), "disconnect")
    await MarketService._recover_paper(s)
    clock.advance_ms(11000)
    await MarketService._recover_paper(s)
    assert not e.running  # no fresh quote yet, even after stable transport
    e.market(market(clock.wall_ns()), clock.wall_ns())
    status["books"]["BTCUSDT"]["state"] = "READY"
    await MarketService._recover_paper(s)
    assert e.running  # the next fresh quote resumes without another ten-second wait


def test_continuous_paper_keeps_loss_history_and_honors_manual_stop():
    e = engine(session_end_ns=T + 100 * MS)
    e.consecutive_losses = 100
    e.cash = e.cash / 2
    e.set_continuous(True, T)
    assert e.settings.session_end_ns is None
    assert e.loss_reason() is None
    e.market(market(T + 200 * MS), T + 200 * MS)
    assert e.running
    assert e.consecutive_losses == 100
    e.stop(T + 200 * MS)
    e.resume_data(T + 200 * MS)
    assert not e.running
    e.set_continuous(False, T + 200 * MS)
    assert e.loss_reason() is not None


async def test_invalid_isolated_symbol_does_not_block_healthy_market():
    e = engine(isolate_stale_symbols=True)
    clock = FakeClock(T, 0)
    status = {"running": True, "connection": {"connected": True}, "data_loss": False,
              "books": {"BTCUSDT": {"state": "READY"}, "OTHERUSDT": {"state": "INVALID"}}}

    async def apply(method, *args, **kwargs):
        getattr(e, method)(*(args or (clock.wall_ns(),)))

    service = SimpleNamespace(paper=SimpleNamespace(engine=e, error=None, apply=apply), clock=clock,
                              status=lambda: status, _paper_recovery_since_ns=None)
    await MarketService._recover_paper(service)
    assert e.running
    e.pause_data(T, "disconnect")
    await MarketService._recover_paper(service)
    clock.advance_ms(10000)
    e.market(market(clock.wall_ns()), clock.wall_ns())
    await MarketService._recover_paper(service)
    assert e.running
    e.market(__import__("dataclasses").replace(market(symbol="OTHERUSDT"), ready=False), clock.wall_ns())
    order = e.submit("OTHERUSDT", "BUY", market().bid / 1000, market().bid, clock.wall_ns(), "invalid-symbol")
    assert order.status == "REJECTED"
    assert not e.active("OTHERUSDT")
