"""edge-tts voice engine — Microsoft's free neural voices.

Far faster than a queued cloud TTS model on a slow connection (measured: 2.3 s vs 14 s),
and needs no API key. Used as Jarvis's default voice until the network allows the
high-fidelity Fish Audio clone to be comfortable again.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import CONFIG

log = logging.getLogger("tts.edge")


class EdgeTTS:
    def __init__(self, voice: str | None = None) -> None:
        self.voice = voice or CONFIG.edge_voice
        self.last_latency_ms = 0.0

    def warm(self) -> None:
        """Import the (heavy) edge-tts/websocket stack ahead of the first sentence."""
        try:
            import edge_tts  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            log.debug("edge-tts warm-up failed: %s", exc)

    async def _collect(self, text: str) -> bytes:
        import edge_tts

        audio = bytearray()
        async for chunk in edge_tts.Communicate(text, self.voice).stream():
            if chunk.get("type") == "audio":
                audio.extend(chunk.get("data", b""))
        return bytes(audio)

    def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""
        import time

        started = time.perf_counter()
        try:
            audio = asyncio.run(self._collect(text))
        except RuntimeError:
            # a loop is already running in this thread (called from async code)
            loop = asyncio.new_event_loop()
            try:
                audio = loop.run_until_complete(self._collect(text))
            finally:
                loop.close()
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        if not audio:
            raise RuntimeError("edge-tts returned no audio")
        return audio

    def synthesize_pcm(self, text: str, rate: int = CONFIG.playback_rate):
        from .fish import decode_to_pcm

        return decode_to_pcm(self.synthesize(text), rate)
