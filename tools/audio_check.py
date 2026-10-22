"""Prove the speaker path: device, volume, mute, synthesis, playback."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd

from jarvis.skills import system
from jarvis.tts.factory import make_tts
from jarvis.tts.player import Player


def main() -> int:
    device = sd.query_devices(kind="output")
    print(f"default output : [{device['index']}] {device['name']}")
    print(f"volume         : {system.get_volume()}")
    try:
        from pycaw.pycaw import AudioUtilities

        endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        print(f"muted          : {bool(endpoint.GetMute())}")
    except Exception as exc:  # noqa: BLE001
        print(f"muted          : unknown ({exc})")

    player = Player()
    print(f"player device  : {player.device}, {player.rate} Hz")

    print("\nplaying a 1-second 440 Hz tone ...")
    tone = (0.25 * np.sin(2 * np.pi * 440 * np.arange(player.rate) / player.rate)).astype(np.float32)
    ok = player.play(tone)
    print(f"tone played    : {ok}")

    tts = make_tts()
    print(f"\ntts engine     : {type(tts).__name__}")
    started = time.perf_counter()
    try:
        pcm = tts.synthesize_pcm("Jarvis audio check. One, two, three.")
    except Exception as exc:  # noqa: BLE001
        print(f"synthesis FAILED after {time.perf_counter() - started:.1f}s: {exc}")
        return 1
    elapsed = time.perf_counter() - started
    print(f"synthesised    : {elapsed:.2f}s -> {len(pcm) / player.rate:.1f}s of audio")

    print("playing the voice ...")
    ok = player.play(pcm)
    print(f"voice played   : {ok}")
    print("\nIf you heard the tone but not the voice, it is the TTS engine.")
    print("If you heard neither, it is the output device or the system volume.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
