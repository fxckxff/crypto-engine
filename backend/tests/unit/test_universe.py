from decimal import Decimal

import pytest

from scripts.prepare_night import select_universe


def test_universe_filters_and_volume_ranks():
    symbols = [
        dict(
            symbol=s + "USDT",
            baseAsset=s,
            quoteAsset="USDT",
            status="TRADING",
            isSpotTradingAllowed=True,
            orderTypes=["LIMIT"],
        )
        for s in ["BTC", "ETH", "SOL", "USDC", "BTCUP", "BAD"]
    ]
    symbols[-1]["status"] = "BREAK"
    tickers = [
        dict(symbol=s["symbol"], quoteVolume=str((i + 1) * 2000000), lastPrice="1", count=20000)
        for i, s in enumerate(symbols)
    ]
    result = select_universe({"symbols": symbols}, tickers, 2)
    assert result["symbols"] == ["SOLUSDT", "ETHUSDT"]
    assert result["eligible_count"] == 3
    assert Decimal(result["eligible_volume_coverage_pct"]) < 100


def test_universe_rejects_no_market():
    with pytest.raises(ValueError):
        select_universe({"symbols": []}, [], 30)
