"""The fast path: deterministic, offline command handling that skips the LLM entirely.

Why it exists: on a slow connection a cloud round trip costs 10+ seconds. "Open Chrome",
"volume up", "lock the screen" and "what time is it" have exact answers, so they are
answered locally in well under a second — and they still work when the network is down.

`Router.handle()` returns (handled, reply). Reply is what Jarvis should say.
"""
from __future__ import annotations

import logging
import re

from .calendar_store import Calendar
from .nlp_time import extract_title, parse_when
from .reminders import Reminders, parse_reminder
from .skills import apps as apps_skill
from .skills import files as files_skill
from .skills import screen as screen_skill
from .skills import system as system_skill

log = logging.getLogger("router")

DELETE_RE = re.compile(r"^(?:delete|remove|trash)\s+(?:the\s+)?(?:file\s+)?(?P<path>.+?)\s*$", re.I)
REMINDER_LIST_RE = re.compile(r"\b(?:what are my reminders|list (?:my )?reminders|my reminders)\b", re.I)
REMINDER_CANCEL_RE = re.compile(r"\b(?:cancel (?:my |all )?reminders|clear (?:my )?reminders)\b", re.I)

SHOW_DESKTOP_RE = re.compile(r"^(?:show|open|go to)\s*(?:the\s+)?desktop\b.*$", re.I)
MINIMISE_RE = re.compile(r"^(?:minimi[sz]e|hide)\s+(?:everything|all|all windows|the windows?|everything else)\b.*$", re.I)
CLICK_RE = re.compile(r"^click\s+(?:on\s+)?(?:the\s+)?(?P<name>.+?)\s*(?:button|link|tab|item)?\s*$", re.I)
TYPE_RE = re.compile(r"^(?:type|write|enter)\s*[:\s]\s*(?P<text>.+)$", re.I)
SWITCH_RE = re.compile(r"^(?:switch to|bring up|focus|activate|go to window)\s+(?P<win>.+?)\s*$", re.I)
READ_SCREEN_RE = re.compile(r"^(?:what(?:'s| is) on (?:the )?screen|read (?:the )?(?:screen|window)|what does (?:this|the) window say)\b", re.I)
VISION_RE = re.compile(
    r"\b(?:look at (?:my |the )?screen|describe (?:my |the )?screen|what does (?:this|the) error"
    r"|what(?:'s| is) (?:this|the) error|do you see (?:my|the) screen|see what(?:'s| is) on my screen"
    r"|what am i looking at)\b", re.I)
LIST_WINDOWS_RE = re.compile(r"^(?:what windows|list windows|which windows)\b.*$", re.I)
BRIEFING_RE = re.compile(r"^(?:brief me|what(?:'s| is) my status|status report|good (?:morning|afternoon|evening))\b.*$", re.I)

# --- calendar ---
SCHEDULE_TODAY_RE = re.compile(
    r"\b(?:what(?:'s| is) (?:up )?(?:for )?today|what(?:'s| is) on today"
    r"|my (?:schedule|calendar|day)|anything (?:on |scheduled )?today"
    r"|what do i have (?:today|on)|what(?:'s| is) happening today)\b", re.I)
UPCOMING_RE = re.compile(
    r"\b(?:what(?:'s| is) (?:coming up|next)|next (?:appointment|event|meeting)"
    r"|upcoming (?:appointments|events|meetings)|my next appointment)\b", re.I)
SCHEDULING_NOUN_RE = re.compile(r"\b(?:appointment|event|meeting|reminder|calendar entry)\b", re.I)
REMIND_ME_RE = re.compile(r"^(?:please\s+)?remind me\s+(?:to\s+|about\s+)?(?P<rest>.+)$", re.I)
OPEN_CALENDAR_RE = re.compile(
    r"^(?:open|show|go to)\s+(?:my\s+|the\s+)?(?:calendar|schedule)\b.*$", re.I)
# Utterances that are questions, not instructions. Never create anything from these.
QUESTION_RE = re.compile(
    r"^(?:have|has|had|did|do|does|is|are|was|were|can|could|will|would|should|shall|"
    r"what|when|where|who|why|how|which|whose|am)\b", re.I)
_GENERIC_TITLES = {"appointment", "event", "meeting", "reminder", "that", "it", ""}


def _is_generic_title(title: str) -> bool:
    """True when nothing meaningful was said about what is being scheduled."""
    return title.strip().lower().rstrip(".") in _GENERIC_TITLES

# Compound "open X and search/play Y" — the most common way people actually ask.
SITE_SEARCH_RE = re.compile(
    r"^(?:open|go to|launch)\s+(?P<site>youtube| google|chrome)\s+and\s+"
    r"(?:search|play|look up|find)\s+(?:for\s+)?(?P<query>.+?)\s*$", re.I)
PLAY_RE = re.compile(r"^(?:play|put on|start playing)\s+(?P<query>.+?)(?:\s+on youtube)?\s*$", re.I)
SEARCH_ON_RE = re.compile(
    r"^(?:search|look up|find)\s+(?:for\s+)?(?P<query>.+?)\s+on\s+(?P<site>youtube|google)\s*$", re.I)

OPEN_RE = re.compile(r"^(?:open|launch|start|run|fire up)\s+(?P<target>.+?)\s*(?:please)?$", re.I)
GOTO_RE = re.compile(r"^(?:go to|visit|browse|navigate to)\s+(?P<target>.+?)\s*(?:please)?$", re.I)
SEARCH_RE = re.compile(r"^(?:search(?: for)?|google|look up)\s+(?P<q>.+?)\s*(?:please)?$", re.I)

SITE_HINTS = ("dot com", ".com", ".co.ke", ".org", ".net", " website", " site", "web")

VOLUME_UP_RE = re.compile(r"\b(?:volume|sound)\b.*\b(?:up|raise|louder|increase|higher)\b|\b(?:louder|turn it up|turn up)\b", re.I)
VOLUME_DOWN_RE = re.compile(r"\b(?:volume|sound)\b.*\b(?:down|lower|quieter|decrease|softer)\b|\b(?:quieter|turn it down|turn down)\b", re.I)
SET_VOLUME_RE = re.compile(r"\b(?:set|change|put)?\s*(?:the\s+)?(?:volume|sound)\b[^0-9]{0,12}(\d{1,3})\s*(?:%|percent)?", re.I)
VOLUME_GET_RE = re.compile(r"\b(?:what(?:'s| is) the volume|volume level|how loud)\b", re.I)
MUTE_RE = re.compile(r"\b(?:mute|silence)\b(?!\s+yourself)", re.I)
UNDO_RE = re.compile(r"^(?:undo|undo that|undo it|take that back|revert|put it back)\b", re.I)
REMEMBER_RE = re.compile(r"^(?:remember|note down|make a note|don't forget)\b(?:\s+that)?\s+(?P<fact>.+)$", re.I)
RECALL_RE = re.compile(r"^(?:what do you (?:remember|know about me)|what have you remembered|recall)\b.*$", re.I)
LOCK_RE = re.compile(r"\block\b.{0,20}\b(?:screen|computer|pc|laptop|workstation)\b", re.I)
TIME_RE = re.compile(r"\b(?:what(?:'s| is)? the time|what time is it|time is it)\b", re.I)
DATE_RE = re.compile(r"\b(?:what(?:'s| is)? (?:the )?(?:date|day)|what day is it|today'?s date)\b", re.I)
MEDIA_RE = re.compile(r"\b(?:pause|resume|play)(?:\s+the)?\s*(?:music|song|video|playback)?\s*$", re.I)
WEATHER_RE = re.compile(
    r"\b(?:what(?:'s| is) the weather|weather(?: (?:in|for|at)\s+(?P<city>[\w\s]+?))?|how(?:'s| is) it outside|is it raining)\b",
    re.I,
)
FORGET_RE = re.compile(r"^(?:forget|stop remembering|delete from memory)\b(?:\s+that)?\s+(?P<what>.+)$", re.I)
ALIAS_RE = re.compile(r"^(?:remember|when i say|call)\s+(?P<name>.+?)\s+(?:means?|refer(?:s)? to|is)\s+(?P<target>.+)$", re.I)
DASHBOARD_RE = re.compile(r"\b(?:open|show|launch)?\s*(?:the\s+)?dashboard\b|\bopen\s+jarvis\s+ui\b", re.I)


class Router:
    def __init__(self) -> None:
        self.apps = apps_skill.AppLauncher()
        self._warmed = False

    def warm(self) -> None:
        """Build the app index ahead of the first command (called in the background)."""
        if self._warmed:
            return
        try:
            self.apps.index()
        except Exception as exc:  # noqa: BLE001
            log.debug("app index warm-up failed: %s", exc)
        self._warmed = True

    # ------------------------------------------------------------------ public
    def handle(self, text: str) -> tuple[bool, str]:
        raw = (text or "").strip()
        if not raw:
            return False, ""
        clean = raw.rstrip(" .!?")

        # ---- calendar (before "open ...", which would treat phrases as app names) ----
        if SCHEDULE_TODAY_RE.search(clean):
            return True, Calendar().speech_today()
        if UPCOMING_RE.search(clean):
            return True, Calendar().speech_upcoming()

        # A question is never a command. "Have you set the reminder?" used to be filed as
        # an appointment because it contained a date — that is exactly the sort of mistake
        # that makes an assistant untrustworthy.
        is_question = bool(QUESTION_RE.match(clean))

        # "remind me to call mum at 6 pm" -> a timed reminder, not minutes-from-now
        match = REMIND_ME_RE.match(clean)
        if match:
            when = parse_when(match.group("rest"))
            if when is not None:
                title = extract_title(clean)
                if _is_generic_title(title):
                    return True, (f"What should I remind you about on "
                                  f"{when.value.strftime('%A %d %B at %I:%M %p').replace(' 0', ' ').lstrip('0')}, sir?")
                return True, Reminders().add_at(title, when.value.timestamp())

        if not is_question:
            when = parse_when(clean)
            if when is not None:
                lowered = clean.lower()
                mentions_reminder = bool(re.search(r"\bremind(?:er)?s?\b", lowered))
                looks_like_event = (
                    SCHEDULING_NOUN_RE.search(clean) is not None
                    or lowered.startswith(("schedule", "book"))
                    or (when.has_date and lowered.startswith("set"))
                )
                if mentions_reminder and not SCHEDULING_NOUN_RE.search(
                        re.sub(r"\bremind(?:er)?s?\b", " ", lowered)):
                    title = extract_title(clean)
                    if _is_generic_title(title):
                        return True, (f"What should I remind you about on "
                                      f"{when.value.strftime('%A %d %B at %I:%M %p').replace(' 0', ' ').lstrip('0')}, sir?")
                    return True, Reminders().add_at(title, when.value.timestamp())
                if looks_like_event:
                    title = extract_title(clean)
                    if _is_generic_title(title):
                        return True, (f"What is the appointment on "
                                      f"{when.value.strftime('%A %d %B at %I:%M %p').replace(' 0', ' ').lstrip('0')}, sir?")
                    return True, Calendar().add(title, when.value)

        if OPEN_CALENDAR_RE.match(clean):
            import webbrowser

            webbrowser.open("https://calendar.google.com")
            return True, "Opening your calendar, sir."

        # "open youtube and play any song" — open the site at the searched query.
        match = SITE_SEARCH_RE.match(clean)
        if match:
            return True, self._site_search(match.group("site"), match.group("query"))
        match = SEARCH_ON_RE.match(clean)
        if match:
            return True, self._site_search(match.group("site"), match.group("query"))
        match = PLAY_RE.match(clean)
        if match:
            query = match.group("query").strip()
            if query and len(query) > 1:
                return True, self._site_search("youtube", query)

        # Compound requests: "open chrome and search youtube" should do both.
        if re.search(r"\sand\s|\sthen\s|;", clean, re.I):
            pieces = [p.strip(" ,.") for p in re.split(r"\s+and(?:\s+then)?\s+|\s+then\s+|\s*;\s*", clean, flags=re.I) if p.strip(" ,.")]
            if len(pieces) > 1:
                replies: list[str] = []
                for piece in pieces[:3]:
                    handled_piece, reply_piece = self.handle(piece)
                    if not handled_piece:
                        replies = []
                        break
                    replies.append(reply_piece)
                if replies:
                    return True, " ".join(replies)

        # "open the desktop" is not an app — it means minimise everything.
        if SHOW_DESKTOP_RE.match(clean) or MINIMISE_RE.match(clean):
            return True, system_skill.show_desktop()

        match = OPEN_RE.match(clean)
        if match:
            target = match.group("target").strip()
            if self._looks_like_site(target):
                return True, self.apps.open_site(target)[1]
            ok, reply = self.apps.launch_app(target)
            return True, reply

        if BRIEFING_RE.match(clean):
            from .briefing import briefing_text

            return True, briefing_text()

        # --- acts on the visible desktop (accessibility tree, offline) ---
        if VISION_RE.search(clean):
            from .vision import describe

            return True, describe(clean)
        if READ_SCREEN_RE.search(clean):
            return True, screen_skill.read_window()
        if LIST_WINDOWS_RE.search(clean):
            titles = screen_skill.list_windows()
            if not titles:
                return True, "I can't see any open windows, sir."
            return True, "Open windows: " + "; ".join(titles[:8]) + "."
        match = SWITCH_RE.match(clean)
        if match:
            return True, screen_skill.activate(match.group("win").strip())
        match = CLICK_RE.match(clean)
        if match:
            return True, screen_skill.click_control(match.group("name").strip())
        match = TYPE_RE.match(clean)
        if match:
            return True, screen_skill.type_text(match.group("text").strip())

        match = GOTO_RE.match(clean)
        if match:
            return True, self.apps.open_site(match.group("target").strip())[1]

        match = SEARCH_RE.match(clean)
        if match:
            return True, self.apps.open_site(match.group("q").strip())[1]

        if UNDO_RE.match(clean):
            from .undo import pop_undo

            return True, pop_undo() or "There's nothing to undo, sir."

        match = REMEMBER_RE.match(clean)
        if match:
            from .memory import Memory

            Memory().remember(match.group("fact").strip())
            return True, "Noted, sir. I'll remember that."
        if RECALL_RE.match(clean):
            from .memory import Memory

            return True, Memory().recall_speech()

        # --- reminders ---
        parsed = parse_reminder(clean)
        if parsed:
            text, minutes = parsed
            return True, Reminders().add(text, minutes)
        if REMINDER_LIST_RE.search(clean):
            return True, Reminders().list_speech()
        if REMINDER_CANCEL_RE.search(clean):
            count = Reminders().cancel_all()
            return True, (f"Cancelled {count} reminder{'s' if count != 1 else ''}, sir."
                          if count else "There were none pending, sir.")

        # --- deleting a file: spoken confirmation first ---
        match = DELETE_RE.match(clean)
        if match:
            target = match.group("path").strip()
            resolved = files_skill.resolve_path(target)
            if resolved:
                return True, files_skill.request_delete(resolved)
            return True, f"I can't find anything called {target}, sir."

        match = SET_VOLUME_RE.search(clean)
        if match:
            return True, system_skill.set_volume(int(match.group(1)))

        if VOLUME_GET_RE.search(clean):
            return True, system_skill.volume_report()
        if VOLUME_UP_RE.search(clean):
            return True, system_skill.volume_up()
        if VOLUME_DOWN_RE.search(clean):
            return True, system_skill.volume_down()
        if MUTE_RE.search(clean):
            return True, system_skill.mute()
        if LOCK_RE.search(clean):
            return True, system_skill.lock_screen()
        if TIME_RE.search(clean):
            return True, system_skill.now_speech()
        if DATE_RE.search(clean):
            return True, system_skill.date_speech()
        if MEDIA_RE.match(clean):
            return True, system_skill.play_pause()

        # --- weather ---
        match = WEATHER_RE.search(clean)
        if match:
            city = (match.group("city") or "").strip()
            from .tools import Tools
            return True, Tools()._get_weather(city)

        # --- memory: forget a fact ---
        match = FORGET_RE.match(clean)
        if match:
            from .memory import Memory
            count = Memory().forget(match.group("what").strip())
            if count == 0:
                return True, "I don't seem to have that recorded, sir."
            return True, f"Forgotten, sir. Removed {count} fact{'s' if count != 1 else ''}."

        # --- teach an alias ---
        match = ALIAS_RE.match(clean)
        if match:
            from .memory import Memory
            name = match.group("name").strip()
            target = match.group("target").strip()
            Memory().set_alias(name, target)
            return True, f'Understood, sir. "{name}" will mean {target} from now on.'

        # --- dashboard ---
        if DASHBOARD_RE.search(clean):
            import pathlib, webbrowser
            dash = pathlib.Path(__file__).resolve().parent / "ui" / "dashboard.html"
            webbrowser.open(dash.as_uri())
            return True, "Opening the command interface in your browser, sir."

        return False, ""

    @staticmethod
    def _looks_like_site(target: str) -> bool:
        lowered = target.lower()
        if any(hint in lowered for hint in SITE_HINTS):
            return True
        return apps_skill.normalize(target) in apps_skill.SITE_ALIASES

    @staticmethod
    def _site_search(site: str, query: str) -> str:
        """Open YouTube/Google already showing results for `query`."""
        import webbrowser

        site = (site or "").strip().lower()
        query = (query or "").strip(" .!?")
        if not query:
            return "What should I look for, sir?"
        encoded = re.sub(r"\s+", "+", query)
        if "youtube" in site:
            webbrowser.open(f"https://www.youtube.com/results?search_query={encoded}")
            return f"Playing {query} on YouTube, sir."
        webbrowser.open(f"https://www.google.com/search?q={encoded}")
        return f"Searching for {query}, sir."
