"""Снять реальный срез Binance Spot в тестовые фикстуры.

Запуск:
    .venv/bin/python scripts/record_fixture.py --symbol BTCUSDT --depth-events 60 --trades 20

Данные кладутся в backend/tests/fixtures/binance/. Это записанный поток биржи,
а не синтетика: тесты синхронизации проверяются на настоящих payload'ах.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.exchanges.binance.rest import BinanceRestClient
from app.exchanges.binance.ws import BinanceWsClient

FIXTURES = Path("backend/tests/fixtures/binance")


async def collect(symbol: str, depth_events: int, trades: int) -> None:
    """Порядок важен: снимок берётся ПОСЛЕ первого события потока.

    Иначе запись воспроизводит ситуацию «поток ушёл вперёд снимка», и тест
    проверял бы не штатную синхронизацию, а бесконечную пересинхронизацию.
    """
    FIXTURES.mkdir(parents=True, exist_ok=True)
    client = BinanceRestClient("https://api.binance.com")
    await client.start()

    ws = BinanceWsClient("wss://stream.binance.com:9443", [symbol])
    depths: list[dict] = []
    trade_rows: list[dict] = []
    snapshot: dict | None = None
    try:
        async for message in ws.messages():
            if message.stream.endswith("aggTrade"):
                if len(trade_rows) < trades:
                    trade_rows.append({"stream": message.stream, "data": message.data})
            elif "depth" in message.stream:
                if snapshot is None:
                    snapshot = await client.depth(symbol, 50)
                    (FIXTURES / f"{symbol}_snapshot.json").write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
                    info = await client.exchange_info([symbol])
                    (FIXTURES / f"{symbol}_exchange_info.json").write_text(
                        json.dumps(info, indent=1), encoding="utf-8"
                    )
                if len(depths) < depth_events:
                    depths.append({"stream": message.stream, "data": message.data})
            if len(depths) >= depth_events and len(trade_rows) >= trades:
                break
    finally:
        await client.close()

    # Сохраняем в порядке прихода: порядок важен для теста синхронизации.
    (FIXTURES / f"{symbol}_depth_stream.jsonl").write_text(
        "\n".join(json.dumps(row) for row in depths), encoding="utf-8"
    )
    (FIXTURES / f"{symbol}_trades.jsonl").write_text(
        "\n".join(json.dumps(row) for row in trade_rows), encoding="utf-8"
    )
    print(f"saved {len(depths)} depth events, {len(trade_rows)} trades to {FIXTURES}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--depth-events", type=int, default=60)
    parser.add_argument("--trades", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(collect(args.symbol.upper(), args.depth_events, args.trades))


if __name__ == "__main__":
    main()
