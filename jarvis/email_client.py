"""Gmail over IMAP: read the inbox, read a message, and save replies as drafts.

App Password only (no OAuth): the user enables 2-Step Verification, creates a
16-character App Password, and puts it in `.env`. Nothing is ever sent — replying
appends a message to Gmail's Drafts folder, where the user presses Send.
"""
from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import threading
import time
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from typing import Any

from .config import CONFIG

log = logging.getLogger("email")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
_TIMEOUT_S = 20

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL_S = 30.0

# The last list we handed out, so "reply to that email" knows which message it means.
_last_messages: list[dict[str, Any]] = []
_last_error = ""


def configured() -> bool:
    return bool(_address() and _password())


def _address() -> str:
    return (os.environ.get("GMAIL_ADDRESS") or CONFIG.gmail_address or "").strip()


def _password() -> str:
    # Google displays app passwords in groups of four; the spaces are not part of it.
    value = os.environ.get("GMAIL_APP_PASSWORD") or CONFIG.gmail_app_password or ""
    return value.replace(" ", "").strip()


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    out: list[str] = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _sender_address(value: str) -> str:
    match = re.search(r"<([^>]+)>", value or "")
    return (match.group(1) if match else (value or "")).strip()


def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=_TIMEOUT_S)
    conn.login(_address(), _password())
    return conn


def _quote(folder: str) -> str:
    return folder if folder.startswith('"') else f'"{folder}"'


def _drafts_folder(conn: imaplib.IMAP4_SSL) -> str:
    """Find the localized Drafts mailbox (usually '[Gmail]/Drafts')."""
    try:
        _typ, lines = conn.list()
        for line in lines or []:
            text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
            if "\\Drafts" in text:
                match = re.search(r'"([^"]+)"\s*$', text)
                if match:
                    return match.group(1)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not list mailboxes: %s", exc)
    return "[Gmail]/Drafts"


def _header_message(uid: bytes, meta: bytes, raw: bytes) -> dict[str, Any]:
    msg = email.message_from_bytes(raw)
    try:
        date = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:  # noqa: BLE001
        date = msg.get("Date") or ""
    flags = meta.decode("utf-8", errors="replace")
    return {
        "uid": uid.decode("ascii", errors="replace"),
        "from": _decode_header(msg.get("From")),
        "from_addr": _sender_address(_decode_header(msg.get("From"))),
        "to": _decode_header(msg.get("To")),
        "subject": _decode_header(msg.get("Subject")) or "(no subject)",
        "date": date,
        "message_id": msg.get("Message-ID") or "",
        "references": msg.get("References") or "",
        "unread": "\\Seen" not in flags,
    }


def _fetch_headers(conn: imaplib.IMAP4_SSL, uids: list[bytes]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    fields = "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID REFERENCES)])"
    for uid in uids:
        try:
            _typ, data = conn.uid("fetch", uid, fields)
            if not data or not isinstance(data[0], tuple):
                continue
            meta, raw = data[0][0], data[0][1]
            if isinstance(raw, bytes) and isinstance(meta, bytes):
                out.append(_header_message(uid, meta, raw))
        except Exception as exc:  # noqa: BLE001
            log.debug("header fetch failed for %s: %s", uid, exc)
    return out


def messages(limit: int = 8, today: bool = True, unread_only: bool = False,
             force: bool = False) -> list[dict[str, Any]]:
    """Newest first. `today` filters by the current local date; cached briefly."""
    global _last_messages, _last_error
    if not configured():
        return []
    cache_key = f"{limit}:{today}:{unread_only}"
    now = time.time()
    with _lock:
        cached = _cache.get("data") or {}
        if (not force and cached.get("key") == cache_key
                and now - float(_cache.get("at") or 0) < _CACHE_TTL_S):
            return list(cached.get("messages") or [])

    conn = None
    try:
        conn = _connect()
        conn.select("INBOX", readonly=True)
        if unread_only:
            criteria = "UNSEEN"
        elif today:
            criteria = "SINCE " + time.strftime("%d-%b-%Y", time.localtime())
        else:
            criteria = "ALL"
        _typ, data = conn.uid("search", None, criteria)
        uids = (data[0].split() if data and data[0] else [])[-limit:]
        found = list(reversed(_fetch_headers(conn, uids)))
        with _lock:
            _cache.update({"at": now, "data": {"key": cache_key, "messages": found}})
            _last_messages = found
        _last_error = ""
        return found
    except Exception as exc:  # noqa: BLE001 - bad password, offline, Gmail change
        log.warning("inbox fetch failed: %s", exc)
        with _lock:
            _last_error = str(exc)
        return []
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


def unread_count() -> int:
    if not configured():
        return 0
    conn = None
    try:
        conn = _connect()
        conn.select("INBOX", readonly=True)
        _typ, data = conn.uid("search", None, "UNSEEN")
        return len(data[0].split()) if data and data[0] else 0
    except Exception as exc:  # noqa: BLE001
        log.debug("unread count failed: %s", exc)
        return 0
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            if "attachment" in str(part.get("Content-Disposition") or ""):
                continue
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if isinstance(payload, bytes) else ""


def read(uid: str) -> dict[str, Any]:
    """Full message body. `uid` comes from messages() or a fragment of sender/subject."""
    if not configured() or not uid:
        return {}
    if not str(uid).isdigit():
        found = match(uid)
        if not found:
            return {}
        uid = str(found["uid"])
    conn = None
    try:
        conn = _connect()
        conn.select("INBOX", readonly=True)
        _typ, data = conn.uid("fetch", str(uid).encode(), "(RFC822)")
        if not data or not isinstance(data[0], tuple):
            return {}
        raw = data[0][1]
        if not isinstance(raw, bytes):
            return {}
        msg = email.message_from_bytes(raw)
        body = re.sub(r"\n{3,}", "\n\n", _extract_body(msg)).strip()
        return {
            "uid": str(uid),
            "from": _decode_header(msg.get("From")),
            "to": _decode_header(msg.get("To")),
            "subject": _decode_header(msg.get("Subject")) or "(no subject)",
            "date": msg.get("Date") or "",
            "body": body[:20000],
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("message read failed: %s", exc)
        return {}
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


def match(fragment: str) -> dict[str, Any] | None:
    """Match a sender/subject fragment against the most recent inbox list."""
    fragment = (fragment or "").strip().lower()
    if not fragment or fragment in {"that", "this", "the email", "the message", "it", "latest"}:
        pool = _last_messages or messages()
        return pool[0] if pool else None
    pool = messages(limit=15, today=False)
    for mail in pool:
        haystack = f"{mail.get('from', '')} {mail.get('from_addr', '')} {mail.get('subject', '')}".lower()
        if fragment in haystack:
            return mail
    return None


def build_draft(original: dict[str, Any], body: str) -> EmailMessage:
    """Create the reply as an RFC 5322 message, threaded to the original."""
    to = original.get("from_addr") or original.get("from") or ""
    subject = original.get("subject") or ""
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    message = EmailMessage()
    message["From"] = _address()
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    if original.get("message_id"):
        message["In-Reply-To"] = original["message_id"]
        refs = (original.get("references") or "").strip()
        message["References"] = (refs + " " + original["message_id"]).strip()
    message.set_content(body)
    return message


def save_draft(original: dict[str, Any], body: str) -> tuple[bool, str]:
    """Append a reply to Gmail's Drafts folder. Never sends."""
    if not configured():
        return False, ("Gmail isn't configured, sir. Add GMAIL_ADDRESS and "
                       "GMAIL_APP_PASSWORD to .env.")
    body = (body or "").strip()
    if not body:
        return False, "What should the reply say, sir?"
    message = build_draft(original, body)
    conn = None
    try:
        conn = _connect()
        folder = _drafts_folder(conn)
        status, _data = conn.append(
            _quote(folder), "\\Draft", imaplib.Time2Internaldate(time.time()),
            message.as_bytes(),
        )
        if status != "OK":
            return False, f"Gmail refused the draft, sir ({status})."
        log.info("draft saved to %s for %r", folder, original.get("subject"))
        return True, "Saved to your Gmail drafts, sir. Nothing has been sent."
    except Exception as exc:  # noqa: BLE001
        log.warning("draft save failed: %s", exc)
        return False, f"I couldn't save the draft, sir: {exc}"
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


def reply_latest(body: str) -> tuple[bool, str]:
    original = _last_messages[0] if _last_messages else (messages() or [None])[0]
    if not original:
        return False, "I don't have an email to reply to, sir."
    return save_draft(original, body)


def by_uid(uid: str) -> dict[str, Any] | None:
    """Find a message by uid in the cached list, then in the last 20."""
    uid = str(uid or "")
    if not uid:
        return None
    for mail in _last_messages:
        if str(mail.get("uid")) == uid:
            return mail
    for mail in messages(limit=20, today=False):
        if str(mail.get("uid")) == uid:
            return mail
    return None


def read_speech(fragment: str = "latest") -> str:
    """Spoken form of one email, trimmed so Jarvis doesn't monologue."""
    if not configured():
        return ("Email isn't set up yet, sir. Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
                "to the .env file.")
    message = read(fragment)
    if not message:
        return f"I couldn't find an email matching {fragment}, sir."
    body = re.sub(r"\s+", " ", message.get("body") or "").strip()
    if len(body) > 900:
        body = body[:900].rsplit(" ", 1)[0] + "…"
    return (f"Email from {_short_sender(message.get('from', ''))} about "
            f"{message.get('subject')}. {body or 'The message is empty.'}")


def reply_speech(fragment: str, body: str) -> str:
    original = match(fragment)
    if not original:
        return f"I couldn't find an email matching {fragment}, sir."
    _ok, message = save_draft(original, body)
    return message


def summary_speech(limit: int = 5) -> str:
    if not configured():
        return ("Email isn't set up yet, sir. Put GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
                "in the .env file and I'll read your inbox.")
    mails = messages(limit=limit, today=True)
    unread = sum(1 for mail in mails if mail.get("unread"))
    if not mails:
        return "No email has arrived today, sir."
    count = len(mails)
    parts = [f"You have {count} email{'s' if count != 1 else ''} today, sir"]
    if unread:
        parts.append(f"{unread} unread")
    opening = ", ".join(parts) + ". "
    details = "; ".join(
        f"from {_short_sender(mail['from'])}: {mail['subject']}" for mail in mails[:3]
    )
    return opening + details + "."


def _short_sender(value: str) -> str:
    name = re.sub(r"<[^>]*>", "", value or "").strip().strip('"')
    return name or _sender_address(value) or "someone"


def summary_state(limit: int = 8) -> dict[str, Any]:
    """Everything the dashboard needs in one call (cached)."""
    if not configured():
        return {"configured": False, "address": _address(),
                "messages": [], "unread": 0, "error": ""}
    mails = messages(limit=limit, today=True)
    return {
        "configured": True,
        "address": _address(),
        "messages": mails,
        "unread": sum(1 for mail in mails if mail.get("unread")),
        "error": (f"Could not reach Gmail: {_last_error}" if _last_error else ""),
    }


def probe() -> tuple[bool, str]:
    """Login once, for --check. Never fetches mail."""
    if not configured():
        return True, "not configured (optional: add GMAIL_ADDRESS + GMAIL_APP_PASSWORD)"
    return test_credentials(_address(), _password())


def test_credentials(address: str, password: str) -> tuple[bool, str]:
    """Try a login without changing anything. Used by the dashboard sign-in form."""
    address = (address or "").strip()
    password = (password or "").replace(" ", "").strip()
    if not address or "@" not in address:
        return False, "Enter a valid Gmail address."
    if not password:
        return False, "Enter the App Password."
    conn = None
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=_TIMEOUT_S)
        conn.login(address, password)
        return True, f"{address} connected."
    except imaplib.IMAP4.error as exc:
        text = str(exc)
        if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text:
            return False, ("Gmail rejected that App Password. Check that 2-Step Verification "
                           "is on and that you used the 16-character App Password.")
        if "Application-specific password required" in text:
            return False, "Google requires an App Password for this account (2-Step Verification)."
        return False, f"Gmail login failed: {text[:110]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not reach Gmail: {exc}"
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
