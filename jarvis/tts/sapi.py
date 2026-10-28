"""Offline Windows voice (SAPI5) — the guaranteed-to-work fallback.

Driven in-process through COM (comtypes) with an in-memory stream: a sentence renders in
well under a second, no temp files, no network, no PowerShell start-up cost.
"""
from __future__ import annotations

import logging
import threading

import numpy as np

from ..config import CONFIG

log = logging.getLogger("tts.sapi")

_VOICE_HINTS = {
    "en-GB": "Microsoft Hazel Desktop",
    "en-US": "Microsoft Zira Desktop",
}
SAFT22kHz16BitMono = 22
SAPI_RATE = 22050


class SapiTTS:
    def __init__(self, voice: str | None = None) -> None:
        self.voice_name = voice or _VOICE_HINTS.get(CONFIG.edge_voice[:5], "Microsoft Hazel Desktop")
        self.last_latency_ms = 0.0
        self._local = threading.local()

    def _ensure(self):
        voice = getattr(self._local, "voice", None)
        if voice is not None:
            return voice
        import comtypes.client

        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        for token in voice.GetVoices():
            if self.voice_name.lower() in token.GetDescription().lower():
                voice.Voice = token
                break
        self._local.voice = voice
        return voice

    def warm(self) -> None:
        import comtypes

        initialized = False
        try:
            comtypes.CoInitialize()
            initialized = True
            self._ensure()
        except Exception as exc:  # noqa: BLE001
            log.debug("SAPI warm-up failed: %s", exc)
        finally:
            self._local.voice = None
            if initialized:
                comtypes.CoUninitialize()

    def synthesize(self, text: str) -> bytes:
        """Render text to raw 22 kHz 16-bit mono PCM, in memory."""
        text = text.strip()
        if not text:
            return b""
        import time

        started = time.perf_counter()
        import comtypes
        import comtypes.client

        comtypes.CoInitialize()
        voice = None
        memory = None
        try:
            voice = self._ensure()
            memory = comtypes.client.CreateObject("SAPI.SpMemoryStream")
            memory.Format.Type = SAFT22kHz16BitMono
            voice.AudioOutputStream = memory
            try:
                voice.Speak(text)
                audio = bytes(memory.GetData())
            finally:
                try:
                    voice.AudioOutputStream = None
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._local.voice = None
            voice = None
            memory = None
            comtypes.CoUninitialize()
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        return audio

    def synthesize_pcm(self, text: str, rate: int = CONFIG.playback_rate) -> np.ndarray:
        """Render and resample to float32 mono at the player's rate."""
        pcm = np.frombuffer(self.synthesize(text), dtype=np.int16).astype(np.float32) / 32768.0
        if not pcm.size or rate == SAPI_RATE:
            return pcm
        duration = pcm.size / SAPI_RATE
        target = max(1, int(duration * rate))
        if target == pcm.size:
            return pcm
        return np.interp(
            np.linspace(0.0, 1.0, target, endpoint=False),
            np.linspace(0.0, 1.0, pcm.size, endpoint=False),
            pcm,
        ).astype(np.float32)
