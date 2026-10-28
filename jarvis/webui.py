"""The local command centre.

Serves the dashboard and a small JSON API on 127.0.0.1 only. Nothing here is reachable
from the network, which matters because the email panel shows real inbox contents.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import CONFIG, DATA, ROOT

from .config import CONFIG, DATA, ROOT

from dotenv import set_key


def _env_path() -> Path:
    return ROOT / ".env"


def _save_gmail_creds(address: str, password: str) -> tuple[bool, str]:
    address = (address or "").strip()
    password = (password or "").replace(" ", "").strip()
    if not address or "@" not in address:
        return False, "Enter a valid Gmail address."
    if len(password) < 6:
        return False, "Enter a valid App Password."
    env = _env_path()
    try:
        env.touch(exist_ok=True)
        set_key(str(env), "GMAIL_ADDRESS", address, quote_mode="never")
        set_key(str(env), "GMAIL_APP_PASSWORD", password, quote_mode="never")
        os.environ["GMAIL_ADDRESS"] = address
        os.environ["GMAIL_APP_PASSWORD"] = password
        return True, "Connected."
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not save: {exc}"


log = logging.getLogger("webui")

UI_FILE = Path(__file__).resolve().parent / "ui" / "dashboard.html"
DEFAULT_PORT = 8765

_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_brain = None
_brain_lock = threading.Lock()
_command_lock = threading.Lock()


def _read_health() -> dict[str, Any]:
    path = DATA / "health.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["age_s"] = round(time.time() - float(data.get("heartbeat") or 0), 1)
        return data
    except Exception:  # noqa: BLE001
        return {}


def _events(rows) -> list[dict[str, Any]]:  # noqa: ANN001
    out = []
    for row in rows:
        out.append({
            "id": row["id"],
            "title": row["title"],
            "start_ts": row["start_ts"],
            "start_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(row["start_ts"])),
        })
    return out


def build_state() -> dict[str, Any]:
    """One snapshot for the dashboard. Each source fails soft."""
    from .calendar_store import Calendar
    from .email_client import summary_state
    from .reminders import Reminders
    from .skills import pulse

    calendar = Calendar()
    try:
        today = _events(calendar.on_day())
        upcoming = _events(calendar.upcoming(8))
    except Exception as exc:  # noqa: BLE001
        log.debug("calendar state failed: %s", exc)
        today, upcoming = [], []
    try:
        reminders = Reminders().pending()
    except Exception as exc:  # noqa: BLE001
        log.debug("reminder state failed: %s", exc)
        reminders = []
    try:
        inbox = summary_state(fast=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("email state failed: %s", exc)
        inbox = {"configured": False, "messages": [], "total": 0, "unread": 0, "error": str(exc)}
    try:
        system = pulse.snapshot()
    except Exception as exc:  # noqa: BLE001
        log.debug("pulse failed: %s", exc)
        system = {}
    try:
        from .activity import ActivityLog
        activity = ActivityLog().recent(40)
    except Exception:
        activity = []
    try:
        from .memory import Memory
        memory = Memory().stats()
    except Exception:
        memory = {}
    try:
        from .routines import RoutineStore
        routines = RoutineStore().list()
    except Exception:
        routines = []
    return {
        "now": time.time(),
        "now_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "address": CONFIG.user_title,
        "health": _read_health(),
        "reminders": reminders,
        "calendar": {"today": today, "upcoming": upcoming},
        "email": inbox,
        "pulse": system,
        "activity": activity,
        "memory": memory,
        "routines": routines,
    }


def _get_brain():
    global _brain
    if _brain is None:
        with _brain_lock:
            if _brain is None:
                from .brain.llm import Brain

                _brain = Brain()
    return _brain


def handle_command(text: str) -> dict[str, Any]:
    """Route a dashboard command through the same fast path, then the brain."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "reply": "", "handled": False, "actions": []}
    with _command_lock:
        try:
            from .router import Router

            handled, reply = Router().handle(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard route failed: %s", exc)
            handled, reply = False, ""
        if handled:
            return {"ok": True, "reply": reply, "handled": True, "actions": []}
        try:
            reply, actions = _get_brain().respond(text)
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard brain failed: %s", exc)
            return {"ok": False, "reply": "I couldn't reach my brain, sir.",
                    "handled": False, "actions": []}
        return {"ok": True, "reply": reply, "handled": False, "actions": actions}


class _Handler(BaseHTTPRequestHandler):
    server_version = "JarvisUI/1.0"

    # ------------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    # ----------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                html = UI_FILE.read_bytes()
            except Exception as exc:  # noqa: BLE001
                self._send(500, f"dashboard unavailable: {exc}".encode(), "text/plain")
                return
            self._send(200, html, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/state":
            self._json(build_state())
            return
        if parsed.path == "/api/email":
            uid = (parse_qs(parsed.query).get("uid") or [""])[0]
            from .email_client import read

            message = read(uid)
            if not message:
                self._json({"ok": False, "error": "could not read that email"}, 404)
                return
            self._json({"ok": True, "message": message})
            return
        if parsed.path == "/api/email/test":
            from . import email_client
            ok, detail = email_client.probe()
            self._json({"ok": ok, "detail": detail})
            return
        self._json({"error": "not found"}, 404)

    # ---------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/command":
            payload = self._body()
            self._json(handle_command(str(payload.get("text") or "")))
            return
        if parsed.path == "/api/draft":
            payload = self._body()
            from .email_client import by_uid, save_draft

            original = by_uid(str(payload.get("uid") or ""))
            if not original:
                self._json({"ok": False, "message": "I don't have that email, sir."}, 404)
                return
            ok, message = save_draft(original, str(payload.get("body") or ""))
            self._json({"ok": ok, "message": message}, 200 if ok else 400)
            return
        if parsed.path == "/api/email/connect":
            payload = self._body()
            address = str(payload.get("address") or "").strip()
            password = str(payload.get("password") or "")
            from . import email_client

            test_ok, detail = email_client.test_credentials(address, password)
            if not test_ok:
                self._json({"ok": False, "message": detail}, 400)
                return
            ok, message = _save_gmail_creds(address, password)
            if not ok:
                self._json({"ok": False, "message": message}, 400)
                return
            email_client.reset_cache()
            email_client.warm()
            self._json({"ok": True, "message": detail})
            return
        self._json({"error": "not found"}, 404)


# ------------------------------------------------------------------ server control
def start(port: int | None = None) -> ThreadingHTTPServer | None:
    """Start the dashboard server once. Safe to call from several threads."""
    global _server, _server_thread
    if _server is not None:
        return _server
    chosen = int(port or CONFIG.dashboard_port or DEFAULT_PORT)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", chosen), _Handler)
    except OSError as exc:
        log.warning("dashboard server could not bind 127.0.0.1:%s: %s", chosen, exc)
        return None
    server.daemon_threads = True
    _server = server
    _server_thread = threading.Thread(target=server.serve_forever, name="dashboard",
                                      daemon=True)
    _server_thread.start()
    log.info("dashboard on http://127.0.0.1:%s/", chosen)
    try:
        from .email_client import warm as warm_email

        warm_email()
    except Exception as exc:  # noqa: BLE001 - email is optional
        log.debug("email warm-up skipped: %s", exc)
    return server


def stop() -> None:
    global _server, _server_thread
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:  # noqa: BLE001
            pass
    _server = None
    _server_thread = None


def url() -> str:
    port = _server.server_address[1] if _server is not None else (CONFIG.dashboard_port or DEFAULT_PORT)
    return f"http://127.0.0.1:{port}/"


def open_dashboard() -> str:
    """Start the server if needed and open it. Returns the spoken reply."""
    if start() is None:
        try:
            webbrowser.open(UI_FILE.as_uri())
            return "I couldn't start the live server, sir, so I opened a static copy."
        except Exception:  # noqa: BLE001
            return "I couldn't start the dashboard, sir."
    webbrowser.open(url())
    return "Opening the command centre, sir."
