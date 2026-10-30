"""Валидация конфигурации: никаких молчаливых подстановок."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config.loader import ConfigError, build_config, load_config

CONFIG = Path(__file__).resolve().parents[3] / "configs" / "default.yaml"


def test_default_config_is_valid() -> None:
    config = load_config(CONFIG)
    assert config.exchange.symbols == ["BTCUSDT", "ETHUSDT"]
    assert config.market_data.orderbook_depth == 200
    assert config.app.mode == "research"


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        build_config({"app": {"mode": "research", "unknown": 1}})
    assert "unknown" in str(exc.value)


def test_bad_mode_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        build_config({"app": {"mode": "turbo"}})
    assert "app.mode" in str(exc.value)


def test_live_mode_is_blocked_until_phase_7() -> None:
    with pytest.raises(ConfigError) as exc:
        build_config({"app": {"mode": "live"}})
    assert "live" in str(exc.value)


def test_spread_bounds_are_checked() -> None:
    with pytest.raises(ConfigError):
        build_config({"scanner": {"min_spread_pct": 5.0, "max_spread_pct": 1.0}})


def test_position_limit_cannot_exceed_total_exposure() -> None:
    with pytest.raises(ConfigError):
        build_config({"risk": {"max_position_usdt": 500, "max_total_exposure_usdt": 100}})


def test_postgres_requires_url() -> None:
    with pytest.raises(ConfigError):
        build_config({"storage": {"database": "postgresql"}})


def test_features_require_raw_recording() -> None:
    with pytest.raises(ConfigError):
        build_config({"market_data": {"record_raw_events": False, "record_feature_snapshots": True}})


def test_symbols_are_normalised_and_deduplicated() -> None:
    config = build_config({"exchange": {"symbols": [" btcusdt ", "ethusdt"]}})
    assert config.exchange.symbols == ["BTCUSDT", "ETHUSDT"]
    with pytest.raises(ConfigError):
        build_config({"exchange": {"symbols": ["BTCUSDT", "btcusdt"]}})
