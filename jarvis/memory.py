"""Persistent memory: what Jarvis knows about you, across sessions.

Three kinds of memory, deliberately separate:

* **Facts** — durable statements the user asked to remember ("prefers tea over coffee").
  Retrieved by keyword and injected into every system prompt.
* **Aliases** — spoken names for real things ("my cv folder" -> a path), so ambiguous
  phrases resolve without the model guessing.
* **Turns** — a rolling transcript with a cheap daily summary, for continuity.

SQLite because it needs no server, survives crashes, and is a single file the user can
delete. Nothing here ever leaves the machine.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import DATA

log = logging.getLogger("memory")

DB_PATH = DATA / "memory.db"


class Memory:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DB_PATH)
        self._lock = threading.Lock()
        self._ensure()

    # --------------------------------------------------------------------- setup
    def _connect(self):
        """Return a context manager that commits/rolls back AND closes the connection."""
        from .sqlite_safe import closing

        return closing(self.path)

    def _ensure(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS facts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        text TEXT NOT NULL,
                        normalised TEXT NOT NULL UNIQUE,
                        created REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS aliases (
                        name TEXT PRIMARY KEY,
                        target TEXT NOT NULL,
                        created REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS turns (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        role TEXT NOT NULL,
                        text TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS notes (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )

    # --------------------------------------------------------------------- facts
    @staticmethod
    def _normalise(text: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()

    def remember(self, fact: str) -> str:
        fact = (fact or "").strip()
        if not fact:
            return ""
        normalised = self._normalise(fact)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO facts (text, normalised, created) VALUES (?, ?, ?)",
                (fact[:500], normalised[:500], time.time()),
            )
        log.info("remembered: %s", fact[:90])
        return fact

    def recall(self, query: str = "", limit: int = 12) -> list[str]:
        with self._lock, self._connect() as db:
            if query.strip():
                needle = f"%{self._normalise(query)[:40]}%"
                rows = db.execute(
                    "SELECT text FROM facts WHERE normalised LIKE ? ORDER BY id DESC LIMIT ?",
                    (needle, limit),
                ).fetchall()
                if rows:
                    return [row["text"] for row in rows]
            rows = db.execute(
                "SELECT text FROM facts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [row["text"] for row in rows]

    def recall_speech(self, query: str = "") -> str:
        facts = self.recall(query, limit=8)
        if not facts:
            return "I don't have anything on that yet, sir."
        if query.strip():
            return "What I know: " + "; ".join(facts) + "."
        return "Here is what I remember: " + "; ".join(facts) + "."

    def forget(self, query: str) -> int:
        needle = f"%{self._normalise(query)[:40]}%"
        with self._lock, self._connect() as db:
            cursor = db.execute("DELETE FROM facts WHERE normalised LIKE ?", (needle,))
            return cursor.rowcount

    # ------------------------------------------------------------------- aliases
    def set_alias(self, name: str, target: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO aliases (name, target, created) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET target=excluded.target",
                (self._normalise(name)[:80], target, time.time()),
            )

    def get_alias(self, name: str) -> str | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT target FROM aliases WHERE name = ?", (self._normalise(name)[:80],)
            ).fetchone()
        return row["target"] if row else None

    def aliases(self) -> dict[str, str]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT name, target FROM aliases").fetchall()
        return {row["name"]: row["target"] for row in rows}

    # --------------------------------------------------------------------- turns
    def log_turn(self, user: str, assistant: str) -> None:
        now = time.time()
        with self._lock, self._connect() as db:
            db.executemany(
                "INSERT INTO turns (ts, role, text) VALUES (?, ?, ?)",
                [(now, "user", (user or "")[:2000]), (now, "assistant", (assistant or "")[:4000])],
            )
            db.execute(
                "DELETE FROM turns WHERE id NOT IN "
                "(SELECT id FROM turns ORDER BY id DESC LIMIT 400)"
            )

    def recent_turns(self, limit: int = 6) -> list[tuple[str, str]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT role, text FROM turns ORDER BY id DESC LIMIT ?", (limit * 2,)
            ).fetchall()
        rows = list(reversed(rows))
        out: list[tuple[str, str]] = []
        for row in rows:
            out.append((row["role"], row["text"]))
        return out[-limit * 2:]

    # ------------------------------------------------------------------ prompt block
    def context(self, max_facts: int = 12) -> str:
        facts = self.recall(limit=max_facts)
        aliases = self.aliases()
        parts: list[str] = []
        if facts:
            parts.append("Facts you remember about the user: " + "; ".join(facts) + ".")
        if aliases:
            pairs = ", ".join(f"'{name}' means {target}" for name, target in list(aliases.items())[:8])
            parts.append("Aliases the user uses: " + pairs + ".")
        return "\n".join(parts)

    def stats(self) -> dict:
        with self._lock, self._connect() as db:
            facts = db.execute("SELECT COUNT(*) AS n FROM facts").fetchone()["n"]
            turns = db.execute("SELECT COUNT(*) AS n FROM turns").fetchone()["n"]
            aliases = db.execute("SELECT COUNT(*) AS n FROM aliases").fetchone()["n"]
        return {"facts": facts, "turns": turns, "aliases": aliases, "path": str(self.path)}


def summarise_day() -> str:
    """Human-readable line for the startup log / `--memory` command."""
    stats = Memory().stats()
    return (f"{stats['facts']} facts, {stats['aliases']} aliases, "
            f"{stats['turns']} turns remembered ({stats['path']})")
