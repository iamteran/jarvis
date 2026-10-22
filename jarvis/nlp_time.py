"""Turning spoken dates and times into real datetimes.

"set an appointment on 12 October at 7:30", "remind me tomorrow at 6 pm",
"next Monday at 9" — this module parses those, so the assistant can keep a calendar
instead of asking the user to open Google Calendar.

Design rules:
* A date with no year takes the next occurrence in the future (12 October 2026 is past on
  22 October 2026, so it means 2027).
* A time already past today rolls to tomorrow for reminders, but stays today for events
  the user is explicitly recording (they may be logging something).
* Month names are matched case-insensitively, including common ASR spellings.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 2, "wed": 3, "thu": 4, "thur": 4, "thurs": 4,
    "fri": 5, "sat": 6, "sun": 6,
}

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


@dataclass
class When:
    value: dt.datetime
    has_date: bool
    has_time: bool

    def speech(self) -> str:
        return self.value.strftime("%A %d %B at %I:%M %p").replace(" 0", " ").lstrip("0")


def _month(name: str) -> int | None:
    return MONTHS.get(name.lower().rstrip("."))


def _fix_year(month: int, day: int, now: dt.datetime) -> dt.date:
    """12 October spoken on 22 October means next year."""
    year = now.year
    try:
        candidate = dt.date(year, month, day)
    except ValueError:
        return now.date()
    if candidate < now.date():
        try:
            candidate = dt.date(year + 1, month, day)
        except ValueError:
            return now.date()
    return candidate


def _parse_time(text: str) -> tuple[int, int] | None:
    lowered = text.lower()

    # 7:30, 19:20, 7.30
    match = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|a\.m\.|p\.m\.)?", lowered)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        meridiem = (match.group(3) or "").replace(".", "")
        return _to_24(hour, minute, meridiem)
    # 7 pm / 7pm
    match = re.search(r"\b(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)\b", lowered)
    if match:
        return _to_24(int(match.group(1)), 0, (match.group(2) or "").replace(".", ""))
    # "at 7" / "at seven"
    match = re.search(r"\bat\s+(\d{1,2}|" + "|".join(_NUM_WORDS) + r")\b", lowered)
    if match:
        raw = match.group(1)
        hour = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw, 0)
        if 0 <= hour <= 23:
            return (hour, 0)
    # "20 to 7" = 6:40, "quarter past 7" = 7:15
    match = re.search(r"\b(\d{1,2})\s+(?:to|past)\s+(\d{1,2})\b", lowered)
    if match:
        minutes, hour = int(match.group(1)), int(match.group(2))
        if minutes < 60 and 0 <= hour <= 23:
            if "to" in match.group(0):
                total = hour * 60 - minutes
                total %= 24 * 60
                return (total // 60, total % 60)
            total = hour * 60 + minutes
            return ((total // 60) % 24, total % 60)
    return None


def _to_24(hour: int, minute: int, meridiem: str) -> tuple[int, int]:
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and minute == 0 and 1 <= hour <= 6:
        # A bare "at 3" almost always means the afternoon; "at 3:30" is left alone
        # because it is genuinely ambiguous and the user said the minutes for a reason.
        hour += 12
    return (hour % 24, max(0, min(59, minute)))


def _parse_date(text: str, now: dt.datetime) -> dt.date | None:
    lowered = text.lower()

    if "day after tomorrow" in lowered:
        return now.date() + dt.timedelta(days=2)
    if "tomorrow" in lowered:
        return now.date() + dt.timedelta(days=1)
    if re.search(r"\btoday\b|\btonight\b", lowered):
        return now.date()
    if re.search(r"\bnext week\b", lowered):
        return now.date() + dt.timedelta(days=7)

    match = re.search(r"\bin\s+(\d{1,2})\s+(day|days|week|weeks)\b", lowered)
    if match:
        amount = int(match.group(1))
        return now.date() + dt.timedelta(days=amount * (7 if "week" in match.group(2) else 1))

    # ISO / numeric: 2026-10-12, 12/10, 12/10/2026
    match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lowered)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.search(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b", lowered)
    if match:
        day, month = int(match.group(1)), int(match.group(2))
        year = match.group(3)
        year = int(year) + (2000 if len(year) == 2 else 0) if year else None
        try:
            return dt.date(year or now.year, month, day)
        except ValueError:
            pass

    # "12 October", "12th of October", "october 12"
    month_pattern = "|".join(MONTHS)
    match = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_pattern})\b", lowered)
    if match:
        month = _month(match.group(2))
        if month:
            return _fix_year(month, int(match.group(1)), now)
    match = re.search(rf"\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", lowered)
    if match:
        month = _month(match.group(1))
        if month:
            return _fix_year(month, int(match.group(2)), now)

    # weekday names, with optional "next"
    for name, index in WEEKDAYS.items():
        if re.search(rf"\b(?:next\s+)?{name}\b", lowered):
            days_ahead = (index - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return now.date() + dt.timedelta(days=days_ahead)
    return None


def parse_when(text: str, now: dt.datetime | None = None,
               prefer_future: bool = True) -> When | None:
    """Parse a spoken datetime. Returns None if there is nothing date-like in the text."""
    now = now or dt.datetime.now()
    date = _parse_date(text, now)
    time_part = _parse_time(text)
    if date is None and time_part is None:
        return None
    if date is None:
        date = now.date()
    if time_part is None:
        time_part = (9, 0)          # date without a time: default to 9 AM
        has_time = False
    else:
        has_time = True
    value = dt.datetime.combine(date, dt.time(*time_part))
    if prefer_future and has_time and not _has_explicit_date(text) and value < now:
        value += dt.timedelta(days=1)   # "at 6 pm" said after 6 pm means tomorrow
    return When(value=value, has_date=_has_explicit_date(text), has_time=has_time)


def _has_explicit_date(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"\b(today|tonight|tomorrow|next week|day after tomorrow)\b", lowered):
        return True
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", lowered):
        return True
    if re.search(r"\b\d{1,2}[/.]\d{1,2}\b", lowered):
        return True
    month_pattern = "|".join(MONTHS)
    if re.search(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?({month_pattern})\b", lowered):
        return True
    if re.search(rf"\b({month_pattern})\s+\d{{1,2}}\b", lowered):
        return True
    if any(re.search(rf"\b(?:next\s+)?{name}\b", lowered) for name in WEEKDAYS):
        return True
    return bool(re.search(r"\bin\s+\d{1,2}\s+(day|days|week|weeks)\b", lowered))


_LEADING = re.compile(
    r"^(?:please\s+)?(?:would\s+you\s+)?(?:set|add|schedule|create|book|put|make|log|"
    r"remind\s+me\s+to|remind\s+me|reminder\s+to|reminder)\b"
    r"|^(?:an?|the)\s+(?:appointment|event|meeting|reminder)\b"
    r"|^(?:to|for|about|that|of)\b",
    re.I,
)

_MONTHS_RE = (r"jan\w*|feb\w*|mar\w*|apr\w*|may|jun\w*|jul\w*|aug\w*|sep\w*|oct\w*|"
              r"nov\w*|dec\w*")
_WEEKDAYS_RE = r"mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*"


def extract_title(text: str) -> str:
    """Strip the command, dates and times, leaving the thing being scheduled."""
    cleaned = text.strip(" .!?,")
    cleaned = _LEADING.sub(" ", cleaned)

    # Anything that looks like a date or a clock time.
    cleaned = re.sub(r"\b\d{4}-\d{1,2}-\d{1,2}\b", " ", cleaned)
    cleaned = re.sub(r"\b\d{1,2}[/.]\d{1,2}(?:[/.]\d{2,4})?\b", " ", cleaned)
    cleaned = re.sub(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS_RE})\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(rf"\b(?:{_MONTHS_RE})\s+\d{{1,2}}(?:st|nd|rd|th)?\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{1,2}[:.]\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)?\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{1,2}\s*(?:am|pm|a\.m\.|p\.m\.)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{1,2}\s+(?:to|past)\s+\d{1,2}\b", " ", cleaned, flags=re.I)

    # "at 9" / "by 9" — remove the preposition and the bare hour together.
    cleaned = re.sub(r"\b(?:at|by)\s+\d{1,2}\b", " ", cleaned, flags=re.I)

    # Spoken date words.
    cleaned = re.sub(r"\b(?:today|tonight|tomorrow|next|in|on|at|by)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(rf"\b(?:{_WEEKDAYS_RE})\b", " ", cleaned, flags=re.I)

    # Command nouns and filler.
    cleaned = re.sub(
        r"\b(?:appointment|appointments|event|events|meeting|meetings|reminder|reminders|"
        r"calendar|schedule|an|a|the|of|for|to|me|please|that|about)\b",
        " ", cleaned, flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,-")
    if cleaned:
        return cleaned
    # Nothing but command nouns were said ("set a meeting for Monday"): keep the noun.
    for word in ("meeting", "appointment", "event", "reminder", "call", "task"):
        if word in text.lower():
            return word.capitalize()
    return "Appointment"


if __name__ == "__main__":  # tiny manual check
    import sys

    now = dt.datetime.now()
    for probe in (
        "set an appointment on 12 October at 7:30",
        "remind me to call mum tomorrow at 6 pm",
        "meeting next monday at 9",
        "dentist on 2026-11-03 at 14:00",
        "lunch at 13:00",
    ):
        parsed = parse_when(probe, now)
        print(f"{probe!r:52} -> {parsed.value if parsed else None} | title={extract_title(probe)!r}")
