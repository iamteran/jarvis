"""The orchestrator: hearing -> thinking -> speaking, with wake word, barge-in and
follow-up mode.

Threading model (deliberately simple):
  * audio thread  — the ONLY consumer of microphone blocks; runs wake word, endpointing,
                    barge-in and follow-up detection.
  * turn threads  — one per utterance: transcribe, think, speak. They never touch the
                    microphone, and a stale turn can never overwrite a newer state
                    because every state change is fenced by a turn id.
  * UI threads    — tkinter mainloop (orb) and pystray (tray).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from enum import Enum

import numpy as np

from .ai import Gemini, GeminiError
from .audio.capture import AudioHub
from .audio.vad import EnergyVAD, Recorder
from .audio.wake import WakeWord
from .brain.llm import Brain
from .config import CONFIG
from .ears import Ears
from .echo import EchoGuard
from .log import Timer
from .reminders import ReminderService
from .router import Router
from . import safety
from .tts.factory import make_tts
from .tts.fish import TTSError
from .tts.player import Player
from .tts.speaker import Speaker

log = logging.getLogger("jarvis")

STOP_WORDS = {
    "stop", "stop it", "never mind", "nevermind", "cancel", "that's all", "thats all",
    "that is all", "shut up", "be quiet", "quiet",
}

# "Jarvis …", "Hey Jarvis, …", "okay Jarvis …" — used when the wake model did not fire.
WAKE_NAME_RE = re.compile(
    r"^\s*(?:hey\s+|hi\s+|ok(?:ay)?\s+|oi\s+)?jarvis\b[\s,.:;!-]*(?P<command>.*)$", re.I
)


class State(str, Enum):
    BOOTING = "booting"
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    FOLLOWUP = "followup"
    MUTED = "muted"
    ERROR = "error"


class Jarvis:
    def __init__(self, show_orb: bool | None = None, greeting: bool | None = None) -> None:
        self.state = State.IDLE
        self.muted = False
        self.recorder: Recorder | None = None
        self._stop = threading.Event()
        self._turn = 0
        self._state_lock = threading.RLock()
        self._listen_lock = threading.Lock()
        self._followup_deadline = 0.0
        self._followup_hits = 0
        self._followup_ignore_until = 0.0
        self._barge_hits = 0
        self._idle_speech = 0
        self._asr_capture = False
        self._wake_check_busy = False
        self._wake_cooldown_until = 0.0
        self._wake_word_not_before = 0.0
        self._speaking_since = 0.0
        self._busy = False
        self.echo = EchoGuard()
        from .activity import ActivityLog
        self.activity = ActivityLog()

        self.hub: AudioHub | None = None
        self.vad = EnergyVAD()
        self.wake: WakeWord | None = None
        self.gemini = Gemini()
        self.ears = Ears()
        self.brain = Brain(self.gemini)
        self.router = Router()
        self.tts = make_tts()
        self._transcribe_lock = threading.Lock()
        self.player = Player()
        self.speaker = Speaker(self.tts, self.player)

        self.show_orb = CONFIG.show_orb if show_orb is None else show_orb
        self.greeting = True if greeting is None else greeting
        self.orb = None
        self.tray = None
        self.reminders: ReminderService | None = None
        self.telegram = None
        self._brain_lock = threading.Lock()
        self._started_ts = time.time()
        self._last_block_ts = time.time()
        self._last_turn_ts = 0.0
        self._watchdog: threading.Thread | None = None
        self._audio_thread: threading.Thread | None = None
        self._hotkeys = None

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> None:
        problems = CONFIG.validate()
        if problems:
            raise SystemExit("Jarvis cannot start:\n  - " + "\n  - ".join(problems))

        # UI first: the orb appears in ~1 s, then the heavy pieces boot behind it.
        if self.show_orb:
            from .ui.orb import Orb
            self.orb = Orb(on_listen=self.trigger_listen, on_mute=self.toggle_mute,
                           on_quit=lambda: self.shutdown("orb menu"))
        from .ui.tray import Tray
        self.tray = Tray(on_listen=self.trigger_listen, on_mute=self.toggle_mute,
                         on_quit=lambda: self.shutdown("tray menu"),
                         on_show=self.reveal_orb)
        self.tray.start()
        self._start_hotkeys()
        self._set_state(State.BOOTING)

        threading.Thread(target=self._boot, name="boot", daemon=True).start()

        if self.orb:
            self.orb.run()          # blocks on the tkinter mainloop
        else:
            while not self._stop.wait(0.5):
                pass
        self._cleanup()

    def _boot(self) -> None:
        """Heavy start-up, off the UI thread: wake model, microphone, warm-ups."""
        try:
            threading.Thread(target=self.router.warm, name="app-index", daemon=True).start()
            threading.Thread(target=self._warm_voice, name="tts-warm", daemon=True).start()
            threading.Thread(target=self.ears.warm, name="ears-warm", daemon=True).start()
            threading.Thread(target=self._warm_desktop, name="uia-warm", daemon=True).start()
            if CONFIG.wake_word_enabled:
                self.wake = WakeWord(CONFIG.wake_model, CONFIG.wake_threshold, CONFIG.wake_frames)
            else:
                self.wake = None
                log.info("wake-word detection disabled by config; hotkey listening remains available")
            from .audio.capture import describe_input_devices
            for device in describe_input_devices():
                log.info("available input device: %s", device)
            self.hub = AudioHub()
            self.hub.start()
            log.info("selected microphone: %s (index %s)",
                     self.hub.device_name, self.hub.device_index)
            self._audio_thread = threading.Thread(target=self._audio_loop, name="audio", daemon=True)
            self._audio_thread.start()
            self._set_state(State.IDLE)
            log.info("Jarvis online — %s",
                     "say 'Hey Jarvis' or press Ctrl+Space" if self.wake
                     else "press Ctrl+Space to listen")
            try:
                from .webui import start as start_dashboard

                start_dashboard()
            except Exception as exc:  # noqa: BLE001 - the dashboard is optional
                log.warning("dashboard failed to start: %s", exc)
            self.reminders = ReminderService(self._announce)
            self.reminders.start()
            # Wire the live reminder service into the tool layer so that LLM-triggered
            # reminders are seen by the same background polling thread that fires them.
            self.brain.tools._reminder_service = self.reminders
            if CONFIG.telegram_bot_token:
                from .telegram_bridge import TelegramBridge

                self.telegram = TelegramBridge(
                    CONFIG.telegram_bot_token,
                    CONFIG.telegram_allowed_user_id,
                    self._handle_telegram_message,
                )
                self.telegram.start()
                # Expose send_file to tools so they can send files via Telegram
                self.brain.tools._telegram_bridge = self.telegram
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="watchdog",
                                              daemon=True)
            self._watchdog.start()
            if self.greeting:
                # Wait for Whisper to finish loading first: on two cores, greeting while the
                # model loads makes both slow and the first words arrive late.
                threading.Thread(target=self._brief_when_ready, name="brief", daemon=True).start()
        except Exception as exc:  # noqa: BLE001 - a failed boot must be visible, not silent
            log.exception("boot failed: %s", exc)
            self._set_state(State.ERROR)
            try:
                self.speaker.say("I'm afraid I failed to start, sir. Please check the logs.")
            except Exception:  # noqa: BLE001
                pass

    def _warm_voice(self) -> None:
        warm = getattr(self.tts, "warm", None)
        if callable(warm):
            try:
                warm()
            except Exception:  # noqa: BLE001
                pass

    def _warm_desktop(self) -> None:
        try:
            from .skills import screen

            screen.warm()
        except Exception:  # noqa: BLE001
            pass

    def _greet(self) -> None:
        try:
            self._speak("At your service, sir. All systems are online.")
        except Exception as exc:  # noqa: BLE001
            log.warning("greeting failed: %s", exc)

    def _brief_when_ready(self) -> None:
        """Greet once the ears are loaded, so the two do not fight over two CPU cores."""
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self._whisper_ready():
                break
            time.sleep(0.4)
        self._brief()

    def _brief(self) -> None:
        """Proactive start-up briefing: time, reminders, one remembered fact."""
        try:
            from .briefing import briefing_text

            self._speak(briefing_text())
        except Exception as exc:  # noqa: BLE001
            log.warning("briefing failed: %s", exc)

    # ------------------------------------------------------------------ watchdog
    def _watchdog_loop(self) -> None:
        """Keep the microphone alive, un-stick speech, and publish a health heartbeat."""
        while not self._stop.wait(10.0):
            try:
                if self.hub is not None:
                    stale = time.monotonic() - self._last_block_ts
                    thread_dead = self._audio_thread is None or not self._audio_thread.is_alive()
                    if not self.hub.running or stale > 8.0 or thread_dead:
                        log.warning("audio stalled (running=%s, silent %.1fs, thread=%s) — restarting",
                                    self.hub.running, stale, "dead" if thread_dead else "alive")
                        self.hub.restart()
                        if thread_dead:
                            self._audio_thread = threading.Thread(
                                target=self._audio_loop, name="audio", daemon=True)
                            self._audio_thread.start()
                        self._last_block_ts = time.monotonic()
                self._recover_stuck_state()
                self._write_health()
            except Exception:  # noqa: BLE001 - a watchdog must never die
                log.exception("watchdog tick failed")

    def _recover_stuck_state(self) -> None:
        """Never stay in speaking/listening/thinking forever if the audio path wedges."""
        if self.state is State.SPEAKING and not self.speaker.is_speaking:
            since = getattr(self, "_speaking_since", 0.0)
            if since and time.monotonic() - since > 90:
                log.error("stuck in speaking for %.0f s — resetting", time.monotonic() - since)
                self.speaker.stop()
                self._set_state(State.IDLE)
                self._speaking_since = 0.0
        elif self.state is State.LISTENING and self.recorder is None:
            self._set_state(State.IDLE)
        else:
            self._speaking_since = 0.0

    def _write_health(self) -> None:
        try:
            from .config import DATA

            payload = {
                "pid": os.getpid(),
                "started": self._started_ts,
                "uptime_s": round(time.time() - self._started_ts),
                "state": self.state.value,
                "mic": getattr(self.hub, "device_name", None),
                "mic_alive": bool(self.hub and self.hub.running),
                "last_turn": self._last_turn_ts,
                "heartbeat": time.time(),
            }
            (DATA / "health.json").write_text(json.dumps(payload), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _announce(self, text: str) -> bool:
        """Speak something unprompted (a reminder). Refuses if Jarvis is mid-conversation."""
        if self.state in (State.LISTENING, State.THINKING, State.SPEAKING):
            return False
        try:
            self._start_turn()
            self._speak(text, turn=self._turn)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("announce failed: %s", exc)
            return False

    def shutdown(self, reason: str = "unknown") -> None:
        if self._stop.is_set():
            return
        log.info("shutting down (requested by: %s)", reason)
        self._stop.set()
        if self.telegram is not None:
            self.telegram.stop()
        try:
            from .webui import stop as stop_dashboard

            stop_dashboard()
        except Exception:  # noqa: BLE001
            pass
        if self.reminders is not None:
            self.reminders.stop()
        try:
            self.speaker.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._hotkeys is not None:
            try:
                self._hotkeys.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.tray is not None:
            self.tray.stop()
        if self.orb is not None:
            self.orb.close()

    def _cleanup(self) -> None:
        if self.hub is not None:
            try:
                self.hub.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.tray is not None:
            self.tray.stop()
        log.info("Jarvis offline")

    # -------------------------------------------------------------------- states
    def _set_state(self, state: State) -> None:
        with self._state_lock:
            self.state = state
        if self.orb is not None:
            self.orb.set_state(state.value)
        if self.tray is not None:
            self.tray.set_state(state.value)
        log.debug("state -> %s", state.value)
        try:
            self.activity.record("state", state.value)
        except Exception:
            pass

    def _start_turn(self) -> int:
        with self._state_lock:
            self._turn += 1
            return self._turn

    def _turn_is_current(self, turn: int) -> bool:
        with self._state_lock:
            return turn == self._turn

    # ------------------------------------------------------------- public actions
    def trigger_listen(self) -> None:
        """Wake by hotkey, click or tray (same path as the wake word)."""
        if self.muted or self._stop.is_set():
            return
        if self.state in (State.THINKING, State.SPEAKING):
            self.speaker.stop()
        self._begin_listening()

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        if self.muted:
            log.info("microphone muted")
            self.speaker.stop()
            self._set_state(State.MUTED)
        else:
            log.info("microphone unmuted")
            self._set_state(State.IDLE)

    def panic(self) -> None:
        log.info("panic stop")
        self.speaker.stop()
        self.recorder = None
        self._followup_deadline = 0.0
        self._set_state(State.MUTED if self.muted else State.IDLE)

    def reveal_orb(self) -> None:
        """Make the orb visible again after it has been hidden."""
        if self.orb is not None:
            self.orb.show()

    # ------------------------------------------------------------------ audio loop
    def _audio_loop(self) -> None:
        log.info("audio loop running (mic: %s)", self.hub.device_name)
        while not self._stop.is_set():
            try:
                block = self.hub.get_block(timeout=0.25)
                if block is not None:
                    self._last_block_ts = time.monotonic()
                    if not self.muted:
                        self._handle_block(block)
                # A PortAudio stream can stop without raising from get_block (for
                # example, if another app grabs the device). Watch it independently
                # of queue timeouts so the listener doesn't silently stay dead.
                if self.hub is not None and not self.hub.running:
                    log.warning("microphone stream is inactive; reopening it")
                    self.hub.restart()
                self._expire_followup()
            except Exception:  # noqa: BLE001 - the audio thread must never die
                log.exception("audio loop error; listener remains active")
                try:
                    if self.hub is not None and not self.hub.running:
                        log.warning("microphone stream is closed; reopening it")
                        self.hub.restart()
                except Exception:
                    log.exception("microphone stream restart failed; retrying")
                self._stop.wait(0.25)

    def _handle_block(self, block: np.ndarray) -> None:
        state = self.state
        samples = block.astype(np.float32) / 32768.0
        level = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0

        # While speaking, our own voice is in the room. The echo guard decides whether a
        # block is us or the user talking over us — the wake word cannot (it is our voice).
        if state is State.SPEAKING:
            if not CONFIG.barge_in:
                return
            # Never interrupt for arbitrary speech or echo: only an actual wake word.
            if self.wake is not None and self.wake.detect(block):
                log.info("barge-in by wake word")
                self.speaker.stop()
                self._start_turn()
                self._begin_listening(followup=True)
            return

        # Wake word while idle, thinking or in the follow-up window.
        if state is not State.LISTENING:
            if (self.wake is not None
                    and time.monotonic() >= self._wake_word_not_before
                    and self.wake.detect(block)):
                if state in (State.THINKING, State.SPEAKING):
                    log.info("barge-in by wake word")
                    self.speaker.stop()
                    self._start_turn()      # invalidate the in-flight answer
                self._begin_listening()
                return

        # Always-ready mode: even if the wake model missed it, someone saying the name
        # should work. Speech in idle is captured and checked locally with Whisper.
        # Guarded by a busy flag and a cooldown: without them, every sentence in the room
        # costs seconds of CPU and the machine turns into a transcription service.
        if (state is State.IDLE and CONFIG.asr_wake and self._whisper_ready()
                and not self._wake_check_busy
                and time.monotonic() >= self._wake_cooldown_until):
            if self.vad.is_speech(block):
                self._idle_speech += 1
                if self._idle_speech >= 4:       # ~320 ms of voice, not a door slam
                    self._idle_speech = 0
                    log.debug("idle speech detected — listening for the name")
                    self._begin_listening(asr_wake=True)
            else:
                self._idle_speech = 0
            return

        if state is State.LISTENING and self.recorder is not None:
            if self.recorder.feed(block):
                self._end_listening()
            return

        if state is State.FOLLOWUP and time.monotonic() >= self._followup_ignore_until:
            # Require both a real voice (not our own tail) and speech energy.
            if self.echo.is_user_speech(samples, CONFIG.sample_rate, level) and self.vad.is_speech(block):
                self._followup_hits += 1
                if self._followup_hits >= 3:
                    self._begin_listening(followup=True)
            else:
                self._followup_hits = 0

    def _begin_listening(self, followup: bool = False, asr_wake: bool = False) -> None:
        if self._stop.is_set() or self.hub is None:
            return
        with self._listen_lock:
            if self.state in (State.LISTENING, State.TRANSCRIBING):
                return
            self._followup_deadline = 0.0
            if self.wake is not None:
                self.wake.reset()
            self.hub.flush()
            turn = self._start_turn()
            self._turn_at_listen = turn
            self._asr_capture = asr_wake
            preroll_ms = 900 if followup else (500 if asr_wake else CONFIG.preroll_ms)
            self.recorder = Recorder(self.vad, self.hub.recent(preroll_ms))
            self._set_state(State.LISTENING)
            if not followup and not asr_wake:
                self.player.blip(880, 70, 0.05)

    def _end_listening(self) -> None:
        rec, self.recorder = self.recorder, None
        if rec is None:
            return
        log.debug("utterance peak %.4f, speech %.0f ms, usable=%s",
                  rec.peak, rec.speech_ms, rec.usable)

        if self._asr_capture:
            self._asr_capture = False
            if not rec.usable:
                self._set_state(State.IDLE)
                return
            self._set_state(State.TRANSCRIBING)
            threading.Thread(target=self._check_asr_wake,
                             args=(rec.audio(), getattr(self, "_turn_at_listen", 0)),
                             name="asr-wake", daemon=True).start()
            return

        if not rec.usable:
            log.info("no speech after wake (peak %.4f)", rec.peak)
            if rec.peak >= CONFIG.salvage_peak:
                audio = rec.audio()
                self._set_state(State.TRANSCRIBING)
                threading.Thread(target=self._respond,
                                 args=(audio, getattr(self, "_turn_at_listen", 0)),
                                 name="turn", daemon=True).start()
            else:
                self._set_state(State.IDLE)
            return
        audio = rec.audio()
        log.debug("utterance: %.2fs, peak %.3f", len(audio) / CONFIG.sample_rate, rec.peak)
        self._set_state(State.TRANSCRIBING)
        threading.Thread(target=self._respond, args=(audio, getattr(self, "_turn_at_listen", 0)),
                         name="turn", daemon=True).start()

    def _expire_followup(self) -> None:
        if self.state is State.FOLLOWUP and self._followup_deadline:
            if time.monotonic() > self._followup_deadline:
                self._followup_deadline = 0.0
                self._set_state(State.IDLE)

    # --------------------------------------------------------------------- a turn
    def _respond(self, audio: np.ndarray, turn: int) -> None:
        self._last_turn_ts = time.time()
        try:
            with self._transcribe_lock:
                with Timer("stt", log):
                    text = self.ears.transcribe(audio, CONFIG.sample_rate)
        except Exception as exc:  # noqa: BLE001 - network, quota, or timeout
            log.error("transcription failed: %s", exc)
            self._speak("Forgive me, sir. My connection is struggling; I didn't catch that.",
                        turn=turn)
            return
        self._set_state(State.THINKING)
        text = (text or "").strip()
        if not text:
            log.warning("empty transcription")
            self._speak("I'm sorry, sir, I didn't catch that.", turn=turn)
            return
        self._process_text(text, turn)

    def _process_text(self, text: str, turn: int) -> None:
        """Interpret one already-transcribed request: stop words, confirmations, fast
        commands, then the model with its tools."""
        try:
            log.info("USER : %s", text)
            try:
                self.activity.record("user", text)
            except Exception:
                pass

            if text.lower().strip(" .!?") in STOP_WORDS:
                log.info("user asked to stop")
                self._set_state(State.IDLE)
                return

            # ---- spoken confirmation: yes/no for a pending destructive action ----
            if safety.has_pending():
                decision = safety.interpret(text)
                if decision is None:
                    self._speak("Just say yes or no, sir.", turn=turn)
                    return
                self._speak(safety.resolve(decision), turn=turn)
                return

            # ---- fast path: no network, no LLM, instant ----
            handled, fast_reply = self.router.handle(text)
            if handled:
                log.info("JARVIS (fast path): %s", fast_reply)
                self._speak(fast_reply, turn=turn)
                return

            try:
                with Timer("llm", log):
                    with self._brain_lock:
                        reply, actions = self.brain.respond(text)
                for action in actions:
                    log.info("ACTION: %s", action)
            except Exception as exc:  # noqa: BLE001 - slow or congested network
                log.error("brain failed: %s", exc)
                # Check if it's a quota/network error vs other error
                err_text = str(exc).lower()
                if any(k in err_text for k in ("quota", "rate limit", "429", "403", "insufficient")):
                    self._speak("Forgive me, sir. All my cloud providers are at capacity. I can still help with local commands like opening apps, checking the time, or controlling volume.", turn=turn)
                else:
                    self._speak("Forgive me, sir. Something went wrong with my thinking process.", turn=turn)
                return
            if not self._turn_is_current(turn):
                log.debug("turn superseded before speaking")
                return
            if not reply:
                reply = "I'm not quite sure what to say to that, sir."
            log.info("JARVIS: %s", reply)
            try:
                self.activity.record("assistant", reply)
            except Exception:
                pass

            self._speak(reply, turn=turn)
        except (GeminiError, TTSError, RuntimeError) as exc:
            log.error("turn failed: %s", exc)
            self._fail_softly(turn)
        except Exception as exc:  # noqa: BLE001 - a turn must never kill the app
            log.exception("unexpected turn failure: %s", exc)
            self._fail_softly(turn)

    def _handle_telegram_message(self, text: str) -> str:
        """Run a private, allowlisted Telegram command through Jarvis's normal brain/tools."""
        log.info("Telegram command received from the configured owner")
        with self._brain_lock:
            reply, actions = self.brain.respond(text)
        for action in actions:
            log.info("TELEGRAM ACTION: %s", action)
        return reply or "I couldn't produce a reply, sir."

    # ------------------------------------------------------------- always-ready mode
    def _whisper_ready(self) -> bool:
        return self.ears.whisper is not None and getattr(self.ears.whisper, "ready", False)

    def _check_asr_wake(self, audio: np.ndarray, turn: int) -> None:
        """Someone spoke in the room: was it for Jarvis? Transcribe locally and look for
        the name, so 'Jarvis, open notepad' works without the wake model firing."""
        self._wake_check_busy = True
        try:
            self._set_state(State.THINKING)
            # Only the first few seconds matter for the name, and greedy decoding is enough.
            # This keeps idle listening cheap on a two-core machine.
            snippet = audio[: CONFIG.sample_rate * 5 // 2]   # 2.5 s is plenty for a name
            with self._transcribe_lock:
                with Timer("wake-stt", log):
                    text = self.ears.transcribe(snippet, CONFIG.sample_rate, fast=True)
            text = (text or "").strip()
            if not self._turn_is_current(turn):
                return
            if not text:
                self._wake_cooldown_until = time.monotonic() + 5.0
                self._set_state(State.IDLE)
                return
            log.info("idle speech heard: %r", text)
            match = WAKE_NAME_RE.match(text)
            if not match:
                # Not addressed to me — but log what was heard to aid tuning.
                log.info("wake-stt: no name match in %r — cooldown 5 s", text)
                self._wake_cooldown_until = time.monotonic() + 5.0
                self._set_state(State.IDLE)
                return
            command = (match.group("command") or "").strip(" ,.!?;:")
            log.info("ASR wake: %r -> command %r", text, command)
            if not command:
                self.player.blip(880, 70, 0.05)
                self._begin_listening()
                return
            self.player.blip(720, 60, 0.04)
            self._process_text(command, turn)
        except Exception:  # noqa: BLE001 - never let this path kill audio
            log.exception("asr wake check failed")
            self._set_state(State.IDLE)
        finally:
            self._wake_check_busy = False

    def _speak(self, text: str, turn: int | None = None) -> None:
        turn = self._turn if turn is None else turn
        if not self._turn_is_current(turn):
            return
        self._set_state(State.SPEAKING)
        self._speaking_since = time.monotonic()
        self.echo.reset()
        started = time.perf_counter()

        def first_audio() -> None:
            log.info("first audio out       %6.0f ms", (time.perf_counter() - started) * 1000)

        def note_output(chunk) -> None:  # noqa: ANN001
            level = float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0
            self.echo.note_output(chunk, self.player.rate, level)

        finished = False
        try:
            finished = self.speaker.say(text, on_first_audio=first_audio, on_chunk=note_output)
        except Exception:  # noqa: BLE001 - a speech failure must never leave us stuck
            log.exception("speech failed")
        finally:
            self._speaking_since = 0.0
        if not self._turn_is_current(turn):
            return
        if finished:
            self._set_state(State.FOLLOWUP)
            self._wake_word_not_before = (
                time.monotonic() + CONFIG.tts_cooldown_ms / 1000.0
            )
            self._followup_deadline = time.monotonic() + CONFIG.followup_ms / 1000
            self._followup_ignore_until = (
                time.monotonic() + CONFIG.tts_cooldown_ms / 1000.0
            )
            self._followup_hits = 0
            log.debug("follow-up window open for %d ms", CONFIG.followup_ms)
        else:
            self._set_state(State.IDLE)

    def _fail_softly(self, turn: int) -> None:
        if not self._turn_is_current(turn):
            return
        self._set_state(State.ERROR)
        try:
            self.speaker.say("Forgive me, sir. I'm having a little trouble.")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
        if self._turn_is_current(turn):
            self._set_state(State.IDLE)

    # ------------------------------------------------------------------- hotkeys
    def _start_hotkeys(self) -> None:
        try:
            from pynput import keyboard
        except Exception as exc:  # noqa: BLE001
            log.warning("global hotkeys unavailable: %s", exc)
            return
        try:
            self._hotkeys = keyboard.GlobalHotKeys({
                CONFIG.hotkey_listen: self.trigger_listen,
                CONFIG.hotkey_mute: self.toggle_mute,
                CONFIG.hotkey_panic: self.panic,
            })
            self._hotkeys.start()
            log.info("hotkeys: %s listen, %s mute, %s panic",
                     CONFIG.hotkey_listen, CONFIG.hotkey_mute, CONFIG.hotkey_panic)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not register global hotkeys: %s", exc)
