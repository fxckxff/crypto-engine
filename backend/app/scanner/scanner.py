"""Сканер инструментов (ТЗ §12).

Оценка Opportunity Score — эвристика с явными весами. Она НЕ является статистически
обоснованной моделью доходности: это ранжирование для отбора кандидатов на исследование.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.analytics.engine import FeatureSnapshot
from app.analytics.walls import Wall
from app.config.schema import ScannerSection
from app.core.enums import BookIntegrity, Side
from app.exchanges.base import Instrument, TradingRules
from app.market_data.orderbook import LocalOrderBook


@dataclass(frozen=True, slots=True)
class OpportunityComponents:
    spread_opportunity: float
    execution_probability: float
    liquidity_quality: float
    adverse_selection_cost: float
    volatility_risk: float
    total_cost: float


@dataclass(frozen=True, slots=True)
class ScannerRow:
    symbol: str
    eligible: bool
    exclude_reason: str | None
    spread_pct: float | None
    spread_ticks: float | None
    bid_depth_usdt: Decimal
    ask_depth_usdt: Decimal
    depth_imbalance: float | None
    quote_age_ms: float | None
    book_state: BookIntegrity
    opportunity_score: float
    components: OpportunityComponents | None
    volume_24h_usdt: Decimal | None = None
    wall_count: int = 0
    top_wall_score: float = 0.0
    notes: list[str] = field(default_factory=list)


class SymbolScanner:
    def __init__(
        self,
        config: ScannerSection,
        instruments: dict[str, Instrument],
        rules: dict[str, TradingRules],
        max_position_usdt: float = 50.0,
    ) -> None:
        self.config = config
        self.instruments = instruments
        self.rules = rules
        self.max_position_usdt = max_position_usdt
        # Обновляется сервисом из /api/v3/ticker/24hr. Пусто — объём неизвестен.
        self.volume_24h_usdt: dict[str, Decimal] = {}

    def evaluate(
        self,
        snapshot: FeatureSnapshot,
        book: LocalOrderBook,
        walls: list[Wall] | None = None,
    ) -> ScannerRow:
        symbol = snapshot.symbol
        bid_depth = book.total_notional(Side.BID, 20)
        ask_depth = book.total_notional(Side.ASK, 20)
        instrument = self.instruments.get(symbol)
        rules = self.rules.get(symbol)
        volume = self.volume_24h_usdt.get(symbol)
        wall_list = walls or []
        wall_count = len(wall_list)
        top_wall_score = max((wall.score for wall in wall_list), default=0.0)

        reason = self._exclusion(snapshot, bid_depth, ask_depth, instrument, rules, volume)
        notes: list[str] = []
        if volume is None:
            notes.append("24h объём неизвестен: эндпоинт тикера ещё не ответил или недоступен")
        if reason is not None:
            return ScannerRow(
                symbol=symbol,
                eligible=False,
                exclude_reason=reason,
                spread_pct=snapshot.spread.spread_pct if snapshot.spread else None,
                spread_ticks=snapshot.spread.spread_ticks if snapshot.spread else None,
                bid_depth_usdt=bid_depth,
                ask_depth_usdt=ask_depth,
                depth_imbalance=snapshot.obi.get(10),
                quote_age_ms=None,
                book_state=snapshot.book_state,
                opportunity_score=0.0,
                components=None,
                volume_24h_usdt=volume,
                wall_count=wall_count,
                top_wall_score=top_wall_score,
                notes=notes,
            )

        assert snapshot.spread is not None
        components = self._components(snapshot, bid_depth, ask_depth)
        score = (
            0.30 * components.spread_opportunity
            + 0.20 * components.execution_probability
            + 0.20 * components.liquidity_quality
            + 0.15 * (100.0 - components.adverse_selection_cost)
            + 0.15 * (100.0 - components.volatility_risk)
        )
        return ScannerRow(
            symbol=symbol,
            eligible=True,
            exclude_reason=None,
            spread_pct=snapshot.spread.spread_pct,
            spread_ticks=snapshot.spread.spread_ticks,
            bid_depth_usdt=bid_depth,
            ask_depth_usdt=ask_depth,
            depth_imbalance=snapshot.obi.get(10),
            quote_age_ms=snapshot.book_age_ms,
            book_state=snapshot.book_state,
            opportunity_score=round(score, 2),
            components=components,
            volume_24h_usdt=volume,
            wall_count=wall_count,
            top_wall_score=top_wall_score,
            notes=notes,
        )

    # ------------------------------------------------------------------ фильтры

    def _exclusion(
        self,
        snapshot: FeatureSnapshot,
        bid_depth: Decimal,
        ask_depth: Decimal,
        instrument: Instrument | None,
        rules: TradingRules | None,
        volume_24h_usdt: Decimal | None,
    ) -> str | None:
        if snapshot.book_state is not BookIntegrity.READY:
            return f"book {snapshot.book_state.value}"
        if instrument is None:
            return "instrument unknown"
        if not instrument.trading_allowed:
            return f"trading not allowed (status={instrument.status})"
        if rules is None:
            return "trading rules unavailable"
        if snapshot.spread is None:
            return "spread unavailable"
        if snapshot.spread.spread_pct < self.config.min_spread_pct:
            return f"spread {snapshot.spread.spread_pct:.3f}% below min {self.config.min_spread_pct}%"
        if snapshot.spread.spread_pct > self.config.max_spread_pct:
            return f"spread {snapshot.spread.spread_pct:.3f}% above max {self.config.max_spread_pct}%"
        if min(bid_depth, ask_depth) < Decimal(str(self.config.min_orderbook_depth_usdt)):
            return "orderbook depth below minimum"
        if volume_24h_usdt is not None and volume_24h_usdt < Decimal(str(self.config.min_24h_volume_usdt)):
            return (
                f"24h volume {volume_24h_usdt:.0f} USDT below min "
                f"{self.config.min_24h_volume_usdt:.0f}"
            )
        if rules.min_notional > Decimal(str(self.max_position_usdt)):
            return "min order notional exceeds allowed position"
        return None

    # ------------------------------------------------------------------ скоринг

    def _components(self, snapshot: FeatureSnapshot, bid_depth: Decimal, ask_depth: Decimal) -> OpportunityComponents:
        assert snapshot.spread is not None
        spread_pct = snapshot.spread.spread_pct

        # 1. Ширина спреда относительно порога: 0..100
        span = max(self.config.max_spread_pct - self.config.min_spread_pct, 1e-9)
        spread_opportunity = _clamp((spread_pct - self.config.min_spread_pct) / span * 100.0, 0.0, 100.0)

        # 2. Вероятность исполнения: сколько минимальных заявок влезает в видимую глубину
        min_notional = Decimal(str(max(self.max_position_usdt, 1.0)))
        sides = float(min(bid_depth, ask_depth) / min_notional)
        execution_probability = _clamp(sides * 20.0, 0.0, 100.0)

        # 3. Качество ликвидности: логарифм глубины + сбалансированность сторон
        depth_usdt = float(min(bid_depth, ask_depth))
        depth_score = _clamp(20.0 * _log10(depth_usdt / 500.0 + 1.0), 0.0, 100.0)
        imbalance = abs(snapshot.obi.get(10) or 0.0)
        balance_score = (1.0 - min(imbalance, 1.0)) * 100.0
        liquidity_quality = 0.7 * depth_score + 0.3 * balance_score

        # 4. Adverse selection: прокси — сила потока против пассивной стороны
        flow = snapshot.flow
        total_volume = flow.buy_volume + flow.sell_volume
        imbalance_flow = abs(float((flow.buy_volume - flow.sell_volume) / total_volume)) if total_volume > 0 else 0.0
        adverse_selection_cost = _clamp(imbalance_flow * 100.0, 0.0, 100.0)

        # 5. Риск волатильности: разброс спреда и скорость microprice
        vol = snapshot.spread.volatility or 0.0
        volatility_risk = _clamp(vol / max(snapshot.spread.rolling_median or 1.0, 1e-9) * 50.0, 0.0, 100.0)

        # 6. Полная стоимость: спред + консервативная комиссия 20 bps на круг
        fee_bps = 20.0
        total_cost = _clamp(spread_pct * 100.0 + fee_bps, 0.0, 1000.0)

        return OpportunityComponents(
            spread_opportunity=round(spread_opportunity, 2),
            execution_probability=round(execution_probability, 2),
            liquidity_quality=round(liquidity_quality, 2),
            adverse_selection_cost=round(adverse_selection_cost, 2),
            volatility_risk=round(volatility_risk, 2),
            total_cost=round(total_cost, 2),
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _log10(value: float) -> float:
    import math

    return math.log10(max(value, 1e-12))
