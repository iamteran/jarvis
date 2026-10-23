"""The gate. One command that says whether Jarvis is actually healthy.

Why this exists: a `NameError` inside the speech pipeline once shipped and made Jarvis
silent while the app still reported "healthy". Unit tests did not catch it because they
never exercised synthesis -> hearing -> intent -> speech together. This script does, and
nothing is called "fixed" until it passes.

    .venv\\Scripts\\python.exe tools\\doctor.py

Checks, in order:
  1. every module compiles;
  2. static analysis finds no undefined names (the class of bug that broke speech);
  3. every module imports;
  4. the unit test suite passes;
  5. the real voice path works end to end, offline: SAPI speech -> Whisper transcript ->
     router intent -> reply synthesised to audio bytes;
  6. storage (memory / calendar / reminders) answers without error;
  7. a running instance, if any, is alive and not stuck in a stuck state.

Exit code 0 = everything passed. Any failure prints what broke and exits non-zero.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
FAILURES: list[str] = []


def check(label: str, fn) -> bool:  # noqa: ANN001
    try:
        detail = fn() or ""
        print(f"  [ok]   {label:<34} {detail}")
        return True
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(label)
        print(f"  [FAIL] {label:<34} {exc}")
        return False


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)


# --------------------------------------------------------------------------- checks
def check_compile() -> str:
    result = _run([PY, "-m", "compileall", "-q", "jarvis", "tools", "tests"])
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip()[-800:])
    return "all modules compile"


def check_static() -> str:
    result = _run([PY, "-m", "pyflakes", "jarvis", "tools", "tests"])
    output = (result.stdout + result.stderr).strip()
    problems = [line for line in output.splitlines() if "undefined name" in line]
    if problems:
        raise RuntimeError("; ".join(problems)[:800])
    return "no undefined names"


def check_imports() -> str:
    import importlib

    failed: list[str] = []
    for path in sorted((ROOT / "jarvis").rglob("*.py")):
        if path.name == "__main__.py":
            continue
        name = ".".join(path.with_suffix("").relative_to(ROOT).parts)
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {exc}")
    if failed:
        raise RuntimeError("; ".join(failed)[:800])
    return "all modules import"


def check_tests() -> str:
    result = _run([PY, "-m", "unittest", "discover", "-s", "tests"])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[-800:])
    tail = (result.stderr or "").strip().splitlines()
    summary = next((line for line in reversed(tail) if line.startswith("Ran ")), "tests passed")
    return summary


def check_voice_pipeline() -> str:
    """The check that was missing: speak -> hear -> understand -> reply, all offline."""
    import numpy as np

    from jarvis.asr import WhisperEars
    from jarvis.router import Router
    from jarvis.tts.sapi import SapiTTS

    tts = SapiTTS()
    tts.warm()
    spoken = "open notepad"
    pcm24 = tts.synthesize_pcm(spoken, 24000)
    if pcm24 is None or len(pcm24) < 24000 // 4:
        raise RuntimeError("TTS produced almost no audio")
    samples = np.interp(
        np.linspace(0, 1, int(len(pcm24) * 16000 / 24000), endpoint=False),
        np.linspace(0, 1, len(pcm24), endpoint=False),
        pcm24,
    )
    pcm16 = (samples * 32768).astype(np.int16)

    ears = WhisperEars()
    ears.warm()
    heard = ears.transcribe(pcm16, 16000, fast=True).strip()
    if not heard:
        raise RuntimeError("Whisper returned an empty transcript")
    handled, reply = Router().handle(heard)
    if not handled:
        raise RuntimeError(f"router did not understand {heard!r}")
    reply_pcm = tts.synthesize_pcm(reply, 24000)
    if reply_pcm is None or len(reply_pcm) < 2400:
        raise RuntimeError("reply synthesis produced no audio")
    return f"heard {heard!r} -> {reply!r}"


def check_storage() -> str:
    from jarvis.calendar_store import Calendar
    from jarvis.memory import Memory
    from jarvis.reminders import Reminders

    Memory().recall(limit=1)
    calendar = Calendar().speech_today()
    reminders = Reminders().list_speech()
    return f"calendar={calendar[:40]!r} reminders={reminders[:40]!r}"


def check_live_instance() -> str:
    health_path = ROOT / "data" / "health.json"
    if not health_path.exists():
        return "no running instance (not started yet)"
    data = json.loads(health_path.read_text(encoding="utf-8"))
    age = time.time() - float(data.get("heartbeat", 0))
    if age > 120:
        raise RuntimeError(f"heartbeat is {age:.0f}s old — instance may be dead")
    if data.get("state") == "speaking" and age > 90:
        raise RuntimeError("instance appears stuck in 'speaking'")
    if not data.get("mic_alive"):
        raise RuntimeError("microphone reported dead")
    return f"pid {data.get('pid')}, {age:.0f}s heartbeat, state {data.get('state')}"


def main() -> int:
    print("\n  JARVIS DOCTOR")
    print("  " + "-" * 52)
    check("modules compile", check_compile)
    check("static analysis", check_static)
    check("modules import", check_imports)
    check("unit tests", check_tests)
    check("voice pipeline (offline)", check_voice_pipeline)
    check("storage", check_storage)
    check("live instance", check_live_instance)
    print("  " + "-" * 52)
    if FAILURES:
        print(f"  {len(FAILURES)} CHECK(S) FAILED: {', '.join(FAILURES)}\n")
        return 1
    print("  ALL CHECKS PASSED\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(1)
