"""Piper: a neural voice that runs on this machine, not in a data centre.

Why this is the best default here: edge-tts and Fish Audio are far better than Windows
SAPI, but on a 50–60 KB/s link they cost 14–40 seconds. Piper synthesises a sentence in a
few hundred milliseconds on this CPU, works offline forever, and sounds human.

Voice models are ~60 MB, downloaded once into `assets/voices/`.
"""
from __future__ import annotations

import io
import logging
import re
import wave
from importlib.util import find_spec
from pathlib import Path

import numpy as np

from ..config import ASSETS

log = logging.getLogger("tts.piper")

VOICE_DIR = ASSETS / "voices"


def available() -> bool:
    return find_spec("piper") is not None


class PiperTTS:
    def __init__(self, voice_name: str | None = None) -> None:
        from ..config import CONFIG

        self.voice_name = voice_name or CONFIG.piper_voice
        self._voice = None
        self._rate = 22050
        self.last_latency_ms = 0.0

    # ------------------------------------------------------------------- model
    def model_path(self) -> Path | None:
        candidates = sorted(VOICE_DIR.glob("*.onnx")) if VOICE_DIR.is_dir() else []
        if not candidates:
            return None
        wanted = self.voice_name.lower()
        for path in candidates:
            if wanted in path.stem.lower():
                return path
        return candidates[0]

    def ready(self) -> bool:
        return available() and self.model_path() is not None

    def warm(self) -> None:
        if not self.ready():
            log.info("piper voice not ready (package or model missing)")
            return
        self._ensure()

    def _ensure(self) -> None:
        if self._voice is not None:
            return
        from piper import PiperVoice

        path = self.model_path()
        if path is None:
            raise RuntimeError("no Piper voice model found in assets/voices")
        import time

        started = time.perf_counter()
        self._voice = PiperVoice.load(str(path))
        try:
            self._rate = int(self._voice.config.sample_rate)
        except Exception:  # noqa: BLE001 - older piper API
            self._rate = 22050
        log.info("piper voice loaded: %s (%d Hz) in %.2f s",
                 path.stem, self._rate, time.perf_counter() - started)

    # --------------------------------------------------------------- synthesis
    def synthesize(self, text: str) -> bytes:
        """Render to a WAV container in memory."""
        self._ensure()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            self._voice.synthesize_wav(text, wav_file)
        return buffer.getvalue()

    def synthesize_pcm(self, text: str, rate: int = 24000) -> np.ndarray:
        import time

        started = time.perf_counter()
        audio = self.synthesize(text)
        with wave.open(io.BytesIO(audio)) as wav_file:
            source_rate = wav_file.getframerate()
            frames = wav_file.readframes(wav_file.getnframes())
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        if source_rate != rate and pcm.size:
            duration = pcm.size / source_rate
            target = max(1, int(duration * rate))
            pcm = np.interp(
                np.linspace(0.0, 1.0, target, endpoint=False),
                np.linspace(0.0, 1.0, pcm.size, endpoint=False),
                pcm,
            ).astype(np.float32)
        return pcm


def clean_for_speech(text: str) -> str:
    """Piper reads punctuation literally in places; keep it simple."""
    text = re.sub(r"[*_`#]+", "", text)
    return re.sub(r"\s+", " ", text).strip()
