"""Minimal, fail-closed Telegram long-poll bridge for local Jarvis commands."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Callable, Optional

log = logging.getLogger("telegram")


class TelegramBridge:
    def __init__(
        self,
        token: str,
        allowed_user_id: str,
        on_message: Callable[[str], str],
    ) -> None:
        self._token = token.strip()
        self._allowed_user_id = str(allowed_user_id or "").strip()
        self._on_message = on_message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._token:
            return
        self._thread = threading.Thread(
            target=self._poll, name="telegram", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _api(self, method: str, payload: dict) -> dict:
        # Keep the bot token out of logs and exception messages.
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=35) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError(type(exc).__name__) from None
        if not result.get("ok"):
            raise RuntimeError("Telegram API returned ok=false")
        return result

    def _send(self, chat_id: int, text: str) -> None:
        self._api("sendMessage", {
            "chat_id": chat_id,
            "text": (text or "I couldn't produce a reply.")[:4000],
        })

    def send_file(self, chat_id: int, file_path: str, caption: str = "") -> bool:
        """Send a file to the authorized chat. Returns True on success."""
        if not os.path.isfile(file_path):
            log.warning("Telegram send_file: file not found: %s", file_path)
            return False
        if not self._allowed_user_id:
            log.warning("Telegram send_file: no authorized user configured")
            return False
        try:
            import requests
        except ImportError:
            log.error("Telegram send_file: requests module not available")
            return False

        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = "application/octet-stream"

        filename = os.path.basename(file_path)
        url = f"https://api.telegram.org/bot{self._token}/sendDocument"
        try:
            with open(file_path, "rb") as f:
                files = {"document": (filename, f, mime_type)}
                data = {"chat_id": chat_id, "caption": caption[:1024] if caption else ""}
                response = requests.post(url, files=files, data=data, timeout=60)
                response.raise_for_status()
                result = response.json()
                if result.get("ok"):
                    log.info("Telegram: sent file %s to chat %s", filename, chat_id)
                    return True
                log.warning("Telegram send_file failed: %s", result)
                return False
        except Exception as exc:  # noqa: BLE001
            log.exception("Telegram send_file error: %s", exc)
            return False

    def _poll(self) -> None:
        offset: int | None = None
        delay = 1.0
        log.info("Telegram command listener started (token hidden)")
        while not self._stop.is_set():
            try:
                payload: dict = {"timeout": 25, "allowed_updates": ["message"]}
                if offset is not None:
                    payload["offset"] = offset
                updates = self._api("getUpdates", payload).get("result", [])
                delay = 1.0
                for update in updates:
                    offset = max(offset or 0, int(update.get("update_id", 0)) + 1)
                    self._handle_update(update)
            except Exception as exc:  # keep the listener alive on network/API errors
                if not self._stop.is_set():
                    log.warning("Telegram polling error (%s); retrying", type(exc).__name__)
                    self._stop.wait(delay)
                    delay = min(delay * 2, 30.0)

    def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = (message.get("text") or "").strip()
        chat_id = chat.get("id")
        user_id = str(sender.get("id", ""))
        if chat.get("type") != "private" or not isinstance(chat_id, int) or not text:
            return

        # /id is the only response available before an owner ID is configured.
        if text.split()[0].split("@")[0].lower() == "/id":
            status = (
                "This account is authorized."
                if self._allowed_user_id and user_id == self._allowed_user_id
                else "Not authorized yet. Add this numeric sender ID to "
                     "JARVIS_TELEGRAM_ALLOWED_USER_ID in your local .env, then restart Jarvis."
            )
            self._send(chat_id, f"Your Telegram user ID is {user_id}. {status}")
            return
        if not self._allowed_user_id or user_id != self._allowed_user_id:
            self._send(chat_id, "Jarvis isn't authorized for this Telegram account.")
            return
        if text.split()[0].split("@")[0].lower() == "/start":
            self._send(chat_id, "Jarvis is connected. Send me a command.")
            return
        try:
            reply = self._on_message(text)
            self._send(chat_id, reply)
        except Exception as exc:  # never leak token or stack traces to Telegram
            log.exception("Telegram command failed")
            self._send(chat_id, f"Sorry, that command failed ({type(exc).__name__}).")
