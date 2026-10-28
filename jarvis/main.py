"""Entry points: normal run, plus diagnostics (--devices, --check, --selftest, --wake-test)."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np

from . import log as jarvis_log
from .config import CONFIG

log = logging.getLogger("main")


# --------------------------------------------------------------------------- utils
def load_audio_as_pcm16(path: str | Path) -> np.ndarray:
    """Any audio file -> 16 kHz mono int16 PCM, using ffmpeg."""
    from .tts.fish import decode_to_pcm

    data = Path(path).read_bytes()
    pcm = decode_to_pcm(data, CONFIG.sample_rate)
    return np.clip(pcm * 32768.0, -32768, 32767).astype(np.int16)


def _banner() -> None:
    print("\n  J A R V I S   -   voice assistant")
    print("  " + "-" * 38)


# ------------------------------------------------------------------------ commands
def cmd_devices() -> int:
    from .audio.capture import describe_devices

    _banner()
    print("\nAudio devices (set JARVIS_MIC / JARVIS_SPEAKER to an index or name):\n")
    for line in describe_devices():
        print("  " + line)
    print()
    return 0


def cmd_check() -> int:
    """Verify every dependency of a working conversation, one by one."""
    _banner()
    failures = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  [{'ok  ' if ok else 'FAIL'}] {label:<26} {detail}")

    problems = CONFIG.validate()
    brains = [name for name, key in (
        ("Grok", CONFIG.grok_api_key), ("Groq", CONFIG.groq_api_key),
        ("DeepSeek", CONFIG.deepseek_api_key), ("Gemini", CONFIG.gemini_api_key),
    ) if key]
    report(not problems, "credentials",
           ", ".join(problems) or f"brains: {', '.join(brains) or 'none'}; Fish key "
                                  f"{'present' if CONFIG.fish_api_key else 'missing'}")

    # Brain (provider chain)
    try:
        from .brain.llm import Brain
        t0 = time.perf_counter()
        brain = Brain()
        reply = brain.reply("Reply with exactly one word: ONLINE")
        ms = (time.perf_counter() - t0) * 1000
        report(bool(reply.strip()), "Language model",
               f"{brain.last_model} -> {reply.strip()[:30]!r} ({ms:.0f} ms)")
    except Exception as exc:  # noqa: BLE001
        report(False, "Language model", str(exc)[:120])

    # Ears
    try:
        from .ears import Ears
        silence = np.zeros(CONFIG.sample_rate, dtype=np.int16)
        ears = Ears()
        t0 = time.perf_counter()
        ears.transcribe(silence, CONFIG.sample_rate)
        ms = (time.perf_counter() - t0) * 1000
        provider = "whisper" if ears.whisper is not None else (
            "groq" if ears.groq is not None else "gemini")
        extra = (f"{CONFIG.whisper_model} (offline)" if ears.whisper is not None else
                 f"{CONFIG.groq_stt_model}" if ears.groq is not None else f"{CONFIG.stt_model}")
        report(True, f"Ears ({provider})", f"{extra} reachable ({ms:.0f} ms)")
    except Exception as exc:  # noqa: BLE001
        report(False, "Ears", str(exc)[:120])

    # Vision (synthetic image, so --check never uploads your screen)
    try:
        import io

        from PIL import Image, ImageDraw

        from . import vision

        image = Image.new("RGB", (320, 120), "white")
        ImageDraw.Draw(image).text((24, 48), "JARVIS 42", fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        provider = next((f"{p}/{m}" for p, m in CONFIG.vision_chain
                         if (p == "grok" and CONFIG.grok_api_key)
                         or (p == "gemini" and CONFIG.gemini_api_key)), "none")
        t0 = time.perf_counter()
        answer = vision.describe("What number is written in this image?",
                                 buffer.getvalue())
        ms = (time.perf_counter() - t0) * 1000
        report("42" in answer, f"Vision ({provider})", f"{answer[:48]!r} ({ms:.0f} ms)")
    except Exception as exc:  # noqa: BLE001
        report(False, "Vision", str(exc)[:120])

    # Voice
    try:
        from .tts.factory import make_tts
        t0 = time.perf_counter()
        pcm = make_tts().synthesize_pcm("Systems check complete.")
        ms = (time.perf_counter() - t0) * 1000
        report(len(pcm) > 1000, f"Voice ({CONFIG.tts_engine})",
               f"{len(pcm) / CONFIG.playback_rate:.1f} s audio in {ms:.0f} ms")
    except Exception as exc:  # noqa: BLE001
        report(False, "Voice", str(exc))

    # Local Whisper (optional)
    try:
        from .asr import WhisperEars, available as whisper_available
        if whisper_available():
            report(True, "Local Whisper", f"{CONFIG.whisper_model} installed (offline ears, no network)")
        else:
            report(True, "Local Whisper", "not installed (pip install faster-whisper) — using cloud ears")
    except Exception as exc:  # noqa: BLE001
        report(True, "Local Whisper", f"unavailable ({str(exc)[:60]})")

    # Accessibility automation
    try:
        from .skills import screen
        ok = screen.available()
        report(ok, "UI Automation", "pywinauto ready: click/type/read windows" if ok else "pywinauto missing")
    except Exception as exc:  # noqa: BLE001
        report(False, "UI Automation", str(exc)[:100])

    # Memory + tools
    try:
        from .memory import summarise_day
        from .tools import Tools
        report(True, "Memory", summarise_day())
        report(True, "Toolbelt", f"{len(Tools().names())} tools: " + ", ".join(Tools().names()[:6]) + ", ...")
    except Exception as exc:  # noqa: BLE001
        report(False, "Memory/tools", str(exc)[:100])

    # Gmail (optional; login only, never reads mail here)
    try:
        from .email_client import probe
        ok, detail = probe()
        report(ok, "Email (Gmail)", detail)
    except Exception as exc:  # noqa: BLE001
        report(False, "Email (Gmail)", str(exc)[:100])

    # Dashboard (starts with Jarvis, or on demand with --dashboard)
    try:
        from .webui import DEFAULT_PORT
        port = CONFIG.dashboard_port or DEFAULT_PORT
        report(True, "Dashboard", f"http://127.0.0.1:{port}/ (local only)")
    except Exception as exc:  # noqa: BLE001
        report(False, "Dashboard", str(exc)[:100])

    # Wake word
    try:
        from .audio.wake import WakeWord
        w = WakeWord(CONFIG.wake_model, CONFIG.wake_threshold, CONFIG.wake_frames)
        report(True, "Wake word model", w.model_name)
    except Exception as exc:  # noqa: BLE001
        report(False, "Wake word model", str(exc))

    # Microphone
    try:
        from .audio.capture import AudioHub
        hub = AudioHub()
        hub.start()
        blocks = 0
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if hub.get_block(timeout=0.2) is not None:
                blocks += 1
        hub.stop()
        report(blocks > 3, "Microphone", f"{hub.device_name}, {blocks} blocks in 1 s")
    except Exception as exc:  # noqa: BLE001
        report(False, "Microphone", str(exc))

    print()
    print("  All good - Jarvis is ready." if not failures else f"  {failures} problem(s) to fix above.")
    print()
    return 0 if not failures else 1


def cmd_selftest(path: str) -> int:
    """Run the whole conversation pipeline on an audio file, no microphone needed."""
    from .brain.llm import Brain
    from .ears import Ears
    from .tts.factory import make_tts
    from .tts.player import Player
    from .tts.speaker import Speaker

    _banner()
    print(f"  self-test with: {path}\n")
    pcm = load_audio_as_pcm16(path)
    print(f"  audio: {len(pcm) / CONFIG.sample_rate:.2f} s\n")

    ears = Ears()

    t0 = time.perf_counter()
    text = ears.transcribe(pcm, CONFIG.sample_rate)
    print(f"  [stt]  {(time.perf_counter() - t0) * 1000:5.0f} ms  "
          f"({ears.last_provider or 'none'}) heard: {text!r}")

    brain = Brain()
    t0 = time.perf_counter()
    reply = brain.reply(text or "Hello Jarvis, are you there?")
    print(f"  [llm]  {(time.perf_counter() - t0) * 1000:5.0f} ms  "
          f"({brain.last_model}) reply: {reply!r}")

    speaker = Speaker(make_tts(), Player())
    t0 = time.perf_counter()
    speaker.say(reply)
    print(f"  [tts]  {(time.perf_counter() - t0) * 1000:5.0f} ms  spoken\n")
    return 0


def cmd_stt(path: str) -> int:
    """Transcribe an audio file with the exact chain Jarvis uses for its ears."""
    from .ears import Ears

    _banner()
    print(f"  transcribing: {path}\n")
    pcm = load_audio_as_pcm16(path)
    print(f"  audio: {len(pcm) / CONFIG.sample_rate:.2f} s")
    ears = Ears()
    ears.warm()          # load the local Whisper model before timing the chain
    t0 = time.perf_counter()
    try:
        text = ears.transcribe(pcm, CONFIG.sample_rate)
        ms = (time.perf_counter() - t0) * 1000
        print(f"  [{ms:6.0f} ms] {ears.last_provider or 'unknown':<28} -> {text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED {type(exc).__name__}: {str(exc)[:140]}")
    print()
    return 0


def cmd_mic(seconds: float = 6.0) -> int:
    """Record from the microphone and show what Jarvis actually hears."""
    from .audio.capture import AudioHub
    from .audio.vad import EnergyVAD, rms

    _banner()
    print(f"  recording {seconds:.0f}s — say a command now\n")
    hub = AudioHub()
    hub.start()
    vad = EnergyVAD()
    levels: list[float] = []
    blocks = []
    speech_ms = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        block = hub.get_block(0.2)
        if block is None:
            continue
        level = rms(block)
        levels.append(level)
        blocks.append(block)
        if vad.is_speech(block):
            speech_ms += CONFIG.block_ms
    hub.stop()
    if not blocks:
        print("  no audio captured — is the microphone muted?\n")
        return 1
    peak = max(levels)
    average = sum(levels) / len(levels)
    print(f"  mic      : {hub.device_name}")
    print(f"  average  : {average:.4f}   peak: {peak:.4f}")
    print(f"  threshold: {max(CONFIG.speech_rms, vad.noise * CONFIG.noise_mult):.4f} "
          f"(noise floor {vad.noise:.4f})")
    print(f"  speech   : {speech_ms} ms detected")
    verdict = ("speech detected — the VAD can hear you" if speech_ms >= CONFIG.min_speech_ms
               else "NO speech detected — the threshold is too high for this mic")
    print(f"  verdict  : {verdict}\n")
    if peak >= CONFIG.salvage_peak:
        import numpy as np

        from .ears import Ears

        text = Ears().transcribe(np.concatenate(blocks), CONFIG.sample_rate)
        print(f"  whisper  : {text!r}\n")
    return 0


def cmd_tools() -> int:
    """List the toolbelt the model can call."""
    from .tools import Tools

    _banner()
    tools = Tools()
    for name in tools.names():
        tool = tools.get(name)
        print(f"  {name:<18} {tool.description}")
    print(f"\n  {len(tools.names())} tools\n")
    return 0


def cmd_memory() -> int:
    _banner()
    from .memory import Memory

    memory = Memory()
    stats = memory.stats()
    print(f"  memory file : {stats['path']}")
    print(f"  facts       : {stats['facts']}")
    print(f"  aliases     : {stats['aliases']}")
    print(f"  turns       : {stats['turns']}\n")
    facts = memory.recall(limit=20)
    if facts:
        print("  remembered facts:")
        for fact in facts:
            print(f"    - {fact}")
    else:
        print("  nothing remembered yet — say 'remember that ...'")
    print()
    return 0


def cmd_autostart(enable: bool) -> int:
    """Start Jarvis with Windows (a shortcut in the user's Startup folder)."""
    import os
    from pathlib import Path

    _banner()
    startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
    bat = startup / "Jarvis.bat"
    if not enable:
        bat.unlink(missing_ok=True)
        print("  Jarvis will no longer start with Windows.\n")
        return 0
    if not startup.is_dir():
        print("  could not find the Startup folder\n")
        return 1
    root = Path(__file__).resolve().parent.parent
    supervisor = root / "run_jarvis.bat"
    script = (
        "@echo off\r\n"
        f'cd /d "{root}"\r\n'
        f'start "" /min "{supervisor}"\r\n'
    )
    bat.write_text(script, encoding="utf-8")
    print(f"  Jarvis will start with Windows (supervised).\n  shortcut: {bat}\n")
    return 0


def cmd_wake_test(path: str) -> int:
    """Feed a recording through the wake word detector and report the best score."""
    from .audio.wake import WakeWord

    _banner()
    print(f"  wake-word test with: {path}\n")
    pcm = load_audio_as_pcm16(path)
    wake = WakeWord(CONFIG.wake_model, CONFIG.wake_threshold, CONFIG.wake_frames)
    block = int(CONFIG.sample_rate * CONFIG.block_ms / 1000)
    best = 0.0
    best_at = 0.0
    triggered = False
    for start in range(0, len(pcm) - block, block):
        if wake.detect(pcm[start:start + block]):
            triggered = True
        if wake.last_score > best:
            best, best_at = wake.last_score, start / CONFIG.sample_rate
    print(f"  best score {best:.3f} at {best_at:.2f} s (threshold {CONFIG.wake_threshold:.2f})")
    print(f"  result: {'TRIGGERED' if triggered else 'not triggered'}\n")
    return 0 if best >= CONFIG.wake_threshold else 2


def cmd_say(text: str) -> int:
    """Send a typed sentence through the exact same path as a spoken one (no mic)."""
    from .brain.llm import Brain
    from .router import Router
    from .tts.factory import make_tts
    from .tts.player import Player
    from .tts.speaker import Speaker

    _banner()
    print(f"  you    : {text}")
    router = Router()
    t0 = time.perf_counter()
    handled, reply = router.handle(text)
    if handled:
        print(f"  [router] {(time.perf_counter() - t0) * 1000:5.0f} ms")
    else:
        t0 = time.perf_counter()
        actions: list[str] = []
        try:
            reply, actions = Brain().respond(text)
        except Exception as exc:  # noqa: BLE001 - network or quota
            print(f"  [llm] failed: {exc}")
            reply = "Forgive me, sir. The network is delaying my thoughts."
        print(f"  [llm]    {(time.perf_counter() - t0) * 1000:5.0f} ms")
        for action in actions:
            print(f"  [tool]   {action}")
    print(f"  jarvis : {reply}")

    t0 = time.perf_counter()
    Speaker(make_tts(), Player()).say(reply)
    print(f"  [voice]  {(time.perf_counter() - t0) * 1000:5.0f} ms\n")
    return 0


_MUTEX_HANDLE = None


def _claim_single_instance() -> bool:
    """One Jarvis per desktop: two instances fight over the microphone.

    Uses a named Windows mutex, so it also catches instances started from a different
    console or by the tray autostart.
    """
    global _MUTEX_HANDLE
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Global\\JarvisVoiceAssistant")
        already_running = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        if already_running:
            return False
        _MUTEX_HANDLE = handle  # keep the handle alive for the process lifetime
        return True
    except Exception:  # noqa: BLE001 - never block startup on this check
        return True


def cmd_run(show_orb: bool) -> int:
    from .app import Jarvis

    jarvis_log.setup()
    if not _claim_single_instance():
        print("\n  Jarvis is already running — use its tray icon to control or quit it.\n")
        return 2    # distinct code: the supervisor must not retry this
    try:
        Jarvis(show_orb=show_orb).run()
    except KeyboardInterrupt:
        print("\ninterrupted")
    return 0


def cmd_status() -> int:
    """Is Jarvis running, and how healthy is it? Reads the heartbeat the app writes."""
    import json

    from .config import DATA

    _banner()
    health_path = DATA / "health.json"
    if not health_path.exists():
        print("  no heartbeat yet — Jarvis has not run since this feature was added.\n")
        return 1
    try:
        data = json.loads(health_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  unreadable heartbeat: {exc}\n")
        return 1
    age = time.time() - float(data.get("heartbeat", 0))
    alive = False
    try:
        import psutil

        alive = psutil.pid_exists(int(data.get("pid", 0)))
    except Exception:  # noqa: BLE001
        alive = age < 30
    print(f"  pid      : {data.get('pid')}  ({'alive' if alive else 'NOT RUNNING'})")
    print(f"  uptime   : {data.get('uptime_s', 0) / 60:.0f} min")
    print(f"  state    : {data.get('state')}")
    print(f"  mic      : {data.get('mic')} ({'ok' if data.get('mic_alive') else 'DEAD'})")
    print(f"  heartbeat: {age:.0f} s ago")
    print(f"  verdict  : {'healthy' if alive and age < 30 and data.get('mic_alive') else 'needs attention'}\n")
    return 0 if alive and age < 30 and data.get("mic_alive") else 1


def cmd_dashboard() -> int:
    """Open the command centre; reuse a running Jarvis's server when there is one."""
    import urllib.request
    import webbrowser

    from .webui import DEFAULT_PORT, start, stop, url

    _banner()
    port = CONFIG.dashboard_port or DEFAULT_PORT
    live = False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1.5) as response:
            live = response.status == 200
    except Exception:  # noqa: BLE001
        live = False
    if not live and start() is None:
        print("  could not start the dashboard (is another program using the port?)\n")
        return 1
    webbrowser.open(url())
    print(f"  dashboard: {url()}")
    if live:
        print("  (served by the running Jarvis)\n")
        return 0
    print("  Ctrl+C to stop the dashboard server.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
        print()
    return 0


# ---------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis — voice-operated desktop assistant")
    parser.add_argument("--devices", action="store_true", help="list audio devices and exit")
    parser.add_argument("--check", action="store_true", help="verify keys, models, mic and voice")
    parser.add_argument("--selftest", metavar="AUDIO", help="run the full pipeline on an audio file")
    parser.add_argument("--wake-test", metavar="AUDIO", help="score an audio file against the wake word")
    parser.add_argument("--stt", metavar="AUDIO", help="transcribe an audio file with Jarvis's STT chain")
    parser.add_argument("--mic", nargs="?", const=6.0, type=float, metavar="SECONDS",
                        help="record from the mic and show what Jarvis hears")
    parser.add_argument("--tools", action="store_true", help="list the model's tools and exit")
    parser.add_argument("--memory", action="store_true", help="show what Jarvis remembers")
    parser.add_argument("--autostart", choices=["on", "off"], help="start Jarvis with Windows")
    parser.add_argument("--status", action="store_true", help="show whether Jarvis is running and healthy")
    parser.add_argument("--say", metavar="TEXT", help="send typed text through the same path as speech")
    parser.add_argument("--dashboard", action="store_true", help="open the live command centre")
    parser.add_argument("--no-orb", action="store_true", help="run without the floating orb")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    args = parser.parse_args(argv)

    jarvis_log.setup(debug=args.debug)

    if args.devices:
        return cmd_devices()
    if args.check:
        return cmd_check()
    if args.selftest:
        return cmd_selftest(args.selftest)
    if args.wake_test:
        return cmd_wake_test(args.wake_test)
    if args.stt:
        return cmd_stt(args.stt)
    if args.mic:
        return cmd_mic(float(args.mic))
    if args.tools:
        return cmd_tools()
    if args.memory:
        return cmd_memory()
    if args.autostart:
        return cmd_autostart(args.autostart == "on")
    if args.status:
        return cmd_status()
    if args.say:
        return cmd_say(args.say)
    if args.dashboard:
        return cmd_dashboard()
    return cmd_run(show_orb=CONFIG.show_orb and not args.no_orb)


if __name__ == "__main__":
    sys.exit(main())
