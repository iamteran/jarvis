"""The spoken start-up briefing: time, reminders, and something worth remembering.

Deliberately short. A briefing that lists everything becomes noise; three facts at most,
and the user can ask for more.
"""
from __future__ import annotations

from datetime import datetime


def briefing_text() -> str:
    now = datetime.now()
    hour = now.hour
    part = "Good morning" if 5 <= hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
    clock = now.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
    text = f"{part}, sir. It is {clock} on {now.strftime('%A')}."

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
