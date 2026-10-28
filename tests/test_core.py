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
        self._yt = mock.patch("jarvis.skills.youtube.find_first", return_value="dQw4w9WgXcQ")
        self._yt.start()
        self.addCleanup(self._yt.stop)

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

    def test_play_opens_the_video_not_the_results(self) -> None:
        calls: list[str] = []
        with mock.patch("webbrowser.open", side_effect=lambda url, *a, **k: calls.append(url)):
            handled, reply = self.router.handle("play yellow by coldplay")
        self.assertTrue(handled)
        self.assertIn("Playing", reply)
        self.assertTrue(
            any("watch?v=" in url and "autoplay=1" in url for url in calls),
            f"expected a watch URL, got {calls}",
        )

    def test_search_on_youtube_does_not_claim_to_play(self) -> None:
        handled, reply = self.router.handle("search cats on youtube")
        self.assertTrue(handled)
        self.assertIn("Searching", reply)
        self.assertNotIn("Playing", reply)

    def test_compound_open_then_search(self) -> None:
        launched: list[str] = []
        sites: list[str] = []
        self.router.apps.launch_app = lambda q: launched.append(q) or (True, f"[launch {q}]")
        self.router.apps.open_site = lambda q: sites.append(q) or (True, f"[site {q}]")
        handled, reply = self.router.handle("open chrome and search youtube")
        self.assertTrue(handled)
        self.assertIn("[launch chrome]", reply)
        self.assertEqual(launched, ["chrome"], "only the app clause should launch an app")
        self.assertEqual([s.lower() for s in sites], ["youtube"])

    def test_open_app_search_and_play_without_and_before_search(self) -> None:
        launched: list[str] = []
        sites: list[str] = []
        played: list[str] = []
        self.router.apps.launch_app = lambda q: launched.append(q) or (True, f"[launch {q}]")
        self.router.apps.open_site = lambda q: sites.append(q) or (True, f"[site {q}]")
        with mock.patch("jarvis.skills.youtube.play",
                        side_effect=lambda q: played.append(q) or (True, f"[play {q}]")):
            handled, reply = self.router.handle(
                "open chrome search youtube and play bud for bud")

        self.assertTrue(handled)
        self.assertEqual(launched, ["chrome"])
        self.assertEqual(sites, ["youtube"])
        self.assertEqual(played, ["bud for bud"])
        self.assertIn("[play bud for bud]", reply)

    def test_open_youtube_search_with_possessive_misheard_as_such(self) -> None:
        calls: list[tuple[str, str, bool]] = []
        with mock.patch.object(
                self.router, "_site_search",
                side_effect=lambda site, query, play=False:
                    calls.append((site, query, play)) or "Searching YouTube."):
            handled, reply = self.router.handle("open youtube's such bonful band")

        self.assertTrue(handled)
        self.assertEqual(calls, [("youtube", "bonful band", False)])
        self.assertIn("Searching", reply)

    def test_autocorrects_misheard_search_word_only_in_youtube_command(self) -> None:
        calls: list[tuple[str, str, bool]] = []
        with mock.patch.object(
                self.router, "_site_search",
                side_effect=lambda site, query, play=False:
                    calls.append((site, query, play)) or "Searching YouTube."):
            handled, reply = self.router.handle("open youtube's serch bonful band")

        self.assertTrue(handled)
        self.assertEqual(calls, [("youtube", "bonful band", False)])
        self.assertIn("Searching", reply)

    def test_open_youtube_play_without_space_after_play(self) -> None:
        calls: list[tuple[str, str, bool]] = []
        with mock.patch.object(
                self.router, "_site_search",
                side_effect=lambda site, query, play=False:
                    calls.append((site, query, play)) or "Playing on YouTube."):
            handled, reply = self.router.handle("open youtube playbud fubud")

        self.assertTrue(handled)
        self.assertEqual(calls, [("youtube", "bud fubud", True)])
        self.assertIn("Playing", reply)

    def test_misheard_search_is_repaired(self) -> None:
        launched: list[str] = []
        sites: list[str] = []
        self.router.apps.launch_app = lambda q: launched.append(q) or (True, f"[launch {q}]")
        self.router.apps.open_site = lambda q: sites.append(q) or (True, f"[site {q}]")
        handled, reply = self.router.handle("open chrome and such YouTube")
        self.assertTrue(handled)
        self.assertIn("[launch chrome]", reply)
        self.assertEqual(launched, ["chrome"])
        self.assertEqual([s.lower() for s in sites], ["youtube"])
        self.assertNotIn("couldn't find", reply.lower())

    def test_compound_bare_app_after_and(self) -> None:
        launched: list[str] = []
        self.router.apps.names = lambda: ["google chrome", "notepad"]
        self.router.apps.launch_app = lambda q: launched.append(q) or (True, f"[launch {q}]")
        handled, _reply = self.router.handle("open chrome and notepad")
        self.assertTrue(handled)
        self.assertEqual(launched, ["chrome", "notepad"])

    def test_partial_compound_still_reports_the_action(self) -> None:
        launched: list[str] = []
        self.router.apps.launch_app = lambda q: launched.append(q) or (True, f"[launch {q}]")
        handled, reply = self.router.handle("open chrome and flibbertigibbet")
        self.assertTrue(handled)
        self.assertIn("[launch chrome]", reply)
        self.assertIn("didn't catch", reply.lower())
        self.assertEqual(launched, ["chrome"], "the whole sentence must not be launched as an app")

    def test_accent_guesses_the_intended_app_or_site(self) -> None:
        launched: list[str] = []
        self.router.apps.names = lambda: ["google chrome", "notepad", "calculator"]
        self.router.apps.launch_app = (
            lambda q: launched.append(q) or (True, f"[launch {q}]")
            if q in ("google chrome", "notepad")
            else (False, f"I couldn't find {q}")
        )
        handled, reply = self.router.handle("open crhome")
        self.assertTrue(handled)
        self.assertEqual(launched, ["google chrome"])
        self.assertIn("took that as opening chrome", reply.lower())
        handled, reply = self.router.handle("open youtub")
        self.assertTrue(handled)
        self.assertIn("youtube", reply.lower())

    def test_repair_leaves_ordinary_speech_alone(self) -> None:
        from jarvis.router import _repair_accent

        self.assertEqual(_repair_accent("what app is this"), "what app is this")
        self.assertEqual(_repair_accent("such a good day"), "such a good day")

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


class TestGmailSetup(unittest.TestCase):
    def test_save_credentials_preserves_other_env_lines(self) -> None:
        import pathlib
        import tempfile

        from jarvis import webui

        root = pathlib.Path(tempfile.mkdtemp())
        (root / ".env").write_text("GEMINI_API_KEY=keepme\n", encoding="utf-8")
        old_root = webui.ROOT
        webui.ROOT = root
        try:
            ok, message = webui._save_gmail_creds("me@gmail.com", "abcd efgh ijkl mnop")
            self.assertTrue(ok, message)
            text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("GEMINI_API_KEY=keepme", text)
            self.assertIn("GMAIL_ADDRESS=me@gmail.com", text)
            self.assertIn("GMAIL_APP_PASSWORD=abcdefghijklmnop", text)
        finally:
            webui.ROOT = old_root
            import os

            os.environ.pop("GMAIL_ADDRESS", None)
            os.environ.pop("GMAIL_APP_PASSWORD", None)

    def test_invalid_address_is_rejected_without_writing(self) -> None:
        import pathlib
        import tempfile

        from jarvis import webui

        root = pathlib.Path(tempfile.mkdtemp())
        (root / ".env").write_text("KEEP=1\n", encoding="utf-8")
        old_root = webui.ROOT
        webui.ROOT = root
        try:
            ok, _message = webui._save_gmail_creds("not-an-address", "abcdefgh")
            self.assertFalse(ok)
            self.assertNotIn("GMAIL_ADDRESS", (root / ".env").read_text(encoding="utf-8"))
        finally:
            webui.ROOT = old_root


class TestEmailDraft(unittest.TestCase):
    def test_reply_is_threaded_and_not_sent(self) -> None:
        from jarvis.email_client import build_draft

        original = {
            "from": "Mum <mum@example.com>",
            "from_addr": "mum@example.com",
            "subject": "Dinner",
            "message_id": "<abc@example.com>",
            "references": "",
        }
        message = build_draft(original, "I'll be there at seven.")
        self.assertEqual(message["To"], "mum@example.com")
        self.assertEqual(message["Subject"], "Re: Dinner")
        self.assertEqual(message["In-Reply-To"], "<abc@example.com>")
        self.assertIn("I'll be there at seven.", message.get_content())


class TestPulse(unittest.TestCase):
    def test_snapshot_shape(self) -> None:
        from jarvis.skills import pulse

        snap = pulse.snapshot()
        for key in ("battery", "cpu", "memory", "disk", "uptime", "windows", "recent"):
            self.assertIn(key, snap)
        self.assertIn("percent", snap["cpu"])
        self.assertIsInstance(snap["recent"], list)


class TestYouTubeSearch(unittest.TestCase):
    def test_first_video_id_from_nested_json(self) -> None:
        from jarvis.skills.youtube import _first_video_id

        payload = {
            "contents": [
                {"adSlotRenderer": {"x": 1}},
                {"itemSectionRenderer": {"contents": [
                    {"videoRenderer": {"videoId": "abc12345678"}},
                    {"videoRenderer": {"videoId": "zzz99999999"}},
                ]}},
            ]
        }
        self.assertEqual(_first_video_id(payload), "abc12345678")
        self.assertIsNone(_first_video_id({"contents": []}))


class TestAvatarAndVoiceConfig(unittest.TestCase):
    def test_saved_orb_setting_is_respected_by_default_launch(self) -> None:
        from jarvis import main

        with mock.patch.object(main, "CONFIG") as config, \
                mock.patch.object(main, "cmd_run", return_value=0) as cmd_run:
            config.show_orb = False
            self.assertEqual(main.main([]), 0)
        cmd_run.assert_called_once_with(show_orb=False)

    def test_no_orb_flag_also_hides_orb_when_config_enables_it(self) -> None:
        from jarvis import main

        with mock.patch.object(main, "CONFIG") as config, \
                mock.patch.object(main, "cmd_run", return_value=0) as cmd_run:
            config.show_orb = True
            self.assertEqual(main.main(["--no-orb"]), 0)
        cmd_run.assert_called_once_with(show_orb=False)

    def test_hidden_orb_does_not_disable_startup_speech(self) -> None:
        from jarvis.app import Jarvis

        with mock.patch.multiple(
                "jarvis.app",
                EchoGuard=mock.DEFAULT,
                EnergyVAD=mock.DEFAULT,
                Gemini=mock.DEFAULT,
                Ears=mock.DEFAULT,
                Brain=mock.DEFAULT,
                Router=mock.DEFAULT,
                make_tts=mock.DEFAULT,
                Player=mock.DEFAULT,
                Speaker=mock.DEFAULT):
            jarvis = Jarvis(show_orb=False)

        self.assertFalse(jarvis.show_orb)
        self.assertTrue(jarvis.greeting)

    def test_fish_uses_configured_api_key_for_authorization(self) -> None:
        from jarvis.tts.fish import FishTTS

        fish = FishTTS(api_key="test-key", voice_id="test-voice")
        self.assertTrue(fish.session.headers["Authorization"].startswith("Bearer "))
        self.assertTrue(fish.session.headers["Authorization"].endswith("test-key"))

    def test_fish_requires_credentials_when_constructed(self) -> None:
        from jarvis.tts.fish import FishTTS, TTSError

        with mock.patch("jarvis.tts.fish.CONFIG") as config:
            config.fish_api_key = ""
            config.fish_voice_id = ""
            with self.assertRaisesRegex(TTSError, "FISH_API_KEY"):
                FishTTS()

    def test_fish_failure_uses_sapi_and_logs_the_reason(self) -> None:
        import numpy as np

        from jarvis.tts.factory import _BudgetedTTS

        class Fish:
            def synthesize_pcm(self, _text, _rate):
                raise RuntimeError("service unavailable")

        class Sapi:
            def synthesize_pcm(self, _text, _rate):
                return np.array([0.25], dtype=np.float32)

        engine = _BudgetedTTS("fish", "sapi", 1000)
        with mock.patch.object(engine, "_get", side_effect=lambda name: (
                Fish() if name == "fish" else Sapi())):
            with self.assertLogs("tts.factory", level="WARNING") as logs:
                audio = engine.synthesize_pcm("hello", 24000)

        self.assertEqual(audio.tolist(), [0.25])
        self.assertIn("Fish voice failed", logs.output[0])

    def test_successful_fish_audio_preserves_numpy_pcm(self) -> None:
        import numpy as np

        from jarvis.tts.factory import _BudgetedTTS

        expected = np.array([0.1, 0.2], dtype=np.float32)

        class Fish:
            def synthesize_pcm(self, _text, _rate):
                return expected

        engine = _BudgetedTTS("fish", "sapi", 1000)
        with mock.patch.object(engine, "_get", side_effect=lambda _name: Fish()):
            actual = engine.synthesize_pcm("hello", 24000)

        np.testing.assert_array_equal(actual, expected)

    def test_sapi_initializes_com_on_the_synthesis_thread(self) -> None:
        import types

        from jarvis.tts.sapi import SapiTTS

        class Memory:
            class Format:
                Type = None

            @staticmethod
            def GetData():
                return b"\x00\x00"

        class Voice:
            AudioOutputStream = None

            @staticmethod
            def Speak(_text):
                return None

        comtypes = types.ModuleType("comtypes")
        comtypes.CoInitialize = mock.Mock()
        comtypes.CoUninitialize = mock.Mock()
        client = types.ModuleType("comtypes.client")
        client.CreateObject = mock.Mock(return_value=Memory())
        comtypes.client = client

        engine = SapiTTS()
        with mock.patch.dict(sys.modules, {"comtypes": comtypes, "comtypes.client": client}), \
                mock.patch.object(engine, "_ensure", return_value=Voice()):
            self.assertEqual(engine.synthesize("Testing"), b"\x00\x00")

        comtypes.CoInitialize.assert_called_once_with()
        comtypes.CoUninitialize.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
