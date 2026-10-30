"""Оркестратор рыночных данных: адаптер → стакан → признаки → сканер → запись.

Ни одна стратегия не работает напрямую с адаптером (ТЗ §21): сервис отдаёт готовые
снимки признаков.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.analytics.engine import FeatureEngine, FeatureSnapshot
from app.analytics.flow import Trade
from app.analytics.walls import Transition, Wall, WallConfig, WallDetector
from app.config.schema import Config
from app.core.enums import Side
from app.core.time import NS_MS, Clock
from app.exchanges.base import ExchangeAdapter, Instrument, TradingRules
from app.market_data.synchronizer import BookSynchronizer
from app.paper.engine import PaperEngine
from app.paper.models import Market
from app.paper.runtime import PaperRuntime
from app.recorder.parquet_recorder import (
    ParquetRecorder,
    feature_schema,
    raw_event_schema,
    trade_row,
)
from app.scanner.scanner import ScannerRow, SymbolScanner

if TYPE_CHECKING:
    from app.trading.live import LiveTradingManager

logger = logging.getLogger(__name__)

D = Decimal


@dataclass(slots=True)
class SymbolState:
    symbol: str
    instrument: Instrument
    rules: TradingRules
    engine: FeatureEngine
    walls: WallDetector
    synchronizer: BookSynchronizer
    snapshot: FeatureSnapshot | None = None
    scanner_row: ScannerRow | None = None
    transitions: list[Transition] = field(default_factory=list)
    trades_seen: int = 0
    last_feature_ns: int = 0
    last_latency_ms: float | None = None
    last_resyncs: int = 0


class MarketService:
    def __init__(
        self,
        config: Config,
        adapter: ExchangeAdapter,
        clock: Clock | None = None,
        wall_config: WallConfig | None = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.clock = clock or Clock()
        self.states: dict[str, SymbolState] = {}
        self.scanner: SymbolScanner | None = None
        self.universe_rows: list[dict] = []
        self.universe_updated_ns = 0
        self.universe_error: str | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        self.live_manager: LiveTradingManager | None = None
        self.paper: PaperRuntime | None = None
        self._started_ns = 0
        self._paper_recovery_since_ns: int | None = None
        self._feature_hz = 1000.0 / max(config.market_data.feature_interval_ms, 20)

        self.raw_recorder: ParquetRecorder | None = None
        self.feature_recorder: ParquetRecorder | None = None
        if config.market_data.record_raw_events:
            self.raw_recorder = ParquetRecorder(
                dataset="raw_events",
                directory=config.storage.raw_dir,
                schema=raw_event_schema(),
                rotation_mb=config.storage.rotation_mb,
                queue_max=config.storage.queue_max_events,
            )
        if config.market_data.record_feature_snapshots:
            self.feature_recorder = ParquetRecorder(
                dataset="feature_snapshots",
                directory=config.storage.derived_dir,
                schema=feature_schema(),
                rotation_mb=config.storage.rotation_mb,
                queue_max=config.storage.queue_max_events,
            )

        self._wall_config = wall_config or WallConfig()
        self._trade_buffer: dict[str, deque[tuple[int, Decimal, Decimal]]] = defaultdict(deque)
        self._subscribers: dict[asyncio.Queue[dict[str, Any]], str | None] = {}

    # ------------------------------------------------------------------ запуск

    async def start(self) -> None:
        if self._running:
            return
        self._started_ns = self.clock.wall_ns()
        await self.adapter.connect()
        instruments = await self.adapter.get_instruments()
        rules: dict[str, TradingRules] = {}
        for symbol in self.config.exchange.symbols:
            instrument = instruments[symbol]
            if not instrument.trading_allowed:
                logger.warning(
                    "instrument is not trading",
                    extra={"extra_fields": {"symbol": symbol, "status": instrument.status}},
                )
            trading_rules = await self.adapter.get_trading_rules(symbol)
            rules[symbol] = trading_rules
            self.states[symbol] = SymbolState(
                symbol=symbol,
                instrument=instrument,
                rules=trading_rules,
                engine=FeatureEngine(
                    symbol=symbol,
                    tick_size=trading_rules.tick_size,
                    spread_window_ms=float(self.config.market_data.feature_window_ms),
                    flow_window_ms=float(self.config.market_data.feature_window_ms),
                ),
                walls=WallDetector(symbol, self._wall_config),
                synchronizer=BookSynchronizer(
                    adapter=self.adapter,
                    symbol=symbol,
                    depth=self.config.market_data.orderbook_depth,
                    clock=self.clock,
                    stale_after_ms=self.config.market_data.stale_after_ms,
                    trade_hint=self._make_trade_hint(symbol),
                    recorder=self.raw_recorder,
                ),
            )

        self.scanner = SymbolScanner(
            config=self.config.scanner,
            instruments=instruments,
            rules=rules,
            max_position_usdt=self.config.risk.max_position_usdt,
        )

        if self.config.app.mode == "paper":
            unsupported = [s.symbol for s in self.states.values() if s.instrument.quote_asset != "USDT"]
            if unsupported:
                raise ValueError(f"Paper account supports USDT quote only: {unsupported}")
            self.paper = PaperRuntime(
                PaperEngine(self.config.paper, self.config.risk, self.config.market_data.max_data_age_ms)
            )
            await self.paper.open(self.clock.wall_ns())

        if self.raw_recorder is not None:
            await self.raw_recorder.start()
        if self.feature_recorder is not None:
            await self.feature_recorder.start()

        for symbol, state in self.states.items():
            self._tasks.append(asyncio.create_task(self._run_symbol(state), name=f"book-{symbol}"))
            self._tasks.append(asyncio.create_task(self._pump_trades(state), name=f"trades-{symbol}"))
        self._tasks.append(asyncio.create_task(self._feature_loop(), name="feature-loop"))
        self._tasks.append(asyncio.create_task(self._refresh_volumes_loop(), name="volume-loop"))
        if self.config.scanner.universe_enabled:
            self._tasks.append(asyncio.create_task(self._universe_loop(), name="universe-loop"))
        self._running = True
        logger.info(
            "market service started",
            extra={
                "extra_fields": {
                    "symbols": list(self.states),
                    "depth": self.config.market_data.orderbook_depth,
                    "record_raw": self.config.market_data.record_raw_events,
                }
            },
        )

    async def stop(self) -> None:
        self._running = False
        # PHASE 7: Stop live trading manager first
        if self.live_manager is not None:
            await self.live_manager.stop()
            self.live_manager = None
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        for state in self.states.values():
            await state.synchronizer.stop()
        if self.raw_recorder is not None:
            await self.raw_recorder.close()
        if self.feature_recorder is not None:
            await self.feature_recorder.close()
        if self.paper is not None:
            await self.paper.close(self.clock.wall_ns())
        await self.adapter.close()

    # ------------------------------------------------------------------ задачи

    async def _run_symbol(self, state: SymbolState) -> None:
        while True:
            try:
                await state.synchronizer.run()
                state.synchronizer.book.invalidate("depth stream ended")
            except asyncio.CancelledError:
                raise
            except Exception:
                state.synchronizer.book.invalidate("synchronization failed")
                logger.exception("book synchronizer failed", extra={"extra_fields": {"symbol": state.symbol}})
            finally:
                await state.synchronizer.stop()
            await asyncio.sleep(self.config.market_data.reconnect_initial_ms / 1000)

    def _make_trade_hint(self, symbol: str):
        buffer = self._trade_buffer[symbol]

        def hint(since_ns: int, until_ns: int) -> dict[Decimal, Decimal]:
            by_price: dict[Decimal, Decimal] = defaultdict(Decimal)
            while buffer and buffer[0][0] < since_ns - 5_000_000_000:
                buffer.popleft()
            for ts, price, qty in buffer:
                if since_ns <= ts <= until_ns:
                    by_price[price] += qty
            return dict(by_price)

        return hint

    async def _pump_trades(self, state: SymbolState) -> None:
        try:
            async for tick in self.adapter.stream_trades(state.symbol):
                ingest_ns = self.clock.wall_ns()
                trade = Trade(
                    symbol=tick.symbol,
                    price=tick.price,
                    quantity=tick.quantity,
                    aggressor=tick.aggressor,
                    exchange_ts_ms=tick.exchange_ts_ms,
                    receive_ns=tick.receive_ns,
                    receive_mono_ns=tick.meta.receive_mono_ns,
                    trade_id=tick.trade_id,
                )
                state.engine.on_trade(trade)
                state.trades_seen += 1
                # Wall observations use the actual public aggressor stream.
                state.transitions.extend(state.walls._apply_trades([trade], tick.meta.receive_mono_ns))
                del state.transitions[:-50]
                if self.paper is not None and not self.paper.error:
                    try:
                        status = self.status()
                        if (status["data_loss"] or not status["connection"]["connected"]) and (
                            self.paper.engine.running or self.paper.engine.active() or self.paper.engine.flattening
                        ):
                            await self.paper.apply("pause_data", ingest_ns, "Ошибка рыночных данных")
                        await self.paper.apply(
                            "trade",
                            tick.symbol,
                            tick.trade_id,
                            tick.price,
                            tick.quantity,
                            tick.aggressor.value,
                            ingest_ns,
                            tick.exchange_ts_ms * NS_MS if tick.exchange_ts_ms else None,
                        )
                    except RuntimeError:
                        logger.exception("paper trade processing stopped")
                buffer = self._trade_buffer[state.symbol]
                buffer.append((tick.meta.receive_mono_ns, tick.price, tick.quantity))
                while buffer and buffer[0][0] < tick.meta.receive_mono_ns - 5_000_000_000:
                    buffer.popleft()
                if self.raw_recorder is not None:
                    self.raw_recorder.record(trade_row(tick, ingest_ns))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("trade pump crashed", extra={"extra_fields": {"symbol": state.symbol}})

    async def _universe_loop(self) -> None:
        from app.scanner.universe import fetch_universe

        while True:
            try:
                self.universe_rows = await fetch_universe(self.adapter.rest)
                self.universe_updated_ns = self.clock.wall_ns()
                self.universe_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.universe_error = f"{type(exc).__name__}: {exc}"
                logger.warning("universe refresh failed: %s", self.universe_error)
            await asyncio.sleep(60)

    async def _refresh_volumes_loop(self, interval_s: float = 300.0) -> None:
        """24h оборот для фильтра сканера. Обновляется редко: значение меняется медленно."""
        try:
            while True:
                await self._refresh_volumes()
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("volume refresh loop crashed")

    async def _refresh_volumes(self) -> None:
        volumes: dict[str, Decimal] = {}
        try:
            volumes = await self.adapter.get_24h_volumes(list(self.states))
        except Exception as exc:
            logger.warning(
                "24h volume unavailable",
                extra={"extra_fields": {"error": f"{type(exc).__name__}: {exc}"}},
            )
        if self.scanner is not None and volumes:
            self.scanner.volume_24h_usdt.update(volumes)

    async def _feature_loop(self) -> None:
        interval = 1.0 / self._feature_hz
        try:
            while True:
                await asyncio.sleep(interval)
                for state in self.states.values():
                    now = self.clock.monotonic_ns()
                    if state.last_resyncs != state.synchronizer.resyncs:
                        state.last_resyncs = state.synchronizer.resyncs
                        state.walls = WallDetector(state.symbol, self._wall_config)
                        state.engine = FeatureEngine(
                            symbol=state.symbol,
                            tick_size=state.rules.tick_size,
                            spread_window_ms=float(self.config.market_data.feature_window_ms),
                            flow_window_ms=float(self.config.market_data.feature_window_ms),
                        )
                        self._paper_recovery_since_ns = None
                        if (
                            self.paper is not None
                            and self.paper.engine.running
                            and not self.paper.error
                            and not self.paper.engine.settings.isolate_stale_symbols
                        ):
                            await self.paper.apply(
                                "pause_data", self.clock.wall_ns(), "Пересинхронизация стакана; ожидание восстановления"
                            )
                    snapshot = state.engine.update(state.synchronizer.book, now)
                    state.snapshot = snapshot
                    state.last_feature_ns = snapshot.now_ns
                    if snapshot.tradable:
                        transitions = state.walls.update(state.synchronizer.book, now)
                        if transitions:
                            state.transitions.extend(transitions[-50:])
                            del state.transitions[:-50]
                    else:
                        state.walls.invalidate_observation(now)
                    if self.scanner is not None:
                        state.scanner_row = self.scanner.evaluate(
                            snapshot, state.synchronizer.book, state.walls.active_walls()
                        )
                    if self.feature_recorder is not None and snapshot.tradable:
                        self.feature_recorder.record(_feature_row(snapshot, state))
                    self._publish(state)
                    if self.paper is not None and not self.paper.error:
                        try:
                            status = self.status()
                            if (status["data_loss"] or not status["connection"]["connected"]) and (
                                self.paper.engine.running or self.paper.engine.active() or self.paper.engine.flattening
                            ):
                                await self.paper.apply("pause_data", self.clock.wall_ns(), "Ошибка рыночных данных")
                            await self.paper.apply("market", self._paper_market(state), self.clock.wall_ns())
                        except RuntimeError:
                            logger.exception("paper execution stopped")
                await self._recover_paper()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("feature loop crashed")

    async def _recover_paper(self) -> None:
        paper = self.paper
        if not paper or paper.error or not (paper.engine.running or paper.engine.data_paused):
            self._paper_recovery_since_ns = None
            return
        status = self.status()
        now = self.clock.wall_ns()
        engine = paper.engine
        fresh = [engine.fresh(m, now) for m in engine.markets.values()]
        ready = any(fresh) if engine.settings.isolate_stale_symbols else bool(fresh) and all(fresh)
        allowed = {"READY", "STALE"} if engine.settings.isolate_stale_symbols else {"READY"}
        healthy = (
            status["running"]
            and status["connection"]["connected"]
            and not status["data_loss"]
            and bool(status["books"])
            and (
                any(b["state"] in allowed for b in status["books"].values())
                if engine.settings.isolate_stale_symbols
                else all(b["state"] in allowed for b in status["books"].values())
            )
        )
        if not healthy:
            self._paper_recovery_since_ns = None
            if engine.running:
                await paper.apply("pause_data", now, "Ожидание свежих синхронизированных данных")
            return
        if engine.running:
            self._paper_recovery_since_ns = None
            return
        mono = self.clock.monotonic_ns()
        if self._paper_recovery_since_ns is None:
            self._paper_recovery_since_ns = mono
        elif (
            mono - self._paper_recovery_since_ns >= 10_000_000_000
            and ready
            and (
                engine.settings.isolate_stale_symbols
                or all(engine.fresh(engine.markets.get(s), now) for s in engine.positions)
            )
        ):
            try:
                await paper.apply("resume_data", current_time=True)
            except ValueError:
                # Freshness is checked again under the command lock.
                return
            self._paper_recovery_since_ns = None

    def _paper_market(self, state: SymbolState) -> Market:
        book, snapshot = state.synchronizer.book, state.snapshot
        age_ns = max(0, self.clock.monotonic_ns() - book.last_update_ns)
        walls = [w for w in state.walls.active_walls() if w.side == Side.BID and w.qualified]
        wall = max(walls, key=lambda w: w.score) if walls else None
        return Market(
            symbol=state.symbol,
            ts_ns=self.clock.wall_ns() - age_ns,
            sequence=book.last_update_id or 0,
            ready=bool(book.ready and snapshot and snapshot.tradable and state.instrument.trading_allowed),
            bids=tuple((lv.price, lv.quantity) for lv in book.side_levels(Side.BID, 50)),
            asks=tuple((lv.price, lv.quantity) for lv in book.side_levels(Side.ASK, 50)),
            rules=state.rules,
            obi=(snapshot.obi.get(5) or 0) if snapshot else 0,
            ofi=(snapshot.ofi.rolling or 0) if snapshot and snapshot.ofi else 0,
            microprice_bps=snapshot.microprice.deviation_bps if snapshot and snapshot.microprice else 0,
            buy_volume=snapshot.flow.buy_volume if snapshot else D(0),
            sell_volume=snapshot.flow.sell_volume if snapshot else D(0),
            trades=snapshot.flow.trade_count if snapshot else 0,
            wall_score=wall.score if wall else 0,
            wall_absorbed=wall.absorbed_quantity if wall else D(0),
            wall_price=wall.price if wall else None,
            wall_persistent=bool(wall and self.clock.monotonic_ns() - wall.detected_ns >= 5_000_000_000),
        )

    # ------------------------------------------------------------------ подписки

    def subscribe(self, symbol: str | None = None) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4)
        self._subscribers[queue] = symbol.upper() if symbol else None
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._subscribers:
            del self._subscribers[queue]

    def _publish(self, state: SymbolState) -> None:
        if not self._subscribers:
            return
        payload = self.symbol_payload(state.symbol, levels=20)
        if payload is None:
            return
        for queue, symbol in list(self._subscribers.items()):
            if symbol is not None and state.symbol != symbol:
                continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # UI не должен тормозить торговый движок: старое значение выбрасываем.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # ------------------------------------------------------------------ чтение состояния

    def status(self) -> dict[str, Any]:
        now = self.clock.wall_ns()
        for state in self.states.values():
            state.synchronizer.book.refresh_state()
        connection = getattr(self.adapter, "connection_stats", None)
        return {
            "mode": self.config.app.mode,
            "paper": {
                "enabled": self.paper is not None,
                "running": bool(self.paper and self.paper.engine.running),
                "storage_error": self.paper.error if self.paper else None,
            },
            "exchange": self.config.exchange.provider,
            "market": self.config.exchange.market,
            "uptime_s": round((now - self._started_ns) / 1e9, 1) if self._started_ns else 0.0,
            "running": self._running and all(not task.done() for task in self._tasks),
            "symbols": list(self.states),
            "connection": {
                "connected": bool(getattr(connection, "connected", False)),
                "connects": getattr(connection, "connects", 0),
                "disconnects": getattr(connection, "disconnects", 0),
                "last_error": getattr(connection, "last_error", None),
            },
            "recorder": {
                "raw": _recorder_stats(self.raw_recorder),
                "features": _recorder_stats(self.feature_recorder),
            },
            "data_loss": bool(getattr(self.adapter, "market_data_loss", False))
            or any(state.synchronizer.dropped_diffs > 0 for state in self.states.values())
            or any(r is not None and r.stats.data_loss for r in (self.raw_recorder, self.feature_recorder)),
            "books": {
                symbol: {
                    "state": state.synchronizer.book.state.value,
                    "levels": state.synchronizer.book.levels_loaded,
                    "age_ms": round(state.synchronizer.book.age_ms(), 1),
                    "gaps": state.synchronizer.gaps,
                    "resyncs": state.synchronizer.resyncs,
                    "dropped_diffs": state.synchronizer.dropped_diffs,
                    "trades": state.trades_seen,
                }
                for symbol, state in self.states.items()
            },
        }

    def symbol_payload(self, symbol: str, levels: int = 20) -> dict[str, Any] | None:
        state = self.states.get(symbol.upper())
        if state is None:
            return None
        book = state.synchronizer.book
        snapshot = state.snapshot
        bids: list[list[str]] = []
        asks: list[list[str]] = []
        from app.core.enums import Side

        for level in book.side_levels(Side.BID, levels):
            bids.append([str(level.price), str(level.quantity), str(level.notional_usdt)])
        for level in book.side_levels(Side.ASK, levels):
            asks.append([str(level.price), str(level.quantity), str(level.notional_usdt)])

        return {
            "symbol": book.symbol,
            "ts_ns": snapshot.now_ns if snapshot else 0,
            "server_ts_ms": self.clock.wall_ns() // NS_MS,
            "book_state": book.state.value,
            "book_age_ms": round(book.age_ms(), 1),
            "tradable": bool(snapshot and snapshot.tradable),
            "block_reason": snapshot.block_reason if snapshot else None,
            "bids": bids,
            "asks": asks,
            "spread": {
                "value": snapshot.spread.spread if snapshot and snapshot.spread else None,
                "pct": snapshot.spread.spread_pct if snapshot and snapshot.spread else None,
                "bps": snapshot.spread.spread_bps if snapshot and snapshot.spread else None,
                "ticks": snapshot.spread.spread_ticks if snapshot and snapshot.spread else None,
                "percentile": snapshot.spread.percentile if snapshot and snapshot.spread else None,
                "median": snapshot.spread.rolling_median if snapshot and snapshot.spread else None,
            },
            "obi": {str(k): v for k, v in (snapshot.obi.items() if snapshot else [])},
            "obi_weighted": snapshot.obi_weighted if snapshot else None,
            "microprice": {
                "value": snapshot.microprice.value if snapshot and snapshot.microprice else None,
                "dev_bps": snapshot.microprice.deviation_bps if snapshot and snapshot.microprice else None,
                "velocity_bps_s": snapshot.microprice.velocity_bps_s if snapshot and snapshot.microprice else None,
            },
            "ofi": {
                "top": snapshot.ofi.top_of_book if snapshot and snapshot.ofi else None,
                "multi": snapshot.ofi.multi_level if snapshot and snapshot.ofi else None,
                "rolling": snapshot.ofi.rolling if snapshot and snapshot.ofi else None,
            },
            "flow": {
                "buy_volume": str(snapshot.flow.buy_volume) if snapshot else "0",
                "sell_volume": str(snapshot.flow.sell_volume) if snapshot else "0",
                "cvd": str(snapshot.flow.cvd) if snapshot else "0",
                "trades": snapshot.flow.trade_count if snapshot else 0,
                "classified_share": snapshot.flow.classified_share if snapshot else 0.0,
            },
            "walls": [_wall_payload(w) for w in _sorted_walls(state.walls.active_walls())],
            "latency_ms": _latency(state),
            "scanner": _scanner_payload(state.scanner_row),
        }

    def active_walls(self, symbol: str | None = None) -> list[Wall]:
        if symbol and symbol.upper() in self.states:
            states = [self.states[symbol.upper()]]
        else:
            states = list(self.states.values())
        walls: list[Wall] = []
        for state in states:
            walls.extend(state.walls.active_walls())
        return _sorted_walls(walls)

    def scanner_rows(self) -> list[dict[str, Any]]:
        rows = [_scanner_payload(s.scanner_row) for s in self.states.values() if s.scanner_row is not None]
        return sorted([r for r in rows if r is not None], key=lambda r: r["opportunity_score"], reverse=True)


def _sorted_walls(walls: list[Wall]) -> list[Wall]:
    return sorted(walls, key=lambda w: w.score, reverse=True)


def _wall_payload(wall: Wall) -> dict[str, Any]:
    return {
        "side": wall.side.value,
        "price": str(wall.price),
        "state": wall.state.value,
        "score": wall.score,
        "notional": str(wall.current_notional),
        "initial_quantity": str(wall.initial_quantity),
        "current_quantity": str(wall.current_quantity),
        "qualified": wall.qualified,
        "quality_reasons": wall.quality_reasons,
        "score_components": wall.score_components,
        "size_ratio": round(wall.size_ratio, 2),
        "cancelled_estimate": str(wall.cancelled_quantity_estimate),
        "replenished_estimate": str(wall.replenished_quantity_estimate),
        "absorbed": str(wall.absorbed_quantity),
        "age_ms": round((time.monotonic_ns() - wall.detected_ns) / 1e6, 1),
        "distance_pct": round(wall.distance_pct, 4),
    }


def _scanner_payload(row: ScannerRow | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "symbol": row.symbol,
        "eligible": row.eligible,
        "exclude_reason": row.exclude_reason,
        "spread_pct": row.spread_pct,
        "spread_ticks": row.spread_ticks,
        "bid_depth_usdt": str(row.bid_depth_usdt),
        "ask_depth_usdt": str(row.ask_depth_usdt),
        "depth_imbalance": row.depth_imbalance,
        "book_state": row.book_state.value,
        "opportunity_score": row.opportunity_score,
        "components": asdict(row.components) if row.components else None,
        "volume_24h_usdt": str(row.volume_24h_usdt) if row.volume_24h_usdt is not None else None,
        "wall_count": row.wall_count,
        "top_wall_score": row.top_wall_score,
        "notes": row.notes,
        "quote_age_ms": row.quote_age_ms,
    }


def _latency(state: SymbolState) -> dict[str, float | None]:
    book = state.synchronizer.book
    return {
        "book_age_ms": round(book.age_ms(), 1),
        "last_exchange_ts_ms": book.last_exchange_ts_ms,
    }


def _feature_row(snapshot: FeatureSnapshot, state: SymbolState) -> dict:
    spread = snapshot.spread
    micro = snapshot.microprice
    ofi = snapshot.ofi
    flow = snapshot.flow
    walls = state.walls.active_walls()
    top_score = max((w.score for w in walls), default=0.0)
    return {
        "schema_version": "1",
        "symbol": snapshot.symbol,
        "ts_ns": snapshot.now_ns,
        "book_state": snapshot.book_state.value,
        "book_age_ms": snapshot.book_age_ms,
        "tradable": snapshot.tradable,
        "block_reason": snapshot.block_reason,
        "best_bid": str(snapshot.best_bid) if snapshot.best_bid is not None else None,
        "best_ask": str(snapshot.best_ask) if snapshot.best_ask is not None else None,
        "mid": str(snapshot.mid) if snapshot.mid is not None else None,
        "spread": spread.spread if spread else None,
        "spread_pct": spread.spread_pct if spread else None,
        "spread_bps": spread.spread_bps if spread else None,
        "spread_ticks": spread.spread_ticks if spread else None,
        "spread_percentile": spread.percentile if spread else None,
        "spread_median": spread.rolling_median if spread else None,
        "obi_1": snapshot.obi.get(1),
        "obi_5": snapshot.obi.get(5),
        "obi_10": snapshot.obi.get(10),
        "obi_20": snapshot.obi.get(20),
        "obi_50": snapshot.obi.get(50),
        "obi_weighted": snapshot.obi_weighted,
        "microprice": micro.value if micro else None,
        "microprice_dev_bps": micro.deviation_bps if micro else None,
        "ofi_top": ofi.top_of_book if ofi else None,
        "ofi_multi": ofi.multi_level if ofi else None,
        "ofi_rolling": ofi.rolling if ofi else None,
        "buy_volume": str(flow.buy_volume),
        "sell_volume": str(flow.sell_volume),
        "cvd": str(flow.cvd),
        "trade_count": flow.trade_count,
        "wall_count": len(walls),
        "top_wall_score": top_score,
    }


def _recorder_stats(recorder: ParquetRecorder | None) -> dict[str, Any] | None:
    if recorder is None:
        return None
    stats = recorder.stats
    return {
        "recorded": stats.recorded,
        "dropped": stats.dropped,
        "files": stats.files,
        "bytes": stats.bytes_written,
        "queue": recorder.queue_size,
        "last_error": stats.last_error,
    }
