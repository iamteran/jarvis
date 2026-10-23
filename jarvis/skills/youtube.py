"""Turning "play X on YouTube" into the actual video, not just a search page.

The web search URL never presses play, so Jarvis used to say "Playing ..." while
only showing results. This asks YouTube's own search endpoint (the lightweight
JSON one the site uses) for the first real video and opens it with autoplay on.
Falls back to the ordinary results page if the network or the endpoint changes.
"""
from __future__ import annotations

import logging
import urllib.parse
import webbrowser

import requests

log = logging.getLogger("skills.youtube")

_SEARCH_URL = "https://www.youtube.com/youtubei/v1/search"
# Public key embedded in youtube.com; not a secret, but may change over time.
_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_CLIENT_VERSION = "2.20240101.00.00"
_TIMEOUT_S = 8
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _first_video_id(node: object) -> str | None:
    """Walk YouTube's response JSON and return the first videoRenderer id."""
    if isinstance(node, dict):
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict):
            video_id = renderer.get("videoId")
            if isinstance(video_id, str) and video_id:
                return video_id
        for value in node.values():
            found = _first_video_id(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _first_video_id(item)
            if found:
                return found
    return None


def find_first(query: str) -> str | None:
    """Return the id of the first search result for `query`, or None."""
    try:
        response = requests.post(
            _SEARCH_URL,
            params={"key": _API_KEY, "prettyPrint": "false"},
            json={
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": _CLIENT_VERSION,
                        "hl": "en",
                        "gl": "US",
                    }
                },
                "query": query,
            },
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
        return _first_video_id(response.json())
    except Exception as exc:  # noqa: BLE001 - network, JSON, or layout change
        log.debug("youtube search failed: %s", exc)
        return None


def play(query: str) -> tuple[bool, str]:
    """Open the first matching video and press play, or fall back to the results."""
    query = (query or "").strip(" .!?")
    if not query:
        return False, "What should I play, sir?"
    video_id = find_first(query)
    if video_id:
        webbrowser.open(f"https://www.youtube.com/watch?v={video_id}&autoplay=1")
        log.info("playing %r on YouTube (video %s)", query, video_id)
        return True, f"Playing {query} on YouTube, sir."
    webbrowser.open(
        "https://www.youtube.com/results?search_query="
        + urllib.parse.quote_plus(query)
    )
    log.info("youtube search for %r fell back to results page", query)
    return False, f"I couldn't pick a video for {query}, sir, so the results are on screen."
