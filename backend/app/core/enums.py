"""Базовые перечисления домена."""

from __future__ import annotations

from enum import StrEnum


class MarketType(StrEnum):
    """Тип рынка. В MVP торгуем только SPOT (ТЗ §3.2)."""

    SPOT = "SPOT"
    LINEAR_PERPETUAL = "LINEAR_PERPETUAL"
    INVERSE_PERPETUAL = "INVERSE_PERPETUAL"


class Side(StrEnum):
    BID = "BID"
    ASK = "ASK"


class Aggressor(StrEnum):
    """Сторона агрессора в публичной сделке."""

    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


class BookIntegrity(StrEnum):
    """Состояние целостности локального стакана (ТЗ §4.2)."""

    EMPTY = "EMPTY"
    SYNCING = "SYNCING"
    READY = "READY"
    STALE = "STALE"
    INVALID = "INVALID"


class Confidence(StrEnum):
    """Достоверность значения.

    VERIFIED — значение прямо дано биржей.
    ESTIMATED — выведено эвристикой из агрегированных данных L2 (ТЗ §4.3).
    CONFIGURED — взято из конфигурации, не из API биржи.
    """

    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"
    CONFIGURED = "CONFIGURED"


class OrderStatus(StrEnum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    LIMIT_MAKER = "LIMIT_MAKER"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTX = "GTX"


class WallState(StrEnum):
    """Жизненный цикл лимитной стены (ТЗ §6.2)."""

    DETECTED = "DETECTED"
    OBSERVING = "OBSERVING"
    PERSISTENT = "PERSISTENT"
    TESTED = "TESTED"
    ABSORBING = "ABSORBING"
    WEAKENING = "WEAKENING"
    REMOVED = "REMOVED"
    BROKEN = "BROKEN"
    EXPIRED = "EXPIRED"
