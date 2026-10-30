"""Детектор крупных лимитных стен и их жизненный цикл (ТЗ §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median

from app.analytics.flow import Trade
from app.core.enums import Aggressor, Side, WallState
from app.core.time import NS_MS
from app.market_data.orderbook import LocalOrderBook

ACTIVE_STATES = frozenset(
    {
        WallState.DETECTED,
        WallState.OBSERVING,
        WallState.PERSISTENT,
        WallState.TESTED,
        WallState.ABSORBING,
        WallState.WEAKENING,
    }
)
TERMINAL_STATES = frozenset({WallState.REMOVED, WallState.BROKEN, WallState.EXPIRED})


@dataclass(frozen=True, slots=True)
class WallConfig:
    """Research thresholds: relative size plus an absolute noise floor, not proof of intent."""

    min_wall_notional: Decimal = Decimal(500)
    wall_size_ratio: float = 5.0
    max_wall_distance_pct: float = 0.2
    scan_levels: int = 50
    reference_levels: int = 5
    observe_ms: float = 1_000.0
    persist_ms: float = 5_000.0
    test_tolerance_pct: float = 0.05
    absorption_ratio: float = 0.30
    weaken_fraction: float = 0.50
    expire_ms: float = 300_000.0
    max_observation_gap_ms: float = 2000.0
    quality_min_absorption: float = 0.10
    quality_min_touches: int = 2
    quality_min_score: float = 65.0


@dataclass(frozen=True, slots=True)
class Transition:
    from_state: WallState | None
    to_state: WallState
    ts_ns: int
    reason: str


@dataclass(slots=True)
class Wall:
    symbol: str
    side: Side
    price: Decimal
    initial_quantity: Decimal
    detected_ns: int
    state: WallState = WallState.DETECTED
    last_change_ns: int = 0
    peak_notional: Decimal = Decimal(0)
    current_notional: Decimal = Decimal(0)
    absorbed_quantity: Decimal = Decimal(0)
    tested_count: int = 0
    size_change_count: int = 0
    score: float = 0.0
    reference_notional: Decimal = Decimal(0)
    distance_pct: float = 0.0
    transitions: list[Transition] = field(default_factory=list)
    current_quantity: Decimal = Decimal(0)
    exact_touches: int = 0
    observed_since_ns: int = 0
    last_observed_ns: int = 0
    baseline_cancelled: Decimal = Decimal(0)
    baseline_added: Decimal = Decimal(0)
    cancelled_quantity_estimate: Decimal = Decimal(0)
    replenished_quantity_estimate: Decimal = Decimal(0)
    size_ratio: float = 0.0
    qualified: bool = False
    quality_reasons: list[str] = field(default_factory=list)
    score_components: dict[str, float] = field(default_factory=dict)

    @property
    def key(self) -> tuple[Side, Decimal]:
        return (self.side, self.price)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def transition(self, new_state: WallState, now_ns: int, reason: str) -> Transition:
        tr = Transition(self.state, new_state, now_ns, reason)
        self.state = new_state
        self.last_change_ns = now_ns
        self.transitions.append(tr)
        return tr


def reference_notional(notionals: list[Decimal], index: int, k: int) -> Decimal | None:
    """Устойчивая опорная величина по соседним уровням с защитой от нулевых объёмов."""
    neighbours = notionals[max(0, index - k) : index] + notionals[index + 1 : index + 1 + k]
    positive = [n for n in neighbours if n > 0]
    if not positive:
        positive = [n for n in notionals if n > 0 and n != notionals[index]]
    if not positive:
        return None
    return median(positive)


class WallDetector:
    def __init__(self, symbol: str, config: WallConfig | None = None) -> None:
        self.symbol = symbol
        self.config = config or WallConfig()
        self.walls: dict[tuple[Side, Decimal], Wall] = {}
        self._last_trade_id: int | None = None

    # ------------------------------------------------------------------ обновление

    def update(
        self,
        book: LocalOrderBook,
        now_ns: int,
        trades: list[Trade] | None = None,
    ) -> list[Transition]:
        """Один такт детектора. Возвращает только что случившиеся переходы состояний."""
        if not book.ready:
            self.invalidate_observation(now_ns)
            return []
        mid = book.mid_price()
        if mid is None or mid <= 0:
            return []

        trades = trades or []
        transitions = self._apply_trades(trades, now_ns)
        self._detect_candidates(book, mid, now_ns)
        transitions.extend(self._advance_states(book, now_ns))
        self._score(now_ns)
        self.walls = {
            key: wall
            for key, wall in self.walls.items()
            if wall.active or now_ns - wall.last_change_ns <= self.config.expire_ms * NS_MS
        }
        return transitions

    def invalidate_observation(self, now_ns: int) -> None:
        for wall in self.active_walls():
            wall.qualified = False
            wall.quality_reasons = ["Нет непрерывного свежего наблюдения"]
            wall.observed_since_ns = now_ns
            wall.absorbed_quantity = Decimal(0)
            wall.exact_touches = 0

    def active_walls(self) -> list[Wall]:
        return [w for w in self.walls.values() if w.active]

    # ------------------------------------------------------------------ обнаружение

    def _detect_candidates(self, book: LocalOrderBook, mid: Decimal, now_ns: int) -> None:
        for side in (Side.BID, Side.ASK):
            levels = book.side_levels(side, self.config.scan_levels)
            notionals = [lv.notional_usdt for lv in levels]
            for i, level in enumerate(levels):
                ref = reference_notional(notionals, i, self.config.reference_levels)
                if ref is None or ref <= 0:
                    continue
                # A quiet neighbour must not make an ordinary level look exceptional.
                other = [n for j, n in enumerate(notionals) if j != i and n > 0]
                if other:
                    ref = max(ref, median(other))
                ratio = float(level.notional_usdt / ref)
                distance = abs(float(level.price - mid)) / float(mid) * 100.0

                key = (side, level.price)
                wall = self.walls.get(key)
                if wall is not None and wall.active:
                    wall.current_notional = level.notional_usdt
                    wall.reference_notional = ref
                    wall.distance_pct = distance
                    wall.peak_notional = max(wall.peak_notional, level.notional_usdt)
                    continue

                if (
                    level.notional_usdt < self.config.min_wall_notional
                    or ratio < self.config.wall_size_ratio
                    or distance > self.config.max_wall_distance_pct
                ):
                    continue

                wall = Wall(
                    symbol=self.symbol,
                    side=side,
                    price=level.price,
                    initial_quantity=level.quantity,
                    detected_ns=now_ns,
                    last_change_ns=now_ns,
                    peak_notional=level.notional_usdt,
                    current_notional=level.notional_usdt,
                    reference_notional=ref,
                    distance_pct=distance,
                    size_change_count=0,
                    current_quantity=level.quantity,
                    observed_since_ns=now_ns,
                    last_observed_ns=now_ns,
                    baseline_cancelled=level.estimated_cancelled_quantity,
                    baseline_added=level.added_quantity,
                )
                self.walls[key] = wall

    # ------------------------------------------------------------------ сделки

    def _apply_trades(self, trades: list[Trade], now_ns: int) -> list[Transition]:
        transitions: list[Transition] = []
        for trade in trades:
            if trade.symbol != self.symbol or trade.quantity <= 0:
                continue
            if trade.trade_id is not None:
                if self._last_trade_id is not None and trade.trade_id <= self._last_trade_id:
                    continue
                self._last_trade_id = trade.trade_id
            for wall in self.walls.values():
                if not wall.active or (trade.receive_mono_ns is not None and trade.receive_mono_ns < wall.detected_ns):
                    continue
                expected = Aggressor.BUY if wall.side is Side.ASK else Aggressor.SELL
                if trade.aggressor is not expected:
                    continue
                if wall.side is Side.ASK:
                    touched = trade.price >= wall.price * (1 - Decimal(str(self.config.test_tolerance_pct)) / 100)
                    broken = trade.price > wall.price
                else:
                    touched = trade.price <= wall.price * (1 + Decimal(str(self.config.test_tolerance_pct)) / 100)
                    broken = trade.price < wall.price

                if broken:
                    transitions.append(
                        wall.transition(WallState.BROKEN, now_ns, f"trade {trade.price} through {wall.price}")
                    )
                    continue

                if touched:
                    wall.tested_count += 1
                    if wall.state in (WallState.DETECTED, WallState.OBSERVING, WallState.PERSISTENT):
                        transitions.append(
                            wall.transition(
                                WallState.TESTED, now_ns, f"trade at {trade.price}, count={wall.tested_count}"
                            )
                        )
                    if trade.price == wall.price and wall.state in (WallState.TESTED, WallState.ABSORBING):
                        wall.absorbed_quantity += trade.quantity
                        wall.exact_touches += 1
        return transitions

    # ------------------------------------------------------------------ состояния

    def _advance_states(self, book: LocalOrderBook, now_ns: int) -> list[Transition]:
        transitions: list[Transition] = []
        for wall in list(self.walls.values()):
            if not wall.active:
                continue

            level = book.level_at(wall.side, wall.price)
            age_ms = (now_ns - wall.detected_ns) / NS_MS

            if level is None:
                if self._traded_through(book, wall):
                    transitions.append(wall.transition(WallState.BROKEN, now_ns, "level removed after trading through"))
                else:
                    transitions.append(wall.transition(WallState.REMOVED, now_ns, "level removed from book"))
                continue

            if now_ns - wall.last_observed_ns > self.config.max_observation_gap_ms * NS_MS:
                wall.observed_since_ns = now_ns
                wall.absorbed_quantity = Decimal(0)
                wall.exact_touches = 0
                wall.baseline_cancelled = level.estimated_cancelled_quantity
                wall.baseline_added = level.added_quantity
            wall.last_observed_ns = now_ns
            wall.current_notional = level.notional_usdt
            wall.current_quantity = level.quantity
            mid = book.mid_price()
            if mid:
                wall.distance_pct = float(abs(wall.price - mid) / mid * 100)
            wall.cancelled_quantity_estimate = max(
                Decimal(0), level.estimated_cancelled_quantity - wall.baseline_cancelled
            )
            wall.replenished_quantity_estimate = max(Decimal(0), level.added_quantity - wall.baseline_added)
            if wall.distance_pct > self.config.max_wall_distance_pct:
                transitions.append(wall.transition(WallState.EXPIRED, now_ns, "outside observation distance"))
                continue
            if level.quantity < wall.initial_quantity * Decimal(str(self.config.weaken_fraction)):
                if wall.state in (WallState.PERSISTENT, WallState.TESTED, WallState.ABSORBING):
                    transitions.append(
                        wall.transition(
                            WallState.WEAKENING,
                            now_ns,
                            f"size {level.quantity} below {self.config.weaken_fraction:.0%}"
                            f" of initial {wall.initial_quantity}",
                        )
                    )
                elif wall.state is WallState.WEAKENING and level.quantity <= 0:
                    transitions.append(wall.transition(WallState.REMOVED, now_ns, "empty"))

            if wall.absorbed_quantity >= wall.initial_quantity * Decimal(str(self.config.absorption_ratio)):
                if wall.state in (WallState.TESTED, WallState.PERSISTENT):
                    transitions.append(
                        wall.transition(
                            WallState.ABSORBING,
                            now_ns,
                            f"absorbed {wall.absorbed_quantity} of {wall.initial_quantity}",
                        )
                    )

            if wall.state is WallState.ABSORBING and age_ms > self.config.expire_ms:
                transitions.append(wall.transition(WallState.EXPIRED, now_ns, "absorption window exceeded"))

            if wall.state is WallState.DETECTED and age_ms >= self.config.observe_ms:
                transitions.append(wall.transition(WallState.OBSERVING, now_ns, "survived observe window"))

            if wall.state is WallState.OBSERVING and age_ms >= self.config.persist_ms:
                transitions.append(wall.transition(WallState.PERSISTENT, now_ns, "survived persist window"))

            if wall.state in (WallState.DETECTED, WallState.OBSERVING) and age_ms > self.config.expire_ms:
                transitions.append(wall.transition(WallState.EXPIRED, now_ns, "aged out without events"))

        return transitions

    @staticmethod
    def _traded_through(book: LocalOrderBook, wall: Wall) -> bool:
        bid, ask = book.best_bid(), book.best_ask()
        if wall.side is Side.ASK:
            return bid is not None and bid.price >= wall.price
        return ask is not None and ask.price <= wall.price

    # ------------------------------------------------------------------ сила

    def _score(self, now_ns: int) -> None:
        cfg = self.config
        for wall in self.active_walls():
            age_ms = max(0, now_ns - wall.observed_since_ns) / NS_MS
            ratio = float(wall.current_notional / wall.reference_notional) if wall.reference_notional > 0 else 0.0
            wall.size_ratio = ratio
            absorption = float(wall.absorbed_quantity / wall.initial_quantity) if wall.initial_quantity else 0.0
            cancelled = (
                float(wall.cancelled_quantity_estimate / wall.initial_quantity) if wall.initial_quantity else 0.0
            )
            components = {
                "size": min(ratio / 10.0, 1.0) * 25,
                "persistence": min(age_ms / max(cfg.persist_ms, 1), 1.0) * 20,
                "absorption": min(absorption / 0.30, 1.0) * 25,
                "retention": max(0.0, 1.0 - cancelled) * 15,
                "proximity": max(0.0, 1.0 - wall.distance_pct / max(cfg.max_wall_distance_pct, 0.0001)) * 15,
                "withdrawal_penalty": -min(cancelled, 1.0) * 30,
            }
            reasons = []
            if wall.current_notional < cfg.min_wall_notional or ratio < cfg.wall_size_ratio:
                reasons.append("Размер больше не выделяется относительно стакана")
            if age_ms < cfg.persist_ms:
                reasons.append("Недостаточно непрерывного наблюдения")
            if wall.exact_touches < cfg.quality_min_touches or absorption < cfg.quality_min_absorption:
                reasons.append("Недостаточно агрессивных сделок точно по цене стены")
            if cancelled >= 0.5:
                reasons.append("Значительное снятие объёма без подтверждения сделками (оценка)")
            if wall.state is WallState.WEAKENING:
                reasons.append("Стена ослабевает")
            wall.score_components = {key: round(value, 2) for key, value in components.items()}
            wall.score = round(max(0, min(sum(components.values()), 100)), 2)
            if wall.score < cfg.quality_min_score:
                reasons.append("Оценка ниже порога качества")
            wall.qualified = not reasons
            wall.quality_reasons = reasons or ["Размер, устойчивость и поглощение подтверждены наблюдениями"]
