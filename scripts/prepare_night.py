"""Select a reproducible liquid USDT universe using public Binance data; never sends orders."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import urlopen

import yaml
from app.config.loader import build_config
from app.core.tls import build_ssl_context

STABLE_BASES = {
    "U",
    "USDC",
    "FDUSD",
    "TUSD",
    "USDP",
    "DAI",
    "USDD",
    "USD1",
    "USDE",
    "USDS",
    "BUSD",
    "EUR",
    "EURI",
    "AEUR",
    "XUSD",
    "RLUSD",
    "PAXG",
    "XAUT",
}


TOKENIZED_EQUITIES = {
    "INTCB",
    "MSTRB",
    "GOOGLB",
    "SNDKB",
    "SPCXB",
    "SOXLB",
    "SKHYB",
    "CRCLB",
    "MUB",
    "AAPLB",
    "TSLAB",
    "NVDAB",
    "METAB",
    "MSFTB",
    "AMZNB",
    "AMDB",
    "COINB",
    "PLTRB",
    "QQQB",
    "SPYB",
}


def select_universe(info: dict, tickers: list, count: int = 30, min_volume: Decimal = Decimal("1000000")) -> dict:
    if not 2 <= count <= 30:
        raise ValueError("count must be between 2 and 30")
    instruments = {
        x["symbol"]: x
        for x in info["symbols"]
        if x.get("status") == "TRADING"
        and x.get("quoteAsset") == "USDT"
        and x.get("isSpotTradingAllowed", False)
        and "LIMIT" in x.get("orderTypes", [])
        and x.get("baseAsset") not in STABLE_BASES | TOKENIZED_EQUITIES
        and not x.get("baseAsset", "").endswith(("UP", "DOWN", "BULL", "BEAR"))
    }
    candidates = []
    for ticker in tickers:
        if ticker.get("symbol") not in instruments:
            continue
        try:
            volume = Decimal(ticker["quoteVolume"])
            price = Decimal(ticker["lastPrice"])
            trades = int(ticker["count"])
            if not volume.is_finite() or not price.is_finite() or volume < min_volume or price <= 0 or trades < 10000:
                continue
        except (KeyError, ValueError, InvalidOperation):
            continue
        candidates.append({"symbol": ticker["symbol"], "quote_volume_usdt": str(volume), "trades_24h": trades})
    candidates.sort(key=lambda x: (-Decimal(x["quote_volume_usdt"]), x["symbol"]))
    selected = candidates[:count]
    if len(selected) < 2:
        raise ValueError("Not enough liquid spot markets")
    total = sum((Decimal(x["quote_volume_usdt"]) for x in candidates), Decimal(0))
    coverage = sum((Decimal(x["quote_volume_usdt"]) for x in selected), Decimal(0)) / total * 100
    return {
        "symbols": [x["symbol"] for x in selected],
        "selected": selected,
        "eligible_count": len(candidates),
        "eligible_volume_coverage_pct": str(coverage),
        "candidates": candidates,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--until", required=True, help="ISO datetime with explicit timezone")
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--output", default="configs/night.yaml")
    args = parser.parse_args()
    end = datetime.fromisoformat(args.until)
    if end.tzinfo is None or end.timestamp() <= datetime.now().timestamp():
        parser.error("--until must be a future time with timezone")
    config = yaml.safe_load(Path("configs/paper.yaml").read_text())
    base = config["exchange"]["rest_base_url"]
    # One exchangeInfo request (weight 20) and one full ticker request (weight 80).
    with urlopen(base + "/api/v3/exchangeInfo", timeout=30, context=build_ssl_context()) as response:
        info = json.load(response)
    with urlopen(base + "/api/v3/ticker/24hr", timeout=30, context=build_ssl_context()) as response:
        tickers = json.load(response)
    universe = select_universe(info, tickers, args.count)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    folder = Path("datasets/night") / stamp
    folder.mkdir(parents=True)
    config["exchange"]["symbols"] = universe["symbols"]
    config["market_data"]["record_raw_events"] = False
    config["market_data"]["record_feature_snapshots"] = False
    config["market_data"]["orderbook_depth"] = 50
    config["market_data"]["feature_interval_ms"] = 250
    config["storage"]["raw_dir"] = str(folder / "raw")
    config["storage"]["derived_dir"] = str(folder / "derived")
    config["paper"] = {
        "initial_cash": "1000",
        "database_path": str(folder / "account.sqlite3"),
        "session_end_ns": int(end.timestamp() * 1e9),
        "isolate_stale_symbols": True,
    }
    build_config(config)
    Path(args.output).write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    universe.update(
        {
            "captured_at": datetime.now().astimezone().isoformat(),
            "end_at": end.isoformat(),
            "config": args.output,
            "run_directory": str(folder),
            "selection": "Top quote volume among eligible USDT spot markets; fixed for the session",
        }
    )
    (folder / "universe.json").write_text(json.dumps(universe, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in universe.items() if k != "candidates"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
