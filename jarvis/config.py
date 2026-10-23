"""Central configuration. Every tunable lives here so the rest of the code stays clean.

Values come from environment variables (loaded from `.env`) with sensible defaults,
so Jarvis runs out of the box but can be re-tuned without touching code.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = DATA / "logs"
ASSETS = ROOT / "assets"
CONCEPTS = ASSETS / "concepts"

for _d in (DATA, LOGS):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


def _s(name: str, default: str = "") -> str:
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


@dataclass(frozen=True)
class Config:
    # ---- credentials ----
    gemini_api_key: str = _s("GEMINI_API_KEY")
    deepseek_api_key: str = _s("DEEPSEEK_API_KEY")
    groq_api_key: str = _s("GROQ_API_KEY")
    # Grok through an OpenAI-compatible gateway (api.openlux.ai by default).
    grok_api_key: str = _s("GROK_API_KEY")
    grok_base_url: str = _s("GROK_BASE_URL", "https://api.openlux.ai/v1")
    fish_api_key: str = _s("FISH_API_KEY")
    fish_voice_id: str = _s("FISH_VOICE_ID")

    # ---- models ----
    deepseek_model: str = _s("DEEPSEEK_MODEL", "deepseek-chat")
    groq_model: str = _s("GROQ_MODEL", "llama-3.3-70b-versatile")
    groq_stt_model: str = _s("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    grok_model: str = _s("GROK_MODEL", "grok-4.7")
    # provider:model pairs, tried in order when one is rate-limited.
    # Only Gemini models that are verified on the free tier are listed here.
    # gemini-3.8-flash is the current recommended flash model (gemini-2.0-flash retired).
    llm_chain: tuple[tuple[str, str], ...] = tuple(
        (part.split(":", 1)[0].strip(), part.split(":", 1)[1].strip())
        for part in _s("JARVIS_LLM_CHAIN",
                       "groq:llama-3.3-70b-versatile,deepseek:deepseek-chat,"
                       "gemini:gemini-2.0-flash-lite,gemini:gemini-2.0-flash").split(",")
        if ":" in part
    )
    chat_model: str = _s("JARVIS_CHAT_MODEL", llm_chain[0][1] if llm_chain else "gemini-2.0-flash-lite")
    chat_fallbacks: tuple[str, ...] = tuple(
        m.strip() for m in _s("JARVIS_CHAT_FALLBACKS", "gemini-2.0-flash-lite,gemini-2.0-flash").split(",")
        if m.strip()
    )
    # STT models — flash-lite is fastest; fall back through progressively heavier models.
    stt_model: str = _s("JARVIS_STT_MODEL", "gemini-2.0-flash-lite")
    stt_fallback_model: str = _s("JARVIS_STT_FALLBACK", "gemini-2.0-flash")
    # Models tried in order for hearing; each has its own daily free-tier quota, so a
    # 429 on one rolls to the next instead of ending the conversation.
    stt_chain: tuple[str, ...] = tuple(
        m.strip() for m in _s(
            "JARVIS_STT_CHAIN",
            "gemini-2.0-flash-lite,gemini-2.0-flash,gemini-1.5-flash-latest,gemini-1.5-flash-8b",
        ).split(",") if m.strip()
    )
    # Local Whisper (needs `pip install faster-whisper`). "auto" uses it when importable.
    # Two models: a bigger one for accuracy on real commands, a tiny one for the quick
    # "did they say Jarvis?" checks on idle speech (where speed matters and accuracy does not).
    whisper_mode: str = _s("JARVIS_WHISPER", "auto")          # auto | off
    whisper_model: str = _s("JARVIS_WHISPER_MODEL", "base.en")
    whisper_fast_model: str = _s("JARVIS_WHISPER_FAST_MODEL", "tiny.en")
    whisper_threads: int = _i("JARVIS_WHISPER_THREADS", 2)
    whisper_beam: int = _i("JARVIS_WHISPER_BEAM", 3)
    whisper_prompt: str = _s(
        "JARVIS_WHISPER_PROMPT",
        "Spoken commands to a desktop assistant named Jarvis: open notepad, "
        "open chrome, open the folder, what time is it, set the volume, "
        "click save, type hello, remind me, undo that.",
    )
    fish_model: str = _s("FISH_MODEL", "s2.1-pro-free")

    # ---- wake word ----
    wake_model: str = _s("JARVIS_WAKE_MODEL", "hey_jarvis")
    wake_threshold: float = _f("JARVIS_WAKE_THRESHOLD", 0.50)
    wake_frames: int = _i("JARVIS_WAKE_FRAMES", 3)  # consecutive frames over threshold
    # Always-ready mode: idle speech is transcribed locally and checked for the name,
    # so "Jarvis, open notepad" works even when the wake model misses it.
    asr_wake: bool = _s("JARVIS_ASR_WAKE", "1") not in ("0", "false", "False")

    # ---- audio ----
    sample_rate: int = 16000
    block_ms: int = 80                      # openWakeWord requires 80 ms frames
    mic_device: str | None = _s("JARVIS_MIC") or None
    speaker_device: str | None = _s("JARVIS_SPEAKER") or None
    # Onset thresholds. These were tuned down to 0.002/1.15/0.004 on this machine to cure
    # deafness, which made the room's own noise count as speech: silence never accumulated
    # and every utterance ran to `max_utterance_s` (measured: 21/21 clips were 10.32 s).
    # The values below sit above a normal laptop fan but well below quiet speech (~0.014).
    speech_rms: float = _f("JARVIS_SPEECH_RMS", 0.008)   # speech onset threshold
    noise_mult: float = _f("JARVIS_NOISE_MULT", 1.6)     # speech must beat the noise floor by this
    noise_ceiling: float = _f("JARVIS_NOISE_CEILING", 0.006)  # floor can never rise past this
    min_speech_ms: int = _i("JARVIS_MIN_SPEECH_MS", 160)  # below this an utterance is "not speech"
    salvage_peak: float = _f("JARVIS_SALVAGE_PEAK", 0.0035)  # still transcribe if this loud
    end_silence_ms: int = _i("JARVIS_END_SILENCE_MS", 650)
    max_utterance_s: float = _f("JARVIS_MAX_UTTERANCE_S", 8.0)
    preroll_ms: int = _i("JARVIS_PREROLL_MS", 320)
    followup_ms: int = _i("JARVIS_FOLLOWUP_MS", 12000)
    listen_start_timeout_ms: int = _i("JARVIS_LISTEN_START_MS", 1600)  # silence after wake = false alarm
    barge_in: bool = _s("JARVIS_BARGE_IN", "1") not in ("0", "false", "False")

    # ---- voice out ----
    tts_engine: str = _s("JARVIS_TTS", "auto")     # auto | piper | sapi | edge | fish
    piper_voice: str = _s("JARVIS_PIPER_VOICE", "en_GB-alan-medium")
    edge_voice: str = _s("JARVIS_EDGE_VOICE", "en-GB-RyanNeural")
    fish_budget_ms: float = _f("JARVIS_FISH_BUDGET_MS", 4500.0)
    tts_timeout: float = _f("JARVIS_TTS_TIMEOUT", 60.0)
    playback_rate: int = 24000

    # ---- brain timeouts ----
    # A voice assistant cannot sit on a dead socket for 90 s. One short attempt per model,
    # then the next model, then an apology — the caller speaks a fallback line instead.
    llm_timeout: float = _f("JARVIS_LLM_TIMEOUT", 12.0)
    llm_model_cooldown_s: float = _f("JARVIS_LLM_COOLDOWN", 60.0)

    # ---- personality ----
    user_title: str = _s("JARVIS_USER_TITLE", "sir")
    max_history_turns: int = _i("JARVIS_MAX_HISTORY", 16)

    # ---- ui ----
    orb_size: int = _i("JARVIS_ORB_SIZE", 210)
    orb_margin: int = _i("JARVIS_ORB_MARGIN", 36)
    show_orb: bool = _s("JARVIS_SHOW_ORB", "1") not in ("0", "false", "False")

    # ---- hotkeys ----
    hotkey_listen: str = _s("JARVIS_HOTKEY_LISTEN", "<ctrl>+<space>")
    hotkey_panic: str = _s("JARVIS_HOTKEY_PANIC", "<ctrl>+<alt>+<esc>")
    hotkey_mute: str = _s("JARVIS_HOTKEY_MUTE", "<ctrl>+<alt>+j")

    def validate(self) -> list[str]:
        """Return a list of problems that would stop Jarvis from working."""
        problems: list[str] = []
        if (not self.gemini_api_key and not self.deepseek_api_key and not self.groq_api_key
                and not self.grok_api_key):
            problems.append(
                "no brain: set at least one of GEMINI_API_KEY, DEEPSEEK_API_KEY, "
                "GROQ_API_KEY or GROK_API_KEY in .env"
            )
        # Fish credentials are only required if the user explicitly selected the fish engine.
        if self.tts_engine.lower() == "fish":
            if not self.fish_api_key:
                problems.append("FISH_API_KEY is missing in .env (required because JARVIS_TTS=fish)")
            if not self.fish_voice_id:
                problems.append("FISH_VOICE_ID is missing in .env (required because JARVIS_TTS=fish)")
        return problems


CONFIG = Config()
