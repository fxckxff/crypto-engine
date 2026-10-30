"""Конфигурация: строгая схема на Pydantic v2 (ТЗ §19).

Некорректная конфигурация не подменяется значениями по умолчанию — загрузка падает
с перечнем ошибок.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.paper.config import PaperSettings


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(StrictModel):
    name: str = "Crypto Liquidity Engine"
    mode: Literal["research", "backtest", "paper", "live"] = "research"
    timezone: Literal["UTC"] = "UTC"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True


class ExchangeSection(StrictModel):
    provider: Literal["binance"] = "binance"
    market: Literal["spot"] = "spot"
    testnet: bool = False
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"])
    rest_base_url: str = "https://api.binance.com"
    ws_base_url: str = "wss://stream.binance.com:9443"
    rest_timeout_s: float = Field(default=10.0, gt=0, le=60)
    recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    max_requests_per_minute: int = Field(default=600, gt=0, le=6000)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str]) -> list[str]:
        cleaned = [s.strip().upper() for s in value if s.strip()]
        if not cleaned:
            raise ValueError("exchange.symbols must contain at least one symbol")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("exchange.symbols contains duplicates")
        return cleaned


class MarketDataSection(StrictModel):
    orderbook_depth: Literal[50, 200, 500, 1000] = 200
    stale_after_ms: float = Field(default=2000.0, gt=0)
    max_data_age_ms: int = Field(default=500, gt=0)
    record_raw_events: bool = True
    record_feature_snapshots: bool = True
    reconnect_initial_ms: int = Field(default=500, gt=0)
    reconnect_max_ms: int = Field(default=30_000, gt=0)
    feature_window_ms: int = Field(default=60_000, gt=0)
    feature_interval_ms: int = Field(default=100, gt=0, le=5000)


class ScannerSection(StrictModel):
    enabled: bool = True
    max_symbols: int = Field(default=30, gt=0, le=200)
    universe_enabled: bool = False
    discovery_slots: int = Field(default=0, ge=0, le=100)
    discovery_low_liquidity: bool = False
    min_24h_volume_usdt: float = Field(default=100_000, ge=0)
    min_spread_pct: float = Field(default=0.30, ge=0)
    max_spread_pct: float = Field(default=5.00, gt=0)
    min_orderbook_depth_usdt: float = Field(default=500, ge=0)

    @model_validator(mode="after")
    def _ranges(self) -> ScannerSection:
        if self.min_spread_pct >= self.max_spread_pct:
            raise ValueError("scanner.min_spread_pct must be below scanner.max_spread_pct")
        return self


class StorageSection(StrictModel):
    database: Literal["postgresql", "none"] = "none"
    database_url: str | None = None
    historical_format: Literal["parquet"] = "parquet"
    raw_dir: str = "datasets/raw"
    derived_dir: str = "datasets/derived"
    save_raw_orderbook: bool = True
    rotation_mb: int = Field(default=256, gt=0)
    retention_days: int = Field(default=30, gt=0)
    queue_max_events: int = Field(default=100_000, gt=0)

    @model_validator(mode="after")
    def _db(self) -> StorageSection:
        if self.database == "postgresql" and not self.database_url:
            raise ValueError("storage.database_url is required when storage.database=postgresql")
        return self


class PostgresSection(StrictModel):
    host: str = "localhost"
    port: int = 5432
    database: str = "crypto_engine"
    user: str = "postgres"
    password: str = "postgres"
    min_pool: int = Field(default=2, ge=1)
    max_pool: int = Field(default=10, ge=1)


class ExecutionSection(StrictModel):
    """Настройки исполнения ордеров (PHASE 6)."""
    enabled: bool = False
    default_type: Literal["direct", "twap", "iceberg"] = "direct"
    twap_slices: int = Field(default=4, ge=1, le=20)
    twap_interval_s: float = Field(default=30.0, gt=0)
    iceberg_display_ratio: float = Field(default=0.25, gt=0, le=1.0)
    iceberg_refresh_ms: float = Field(default=500.0, gt=0)
    min_qty_for_twap: float = Field(default=10.0, gt=0)
    max_slippage_pct: float = Field(default=0.20, ge=0, le=5.0)


class ApiSection(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)
    ui_update_hz: float = Field(default=5.0, gt=0, le=60)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])


class RiskSection(StrictModel):
    """Заполняется и применяется в PHASE 6. Здесь только валидация схемы."""

    enabled: bool = False
    max_position_usdt: float = Field(default=50, gt=0)
    max_total_exposure_usdt: float = Field(default=100, gt=0)
    max_open_positions: int = Field(default=2, ge=0)
    max_daily_loss_usdt: float = Field(default=10, ge=0)
    max_slippage_pct: float = Field(default=0.20, ge=0)
    allow_market_orders: bool = False
    leverage_enabled: bool = False

    @model_validator(mode="after")
    def _consistency(self) -> RiskSection:
        if self.max_position_usdt > self.max_total_exposure_usdt:
            raise ValueError("risk.max_position_usdt cannot exceed risk.max_total_exposure_usdt")
        if self.leverage_enabled:
            raise ValueError("risk.leverage_enabled=true не поддерживается в MVP (SPOT)")
        return self


class UiSection(StrictModel):
    language: Literal["ru"] = "ru"
    theme: Literal["dark"] = "dark"


class Config(StrictModel):
    app: AppSection = AppSection()
    exchange: ExchangeSection = ExchangeSection()
    market_data: MarketDataSection = MarketDataSection()
    scanner: ScannerSection = ScannerSection()
    storage: StorageSection = StorageSection()
    postgres: PostgresSection = PostgresSection()
    execution: ExecutionSection = ExecutionSection()
    paper: PaperSettings = PaperSettings()
    api: ApiSection = ApiSection()
    risk: RiskSection = RiskSection()
    ui: UiSection = UiSection()

    @model_validator(mode="after")
    def _live_guard(self) -> Config:
        if self.app.mode == "live":
            raise ValueError(
                "app.mode=live недоступен: PHASE 7 не реализован. "
                "Допустимые режимы: research, backtest, paper."
            )
        if self.app.mode == "paper" and not self.risk.enabled:
            raise ValueError("paper mode requires risk.enabled=true")
        if self.execution.enabled:
            raise ValueError("execution.enabled=true недоступен: Live execution не прошёл приёмку")
        if self.exchange.market != "spot":
            raise ValueError("в MVP поддерживается только spot")
        if not self.market_data.record_raw_events and self.market_data.record_feature_snapshots:
            raise ValueError("нельзя писать признаки без записи событий: включите record_raw_events")
        return self
