"""Sentence-level speech pipeline: synthesise the next line while the current one plays.

This is what makes Jarvis feel immediate — the first sentence starts speaking while the
rest is still being generated upstream.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

import numpy as np

try:
    from .fish import FishTTS, TTSError
except Exception:  # pragma: no cover - fallback when optional TTS packages are unavailable
    FishTTS = None  # type: ignore[assignment]
    TTSError = Exception

from .player import Player

log = logging.getLogger("tts.speaker")

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[;:])\s+")
_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "st.", "e.g.", "i.e.", "vs.", "etc.", "a.m.", "p.m."}

# Emoji and special characters that TTS engines mispronounce or read as literal text.
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+", flags=re.UNICODE,
)
_SYMBOL_MAP = [
    (re.compile(r"\*+"), ""),           # asterisks (bold/italic markdown)
    (re.compile(r"#+\s*"), ""),         # markdown headings
    (re.compile(r"```[^`]*```", re.S), ""),  # code blocks
    (re.compile(r"`[^`]*`"), ""),       # inline code
    (re.compile(r"\b(\d+)%"), r"\1 percent"),
    (re.compile(r"\$([\d,.]+)"), r"\1 dollars"),
    (re.compile(r"&"), " and "),
    (re.compile(r"@"), " at "),
    (re.compile(r"[\u2022\u2023\u25E6\u2043\u2219]"), ""),  # bullet points
    (re.compile(r"\s{2,}"), " "),       # collapse whitespace
]


def clean_for_speech(text: str) -> str:
    """Strip content that TTS engines cannot pronounce gracefully."""
    text = _EMOJI_RE.sub("", text or "")
    for pattern, replacement in _SYMBOL_MAP:
        text = pattern.sub(replacement, text)
    return text.strip()


def split_sentences(text: str, max_len: int = 240) -> list[str]:
    """Split into speakable chunks without breaking abbreviations mid-sentence."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    raw = _SENTENCE_END.split(text)
    chunks: list[str] = []
    current = ""
    for piece in raw:
        piece = piece.strip()
        if not piece:
            continue
        candidate = f"{current} {piece}".strip() if current else piece
        if current and current.lower().rstrip() in _ABBREV:
            current = candidate
            continue
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            while len(piece) > max_len:
                cut = piece.rfind(" ", 0, max_len)
                cut = cut if cut > 40 else max_len
                chunks.append(piece[:cut].strip())
                piece = piece[cut:].strip()
            current = piece
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


class Speaker:
    def __init__(self, tts: FishTTS | None = None, player: Player | None = None) -> None:
        if tts is None:
            if FishTTS is None:
                raise RuntimeError("FishTTS is unavailable; install the required TTS dependencies.")
            tts = FishTTS()
        self.tts = tts
        self.player = player or Player()
        self._stop = threading.Event()

    @property
    def is_speaking(self) -> bool:
        return self.player.is_playing

    def stop(self) -> None:
        self._stop.set()
        self.player.stop()

    def say(self, text: str, on_first_audio=None, on_chunk=None) -> bool:  # noqa: ANN001
        """Speak `text`. Returns False if interrupted, True if it finished.

        Every path out of here is bounded: a synthesis failure, a stuck vendor API or a
        dead producer thread must never leave the assistant frozen in "speaking" with no
        audio — which is exactly what happened once and looked like "Jarvis is silent".
        """
        text = clean_for_speech(text)
        sentences = split_sentences(text)
        if not sentences:
            return True
        self._stop.clear()
        pipeline: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=6)

        def produce() -> None:
            try:
                for sentence in sentences:
                    if self._stop.is_set():
                        break
                    try:
                        pcm = self.tts.synthesize_pcm(sentence, self.player.rate)
                    except Exception as exc:  # noqa: BLE001 - never kill the producer
                        log.error("synthesis failed: %s", exc)
                        continue
                    while not self._stop.is_set():
                        try:
                            pipeline.put(pcm, timeout=0.25)
                            break
                        except queue.Full:
                            continue
            except Exception:  # noqa: BLE001
                log.exception("tts producer crashed")
            finally:
                try:
                    pipeline.put(None, timeout=2.0)
                except queue.Full:
                    pass

        producer = threading.Thread(target=produce, name="tts-producer", daemon=True)
        producer.start()
        # Generous cap: synthesis plus playback. Long texts get more room.
        deadline = time.monotonic() + max(25.0, len(text) * 0.15 + 20.0)
        first = True
        timed_out = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.error("speech timed out after %.0f s; abandoning", deadline - time.monotonic())
                    timed_out = True
                    self._stop.set()
                    break
                try:
                    item = pipeline.get(timeout=min(remaining, 2.0))
                except queue.Empty:
                    continue
                if item is None:
                    break
                if first:
                    first = False
                    if on_first_audio is not None:
                        try:
                            on_first_audio()
                        except Exception:  # noqa: BLE001
                            pass
                if self._stop.is_set() or not self.player.play(item, on_chunk=on_chunk):
                    self._stop.set()
                    break
        finally:
            producer.join(timeout=1.0)
        return not self._stop.is_set() and not timed_out
