"""The floating orb: Jarvis's visible face.

A frameless, always-on-top, draggable HUD disc drawn on a tkinter canvas. It has no
image assets and no GPU requirement — just arcs, circles and a state machine — and it
costs ~0% CPU while idle because it only redraws what changed at 20 fps.

States: idle, listening, thinking, speaking, followup, muted, error.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import tkinter as tk
from typing import Callable

from ..config import CONFIG, DATA

log = logging.getLogger("ui.orb")

BG = "#010203"          # colour-keyed to transparency; never drawn on purpose
POS_FILE = DATA / "ui.json"

COLORS = {
    "booting":   {"ring": "#6b5a12", "bright": "#c9a227", "core": "#f0d27a", "glow": "#1a1503"},
    "idle":      {"ring": "#0d6a80", "bright": "#1f9ab8", "core": "#37c9ea", "glow": "#06222b"},
    "listening": {"ring": "#00b7e6", "bright": "#3fe0ff", "core": "#b9f4ff", "glow": "#04303c"},
    "thinking":  {"ring": "#a97a09", "bright": "#ffc233", "core": "#ffd97a", "glow": "#2a1f03"},
    "speaking":  {"ring": "#00c2f2", "bright": "#63e6ff", "core": "#d6fbff", "glow": "#043441"},
    "followup":  {"ring": "#0f8fa8", "bright": "#2fc9e8", "core": "#8fe9ff", "glow": "#062a33"},
    "muted":     {"ring": "#3a4048", "bright": "#5b636e", "core": "#78818d", "glow": "#14171b"},
    "error":     {"ring": "#8f2b2b", "bright": "#ff5d5d", "core": "#ff9c9c", "glow": "#2a0d0d"},
}

SPIN = {"booting": 4.0, "idle": 0.35, "listening": 3.2, "thinking": 6.0, "speaking": 2.2,
        "followup": 0.9, "muted": 0.0, "error": 0.0}
PULSE = {"booting": 0.040, "idle": 0.020, "listening": 0.075, "thinking": 0.045,
         "speaking": 0.095, "followup": 0.030, "muted": 0.0, "error": 0.0}


class Orb:
    def __init__(self, on_listen: Callable[[], None] | None = None,
                 on_mute: Callable[[], None] | None = None,
                 on_quit: Callable[[], None] | None = None,
                 size: int | None = None) -> None:
        self.size = int(size or CONFIG.orb_size)
        self.on_listen = on_listen
        self.on_mute = on_mute
        self.on_quit = on_quit
        self.state = "idle"
        self.angle = 0.0
        self.phase = 0.0
        self._main_thread = threading.current_thread().ident
        self._drag_offset: tuple[int, int] | None = None
        self._drag_distance = 0.0

        self.root = tk.Tk()
        self.root.title("Jarvis")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", BG)
        except tk.TclError:  # pragma: no cover - platform without colour keying
            log.warning("transparentcolor unsupported; orb will have a dark square")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        self.canvas = tk.Canvas(self.root, width=self.size, height=self.size,
                                bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack()

        self._build()
        self._place()
        self._bind()
        self.root.after(50, self._tick)

    # ------------------------------------------------------------------ drawing
    def _build(self) -> None:
        s = self.size
        c = s / 2.0
        self.cx, self.cy = c, c
        self.R = s * 0.455
        col = COLORS["idle"]

        self.disc = self.canvas.create_oval(
            c - self.R, c - self.R, c + self.R, c + self.R,
            fill="#05080d", outline=col["ring"], width=1)

        # faint inner glass
        self.glass = self.canvas.create_oval(
            c - self.R * 0.86, c - self.R * 0.86, c + self.R * 0.86, c + self.R * 0.86,
            fill=col["glow"], outline="")

        # static tick ring
        self.ticks = []
        for i in range(60):
            ang = math.radians(i * 6)
            long = i % 5 == 0
            r1 = self.R * (0.80 if long else 0.84)
            r2 = self.R * 0.90
            self.ticks.append(self.canvas.create_line(
                c + r1 * math.cos(ang), c + r1 * math.sin(ang),
                c + r2 * math.cos(ang), c + r2 * math.sin(ang),
                fill=col["ring"], width=1))

        # rotating ring segments: (radius factor, extent, speed, base angle, width)
        self.rings = []
        for r_f, extent, speed, base, width in (
            (0.72, 96, 1.0, 20, 3), (0.72, 42, 1.0, 200, 2),
            (0.58, 54, -1.6, 90, 2), (0.58, 30, -1.6, 250, 2),
        ):
            item = self.canvas.create_arc(
                c - self.R * r_f, c - self.R * r_f, c + self.R * r_f, c + self.R * r_f,
                start=base, extent=extent, style=tk.ARC, outline=col["bright"], width=width)
            self.rings.append({"item": item, "speed": speed, "base": base, "r": r_f})

        # core: layered circles fake a soft glow without alpha
        self.core_outer = self.canvas.create_oval(0, 0, 0, 0, fill=col["glow"], outline="")
        self.core_mid = self.canvas.create_oval(0, 0, 0, 0, fill=col["ring"], outline="")
        self.core_inner = self.canvas.create_oval(0, 0, 0, 0, fill=col["core"], outline="")
        self.core_glint = self.canvas.create_oval(0, 0, 0, 0, fill="#ffffff", outline="", stipple="gray50")

        # expanding ripples (speaking)
        self.ripples = [self.canvas.create_oval(0, 0, 0, 0, outline="", width=2) for _ in range(3)]

        self.label = self.canvas.create_text(
            c, s - 12, text="", fill=col["bright"], font=("Segoe UI", 7, "bold"))

    def _paint(self) -> None:
        col = COLORS.get(self.state, COLORS["idle"])
        c = self.cx
        self.canvas.itemconfig(self.disc, outline=col["ring"])
        self.canvas.itemconfig(self.glass, fill=col["glow"])
        for tick in self.ticks:
            self.canvas.itemconfig(tick, fill=col["ring"])
        for ring in self.rings:
            self.canvas.itemconfig(ring["item"], outline=col["bright"])
        self.canvas.itemconfig(self.core_outer, fill=col["glow"])
        self.canvas.itemconfig(self.core_mid, fill=col["ring"])
        self.canvas.itemconfig(self.core_inner, fill=col["core"])
        self.canvas.itemconfig(self.label, fill=col["bright"], text=self.state.upper())

    # ------------------------------------------------------------------- motion
    def _tick(self) -> None:
        try:
            self._paint()
            spin = SPIN.get(self.state, 0.3)
            pulse = PULSE.get(self.state, 0.02)
            self.angle = (self.angle + spin) % 360.0
            self.phase += 0.22

            for ring in self.rings:
                start = (ring["base"] + self.angle * ring["speed"]) % 360.0
                self.canvas.itemconfig(ring["item"], start=start)

            breath = 1.0 + pulse * math.sin(self.phase)
            r_out = self.R * 0.30 * breath
            r_mid = self.R * 0.21 * breath
            r_in = self.R * 0.135 * breath
            self._circle(self.core_outer, r_out)
            self._circle(self.core_mid, r_mid)
            self._circle(self.core_inner, r_in)
            self._circle(self.core_glint, r_in * 0.42)

            # ripples while speaking
            if self.state == "speaking":
                for i, item in enumerate(self.ripples):
                    t = ((self.phase * 0.35 + i / len(self.ripples)) % 1.0)
                    rr = self.R * (0.34 + 0.52 * t)
                    self._circle(item, rr)
                    self.canvas.itemconfig(item, state="normal",
                                           outline=self._fade(COLORS["speaking"]["bright"], t))
            else:
                for item in self.ripples:
                    self.canvas.itemconfig(item, state="hidden")
        except tk.TclError:  # window closing
            return
        self.root.after(50, self._tick)

    def _circle(self, item: int, r: float) -> None:
        self.canvas.coords(item, self.cx - r, self.cy - r, self.cx + r, self.cy + r)

    @staticmethod
    def _fade(hex_color: str, t: float) -> str:
        """Blend a colour towards the disc background as t goes 0 -> 1."""
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        br, bg_, bb = 5, 8, 13
        return "#%02x%02x%02x" % (
            int(r + (br - r) * t), int(g + (bg_ - g) * t), int(b + (bb - b) * t))

    # -------------------------------------------------------------- interaction
    def _bind(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Double-Button-1>", lambda e: self._fire(self.on_listen))

        self.menu = tk.Menu(self.root, tearoff=0, bg="#0b1118", fg="#d7f4ff",
                            activebackground="#12313d", activeforeground="#ffffff", bd=0)
        self.menu.add_command(label="Listen now   (Ctrl+Space)", command=lambda: self._fire(self.on_listen))
        self.menu.add_command(label="Mute / unmute   (Ctrl+Alt+J)", command=lambda: self._fire(self.on_mute))
        # No "Quit" here on purpose: the orb sits under the cursor all day, and a stray
        # right-click-plus-click used to shut Jarvis down. Quitting lives in the tray menu,
        # where it can only be reached deliberately.
        self.menu.add_command(label="Hide for now", command=self.hide)

    # -------------------------------------------------------------- visibility
    def hide(self) -> None:
        try:
            self.root.withdraw()
            log.info("orb hidden")
        except tk.TclError:
            pass

    def show(self) -> None:
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            log.info("orb shown")
        except tk.TclError:
            pass

    @staticmethod
    def _fire(callback: Callable[[], None] | None) -> None:
        if callback:
            try:
                callback()
            except Exception:  # noqa: BLE001 - UI thread must never die
                log.exception("orb callback failed")

    def _press(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())
        self._drag_distance = 0.0

    def _drag(self, event: tk.Event) -> None:
        if not self._drag_offset:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self._drag_distance += abs(event.x - self._drag_offset[0]) + abs(event.y - self._drag_offset[1])
        self.root.geometry(f"+{x}+{y}")

    def _release(self, event: tk.Event) -> None:
        if self._drag_distance < 6:
            self._fire(self.on_listen)     # a tap, not a drag
        else:
            self._save_position()
        self._drag_offset = None

    def _menu(self, event: tk.Event) -> None:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------------ geometry
    def _place(self) -> None:
        try:
            saved = json.loads(POS_FILE.read_text(encoding="utf-8")).get("orb")
        except Exception:  # noqa: BLE001
            saved = None
        if saved and isinstance(saved, list) and len(saved) == 2:
            x, y = int(saved[0]), int(saved[1])
            # keep it on screen after a resolution change
            x = max(-self.size // 2, min(x, self.root.winfo_screenwidth() - self.size // 2))
            y = max(0, min(y, self.root.winfo_screenheight() - self.size // 3))
        else:
            x = self.root.winfo_screenwidth() - self.size - CONFIG.orb_margin
            y = self.root.winfo_screenheight() - self.size - CONFIG.orb_margin - 48
        self.root.geometry(f"{self.size}x{self.size}+{x}+{y}")

    def _save_position(self) -> None:
        try:
            data = json.loads(POS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        data["orb"] = [self.root.winfo_x(), self.root.winfo_y()]
        POS_FILE.write_text(json.dumps(data), encoding="utf-8")

    # -------------------------------------------------------------------- public
    def set_state(self, state: str) -> None:
        if state not in COLORS:
            state = "idle"
        if threading.current_thread().ident == self._main_thread:
            self.state = state
        else:
            try:
                self.root.after(0, self._set_state_main, state)
            except (tk.TclError, RuntimeError):
                pass

    def _set_state_main(self, state: str) -> None:
        self.state = state

    def close(self) -> None:
        try:
            self.root.after(0, self.root.destroy)
        except (tk.TclError, RuntimeError):
            pass

    def run(self) -> None:
        self.root.mainloop()
