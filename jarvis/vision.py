"""Vision: looking at the screen and answering questions about it.

Two very different ways to "see", used for different jobs:

* **Accessibility text** (`skills/screen.py`) — exact, instant, offline. Use it to find a
  button or read a dialog's text.
* **A screenshot through the vision model** (this module) — costs a network round trip and
  quota, but can answer "what does this error mean?" or "why does this look wrong?".

Because the free tier gives only 20 requests/day per model, vision is used deliberately:
one capture, one question, short answer.
"""
from __future__ import annotations

import base64
import io
import logging

from .ai import Gemini, Grok, extract_text
from .config import CONFIG

log = logging.getLogger("vision")

MAX_EDGE = 1024
JPEG_QUALITY = 82

PROMPT = (
    "You are Jarvis, looking at the user's computer screen. Answer their question about "
    "the screen in one or two short spoken sentences. Be concrete: name the application, "
    "the error, the text, or the control you can see. Do not use markdown or lists. If the "
    "screen does not show what they asked about, say so plainly."
)


def capture(max_edge: int = MAX_EDGE):
    """Grab the whole screen and shrink it to something quick to upload."""
    from PIL import ImageGrab

    image = ImageGrab.grab()
    width, height = image.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / longest
        image = image.resize((int(width * scale), int(height * scale)))
    return image


def capture_png(max_edge: int = MAX_EDGE) -> bytes:
    """Screenshot as JPEG bytes — 4x smaller than PNG to upload on a slow link."""
    buffer = io.BytesIO()
    capture(max_edge).convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY,
                                         optimize=True)
    return buffer.getvalue()


def save_screenshot(path: str | None = None) -> str:
    """Save a screenshot (default: Desktop) and return the path."""
    from pathlib import Path

    target = Path(path).expanduser() if path else Path.home() / "Desktop" / "jarvis_screenshot.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(capture_png())
    return str(target)


def describe(question: str, png: bytes | None = None) -> str:
    """Answer `question` about the current screen."""
    question = (question or "").strip() or "What is on my screen right now?"
    try:
        image = png or capture_png()
    except Exception as exc:  # noqa: BLE001
        log.warning("screen capture failed: %s", exc)
        return "I couldn't capture the screen, sir."

    prompt = f"{PROMPT}\n\nQuestion: {question}"
    for provider, model in CONFIG.vision_chain:
        try:
            if provider == "grok":
                if not CONFIG.grok_api_key:
                    continue
                text = Grok().describe_image(
                    model, image, prompt, temperature=0.4,
                    max_output_tokens=450, timeout=90,
                )
            elif provider == "gemini":
                if not CONFIG.gemini_api_key:
                    continue
                encoded = base64.b64encode(image).decode("ascii")
                contents = [{
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {"inlineData": {"mimeType": "image/jpeg", "data": encoded}},
                    ],
                }]
                data = Gemini().generate(model, contents, temperature=0.4,
                                         max_output_tokens=450, timeout=90)
                text = extract_text(data).strip()
            else:
                continue
            if text:
                log.info("vision via %s/%s (%d KB screenshot)",
                         provider, model, len(image) // 1024)
                return text
            log.debug("vision %s/%s returned nothing", provider, model)
        except Exception as exc:  # noqa: BLE001 - quota, network, unsupported model
            log.info("vision via %s/%s failed: %s", provider, model, exc)
    return "I'm afraid I couldn't make sense of the screen just now, sir."


def read_error() -> str:
    """Convenience: explain whatever is on screen as though it were an error."""
    return describe("What error or problem is visible on this screen, and what should the user do?")
