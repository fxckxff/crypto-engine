from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.exchanges.base import TradingRules

D = Decimal
ACTIVE = {"SUBMITTING", "OPEN", "PARTIALLY_FILLED", "CANCEL_PENDING"}


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0 or not value.is_finite():
        raise ValueError("invalid quantity or step")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


@dataclass(frozen=True)
class Market:
    symbol: str
    ts_ns: int
    sequence: int
    ready: bool
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    rules: TradingRules
    obi: float = 0
    ofi: float = 0
    microprice_bps: float = 0
    buy_volume: Decimal = Decimal(0)
    sell_volume: Decimal = Decimal(0)
    trades: int = 0
    wall_score: float = 0
    wall_absorbed: Decimal = Decimal(0)
    wall_price: Decimal | None = None
    wall_persistent: bool = False

    @property
    def bid(self) -> Decimal:
        return self.bids[0][0] if self.bids else D(0)

    @property
    def ask(self) -> Decimal:
        return self.asks[0][0] if self.asks else D(0)

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2) * 10000 if self.bid > 0 else D(0)


@dataclass
class PaperOrder:
    id: str
    symbol: str
    side: str
    kind: str
    quantity: Decimal
    price: Decimal
    strategy: str
    reason: str
    created_ns: int
    active_ns: int
    expires_ns: int
    status: str = "SUBMITTING"
    filled: Decimal = Decimal(0)
    average_price: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    queue_ahead: Decimal = Decimal(0)
    cancel_ns: int | None = None
    acknowledged: bool = False

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled

    def to_dict(self) -> dict:
        # Terminal orders are immutable; avoid recursive dataclass deepcopy on every tick.
        if self.status not in ACTIVE and hasattr(self, "_terminal_snapshot"):
            return dict(self._terminal_snapshot)
        data = json_value({k: v for k, v in vars(self).items() if not k.startswith("_")})
        data["remaining"] = str(self.remaining)
        if self.status not in ACTIVE:
            self._terminal_snapshot = data
        return dict(data)

    @classmethod
    def restore(cls, data: dict) -> PaperOrder:
        data = dict(data)
        data.pop("remaining", None)
        for key in ("quantity", "price", "filled", "average_price", "fees", "queue_ahead"):
            data[key] = D(data[key])
        return cls(**data)


@dataclass
class PaperPosition:
    symbol: str
    strategy: str
    quantity: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)
    opened_ns: int = 0
    realized: Decimal = Decimal(0)
    exit_reason: str | None = None
    wall_price: Decimal | None = None

    @property
    def average_cost(self) -> Decimal:
        return self.cost / self.quantity if self.quantity else D(0)

    def to_dict(self) -> dict:
        return json_value(asdict(self))

    @classmethod
    def restore(cls, data: dict) -> PaperPosition:
        data = dict(data)
        for key in ("quantity", "cost", "realized"):
            data[key] = D(data[key])
        if data["wall_price"] is not None:
            data["wall_price"] = D(data["wall_price"])
        return cls(**data)


@dataclass
class Decision:
    strategy: str
    symbol: str
    allowed: bool
    reason: str
    edge_bps: Decimal = Decimal(0)
    components: dict = field(default_factory=dict)
