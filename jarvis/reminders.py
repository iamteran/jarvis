"""Reminders and timers that survive restarts.

Stored in the same SQLite file as memory. A background service checks every few seconds
and hands due reminders to the app to speak — but only when Jarvis is not already talking
or listening, so a reminder never interrupts a conversation.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from .config import DATA

log = logging.getLogger("reminders")

DB_PATH = DATA / "memory.db"


class Reminders:
    def __init__(self) -> None:
        import sqlite3

        self._lock = threading.Lock()
        self._sqlite = sqlite3
        self.path = DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS reminders (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       text TEXT NOT NULL,
                       due REAL NOT NULL,
                       spoken INTEGER NOT NULL DEFAULT 0,
                       created REAL NOT NULL
                   )"""
            )

    def _connect(self):  # noqa: ANN202
        """Context manager that commits/rolls back AND closes (see sqlite_safe)."""
        from .sqlite_safe import closing

        return closing(self.path)

    # ------------------------------------------------------------------- creating
    def add(self, text: str, minutes: float) -> str:
        text = (text or "").strip()
        if not text:
            return "What should I remind you about, sir?"
        due = time.time() + max(1.0, minutes * 60.0)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO reminders (text, due, created) VALUES (?, ?, ?)",
                (text[:300], due, time.time()),
            )
        when = _humanise(minutes)
        log.info("reminder in %s: %s", when, text)
        return f"Righto, sir. I'll remind you to {text} in {when}."

    def add_at(self, text: str, when_ts: float) -> str:
        with self._lock, self._connect() as db:
            duplicate = db.execute(
                "SELECT id FROM reminders WHERE spoken = 0 AND text = ? AND ABS(due - ?) < 60",
                (text[:300], when_ts),
            ).fetchone()
            if duplicate is not None:
                return "You already asked me for that one, sir."
            db.execute(
                "INSERT INTO reminders (text, due, created) VALUES (?, ?, ?)",
                (text[:300], when_ts, time.time()),
            )
        clock = time.strftime("%I:%M %p", time.localtime(when_ts)).lstrip("0").replace(" 0", " ")
        day = time.strftime("%A", time.localtime(when_ts))
        return f"Righto, sir. I'll remind you to {text} on {day} at {clock}."

    # ------------------------------------------------------------------ lifecycle
    def due(self) -> list:  # noqa: ANN201
        now = time.time()
        with self._lock, self._connect() as db:
            return db.execute(
                "SELECT id, text, due FROM reminders WHERE spoken = 0 AND due <= ? ORDER BY due",
                (now,),
            ).fetchall()

    def mark_spoken(self, reminder_id: int) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE reminders SET spoken = 1 WHERE id = ?", (reminder_id,))

    def cancel_all(self) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM reminders WHERE spoken = 0")
            return cursor.rowcount

    def list_speech(self) -> str:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT text, due FROM reminders WHERE spoken = 0 ORDER BY due"
            ).fetchall()
        if not rows:
            return "You have no reminders pending, sir."
        parts = []
        for row in rows:
            parts.append(f"{row['text']} at {time.strftime('%H:%M', time.localtime(row['due']))}")
        return "You have: " + "; ".join(parts) + "."


def _humanise(minutes: float) -> str:
    if minutes < 1:
        return f"{int(minutes * 60)} seconds"
    if minutes < 60:
        return f"{int(minutes)} minute{'s' if int(minutes) != 1 else ''}"
    hours = minutes / 60
    if abs(hours - round(hours)) < 0.05:
        return f"{int(round(hours))} hour{'s' if int(round(hours)) != 1 else ''}"
    return f"{hours:.1f} hours"


_UNITS = {"second": 1 / 60, "sec": 1 / 60, "s": 1 / 60,
          "minute": 1, "min": 1, "m": 1,
          "hour": 60, "hr": 60, "h": 60}

REMIND_RE = re.compile(
    r"\b(?:remind me|set a reminder|set a timer|wake me)\b"
    r".*?(?:\bto\b|\bfor\b|\bthat\b)?\s*(?P<text>.+?)"
    r"\s+in\s+(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.I,
)
TIMER_RE = re.compile(
    r"\b(?:set a timer|timer)\b.*?(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.I,
)


class ReminderService(threading.Thread):
    """Polls for due reminders and asks the app to speak them."""

    def __init__(self, speak_callback, interval: float = 5.0) -> None:
        super().__init__(name="reminders", daemon=True)
        self.reminders = Reminders()
        self.speak = speak_callback
        self.interval = interval
        self._stop = threading.Event()

    def run(self) -> None:
        log.info("reminder service running")
        while not self._stop.wait(self.interval):
            try:
                for row in self.reminders.due():
                    text = row["text"]
                    if self.speak(f"Reminder, sir: {text}."):
                        self.reminders.mark_spoken(row["id"])
            except Exception as exc:  # noqa: BLE001 - never die in a background thread
                log.warning("reminder tick failed: %s", exc)
            try:
                self._announce_events()
            except Exception as exc:  # noqa: BLE001
                log.warning("calendar tick failed: %s", exc)

    def _announce_events(self) -> None:
        """Speak an upcoming calendar event once, shortly before it starts."""
        from .calendar_store import Calendar

        calendar = Calendar()
        for row in calendar.due_soon():
            if self.speak(f"Heads up, sir: {row['title']} starts in a moment."):
                calendar.mark_announced(row["id"])

    def stop(self) -> None:
        self._stop.set()


def parse_reminder(text: str) -> tuple[str, float] | None:
    """Parse 'remind me to stretch in 20 minutes' -> ('stretch', 20.0)."""
    for pattern in (REMIND_RE, TIMER_RE):
        match = pattern.search(text or "")
        if not match:
            continue
        amount = float(match.group("n"))
        unit = match.group("unit").lower().rstrip("s")
        factor = _UNITS.get(unit)
        if factor is None:
            continue
        body = match.groupdict().get("text") or "timer"
        body = body.strip(" ,.")
        body = re.sub(r"^(?:to|that)\s+", "", body, flags=re.I)
        if not body or body.lower() in {"a timer", "timer"}:
            body = "the timer"
        return body, amount * factor
    return None
