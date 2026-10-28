"""Local, privacy-first event timeline for JARVIS."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import DATA


class ActivityLog:
    """Append-only JSONL timeline; bounded reads keep the dashboard fast."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DATA / "activity.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, kind: str, summary: str, *, status: str = "ok",
               detail: str = "") -> None:
        row = {
            "ts": time.time(),
            "kind": str(kind)[:40],
            "summary": str(summary)[:500],
            "status": str(status)[:20],
        }
        if detail:
            row["detail"] = str(detail)[:1000]
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            with self._lock:
                rows = self.path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 200)):]
            result = []
            for line in reversed(rows):
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return result
        except OSError:
            return []

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
