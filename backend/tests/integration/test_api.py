"""API: контракты ответов и явные 501 для нереализованных действий."""

from __future__ import annotations

from app.api.routes import router
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeService:
    def __init__(self) -> None:
        self.states: dict[str, object] = {"BTCUSDT": object()}

    def status(self) -> dict:
        return {
            "mode": "research",
            "exchange": "binance",
            "market": "spot",
            "uptime_s": 1.0,
            "running": True,
            "symbols": ["BTCUSDT"],
            "connection": {"connected": True, "connects": 1, "disconnects": 0, "last_error": None},
            "recorder": {"raw": None, "features": None},
            "data_loss": False,
            "books": {"BTCUSDT": {"state": "READY", "levels": 400, "age_ms": 5.0, "gaps": 0, "resyncs": 1}},
        }

    def scanner_rows(self) -> list[dict]:
        return [{"symbol": "BTCUSDT", "eligible": True, "opportunity_score": 42.0}]

    def symbol_payload(self, symbol: str, levels: int = 20) -> dict | None:
        if symbol.upper() != "BTCUSDT":
            return None
        return {"symbol": "BTCUSDT", "bids": [], "asks": [], "spread": {"pct": 0.5}}

    def active_walls(self, symbol: str | None = None) -> list:
        return []


def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.service = FakeService()
    return TestClient(app)


def test_health() -> None:
    response = client().get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["books"]["BTCUSDT"] == "READY"


def test_system_status() -> None:
    body = client().get("/api/system/status").json()
    assert body["mode"] == "research"
    assert body["data_loss"] is False


def test_scanner_results() -> None:
    rows = client().get("/api/scanner/results").json()
    assert rows[0]["symbol"] == "BTCUSDT"


def test_orderbook_known_and_unknown_symbol() -> None:
    api = client()
    assert api.get("/api/orderbook/BTCUSDT").status_code == 200
    assert api.get("/api/orderbook/DOGEUSDT").status_code == 404


def test_kill_switch_returns_400_when_live_disabled() -> None:
    api = client()
    response = api.post("/api/system/stop")
    assert response.status_code == 400
    assert "live trading not enabled" in response.json()["detail"]
    response = api.post("/api/system/reconcile")
    assert response.status_code == 400
    assert "live trading not enabled" in response.json()["detail"]


def test_invalid_depth_is_rejected():
    api = client()
    for levels in (0, -1, 1001):
        assert api.get(f"/api/orderbook/BTCUSDT?levels={levels}").status_code == 422


def test_stale_book_makes_health_degraded():
    api = client()
    service = api.app.state.service
    status = service.status()
    status["books"]["BTCUSDT"]["state"] = "STALE"
    service.status = lambda: status
    response = api.get("/api/health")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
