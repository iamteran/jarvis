"""The toolbelt: everything Jarvis can actually do, exposed to the language model.

The router handles the twenty most common spoken commands with no LLM at all (fast and
offline). This module is the other half: the model reads these descriptions and decides
for itself which tool to call, so "put the folder where my CVs live in front of me" works
without a regex for it.

Safety rule: only reversible, non-destructive tools live here. Deleting, shutting down and
sending messages go through a spoken confirmation gate first (see jarvis/safety.py).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("tools")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[..., str]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ------------------------------------------------------------------------ schemas
def _obj(**props) -> dict:
    required = [name for name, spec in props.items() if spec.pop("_required", False)]
    return {
        "type": "object",
        "properties": {name: spec for name, spec in props.items()},
        "required": required,
    }


def _str(desc: str, required: bool = False) -> dict:
    return {"type": "string", "description": desc, "_required": required}


def _int(desc: str, required: bool = False, minimum: int | None = None,
         maximum: int | None = None) -> dict:
    spec: dict[str, Any] = {"type": "integer", "description": desc, "_required": required}
    if minimum is not None:
        spec["minimum"] = minimum
    if maximum is not None:
        spec["maximum"] = maximum
    return spec


class Tools:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._register_all()

    # ------------------------------------------------------------------ plumbing
    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def execute(self, name: str, arguments: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool {name!r}."
        started = time.perf_counter()
        try:
            result = tool.run(**(arguments or {}))
            log.info("tool %s(%s) -> %.0f ms: %s", name, _short(arguments),
                     (time.perf_counter() - started) * 1000, _short_text(result))
            return str(result)
        except TypeError as exc:
            log.warning("tool %s bad arguments %s: %s", name, _short(arguments), exc)
            return f"I couldn't call {name}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a failed tool must not kill the turn
            log.exception("tool %s failed", name)
            return f"{name} failed: {exc}"

    # -------------------------------------------------------------------- tools
    def _register_all(self) -> None:
        self.add(Tool(
            "open_app",
            "Open an installed Windows application or a well-known web app by name.",
            _obj(app=_str("Application name, e.g. chrome, notepad, whatsapp, spotify", True)),
            self._open_app,
        ))
        self.add(Tool(
            "open_url",
            "Open a website in the default browser.",
            _obj(url=_str("Full URL or site name, e.g. github.com", True)),
            self._open_url,
        ))
        self.add(Tool(
            "web_search",
            "Search the web for something. Opens the results in the browser.",
            _obj(query=_str("What to search for", True)),
            self._web_search,
        ))
        self.add(Tool(
            "list_windows",
            "List the titles of the windows currently open on the desktop.",
            _obj(),
            self._list_windows,
        ))
        self.add(Tool(
            "switch_window",
            "Bring an open window to the front by (part of) its title.",
            _obj(title=_str("Part of the window title, e.g. chrome, explorer", True)),
            self._switch_window,
        ))
        self.add(Tool(
            "read_screen",
            "Read the text and controls visible in a window (default: the focused one).",
            _obj(window=_str("Optional window title; omit for the focused window")),
            self._read_screen,
        ))
        self.add(Tool(
            "click_control",
            "Click a named button, link, tab or menu item in a window.",
            _obj(name=_str("Visible label of the control, e.g. Save, Send", True),
                 window=_str("Optional window title")),
            self._click_control,
        ))
        self.add(Tool(
            "type_text",
            "Type text into the currently focused window.",
            _obj(text=_str("Exact text to type", True)),
            self._type_text,
        ))
        self.add(Tool(
            "press_keys",
            "Press a key combination in the focused window, e.g. ctrl+s, alt+tab, enter.",
            _obj(keys=_str("Combination like ctrl+shift+s", True)),
            self._press_keys,
        ))
        self.add(Tool(
            "set_volume",
            "Set the system output volume to an exact percentage.",
            _obj(percent=_int("0 to 100", True, 0, 100)),
            self._set_volume,
        ))
        self.add(Tool(
            "change_volume",
            "Turn the system volume up or down by a number of percentage points.",
            _obj(delta=_int("Positive turns up, negative turns down", True, -100, 100)),
            self._change_volume,
        ))
        self.add(Tool(
            "mute_audio",
            "Mute or unmute the system audio.",
            _obj(),
            self._mute,
        ))
        self.add(Tool(
            "lock_screen",
            "Lock the Windows session immediately.",
            _obj(),
            self._lock_screen,
        ))
        self.add(Tool(
            "tell_time",
            "Report the current local time.",
            _obj(),
            self._tell_time,
        ))
        self.add(Tool(
            "tell_date",
            "Report today's date.",
            _obj(),
            self._tell_date,
        ))
        self.add(Tool(
            "find_file",
            "Find files by name across the user's common folders.",
            _obj(name=_str("File name or part of it, e.g. cv, invoice", True)),
            self._find_file,
        ))
        self.add(Tool(
            "open_file",
            "Open a file or folder with its default application.",
            _obj(path=_str("Absolute path to the file", True)),
            self._open_file,
        ))
        self.add(Tool(
            "recall_memory",
            "Look up something the user asked you to remember.",
            _obj(query=_str("What to look for; omit for everything you remember")),
            self._recall_memory,
        ))
        self.add(Tool(
            "remember_fact",
            "Store a durable fact or preference about the user for later sessions.",
            _obj(fact=_str("The fact to remember, phrased in the third person", True)),
            self._remember_fact,
        ))
        self.add(Tool(
            "undo_last_action",
            "Undo the most recent reversible action (e.g. restore the previous volume).",
            _obj(),
            self._undo,
        ))
        self.add(Tool(
            "read_file",
            "Read a document and return its text so you can summarise or answer about it. "
            "Supports txt, md, docx, pdf, xlsx, pptx.",
            _obj(path=_str("Absolute path, or an alias the user taught you", True)),
            self._read_file,
        ))
        self.add(Tool(
            "list_folder",
            "List what is inside a folder.",
            _obj(path=_str("Absolute folder path, or an alias", True)),
            self._list_folder,
        ))
        self.add(Tool(
            "delete_file",
            "Move a file or folder to the Recycle Bin. This asks the user to confirm first.",
            _obj(path=_str("Absolute path, or an alias", True)),
            self._delete_file,
        ))
        self.add(Tool(
            "set_reminder",
            "Set a reminder or timer that speaks at the right time.",
            _obj(text=_str("What to remind the user about", True),
                 minutes=_int("How many minutes from now", True, 1, 10080)),
            self._set_reminder,
        ))
        self.add(Tool(
            "set_reminder_at",
            "Set a reminder for a spoken date and time, e.g. '12 November at 7:30'.",
            _obj(text=_str("What to remind the user about", True),
                 when=_str("Spoken date and time", True)),
            self._set_reminder_at,
        ))
        self.add(Tool(
            "list_reminders",
            "List pending reminders.",
            _obj(),
            self._list_reminders,
        ))
        self.add(Tool(
            "cancel_reminders",
            "Cancel all pending reminders.",
            _obj(),
            self._cancel_reminders,
        ))
        self.add(Tool(
            "add_appointment",
            "Add a calendar appointment. 'when' is spoken text such as "
            "'tomorrow at 6 pm' or 'on 12 October at 7:30'.",
            _obj(title=_str("What the appointment is, e.g. dentist", True),
                 when=_str("Spoken date and time", True)),
            self._add_appointment,
        ))
        self.add(Tool(
            "check_schedule",
            "Read the calendar: what is scheduled today, or what is coming up next.",
            _obj(scope=_str("'today' or 'upcoming'", False)),
            self._check_schedule,
        ))
        self.add(Tool(
            "close_app",
            "Close an application window. Asks the user to confirm first.",
            _obj(title=_str("Part of the window title, e.g. notepad", True)),
            self._close_app,
        ))
        self.add(Tool(
            "look_at_screen",
            "Look at the user's screen with vision and answer a question about it, e.g. "
            "'what does this error mean?'. Slower than read_screen but understands images.",
            _obj(question=_str("What to answer about the screen", True)),
            self._look_at_screen,
        ))
        self.add(Tool(
            "take_screenshot",
            "Save a screenshot of the screen to the Desktop and return its path.",
            _obj(),
            self._take_screenshot,
        ))
        self.add(Tool(
            "shutdown_pc",
            "Shut the computer down after a short delay. Asks the user to confirm first.",
            _obj(minutes=_int("Delay in minutes before shutting down", False, 0, 120)),
            self._shutdown_pc,
        ))

    # ------------------------------------------------------------------ handlers
    def _open_app(self, app: str) -> str:
        from .skills.apps import AppLauncher

        ok, reply = AppLauncher().launch_app(app)
        return reply

    def _open_url(self, url: str) -> str:
        from .skills.apps import AppLauncher

        return AppLauncher().open_site(url)[1]

    def _web_search(self, query: str) -> str:
        import webbrowser

        webbrowser.open("https://www.google.com/search?q=" + re.sub(r"\s+", "+", query))
        return f"Searching the web for {query}, sir."

    def _list_windows(self) -> str:
        from .skills import screen

        titles = screen.list_windows()
        if not titles:
            return "No windows are open, sir."
        return "Open windows: " + "; ".join(titles[:12]) + "."

    def _switch_window(self, title: str) -> str:
        from .skills import screen

        return screen.activate(title)

    def _read_screen(self, window: str = "") -> str:
        from .skills import screen

        return screen.read_window(window or "")

    def _click_control(self, name: str, window: str = "") -> str:
        from .skills import screen

        return screen.click_control(name, window or "")

    def _type_text(self, text: str) -> str:
        from .skills import screen

        return screen.type_text(text)

    def _press_keys(self, keys: str) -> str:
        try:
            from pynput.keyboard import Controller, Key
        except Exception:  # noqa: BLE001
            return "I can't reach the keyboard, sir."
        parts = [p.strip().lower() for p in re.split(r"[+\s]+", keys) if p.strip()]
        if not parts:
            return "Which keys, sir?"
        modifier_names = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
                          "win": "cmd", "windows": "cmd", "cmd": "cmd"}
        special = {"enter": "enter", "tab": "tab", "esc": "esc", "escape": "esc",
                   "space": "space", "backspace": "backspace", "delete": "delete",
                   "up": "up", "down": "down", "left": "left", "right": "right",
                   "home": "home", "end": "end", "pageup": "page_up", "pagedown": "page_down"}
        board = Controller()
        held = []
        try:
            for part in parts[:-1]:
                key = getattr(Key, modifier_names.get(part, part), None)
                if key is not None:
                    board.press(key)
                    held.append(key)
            last = parts[-1]
            target = getattr(Key, special.get(last, last), None)
            if target is not None:
                board.press(target)
                board.release(target)
            else:
                board.type(last)
            return f"Pressed {keys}, sir."
        finally:
            for key in reversed(held):
                try:
                    board.release(key)
                except Exception:  # noqa: BLE001
                    pass

    def _set_volume(self, percent: int) -> str:
        from .skills import system

        return system.set_volume(int(percent))

    def _change_volume(self, delta: int) -> str:
        from .skills import system

        return system.volume_up() if delta > 0 else system.volume_down()

    def _mute(self) -> str:
        from .skills import system

        return system.mute()

    def _lock_screen(self) -> str:
        from .skills import system

        return system.lock_screen()

    def _tell_time(self) -> str:
        from .skills import system

        return system.now_speech()

    def _tell_date(self) -> str:
        from .skills import system

        return system.date_speech()

    def _find_file(self, name: str) -> str:
        from .skills import files

        match = files.find_first(name)
        if match is None:
            return f"I couldn't find a file matching {name}, sir."
        return f"Found it: {match}"

    def _open_file(self, path: str) -> str:
        target = Path(path).expanduser()
        if not target.exists():
            return f"{path} doesn't exist, sir."
        os.startfile(str(target))  # noqa: S606 - the user asked for it
        return f"Opened {target.name}, sir."

    def _recall_memory(self, query: str = "") -> str:
        from .memory import Memory

        return Memory().recall_speech(query)

    def _remember_fact(self, fact: str) -> str:
        from .memory import Memory

        Memory().remember(fact)
        return "Noted, sir."

    def _undo(self) -> str:
        from .undo import pop_undo

        return pop_undo() or "There's nothing to undo, sir."

    # ------------------------------------------------------------- files & memory
    def _read_file(self, path: str) -> str:
        from .skills import files

        resolved = files.resolve_path(path) or path
        return files.read_file_for_model(resolved)

    def _list_folder(self, path: str) -> str:
        from .skills import files

        resolved = files.resolve_path(path) or path
        return files.list_folder(resolved)

    def _delete_file(self, path: str) -> str:
        from .skills import files

        resolved = files.resolve_path(path) or path
        return files.request_delete(resolved)

    # ----------------------------------------------------------------- reminders
    def _set_reminder(self, text: str, minutes: int) -> str:
        from .reminders import Reminders

        return Reminders().add(text, float(minutes))

    def _list_reminders(self) -> str:
        from .reminders import Reminders

        return Reminders().list_speech()

    def _cancel_reminders(self) -> str:
        from .reminders import Reminders

        count = Reminders().cancel_all()
        return f"Cancelled {count} reminder{'s' if count != 1 else ''}, sir." if count else "There were none pending, sir."

    def _set_reminder_at(self, text: str, when: str) -> str:
        from .nlp_time import parse_when
        from .reminders import Reminders

        parsed = parse_when(when)
        if parsed is None:
            return f"I couldn't work out the date and time from {when!r}, sir."
        return Reminders().add_at(text, parsed.value.timestamp())

    # ------------------------------------------------------------- calendar
    def _add_appointment(self, title: str, when: str) -> str:
        from .calendar_store import Calendar
        from .nlp_time import parse_when

        parsed = parse_when(when)
        if parsed is None:
            return f"I couldn't work out the date and time from {when!r}, sir."
        return Calendar().add(title, parsed.value)

    def _check_schedule(self, scope: str = "today") -> str:
        from .calendar_store import Calendar

        calendar = Calendar()
        if "upcoming" in (scope or "").lower() or "next" in (scope or "").lower():
            return calendar.speech_upcoming()
        return calendar.speech_today()

    # ------------------------------------------------------------------ vision
    def _look_at_screen(self, question: str) -> str:
        from .vision import describe

        return describe(question)

    def _take_screenshot(self) -> str:
        from .vision import save_screenshot

        path = save_screenshot()
        return f"Saved the screenshot to {path}, sir."

    # ------------------------------------------------------------- system actions
    def _close_app(self, title: str) -> str:
        from .safety import request
        from .skills import screen

        window = screen.find_window(title)
        if window is None:
            return f"I can't see a window called {title}, sir."

        def _close() -> str:
            try:
                window.close()
                return f"Closed {title}, sir."
            except Exception as exc:  # noqa: BLE001
                return f"I couldn't close {title}, sir: {exc}"

        return request(f"close {title}", "Anything unsaved in that window will be lost.",
                       _close)

    def _shutdown_pc(self, minutes: int = 0) -> str:
        from .safety import request

        def _shutdown() -> str:
            import subprocess

            delay = max(0, int(minutes)) * 60
            subprocess.Popen(["shutdown", "/s", "/t", str(delay)])
            return "Shutting down, sir. Goodnight." if delay == 0 else f"Shutting down in {minutes} minutes, sir."

        detail = "The computer will shut down immediately." if not minutes else f"It will shut down in {minutes} minutes."
        return request("shut the computer down", detail, _shutdown)


def _short(arguments: dict) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (arguments or {}).items())
    return text[:90]


def _short_text(result: str) -> str:
    return (result or "").replace("\n", " ")[:110]
