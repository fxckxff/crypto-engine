"""Загрузка конфигурации из YAML с валидацией."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.config.schema import Config


class ConfigError(Exception):
    """Конфигурация невалидна. Сообщение содержит все обнаруженные проблемы."""


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")
    return build_config(raw)


def build_config(raw: dict[str, Any]) -> Config:
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "<root>"
            problems.append(f"{loc}: {err['msg']}")
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems)) from exc
