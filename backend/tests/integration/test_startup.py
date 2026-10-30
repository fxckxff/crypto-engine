"""Exercise the real app imports and lifecycle, not only a detached API router."""
from unittest.mock import AsyncMock

import pytest
from app.main import create_app
from app.services.market_service import MarketService
from fastapi.testclient import TestClient


def test_application_imports_and_serves(monkeypatch):
    start, stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(MarketService, "start", start)
    monkeypatch.setattr(MarketService, "stop", stop)
    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/api/health").status_code == 503
    start.assert_awaited_once()
    stop.assert_awaited_once()


def test_startup_failure_closes_resources(monkeypatch):
    monkeypatch.setattr(MarketService, "start", AsyncMock(side_effect=RuntimeError("offline")))
    stop = AsyncMock()
    monkeypatch.setattr(MarketService, "stop", stop)
    with pytest.raises(RuntimeError, match="offline"), TestClient(create_app()):
        pass
    stop.assert_awaited_once()
