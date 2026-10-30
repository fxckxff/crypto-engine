"""PostgreSQL интеграция для каталога инструментов, истории ордеров и метрик (PHASE 6)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import asyncpg
import asyncpg.pool

from app.core.enums import Side
from app.exchanges.base import Order, OrderFill

logger = logging.getLogger(__name__)


# Schema SQL
SCHEMA_SQL = """
-- Инструменты
CREATE TABLE IF NOT EXISTS instruments (
    symbol TEXT PRIMARY KEY,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    status TEXT NOT NULL,
    trading_allowed BOOLEAN NOT NULL,
    tick_size TEXT NOT NULL,
    step_size TEXT NOT NULL,
    min_quantity TEXT NOT NULL,
    min_notional TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- История ордеров
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    exchange_order_id TEXT NOT NULL,
    client_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    quantity TEXT NOT NULL,
    price TEXT,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    avg_price TEXT,
    commission TEXT,
    commission_asset TEXT,
    time_in_force TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_time ON orders(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_client_oid ON orders(client_order_id);

-- Исполнения (fills)
CREATE TABLE IF NOT EXISTS order_fills (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    exchange_trade_id TEXT NOT NULL,
    price TEXT NOT NULL,
    quantity TEXT NOT NULL,
    commission TEXT,
    commission_asset TEXT,
    is_maker BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON order_fills(order_id);

-- Позиции (snapshot для отчетов)
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    side TEXT,
    quantity TEXT NOT NULL DEFAULT '0',
    entry_price TEXT,
    entry_time TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Метрики производительности (агрегированные по дням)
CREATE TABLE IF NOT EXISTS daily_pnl (
    date DATE NOT NULL,
    symbol TEXT NOT NULL,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    unrealized_pnl TEXT NOT NULL DEFAULT '0',
    volume TEXT NOT NULL DEFAULT '0',
    trades_count INTEGER NOT NULL DEFAULT 0,
    fees TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY (date, symbol)
);
"""


@dataclass
class PostgresConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = "crypto_engine"
    user: str = "postgres"
    password: str = "postgres"
    min_size: int = 2
    max_size: int = 10

    @classmethod
    def from_env(cls) -> PostgresConfig:
        return cls(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            database=os.getenv("POSTGRES_DB", "crypto_engine"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", "postgres"),
        )


class PostgresCatalog:
    """Каталог инструментов и транзакционный лог ордеров."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def init_schema(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info("postgres schema initialized")

    # --- Instruments ---

    async def upsert_instrument(self, symbol: str, base: str, quote: str, status: str, trading: bool,
                                tick: str, step: str, min_qty: str, min_notional: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO instruments (symbol, base_asset, quote_asset, status, trading_allowed,
                    tick_size, step_size, min_quantity, min_notional)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (symbol) DO UPDATE SET
                    base_asset=EXCLUDED.base_asset, quote_asset=EXCLUDED.quote_asset,
                    status=EXCLUDED.status, trading_allowed=EXCLUDED.trading_allowed,
                    tick_size=EXCLUDED.tick_size, step_size=EXCLUDED.step_size,
                    min_quantity=EXCLUDED.min_quantity, min_notional=EXCLUDED.min_notional,
                    updated_at=NOW()
            """, symbol, base, quote, status, trading, tick, step, min_qty, min_notional)

    async def get_instruments(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM instruments WHERE trading_allowed")
            return [dict(r) for r in rows]

    # --- Orders ---

    async def record_order(self, order: Order) -> int:
        """Сохраняет ордер, возвращает внутренний ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO orders (exchange_order_id, client_order_id, symbol, side, type, status,
                    quantity, price, filled_quantity, avg_price, commission, commission_asset, time_in_force)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (exchange_order_id) DO UPDATE SET
                    status=EXCLUDED.status, filled_quantity=EXCLUDED.filled_quantity,
                    avg_price=EXCLUDED.avg_price, commission=EXCLUDED.commission,
                    commission_asset=EXCLUDED.commission_asset, updated_at=NOW()
                RETURNING id
            """, order.exchange_order_id, order.client_order_id, order.symbol,
                order.side.value, order.order_type.value, order.status.value,
                str(order.quantity), str(order.price) if order.price else None,
                str(order.filled_quantity), str(order.average_price) if order.average_price else None,
                str(order.fee_paid), "USDT", "GTC")
            return row["id"]

    async def record_fills(self, order_id: int, fills: list[OrderFill]) -> None:
        if not fills:
            return
        async with self.pool.acquire() as conn:
            await conn.executemany("""
                INSERT INTO order_fills (order_id, exchange_trade_id, price, quantity, commission,
                    commission_asset, is_maker)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (order_id, exchange_trade_id) DO NOTHING
            """, [(order_id, str(f.trade_id), str(f.price), str(f.quantity),
                   str(f.commission), f.commission_asset, False) for f in fills])

    async def get_order_history(self, symbol: str, limit: int = 100) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM orders WHERE symbol=$1 ORDER BY created_at DESC LIMIT $2
            """, symbol, limit)
            return [dict(r) for r in rows]

    # --- Positions ---

    async def upsert_position(self, symbol: str, side: Side | None, qty: Decimal,
                              entry_price: Decimal | None) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO positions (symbol, side, quantity, entry_price)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (symbol) DO UPDATE SET
                    side=EXCLUDED.side, quantity=EXCLUDED.quantity,
                    entry_price=EXCLUDED.entry_price, updated_at=NOW()
            """, symbol, side.value if side else None, str(qty), str(entry_price) if entry_price else None)


class PostgresPool:
    """Управление пулом соединений."""

    def __init__(self, config: PostgresConfig) -> None:
        self.config = config
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> asyncpg.Pool:
        self.pool = await asyncpg.create_pool(
            host=self.config.host,
            port=self.config.port,
            database=self.config.database,
            user=self.config.user,
            password=self.config.password,
            min_size=self.config.min_size,
            max_size=self.config.max_size,
            command_timeout=30,
        )
        logger.info(
            "postgres pool created",
            extra={"extra_fields": {"host": self.config.host, "db": self.config.database}},
        )
        return self.pool

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None