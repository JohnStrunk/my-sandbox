#!/usr/bin/env python3
"""Run a synthetic session-file and SQLite locking workload on a scratch path.

Usage: python3 lima/benchmark_sessiondb.py /path/to/new/output-directory

The output directory must not already exist. Use a disposable path on the
filesystem being tested; this script does not touch a real OpenCode database.
"""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import sys
import threading
import time
from pathlib import Path

FILE_COUNT = 1_500
TRANSACTION_COUNT = 200
LOCK_HOLD_SECONDS = 0.25


def timed(callback):
    started = time.perf_counter()
    result = callback()
    return time.perf_counter() - started, result


def write_files(root: Path) -> None:
    files = root / "messages"
    files.mkdir()
    payload = {"role": "assistant", "content": "benchmark message" * 4}
    for index in range(FILE_COUNT):
        (files / f"{index:05d}.json").write_text(
            json.dumps({"id": index, **payload}), encoding="utf-8"
        )
    for path in files.iterdir():
        message = json.loads(path.read_text(encoding="utf-8"))
        message["updated"] = True
        path.write_text(json.dumps(message), encoding="utf-8")
    for path in files.iterdir():
        json.loads(path.read_text(encoding="utf-8"))


def hold_sqlite_lock(db_path: str, ready, release) -> None:
    connection = sqlite3.connect(db_path, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        ready.set()
        if not release.wait(5):
            raise TimeoutError("parent did not release SQLite lock")
        connection.commit()
    finally:
        connection.close()


def write_database(root: Path) -> tuple[str, float, float]:
    db_path = root / "sessions.sqlite3"
    connection = sqlite3.connect(db_path, timeout=5)
    journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if journal_mode.lower() != "wal":
        connection.close()
        raise RuntimeError(f"SQLite selected {journal_mode!r}, not WAL")
    connection.execute(
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
    )

    started = time.perf_counter()
    for index in range(TRANSACTION_COUNT):
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO sessions (id, payload) VALUES (?, ?)",
            (index, json.dumps({"message": index, "text": "x" * 128})),
        )
        connection.commit()
    transaction_seconds = time.perf_counter() - started
    connection.close()

    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=hold_sqlite_lock, args=(str(db_path), ready, release)
    )
    holder.start()
    if not ready.wait(5):
        holder.terminate()
        holder.join()
        raise TimeoutError("SQLite lock holder did not start")

    timer = threading.Timer(LOCK_HOLD_SECONDS, release.set)
    timer.start()
    contender = sqlite3.connect(db_path, timeout=5)
    try:
        lock_started = time.perf_counter()
        contender.execute("BEGIN IMMEDIATE")
        lock_wait = time.perf_counter() - lock_started
        contender.execute(
            "INSERT INTO sessions (id, payload) VALUES (?, ?)",
            (TRANSACTION_COUNT, "lock-waiter"),
        )
        contender.commit()
    finally:
        contender.close()
        timer.join()
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join()
    if holder.exitcode != 0:
        raise RuntimeError(f"SQLite lock holder exited {holder.exitcode}")
    return journal_mode, transaction_seconds, lock_wait


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} OUTPUT_DIR", file=sys.stderr)
        return 2

    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=False)
    file_seconds, _ = timed(lambda: write_files(root))
    db_seconds, (journal_mode, transaction_seconds, lock_wait) = timed(
        lambda: write_database(root)
    )
    print(
        json.dumps(
            {
                "files": FILE_COUNT,
                "file_write_rewrite_read_seconds": round(file_seconds, 3),
                "sqlite_transactions": TRANSACTION_COUNT,
                "sqlite_journal_mode": journal_mode,
                "sqlite_transaction_seconds": round(transaction_seconds, 3),
                "sqlite_end_to_end_seconds": round(db_seconds, 3),
                "sqlite_lock_wait_seconds": round(lock_wait, 3),
                "errors": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
