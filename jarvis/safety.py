"""Spoken confirmation for actions that cannot be undone.

The design rule (adapted from Mark-LVI's `core/confirm.py`): the confirmation token is
issued by the *interface*, never by the model. A tool parameter like `confirmed=true` is
a convention the model can always forge — so it is not a gate.

Flow:
    1. a destructive tool calls `request(title, detail, run)`;
    2. this module stores the callable and returns a sentence for Jarvis to say out loud;
    3. the app listens for "yes" / "no";
    4. only `resolve(True)` — called by the app after the user actually said yes — runs it.

Nothing blocks: the model keeps talking while the question is pending, and the user has
90 seconds to answer before the action quietly expires.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("safety")

TIMEOUT_SECONDS = 90.0

YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed",
    "do it", "go ahead", "go on", "please do", "affirmative", "correct", "absolutely",
}
NO_WORDS = {
    "no", "nope", "nah", "cancel", "stop", "don't", "dont", "do not", "not now",
    "never mind", "nevermind", "leave it", "forget it", "negative",
}


@dataclass
class Pending:
    title: str
    detail: str
    run: Callable[[], str]
    at: float


_pending: Pending | None = None
_lock = threading.Lock()


def request(title: str, detail: str, run: Callable[[], str]) -> str:
    """Register a destructive action and return the sentence to speak."""
    global _pending
    if not callable(run):
        return "I couldn't prepare that action, sir."
    with _lock:
        _pending = Pending(title=title[:120], detail=detail[:200], run=run, at=time.monotonic())
    log.info("confirmation requested: %s (%s)", title, detail)
    prompt = f"Shall I {title}? {detail} Say yes to confirm, or no to cancel."
    return " ".join(prompt.split())


def current() -> Pending | None:
    """The pending action, if any has not expired."""
    global _pending
    with _lock:
        if _pending is None:
            return None
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            log.info("confirmation expired: %s", _pending.title)
            _pending = None
            return None
        return _pending


def has_pending() -> bool:
    return current() is not None


def interpret(text: str) -> bool | None:
    """True = yes, False = no, None = not an answer."""
    cleaned = (text or "").strip().lower().strip(" .!?,")
    if not cleaned:
        return None
    if cleaned in YES_WORDS:
        return True
    if cleaned in NO_WORDS:
        return False
    words = set(cleaned.split())
    if words & {"yes", "yeah", "yep", "sure", "okay", "ok", "confirm"}:
        return True
    if words & {"no", "nope", "cancel", "stop", "don't", "dont"}:
        return False
    return None


def resolve(approved: bool) -> str:
    """Run or discard the pending action. Called only after the user answered."""
    global _pending
    with _lock:
        pending, _pending = _pending, None
    if pending is None:
        return "There's nothing waiting for confirmation, sir."
    if not approved:
        log.info("confirmation declined: %s", pending.title)
        return "Cancelled, sir."
    log.info("confirmation accepted: %s", pending.title)
    try:
        return pending.run()
    except Exception as exc:  # noqa: BLE001 - a failed action must still answer
        log.exception("confirmed action failed: %s", exc)
        return f"I tried, sir, but it failed: {exc}"


def cancel() -> None:
    global _pending
    with _lock:
        _pending = None
