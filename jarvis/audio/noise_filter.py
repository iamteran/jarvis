"""Lightweight noise filtering for microphone input.

Applies a high-pass filter to remove low-frequency rumble (fans, HVAC, traffic)
and optional spectral gating for stationary noise. Designed to run on every
audio block with minimal CPU overhead on a 2-core laptop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy.signal import butter, sosfilt

from ..config import CONFIG

log = logging.getLogger("audio.noise_filter")

# High-pass filter to kill sub-100 Hz rumble (laptop fans, HVAC, desk vibration)
# 120 Hz is a good default: below voiced speech fundamentals, above fan noise.
_HP_DEFAULT_HZ = 120
_HP_ORDER = 2  # 2nd order = 12 dB/oct, gentle enough to not distort voice


@dataclass(frozen=True)
class NoiseFilterConfig:
    enabled: bool = True
    highpass_hz: int = _HP_DEFAULT_HZ
    # Spectral gate (optional, more CPU). Disable on slow machines.
    spectral_gate_enabled: bool = False
    spectral_gate_threshold_db: float = -35.0  # floor in dB
    spectral_gate_smoothing: float = 0.85      # 0..1, higher = slower adaptation


class NoiseFilter:
    """Real-time noise filter for 16 kHz mono int16 audio blocks."""

    def __init__(self, cfg: NoiseFilterConfig | None = None) -> None:
        self.cfg = cfg or NoiseFilterConfig()
        self._sos = None
        self._zi = None  # filter state for continuous streaming
        self._noise_profile = None  # for spectral gate
        if self.cfg.enabled and self.cfg.highpass_hz > 0:
            self._sos = self._design_highpass(self.cfg.highpass_hz, CONFIG.sample_rate)
            self._zi = np.zeros((self._sos.shape[0], 2), dtype=np.float32)

    @staticmethod
    @lru_cache(maxsize=8)
    def _design_highpass(cutoff_hz: int, sample_rate: int):
        """Design a Butterworth high-pass SOS filter."""
        nyq = 0.5 * sample_rate
        normal = cutoff_hz / nyq
        return butter(_HP_ORDER, normal, btype="highpass", output="sos")

    def process(self, block: np.ndarray) -> np.ndarray:
        """Filter one block in-place and return it (int16 -> float32 -> int16)."""
        if not self.cfg.enabled:
            return block

        # Convert int16 -> float32 [-1, 1]
        x = block.astype(np.float32) / 32768.0

        # High-pass filter (streaming, maintains state)
        if self._sos is not None:
            x, self._zi = sosfilt(self._sos, x, zi=self._zi)

        # Optional spectral gate for stationary noise (fan hum, etc.)
        if self.cfg.spectral_gate_enabled:
            x = self._spectral_gate(x)

        # Back to int16, clipping just in case
        np.clip(x, -1.0, 1.0, out=x)
        return np.round(x * 32768.0).astype(np.int16)

    def _spectral_gate(self, x: np.ndarray) -> np.ndarray:
        """Simple spectral gate: estimate noise floor per FFT bin, attenuate below threshold.

        This is a very lightweight version of spectral subtraction. Not perfect but
        helps with constant fan hum. Runs on each block independently for low latency.
        """
        # Short FFT (block is 80 ms = 1280 samples at 16 kHz)
        n_fft = 512  # power of 2 >= block size
        X = np.fft.rfft(x, n=n_fft)
        mag = np.abs(X)
        phase = np.angle(X)

        # Initialize noise profile from first few seconds of silence
        if self._noise_profile is None:
            self._noise_profile = mag.copy()
        else:
            # Update noise profile only when signal is below threshold (assumed noise)
            frame_energy = np.mean(mag)
            thresh = 10 ** (self.cfg.spectral_gate_threshold_db / 20.0)
            if frame_energy < thresh:
                alpha = self.cfg.spectral_gate_smoothing
                self._noise_profile = alpha * self._noise_profile + (1 - alpha) * mag

        # Spectral gate: attenuate bins where signal is close to noise floor
        # Gain = max(0, (mag - noise) / (mag + eps))
        eps = 1e-10
        gain = np.maximum(0.0, (mag - self._noise_profile) / (mag + eps))
        # Smooth the gain to avoid musical noise
        gain = 0.5 * gain + 0.5 * np.maximum.accumulate(gain)

        Y = gain * mag * np.exp(1j * phase)
        y = np.fft.irfft(Y, n=n_fft)
        return y[:len(x)]  # trim to original length

    def reset(self) -> None:
        """Reset filter state (call when stream restarts)."""
        if self._zi is not None:
            self._zi.fill(0.0)
        self._noise_profile = None


def make_noise_filter() -> NoiseFilter:
    """Factory using config env vars (with sensible defaults)."""
    cfg = NoiseFilterConfig(
        enabled=_s("JARVIS_NOISE_FILTER", "1") not in ("0", "false", "False"),
        highpass_hz=_i("JARVIS_NOISE_FILTER_HP_HZ", _HP_DEFAULT_HZ),
        spectral_gate_enabled=_s("JARVIS_NOISE_FILTER_SPECTRAL", "0") not in ("0", "false", "False"),
        spectral_gate_threshold_db=_f("JARVIS_NOISE_FILTER_THRESH_DB", -35.0),
        spectral_gate_smoothing=_f("JARVIS_NOISE_FILTER_SMOOTH", 0.85),
    )
    if cfg.enabled:
        log.info("noise filter: highpass=%d Hz, spectral_gate=%s",
                 cfg.highpass_hz, cfg.spectral_gate_enabled)
    return NoiseFilter(cfg)


# ---- helpers mirroring config.py ----
def _s(name: str, default: str = "") -> str:
    import os
    return (os.getenv(name) or default).strip()


def _f(name: str, default: float) -> float:
    try:
        return float(_s(name) or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(float(_s(name) or default))
    except ValueError:
        return default
