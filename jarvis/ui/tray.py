"""System tray icon: shows Jarvis is alive, offers listen/mute/quit without the orb."""
from __future__ import annotations

import logging
import threading
from typing import Callable

from ..config import CONCEPTS

log = logging.getLogger("ui.tray")

_STATE_TITLES = {
    "booting": "Jarvis — starting up",
    "idle": "Jarvis — idle (say 'Hey Jarvis')",
    "listening": "Jarvis — listening",
    "thinking": "Jarvis — thinking",
    "speaking": "Jarvis — speaking",
    "followup": "Jarvis — listening for a follow-up",
    "muted": "Jarvis — microphone muted",
    "error": "Jarvis — error",
}


class Tray:
    def __init__(self, on_listen: Callable[[], None], on_mute: Callable[[], None],
                 on_quit: Callable[[], None],
                 on_show: Callable[[], None] | None = None) -> None:
        from PIL import Image
        from pystray import Icon, Menu, MenuItem

        icon_path = CONCEPTS / "tray_64.png"
        image = Image.open(icon_path) if icon_path.exists() else Image.new("RGBA", (64, 64), (0, 209, 255, 255))

        self._on_listen = on_listen
        self._on_mute = on_mute
        self._on_quit = on_quit
        self._thread: threading.Thread | None = None
        items = [
            MenuItem("Listen now   (Ctrl+Space)", lambda *_: self._safe(on_listen), default=True),
            MenuItem("Mute / unmute   (Ctrl+Alt+J)", lambda *_: self._safe(on_mute)),
        ]
        if on_show is not None:
            items.append(MenuItem("Show orb", lambda *_: self._safe(on_show)))
        items.append(Menu.SEPARATOR)
        items.append(MenuItem("Quit Jarvis", lambda *_: self._safe(on_quit)))
        self.icon = Icon("Jarvis", image, "Jarvis — voice assistant", Menu(*items))

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("tray callback failed")

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="tray", daemon=True)
        self._thread.start()

    def set_state(self, state: str) -> None:
        try:
            self.icon.title = _STATE_TITLES.get(state, "Jarvis")
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:  # noqa: BLE001
            pass
