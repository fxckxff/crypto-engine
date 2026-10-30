"""Подпись приватных запросов и локальный лимит."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from app.core.errors import CredentialsMissing
from app.exchanges.binance.ratelimit import RateLimiter
from app.exchanges.binance.sign import Credentials, build_query, sign_query, signed_params


def test_signature_matches_manual_hmac() -> None:
    query = "symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=0.001&price=20000"
    secret = "topsecret"
    expected = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert sign_query(query, secret) == expected


def test_signed_params_has_timestamp_and_signature() -> None:
    secret = "s3cr3t"
    query = signed_params({"symbol": "BTCUSDT"}, secret, recv_window_ms=5000, timestamp_ms=1700000000000)
    assert "timestamp=1700000000000" in query
    assert "recvWindow=5000" in query
    assert "signature=" in query

    body, signature = query.rsplit("&signature=", 1)
    assert sign_query(body, secret) == signature


def test_query_keeps_order_and_skips_none() -> None:
    assert build_query({"a": 1, "b": None, "c": "x"}) == "a=1&c=x"


def test_credentials_missing_raises() -> None:
    credentials = Credentials(None, None)
    assert credentials.available is False
    with pytest.raises(CredentialsMissing):
        credentials.require()
    assert "***" in repr(Credentials("key", "secret"))


async def test_rate_limiter_refills() -> None:
    limiter = RateLimiter(tokens_per_minute=6000, burst=2)  # 100/сек
    await limiter.acquire(2)
    assert limiter.available < 1
    await limiter.acquire(1)  # должен подождать ~0.01 с, а не упасть
    assert limiter.waited_seconds > 0
