"""Pick the voice engine from config, degrading gracefully on a slow machine and link.

JARVIS_TTS=auto   -> Piper if its model is present; else edge-tts with a time budget;
                     else Windows SAPI.                                         [default]
JARVIS_TTS=piper  -> local neural voice (assets/voices/*.onnx), else SAPI
JARVIS_TTS=sapi   -> Windows SAPI5 offline voice (instant, robotic)
JARVIS_TTS=edge   -> Microsoft neural voices (good, needs a healthy connection)
JARVIS_TTS=fish   -> your Fish Audio Jarvis clone (best, slowest on a poor link)
"""
from __future__ import annotations

import logging
import threading
import time

from ..config import CONFIG

log = logging.getLogger("tts.factory")


def _build(kind: str):
    if kind == "edge":
        from .edge import EdgeTTS
        return EdgeTTS()
    if kind == "fish":
        from .fish import FishTTS
        return FishTTS()
    if kind == "piper":
        from .piper_engine import PiperTTS
        return PiperTTS()
    from .sapi import SapiTTS
    return SapiTTS()


class _BudgetedTTS:
    """Try the good voice; if it takes too long, use the offline voice for a while."""

    DEGRADE_S = 300.0

    def __init__(self, primary: str, fallback: str, budget_ms: float) -> None:
        self._primary = primary
        self._fallback = fallback
        self._budget_ms = budget_ms
        # Start on the instant offline voice for the first minute; the start-up probe can
        # only *demote* us. That keeps the greeting immediate on a slow link and avoids
        # competing with the Whisper model load for the CPU.
        self._degraded_until = time.monotonic() + 60.0
        self._in_flight = threading.Event()
        self._primary_engine = None
        self._fallback_engine = None
        self.last_latency_ms = 0.0

    def _get(self, kind: str):
        if kind == self._primary:
            if self._primary_engine is None:
                self._primary_engine = _build(kind)
            return self._primary_engine
        if self._fallback_engine is None:
            self._fallback_engine = _build(kind)
        return self._fallback_engine

    def warm(self) -> None:
        for kind in (self._primary, self._fallback):
            engine = self._get(kind)
            warm = getattr(engine, "warm", None)
            if callable(warm):
                try:
                    warm()
                except Exception:  # noqa: BLE001
                    pass
        self._probe()

    def _probe(self) -> None:
        """Find out now whether the good voice is usable, instead of mid-sentence."""
        if self._primary != "edge" or time.monotonic() < self._degraded_until:
            return
        try:
            started = time.perf_counter()
            self._get("edge").synthesize_pcm("Ready.", CONFIG.playback_rate)
            ms = (time.perf_counter() - started) * 1000
        except Exception as exc:  # noqa: BLE001
            log.info("edge voice unavailable (%s); using SAPI", exc)
            self._degraded_until = time.monotonic() + self.DEGRADE_S
            return
        if ms > self._budget_ms:
            log.info("edge voice slow right now (%.0f ms); using SAPI for %ds",
                     ms, self.DEGRADE_S)
            self._degraded_until = time.monotonic() + self.DEGRADE_S
        else:
            log.info("edge voice healthy (%.0f ms) — switching to the neural voice", ms)
            self._degraded_until = 0.0

    def synthesize_pcm(self, text: str, rate: int = CONFIG.playback_rate):
        fallback = self._get(self._fallback)
        if time.monotonic() < self._degraded_until:
            return fallback.synthesize_pcm(text, rate)

        # If a previous attempt is still stuck in the network, do not start another one —
        # that is how threads pile up until the machine is unusable. Use the offline voice.
        if self._in_flight.is_set():
            return fallback.synthesize_pcm(text, rate)

        result: dict = {}

        def _work() -> None:
            try:
                result["pcm"] = self._get(self._primary).synthesize_pcm(text, rate)
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc
            finally:
                self._in_flight.clear()

        self._in_flight.set()
        worker = threading.Thread(target=_work, name="tts-primary", daemon=True)
        started = time.perf_counter()
        worker.start()
        worker.join(timeout=self._budget_ms / 1000.0)
        if worker.is_alive():
            log.info("%s voice exceeded its %.1f s budget; using %s for %ds",
                     self._primary, self._budget_ms / 1000.0, self._fallback, self.DEGRADE_S)
            self._degraded_until = time.monotonic() + self.DEGRADE_S
            return fallback.synthesize_pcm(text, rate)
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        if "error" in result:
            log.warning("%s voice failed (%s); using %s", self._primary,
                        result["error"], self._fallback)
            self._degraded_until = time.monotonic() + self.DEGRADE_S
            return fallback.synthesize_pcm(text, rate)
        return result.get("pcm") or fallback.synthesize_pcm(text, rate)


def make_tts():
    engine = (CONFIG.tts_engine or "auto").lower()

    if engine == "auto":
        try:
            from .piper_engine import PiperTTS, available as piper_available

            if piper_available() and PiperTTS().ready():
                log.info("voice: piper (local neural, offline)")
                return _build("piper")
        except Exception:  # noqa: BLE001
            pass
        log.info("voice: edge-tts with a %.1f s budget, SAPI fallback", CONFIG.fish_budget_ms / 1000)
        return _BudgetedTTS("edge", "sapi", CONFIG.fish_budget_ms)

    if engine == "piper":
        try:
            from .piper_engine import PiperTTS

            if PiperTTS().ready():
                return _build("piper")
            log.warning("piper requested but no voice model found; using SAPI")
        except Exception as exc:  # noqa: BLE001
            log.warning("piper unavailable (%s); using SAPI", exc)
        return _build("sapi")

    if engine in ("sapi", "edge", "fish"):
        return _build(engine)

    log.warning("unknown JARVIS_TTS=%r, using SAPI", engine)
    return _build("sapi")
