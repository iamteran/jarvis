"""Launching applications and websites, with a cached index of what is installed.

Index sources (in priority order):
  1. Start Menu shortcuts (Start Menu\\Programs\\**\\*.lnk)  — classic desktop apps
  2. `Get-StartApps` AppIDs                                — Microsoft Store apps
  3. Curated aliases + direct paths                        — things users actually say
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
import time
import webbrowser
from pathlib import Path

from ..config import DATA

log = logging.getLogger("skills.apps")

CACHE = DATA / "apps.json"
CACHE_TTL_S = 3 * 24 * 3600

# what people say -> what the Start Menu calls it
ALIASES = {
    "chrome": "google chrome", "google": "google chrome", "browser": "google chrome",
    "edge": "microsoft edge", "firefox": "firefox",
    "code": "visual studio code", "vscode": "visual studio code", "vs code": "visual studio code",
    "whatsapp": "whatsapp", "telegram": "telegram desktop", "spotify": "spotify",
    "vlc": "vlc media player", "media player": "vlc media player",
    "notepad": "notepad", "calc": "calculator", "calculator": "calculator",
    "word": "word", "excel": "excel", "powerpoint": "powerpoint",
    "files": "file explorer", "explorer": "file explorer", "file explorer": "file explorer",
    "terminal": "windows terminal", "cmd": "command prompt", "powershell": "windows powershell",
    "settings": "settings", "store": "microsoft store", "paint": "paint",
    "task manager": "task manager", "camera": "camera", "photos": "photos",
}

KNOWN_PATHS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "command prompt": "cmd.exe",
    "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "paint": "mspaint.exe",
}

SITE_ALIASES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "github": "https://github.com",
    "chatgpt": "https://chatgpt.com", "gemini": "https://gemini.google.com",
    "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
    "twitter": "https://x.com", "x": "https://x.com", "netflix": "https://www.netflix.com",
    "whatsapp web": "https://web.whatsapp.com", "linkedin": "https://www.linkedin.com",
}

# Apps that are often not installed but have an excellent web version. If the desktop
# app is missing, open the site instead of saying "I couldn't find it".
WEB_APPS = {
    "spotify": "https://open.spotify.com",
    "netflix": "https://www.netflix.com",
    "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com",
    "maps": "https://maps.google.com",
    "notion": "https://www.notion.so",
    "figma": "https://www.figma.com",
    "discord": "https://discord.com/app",
    "whatsapp": "https://web.whatsapp.com",
    "telegram": "https://web.telegram.org",
    "chatgpt": "https://chatgpt.com",
    "gemini": "https://gemini.google.com",
    "x": "https://x.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
}

# Start Menu entries that must never be launched by voice.
_DANGEROUS = ("uninstall", "unwise", "repair", "reset", "remove ", "setup", "installer")

_URL_RE = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?$", re.IGNORECASE)


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\b(the|my|a|an)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,!?")
    return text


def _scan_lnks() -> dict[str, str]:
    roots = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    found: dict[str, str] = {}
    for root in roots:
        try:
            for lnk in root.rglob("*.lnk"):
                found[lnk.stem.lower()] = str(lnk)
        except Exception:  # noqa: BLE001 - a missing Start Menu is not fatal
            continue
    return found


def _scan_start_apps() -> dict[str, str]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=25,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return {str(item.get("Name", "")).lower(): str(item.get("AppID", "")) for item in data if item.get("Name")}
    except Exception as exc:  # noqa: BLE001
        log.debug("Get-StartApps failed: %s", exc)
        return {}


class AppLauncher:
    def __init__(self) -> None:
        self._index: dict | None = None

    # -------------------------------------------------------------------- index
    def index(self, force: bool = False) -> dict:
        if self._index is not None and not force:
            return self._index
        try:
            if not force and CACHE.exists() and (time.time() - CACHE.stat().st_mtime) < CACHE_TTL_S:
                self._index = json.loads(CACHE.read_text(encoding="utf-8"))
                return self._index
        except Exception:  # noqa: BLE001
            pass
        index = {"lnk": _scan_lnks(), "appid": _scan_start_apps(), "built": time.time()}
        log.info("app index built: %d shortcuts, %d start apps",
                 len(index["lnk"]), len(index["appid"]))
        try:
            CACHE.write_text(json.dumps(index), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        self._index = index
        return index

    def names(self) -> list[str]:
        idx = self.index()
        return sorted(set(idx["lnk"]) | set(idx["appid"]))

    # -------------------------------------------------------------------- lookup
    @staticmethod
    def _score(name: str, query: str) -> float:
        """Rank candidate app names: exact beats prefix beats contains, shorter wins."""
        if name == query:
            return 1000.0
        # Speech recognition often inserts or drops spaces: "note pad" for "notepad",
        # "youtube" for "you tube". Compare without spaces before giving up.
        compact_name = name.replace(" ", "")
        compact_query = query.replace(" ", "")
        if compact_name == compact_query:
            return 950.0
        if name.startswith(query):
            return 500.0 - len(name) * 0.1
        if compact_name.startswith(compact_query):
            return 480.0 - len(name) * 0.1
        if re.search(rf"\b{re.escape(query)}\b", name):
            return 300.0 - len(name) * 0.1
        if query in name:
            return 100.0 - len(name) * 0.1
        if compact_query in compact_name:
            return 90.0 - len(name) * 0.1
        return 0.0

    def _resolve(self, query: str) -> tuple[str, str] | None:
        """Return ("lnk"|"appid"|"path", value) for a spoken app name."""
        q = normalize(query)
        q = re.sub(r"\b(app|application|program)\b", "", q).strip()
        if not q:
            return None

        alias = ALIASES.get(q) or ALIASES.get(q.replace(" ", "")) or q
        idx = self.index()
        lnk, appid = idx["lnk"], idx["appid"]

        best: tuple[float, str, str] | None = None
        tables = [
            ("lnk", lnk),
            ("appid", appid),
            ("path", KNOWN_PATHS),
        ]
        for candidate in {alias, q, q.replace(" ", "")}:
            if candidate in KNOWN_PATHS:
                return ("path", KNOWN_PATHS[candidate])
            for kind, table in tables:
                for name, value in table.items():
                    if any(bad in name for bad in _DANGEROUS):
                        continue
                    score = self._score(name, candidate)
                    if score > 0 and (best is None or score > best[0]):
                        best = (score, kind, value)
        if best is not None and best[0] >= 90.0:
            return (best[1], best[2])

        # fuzzy match as a last resort
        pool = [n for n in list(lnk) + list(appid) if not any(bad in n for bad in _DANGEROUS)]
        close = difflib.get_close_matches(alias, pool, n=1, cutoff=0.72)
        if close:
            match = close[0]
            if match in lnk:
                return ("lnk", lnk[match])
            return ("appid", appid[match])
        return None

    # -------------------------------------------------------------------- launch
    def launch_app(self, query: str) -> tuple[bool, str]:
        resolved = self._resolve(query)
        if resolved is None:
            compact = normalize(query).replace(" ", "")
            site = WEB_APPS.get(normalize(query)) or next(
                (url for name, url in WEB_APPS.items() if name.replace(" ", "") == compact), None
            )
            if site:
                webbrowser.open(site)
                log.info("app %r not installed; opened web version", query)
                return True, f"{query.title()} isn't installed, sir, so I've opened the web version."
            return False, f"I couldn't find an app called {query}, sir. It may not be installed."
        kind, value = resolved
        try:
            if kind == "lnk":
                os.startfile(value)  # noqa: S606 - launching a user-chosen shortcut
            elif kind == "appid":
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{value}"])
            else:
                subprocess.Popen(value, shell=True)
            name = Path(value).stem if kind == "lnk" else query
            log.info("launched %s (%s -> %s)", query, kind, value)
            return True, f"Opening {name}, sir."
        except Exception as exc:  # noqa: BLE001
            log.error("launch failed: %s", exc)
            return False, f"I tried to open {query}, but it refused, sir."

    def open_site(self, target: str) -> tuple[bool, str]:
        raw = target.strip()
        if _URL_RE.match(raw):
            url = raw if raw.startswith("http") else f"https://{raw}"
            webbrowser.open(url)
            return True, "Opening that now, sir."
        site = SITE_ALIASES.get(normalize(raw).replace("dot com", "").strip())
        if site:
            webbrowser.open(site)
            return True, f"Opening {raw}, sir."
        # "search for cats" -> Google
        webbrowser.open("https://www.google.com/search?q=" + target.replace(" ", "+"))
        return True, f"Searching the web for {raw}, sir."
