"""Microphone capture: one continuous 16 kHz stream, a ring buffer, and a block queue.

A single consumer (the app's audio loop) reads blocks. That keeps wake-word detection,
endpointing and barge-in strictly ordered and avoids two threads fighting over the mic.
"""
from __future__ import annotations

import collections
import logging
import queue
import time

import numpy as np
import sounddevice as sd

from ..config import CONFIG
from .noise_filter import make_noise_filter

log = logging.getLogger("audio.capture")


def describe_devices() -> list[str]:
    """Human-readable list of input/output devices, for `--devices` and logs."""
    lines: list[str] = []
    for idx, dev in enumerate(sd.query_devices()):
        kind = []
        if dev["max_input_channels"] > 0:
            kind.append("IN")
        if dev["max_output_channels"] > 0:
            kind.append("OUT")
        lines.append(f"[{idx:>2}] {'/'.join(kind):<7} {dev['name']}  ({dev['default_samplerate']:.0f} Hz)")
    return lines


def describe_input_devices() -> list[str]:
    """Return available microphone devices without listing output-only devices."""
    return [
        f"[{idx:>2}] {dev['name']} ({dev['max_input_channels']} ch, "
        f"{dev['default_samplerate']:.0f} Hz)"
        for idx, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] > 0
    ]


def resolve_device(spec: str | None, kind: str) -> int | None:
    """Resolve a config value (index or name substring) to a sounddevice index."""
    if not spec:
        return None
    spec = str(spec).strip()
    if spec.isdigit():
        return int(spec)
    ch_key = "max_input_channels" if kind == "input" else "max_output_channels"
    for idx, dev in enumerate(sd.query_devices()):
        if spec.lower() in dev["name"].lower() and dev[ch_key] > 0:
            log.info("using %s device [%d] %s", kind, idx, dev["name"])
            return idx
    log.warning("%s device '%s' not found, falling back to system default", kind, spec)
    return None


class AudioHub:
    """Continuous microphone stream with a rolling 30 s history."""

    def __init__(self, rate: int = CONFIG.sample_rate, block_ms: int = CONFIG.block_ms) -> None:
        self.rate = rate
        self.block_ms = block_ms
        self.block_size = int(rate * block_ms / 1000)
        self._ring: collections.deque[np.ndarray] = collections.deque(
            maxlen=int(30_000 / block_ms)
        )
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=600)
        self._stream: sd.InputStream | None = None
        self.device_index: int | None = None
        self.device_name: str = "(default)"
        self.dropped = 0
        self._noise_filter = make_noise_filter()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.device_index = resolve_device(CONFIG.mic_device, "input")
        candidates = [self.device_index]
        if self.device_index is None:
            # try default, then every input device, so a dead Bluetooth headset can't brick Jarvis
            candidates = [None] + [i for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
        last_error: Exception | None = None
        for dev in candidates:
            try:
                self._stream = sd.InputStream(
                    samplerate=self.rate,
                    channels=1,
                    dtype="int16",
                    blocksize=self.block_size,
                    device=dev,
                    callback=self._callback,
                )
                self._stream.start()
                self.device_index = dev
                self.device_name = (
                    sd.query_devices(dev)["name"] if dev is not None else "(system default)"
                )
                log.info("microphone open: %s @ %d Hz, %d ms blocks", self.device_name, self.rate, self.block_ms)
                return
            except Exception as exc:  # noqa: BLE001 - we want to try the next device
                last_error = exc
                log.debug("mic %s failed: %s", dev, exc)
        raise RuntimeError(f"could not open any microphone: {last_error}")

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None

    def restart(self) -> None:
        """Recover from a dead stream (unplugged headset, device sleep, driver reset)."""
        log.warning("restarting microphone stream")
        self.stop()
        time.sleep(0.4)
        try:
            self.start()
            self._noise_filter.reset()
        except Exception as exc:  # noqa: BLE001 - keep trying on the next watchdog tick
            log.error("microphone restart failed: %s", exc)

    # ------------------------------------------------------------------ callback
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            # PortAudio raises overflow every callback under load; log at most once a second
            now = time.monotonic()
            if now - getattr(self, "_last_status_log", 0.0) > 1.0:
                self._last_status_log = now
                log.debug("audio status: %s", status)
        block = np.array(indata[:, 0], dtype=np.int16, copy=True)
        self._ring.append(block)
        block = self._noise_filter.process(block)
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            self.dropped += 1
            try:  # drop the oldest, keep the live audio
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(block)

    # -------------------------------------------------------------------- reads
    def get_block(self, timeout: float = 0.25) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self) -> None:
        """Throw away buffered audio (used right before listening begins)."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def recent(self, ms: int) -> list[np.ndarray]:
        """Last `ms` milliseconds of audio as a list of blocks (oldest first)."""
        need = max(1, int(ms / self.block_ms))
        return list(self._ring)[-need:]

    def recent_samples(self, ms: int) -> np.ndarray:
        blocks = self.recent(ms)
        if not blocks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(blocks)

    @property
    def running(self) -> bool:
        return self._stream is not None and self._stream.active
