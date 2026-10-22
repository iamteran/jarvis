"""System control: volume, screen lock, clock. No network, no LLM — instant."""
from __future__ import annotations

import datetime as _dt
import logging
import subprocess

log = logging.getLogger("skills.system")


def _press_media_key(name: str, repeat: int = 1) -> bool:
    try:
        from pynput.keyboard import Controller, Key
    except Exception:  # noqa: BLE001
        return False
    key = getattr(Key, name, None)
    if key is None:
        return False
    board = Controller()
    for _ in range(max(1, repeat)):
        board.press(key)
        board.release(key)
    return True


def _endpoint():
    """Windows master-volume interface, or None if pycaw is unavailable."""
    try:
        from pycaw.pycaw import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()
        volume = getattr(speakers, "EndpointVolume", None)   # pycaw >= 2024
        if volume is not None:
            return volume
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume

        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))
    except Exception as exc:  # noqa: BLE001
        log.debug("volume endpoint unavailable: %s", exc)
        return None


def get_volume() -> int | None:
    try:
        volume = _endpoint()
        if volume is None:
            return None
        return int(round(float(volume.GetMasterVolumeLevelScalar()) * 100))
    except Exception:  # noqa: BLE001
        return None


def set_volume_raw(percent: int) -> bool:
    percent = max(0, min(100, int(percent)))
    try:
        volume = _endpoint()
        if volume is None:
            return False
        volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("set volume failed: %s", exc)
        return False


def _step_volume(delta: int) -> str:
    """Change volume by `delta` points, with an undo entry."""
    from ..undo import push_undo

    old = get_volume()
    if old is None:
        # no pycaw: fall back to media keys, no undo
        key = "media_volume_up" if delta > 0 else "media_volume_down"
        ok = _press_media_key(key, abs(delta) // 2 or 1)
        direction = "up" if delta > 0 else "down"
        return f"Turning it {direction}, sir." if ok else "I can't reach the volume control, sir."
    new = max(0, min(100, old + delta))
    if not set_volume_raw(new):
        return "I can't reach the volume control, sir."
    push_undo(f"volume {new}%", lambda: _restore_volume(old))
    direction = "up" if delta > 0 else "down"
    return f"Volume {direction} to {new} percent, sir."


def _restore_volume(level: int) -> str:
    set_volume_raw(level)
    return f"Back to {level} percent."


def volume_up(steps: int = 2) -> str:
    return _step_volume(10 if steps <= 2 else 20)


def volume_down(steps: int = 2) -> str:
    return _step_volume(-10 if steps <= 2 else -20)


def set_volume(percent: int) -> str:
    from ..undo import push_undo

    old = get_volume()
    if not set_volume_raw(percent):
        return "I can't reach the volume control, sir."
    if old is not None:
        push_undo(f"volume {percent}%", lambda: _restore_volume(old))
    return f"Volume set to {percent} percent, sir."


def volume_report() -> str:
    level = get_volume()
    return f"The volume is at {level} percent, sir." if level is not None else "I can't read the volume, sir."


def mute() -> str:
    ok = _press_media_key("media_volume_mute")
    return "Muted, sir." if ok else "I can't reach the volume control, sir."


def play_pause() -> str:
    ok = _press_media_key("media_play_pause")
    return "Done, sir." if ok else "The media keys aren't responding, sir."


def lock_screen() -> str:
    try:
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
        return "Locking the screen, sir."
    except Exception as exc:  # noqa: BLE001
        log.error("lock failed: %s", exc)
        return "I couldn't lock the screen, sir."


def show_desktop() -> str:
    """Minimise everything (Win+D) — what people mean by 'show the desktop'."""
    try:
        from pynput.keyboard import Controller, Key

        board = Controller()
        board.press(Key.cmd)
        board.press("d")
        board.release("d")
        board.release(Key.cmd)
        return "Desktop, sir."
    except Exception as exc:  # noqa: BLE001
        log.warning("show desktop failed: %s", exc)
        return "I couldn't show the desktop, sir."


def now_speech() -> str:
    now = _dt.datetime.now()
    return "It is " + now.strftime("%I:%M %p").lstrip("0").replace(" 0", " ") + ", sir."


def date_speech() -> str:
    now = _dt.datetime.now()
    return "Today is " + now.strftime("%A, %d %B %Y") + "."


def battery_speech() -> str:
    try:
        import psutil  # optional dependency

        battery = psutil.sensors_battery()
        if battery is None:
            return "I can't read the battery on this machine, sir."
        state = "charging" if battery.power_plugged else "on battery"
        return f"The battery is at {round(battery.percent)} percent and {state}, sir."
    except Exception:  # noqa: BLE001
        return "I can't read the battery on this machine, sir."
