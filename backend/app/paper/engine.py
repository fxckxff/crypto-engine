"""Deterministic spot paper account. All mutations are serialized and committed by runtime."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

from app.config.schema import RiskSection
from app.paper.config import PaperSettings, StrategySettings
from app.paper.models import ACTIVE, Market, PaperOrder, PaperPosition, floor_step, json_value
from app.paper.signals import evaluate

D = Decimal
MS = 1_000_000


class PaperEngine:
    def __init__(self, settings: PaperSettings, risk: RiskSection, max_age_ms: int = 500):
        self.settings = settings
        self.risk = risk
        self.max_age_ms = max_age_ms
        self.cash = settings.initial_cash
        self.initial_cash = settings.initial_cash
        self.realized = D(0)
        self.fees = D(0)
        self.orders: dict[str, PaperOrder] = {}
        self.positions: dict[str, PaperPosition] = {}
        self.last_trade_ids: dict[str, int] = {}
        self.markets: dict[str, Market] = {}
        self.marks: dict[str, Decimal] = {}
        self.running = False
        self.halt_reason: str | None = "Торговля на паузе"
        self.flattening = False
        self.data_paused = False
        self.revision = 0
        self.events: list[dict] = []
        self.decisions: dict[str, dict] = {}
        self.last_decision_ns: dict[str, int] = {}
        self.order_times: list[int] = []
        self.cooldowns: dict[str, int] = {}
        self.consecutive_losses = 0
        self.day = ""
        self.day_equity = self.initial_cash
        self.peak_equity = self.initial_cash
        self.consumed: dict[str, tuple[int, dict[str, Decimal]]] = {}

    def emit(self, kind: str, now: int, **data) -> None:
        self.revision += 1
        self.events.append(json_value({"sequence": self.revision, "kind": kind, "ts_ns": now, **data}))

    def active(self, symbol: str | None = None) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.status in ACTIVE and (symbol is None or o.symbol == symbol)]

    @property
    def reserved_cash(self) -> Decimal:
        fee = max(self.settings.maker_fee_bps, self.settings.taker_fee_bps) / 10000
        return sum((o.remaining * o.price * (1 + fee) for o in self.active() if o.side == "BUY"), D(0))

    @property
    def equity(self) -> Decimal:
        return self.cash + sum(
            (p.quantity * self.marks.get(p.symbol, p.average_cost) for p in self.positions.values()), D(0)
        )

    def fresh(self, market: Market | None, now: int) -> bool:
        return bool(
            market
            and market.ready
            and 0 <= now - market.ts_ns <= self.max_age_ms * MS
            and market.bid > 0
            and market.ask > market.bid
        )

    def loss_reason(self) -> str | None:
        if self.settings.continuous:
            return None
        equity = self.equity
        loss = self.day_equity - equity
        if loss >= D(str(self.risk.max_daily_loss_usdt)):
            return "Дневной лимит убытка USDT"
        if loss >= self.day_equity * self.settings.max_daily_loss_pct / 100:
            return "Дневной лимит просадки"
        if self.peak_equity - equity >= self.peak_equity * self.settings.max_drawdown_pct / 100:
            return "Максимальная просадка счёта"
        if self.consecutive_losses >= self.settings.max_consecutive_losses:
            return "Лимит убыточных сделок подряд"
        return None

    def check_deadline(self, now: int) -> None:
        if self.settings.session_end_ns and now >= self.settings.session_end_ns and self.running:
            self.stop(now, "Время Paper-сессии истекло", flatten=True)

    def start(self, now: int) -> None:
        if self.settings.session_end_ns and now >= self.settings.session_end_ns:
            raise ValueError("Время Paper-сессии истекло")
        if set(self.positions) - set(self.markets):
            raise ValueError("Восстановленные позиции отсутствуют в списке отслеживаемых инструментов")
        fresh = [self.fresh(m, now) for m in self.markets.values()]
        ready = any(fresh) if self.settings.isolate_stale_symbols else all(fresh)
        if not fresh or not ready:
            raise ValueError("Нет свежих синхронизированных стаканов")
        reason = self.loss_reason()
        if reason:
            raise ValueError(reason)
        self.running, self.flattening, self.halt_reason = True, False, None
        self.data_paused = False
        self.emit("START", now)

    def stop(self, now: int, reason: str = "Остановлено пользователем", flatten: bool = False) -> None:
        self.running = False
        self.data_paused = False
        self.flattening = flatten
        self.halt_reason = reason
        for order in self.active():
            # This cancels LOCAL simulated orders, not a claim about exchange cancellation.
            order.status = "CANCELLED"
            self.emit("CANCELLED", now, order=order.to_dict())
        if flatten:
            for position in self.positions.values():
                position.exit_reason = "EMERGENCY_EXIT"
                self.cooldowns[position.symbol] = now
        self.emit("STOP", now, reason=reason, flatten=flatten)

    def pause_data(self, now: int, reason: str) -> None:
        # Only an already running session can opt into data recovery.
        if self.running or self.data_paused:
            self.stop(now, reason)
            self.data_paused = True
            self.emit("DATA_PAUSED", now, reason=reason)

    def resume_data(self, now: int) -> None:
        if not self.data_paused:
            return
        if self.settings.session_end_ns and now >= self.settings.session_end_ns:
            self.stop(now, "Время Paper-сессии истекло", flatten=True)
            return
        reason = self.loss_reason()
        if reason:
            self.stop(now, reason, flatten=True)
            return
        self.start(now)
        self.emit("DATA_RESUMED", now)

    def set_continuous(self, enabled: bool, now: int) -> None:
        update: dict[str, object] = {"continuous": enabled}
        if enabled:
            update["session_end_ns"] = None
        self.settings = self.settings.model_copy(update=update)
        self.emit("SESSION_POLICY", now, continuous=enabled, session_end_ns=self.settings.session_end_ns)

    def cancel(self, order_id: str, now: int) -> PaperOrder:
        order = self.orders.get(order_id)
        if order is None:
            raise ValueError("Ордер не найден")
        if order.status in ACTIVE and order.cancel_ns is None:
            order.cancel_ns = now + self.settings.cancel_latency_ms * MS
            order.status = "CANCEL_PENDING"
            self.emit("CANCEL_PENDING", now, order=order.to_dict())
        return order

    def update_strategy(self, name: str, settings: StrategySettings, now: int) -> None:
        if name not in self.settings.strategies:
            raise ValueError("Стратегия не найдена")
        strategies = {**self.settings.strategies, name: settings}
        self.settings = self.settings.model_copy(update={"strategies": strategies})
        if not settings.enabled:
            for order in self.active():
                if order.strategy == name and order.side == "BUY":
                    self.cancel(order.id, now)
        self.emit("STRATEGY_CONFIG", now, strategy=name, config=settings.model_dump(mode="json"))

    def submit(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        now: int,
        client_id: str,
        strategy: str = "manual",
        reason: str = "MANUAL",
        kind: str = "POST_ONLY",
    ) -> PaperOrder:
        self.check_deadline(now)
        previous = self.orders.get(client_id)
        if previous:
            if (previous.symbol, previous.side, previous.quantity, previous.price, previous.kind) != (
                symbol,
                side,
                quantity,
                price,
                kind,
            ):
                raise ValueError("client_order_id уже использован с другими параметрами")
            return previous
        market = self.markets.get(symbol)
        error = self._validate(symbol, side, quantity, price, now, kind, market)
        order = PaperOrder(
            client_id,
            symbol,
            side,
            kind,
            quantity,
            price,
            strategy,
            reason,
            now,
            now + self.settings.submission_latency_ms * MS,
            now + self.settings.order_ttl_ms * MS,
        )
        self.orders[client_id] = order
        if error:
            order.status = "REJECTED"
            order.reason = error
            self._prune_orders()
            self.emit("REJECTED", now, order=order.to_dict())
            return order
        self.order_times.append(now)
        self._prune_orders()
        self.emit("SUBMITTING", now, order=order.to_dict())
        return order

    def _validate(self, symbol, side, quantity, price, now, kind, market) -> str | None:
        if side not in ("BUY", "SELL") or kind not in ("POST_ONLY", "IOC"):
            return "Неподдерживаемая сторона или тип"
        if not quantity.is_finite() or not price.is_finite() or quantity <= 0 or price <= 0:
            return "Цена и количество должны быть положительными конечными числами"
        if not self.fresh(market, now):
            return "Нет свежего стакана"
        rules = market.rules
        if "LIMIT" not in rules.allowed_order_types:
            return "LIMIT не поддерживается инструментом"
        if quantity != floor_step(quantity, rules.step_size) or price != floor_step(price, rules.tick_size):
            return "Нарушение step_size или tick_size"
        if quantity < rules.min_quantity or quantity > rules.max_quantity or quantity * price < rules.min_notional:
            return "Нарушение min/max quantity или min_notional"
        if self.active(symbol):
            return "Для инструмента уже есть активный ордер"
        self.order_times = [ts for ts in self.order_times if now - ts < 60_000 * MS]
        if len(self.order_times) >= self.settings.max_orders_per_minute:
            return "Лимит ордеров в минуту"
        if kind == "POST_ONLY" and (
            (side == "BUY" and price >= market.ask) or (side == "SELL" and price <= market.bid)
        ):
            return "Post-only ордер пересекает стакан"
        if kind == "IOC":
            reference = market.ask if side == "BUY" else market.bid
            slippage = D(str(self.risk.max_slippage_pct)) / 100
            if (side == "BUY" and price > reference * (1 + slippage)) or (
                side == "SELL" and price < reference * (1 - slippage)
            ):
                return "Цена за пределами лимита проскальзывания"
        if side == "SELL":
            position = self.positions.get(symbol)
            if not position or quantity > position.quantity:
                return "Недостаточно актива: short на spot запрещён"
            if not self.running and not self.flattening:
                return "Счёт на паузе"
            return None
        if not self.running:
            return "Новые входы остановлены"
        if self.loss_reason():
            return self.loss_reason()
        if market.spread_bps > self.settings.max_spread_bps:
            return "Аномально широкий спред"
        if now < self.cooldowns.get(symbol, 0):
            return "Cooldown после сделки"
        position = self.positions.get(symbol)
        if position and position.quantity > 0:
            return "Усреднение позиции отключено"
        if quantity * price > D(str(self.risk.max_position_usdt)):
            return "Лимит позиции"
        exposure = sum(
            (
                p.quantity * max(p.average_cost, self.marks.get(p.symbol, p.average_cost))
                for p in self.positions.values()
            ),
            D(0),
        )
        pending = sum((o.remaining * o.price for o in self.active() if o.side == "BUY"), D(0))
        if exposure + pending + quantity * price > D(str(self.risk.max_total_exposure_usdt)):
            return "Лимит общей экспозиции с учётом заявок"
        symbols = {p.symbol for p in self.positions.values() if p.quantity > 0 and not self.is_dust(p)}
        symbols.update(o.symbol for o in self.active() if o.side == "BUY")
        if self.risk.max_open_positions and symbol not in symbols and len(symbols) >= self.risk.max_open_positions:
            return "Лимит числа позиций с учётом заявок"
        fee = max(self.settings.maker_fee_bps, self.settings.taker_fee_bps) / 10000
        if quantity * price * (1 + fee) > self.cash - self.reserved_cash:
            return "Недостаточно свободного USDT с учётом комиссии"
        return None

    def _prune_orders(self) -> None:
        """Bound order history: terminal orders are pruned, active ones are kept.

        Called on every submission (both accepted and rejected). Everything in
        ``orders`` is serialized into the durable account state, so an unbounded
        history makes every state commit grow without limit.
        """
        limit = self.settings.order_history_limit
        if len(self.orders) <= limit:
            return
        terminal = [key for key, order in self.orders.items() if order.status not in ACTIVE]
        excess = len(terminal) - limit
        if excess <= 0:
            return
        for key in terminal[:excess]:
            del self.orders[key]

    def _activate(self, order: PaperOrder, now: int) -> bool:
        if order.cancel_ns is not None and now >= order.cancel_ns:
            order.status = "CANCELLED"
            self.emit("CANCELLED", now, order=order.to_dict())
            return False
        if now >= order.expires_ns:
            order.status = "EXPIRED"
            self.emit("EXPIRED", now, order=order.to_dict())
            return False
        if now < order.active_ns:
            return False
        market = self.markets.get(order.symbol)
        if not self.fresh(market, now):
            return False
        if not order.acknowledged:
            assert market is not None
            crosses = (order.side == "BUY" and order.price >= market.ask) or (
                order.side == "SELL" and order.price <= market.bid
            )
            if order.kind == "POST_ONLY" and crosses:
                order.status = "REJECTED"
                order.reason = "Post-only пересёк стакан за время задержки"
                self.emit("REJECTED", now, order=order.to_dict())
                return False
            levels = market.bids if order.side == "BUY" else market.asks
            order.queue_ahead = sum((qty for price, qty in levels if price == order.price), D(0))
            order.acknowledged = True
            order.status = "CANCEL_PENDING" if order.cancel_ns is not None else "OPEN"
            self.emit("OPEN", now, order=order.to_dict())
        return True

    def _fill(
        self, order: PaperOrder, quantity: Decimal, price: Decimal, now: int, maker: bool, source_id: str
    ) -> None:
        quantity = min(quantity, order.remaining)
        if quantity <= 0:
            return
        fee = price * quantity * (self.settings.maker_fee_bps if maker else self.settings.taker_fee_bps) / 10000
        pnl = D(0)
        if order.side == "BUY":
            if price * quantity + fee > self.cash:
                raise RuntimeError("paper cash invariant violated")
            self.cash -= price * quantity + fee
            position = self.positions.setdefault(
                order.symbol, PaperPosition(order.symbol, order.strategy, opened_ns=now)
            )
            position.quantity += quantity
            position.cost += price * quantity + fee
            market = self.markets.get(order.symbol)
            if order.strategy == "liquidity_wall" and market:
                position.wall_price = market.wall_price
        else:
            position = self.positions[order.symbol]
            if quantity > position.quantity:
                raise RuntimeError("paper inventory invariant violated")
            basis = position.average_cost * quantity
            pnl = price * quantity - fee - basis
            position.quantity -= quantity
            position.cost -= basis
            position.realized += pnl
            self.cash += price * quantity - fee
            self.realized += pnl
            if position.quantity == 0:
                self.consecutive_losses = self.consecutive_losses + 1 if position.realized < 0 else 0
                self.emit(
                    "POSITION_CLOSED",
                    now,
                    symbol=position.symbol,
                    realized=position.realized,
                    strategy=position.strategy,
                    reason=order.reason,
                )
                del self.positions[order.symbol]
                self.cooldowns[order.symbol] = now + self.settings.cooldown_ms * MS
        order.average_price = (order.average_price * order.filled + price * quantity) / (order.filled + quantity)
        order.filled += quantity
        order.fees += fee
        self.fees += fee
        order.status = "FILLED" if order.remaining == 0 else "PARTIALLY_FILLED"
        if order.cancel_ns is not None and order.remaining > 0:
            order.status = "CANCEL_PENDING"
        self.emit(
            "FILL",
            now,
            order_id=order.id,
            symbol=order.symbol,
            strategy=order.strategy,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            realized=pnl,
            maker=maker,
            source_id=source_id,
            order=order.to_dict(),
        )

    def trade(
        self,
        symbol: str,
        trade_id: int,
        price: Decimal,
        quantity: Decimal,
        aggressor: str,
        now: int,
        exchange_ns: int | None = None,
    ) -> None:
        self.check_deadline(now)
        if trade_id <= self.last_trade_ids.get(symbol, -1):
            return
        self.last_trade_ids[symbol] = trade_id
        for order in self.active(symbol):
            if order.kind != "POST_ONLY" or not self._activate(order, now):
                continue
            if exchange_ns is not None and exchange_ns < order.active_ns:
                continue
            matched = (order.side == "BUY" and aggressor == "SELL") or (order.side == "SELL" and aggressor == "BUY")
            if not matched or price != order.price:
                continue
            consumed = min(quantity, order.queue_ahead)
            order.queue_ahead -= consumed
            if consumed:
                self.emit("QUEUE", now, order_id=order.id, queue_ahead=order.queue_ahead)
            self._fill(order, quantity - consumed, price, now, True, f"trade:{symbol}:{trade_id}")

    def _ioc(self, order: PaperOrder, market: Market, now: int) -> None:
        if order.kind != "IOC" or not self._activate(order, now):
            return
        seq, used = self.consumed.get(market.symbol, (market.sequence, {}))
        if seq != market.sequence:
            used = {}
        for price, quantity in market.asks if order.side == "BUY" else market.bids:
            if (order.side == "BUY" and price > order.price) or (order.side == "SELL" and price < order.price):
                break
            key = f"{order.side}:{price}"
            fill = min(max(D(0), quantity - used.get(key, D(0))), order.remaining)
            if fill:
                self._fill(order, fill, price, now, False, f"book:{market.symbol}:{market.sequence}:{price}")
                used[key] = used.get(key, D(0)) + fill
            if order.remaining == 0:
                break
        self.consumed[market.symbol] = (market.sequence, used)
        if order.remaining > 0:
            order.status = "EXPIRED"
            self.emit("EXPIRED", now, order=order.to_dict())

    def market(self, market: Market, now: int) -> None:
        self.check_deadline(now)
        self.markets[market.symbol] = market
        if market.bid > 0:
            self.marks[market.symbol] = market.bid
        day = datetime.fromtimestamp(now / 1e9, UTC).date().isoformat()
        if day != self.day:
            self.day, self.day_equity = day, self.equity
            self.emit("DAY_START", now, day=day, equity=self.day_equity)
        self.peak_equity = max(self.peak_equity, self.equity)
        if not self.fresh(market, now):
            if self.settings.isolate_stale_symbols:
                for order in self.active(market.symbol):
                    order.status = "CANCELLED"
                    self.emit("CANCELLED", now, order=order.to_dict(), reason="STALE_SYMBOL")
            elif self.running or self.active(market.symbol):
                self.pause_data(now, "Потеря целостности или свежести данных")
            return
        reason = self.loss_reason()
        if reason and self.running:
            self.stop(now, reason, flatten=True)
        for order in list(self.active(market.symbol)):
            if not self._activate(order, now):
                continue
            self._ioc(order, market, now)
        if self.flattening and not self.positions:
            self.flattening = False
            self.halt_reason = "Позиции закрыты; новые входы остановлены"
            self.emit("FLATTEN_COMPLETE", now)
        position = self.positions.get(market.symbol)
        if position and position.quantity > 0 and (self.running or self.flattening):
            self._exit(position, market, now)
        if not self.running or position or self.active(market.symbol):
            return
        candidates = []
        for name, settings in self.settings.strategies.items():
            decision = evaluate(name, settings, self.settings, market)
            key = f"{market.symbol}:{name}"
            data = json_value(asdict(decision))
            previous = self.decisions.get(key)
            self.decisions[key] = data
            if (
                not previous
                or previous["reason"] != data["reason"]
                or now - self.last_decision_ns.get(key, 0) > 5_000 * MS
            ):
                self.emit("DECISION", now, **data)
                self.last_decision_ns[key] = now
            if decision.allowed:
                candidates.append((decision.edge_bps, name, settings, decision))
        if candidates and now >= self.cooldowns.get(market.symbol, 0):
            _, name, settings, decision = max(candidates, key=lambda item: (item[0], item[1] != "spread_capture"))
            qty = floor_step(settings.order_notional / market.bid, market.rules.step_size)
            self.submit(
                market.symbol, "BUY", qty, market.bid, now, f"paper-{self.revision + 1:012d}", name, decision.reason
            )
            self.cooldowns[market.symbol] = now + self.settings.cooldown_ms * MS

    def is_dust(self, position: PaperPosition) -> bool:
        market = self.markets.get(position.symbol)
        if market is None or market.bid <= 0:
            return False  # Unknown rules/valuation cannot free a slot.
        quantity = floor_step(position.quantity, market.rules.step_size)
        return quantity < market.rules.min_quantity or quantity * market.bid < market.rules.min_notional

    def _exit(self, position: PaperPosition, market: Market, now: int) -> None:
        minimum_quantity = floor_step(position.quantity, market.rules.step_size)
        if minimum_quantity < market.rules.min_quantity or minimum_quantity * market.bid < market.rules.min_notional:
            if position.exit_reason != "DUST_BELOW_MIN_NOTIONAL":
                position.exit_reason = "DUST_BELOW_MIN_NOTIONAL"
                self.emit("EXIT_BLOCKED", now, symbol=position.symbol, reason=position.exit_reason)
            return
        net_bid = market.bid * (1 - self.settings.taker_fee_bps / 10000)
        change = (net_bid / position.average_cost - 1) * 10000
        reason = None if position.exit_reason == "DUST_BELOW_MIN_NOTIONAL" else position.exit_reason
        if reason == "EMERGENCY_EXIT":
            pass
        elif change <= -self.settings.stop_loss_bps:
            reason = "STOP_LOSS"
        elif now - position.opened_ns >= self.settings.max_hold_seconds * 1_000_000_000:
            reason = "TIME_EXIT"
        elif position.wall_price is not None and market.bid < position.wall_price:
            reason = "WALL_FAILURE"
        elif market.obi < -0.5 and market.ofi < 0 and market.sell_volume > market.buy_volume:
            reason = "WEAKNESS_EXIT"
        elif change >= self.settings.take_profit_bps:
            # Тейк-профит теперь не вызывает немедленный IOC-выход,
            # позволяя POST_ONLY ордеру исполниться как maker.
            pass
        if reason != position.exit_reason:
            position.exit_reason = reason
            self.emit("EXIT_REASON", now, symbol=position.symbol, reason=reason)
        active = self.active(position.symbol)
        if active:
            if reason or any(o.side == "BUY" for o in active):
                for order in active:
                    self.cancel(order.id, now)
            return
        quantity = floor_step(position.quantity, market.rules.step_size)
        if reason:
            lower = market.bid * (1 - D(str(self.risk.max_slippage_pct)) / 100)
            price = floor_step(lower, market.rules.tick_size)
            if price < lower:
                price += market.rules.tick_size
            kind = "IOC"
        else:
            strategy = self.settings.strategies.get(position.strategy)
            profit_bps = (
                strategy.min_edge_bps + strategy.adverse_selection_bps if strategy else self.settings.take_profit_bps
            )
            target = position.average_cost * (1 + profit_bps / 10000)
            target /= 1 - self.settings.maker_fee_bps / 10000
            rounded = floor_step(target, market.rules.tick_size)
            if rounded < target:
                rounded += market.rules.tick_size
            price = max(market.ask, rounded)
            kind = "POST_ONLY"
        # Dust stays visible; do not create the same rejected exit every feature tick.
        if quantity < market.rules.min_quantity or quantity * price < market.rules.min_notional:
            if position.exit_reason != "DUST_BELOW_MIN_NOTIONAL":
                position.exit_reason = "DUST_BELOW_MIN_NOTIONAL"
                self.emit("EXIT_BLOCKED", now, symbol=position.symbol, reason=position.exit_reason)
            return
        if now < self.cooldowns.get(position.symbol, 0):
            return
        self.cooldowns[position.symbol] = now + self.settings.cooldown_ms * MS
        self.submit(
            position.symbol,
            "SELL",
            quantity,
            price,
            now,
            f"paper-{self.revision + 1:012d}",
            position.strategy,
            reason or "PASSIVE_TAKE_PROFIT",
            kind,
        )

    def state(self) -> dict:
        return {
            "version": 1,
            "cash": str(self.cash),
            "initial_cash": str(self.initial_cash),
            "realized": str(self.realized),
            "fees": str(self.fees),
            "revision": self.revision,
            "running": self.running,
            "halt_reason": self.halt_reason,
            "flattening": self.flattening,
            "data_paused": self.data_paused,
            "orders": {k: o.to_dict() for k, o in self.orders.items()},
            "positions": {k: p.to_dict() for k, p in self.positions.items()},
            "last_trade_ids": self.last_trade_ids.copy(),
            "marks": {s: str(v) for s, v in self.marks.items()},
            "day": self.day,
            "day_equity": str(self.day_equity),
            "peak_equity": str(self.peak_equity),
            "consecutive_losses": self.consecutive_losses,
            "order_times": self.order_times.copy(),
            "cooldowns": self.cooldowns.copy(),
            "settings": self.settings.model_dump(mode="json"),
        }

    def restore(self, data: dict) -> None:
        if data["version"] != 1:
            raise ValueError("Unsupported paper state version")
        for key in ("cash", "initial_cash", "realized", "fees", "day_equity", "peak_equity"):
            setattr(self, key, D(data[key]))
        for key in (
            "revision",
            "running",
            "halt_reason",
            "flattening",
            "last_trade_ids",
            "day",
            "consecutive_losses",
            "order_times",
            "cooldowns",
        ):
            setattr(self, key, data[key])
        self.data_paused = data.get("data_paused", False)
        self.orders = {k: PaperOrder.restore(v) for k, v in data["orders"].items()}
        self.positions = {k: PaperPosition.restore(v) for k, v in data["positions"].items()}
        self.marks = {k: D(v) for k, v in data["marks"].items()}
        self.settings = PaperSettings.model_validate(data["settings"])
        self.events.clear()

    def summary(self, now: int) -> dict:
        positions = []
        for position in self.positions.values():
            mark = self.marks.get(position.symbol, position.average_cost)
            positions.append(
                {
                    **position.to_dict(),
                    "average_cost": str(position.average_cost),
                    "mark_price": str(mark),
                    "unrealized_pnl": str(mark * position.quantity - position.cost),
                    "mark_stale": not self.fresh(self.markets.get(position.symbol), now),
                    "dust": self.is_dust(position),
                }
            )
        return {
            "mode": "paper",
            "running": self.running,
            "halt_reason": self.halt_reason,
            "flattening": self.flattening,
            "data_paused": self.data_paused,
            "cash": str(self.cash),
            "reserved_cash": str(self.reserved_cash),
            "free_cash": str(self.cash - self.reserved_cash),
            "equity": str(self.equity),
            "initial_cash": str(self.initial_cash),
            "realized_pnl": str(self.realized),
            "fees": str(self.fees),
            "positions": positions,
            "orders": [o.to_dict() for o in list(self.orders.values())[-100:]],
            "decisions": list(self.decisions.values()),
            "revision": self.revision,
            "strategies": {k: v.model_dump(mode="json") for k, v in self.settings.strategies.items()},
            "settings": self.settings.model_dump(mode="json"),
            "risk": self.risk.model_dump(mode="json"),
            "execution_model": "L2 estimate; exact-price FIFO; cancellations ahead give no credit",
            "profitability_verified": False,
        }
