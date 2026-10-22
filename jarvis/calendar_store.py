"""A local calendar, kept in the same SQLite file as memories and reminders.

Why local rather than Google Calendar: it works offline, needs no OAuth, and answers in
milliseconds. The assistant can still *open* Google Calendar when asked — but "what's up
for today?" and "set an appointment on 12 October at 7:30" are answered from here.

Events are announced once, shortly before they start, by the reminder service.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import threading
import time

from .config import DATA

log = logging.getLogger("calendar")

DB_PATH = DATA / "memory.db"
ANNOUNCE_WINDOW_S = 90     # announce this long before an event starts


class Calendar:
    def __init__(self) -> None:
        self.path = DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS events (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       title TEXT NOT NULL,
                       start_ts REAL NOT NULL,
                       announced INTEGER NOT NULL DEFAULT 0,
                       created REAL NOT NULL
                   )"""
            )

    def _connect(self):
        """Context manager that commits/rolls back AND closes (see sqlite_safe)."""
        from .sqlite_safe import closing

        return closing(self.path)

    # ------------------------------------------------------------------- writing
    def add(self, title: str, start: dt.datetime) -> str:
        title = (title or "Appointment").strip()[:200]
        timestamp = start.timestamp()
        with self._lock, self._connect() as db:
            # Asking twice must not create two appointments.
            duplicate = db.execute(
                "SELECT title FROM events WHERE title = ? AND ABS(start_ts - ?) < 60",
                (title, timestamp),
            ).fetchone()
            if duplicate is not None:
                log.info("duplicate event ignored: %s at %s", title, start)
                return f"You already have {title} at that time, sir."
            db.execute(
                "INSERT INTO events (title, start_ts, created) VALUES (?, ?, ?)",
                (title, timestamp, time.time()),
            )
        log.info("event added: %s at %s", title, start)
        return f"Righto, sir. {title} on {start.strftime('%A %d %B at %I:%M %p').replace(' 0', ' ').lstrip('0')}."

    def delete(self, event_id: int) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------------- reading
    def between(self, start: dt.datetime, end: dt.datetime) -> list[sqlite3.Row]:
        with self._lock, self._connect() as db:
            return db.execute(
                "SELECT * FROM events WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts",
                (start.timestamp(), end.timestamp()),
            ).fetchall()

    def on_day(self, day: dt.date | None = None) -> list[sqlite3.Row]:
        day = day or dt.date.today()
        start = dt.datetime.combine(day, dt.time.min)
        return self.between(start, start + dt.timedelta(days=1))

    def upcoming(self, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock, self._connect() as db:
            return db.execute(
                "SELECT * FROM events WHERE start_ts >= ? ORDER BY start_ts LIMIT ?",
                (time.time() - 60, limit),
            ).fetchall()

    def due_soon(self, window_s: int = ANNOUNCE_WINDOW_S) -> list[sqlite3.Row]:
        now = time.time()
        with self._lock, self._connect() as db:
            return db.execute(
                "SELECT * FROM events WHERE announced = 0 AND start_ts BETWEEN ? AND ? "
                "ORDER BY start_ts",
                (now, now + window_s),
            ).fetchall()

    def mark_announced(self, event_id: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE events SET announced = 1 WHERE id = ?", (event_id,))

    # --------------------------------------------------------------------- speech
    @staticmethod
    def _format_row(row: sqlite3.Row) -> str:
        when = dt.datetime.fromtimestamp(row["start_ts"])
        return f"{row['title']} at {when.strftime('%I:%M %p').lstrip('0').replace(' 0', ' ')}"

    def speech_today(self) -> str:
        rows = self.on_day()
        if not rows:
            return "You have nothing scheduled today, sir."
        if len(rows) == 1:
            return f"You have one thing today: {self._format_row(rows[0])}."
        listing = "; ".join(self._format_row(row) for row in rows[:5])
        return f"You have {len(rows)} things today: {listing}."

    def speech_upcoming(self) -> str:
        rows = self.upcoming()
        if not rows:
            return "There's nothing coming up, sir."
        parts = []
        for row in rows[:5]:
            when = dt.datetime.fromtimestamp(row["start_ts"])
            parts.append(f"{row['title']} on {when.strftime('%A at %I:%M %p').replace(' 0', ' ').lstrip('0')}")
        return "Coming up: " + "; ".join(parts) + "."

    def next_event_speech(self) -> str:
        rows = self.upcoming(1)
        if not rows:
            return ""
        row = rows[0]
        when = dt.datetime.fromtimestamp(row["start_ts"])
        return f"Your next appointment is {row['title']} on {when.strftime('%A at %I:%M %p').replace(' 0', ' ').lstrip('0')}."


def today_count() -> int:
    try:
        return len(Calendar().on_day())
    except Exception:  # noqa: BLE001
        return 0
