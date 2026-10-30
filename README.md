# Crypto Liquidity Engine

Локальный исследовательский монитор Binance Spot: стакан, публичные сделки,
спред, OBI, OFI, microprice, поток сделок, стены и сканер. Интерфейс на русском.
События записываются в Parquet. Есть offline replay и консервативный
исследовательский бэктест одной базовой стратегии.

**Добавлен рабочий Paper Trading. Полное ТЗ и Live Trading ещё не приняты.**

Запуск виртуальной торговли: `make paper`. В браузере нажмите «Запустить Paper».
Баланс 1 000 виртуальных USDT, три настраиваемые стратегии, учёт позиций и
восстановление счёта. [Инструкция и ограничения](docs/PAPER_TRADING.md).
`app.mode=live` и `execution.enabled=true` отклоняются конфигурацией.
Наличие модулей `trading`, `execution`, `storage` не означает их приёмку.
См. [отчёт аудита](docs/AUDIT.md) и [ограничения](docs/LIMITATIONS.md).

## Установка и запуск

Требуются Python 3.11+ и Node.js 20+.

```sh
cd crypto-engine
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
cd frontend
npm ci
cd ..
make run
```

Открыть http://127.0.0.1:8000/. `make run` собирает UI и запускает API.
Для публичных данных API-ключи не нужны. Конфигурация — `configs/default.yaml`.
По умолчанию отслеживаются BTCUSDT и ETHUSDT. Сканер с порогом спреда 0.30%
может исключать эти ликвидные пары: это результат фильтра, а не отсутствие данных.

Для разработки интерфейса отдельно: `make ui-dev` (порт 5173, proxy на backend).
Для запуска без пересборки интерфейса:

```sh
CLE_CONFIG=configs/default.yaml PYTHONPATH=backend .venv/bin/python -m uvicorn \
  app.main:create_app --factory --host 127.0.0.1 --port 8000
```

`PYTHONPATH=backend` явно указывает исходники и не зависит от editable-установки.
Общее утверждение, что editable-установки не работают в Python 3.13, неверно.

## Проверки

```sh
make test
make lint
make build
curl -i http://127.0.0.1:8000/api/health
```

Health возвращает HTTP 200 только при работающем сервисе, соединении,
готовых стаканах и отсутствии зарегистрированной потери данных. Иначе HTTP 503.
При старте до синхронизации HTTP 503 ожидаем.

## История и исследования

```sh
make replay
make backtest
# Интервал задаётся UNIX timestamp в наносекундах, по времени ingestion:
PYTHONPATH=backend .venv/bin/python -m scripts.replay \
  --dataset datasets/raw --symbol BTCUSDT --start 1793260800000000000
```

Читаются завершённые `.parquet`. Активные записи имеют расширение
`.parquet.partial`; после корректной остановки/ротации переименовываются атомарно.
Старые файлы без footer пропускаются. Исходные исторические файлы не изменяются.

Replay прогревает состояние до начала выбранного интервала, обрабатывает все
события в порядке ingestion и считает признаки с шагом 100 мс.
Бэктест учитывает очередь перед лимитным ордером, задержку, комиссии и частичное
исполнение; не открывает короткую spot-позицию. Это исследовательская модель,
не доказательство прибыльности. Для сравнения с новой торговой системой
используйте `scripts.paper_backtest`, работающий на том же PaperEngine.

## Docker

```sh
docker compose up --build
```

API/UI — порт 8000. В этой проверке Docker недоступен: сборка контейнера не
запускалась, исправлены обнаруженные ошибки Dockerfile и healthcheck.
PostgreSQL не подключён к монитору. Для отдельной разработки каталога:
`.venv/bin/python -m pip install -e '.[postgres]'`.

## Структура

- `backend/app/market_data`, `exchanges` — транспорт и стакан.
- `backend/app/analytics`, `scanner` — признаки и отбор инструментов.
- `backend/app/recorder`, `backend/scripts` — запись и исследования.
- `backend/app/api`, `services` — API и оркестратор.
- `frontend` — React/TypeScript/Vite.
- `backend/tests` — unit, integration, replay и проверки сбоев.
- `docs/API.md` — маршруты API.

## Альткоины и ночной Paper-тест

Автоотбор до 30 ликвидных USDT-пар, отдельный виртуальный счёт и завершение по времени описаны в [docs/NIGHT_TEST.md](docs/NIGHT_TEST.md).
