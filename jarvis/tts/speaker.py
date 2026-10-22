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

from .fish import FishTTS, TTSError
from .player import Player

log = logging.getLogger("tts.speaker")

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[;:])\s+")
_ABBREV = {"mr.", "mrs.", "ms.", "dr.", "st.", "e.g.", "i.e.", "vs.", "etc.", "a.m.", "p.m."}


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
        self.tts = tts or FishTTS()
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
