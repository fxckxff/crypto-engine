from __future__ import annotations

import time
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.paper.config import StrategySettings

router = APIRouter(prefix="/api")


def runtime(request: Request):
    service = getattr(request.app.state, "service", None)
    paper = getattr(service, "paper", None)
    if paper is None:
        raise HTTPException(409, "Paper Trading недоступен: запустите configs/paper.yaml")
    return paper


def command_guard(request: Request):
    if request.headers.get("x-paper-command") != "1":
        raise HTTPException(403, "Требуется X-Paper-Command")
    origin = request.headers.get("origin")
    if origin:
        allowed = {f"{request.url.scheme}://{request.headers.get('host', '')}"}
        allowed.update(request.app.state.config.api.cors_origins)
        if origin not in allowed or urlparse(origin).scheme not in ("http", "https"):
            raise HTTPException(403, "Недопустимый Origin")


async def apply(request, method, *args, **kwargs):
    paper = runtime(request)
    try:
        await paper.apply(method, *args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return await paper.summary(time.time_ns())


class OrderCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1, max_length=30, pattern=r"^[A-Z0-9]+$")
    side: Literal["BUY", "SELL"]
    kind: Literal["POST_ONLY", "IOC"] = "POST_ONLY"
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    client_order_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/paper/status")
async def status(request: Request):
    service = getattr(request.app.state, "service", None)
    if not getattr(service, "paper", None):
        return {"available": False, "mode": "research"}
    return {"available": True, **await runtime(request).summary(time.time_ns())}


@router.post("/paper/start", dependencies=[Depends(command_guard)])
async def start(request: Request):
    service = request.app.state.service
    status = service.status()
    if (
        not status["running"]
        or status["data_loss"]
        or not status["connection"]["connected"]
        or not status["books"]
        or (
            not any(book["state"] in {"READY", "STALE"} for book in status["books"].values())
            if runtime(request).engine.settings.isolate_stale_symbols
            else any(book["state"] != "READY" for book in status["books"].values())
        )
    ):
        raise HTTPException(409, "Market data health checks не пройдены")
    return await apply(request, "start", current_time=True)


@router.post("/paper/stop", dependencies=[Depends(command_guard)])
async def stop(request: Request):
    return await apply(request, "stop", time.time_ns())


@router.post("/paper/flatten", dependencies=[Depends(command_guard)])
async def flatten(request: Request):
    return await apply(request, "stop", time.time_ns(), "Контролируемое закрытие", flatten=True)


@router.post("/paper/orders", dependencies=[Depends(command_guard)])
async def order(request: Request, command: OrderCommand):
    return await apply(
        request,
        "submit",
        command.symbol,
        command.side,
        command.quantity,
        command.price,
        time.time_ns(),
        command.client_order_id,
        kind=command.kind,
    )


@router.post("/paper/orders/{order_id}/cancel", dependencies=[Depends(command_guard)])
async def cancel(request: Request, order_id: str):
    return await apply(request, "cancel", order_id, time.time_ns())


@router.get("/strategies")
async def strategies(request: Request):
    return (await runtime(request).summary(time.time_ns()))["strategies"]


@router.get("/strategies/{name}")
async def strategy(request: Request, name: str):
    settings = (await strategies(request)).get(name)
    if settings is None:
        raise HTTPException(404, "Стратегия не найдена")
    return settings


@router.put("/strategies/{name}/config", dependencies=[Depends(command_guard)])
async def configure(request: Request, name: str, settings: StrategySettings):
    return await apply(request, "update_strategy", name, settings, time.time_ns())


@router.get("/orders")
async def orders(request: Request):
    return (await runtime(request).summary(time.time_ns()))["orders"]


@router.get("/positions")
async def positions(request: Request):
    return (await runtime(request).summary(time.time_ns()))["positions"]


@router.get("/trades")
async def trades(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    import asyncio

    return await asyncio.to_thread(runtime(request).store.history, limit, "FILL")


@router.get("/paper/events")
async def events(request: Request, limit: int = Query(default=100, ge=1, le=1000)):
    import asyncio

    return await asyncio.to_thread(runtime(request).store.history, limit)


class SessionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    continuous: bool


@router.put("/paper/session", dependencies=[Depends(command_guard)])
async def session_policy(request: Request, policy: SessionPolicy):
    return await apply(request, "set_continuous", policy.continuous, current_time=True)
