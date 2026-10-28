"""User-defined local routines: named sequences of safe text commands."""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .config import DATA


class RoutineStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or DATA / "routines.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def normalise(name: str) -> str:
        return re.sub(r"[^a-z0-9 _-]", "", (name or "").lower()).strip()[:80]

    def save(self, name: str, commands: list[str]) -> str:
        key = self.normalise(name)
        cleaned = [str(c).strip()[:300] for c in commands if str(c).strip()]
        if not key or not cleaned:
            raise ValueError("A routine needs a name and at least one command.")
        with self._lock:
            data = self._read()
            data[key] = {"name": name.strip()[:80], "commands": cleaned, "updated": time.time()}
            self._write(data)
        return key

    def get(self, name: str) -> dict | None:
        with self._lock:
            return self._read().get(self.normalise(name))

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._read().values(), key=lambda x: x.get("name", "").lower())

    def delete(self, name: str) -> bool:
        with self._lock:
            data = self._read()
            removed = data.pop(self.normalise(name), None) is not None
            if removed:
                self._write(data)
            return removed

    def run(self, name: str, dispatch: Callable[[str], str]) -> list[str]:
        routine = self.get(name)
        if not routine:
            return [f"I don't know a routine called {name}, sir."]
        replies = []
        for command in routine["commands"][:12]:
            replies.append(dispatch(command))
        return replies
