"""Voice activity detection and utterance recording.

Energy VAD with an adaptive noise floor: no model download, no compiler, works on a
laptop fan in the background. Silero could be dropped in later behind the same API.
"""
from __future__ import annotations

import logging

import numpy as np

from ..config import CONFIG

log = logging.getLogger("audio.vad")

INT16_SCALE = 1.0 / 32768.0


def rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    x = block.astype(np.float32) * INT16_SCALE
    return float(np.sqrt(np.mean(x * x)))


class EnergyVAD:
    """Speech/no-speech decision that adapts to the room's noise floor.

    Thresholds are deliberately low: a laptop microphone with automatic gain control can
    deliver ordinary speech at an RMS of only 0.02, so a "sensible" 0.02 threshold makes
    the assistant deaf. The floor tracking does the real work; the absolute value is only
    a sanity floor.
    """

    def __init__(self, threshold: float = CONFIG.speech_rms,
                 noise_mult: float = CONFIG.noise_mult,
                 noise_ceiling: float = CONFIG.noise_ceiling) -> None:
        self.threshold = threshold
        self.noise_mult = noise_mult
        self.noise_ceiling = noise_ceiling
        self.noise = 0.0030        # rolling estimate of background level
        self.last_rms = 0.0
        self._calibrated_frames = 0

    def is_speech(self, block: np.ndarray, ongoing: bool = False) -> bool:
        """Is this block speech?

        `ongoing=False` is the *onset* test: strong enough to start an utterance.
        `ongoing=True` is the *continuation* test: a lower bar, so a person who
        trails off softly is not cut off mid-sentence. This two-threshold
        (hysteresis) split is what keeps room noise from holding a recording open.
        """
        level = rms(block)
        self.last_rms = level
        # learn the noise floor from quiet blocks only; track down quickly, up slowly
        if level < self.noise * 1.8:
            self.noise = 0.90 * self.noise + 0.10 * level
        elif level < self.noise:
            self.noise = 0.97 * self.noise + 0.03 * level
        self.noise = max(self.noise, 0.0012)
        # Cap the floor: a room with music or a fan can push it above quiet speech, and
        # then the assistant goes deaf exactly when it needs to hear the user.
        self.noise = min(self.noise, self.noise_ceiling)
        self._calibrated_frames += 1

        if ongoing:
            # Breaths and low-level room noise must not keep an utterance alive.
            hold = max(self.threshold * 0.65, self.noise * 1.2)
            return level > hold
        return level > max(self.threshold, self.noise * self.noise_mult)

    @property
    def silence_level(self) -> float:
        """Level below which a started utterance counts as finished."""
        return max(self.threshold * 0.65, self.noise * 1.2)


class Recorder:
    """Collects one utterance: pre-roll, speech, then silence to the endpoint.

    Feed it consecutive audio blocks; `done` flips True at end-of-speech.
    """

    def __init__(self, vad: EnergyVAD, preroll: list[np.ndarray],
                 block_ms: int = CONFIG.block_ms,
                 end_silence_ms: int = CONFIG.end_silence_ms,
                 max_s: float = CONFIG.max_utterance_s,
                 start_timeout_ms: int = 0) -> None:
        self.vad = vad
        self.blocks: list[np.ndarray] = list(preroll)
        self.block_ms = block_ms
        self.end_silence_ms = end_silence_ms
        self.max_ms = int(max_s * 1000)
        self.start_timeout_ms = start_timeout_ms or CONFIG.listen_start_timeout_ms
        self.elapsed_ms = 0
        self.silence_ms = 0
        self.speech_ms = 0
        self.started = False
        self.aborted = False
        self.peak = 0.0

    def feed(self, block: np.ndarray) -> bool:
        self.blocks.append(block)
        self.elapsed_ms += self.block_ms
        level = rms(block)
        self.peak = max(self.peak, level)

        if self.vad.is_speech(block, ongoing=self.started):
            self.started = True
            self.speech_ms += self.block_ms
            self.silence_ms = 0
        elif self.started:
            self.silence_ms += self.block_ms
        elif self.elapsed_ms >= self.start_timeout_ms:
            # Nobody spoke after the wake word — unless the mic is just quiet. If there is
            # audible sound at all, treat the utterance as started and let Whisper judge.
            if self.peak >= CONFIG.salvage_peak:
                self.started = True
                self.silence_ms = 0
            else:
                self.aborted = True
        return self.done

    @property
    def done(self) -> bool:
        if self.aborted:
            return True
        if self.started and self.silence_ms >= self.end_silence_ms:
            return True
        return self.elapsed_ms >= self.max_ms

    @property
    def usable(self) -> bool:
        """Does this utterance deserve transcribing?"""
        if self.aborted:
            return False
        if self.started and self.speech_ms >= CONFIG.min_speech_ms:
            return True
        # The VAD can be deaf to a quiet microphone. If there was clearly audible sound for
        # long enough to be speech, hand it to Whisper anyway — but never for a brief blip,
        # or a door slam becomes a phantom command.
        return self.peak >= CONFIG.salvage_peak and self.elapsed_ms >= 800

    def audio(self) -> np.ndarray:
        if not self.blocks:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(self.blocks)
        # trim trailing silence beyond the endpoint (keeps the clip small and STT fast)
        if self.silence_ms > self.end_silence_ms:
            trim_ms = self.silence_ms - self.end_silence_ms
            trim = int(trim_ms / 1000 * CONFIG.sample_rate)
            if 0 < trim < len(audio):
                audio = audio[:-trim]
        return audio
