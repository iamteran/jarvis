"""Logging: readable console output plus a rotating file, with a tiny timing helper."""
from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LOGS

_CONFIGURED = False


def setup(level: str = "INFO", debug: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = logging.DEBUG if debug else getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(name)-18s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        Path(LOGS) / "jarvis.log", maxBytes=1_500_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # keep noisy libraries quiet
    for noisy in ("urllib3", "PIL", "pystray", "openwakeword"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


class Timer:
    """Context manager that reports elapsed milliseconds on exit."""

    def __init__(self, label: str, logger: logging.Logger | None = None) -> None:
        self.label = label
        self.log = logger or logging.getLogger("timing")
        self.ms = 0.0

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.ms = (time.perf_counter() - self._t0) * 1000.0
        self.log.info("%-22s %6.0f ms", self.label, self.ms)
