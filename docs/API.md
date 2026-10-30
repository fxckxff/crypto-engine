# API Reference

## WebSocket

- `/ws/market/{symbol}`: Поток снимков стакана и обновлений (JSON).
- `/ws/scanner`: Поток результатов работы сканера (раз в 1 сек).
- `/ws/system`: Поток системного статуса (раз в 2 сек).

## REST

Все методы — `GET` (кроме `/system/...`), `/api/` префикс.

### Публичные

- `/api/health`: Здоровье сервиса и статус книг.
- `/api/system/status`: Полный статус системы.
- `/api/instruments`: Список отслеживаемых инструментов и их правила торгов (tick, step, minNotional).
- `/api/scanner/results`: Результаты сканирования (отсортированы по Opportunity Score).
- `/api/orderbook/{symbol}`: Текущий снимок стакана, метрики, стены.
- `/api/analytics/{symbol}`: Детальные признаки (спред, OBI, microprice, OFI, поток, стены).

### Торговые (PHASE 7, выключены)

- `/api/system/stop`: HTTP 400: live trading not enabled.
- `/api/system/reconcile`: HTTP 400: live trading not enabled.

`/api/health`: HTTP 200 при готовности, HTTP 503 при деградации.
`/api/orderbook/{symbol}?levels=N`: N от 1 до 1000, иначе HTTP 422.

## Paper Trading

Новые маршруты счёта, ордеров и стратегий описаны в [PAPER_TRADING.md](PAPER_TRADING.md#api).
