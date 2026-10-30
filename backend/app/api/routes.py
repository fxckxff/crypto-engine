"""REST-эндпоинты (ТЗ §23)."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response

router = APIRouter(prefix="/api")


def _service(request: Request):
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="market service is not started")
    return service


@router.get("/health")
async def health(request: Request, response: Response) -> dict[str, Any]:
    service = _service(request)
    status = service.status()
    healthy = (
        status["running"]
        and status["connection"]["connected"]
        and not status["data_loss"]
        and not status.get("paper", {}).get("storage_error")
        and bool(status["books"])
        and all(info["state"] == "READY" for info in status["books"].values())
    )
    if not healthy:
        response.status_code = 503
    return {
        "status": "ok" if healthy else "degraded",
        "mode": status["mode"],
        "data_loss": status["data_loss"],
        "books": {symbol: info["state"] for symbol, info in status["books"].items()},
    }


@router.get("/system/status")
async def system_status(request: Request) -> dict[str, Any]:
    return _service(request).status()


@router.get("/instruments")
async def instruments(request: Request) -> list[dict[str, Any]]:
    service = _service(request)
    out: list[dict[str, Any]] = []
    for state in service.states.values():
        out.append(
            {
                "symbol": state.symbol,
                "base_asset": state.instrument.base_asset,
                "quote_asset": state.instrument.quote_asset,
                "status": state.instrument.status,
                "trading_allowed": state.instrument.trading_allowed,
                "tick_size": str(state.rules.tick_size),
                "step_size": str(state.rules.step_size),
                "min_quantity": str(state.rules.min_quantity),
                "min_notional": str(state.rules.min_notional),
            }
        )
    return out


@router.get("/scanner/results")
async def scanner_results(request: Request) -> list[dict[str, Any]]:
    return _service(request).scanner_rows()


@router.get("/orderbook/{symbol}")
async def orderbook(symbol: str, request: Request, levels: int = Query(default=20, ge=1, le=1000)) -> dict[str, Any]:
    payload = _service(request).symbol_payload(symbol, levels=levels)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"symbol {symbol} is not monitored")
    return payload


@router.get("/analytics/{symbol}")
async def analytics(symbol: str, request: Request) -> dict[str, Any]:
    service = _service(request)
    state = service.states.get(symbol.upper())
    if state is None:
        raise HTTPException(status_code=404, detail=f"symbol {symbol} is not monitored")
    snapshot = state.snapshot
    if snapshot is None:
        raise HTTPException(status_code=503, detail="features are not computed yet")
    return {
        "symbol": snapshot.symbol,
        "ts_ns": snapshot.now_ns,
        "book_state": snapshot.book_state.value,
        "book_age_ms": snapshot.book_age_ms,
        "tradable": snapshot.tradable,
        "block_reason": snapshot.block_reason,
        "spread": _jsonable(asdict(snapshot.spread)) if snapshot.spread else None,
        "obi": {str(k): v for k, v in snapshot.obi.items()},
        "obi_weighted": snapshot.obi_weighted,
        "microprice": _jsonable(asdict(snapshot.microprice)) if snapshot.microprice else None,
        "ofi": _jsonable(asdict(snapshot.ofi)) if snapshot.ofi else None,
        "flow": {
            **_jsonable(asdict(snapshot.flow)),
            "buy_volume": str(snapshot.flow.buy_volume),
            "sell_volume": str(snapshot.flow.sell_volume),
            "signed_delta": str(snapshot.flow.signed_delta),
            "cvd": str(snapshot.flow.cvd),
            "average_trade_size": str(snapshot.flow.average_trade_size),
            "large_trade_volume": str(snapshot.flow.large_trade_volume),
            "unknown_volume": str(snapshot.flow.unknown_volume),
            "large_trade_threshold": str(snapshot.flow.large_trade_threshold),
        },
        "walls": [
            {
                "side": wall.side.value,
                "price": str(wall.price),
                "state": wall.state.value,
                "score": wall.score,
                "notional": str(wall.current_notional),
                "absorbed": str(wall.absorbed_quantity),
                "distance_pct": wall.distance_pct,
                "transitions": [
                    {"from": t.from_state.value if t.from_state else None, "to": t.to_state.value, "reason": t.reason}
                    for t in wall.transitions[-10:]
                ],
            }
            for wall in state.walls.active_walls()
        ],
    }


@router.get("/walls")
async def walls(request: Request, symbol: str | None = None) -> list[dict[str, Any]]:
    service = _service(request)
    from app.services.market_service import _wall_payload

    return [{"symbol": wall.symbol, **_wall_payload(wall)} for wall in service.active_walls(symbol)]


@router.post("/system/stop")
async def system_stop(request: Request, emergency: bool = True) -> dict[str, Any]:
    """Kill switch: остановка live-торговли и отмена всех ордеров."""
    service = _service(request)
    live = getattr(service, "live_manager", None)
    if live is None:
        raise HTTPException(status_code=400, detail="live trading not enabled")
    await live.stop(emergency=emergency)
    return {"status": "stopped", "emergency": emergency}


@router.post("/system/reconcile")
async def system_reconcile(request: Request) -> dict[str, Any]:
    """Принудительная реконсиляция открытых ордеров с биржей."""
    service = _service(request)
    live = getattr(service, "live_manager", None)
    if live is None:
        raise HTTPException(status_code=400, detail="live trading not enabled")
    await live._reconcile_all()
    return {"status": "reconciled", "symbols": list(live.states.keys())}


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """Decimal → str: JSON не должен зависеть от float-погрешности."""
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in data.items()}


@router.get("/scanner/universe")
async def universe(request: Request) -> dict:
    service = _service(request)
    return {
        "updated_ns": service.universe_updated_ns,
        "error": service.universe_error,
        "scope": "Binance Spot USDT, TRADING",
        "depth_symbols": list(service.states),
        "selection": "Depth candidates selected at server start; overview refreshes every 60 seconds",
        "rows": [{**row, "monitored": row["symbol"] in service.states} for row in service.universe_rows],
    }
