"""Fish Audio TTS — speaking in your cloned Jarvis voice.

Fish returns a complete mp3 per sentence; ffmpeg decodes it to raw PCM in memory so
playback needs no temp files. A second decoder (edge-tts/SAPI) can slot in behind the
same `synthesize_pcm` interface later.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from array import array

import numpy as np
import requests

from ..config import CONFIG

log = logging.getLogger("tts.fish")

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


class TTSError(RuntimeError):
    pass


def decode_to_pcm(audio_bytes: bytes, rate: int) -> np.ndarray:
    """Decode any ffmpeg-readable audio (mp3/wav/ogg) to float32 mono PCM."""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-ac", "1", "-ar", str(rate), "pipe:1"],
        input=audio_bytes, capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise TTSError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return pcm


def load_sound_file(path: str, rate: int = CONFIG.playback_rate) -> np.ndarray:
    """Load an audio file (mp3/wav/ogg) from disk and decode to float32 mono PCM."""
    from pathlib import Path
    data = Path(path).read_bytes()
    return decode_to_pcm(data, rate)


class FishTTS:
    URL = "https://api.fish.audio/v1/tts"

    def __init__(self, api_key: str | None = None, voice_id: str | None = None,
                 model: str | None = None) -> None:
        self.api_key = api_key or CONFIG.fish_api_key
        self.voice_id = voice_id or CONFIG.fish_voice_id
        self.model = model or CONFIG.fish_model
        if not self.api_key:
            raise TTSError("FISH_API_KEY is missing; set it in .env to use Fish Audio")
        if not self.voice_id:
            raise TTSError("FISH_VOICE_ID is missing; set it in .env to use Fish Audio")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "model": self.model,
        })
        self.session.headers["Authorization"] = "Bearer " + self.api_key
        self.last_latency_ms = 0.0

    def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            return b""
        payload = {
            "text": text,
            "reference_id": self.voice_id,
            "format": "mp3",
            "mp3_bitrate": 128,
            "chunk_length": 200,
            "normalize": True,
        }
        delay = 1.0
        last_error = ""
        for attempt in range(3):
            started = time.perf_counter()
            try:
                response = self.session.post(self.URL, json=payload, timeout=CONFIG.tts_timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if response.status_code == 200 and len(response.content) > 512:
                    self.last_latency_ms = (time.perf_counter() - started) * 1000
                    return response.content
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            if attempt < 2:
                log.warning("Fish TTS failed (%s), retrying in %.1fs", last_error, delay)
                time.sleep(delay)
                delay *= 2
        raise TTSError(f"Fish Audio synthesis failed: {last_error}")

    def synthesize_pcm(self, text: str, rate: int = CONFIG.playback_rate) -> np.ndarray:
        return decode_to_pcm(self.synthesize(text), rate)
