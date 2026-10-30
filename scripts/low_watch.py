"""Run a bounded Paper test against localhost, preserving user stops and risk halts."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.config.loader import load_config


def request(base: str, path: str, command: bool = False):
    req = Request(
        base + path,
        data=b"{}" if command else None,
        headers={"X-Paper-Command": "1", "Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=10) as response:
            return json.load(response)
    except HTTPError as exc:
        if not command and exc.code == 503:
            return json.load(exc)
        raise


def aggregate(database: Path) -> dict:
    counts: Counter = Counter()
    reasons: Counter = Counter()
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5) as conn:
        counts.update(dict(conn.execute("SELECT kind, count(*) FROM events GROUP BY kind")))
        for (payload,) in conn.execute("SELECT payload FROM events WHERE kind='DECISION'"):
            event = json.loads(payload)
            reasons[event.get("reason", "unknown")] += 1
    return {"events": dict(counts), "decision_reasons": dict(reasons)}


def save_report(folder: Path, report: dict):
    temporary = folder / "report.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    temporary.replace(folder / "report.json")
    account = report.get("account", {})
    text = (
        f"# Ночной Paper-тест\n\nСостояние: {report['state']}\n\n"
        f"Обновлено: {report['updated_at']}\n\nЗавершение: {report['end_at']}\n\n"
        f"Пары ({len(report['symbols'])}): {', '.join(report['symbols'])}\n\n"
        f"Эквити: {account.get('equity', 'нет данных')} USDT. "
        f"Реализованный PnL: {account.get('realized_pnl', 'нет данных')} USDT. "
        f"Комиссии: {account.get('fees', 'нет данных')} USDT.\n\n"
        f"Открытых позиций: {len(account.get('positions', []))}. "
        f"Причина остановки: {account.get('halt_reason')}.\n\n"
        f"Ошибка наблюдения: {report.get('last_error')}.\n\n"
        "Исполнения виртуальные, с комиссией и моделью очереди. Отсутствие сделок — допустимый результат. "
        "Один тест не доказывает прибыльность. Журнал — account.sqlite3; агрегаты — report.json.\n"
    )
    temporary = folder / "report-md.tmp"
    temporary.write_text(text)
    temporary.replace(folder / "report.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/night.yaml")
    parser.add_argument("--report-dir", help="Optional directory for the user-facing reports")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if args.base != "http://127.0.0.1:8002":
        parser.error("Only the local Paper service is supported")
    config = load_config(args.config)
    if config.app.mode != "paper" or not config.paper.session_end_ns:
        parser.error("A bounded Paper configuration is required")
    database = Path(config.paper.database_path)
    folder = database.parent
    report_folder = Path(args.report_dir) if args.report_dir else folder
    report_folder.mkdir(parents=True, exist_ok=True)
    end = config.paper.session_end_ns / 1e9
    report = {
        "state": "waiting",
        "started_at": datetime.now(UTC).isoformat(),
        "end_at": datetime.fromtimestamp(end, UTC).isoformat(),
        "symbols": config.exchange.symbols,
        "samples": 0,
        "healthy_samples": 0,
        "running_samples": 0,
        "last_error": None,
    }
    started = False
    matched = False
    flatten_requested = False
    startup_deadline = min(time.time() + 300, end)
    last_aggregate = 0.0
    try:
        while time.time() < end + 120:
            try:
                account = request(args.base, "/api/paper/status")
                if account.get("settings", {}).get("database_path") != str(database):
                    raise RuntimeError("Server account does not match this test; no commands sent")
                matched = True
                health = request(args.base, "/api/health")
                report["account"], report["health"] = account, health
                report["samples"] += 1
                report["healthy_samples"] += int(health.get("status") == "ok")
                report["running_samples"] += int(account.get("running", False))
                if shutil.disk_usage(folder).free < 2 * 1024**3:
                    request(args.base, "/api/paper/stop", True)
                    report["state"] = "stopped_low_disk"
                    break
                if time.time() >= end:
                    if not flatten_requested:
                        request(args.base, "/api/paper/flatten", True)
                        flatten_requested = True
                    report["state"] = "closing"
                    if not account.get("positions"):
                        report["state"] = "completed"
                        break
                elif not started and time.time() < startup_deadline:
                    if account.get("running"):
                        started = True
                    elif health.get("status") == "ok" or (
                        health.get("books")
                        and not health.get("data_loss")
                        and all(state in {"READY", "STALE"} for state in health["books"].values())
                    ):
                        request(args.base, "/api/paper/start", True)
                        started = True
                elif not started:
                    report["state"] = "failed_to_start"
                    break
                else:
                    report["state"] = "running" if account.get("running") else "halted"
                if database.exists() and time.time() - last_aggregate >= 60:
                    report["statistics"] = aggregate(database)
                    last_aggregate = time.time()
                report["last_error"] = None
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                report["last_error"] = str(exc)
                report["state"] = "service_unavailable"
            report["updated_at"] = datetime.now(UTC).isoformat()
            save_report(report_folder, report)
            time.sleep(5)
    finally:
        if matched:
            try:
                current = request(args.base, "/api/paper/status")
                if current.get("settings", {}).get("database_path") == str(database):
                    request(args.base, "/api/paper/stop", True)
                    report["account"] = request(args.base, "/api/paper/status")
            except (OSError, ValueError) as exc:
                report["last_error"] = str(exc)
        if database.exists():
            report["statistics"] = aggregate(database)
        if report.get("account", {}).get("positions"):
            report["state"] = "incomplete_open_positions"
        report["updated_at"] = datetime.now(UTC).isoformat()
        save_report(report_folder, report)


if __name__ == "__main__":
    main()
