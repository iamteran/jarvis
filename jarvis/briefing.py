"""The spoken start-up briefing: time, weather, reminders, and something worth remembering.

Deliberately short. A briefing that lists everything becomes noise; three facts at most,
and the user can ask for more.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger("briefing")


def _weather_summary() -> str:
    """Fetch a one-line weather summary from wttr.in (free, no key, 2 s timeout)."""
    try:
        import urllib.request
        # %C = weather condition, %t = temperature, %w = wind
        url = "https://wttr.in/?format=%C,+%t,+wind+%w"
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - intentional HTTP
            raw = resp.read().decode("utf-8", errors="ignore").strip()
        if raw and len(raw) < 120:
            return f"Outside: {raw}."
    except Exception as exc:  # noqa: BLE001 - weather is nice but never required
        log.debug("weather fetch failed: %s", exc)
    return ""


def briefing_text() -> str:
    now = datetime.now()
    hour = now.hour
    part = "Good morning" if 5 <= hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    clock = now.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
    text = f"{part}, sir. It is {clock} on {now.strftime('%A')}."

    # Weather — fetched in parallel with a tight timeout; silence is fine if it fails.
    weather = _weather_summary()
    if weather:
        text += " " + weather

    try:
        from .calendar_store import Calendar

        text += " " + Calendar().speech_today()
    except Exception:  # noqa: BLE001
        pass

    try:
        from .reminders import Reminders

        pending = Reminders().list_speech()
        if pending and "no reminders" not in pending.lower():
            text += " " + pending
    except Exception:  # noqa: BLE001
        pass

    try:
        from .memory import Memory

        facts = Memory().recall(limit=1)
        if facts:
            text += f" And I still remember: {facts[0]}."
    except Exception:  # noqa: BLE001
        pass

    return text
