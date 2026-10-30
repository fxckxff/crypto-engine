"""WebSocket для интерфейса (ТЗ §23).

Частота обновлений UI не влияет на внутренние расчёты: сервис считает признаки
по своему такту, а UI получает последнее состояние.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/market/{symbol}")
async def market_stream(websocket: WebSocket, symbol: str) -> None:
    service = websocket.app.state.service
    symbol = symbol.upper()
    if symbol not in service.states:
        await websocket.close(code=4004, reason=f"symbol {symbol} is not monitored")
        return

    await websocket.accept()
    queue = service.subscribe(symbol)

    def take(payload: dict[str, Any] | None) -> dict[str, Any] | None:
        return payload if payload and payload.get("symbol") == symbol else None

    try:
        initial = service.symbol_payload(symbol, levels=20)
        if initial is not None:
            await websocket.send_json({"type": "snapshot", "data": initial})
        while True:
            await asyncio.sleep(1 / service.config.api.ui_update_hz)
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            while not queue.empty():
                payload = queue.get_nowait()
            filtered = take(payload)
            if filtered is not None:
                await websocket.send_json({"type": "update", "data": filtered})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("UI WebSocket failed")
    finally:
        service.unsubscribe(queue)


@router.websocket("/ws/scanner")
async def scanner_stream(websocket: WebSocket) -> None:
    service = websocket.app.state.service
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"type": "scanner", "data": service.scanner_rows()})
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("UI WebSocket failed")


@router.websocket("/ws/system")
async def system_stream(websocket: WebSocket) -> None:
    service = websocket.app.state.service
    await websocket.accept()
    try:
        while True:
            await websocket.send_json({"type": "system", "data": service.status()})
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("UI WebSocket failed")
