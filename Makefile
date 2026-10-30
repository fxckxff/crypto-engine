# Makefile для Crypto Liquidity Engine

PYTHON := .venv/bin/python
RUFF := .venv/bin/ruff
MYPY := .venv/bin/mypy
PYTEST := .venv/bin/pytest
NPM := cd frontend && npm

.PHONY: help install build test lint run ui-dev ui-build clean replay backtest strategy-test

help:
	@echo "Доступные команды:"
	@echo "  make install       - Установка питоновских и фронтенд-зависимостей"
	@echo "  make build         - Сборка UI фронтенда (в frontend/dist)"
	@echo "  make test          - Запуск полных тестов (unit + integration + replay)"
	@echo "  make lint          - Проверка ruff + mypy"
	@echo "  make run           - Запуск API-сервера и монтирование UI (127.0.0.1:8000)"
	@echo "  make ui-dev        - Vite dev-сервер для фронтенда"
	@echo "  make replay        - Реплей данных из Parquet (PHASE 3)"
	@echo "  make backtest      - Бэктест стратегий на исторических данных (PHASE 4)"
	@echo "  make strategy-test - Тесты стратегий и рисков (PHASE 5-6)"
	@echo "  make clean         - Очистка временных файлов и сборки UI"

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	$(NPM) install

build:
	$(NPM) run build

test:
	$(PYTHON) -m pytest

lint:
	$(RUFF) check backend scripts
	$(MYPY) backend/app

run: build
	CLE_CONFIG=configs/default.yaml PYTHONPATH=backend $(PYTHON) -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

ui-dev:
	$(NPM) run dev

replay:
	PYTHONPATH=backend $(PYTHON) -m scripts.replay --dataset datasets/raw --symbol BTCUSDT

backtest:
	PYTHONPATH=backend $(PYTHON) -m scripts.backtest --dataset datasets/raw --symbol BTCUSDT --cash 10000

strategy-test:
	PYTHONPATH=backend $(PYTHON) -m pytest -q backend/tests/unit/test_strategy.py

clean:
	rm -rf frontend/dist .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

.PHONY: paper
paper: build
	CLE_CONFIG=configs/paper.yaml PYTHONPATH=backend $(PYTHON) -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
