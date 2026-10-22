"""Where does the time actually go? Network, Gemini thinking, and TTS vendor comparison."""
from __future__ import annotations

import asyncio
import socket
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from jarvis.config import CONFIG

H = {"x-goog-api-key": CONFIG.gemini_api_key, "content-type": "application/json"}


def tls_connect(host: str, port: int = 443) -> tuple[float, float]:
    t0 = time.perf_counter()
    sock = socket.create_connection((host, port), timeout=15)
    t1 = time.perf_counter()
    ctx = ssl.create_default_context()
    ctx.wrap_socket(sock, server_hostname=host).close()
    t2 = time.perf_counter()
    return (t1 - t0) * 1000, (t2 - t1) * 1000


def net_probe() -> None:
    print("\n=== Network reality check ===")
    for host in ("generativelanguage.googleapis.com", "api.fish.audio",
                 "speech.platform.bing.com", "speed.cloudflare.com"):
        try:
            tcp, tls = tls_connect(host)
            print(f"  {host:<40} tcp {tcp:6.0f} ms  tls {tls:6.0f} ms")
        except Exception as exc:  # noqa: BLE001
            print(f"  {host:<40} FAILED {exc}")

    # download speed
    try:
        t0 = time.perf_counter()
        r = requests.get("https://speed.cloudflare.com/__down?bytes=1000000", timeout=120)
        secs = time.perf_counter() - t0
        print(f"  download 1 MB: {secs:5.1f} s  -> {1000 / max(secs, 0.001):5.0f} KB/s")
    except Exception as exc:  # noqa: BLE001
        print(f"  download test failed: {exc}")


def gemini_once(label: str, model: str, extra: dict) -> None:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "One short sentence: how are you today?"}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 200, **extra},
    }
    t0 = time.perf_counter()
    first = None
    try:
        with requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse",
                           headers=H, json=payload, stream=True, timeout=120) as r:
            if r.status_code != 200:
                print(f"  {label:<42} HTTP {r.status_code} {r.text[:60]}")
                return
            for raw in r.iter_lines(decode_unicode=True):
                if raw and raw.startswith("data:"):
                    if first is None:
                        first = time.perf_counter() - t0
                    if raw.strip() == "data: [DONE]":
                        break
        print(f"  {label:<42} first {((first or 0) * 1000):6.0f} ms")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<42} ERROR {exc}")


def gemini_probe() -> None:
    print("\n=== Gemini TTFT (text models, thinking off where supported) ===")
    gemini_once("3.8-flash, thinkingBudget 0", "gemini-3.8-flash", {"thinkingConfig": {"thinkingBudget": 0}})
    gemini_once("3.8-flash, default thinking", "gemini-3.8-flash", {})
    gemini_once("3.5-flash-lite", "gemini-3.5-flash-lite", {})
    gemini_once("3.1-flash-lite, thinkingBudget 0", "gemini-3.1-flash-lite", {"thinkingConfig": {"thinkingBudget": 0}})


async def _edge_tts(text: str, voice: str) -> tuple[float, int]:
    import edge_tts

    t0 = time.perf_counter()
    total = 0
    first = None
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk.get("type") == "audio":
            if first is None:
                first = time.perf_counter() - t0
            total += len(chunk.get("data", b""))
    return (first or 0) * 1000, total


def tts_probe() -> None:
    print("\n=== Text to speech: vendor comparison (same sentence) ===")
    text = "Good evening, sir. All systems are online and at your service."
    for voice in ("en-GB-RyanNeural", "en-GB-ThomasNeural", "en-US-GuyNeural"):
        try:
            first, total = asyncio.run(_edge_tts(text, voice))
            print(f"  edge-tts {voice:<22} first {first:6.0f} ms  {total // 1024} KB")
        except Exception as exc:  # noqa: BLE001
            print(f"  edge-tts {voice:<22} ERROR {exc}")


def main() -> None:
    net_probe()
    gemini_probe()
    tts_probe()
    print()


if __name__ == "__main__":
    main()
