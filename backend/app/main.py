"""FastAPI-приложение: сборка конфигурации, сервиса и маршрутов."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.api.ws import router as ws_router
from app.config.loader import ConfigError, load_config
from app.config.schema import Config
from app.core.logging import configure_logging
from app.exchanges.binance.adapter import BinanceSpotAdapter
from app.market_data.websocket_manager import ReconnectPolicy
from app.paper.api import router as paper_router
from app.scanner.universe import discover_config
from app.services.market_service import MarketService

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("configs/default.yaml")


def build_adapter(config: Config) -> BinanceSpotAdapter:
    return BinanceSpotAdapter(
        symbols=config.exchange.symbols,
        rest_base_url=config.exchange.rest_base_url,
        ws_base_url=config.exchange.ws_base_url,
        depth=config.market_data.orderbook_depth,
        requests_per_minute=config.exchange.max_requests_per_minute,
        recv_window_ms=config.exchange.recv_window_ms,
        timeout_s=config.exchange.rest_timeout_s,
        policy=ReconnectPolicy(
            initial_ms=config.market_data.reconnect_initial_ms,
            max_ms=config.market_data.reconnect_max_ms,
        ),
    )


def _resolve_config_path(config_path: str | Path | None) -> Path:
    if config_path is not None:
        return Path(config_path)
    from_env = os.environ.get("CLE_CONFIG")
    return Path(from_env) if from_env else DEFAULT_CONFIG


def create_app(config_path: str | Path | None = None) -> FastAPI:
    path = _resolve_config_path(config_path)
    config = load_config(path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(config.app.log_level, config.app.log_json)
        logger.info("starting application", extra={"extra_fields": {"config": str(path), "mode": config.app.mode}})
        try:
            active_config = await discover_config(config)
        except Exception:
            logger.exception("Broad discovery failed; retaining configured instruments")
            active_config = config
        service = MarketService(config=active_config, adapter=build_adapter(active_config))
        app.state.service = service
        app.state.config = config
        try:
            await service.start()
            yield
        finally:
            logger.info("stopping application")
            await service.stop()

    app = FastAPI(
        title=config.app.name,
        version="0.2.0",
        description="Монитор микроструктуры крипто-спота: стакан, сделки, признаки, стены.",
        lifespan=lifespan,
    )
    app.state.config = config
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.api.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["*"],
    )
    app.include_router(api_router)
    app.include_router(paper_router)
    app.include_router(ws_router)

    dist = Path("frontend/dist")
    if dist.exists():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="ui")

    return app


def main() -> None:
    import uvicorn

    path = _resolve_config_path(None)
    try:
        config = load_config(path)
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=config.api.host,
        port=config.api.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
