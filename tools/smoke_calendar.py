"""Manual smoke check against the real database (not the test one)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.calendar_store import Calendar
from jarvis.reminders import Reminders
from jarvis.router import Router
from jarvis.tools import Tools


def main() -> int:
    router = Router()
    calendar = Calendar()
    print("today    :", calendar.speech_today())
    print("upcoming :", calendar.speech_upcoming())
    print("route    :", router.handle("what is up for today")[1])
    print("reminders:", Reminders().list_speech())
    print("tools    :", len(Tools().names()))
    # Read-only: parse a scheduling phrase without writing to the real calendar.
    from jarvis.nlp_time import extract_title, parse_when

    probe = "set an appointment on 12 November at 7:30"
    when = parse_when(probe)
    print("parse    :", extract_title(probe), "->", when.value if when else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
