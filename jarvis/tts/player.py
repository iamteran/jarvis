"""Audio playback that can be interrupted mid-sentence (barge-in)."""
from __future__ import annotations

import logging
import threading

import numpy as np

try:
    import sounddevice as sd
except Exception:  # pragma: no cover - handled gracefully at runtime
    sd = None

from ..audio.capture import resolve_device
from ..config import CONFIG

log = logging.getLogger("tts.player")


class Player:
    def __init__(self, rate: int = CONFIG.playback_rate) -> None:
        self.rate = rate
        self.device = resolve_device(CONFIG.speaker_device, "output")
        self._stop = threading.Event()
        self._playing = threading.Event()

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    def play(self, samples: np.ndarray, on_chunk=None) -> bool:  # noqa: ANN001
        """Play float32 mono samples. Returns False if interrupted.

        `on_chunk` is called with each block as it is written, so the echo guard knows
        exactly what came out of the speakers and when.
        """
        if sd is None:
            return True
        if samples is None or len(samples) == 0:
            return True
        self._stop.clear()
        self._playing.set()
        try:
            with sd.OutputStream(
                samplerate=self.rate, channels=1, dtype="float32",
                device=self.device, blocksize=2048,
            ) as stream:
                step = 2048
                for start in range(0, len(samples), step):
                    if self._stop.is_set():
                        log.debug("playback interrupted at %d/%d", start, len(samples))
                        return False
                    chunk = np.ascontiguousarray(samples[start:start + step])
                    if on_chunk is not None:
                        try:
                            on_chunk(chunk)
                        except Exception:  # noqa: BLE001 - bookkeeping must not break audio
                            pass
                    stream.write(chunk.reshape(-1, 1))
            return True
        finally:
            self._playing.clear()

    def stop(self) -> None:
        self._stop.set()

    def blip(self, freq: float = 880.0, ms: int = 70, volume: float = 0.05) -> None:
        """Tiny confirmation tone — how the user knows Jarvis is listening."""
        if sd is None:
            return
        try:
            rate = self.rate
            n = int(rate * ms / 1000)
            t = np.arange(n, dtype=np.float32) / rate
            envelope = np.minimum(1.0, np.minimum(t / 0.004, (ms / 1000 - t) / 0.012))
            envelope = np.clip(envelope, 0.0, 1.0)
            tone = (np.sin(2 * np.pi * freq * t) * envelope * volume).astype(np.float32)
            with sd.OutputStream(samplerate=rate, channels=1, dtype="float32",
                                 device=self.device, blocksize=1024) as stream:
                stream.write(tone.reshape(-1, 1))
        except Exception:  # noqa: BLE001 - a failed beep must never matter
            log.debug("blip failed", exc_info=True)
