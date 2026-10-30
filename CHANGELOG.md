# Changelog

Формат по [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии по
[SemVer](https://semver.org/lang/ru/).

## [Unreleased]

## [0.2.1] — 2026-10-30

### Fixed
- Paper-движок больше не хранит историю ордеров без ограничения: терминальные
  ордера обрезаются до `paper.order_history_limit` (по умолчанию 500), активные
  сохраняются всегда. Это убирает рост `state` до мегабайт и лишние записи
  полного состояния в SQLite.
- `make lint` снова зелёный: три ошибки mypy (`_terminal_snapshot`,
  `set_continuous`, `adapter.rest` в сканере). Ошибки были до этой версии.

### Added
- `scripts/paper_report.py` — атрибуция paper-сессии: реализованный результат,
  комиссии, доля maker/taker, время удержания, разбивка по стратегии/символу.
- `docs/DIAGNOSTICS.md` — диагностика торгового контура по реальным сессиям:
  50 сделок, −2.30 USDT, −40 bps в среднем, прибыльность опровергнута.
- `AGENTS.md` — правила версионирования и проверок для работы в репозитории.

### Changed
- Версия в `pyproject.toml` поднята до 0.2.1.

## [0.2.0] — 2026-10-29

### Added
- Монитор Binance Spot: стакан, публичные сделки, спред, OBI, OFI, microprice,
  CVD, стены, сканер, запись в Parquet, offline replay.
- Paper Trading: виртуальный счёт, три исследовательские стратегии, лимиты риска,
  восстановление состояния через SQLite.
- Фронтенд (React + Vite), Docker-образ, `Makefile`.
- Документация: `README.md`, `docs/AUDIT.md`, `docs/LIMITATIONS.md`,
  `docs/PAPER_TRADING.md`, `docs/PAPER_OPERATIONS.md`, `docs/API.md`,
  `docs/NIGHT_TEST.md`.

### Security
- Live-торговля заблокирована конфигурацией; ключи читаются только из окружения.
