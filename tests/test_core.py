"""Core regression tests for Jarvis.

Run:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
These exercise the logic that must never break: date parsing, calendar, routing,
confirmation safety, memory, reminders, transcript cleaning and the Whisper
normaliser bug that once made every quiet utterance transcribe as empty.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import jarvis.calendar_store as calendar_store  # noqa: E402
import jarvis.memory as memory_mod  # noqa: E402
import jarvis.reminders as reminders_mod  # noqa: E402
from jarvis import safety  # noqa: E402
from jarvis.calendar_store import Calendar  # noqa: E402
from jarvis.ears import clean_transcript  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.nlp_time import extract_title, parse_when  # noqa: E402
from jarvis.reminders import Reminders, parse_reminder  # noqa: E402
from jarvis.router import Router  # noqa: E402
from jarvis.tools import Tools  # noqa: E402


class TempDBTest(unittest.TestCase):
    """Every test gets a private SQLite file, so real memory is never touched."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmp.name) / "test.db"
        cls._saved = {
            calendar_store: calendar_store.DB_PATH,
            memory_mod: memory_mod.DB_PATH,
            reminders_mod: reminders_mod.DB_PATH,
        }
        for module in cls._saved:
            module.DB_PATH = cls.db_path

    @classmethod
    def tearDownClass(cls) -> None:
        for module, old in cls._saved.items():
            module.DB_PATH = old
        cls._tmp.cleanup()


class TestTimeParsing(unittest.TestCase):
    def test_explicit_date_and_time(self) -> None:
        when = parse_when("set an appointment on 12 October at 7:30",
                          now=dt.datetime(2026, 10, 22, 9, 0))
        self.assertIsNotNone(when)
        self.assertEqual(when.value.month, 10)
        self.assertEqual(when.value.day, 12)
        self.assertEqual((when.value.hour, when.value.minute), (7, 30))
        self.assertEqual(when.value.year, 2027)   # 12 Oct already past on 22 Oct 2026

    def test_tomorrow_evening(self) -> None:
        when = parse_when("remind me to call mum tomorrow at 6 pm",
                          now=dt.datetime(2026, 10, 22, 9, 0))
        self.assertIsNotNone(when)
        self.assertEqual(when.value.hour, 18)
        self.assertEqual(when.value.day, 23)

    def test_next_weekday(self) -> None:
        when = parse_when("meeting next monday at 9",
                          now=dt.datetime(2026, 10, 22, 9, 0))
        self.assertIsNotNone(when)
        self.assertEqual(when.value.weekday(), 0)
        self.assertEqual(when.value.hour, 9)

    def test_24h_and_no_date(self) -> None:
        when = parse_when("lunch at 13:00", now=dt.datetime(2026, 10, 22, 9, 0))
        self.assertIsNotNone(when)
        self.assertEqual(when.value.hour, 13)

    def test_not_a_datetime(self) -> None:
        self.assertIsNone(parse_when("what is the capital of Kenya"))
        self.assertIsNone(parse_when("turn the volume up"))

    def test_title_extraction(self) -> None:
        self.assertEqual(
            extract_title("remind me to call mum tomorrow at 6 pm"), "call mum")
        self.assertIn(extract_title("set an appointment on 12 October at 7:30").lower(),
                      {"appointment"})


class TestCalendar(TempDBTest):
    def test_add_and_read(self) -> None:
        calendar = Calendar()
        start = dt.datetime.now().replace(microsecond=0) + dt.timedelta(hours=1)
        reply = calendar.add("Dentist", start)
        self.assertIn("Dentist", reply)
        self.assertTrue(any("Dentist" in row["title"] for row in calendar.on_day(start.date())))
        self.assertIn("Dentist", calendar.speech_today() if start.date() == dt.date.today()
                      else calendar.speech_upcoming())

    def test_due_soon_and_announce(self) -> None:
        calendar = Calendar()
        start = dt.datetime.now() + dt.timedelta(seconds=30)
        calendar.add("Standup", start)
        due = [row for row in calendar.due_soon(window_s=120) if row["title"] == "Standup"]
        self.assertTrue(due)
        calendar.mark_announced(due[0]["id"])
        self.assertFalse([row for row in calendar.due_soon(window_s=120)
                          if row["title"] == "Standup"])


class TestRouter(TempDBTest):
    def setUp(self) -> None:
        self.router = Router()
        self.router.apps.launch_app = lambda q: (True, f"[launch {q}]")
        self.router.apps.open_site = lambda q: (True, f"[site {q}]")
        self._browser = mock.patch("webbrowser.open")
        self._browser.start()
        self.addCleanup(self._browser.stop)

    def test_schedule_creation_and_query(self) -> None:
        handled, reply = self.router.handle("set an appointment on 12 October at 7:30")
        self.assertTrue(handled)
        self.assertIn("October", reply)
        handled, reply = self.router.handle("what's up for today")
        self.assertTrue(handled)
        self.assertIn("scheduled", reply.lower())

    def test_timed_reminder(self) -> None:
        handled, reply = self.router.handle("remind me to call mum at 6 pm")
        self.assertTrue(handled)
        self.assertIn("remind", reply.lower())

    def test_compound_open_and_play(self) -> None:
        handled, reply = self.router.handle("open youtube and play any song")
        self.assertTrue(handled)
        self.assertIn("YouTube", reply)

    def test_plain_commands(self) -> None:
        for phrase in ("open notepad", "open chrome", "what time is it",
                       "open the desktop", "list windows"):
            handled, _ = self.router.handle(phrase)
            self.assertTrue(handled, f"{phrase!r} should be handled")
        self.assertFalse(self.router.handle("tell me a joke")[0])

    def test_delete_requires_confirmation_flow(self) -> None:
        handled, reply = self.router.handle("delete a file that does not exist anywhere")
        self.assertTrue(handled)
        self.assertIn("can't find", reply.lower())

    # --- regressions from the 22 Oct morning session ---
    def test_question_never_creates_an_event(self) -> None:
        before = len(Calendar().upcoming(limit=100))
        self.router.handle("Have you set the reminder of 12th of November?")
        self.assertEqual(before, len(Calendar().upcoming(limit=100)),
                         "a question must never become a calendar entry")

    def test_reminder_phrase_does_not_become_event(self) -> None:
        before = len(Calendar().upcoming(limit=100))
        handled, reply = self.router.handle("Set a reminder of 12th November.")
        self.assertEqual(before, len(Calendar().upcoming(limit=100)),
                         "'reminder' must not be filed as an appointment")
        self.assertTrue(handled)
        self.assertIn("remind", reply.lower())

    def test_duplicate_event_is_ignored(self) -> None:
        first = self.router.handle("set a meeting with the bank on 12 November at 7:30")[1]
        second = self.router.handle("set a meeting with the bank on 12 November at 7:30")[1]
        self.assertIn("already", second.lower(), f"first={first!r} second={second!r}")


class TestSafety(unittest.TestCase):
    def test_yes_runs_no_cancels(self) -> None:
        ran: list[str] = []
        reply = safety.request("delete x", "It is on the desktop.", lambda: ran.append("y") or "done")
        self.assertIn("Say yes", reply)
        self.assertTrue(safety.has_pending())
        self.assertIsNone(safety.interpret("maybe later"))
        self.assertTrue(safety.interpret("Yes please."))
        self.assertEqual(safety.resolve(True), "done")
        self.assertEqual(ran, ["y"])
        self.assertFalse(safety.has_pending())

    def test_decline(self) -> None:
        safety.request("shut down", "Now.", lambda: "done")
        self.assertFalse(safety.interpret("No, don't."))
        self.assertIn("Cancelled", safety.resolve(False))


class TestMemoryAndReminders(TempDBTest):
    def test_memory_roundtrip(self) -> None:
        memory = Memory()
        memory.remember("prefers tea over coffee")
        self.assertIn("tea", " ".join(memory.recall("coffee")).lower())
        self.assertIn("tea", memory.context().lower())

    def test_reminder_minutes_parse(self) -> None:
        parsed = parse_reminder("remind me to stretch in 20 minutes")
        self.assertIsNotNone(parsed)
        text, minutes = parsed
        self.assertEqual(text, "stretch")
        self.assertAlmostEqual(minutes, 20.0)

    def test_reminder_lifecycle(self) -> None:
        reminders = Reminders()
        reminders.cancel_all()
        reminders.add_at("test reminder", dt.datetime.now().timestamp() - 1)
        self.assertTrue(reminders.due())
        row = reminders.due()[0]
        reminders.mark_spoken(row["id"])
        self.assertFalse([r for r in reminders.due() if r["id"] == row["id"]])


class TestTranscriptsAndAudio(unittest.TestCase):
    def test_hallucinations_dropped(self) -> None:
        for junk in ("Thank you.", "Thanks for watching!", "Bye.", "No speech detected. Output nothing."):
            self.assertEqual(clean_transcript(junk), "", junk)
        self.assertEqual(clean_transcript("open notepad"), "open notepad")

    def test_doubled_phrase_collapsed(self) -> None:
        self.assertEqual(clean_transcript("open the desktop, open the desktop."),
                         "open the desktop.")

    def test_normalise_int16_range(self) -> None:
        import numpy as np

        from jarvis.asr import normalise

        samples = np.array([1000, -3000, 0, 2000], dtype=np.int16)
        result = normalise(samples)
        self.assertLessEqual(float(np.max(np.abs(result))), 1.0)
        self.assertGreater(float(np.max(np.abs(result))), 0.5)


class TestToolbelt(unittest.TestCase):
    def test_tools_registered(self) -> None:
        tools = Tools()
        names = tools.names()
        self.assertGreaterEqual(len(names), 33, f"got {len(names)}: {names}")
        for required in ("open_app", "look_at_screen", "add_appointment", "check_schedule",
                         "delete_file", "shutdown_pc", "read_file",
                         "get_weather", "forget_memory", "set_alias"):
            self.assertIn(required, names)

    def test_unknown_tool_is_survivable(self) -> None:
        result = Tools().execute("no_such_tool", {})
        self.assertIn("Unknown tool", result)


class TestSpeakerCleaning(unittest.TestCase):
    def test_emoji_stripped(self) -> None:
        from jarvis.tts.speaker import clean_for_speech
        self.assertEqual(clean_for_speech("Hello! 😊"), "Hello!")

    def test_percent_expanded(self) -> None:
        from jarvis.tts.speaker import clean_for_speech
        result = clean_for_speech("Volume is 75%")
        self.assertIn("percent", result)
        self.assertNotIn("%", result)

    def test_markdown_stripped(self) -> None:
        from jarvis.tts.speaker import clean_for_speech
        result = clean_for_speech("**Bold** and *italic* and # Heading")
        self.assertNotIn("*", result)
        self.assertNotIn("#", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
