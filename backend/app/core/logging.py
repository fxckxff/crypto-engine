"""Structured JSON logging.

Требования ТЗ §20 и §25: логи структурированные, JSON, без секретов.
Ключи API и подписи никогда не попадают в лог — маскирование на уровне фильтра.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_SECRET_KEYS = ("api_key", "api_secret", "signature", "listenkey", "x-mbx-apikey", "authorization")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(_mask(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _mask(data: dict[str, Any]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _SECRET_KEYS and value:
            masked[key] = "***"
        else:
            masked[key] = value
    return masked


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    log_path = os.environ.get("CLE_LOG_FILE")
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler = (
        RotatingFileHandler(log_path, maxBytes=20_000_000, backupCount=3, encoding="utf-8")
        if log_path
        else logging.StreamHandler(sys.stdout)
    )
    plain = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(JsonFormatter() if json_logs else plain)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    if log_path:
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            log = logging.getLogger(name)
            log.handlers.clear()
            log.propagate = True
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
