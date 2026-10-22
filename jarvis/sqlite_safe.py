"""A SQLite connection context manager that actually closes.

`with sqlite3.connect(...) as conn:` commits the transaction but does **not** close the
connection. On Windows the leaked handle keeps a lock on the database file, which made
the test suite crawl (and would eventually leak handles in a long-running assistant).

Use:

    with closing(path) as db:
        db.execute(...)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class _ClosingConnection:
    def __init__(self, path: Path | str, timeout: float = 10.0) -> None:
        self._path = str(path)
        self._timeout = timeout
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path, timeout=self._timeout, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        self._connection = connection
        return connection

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        connection = self._connection
        self._connection = None
        if connection is None:
            return False
        try:
            if exc_type is None:
                connection.commit()
            else:
                connection.rollback()
        finally:
            connection.close()
        return False


def closing(path: Path | str, timeout: float = 10.0) -> _ClosingConnection:
    return _ClosingConnection(path, timeout)
