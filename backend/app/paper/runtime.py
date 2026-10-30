"""Durable paper commands: single writer, SQLite WAL transaction, rollback + halt on failure."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.paper.engine import PaperEngine


class PaperStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock_file: Any = None

    def open(self) -> dict | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.path.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT, hash TEXT)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY, "
                    "kind TEXT, ts_ns INTEGER, payload TEXT)"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS events_kind_sequence ON events(kind, sequence)")
                row = conn.execute("SELECT payload, hash FROM state WHERE id=1").fetchone()
            if row:
                if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                    raise ValueError("Paper state checksum mismatch")
                return json.loads(row[0])
            return None
        except BaseException:
            self.close()
            raise

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def save(self, state: dict, events: list[dict]) -> None:
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO state VALUES (1,?,?)", (payload, hashlib.sha256(payload.encode()).hexdigest())
            )
            conn.executemany(
                "INSERT INTO events VALUES (?,?,?,?)",
                [(e["sequence"], e["kind"], e["ts_ns"], json.dumps(e, ensure_ascii=False)) for e in events],
            )

    def history(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT payload FROM events WHERE kind=? ORDER BY sequence DESC LIMIT ?", (kind, limit)
                )
            else:
                rows = conn.execute("SELECT payload FROM events ORDER BY sequence DESC LIMIT ?", (limit,))
            return [json.loads(row[0]) for row in rows]

    def close(self) -> None:
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None


class PaperRuntime:
    def __init__(self, engine: PaperEngine):
        self.engine = engine
        self.store = PaperStore(engine.settings.database_path)
        self.lock = asyncio.Lock()
        self.error: str | None = None
        self.opened = False

    async def open(self, now: int) -> None:
        state = await asyncio.to_thread(self.store.open)
        self.opened = True
        try:
            if state:
                self.engine.restore(state)
                self.engine.stop(now, "Восстановлено после перезапуска; требуется запуск")
                self.engine.emit("RECOVERED", now, positions=len(self.engine.positions))
            else:
                self.engine.emit("ACCOUNT_CREATED", now, cash=self.engine.cash)
            await asyncio.to_thread(self.store.save, self.engine.state(), self.engine.events)
            self.engine.events.clear()
        except BaseException:
            self.store.close()
            self.opened = False
            raise

    async def apply(self, method: str, *args, current_time: bool = False, **kwargs):
        async with self.lock:
            if self.error:
                raise RuntimeError(self.error)
            if current_time:
                args = (*args, time.time_ns())
            # No orders means a public trade can only advance deduplication.
            # Avoid serializing the entire account for every market tick.
            if method == "trade" and not self.engine.active(args[0]):
                deadline = self.engine.settings.session_end_ns
                if not (self.engine.running and deadline and args[5] >= deadline):
                    self.engine.last_trade_ids[args[0]] = max(
                        args[1], self.engine.last_trade_ids.get(args[0], -1)
                    )
                    return
            before = self.engine.state()
            try:
                result = getattr(self.engine, method)(*args, **kwargs)
                if self.engine.revision != before["revision"]:
                    # Shield commit: a cancelled HTTP client must not interrupt a disk transaction.
                    commit = asyncio.create_task(
                        asyncio.to_thread(self.store.save, self.engine.state(), self.engine.events)
                    )
                    try:
                        await asyncio.shield(commit)
                    except asyncio.CancelledError:
                        await commit
                        self.engine.events.clear()
                        raise
                    self.engine.events.clear()
                return result
            except ValueError:
                self.engine.restore(before)
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.engine.restore(before)
                self.error = f"Paper storage/execution failure: {type(exc).__name__}"
                self.engine.stop(time.time_ns(), self.error)
                raise RuntimeError(self.error) from exc

    async def summary(self, now: int) -> dict:
        async with self.lock:
            return {**self.engine.summary(now), "storage_error": self.error}

    async def close(self, now: int) -> None:
        if not self.opened:
            return
        try:
            if not self.error:
                await self.apply("stop", now, "Приложение остановлено")
        finally:
            self.store.close()
            self.opened = False
