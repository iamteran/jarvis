"""One shared undo stack for every action that changes state.

A voice assistant misunderstands. When it does, "sorry" is not a remedy — the volume is
already at 10% and the window is already closed. Asking "are you sure?" before everything
is worse: an assistant that checks with you before turning the volume down is one you
stop talking to. So: act immediately, remember how to reverse it, let the user say
"undo". Only genuinely irreversible things should ask first (see safety/confirm.py).

Actions capture the "before" state and hand back a zero-argument callable:

    old = get_volume()
    set_volume(30)
    push_undo("volume -> 30%", lambda: set_volume_raw(old))

Adapted from `core/undo.py` in FatihMakes/Mark-LVI (CC BY-NC 4.0, non-commercial).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("undo")

MAX_DEPTH = 10


@dataclass
class _Entry:
    label: str
    undo: Callable[[], str]
    at: float = field(default_factory=time.monotonic)


_stack: list[_Entry] = []
_lock = threading.Lock()


def push_undo(label: str, undo_fn: Callable[[], str]) -> None:
    """Record that `label` just happened and `undo_fn()` reverses it. Never raises."""
    if not callable(undo_fn):
        return
    try:
        with _lock:
            _stack.append(_Entry(label=str(label)[:120], undo=undo_fn))
            if len(_stack) > MAX_DEPTH:
                del _stack[:-MAX_DEPTH]
    except Exception:  # noqa: BLE001 - a broken undo must not break the action
        pass


def pop_undo() -> str | None:
    """Run the most recent undo. Returns what to say, or None if the stack is empty."""
    with _lock:
        if not _stack:
            return None
        entry = _stack.pop()
    try:
        result = entry.undo()
    except Exception as exc:  # noqa: BLE001
        log.warning("undo of %r failed: %s", entry.label, exc)
        return f"I couldn't undo {entry.label}, I'm afraid."
    rest = len(_stack)
    tail = f" {rest} more step{'s' if rest != 1 else ''} available." if rest else ""
    return f"Undone: {entry.label}.{tail}".replace("..", ".")


def pending() -> list[str]:
    with _lock:
        return [entry.label for entry in reversed(_stack)]


def clear() -> None:
    with _lock:
        _stack.clear()


def depth() -> int:
    with _lock:
        return len(_stack)
