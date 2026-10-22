"""Telling the user's voice apart from Jarvis's own voice coming back through the speakers.

Why this matters here: writing to an audio device returns when the buffer accepts the
audio, not when the room has finished with it. So for a moment after a reply ends, sound
is still leaving the speakers and the microphone hears it. That is what makes a voice
assistant interrupt itself, wake itself up, or transcribe its own last sentence as the
user's new request.

How it works (two signals, neither needing per-machine tuning):

1. **Content.** Echo is not merely loud, it is *the same sound we just played*. Both
   streams are reduced to a handful of band energies, then as much of the recent output
   as fits is *subtracted* from the microphone block. Echo cancels to almost nothing; a
   different voice has formants where ours were weak, so it survives.
2. **A learned echo gain.** Whenever the content check says "that is definitely echo", the
   observed mic-to-output ratio is folded into a running estimate, so the system
   calibrates itself to the actual room and re-calibrates when the volume changes.

Adapted from `core/echo.py` in FatihMakes/Mark-LVI (CC BY-NC 4.0, non-commercial),
rewritten for Jarvis's threading model. See NOTICE.md.
"""
from __future__ import annotations

import time

import numpy as np

_BAND_EDGES = (200, 400, 700, 1100, 1700, 2600, 3800, 5200, 7000)

_HISTORY_S = 1.5      # how far back an echo could plausibly have been played
_MIN_LEVEL = 0.06     # below this the mic is room noise; nothing to decide
_MIN_USER = 0.15      # never call anything below this a voice
_HEAD_Q = 97          # percentile of observed echo the threshold must clear
_HEAD_MULT = 1.15     # ...with this much headroom above it
_UNRELIABLE_FLOOR = 0.22
_BLOCKS_NORMAL = 5    # ~320 ms of sustained evidence
_BLOCKS_NOISY = 12    # ~770 ms when the room is this hard
_FLOOR_WINDOW = 60
_FLOOR_Q = 35
_WARMUP = 16          # blocks (~1 s) of listening before judging anyone
_RELEARN_RUN = 28


def band_energies(pcm, sr: int) -> np.ndarray:
    """Raw energy per speech band — the fingerprint we compare."""
    x = np.asarray(pcm, dtype=np.float32)
    if x.size < 64:
        return np.zeros(len(_BAND_EDGES) - 1, dtype=np.float32)
    x = x - x.mean()
    n = 1 << (int(x.size) - 1).bit_length()
    mag = np.abs(np.fft.rfft(x * np.hanning(x.size), n=n))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    out = np.empty(len(_BAND_EDGES) - 1, dtype=np.float32)
    for i in range(len(_BAND_EDGES) - 1):
        mask = (freqs >= _BAND_EDGES[i]) & (freqs < _BAND_EDGES[i + 1])
        out[i] = float(mag[mask].sum())
    return out


class EchoGuard:
    """Classifies microphone blocks while Jarvis is speaking."""

    def __init__(self) -> None:
        self._hist: list[tuple[float, np.ndarray, float]] = []
        self._gain = 0.6
        self._seen = 0
        self._last_sim = 0.0
        self._residuals: list[float] = []
        self._floor = 0.10
        self._run = 0
        self._head = 0.13

    # ---------------------------------------------------------------- diagnostics
    @property
    def calibrated(self) -> bool:
        return self._seen >= 8

    @property
    def floor(self) -> float:
        return self._floor

    @property
    def reliable(self) -> bool:
        return self._floor < _UNRELIABLE_FLOOR

    @property
    def threshold(self) -> float:
        return max(_MIN_USER, self._head * _HEAD_MULT)

    @property
    def required_blocks(self) -> int:
        return _BLOCKS_NORMAL if self.reliable else _BLOCKS_NOISY

    def reset(self) -> None:
        self._hist.clear()
        self._last_sim = 0.0

    # --------------------------------------------------------------- entry points
    def note_output(self, pcm, sr: int, level: float, when: float | None = None) -> None:
        """Record a slice of what is being played, for later comparison."""
        try:
            t = time.monotonic() if when is None else when
            self._hist.append((t, band_energies(pcm, sr), float(level)))
            if len(self._hist) > 8:
                self._hist = [h for h in self._hist if h[0] >= t - _HISTORY_S]
        except Exception:  # noqa: BLE001 - never disturb playback
            pass

    def is_user_speech(self, pcm, sr: int, level: float,
                       when: float | None = None) -> bool:
        """True if this microphone block is a different voice, not our own echo."""
        try:
            if level < _MIN_LEVEL:
                # Playing, but nothing comes back: headphones or a distant mic. Record
                # that, or the window stays empty and the first speaker becomes the
                # calibration sample.
                if self._hist and max(h[2] for h in self._hist) > 0.15:
                    self._residuals.append(0.0)
                    if len(self._residuals) > _FLOOR_WINDOW:
                        del self._residuals[:-_FLOOR_WINDOW]
                    if len(self._residuals) >= _WARMUP:
                        self._floor = float(np.percentile(self._residuals, _FLOOR_Q))
                        self._head = float(np.percentile(self._residuals, _HEAD_Q))
                self._run = 0
                return False
            if not self._hist:
                return True

            t = time.monotonic() if when is None else when
            bands = band_energies(pcm, sr)
            total = float(bands.sum())
            if total <= 1e-9:
                return False

            best_res, best_level = 1.0, 0.0
            for ts, ref, ref_level in self._hist:
                if ts > t or t - ts > _HISTORY_S:
                    continue
                denom = float(np.dot(ref, ref))
                if denom < 1e-12:
                    continue
                alpha = max(0.0, float(np.dot(bands, ref)) / denom)
                residual = np.maximum(bands - alpha * ref, 0.0)
                ratio = float(residual.sum()) / total
                if ratio < best_res:
                    best_res, best_level = ratio, ref_level
            self._last_sim = 1.0 - best_res

            if best_level <= 0.0:
                return True

            warming = len(self._residuals) < _WARMUP
            speech = (not warming) and best_res >= self.threshold

            if speech:
                self._run += 1
                if self._run > _RELEARN_RUN:
                    # Nobody talks over an assistant for seconds on end: the room
                    # changed (volume, headphones). Characterise it again.
                    self._residuals.clear()
                    self._run = 0
                    return False
                return True

            self._run = 0
            self._residuals.append(best_res)
            if len(self._residuals) > _FLOOR_WINDOW:
                del self._residuals[:-_FLOOR_WINDOW]
            if len(self._residuals) >= _WARMUP:
                self._floor = float(np.percentile(self._residuals, _FLOOR_Q))
                self._head = float(np.percentile(self._residuals, _HEAD_Q))
            if warming:
                return False

            if best_res <= self._floor * 1.15 and best_level > 0.05:
                obs = level / max(best_level, 1e-6)
                self._gain += (min(obs, 3.0) - self._gain) * 0.08
                self._seen = min(self._seen + 1, 999)
            return False
        except Exception:  # noqa: BLE001 - any doubt: do not interrupt
            return False
