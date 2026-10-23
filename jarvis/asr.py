"""Local speech recognition with faster-whisper — no network, no API key, no ffmpeg.

The microphone already hands us raw 16 kHz PCM, and `WhisperModel.transcribe()` accepts
numpy directly, so the audio never becomes a file and none of the "works on Linux, fails
on Windows because ffmpeg isn't on PATH" problems arise.

CPU only, deliberately: this laptop has no GPU, and the promise is that Jarvis runs here.
Threads are capped because recognition runs while audio is also being played.
"""
from __future__ import annotations

import logging
import threading
import time
from importlib.util import find_spec

import numpy as np

from .config import CONFIG

log = logging.getLogger("asr.whisper")

MODEL_SIZES = ("tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "large-v3")


def available() -> bool:
    return find_spec("faster_whisper") is not None


def normalise(pcm: np.ndarray, target_peak: float = 0.7, max_gain: float = 20.0) -> np.ndarray:
    """Automatic gain: laptop mics with AGC deliver speech at -35 dBFS, and Whisper
    transcribes quiet audio poorly or not at all. Scale the utterance up, never down.

    int16 input must be divided *before* any gain maths, or the "peak" is measured in
    the thousands and the samples are handed to the model outside its [-1, 1] range.
    """
    if pcm is None or len(pcm) == 0:
        return pcm
    raw = np.asarray(pcm)
    if raw.dtype == np.int16:
        samples = raw.astype(np.float32) / 32768.0
    else:
        samples = raw.astype(np.float32)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak < 1e-4:
        return samples
    gain = min(max_gain, target_peak / peak)
    if gain <= 1.05:
        return samples
    return np.clip(samples * gain, -1.0, 1.0)


class WhisperEars:
    """Thread-safe, lazily-loaded Whisper transcription on the CPU."""

    def __init__(self, model_size: str | None = None, threads: int | None = None,
                 fast_model_size: str | None = None) -> None:
        self.model_size = model_size or CONFIG.whisper_model
        self.fast_model_size = fast_model_size or getattr(CONFIG, "whisper_fast_model", "tiny.en")
        self.threads = max(1, threads or CONFIG.whisper_threads)
        self._model = None
        self._fast_model = None
        self._lock = threading.Lock()
        # Transcription is serialised: two turns used to run the same CTranslate2 model at
        # once on two cores, which is what produced the 72 s and 177 s outliers in the log.
        self._transcribe_lock = threading.Lock()
        self.last_duration_ms = 0.0
        self.ready = False

    def warm(self) -> None:
        """Load the accuracy model (and download it the first time) ahead of the first
        utterance. The small model used for name checks loads lazily."""
        self._ensure()

    def _load(self, size: str):
        from faster_whisper import WhisperModel

        started = time.perf_counter()
        log.info("loading whisper '%s' (CPU, %d threads)", size, self.threads)
        model = WhisperModel(
            size,
            device="cpu",
            compute_type="int8",
            cpu_threads=self.threads,
            num_workers=1,
        )
        log.info("whisper '%s' ready in %.1f s", size, time.perf_counter() - started)
        return model

    def _ensure(self, fast: bool = False):
        """Quality model by default; the fast model only for quick name checks."""
        if fast and self.fast_model_size and self.fast_model_size != self.model_size:
            if self._fast_model is None:
                with self._lock:
                    if self._fast_model is None:
                        try:
                            self._fast_model = self._load(self.fast_model_size)
                        except Exception as exc:  # noqa: BLE001 - fall back to the big one
                            log.warning("fast whisper model unavailable (%s); using %s",
                                        exc, self.model_size)
                            self._fast_model = False
            return self._fast_model if self._fast_model is not False else self._ensure(False)

        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        self._model = self._load(self.model_size)
                    except Exception as exc:  # noqa: BLE001 - fall back to the small model
                        log.warning("whisper '%s' failed (%s); falling back to '%s'",
                                    self.model_size, exc, self.fast_model_size)
                        self.model_size = self.fast_model_size
                        self._model = self._load(self.fast_model_size)
                    self.ready = True
        return self._model

    def transcribe(self, pcm: np.ndarray, rate: int = CONFIG.sample_rate,
                   fast: bool = False) -> str:
        """Transcribe 16 kHz PCM. `fast` uses the small model with greedy decoding and no
        domain prompt — right for a quick "did they say Jarvis?" check on idle audio."""
        if pcm is None or len(pcm) == 0:
            return ""
        model = self._ensure(fast)
        samples = normalise(pcm)
        if rate != 16000:
            duration = samples.size / rate
            target = max(1, int(duration * 16000))
            samples = np.interp(
                np.linspace(0.0, 1.0, target, endpoint=False),
                np.linspace(0.0, 1.0, samples.size, endpoint=False),
                samples,
            ).astype(np.float32)
        with self._transcribe_lock:
            started = time.perf_counter()
            segments, info = model.transcribe(
                samples,
                language="en" if self.model_size.endswith(".en") else None,
                beam_size=1 if fast else CONFIG.whisper_beam,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=None if fast else CONFIG.whisper_prompt,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            self.last_duration_ms = (time.perf_counter() - started) * 1000
        log.debug("whisper: %.2fs audio -> %.0f ms: %r",
                  samples.size / 16000, self.last_duration_ms, text[:80])
        return text
