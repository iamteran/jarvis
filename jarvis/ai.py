"""Thin, dependency-light Gemini REST client.

Uses the plain `v1beta` REST surface (the same one verified live from this machine):
streaming chat with server-sent events, function calling, and audio transcription.
No SDK, so a Google SDK release can never break Jarvis.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import time
import wave
from typing import Any, Iterator

import numpy as np
import requests

from .config import CONFIG

log = logging.getLogger("gemini")

BASE = "https://generativelanguage.googleapis.com/v1beta"
RETRYABLE = {429, 500, 502, 503, 504}
# Fail fast on a bad link. The old 3-attempt/1.2 s+2.4 s ladder, multiplied by three
# models, produced 40-60 s turns that ended in an apology. One quick retry is enough for
# a transient reset; anything worse is the next model's problem.
MAX_ATTEMPTS = 2
RETRY_DELAY = 0.6


class GeminiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.detail = message


def pcm_to_wav(pcm: np.ndarray, rate: int = 16000) -> bytes:
    """Wrap raw int16 mono PCM in a WAV container (what the API wants to hear)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
    return buf.getvalue()


def pcm_to_ogg(pcm: np.ndarray, rate: int = 16000) -> bytes | None:
    """Opus-in-Ogg is ~12x smaller than WAV — critical on a slow or unstable link."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "s16le",
             "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
             "-c:a", "libopus", "-b:a", "16k", "-application", "voip",
             "-f", "ogg", "pipe:1"],
            input=np.asarray(pcm, dtype=np.int16).tobytes(),
            capture_output=True, timeout=40,
        )
    except Exception:  # noqa: BLE001
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def extract_text(data: dict[str, Any]) -> str:
    out: list[str] = []
    for cand in data.get("candidates", []) or []:
        for part in cand.get("content", {}).get("parts", []) or []:
            if isinstance(part.get("text"), str):
                out.append(part["text"])
    return "".join(out)


def first_function_call(data: dict[str, Any]) -> dict[str, Any] | None:
    calls = function_calls(data)
    return calls[0] if calls else None


def response_parts(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The model's raw parts from the first candidate.

    Gemini 3 attaches a `thoughtSignature` to each `functionCall` part and rejects the
    follow-up request (HTTP 400) unless that signature is echoed back verbatim. Building
    fresh `functionCall` parts loses it, so tool loops must replay these parts as-is.
    """
    for cand in data.get("candidates", []) or []:
        parts = cand.get("content", {}).get("parts", []) or []
        if parts:
            return [part for part in parts if isinstance(part, dict)]
    return []


def function_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every `functionCall` part in a Gemini response.

    A single response may contain several calls (e.g. "open Chrome and check the
    calendar"), and older code kept only the first, silently dropping the rest.
    """
    calls: list[dict[str, Any]] = []
    for cand in data.get("candidates", []) or []:
        for part in cand.get("content", {}).get("parts", []) or []:
            call = part.get("functionCall")
            if isinstance(call, dict):
                calls.append(call)
    return calls


class DeepSeek:
    """OpenAI-compatible fallback brain (https://api.deepseek.com).

    Takes the same Gemini-style `contents` list as `Gemini.stream_text` so the Brain can
    swap providers without caring about message shape.
    """

    BASE = "https://api.deepseek.com"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or CONFIG.deepseek_api_key
        self.model = model or CONFIG.deepseek_model
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    @staticmethod
    def _to_messages(contents: list[dict], system: str | None) -> list[dict]:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        for turn in contents:
            role = "assistant" if turn.get("role") == "model" else turn.get("role", "user")
            text = "".join(p.get("text", "") for p in turn.get("parts", []) if isinstance(p, dict))
            if text:
                messages.append({"role": role, "content": text})
        return messages

    def stream_text(self, model: str, contents: list[dict], *, system: str | None = None,
                    temperature: float = 0.75, max_output_tokens: int = 400,
                    timeout: float = 90.0) -> Iterator[str]:
        payload = {
            "model": model or self.model,
            "messages": self._to_messages(contents, system),
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        with self.http.post(f"{self.BASE}/chat/completions", json=payload,
                            stream=True, timeout=timeout) as response:
            if response.status_code != 200:
                raise GeminiError(response.status_code, response.text[:300])
            for raw in response.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                chunk = raw[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                for choice in obj.get("choices", []) or []:
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        yield delta

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *,
             model: str | None = None,
             temperature: float = 0.7, max_output_tokens: int = 600,
             timeout: float = 90.0) -> dict:
        """Non-streaming completion with optional tool calling."""
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = self.http.post(f"{self.BASE}/chat/completions", json=payload, timeout=timeout)
        if response.status_code != 200:
            raise GeminiError(response.status_code, response.text[:300])
        return response.json()


class Groq:
    """OpenAI-compatible provider with a very fast free tier (Llama + Whisper).

    Optional: only used when GROQ_API_KEY is set. Same `contents` shape as the others,
    so the Brain can put it anywhere in the chain.
    """

    BASE = "https://api.groq.com/openai/v1"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or CONFIG.groq_api_key
        self.model = model or CONFIG.groq_model
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    @staticmethod
    def _to_messages(contents: list[dict], system: str | None) -> list[dict]:
        return DeepSeek._to_messages(contents, system)

    def stream_text(self, model: str, contents: list[dict], *, system: str | None = None,
                    temperature: float = 0.75, max_output_tokens: int = 400,
                    timeout: float = 60.0) -> Iterator[str]:
        payload = {
            "model": model or self.model,
            "messages": self._to_messages(contents, system),
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        with self.http.post(f"{self.BASE}/chat/completions", json=payload,
                            stream=True, timeout=timeout) as response:
            if response.status_code != 200:
                raise GeminiError(response.status_code, response.text[:300])
            for raw in response.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                chunk = raw[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                for choice in obj.get("choices", []) or []:
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        yield delta

    def chat(self, messages: list[dict], tools: list[dict] | None = None, *,
             model: str | None = None,
             temperature: float = 0.7, max_output_tokens: int = 600,
             timeout: float = 90.0) -> dict:
        """Non-streaming completion with optional tool calling (OpenAI-compatible)."""
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = self.http.post(f"{self.BASE}/chat/completions", json=payload, timeout=timeout)
        if response.status_code != 200:
            raise GeminiError(response.status_code, response.text[:300])
        return response.json()

    def transcribe(self, pcm: np.ndarray, rate: int = 16000, *,
                   model: str | None = None) -> str:
        """Whisper transcription — typically ~1 s on the free tier."""
        wav = pcm_to_wav(pcm, rate)
        response = self.http.post(
            f"{self.BASE}/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},  # no JSON content-type
            files={"file": ("speech.wav", wav, "audio/wav")},
            data={"model": model or CONFIG.groq_stt_model, "response_format": "json",
                  "temperature": "0", "language": "en"},
            timeout=90,
        )
        if response.status_code != 200:
            raise GeminiError(response.status_code, response.text[:300])
        return (response.json().get("text") or "").strip()


class Grok(DeepSeek):
    """Grok through an OpenAI-compatible gateway (default: https://api.openlux.ai/v1).

    The gateway speaks the same wire format as DeepSeek and Groq, so it reuses those
    methods and only swaps the host, key and model. Configure with GROK_API_KEY,
    GROK_BASE_URL and GROK_MODEL.
    """

    BASE = "https://api.openlux.ai/v1"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str | None = None) -> None:
        self.BASE = (base_url or CONFIG.grok_base_url or self.BASE).rstrip("/")
        super().__init__(api_key or CONFIG.grok_api_key, model or CONFIG.grok_model)


class Gemini:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or CONFIG.gemini_api_key
        self.http = requests.Session()
        self.http.headers.update(
            {"x-goog-api-key": self.api_key, "content-type": "application/json"}
        )

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _payload(contents: list[dict], system: str | None, tools: list[dict] | None,
                 temperature: float, max_output_tokens: int) -> dict:
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [{"functionDeclarations": tools}]
        return payload

    def _check(self, response: requests.Response) -> None:
        if response.status_code == 200:
            return
        message = response.text
        try:
            message = response.json().get("error", {}).get("message", message)
        except ValueError:
            pass
        raise GeminiError(response.status_code, message)

    # ---------------------------------------------------------------------- text
    def generate(self, model: str, contents: list[dict], *, system: str | None = None,
                 tools: list[dict] | None = None, temperature: float = 0.6,
                 max_output_tokens: int = 1024, timeout: float = 60.0) -> dict:
        payload = self._payload(contents, system, tools, temperature, max_output_tokens)
        delay = RETRY_DELAY
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self.http.post(
                    f"{BASE}/models/{model}:generateContent", json=payload, timeout=timeout
                )
                self._check(response)
                return response.json()
            except GeminiError as exc:
                last = exc
                if exc.status not in RETRYABLE or attempt == MAX_ATTEMPTS - 1:
                    raise
            except requests.RequestException as exc:
                last = exc
                if attempt == MAX_ATTEMPTS - 1:
                    raise
            log.warning("Gemini call failed (%s), retrying in %.1fs", last, delay)
            time.sleep(delay)
            delay *= 2
        raise last if last else RuntimeError("Gemini call failed")  # pragma: no cover

    def stream_text(self, model: str, contents: list[dict], *, system: str | None = None,
                    tools: list[dict] | None = None, temperature: float = 0.7,
                    max_output_tokens: int = 1024, timeout: float = 30.0) -> Iterator[str]:
        """Yield text deltas as Gemini produces them."""
        payload = self._payload(contents, system, tools, temperature, max_output_tokens)
        delay = RETRY_DELAY
        for attempt in range(MAX_ATTEMPTS):
            try:
                with self.http.post(
                    f"{BASE}/models/{model}:streamGenerateContent?alt=sse",
                    json=payload, stream=True, timeout=timeout,
                ) as response:
                    if response.status_code != 200:
                        self._check(response)
                    for raw in response.iter_lines(decode_unicode=True):
                        if not raw or not raw.startswith("data:"):
                            continue
                        chunk = raw[5:].strip()
                        if not chunk or chunk == "[DONE]":
                            continue
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        for cand in obj.get("candidates", []) or []:
                            for part in cand.get("content", {}).get("parts", []) or []:
                                text = part.get("text")
                                if isinstance(text, str) and text:
                                    yield text
                return
            except (GeminiError, requests.RequestException) as exc:
                status = getattr(exc, "status", None)
                if attempt == MAX_ATTEMPTS - 1 or (status is not None and status not in RETRYABLE):
                    raise
                log.warning("stream failed (%s), retrying in %.1fs", exc, delay)
                time.sleep(delay)
                delay *= 2

    # --------------------------------------------------------------------- ears
    def transcribe(self, pcm: np.ndarray, rate: int = 16000, *,
                   model: str | None = None, fallback: str | None = None) -> str:
        """Speech to text. Sends compressed Opus (or WAV if ffmpeg is missing)."""
        opus = pcm_to_ogg(pcm, rate)
        if opus:
            audio_b64 = base64.b64encode(opus).decode("ascii")
            mime = "audio/ogg"
            log.debug("uploading %d bytes of opus (wav would be %d)", len(opus), len(pcm) * 2)
        else:
            audio_b64 = base64.b64encode(pcm_to_wav(pcm, rate)).decode("ascii")
            mime = "audio/wav"
        prompt = (
            "Transcribe exactly what the speaker says. Output only their words, "
            "with normal punctuation. If there is no intelligible speech, output nothing."
        )
        contents = [{
            "role": "user",
            "parts": [
                {"text": prompt},
                {"inlineData": {"mimeType": mime, "data": audio_b64}},
            ],
        }]
        models: list[str] = []
        for candidate in (model or CONFIG.stt_model, fallback or CONFIG.stt_fallback_model, *CONFIG.stt_chain):
            if candidate and candidate not in models:
                models.append(candidate)
        for candidate in models:
            try:
                data = self.generate(candidate, contents, temperature=0.0,
                                     max_output_tokens=512, timeout=30)
                text = extract_text(data).strip()
                if text:
                    log.debug("transcribed with %s", candidate)
                    return text
                log.debug("model %s returned empty transcription", candidate)
            except GeminiError as exc:
                if exc.status == 429:
                    log.info("transcription model %s is out of quota; trying next", candidate)
                else:
                    log.warning("transcription via %s failed: %s", candidate, exc)
        return ""
