from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrategySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    enabled: bool = False
    order_notional: Decimal = Field(default=Decimal("20"), gt=0, le=10000)
    min_edge_bps: Decimal = Field(default=Decimal("5"), ge=0)
    min_spread_bps: Decimal = Field(default=Decimal("25"), ge=0)
    min_obi: float = Field(default=0.25, ge=-1, le=1)
    min_wall_score: float = Field(default=60, ge=0, le=100)
    min_trades: int = Field(default=10, ge=1)
    adverse_selection_bps: Decimal = Field(default=Decimal("5"), ge=0)


class PaperSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    continuous: bool = False
    session_end_ns: int | None = Field(default=None, gt=0)
    isolate_stale_symbols: bool = False
    initial_cash: Decimal = Field(default=Decimal("1000"), gt=0)
    database_path: str = "datasets/paper/account.sqlite3"
    maker_fee_bps: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    taker_fee_bps: Decimal = Field(default=Decimal("10"), ge=0, le=100)
    submission_latency_ms: int = Field(default=100, ge=1, le=10000)
    cancel_latency_ms: int = Field(default=100, ge=1, le=10000)
    order_ttl_ms: int = Field(default=10000, ge=100, le=600000)
    cooldown_ms: int = Field(default=5000, ge=100)
    max_orders_per_minute: int = Field(default=30, ge=1, le=1000)
    order_history_limit: int = Field(default=500, ge=50, le=100_000)
    stop_loss_bps: Decimal = Field(default=Decimal("30"), gt=0)
    take_profit_bps: Decimal = Field(default=Decimal("40"), gt=0)
    max_hold_seconds: int = Field(default=180, ge=1)
    max_daily_loss_pct: Decimal = Field(default=Decimal("3"), gt=0, le=100)
    max_drawdown_pct: Decimal = Field(default=Decimal("10"), gt=0, le=100)
    max_consecutive_losses: int = Field(default=5, ge=1)
    max_spread_bps: Decimal = Field(default=Decimal("100"), gt=0)
    strategies: dict[str, StrategySettings] = Field(
        default_factory=lambda: {
            "spread_capture": StrategySettings(enabled=True),
            "liquidity_wall": StrategySettings(),
            "microstructure": StrategySettings(),
        }
    )

    @model_validator(mode="after")
    def validate_latency(self):
        if self.order_ttl_ms <= self.submission_latency_ms:
            raise ValueError("order_ttl_ms must exceed submission_latency_ms")
        if set(self.strategies) != {"spread_capture", "liquidity_wall", "microstructure"}:
            raise ValueError("all three strategy settings are required")
        return self
