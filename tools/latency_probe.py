"""Latency probe: find the fastest Gemini config and Fish TTS settings from THIS machine.

Run:  .venv\\Scripts\\python.exe tools\\latency_probe.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from jarvis.config import CONFIG  # noqa: E402

BASE = "https://generativelanguage.googleapis.com/v1beta"
H = {"x-goog-api-key": CONFIG.gemini_api_key, "content-type": "application/json"}


def stream_probe(label: str, model: str, config_extra: dict, prompt: str) -> None:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": "You are Jarvis, a British butler. Answer in one short spoken sentence."}]},
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 400, **config_extra},
    }
    started = time.perf_counter()
    first = None
    text = ""
    try:
        with requests.post(f"{BASE}/models/{model}:streamGenerateContent?alt=sse",
                           headers=H, json=payload, stream=True, timeout=120) as r:
            if r.status_code != 200:
                print(f"  {label:<48} HTTP {r.status_code}: {r.text[:120]}")
                return
            for raw in r.iter_lines(decode_unicode=True):
                if not raw or not raw.startswith("data:"):
                    continue
                chunk = raw[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                for cand in obj.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if part.get("thought"):
                            continue
                        if part.get("text"):
                            if first is None:
                                first = time.perf_counter() - started
                            text += part["text"]
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<48} ERROR {exc}")
        return
    total = time.perf_counter() - started
    ttfb = f"{first * 1000:6.0f} ms" if first else "     -    "
    print(f"  {label:<48} ttfb {ttfb}  total {total * 1000:6.0f} ms  {text.strip()[:60]!r}")


def fish_probe() -> None:
    url = "https://api.fish.audio/v1/tts"
    text = "Good evening, sir. All systems are online and at your service."
    for model in ("s2.1-pro-free", "s2.1-pro"):
        for latency in ("normal", "balanced"):
            body = {
                "text": text, "reference_id": CONFIG.fish_voice_id, "format": "mp3",
                "mp3_bitrate": 128, "latency": latency, "normalize": True,
            }
            started = time.perf_counter()
            try:
                r = requests.post(url, headers={
                    "Authorization": f"Bearer {CONFIG.fish_api_key}",
                    "Content-Type": "application/json", "model": model,
                }, json=body, timeout=90)
                ms = (time.perf_counter() - started) * 1000
                if r.status_code == 200:
                    print(f"  Fish {model:<16} latency={latency:<9} {ms:6.0f} ms  {len(r.content) // 1024} KB  {len(text)} chars")
                else:
                    print(f"  Fish {model:<16} latency={latency:<9} HTTP {r.status_code}: {r.text[:100]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  Fish {model:<16} latency={latency:<9} ERROR {exc}")


def main() -> None:
    print("\n=== Gemini: first-token latency (what the user actually feels) ===")
    prompt = "In one sentence: what's the weather usually like in Nairobi in October?"
    stream_probe("3.8-flash default", "gemini-3.8-flash", {}, prompt)
    stream_probe("3.8-flash thinkingBudget=0", "gemini-3.8-flash", {"thinkingConfig": {"thinkingBudget": 0}}, prompt)
    stream_probe("3.8-flash thinkingLevel=low", "gemini-3.8-flash", {"thinkingConfig": {"thinkingLevel": "low"}}, prompt)
    stream_probe("flash-latest default", "gemini-flash-latest", {}, prompt)
    stream_probe("3.5-flash default", "gemini-3.5-flash", {}, prompt)
    stream_probe("3.1-flash-lite default", "gemini-3.1-flash-lite", {}, prompt)

    print("\n=== Fish TTS: time to complete audio ===")
    fish_probe()
    print()


if __name__ == "__main__":
    main()
