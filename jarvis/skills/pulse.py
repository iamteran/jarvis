"""The laptop's pulse: battery, CPU, memory, disk, uptime, windows and recent files.

Everything here is read-only and answered with Win32 calls or the standard library, so
it costs no extra dependency and no noticeable CPU on a five-second dashboard poll.
"""
from __future__ import annotations

import ctypes
import logging
import os
import shutil
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

log = logging.getLogger("skills.pulse")

_WINDOWS_CACHE: dict[str, Any] = {"at": 0.0, "titles": []}
_WINDOWS_TTL_S = 20.0


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("SystemStatusFlag", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_value(value: _FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def battery() -> dict[str, Any]:
    """Percent, plugged-in state and seconds remaining (None on a desktop)."""
    try:
        status = _SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return {}
        if status.BatteryLifePercent == 255:
            return {}
        return {
            "percent": int(status.BatteryLifePercent),
            "plugged": status.ACLineStatus == 1,
            "seconds_left": int(status.BatteryLifeTime) if status.BatteryLifeTime != 0xFFFFFFFF else None,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("battery read failed: %s", exc)
        return {}


def memory() -> dict[str, Any]:
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {}
        total = int(status.ullTotalPhys)
        free = int(status.ullAvailPhys)
        return {
            "percent": int(status.dwMemoryLoad),
            "used_gb": round((total - free) / 1024 ** 3, 1),
            "total_gb": round(total / 1024 ** 3, 1),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("memory read failed: %s", exc)
        return {}


def cpu_percent(sample_s: float = 0.15) -> int:
    """CPU busy percentage over a short sample (Windows has no load average)."""
    try:
        kernel32 = ctypes.windll.kernel32
        idle_a, kernel_a, user_a = _FILETIME(), _FILETIME(), _FILETIME()
        if not kernel32.GetSystemTimes(ctypes.byref(idle_a), ctypes.byref(kernel_a), ctypes.byref(user_a)):
            return 0
        time.sleep(max(0.02, sample_s))
        idle_b, kernel_b, user_b = _FILETIME(), _FILETIME(), _FILETIME()
        if not kernel32.GetSystemTimes(ctypes.byref(idle_b), ctypes.byref(kernel_b), ctypes.byref(user_b)):
            return 0
        idle = _filetime_value(idle_b) - _filetime_value(idle_a)
        kernel = _filetime_value(kernel_b) - _filetime_value(kernel_a)
        user = _filetime_value(user_b) - _filetime_value(user_a)
        total = kernel + user
        if total <= 0:
            return 0
        return max(0, min(100, int(round(100 * (total - idle) / total))))
    except Exception as exc:  # noqa: BLE001
        log.debug("cpu read failed: %s", exc)
        return 0


def disk(path: str = "C:\\") -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        used = usage.total - usage.free
        return {
            "drive": path.rstrip("\\") or path,
            "percent": int(round(100 * used / usage.total)) if usage.total else 0,
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "total_gb": round(usage.total / 1024 ** 3, 1),
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("disk read failed: %s", exc)
        return {}


def uptime() -> dict[str, Any]:
    try:
        milliseconds = ctypes.windll.kernel32.GetTickCount64()
        seconds = int(milliseconds / 1000)
        hours, remainder = divmod(seconds, 3600)
        return {"seconds": seconds, "text": f"{hours}h {remainder // 60}m"}
    except Exception:  # noqa: BLE001
        return {}


def windows(limit: int = 8) -> list[str]:
    """Titles of open windows, cached so the dashboard poll stays cheap."""
    now = time.time()
    if now - float(_WINDOWS_CACHE["at"]) < _WINDOWS_TTL_S:
        return list(_WINDOWS_CACHE["titles"])[:limit]
    try:
        from . import screen

        titles = [title for title in screen.list_windows() if title.strip()]
    except Exception as exc:  # noqa: BLE001
        log.debug("window list failed: %s", exc)
        titles = []
    _WINDOWS_CACHE.update({"at": now, "titles": titles})
    return titles[:limit]


def _candidate_folders() -> list[Path]:
    home = Path.home()
    names = ["Downloads", "Desktop", "Documents"]
    folders: list[Path] = []
    for name in names:
        for root in (home, home / "OneDrive"):
            candidate = root / name
            if candidate.is_dir():
                folders.append(candidate)
    return folders


def recent_files(limit: int = 8, hours: int = 48) -> list[dict[str, Any]]:
    """Recently touched documents in the usual three folders (one level deep)."""
    cutoff = time.time() - hours * 3600
    found: list[tuple[float, Path]] = []
    ignore = {"desktop.ini", "thumbs.db"}
    for folder in _candidate_folders():
        try:
            entries = list(folder.iterdir())
        except Exception:  # noqa: BLE001
            continue
        for entry in entries:
            try:
                if entry.name.lower() in ignore or entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    continue
                modified = entry.stat().st_mtime
            except Exception:  # noqa: BLE001
                continue
            if modified >= cutoff:
                found.append((modified, entry))
    found.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for modified, path in found[:limit]:
        try:
            size = path.stat().st_size
        except Exception:  # noqa: BLE001
            size = 0
        out.append({
            "name": path.name,
            "path": str(path),
            "folder": path.parent.name,
            "modified": modified,
            "modified_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(modified)),
            "size_kb": round(size / 1024, 1),
        })
    return out


def snapshot() -> dict[str, Any]:
    """Everything the System panel needs, in one dictionary."""
    return {
        "battery": battery(),
        "cpu": {"percent": cpu_percent()},
        "memory": memory(),
        "disk": disk(),
        "uptime": uptime(),
        "windows": windows(),
        "recent": recent_files(),
        "at": time.time(),
    }


def speech() -> str:
    """A short spoken summary for "how's my laptop?"."""
    parts: list[str] = []
    power = battery()
    if power:
        state = "charging" if power.get("plugged") else "on battery"
        parts.append(f"battery {power.get('percent')} percent, {state}")
    load = memory()
    if load:
        parts.append(f"memory {load.get('percent')} percent used")
    space = disk()
    if space:
        parts.append(f"{space.get('free_gb')} gigabytes free on {space.get('drive')}")
    parts.append(f"processor around {cpu_percent()} percent")
    return "Sir: " + ", ".join(parts) + "."
