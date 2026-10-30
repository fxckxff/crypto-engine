"""Подпись приватных запросов Binance (HMAC SHA256) и ограничение темпа.

Ключи берутся только из окружения, в конфиг и логи не попадают (ТЗ §25).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from app.core.errors import CredentialsMissing


def sign_query(query: str, secret: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def build_query(params: Mapping[str, Any]) -> str:
    return urlencode([(k, v) for k, v in params.items() if v is not None], doseq=True)


def signed_params(
    params: Mapping[str, Any],
    secret: str,
    recv_window_ms: int,
    timestamp_ms: int | None = None,
) -> str:
    """Собрать query string с timestamp/recvWindow и подписью."""
    payload: dict[str, Any] = {k: v for k, v in params.items() if v is not None}
    payload["recvWindow"] = recv_window_ms
    payload["timestamp"] = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    query = build_query(payload)
    return f"{query}&signature={sign_query(query, secret)}"


class Credentials:
    """Пара ключей. Приватные вызовы без ключей запрещены."""

    __slots__ = ("_api_key", "_api_secret")

    def __init__(self, api_key: str | None, api_secret: str | None) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    @classmethod
    def from_env(cls) -> Credentials:
        return cls(os.environ.get("BINANCE_API_KEY"), os.environ.get("BINANCE_API_SECRET"))

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._api_secret)

    def require(self) -> tuple[str, str]:
        if not self.available:
            raise CredentialsMissing(
                "BINANCE_API_KEY / BINANCE_API_SECRET не заданы: приватные методы недоступны"
            )
        assert self._api_key is not None and self._api_secret is not None
        return self._api_key, self._api_secret

    def __repr__(self) -> str:  # pragma: no cover
        return f"Credentials(available={self.available}, api_key='***')"
