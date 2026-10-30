from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
from app.config.schema import Config
from app.paper.api import router
from app.paper.runtime import PaperRuntime
from fastapi import FastAPI

from tests.unit.test_paper import engine, market


async def test_paper_api_commands_risk_and_history(tmp_path):
    now = time.time_ns()
    value = engine(tmp_path)
    value.market(market(now), now)
    value.stop(now)
    paper = PaperRuntime(value)
    await paper.open(now)
    app = FastAPI()
    app.include_router(router)
    app.state.config = Config()
    app.state.service = SimpleNamespace(
        paper=paper,
        status=lambda: {
            "running": True,
            "data_loss": False,
            "connection": {"connected": True},
            "books": {"BTCUSDT": {"state": "READY"}},
        },
    )
    headers = {"X-Paper-Command": "1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        assert (await client.get("/api/paper/status")).json()["available"]
        assert (await client.post("/api/paper/start")).status_code == 403
        assert (
            await client.post("/api/paper/start", headers={**headers, "Origin": "https://evil.example"})
        ).status_code == 403
        assert (await client.post("/api/paper/start", headers=headers)).status_code == 200
        command = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "price": "100",
            "quantity": "0.1",
            "client_order_id": "api-order-1",
        }
        response = await client.post("/api/paper/orders", headers=headers, json=command)
        assert response.status_code == 200
        assert response.json()["reserved_cash"] == "10.0100"
        assert (await client.post("/api/paper/orders", headers=headers, json=command)).status_code == 200
        assert len((await client.get("/api/orders")).json()) == 1
        invalid = {**command, "client_order_id": "api-order-2", "quantity": "NaN"}
        assert (await client.post("/api/paper/orders", headers=headers, json=invalid)).status_code == 422
        assert (await client.post("/api/paper/orders/api-order-1/cancel", headers=headers)).status_code == 200
        assert (await client.post("/api/paper/stop", headers=headers)).status_code == 200
        assert not (await client.get("/api/paper/status")).json()["running"]
        assert (await client.get("/api/positions")).json() == []
        assert (await client.get("/api/trades")).json() == []
        assert any(e["kind"] == "SUBMITTING" for e in (await client.get("/api/paper/events")).json())
        settings = (await client.get("/api/strategies/spread_capture")).json()
        settings["order_notional"] = "15"
        assert (
            await client.put("/api/strategies/spread_capture/config", headers=headers, json=settings)
        ).status_code == 200
        assert (await client.get("/api/strategies/spread_capture")).json()["order_notional"] == "15"
    await paper.close(time.time_ns())


async def test_research_mode_has_no_execution_runtime():
    app = FastAPI()
    app.include_router(router)
    app.state.service = SimpleNamespace(paper=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        assert not (await client.get("/api/paper/status")).json()["available"]
        assert (await client.get("/api/orders")).status_code == 409
