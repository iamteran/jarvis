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
    def __init__(self, reminder_service=None) -> None:  # noqa: ANN001
        self._tools: dict[str, Tool] = {}
        # Optional reference to the live ReminderService so tool calls hit the same
        # SQLite DB instance as the background polling thread.
        self._reminder_service = reminder_service
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
        self.add(Tool(
            "get_weather",
            "Get the current weather at a given city, or the user's local weather if no city "
            "is provided. Returns a short spoken summary.",
            _obj(city=_str("City name, e.g. Nairobi. Leave blank for local weather.")),
            self._get_weather,
        ))
        self.add(Tool(
            "forget_memory",
            "Delete a fact from persistent memory that the user no longer wants remembered.",
            _obj(query=_str("The fact or topic to forget, e.g. 'cilantro preference'", True)),
            self._forget_memory,
        ))
        self.add(Tool(
            "set_alias",
            "Teach Jarvis a spoken shortcut for a path or concept, e.g. "
            "'my music folder' -> 'D:/Music'. The alias persists across sessions.",
            _obj(name=_str("The spoken shortcut the user will use, e.g. 'my cv folder'", True),
                 target=_str("What it actually refers to, e.g. an absolute path", True)),
            self._set_alias,
        ))
        self.add(Tool(
            "run_command",
            "Run a shell command on Windows to diagnose or fix an issue. Use when the user says 'run this', 'fix this', or you need to execute a command to solve a problem shown on screen.",
            _obj(command=_str("The exact command to run, e.g. ipconfig /all, winget --version, sfc /scannow", True),
                 shell=_str("Shell to use: cmd, powershell, or pwsh. Defaults to cmd.", False),
                 cwd=_str("Optional working directory", False)),
            self._run_command,
        ))
        self.add(Tool(
            "run_in_terminal",
            "Open Windows Terminal/Command Prompt/PowerShell and run a command there so the user can see output. Best for long-running fixes.",
            _obj(command=_str("Command to run in the terminal", True),
                 shell=_str("cmd, powershell, or wt. Defaults to powershell.", False)),
            self._run_in_terminal,
        ))
        self.add(Tool(
            "type_and_enter",
            "Type text into the focused window and press Enter. Great for running commands in a terminal or filling and submitting forms.",
            _obj(text=_str("Text to type", True)),
            self._type_and_enter,
        ))
        self.add(Tool(
            "focus_app",
            "Focus/bring an app window to the front by name.",
            _obj(name=_str("App or window name, e.g. terminal, powershell, cmd, chrome", True)),
            self._focus_app,
        ))
        self.add(Tool(
            "check_email",
            "Read today's Gmail inbox: how many arrived, who they are from and the subjects.",
            _obj(limit=_int("How many recent emails to summarise (1-20)", False, 1, 20)),
            self._check_email,
        ))
        self.add(Tool(
            "read_email",
            "Read out the full text of an email, e.g. the latest one or one from a named sender. "
            "Use this when the user says 'read the email from ...'.",
            _obj(who=_str("Sender, subject fragment, or 'latest'", False)),
            self._read_email,
        ))
        self.add(Tool(
            "draft_email_reply",
            "Save a reply in Gmail Drafts. This never sends: the user presses Send in Gmail. "
            "Use when the user says 'reply to that email saying ...'.",
            _obj(body=_str("The reply text to save", True),
                 who=_str("Sender or subject fragment; omit for the latest email", False)),
            self._draft_email_reply,
        ))
        self.add(Tool(
            "system_pulse",
            "Report the laptop's battery, CPU, memory, disk space and uptime.",
            _obj(),
            self._system_pulse,
        ))
        # Optional: only register once its handler exists, so a half-finished skill
        # cannot stop the whole toolbelt (and Jarvis) from starting.
        if hasattr(self, "_cloud"):
            self.add(Tool(
                "cloud",
                "Use an Instabox Linux cloud box (name 'jarvis') for builds, installs, servers, long-running jobs, or anything needing Linux, Docker or to run off Windows. Screen/mic/keyboard/volume stay local.",
                _obj(
                    action=_str("exec|status|files|publish|ssh_cmd|start|stop", True),
                    cmd=_str("Shell command for exec/ssh_cmd", False),
                    port=_int("Port for publish (1-65535)", False, 1, 65535),
                    name=_str("Box name (defaults to jarvis)", False),
                    cwd=_str("Working dir in the box (default HOME)", False),
                ),
                self._cloud,
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
        # Use the live service's Reminders object when available, so this
        # reminder is seen by the same background polling thread that fires them.
        reminders = (
            self._reminder_service.reminders
            if self._reminder_service is not None
            else __import__("jarvis.reminders", fromlist=["Reminders"]).Reminders()
        )
        return reminders.add(text, float(minutes))

    def _list_reminders(self) -> str:
        reminders = (
            self._reminder_service.reminders
            if self._reminder_service is not None
            else __import__("jarvis.reminders", fromlist=["Reminders"]).Reminders()
        )
        return reminders.list_speech()

    def _cancel_reminders(self) -> str:
        reminders = (
            self._reminder_service.reminders
            if self._reminder_service is not None
            else __import__("jarvis.reminders", fromlist=["Reminders"]).Reminders()
        )
        count = reminders.cancel_all()
        return (f"Cancelled {count} reminder{'s' if count != 1 else ''}, sir."
                if count else "There were none pending, sir.")

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

    # -------------------------------------------------------------------- email
    def _check_email(self, limit: int = 8) -> str:
        from .email_client import summary_speech

        return summary_speech(int(limit))

    def _read_email(self, who: str = "latest") -> str:
        from .email_client import read_speech

        return read_speech((who or "latest").strip())

    def _draft_email_reply(self, body: str, who: str = "latest") -> str:
        from .email_client import reply_speech

        return reply_speech((who or "latest").strip(), body)

    # --------------------------------------------------------------------- pulse
    def _system_pulse(self) -> str:
        from .skills import pulse

        return pulse.speech()

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

    # ------------------------------------------------------------------- weather
    def _get_weather(self, city: str = "") -> str:
        import urllib.request as _req

        location = (city or "").strip() or ""
        slug = location.replace(" ", "+") if location else ""
        url = f"https://wttr.in/{slug}?format=3"
        try:
            with _req.urlopen(url, timeout=4) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", errors="ignore").strip()
            if raw and len(raw) < 200:
                return raw + "."
            return "I couldn't get a weather summary just now, sir."
        except Exception as exc:  # noqa: BLE001
            log.warning("weather tool failed: %s", exc)
            return "My weather source is unreachable at the moment, sir."

    # ------------------------------------------------------------------- memory extras
    def _forget_memory(self, query: str) -> str:
        from .memory import Memory

        count = Memory().forget(query)
        if count == 0:
            return f"I couldn't find anything matching {query!r} to forget, sir."
        return f"Done. I've forgotten {count} fact{'s' if count != 1 else ''} about that, sir."

    def _set_alias(self, name: str, target: str) -> str:
        from .memory import Memory

        Memory().set_alias(name.strip(), target.strip())
        return f"Noted, sir. When you say \"{name}\", I'll understand you mean {target}."

    def _run_command(self, command: str, shell: str = "cmd", cwd: str = "") -> str:
        from . import safety

        cmd_lower = (command or "").lower()
        dangerous = ["shutdown", "restart", "rmdir /s", "del /s", "format", "diskpart", "bcdedit", "reg delete", "cipher /w", "takeown /f"]
        if any(d in cmd_lower for d in dangerous):
            def _do() -> str:
                return self._run_command_internal(command, shell, cwd)
            return safety.request("run a potentially destructive command", f"Command: {command}", _do)
        return self._run_command_internal(command, shell, cwd)

    def _run_command_internal(self, command: str, shell: str = "cmd", cwd: str = "") -> str:
        try:
            shell_l = (shell or "cmd").lower()
            if shell_l in ("powershell", "pwsh", "ps"):
                exe = "powershell.exe" if shell_l == "powershell" else "pwsh.exe"
                args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
                proc = subprocess.run([exe] + args, cwd=cwd or None, capture_output=True, text=True, timeout=300)
            elif shell_l == "cmd":
                proc = subprocess.run(["cmd", "/c", command], cwd=cwd or None, capture_output=True, text=True, timeout=300)
            else:
                proc = subprocess.run(command, cwd=cwd or None, shell=True, capture_output=True, text=True, timeout=300)
            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode == 0:
                if out:
                    return f"Done. Output:\n{out[:800]}"
                return "Done, sir. Command completed successfully."
            msg = err or out or f"exit code {proc.returncode}"
            return f"Command failed ({proc.returncode}): {msg[:600]}"
        except subprocess.TimeoutExpired:
            return "That command is taking longer than expected, sir. It may still be running in the background."
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't run that command: {exc}"

    def _run_in_terminal(self, command: str, shell: str = "powershell") -> str:
        try:
            shell_l = (shell or "powershell").lower()
            if shell_l in ("wt", "terminal"):
                subprocess.Popen(["wt", "new-tab", "powershell", "-NoExit", "-Command", command])
                return f"Opened Windows Terminal and running: {command}, sir."
            elif shell_l in ("powershell", "pwsh"):
                exe = "powershell.exe" if shell_l == "powershell" else "pwsh.exe"
                subprocess.Popen([exe, "-NoExit", "-Command", command])
                return f"Opened PowerShell and running: {command}, sir."
            else:
                subprocess.Popen(["cmd", "/k", command])
                return f"Opened Command Prompt and running: {command}, sir."
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't open a terminal: {exc}"

    def _type_and_enter(self, text: str) -> str:
        try:
            from pynput.keyboard import Controller
            Controller().type(text)
            time.sleep(0.1)
            from pynput.keyboard import Key
            Controller().press(Key.enter)
            Controller().release(Key.enter)
            return f"Typed and sent: {text}, sir."
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't type: {exc}"

    def _focus_app(self, name: str) -> str:
        from .skills import screen
        return screen.activate(name)

    def _cloud(self, action: str, cmd: str = "", port: int = 0, name: str = "", cwd: str = "") -> str:
        import subprocess, shutil

        box = (name or "jarvis").strip()
        act = (action or "").strip().lower()
        exe = shutil.which("instabox") or r"C:\Users\Brian\AppData\Roaming\npm\instabox.cmd"
        try:
            if act == "status":
                proc = subprocess.run([exe, "ls"], capture_output=True, text=True, timeout=60)
                out = (proc.stdout or "") + (proc.stderr or "")
                if box in out and "Running" in out:
                    return f"Box {box} is running."
                return f"Box status:\n{out.strip()[:600]}"
            if act == "start":
                proc = subprocess.run([exe, "start", box], capture_output=True, text=True, timeout=300)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:600] or "Done."
            if act == "stop":
                proc = subprocess.run([exe, "stop", box], capture_output=True, text=True, timeout=300)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:600] or "Done."
            if act == "files":
                path = (cwd or cmd or "/data/home").strip()
                proc = subprocess.run([exe, "files", box, path], capture_output=True, text=True, timeout=120)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:900]
            if act == "publish":
                if not port:
                    return "Port number required, sir."
                proc = subprocess.run([exe, "port", "publish", box, str(int(port))], capture_output=True, text=True, timeout=120)
                out = (proc.stdout or "") + (proc.stderr or "")
                return out.strip()[:600]
            if act == "exec":
                if not cmd:
                    return "Command required, sir."
                args = [exe, "exec", box]
                if cwd:
                    args += ["--cwd", cwd]
                args += ["--", cmd]
                proc = subprocess.run(args, capture_output=True, text=True, timeout=610)
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                text = out or err or ""
                if proc.returncode == 0:
                    return f"Done. Output:\n{text[:900]}" if text else "Done, sir. Command completed successfully."
                msg = text or f"exit code {proc.returncode}"
                return f"Command failed ({proc.returncode}): {msg[:700]}"
            if act == "ssh_cmd":
                if not cmd:
                    return "Command required, sir."
                full = f"cd {cwd} && {cmd}" if cwd else cmd
                proc = subprocess.run([exe, "ssh", box, "--", "bash", "-lc", full], capture_output=True, text=True, timeout=3600)
                out = (proc.stdout or "").strip()
                err = (proc.stderr or "").strip()
                text = out or err or ""
                if proc.returncode == 0:
                    return f"Done. Output:\n{text[:900]}" if text else "Done, sir. Command completed successfully."
                msg = text or f"exit code {proc.returncode}"
                return f"Command failed ({proc.returncode}): {msg[:700]}"
            return "Unknown action. Use exec, status, files, publish, ssh_cmd, start, or stop."
        except subprocess.TimeoutExpired:
            return "That box command timed out, sir. For long jobs use ssh_cmd or run in background."
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't reach the box: {exc}"


def _short(arguments: dict) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in (arguments or {}).items())
    return text[:90]


def _short_text(result: str) -> str:
    return (result or "").replace("\n", " ")[:110]
