"""One-off: remove calendar rows created by the question-as-command bug."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

JUNK_TITLES = ("Reminder", "Have you set", "Appointment", "hello, send 20, 26th")


def main() -> int:
    db = Path(__file__).resolve().parent.parent / "data" / "memory.db"
    connection = sqlite3.connect(db)
    try:
        placeholder = ",".join("?" for _ in JUNK_TITLES)
        cursor = connection.execute(
            f"DELETE FROM events WHERE title IN ({placeholder})", JUNK_TITLES
        )
        connection.commit()
        print("deleted:", cursor.rowcount)
        rows = connection.execute("SELECT id, title, start_ts FROM events").fetchall()
        print("remaining:", rows)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
