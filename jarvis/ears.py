"""Ears: speech to text with a preference chain.

Preference order:
  1. Groq Whisper  — ~1 s, needs GROQ_API_KEY (free tier)
  2. Gemini flash  — works on the free key, but slow when the link is congested
Compression (Opus) and retries live in the Gemini client.
"""
from __future__ import annotations

import logging
import re

import numpy as np

from .ai import Gemini, Groq
from .config import CONFIG

log = logging.getLogger("ears")

# Gemini sometimes answers with its own instruction when the clip is silent, e.g.
# "No speech detected. Output nothing." Those are not user words.
_PROMPT_ECHO = (
    "output nothing",
    "no speech detected",
    "transcribe exactly",
    "if there is no intelligible",
    "there is no intelligible speech",
)


def clean_transcript(text: str) -> str:
    """Drop instruction echoes, stray quoting, and Whisper's doubled phrases."""
    text = (text or "").strip().strip('"').strip("'").strip()
    lowered = text.lower()
    if not text:
        return ""
    if any(marker in lowered for marker in _PROMPT_ECHO):
        return ""
    if lowered in {"(silence)", "[silence]", "(no speech)", "..."}:
        return ""
    # Whisper's well-known hallucinations on noise. Acting on one would mean the assistant
    # doing something the user never asked for.
    if lowered.strip(" .!?").strip() in _HALLUCINATIONS:
        log.debug("dropped whisper hallucination: %r", text)
        return ""
    # Whisper sometimes repeats the same clause ("open the desktop, open the desktop").
    text = _collapse_repeats(text)
    return text


_HALLUCINATIONS = {
    "thank you", "thanks", "thanks for watching", "thank you for watching",
    "bye", "goodbye", "you", "the end", "please subscribe", "subscribe",
    "okay", "ok", "hmm", "mm", "uh", "ah", "so", "and",
}


def _collapse_repeats(text: str) -> str:
    parts = [p.strip() for p in re.split(r"[,;]\s*|\.\s+", text) if p.strip()]
    if len(parts) >= 2 and len(set(p.lower().rstrip(".") for p in parts)) == 1:
        return parts[0].rstrip(".") + "."
    return text


class Ears:
    def __init__(self) -> None:
        self.whisper = None
        if CONFIG.whisper_mode.lower() not in ("off", "0", "false"):
            try:
                from .asr import WhisperEars, available

                if available():
                    self.whisper = WhisperEars()
                    log.info("local whisper available (%s)", CONFIG.whisper_model)
            except Exception as exc:  # noqa: BLE001
                log.debug("whisper unavailable: %s", exc)
        self.groq = Groq() if CONFIG.groq_api_key else None
        self.gemini = Gemini()
        self.last_provider = ""

    def warm(self) -> None:
        if self.whisper is not None:
            try:
                self.whisper.warm()
            except Exception as exc:  # noqa: BLE001
                log.warning("whisper warm-up failed: %s", exc)

    def transcribe(self, pcm: np.ndarray, rate: int = CONFIG.sample_rate,
                   fast: bool = False) -> str:
        if pcm is None or len(pcm) == 0:
            return ""
        # Boost quiet microphone input before any recogniser sees it (never change rate:
        # the caller knows whether this is 16 kHz mic audio or something else).
        try:
            from .asr import normalise

            boosted = normalise(np.asarray(pcm, dtype=np.float32) / 32768.0)
            pcm = np.clip(boosted * 32768.0, -32768, 32767).astype(np.int16)
        except Exception:  # noqa: BLE001
            pass
        if self.whisper is not None and self.whisper.ready:
            try:
                text = clean_transcript(self.whisper.transcribe(pcm, rate, fast=fast))
                self.last_provider = f"whisper:{CONFIG.whisper_model}"
                # Trust the local recogniser: if it ran and heard nothing, the clip really
                # is silence. Falling through to a cloud retry here costs 30 s of waiting
                # for the same empty answer.
                return text
            except Exception as exc:  # noqa: BLE001
                log.warning("whisper failed (%s); trying next provider", exc)
        if self.groq is not None:
            try:
                text = clean_transcript(self.groq.transcribe(pcm, rate))
                if text:
                    self.last_provider = f"groq:{CONFIG.groq_stt_model}"
                    return text
                log.debug("groq heard nothing, trying gemini")
            except Exception as exc:  # noqa: BLE001
                log.warning("groq transcription failed (%s); trying gemini", exc)
        text = clean_transcript(self.gemini.transcribe(pcm, rate))
        if text:
            self.last_provider = f"gemini:{CONFIG.stt_model}"
        return text
