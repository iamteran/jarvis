"""Desktop control through the Windows accessibility tree (UI Automation).

Why this matters: a vision model can describe a screenshot, but it cannot reliably press
"Save" on a form it half-recognises. Windows already exposes every button, field and menu
item by name through UI Automation — so "click Save" becomes an exact lookup, works
offline, costs nothing, and can tell you what it actually clicked.

Idea adapted from FatihMakes/Mark-LVI (CC BY-NC 4.0, non-commercial).
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
import warnings

log = logging.getLogger("skills.screen")

# pywinauto prints a STA/MTA advisory the first time a non-main thread touches COM.
# It is expected here (calls come from turn threads) and would otherwise pollute stderr.
warnings.filterwarnings("ignore", message=".*STA COM threading mode.*")

MAX_TEXT_ITEMS = 60


def _desktop():
    from pywinauto import Desktop

    return Desktop(backend="uia")


def available() -> bool:
    try:
        import pywinauto  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe_descendants(window):
    try:
        return list(window.descendants())
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------------ windows
def list_windows() -> list[str]:
    """Titles of visible top-level windows.

    Win32 EnumWindows, not UI Automation: it answers in milliseconds and does not pay
    COM/UIA enumeration cost on every call (which took ~18 s with Chrome open).
    """
    titles: list[str] = []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def _callback(hwnd, _lparam):  # noqa: ANN001
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.strip()
                if title and title not in titles:
                    titles.append(title)
            except Exception:  # noqa: BLE001
                pass
            return True

        user32.EnumWindows(enum_proc(_callback), 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("win32 window enumeration failed: %s", exc)
        return _list_windows_uia()
    return titles


def _list_windows_uia() -> list[str]:
    titles: list[str] = []
    try:
        for window in _desktop().windows():
            try:
                title = (window.window_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if title and title not in titles:
                titles.append(title)
    except Exception as exc:  # noqa: BLE001
        log.debug("uia window enumeration failed: %s", exc)
    return titles


def warm() -> None:
    """Pay COM/UIA start-up costs at boot instead of during the first command."""
    try:
        _desktop()
    except Exception:  # noqa: BLE001
        pass


def find_window(hint: str):
    if not hint:
        return None
    needle = hint.lower().strip()
    try:
        windows = _desktop().windows()
    except Exception:  # noqa: BLE001
        return None
    fallback = None
    for window in windows:
        try:
            title = (window.window_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not title:
            continue
        if needle == title.lower():
            return window
        if needle in title.lower() and fallback is None:
            fallback = window
    return fallback


def activate(hint: str) -> str:
    """Bring a window to the front by (part of) its title."""
    window = find_window(hint)
    if window is None:
        open_titles = [t for t in list_windows() if t]
        sample = ", ".join(open_titles[:6])
        return f"I can't see a window called {hint}, sir. Open windows: {sample}."
    try:
        try:
            if not window.is_visible():
                window.restore()
        except Exception:  # noqa: BLE001
            pass
        window.set_focus()
        time.sleep(0.15)
        return f"{window.window_text()} is in front, sir."
    except Exception as exc:  # noqa: BLE001
        log.debug("focus failed: %s", exc)
        return f"I found {window.window_text()}, but Windows wouldn't let me focus it, sir."


# ----------------------------------------------------------------------- contents
def read_window(hint: str = "", max_items: int = MAX_TEXT_ITEMS) -> str:
    """Spoken summary of the text a window shows (labels, buttons, values)."""
    window = find_window(hint) if hint else _foreground()
    if window is None:
        return f"I can't see a window called {hint}, sir." if hint else "I can't tell which window is in front, sir."
    texts: list[str] = []
    for control in _safe_descendants(window):
        try:
            text = (control.window_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if 1 < len(text) <= 90 and text not in texts:
            texts.append(text)
        if len(texts) >= max_items:
            break
    if not texts:
        return f"{window.window_text()} doesn't expose readable text to me, sir."
    joined = "; ".join(texts[:max_items])
    return f"On {window.window_text()}: {joined}"


def _foreground():
    """The window the user is actually looking at."""
    try:
        import ctypes

        handle = ctypes.windll.user32.GetForegroundWindow()
        if not handle:
            return None
        from pywinauto import Desktop

        return Desktop(backend="uia").window(handle=handle)
    except Exception as exc:  # noqa: BLE001
        log.debug("foreground window lookup failed: %s", exc)
        return None


def type_text(text: str) -> str:
    """Type into whatever window currently has focus."""
    if not text:
        return "What would you like me to type, sir?"
    focused = _foreground()
    if focused is not None:
        try:
            title = (focused.window_text() or "").strip()
            if title.casefold().endswith("notepad"):
                ok, detail = type_in_notepad(text)
                return detail if ok else f"I couldn't verify the Notepad typing: {detail}"
        except Exception:  # noqa: BLE001 - continue with the generic focused-window path
            pass
    try:
        from pynput.keyboard import Controller
    except Exception:  # noqa: BLE001
        return "I can't reach the keyboard, sir."
    try:
        Controller().type(text)
        log.info("typed %d characters", len(text))
        return f"Typed it, sir."
    except Exception as exc:  # noqa: BLE001
        log.debug("type failed: %s", exc)
        return "I couldn't type that, sir."


def type_in_notepad(text: str, timeout_s: float = 10.0) -> tuple[bool, str]:
    """Launch Notepad, focus its confirmed window, type, and verify the UIA readback."""
    if not text:
        return False, "No text was supplied; nothing was typed."

    windir = os.environ.get("WINDIR", r"C:\Windows")
    fallback = os.path.join(windir, "System32", "notepad.exe")

    def find_notepad():
        try:
            for window in _desktop().windows():
                title = (window.window_text() or "").strip()
                if title.casefold().endswith("notepad"):
                    return window
        except Exception as exc:  # noqa: BLE001
            log.warning("Notepad window enumeration failed: %s", exc)
        return None

    window = find_notepad()
    if window is None:
        try:
            subprocess.Popen(["notepad.exe"])
        except Exception as first_error:  # noqa: BLE001
            log.info("notepad.exe launch failed; trying %s: %s", fallback, first_error)
            try:
                subprocess.Popen([fallback])
            except Exception as exc:  # noqa: BLE001
                return False, f"Notepad launch failed: {exc}"

    deadline = time.monotonic() + timeout_s
    while window is None and time.monotonic() < deadline:
        time.sleep(0.2)
        window = find_notepad()
    if window is None:
        return False, "Notepad window was not found; no text was typed."

    title = (window.window_text() or "").strip()
    if not title.casefold().endswith("notepad"):
        return False, f"Refusing to type: the selected window is not Notepad ({title!r})."
    try:
        window.set_focus()
        time.sleep(0.2)
        import ctypes

        if int(ctypes.windll.user32.GetForegroundWindow()) != int(window.handle):
            return False, "Notepad was found but could not be confirmed in the foreground; no text was typed."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not confirm Notepad focus; no text was typed: {exc}"

    try:
        from pynput.keyboard import Controller

        Controller().type(text)
        time.sleep(0.25)
        for control in _safe_descendants(window):
            try:
                control_type = control.element_info.control_type
                if control_type not in ("Edit", "Document"):
                    continue
                try:
                    actual = control.iface_value.CurrentValue
                except Exception:  # noqa: BLE001 - some UIA providers omit ValuePattern
                    actual = control.window_text()
                if actual is not None:
                    actual = str(actual)
                    if actual == text:
                        return True, f"Notepad readback matched exactly: {actual!r}"
                    return False, (
                        f"Notepad readback did not match; actual UIA value: {actual!r}"
                    )
            except Exception:  # noqa: BLE001
                continue
        return False, "Text was sent, but Notepad exposed no readable Edit/Document value to verify it."
    except Exception as exc:  # noqa: BLE001
        return False, f"Typing or Notepad readback failed: {exc}"


# -------------------------------------------------------------------------- click
_CLICKABLE_TYPES = {
    "Button", "CheckBox", "RadioButton", "MenuItem", "TabItem", "ListItem",
    "Hyperlink", "SplitButton", "TreeItem", "ComboBox",
}


def click_control(name: str, window_hint: str = "") -> str:
    """Click the control whose visible name matches `name`."""
    if not name:
        return "Which control, sir?"
    roots = [find_window(window_hint)] if window_hint else (
        [w for w in _desktop().windows()] if available() else []
    )
    needle = name.lower().strip()
    best = None
    best_score = 0.0
    for root in roots:
        if root is None:
            continue
        try:
            controls = root.descendants()
        except Exception:  # noqa: BLE001
            continue
        for control in controls:
            try:
                text = (control.window_text() or "").strip()
                kind = control.element_info.control_type
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            lowered = text.lower()
            if needle == lowered:
                score = 1000.0
            elif lowered.startswith(needle):
                score = 500.0 - len(text) * 0.1
            elif needle in lowered:
                score = 200.0 - len(text) * 0.1
            else:
                continue
            if kind in _CLICKABLE_TYPES:
                score += 50.0
            if score > best_score:
                best_score, best = score, (control, text, kind)
    if best is None:
        return f"I can't find anything called {name} on screen, sir."
    control, text, kind = best
    try:
        try:
            control.invoke()
        except Exception:  # noqa: BLE001 - not every control supports Invoke
            control.click_input()
        log.info("clicked %r (%s)", text, kind)
        return f"Clicked {text}, sir."
    except Exception as exc:  # noqa: BLE001
        log.debug("click failed: %s", exc)
        return f"I found {text}, but couldn't click it, sir."
