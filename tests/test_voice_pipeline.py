from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from jarvis.ai import Gemini, GeminiError
from jarvis.audio.vad import EnergyVAD, Recorder
from jarvis.brain.llm import _is_quota_error
from jarvis.echo import EchoGuard
from jarvis.activity import ActivityLog
from jarvis.routines import RoutineStore
from jarvis.tools import Tool, ToolResult, Tools


RATE = 16_000
BLOCK = 160  # 10 ms, explicit in tests so timing is deterministic


def tone(amplitude: float, hz: float = 220.0) -> np.ndarray:
    t = np.arange(BLOCK, dtype=np.float32) / RATE
    return np.asarray(amplitude * 32767 * np.sin(2 * np.pi * hz * t), dtype=np.int16)


def feed(rec: Recorder, block: np.ndarray, count: int) -> bool:
    done = False
    for _ in range(count):
        done = rec.feed(block)
        if done:
            break
    return done


class TestRecorderEndpointing(unittest.TestCase):
    def make_recorder(self, **kwargs) -> Recorder:
        return Recorder(EnergyVAD(threshold=0.008, noise_mult=1.6, noise_ceiling=0.006),
                         [], block_ms=10, **kwargs)

    def test_long_sentence_is_captured_until_maximum(self) -> None:
        rec = self.make_recorder(max_s=2.0, start_timeout_ms=200)
        self.assertFalse(feed(rec, tone(0.03), 150))
        self.assertTrue(feed(rec, tone(0.03), 100))
        self.assertTrue(rec.usable)
        self.assertGreaterEqual(len(rec.audio()), 2_000)

    def test_short_pause_does_not_split_one_sentence(self) -> None:
        rec = self.make_recorder(end_silence_ms=500, max_s=5.0, start_timeout_ms=200)
        self.assertFalse(feed(rec, tone(0.03, 220), 30))
        self.assertFalse(feed(rec, tone(0.001, 0), 30))  # 300 ms natural pause
        self.assertFalse(feed(rec, tone(0.03, 330), 30))
        self.assertTrue(feed(rec, tone(0.001, 0), 60))  # endpoint after 500 ms
        self.assertTrue(rec.usable)
        self.assertGreater(len(rec.audio()), 900)
        self.assertGreater(rec.speech_ms, 400)

    def test_configured_endpoint_waits_for_a_real_pause(self) -> None:
        rec = self.make_recorder(end_silence_ms=1300, max_s=5.0, start_timeout_ms=200)
        self.assertFalse(feed(rec, tone(0.03), 30))
        self.assertFalse(feed(rec, tone(0.001, 0), 100))  # 1 s pause is not an endpoint
        self.assertFalse(feed(rec, tone(0.03, 330), 30))
        self.assertTrue(feed(rec, tone(0.001, 0), 130))

    def test_continuous_speech_ends_only_at_maximum(self) -> None:
        rec = self.make_recorder(max_s=1.0, start_timeout_ms=200)
        self.assertTrue(feed(rec, tone(0.03), 120))
        self.assertTrue(rec.usable)
        self.assertEqual(rec.elapsed_ms, 1_000)

    def test_background_noise_is_not_a_command(self) -> None:
        rec = self.make_recorder(start_timeout_ms=200, max_s=2.0)
        self.assertTrue(feed(rec, tone(0.001, 60), 30))
        self.assertFalse(rec.usable)


class TestEchoAndQuota(unittest.TestCase):
    def test_playback_echo_is_rejected_but_different_user_voice_is_kept(self) -> None:
        output = tone(0.2, 440).astype(np.float32)
        user = tone(0.2, 2200).astype(np.float32)
        guard = EchoGuard()
        for i in range(30):
            when = i * 0.02
            guard.note_output(output, RATE, 0.2, when=when)
            self.assertFalse(guard.is_user_speech(output, RATE, 0.2, when=when + 0.01))
        self.assertTrue(guard.is_user_speech(user, RATE, 0.2, when=0.61))
        self.assertGreaterEqual(guard.get_diagnostics()["threshold"], 0.25)

    def test_gemini_403_quota_skips_to_next_model(self) -> None:
        client = Gemini(api_key="test-key")
        client.generate = mock.Mock(side_effect=[
            GeminiError(403, "insufficient_quota"),
            {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]},
        ])
        text = client.transcribe(np.zeros(RATE, dtype=np.int16), RATE,
                                 model="first", fallback="second")
        self.assertEqual(text, "hello")
        self.assertEqual(client.generate.call_count, 2)

    def test_quota_errors_are_classified_without_logging_credentials(self) -> None:
        self.assertTrue(_is_quota_error(GeminiError(403, "insufficient_quota")))
        self.assertTrue(_is_quota_error(GeminiError(429, "rate limit")))
        self.assertFalse(_is_quota_error(RuntimeError("network reset")))


class TestPersonalAgentFeatures(unittest.TestCase):
    def test_router_reports_notepad_typing_only_from_verified_result(self) -> None:
        from jarvis.router import Router

        router = Router()
        with mock.patch("jarvis.router.screen_skill.type_in_notepad",
                        return_value=(False, "readback did not match")) as type_text:
            handled, reply = router.handle("type sample text into notepad")
        self.assertTrue(handled)
        self.assertIn("couldn't verify", reply)
        self.assertNotIn("Typed it", reply)
        type_text.assert_called_once_with("sample text")

    def test_tool_execution_returns_structured_status(self) -> None:
        tools = Tools()
        tools.add(Tool("verified", "", {"type": "object"}, lambda: ToolResult(True, "readback matched")))
        tools.add(Tool("failed", "", {"type": "object"}, lambda: ToolResult(False, "no window")))
        self.assertEqual(
            __import__("json").loads(tools.execute("verified", {})),
            {"ok": True, "detail": "readback matched"},
        )
        self.assertEqual(
            __import__("json").loads(tools.execute("failed", {})),
            {"ok": False, "detail": "no window"},
        )

    def test_find_file_only_reports_a_search_match_not_verified_access(self) -> None:
        tools = Tools()
        with mock.patch("jarvis.skills.files.find_first", return_value=r"C:\Users\Brian\Documents\note.txt"):
            result = __import__("json").loads(tools.execute("find_file", {"name": "note"}))
        self.assertTrue(result["ok"])
        self.assertIn("path in the search results", result["detail"])
        self.assertIn("not been verified", result["detail"])

    def test_activity_timeline_round_trip(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = ActivityLog(Path(tmp) / "events.jsonl")
            log.record("tool", "open_app", detail="Opened Notepad")
            rows = log.recent(5)
            self.assertEqual(rows[0]["kind"], "tool")
            self.assertEqual(rows[0]["summary"], "open_app")

    def test_routine_store_round_trip_and_execution_order(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            store = RoutineStore(Path(tmp) / "routines.json")
            store.save("Work Mode", ["open chrome", "open notepad"])
            seen = []
            replies = store.run("work mode", lambda command: seen.append(command) or "done")
            self.assertEqual(seen, ["open chrome", "open notepad"])
            self.assertEqual(replies, ["done", "done"])
            self.assertTrue(store.delete("work mode"))
            self.assertIsNone(store.get("work mode"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
