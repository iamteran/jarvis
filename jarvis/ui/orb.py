"""The floating orb: Jarvis's visible face.

A frameless, always-on-top, draggable HUD disc drawn on a tkinter canvas.
Premium redesign: multi-layer glow, rotating dual-ring system, animated arc segments,
particle field, waveform bars (speaking), and a clean label with a digital font feel.
Costs ~0% CPU while idle \u2014 only redraws at 33 fps what actually changed.

States: idle, listening, thinking, speaking, followup, muted, error, booting.
"""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import tkinter as tk
from typing import Callable

from ..config import CONFIG, DATA

log = logging.getLogger("ui.orb")

BG = "#010203"          # colour-keyed to transparency; never drawn on purpose
POS_FILE = DATA / "ui.json"
FPS = 33                # frames per second
_MS = int(1000 / FPS)

# ------------------------------------------------------------------ colour palette
COLORS = {
    "booting":   {"ring": "#4a3a0a", "bright": "#d4a820", "core": "#ffe090",
                  "glow": "#1a1200", "glow2": "#2d1f00", "accent": "#ffcc44"},
    "idle":      {"ring": "#083d4f", "bright": "#19a8cc", "core": "#4de0ff",
                  "glow": "#041520", "glow2": "#07253a", "accent": "#00d4ff"},
    "listening": {"ring": "#006a8a", "bright": "#00d8ff", "core": "#c8f8ff",
                  "glow": "#002a38", "glow2": "#004055", "accent": "#ffffff"},
    "transcribing": {"ring": "#164b70", "bright": "#4db8ff", "core": "#d8f1ff",
                  "glow": "#061626", "glow2": "#0b2942", "accent": "#8ed8ff"},
    "thinking":  {"ring": "#5a3d00", "bright": "#e8a800", "core": "#ffe066",
                  "glow": "#1a1000", "glow2": "#2a1c00", "accent": "#ffcc00"},
    "speaking":  {"ring": "#004466", "bright": "#00ccff", "core": "#e0faff",
                  "glow": "#001822", "glow2": "#002e44", "accent": "#44eeff"},
    "followup":  {"ring": "#054555", "bright": "#1ab8d4", "core": "#80e8ff",
                  "glow": "#021520", "glow2": "#042a34", "accent": "#20d0ee"},
    "muted":     {"ring": "#1e2228", "bright": "#445055", "core": "#606870",
                  "glow": "#0a0c0e", "glow2": "#111518", "accent": "#506060"},
    "error":     {"ring": "#5a0a0a", "bright": "#ff3333", "core": "#ffaaaa",
                  "glow": "#1a0000", "glow2": "#2d0000", "accent": "#ff5555"},
}

# Spin speed (deg/frame) per state
SPIN = {"booting": 2.8, "idle": 0.25, "listening": 2.4, "transcribing": 3.2, "thinking": 4.5, "speaking": 1.8,
        "followup": 0.7, "muted": 0.0, "error": 0.0}
# Core breathe amplitude
PULSE = {"booting": 0.038, "idle": 0.018, "listening": 0.072, "transcribing": 0.055, "thinking": 0.042,
         "speaking": 0.090, "followup": 0.028, "muted": 0.0, "error": 0.012}

# Ring layout: (radius_factor, arc_extent, spin_multiplier, base_angle, stroke_width)
RING_DEFS = [
    (0.78, 110, 1.0,  15, 2),    # outer slow  segment 1
    (0.78,  44, 1.0, 180, 1),    # outer slow  segment 2
    (0.78,  22, 1.0, 310, 1),    # outer slow  segment 3
    (0.62,  70, -1.7, 60, 2),    # mid   fast  segment 1
    (0.62,  32, -1.7, 200, 1),   # mid   fast  segment 2
    (0.48,  90,  2.3, 130, 2),   # inner fast+ segment 1
    (0.48,  18,  2.3, 290, 1),   # inner fast+ segment 2
]

_STATE_LABELS = {
    "booting": "initialising",
    "idle": "standing by",
    "listening": "listening",
    "transcribing": "decoding voice",
    "thinking": "thinking",
    "speaking": "speaking",
    "followup": "follow-up",
    "muted": "muted",
    "error": "error",
}


class _Particle:
    """A floating dot in the background field."""
    __slots__ = ("x", "y", "r", "speed", "angle", "life", "max_life")

    def __init__(self, cx: float, cy: float, R: float) -> None:
        ang = random.uniform(0, math.tau)
        dist = random.uniform(R * 0.15, R * 0.75)
        self.x = cx + dist * math.cos(ang)
        self.y = cy + dist * math.sin(ang)
        self.r = random.uniform(0.8, 2.2)
        self.speed = random.uniform(0.18, 0.55)
        self.angle = random.uniform(0, math.tau)
        self.max_life = random.randint(60, 160)
        self.life = random.randint(0, self.max_life)

    def step(self) -> None:
        self.life += 1
        self.x += self.speed * math.cos(self.angle)
        self.y += self.speed * math.sin(self.angle)
        self.angle += random.uniform(-0.05, 0.05)

    def alpha(self) -> float:
        t = self.life / self.max_life
        return max(0.0, 1.0 - abs(2 * t - 1))   # fade in, then out


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
        self._prev_state = ""
        self.angle = 0.0
        self.phase = 0.0
        self._main_thread = threading.current_thread().ident
        self._drag_offset: tuple[int, int] | None = None
        self._drag_distance = 0.0
        # State transition smoothing
        self._col_current: dict = dict(COLORS["idle"])
        self._col_target: dict = dict(COLORS["idle"])
        self._lerp_t = 1.0  # 1 = fully transitioned

        self.root = tk.Tk()
        self.root.title("Jarvis")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", BG)
        except tk.TclError:
            log.warning("transparentcolor unsupported; orb will have a dark square")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        self.canvas = tk.Canvas(self.root, width=self.size, height=self.size,
                                bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack()

        self._build()
        self._place()
        self._bind()
        self.root.after(_MS, self._tick)

    # ------------------------------------------------------------------ drawing
    def _build(self) -> None:
        s = self.size
        c = s / 2.0
        self.cx, self.cy = c, c
        self.R = s * 0.46
        col = COLORS["idle"]

        # ---- background halo layers (outermost to innermost) ----
        self.halo3 = self.canvas.create_oval(
            c - self.R * 1.08, c - self.R * 1.08, c + self.R * 1.08, c + self.R * 1.08,
            fill=col["glow"], outline="")
        self.halo2 = self.canvas.create_oval(
            c - self.R * 0.98, c - self.R * 0.98, c + self.R * 0.98, c + self.R * 0.98,
            fill=col["glow2"], outline="")

        # main disc
        self.disc = self.canvas.create_oval(
            c - self.R, c - self.R, c + self.R, c + self.R,
            fill="#060a10", outline=col["ring"], width=2)

        # glass highlight (inside disc, subtle gradient illusion)
        self.glass = self.canvas.create_oval(
            c - self.R * 0.88, c - self.R * 0.88, c + self.R * 0.88, c + self.R * 0.88,
            fill=col["glow2"], outline="")

        # ---- tick / graduation marks ----
        self.ticks = []
        for i in range(72):
            ang = math.radians(i * 5)
            major = i % 9 == 0
            medium = i % 3 == 0
            r1 = self.R * (0.80 if major else (0.84 if medium else 0.87))
            r2 = self.R * 0.91
            w = 2 if major else 1
            self.ticks.append(self.canvas.create_line(
                c + r1 * math.cos(ang), c + r1 * math.sin(ang),
                c + r2 * math.cos(ang), c + r2 * math.sin(ang),
                fill=col["ring"], width=w))

        # ---- rotating ring arc segments ----
        self.rings = []
        for r_f, extent, speed, base, width in RING_DEFS:
            item = self.canvas.create_arc(
                c - self.R * r_f, c - self.R * r_f, c + self.R * r_f, c + self.R * r_f,
                start=base, extent=extent, style=tk.ARC, outline=col["bright"], width=width)
            self.rings.append({"item": item, "speed": speed, "base": float(base), "r": r_f})

        # ---- particle field ----
        self._particles: list[_Particle] = [
            _Particle(c, c, self.R) for _ in range(22)
        ]
        self._particle_items = [
            self.canvas.create_oval(0, 0, 0, 0, fill=col["accent"], outline="")
            for _ in self._particles
        ]

        # ---- speaking waveform bars ----
        self._bar_count = 9
        self._bars = []
        bar_y = c
        bar_w = max(2, int(self.R * 0.06))
        spacing = self.R * 0.13
        for i in range(self._bar_count):
            offset = (i - self._bar_count // 2) * spacing
            bx = c + offset
            item = self.canvas.create_rectangle(
                bx - bar_w / 2, bar_y, bx + bar_w / 2, bar_y,
                fill=col["bright"], outline="", state="hidden")
            self._bars.append({"item": item, "x": bx, "phase": i * 0.65})

        # ---- core glow (layered circles = no alpha needed) ----
        self.core_glow4 = self.canvas.create_oval(0, 0, 0, 0, fill=col["glow"],  outline="")
        self.core_glow3 = self.canvas.create_oval(0, 0, 0, 0, fill=col["glow2"], outline="")
        self.core_outer = self.canvas.create_oval(0, 0, 0, 0, fill=col["ring"],  outline="")
        self.core_mid   = self.canvas.create_oval(0, 0, 0, 0, fill=col["bright"], outline="")
        self.core_inner = self.canvas.create_oval(0, 0, 0, 0, fill=col["core"],  outline="")
        self.core_glint = self.canvas.create_oval(0, 0, 0, 0, fill="#ffffff",    outline="",
                                                  stipple="gray50")

        # ---- expanding ripples (speaking / listening) ----
        self.ripples = [self.canvas.create_oval(0, 0, 0, 0, outline="", width=2)
                        for _ in range(4)]

        # ---- outer pulse ring (listening only) ----
        self.pulse_ring = self.canvas.create_oval(
            c - self.R, c - self.R, c + self.R, c + self.R,
            outline="", width=2, state="hidden")

        # ---- state label ----
        self.label = self.canvas.create_text(
            c, s - 14, text="", fill=col["bright"],
            font=("Consolas", 7, "bold"))

    # ---------------------------------------------------------------- colour lerp
    def _lerp_col(self) -> dict:
        """Return an interpolated colour dict between _col_current and _col_target."""
        t = min(1.0, self._lerp_t)
        if t >= 1.0:
            return self._col_target
        result = {}
        for key in self._col_target:
            a = self._col_current[key]
            b = self._col_target[key]
            try:
                ra, ga, ba_ = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
                rb, gb, bb_ = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
                result[key] = "#%02x%02x%02x" % (
                    int(ra + (rb - ra) * t),
                    int(ga + (gb - ga) * t),
                    int(ba_ + (bb_ - ba_) * t),
                )
            except Exception:  # noqa: BLE001
                result[key] = b
        return result

    def _paint(self, col: dict) -> None:
        label_text = _STATE_LABELS.get(self.state, self.state)
        self.canvas.itemconfig(self.halo3,   fill=col["glow"])
        self.canvas.itemconfig(self.halo2,   fill=col["glow2"])
        self.canvas.itemconfig(self.disc,    outline=col["ring"])
        self.canvas.itemconfig(self.glass,   fill=col["glow2"])
        for tick in self.ticks:
            self.canvas.itemconfig(tick, fill=col["ring"])
        for ring in self.rings:
            self.canvas.itemconfig(ring["item"], outline=col["bright"])
        for item in self._particle_items:
            self.canvas.itemconfig(item, fill=col["accent"])
        for bar in self._bars:
            self.canvas.itemconfig(bar["item"], fill=col["bright"])
        self.canvas.itemconfig(self.core_glow4, fill=col["glow"])
        self.canvas.itemconfig(self.core_glow3, fill=col["glow2"])
        self.canvas.itemconfig(self.core_outer, fill=col["ring"])
        self.canvas.itemconfig(self.core_mid,   fill=col["bright"])
        self.canvas.itemconfig(self.core_inner, fill=col["core"])
        self.canvas.itemconfig(self.label, fill=col["accent"], text=label_text)

    # ------------------------------------------------------------------- motion
    def _tick(self) -> None:
        try:
            # Advance lerp
            if self._lerp_t < 1.0:
                self._lerp_t += 0.07
            col = self._lerp_col()

            self._paint(col)
            spin = SPIN.get(self.state, 0.3)
            pulse = PULSE.get(self.state, 0.02)
            self.angle = (self.angle + spin) % 360.0
            self.phase += 0.18

            # ---- ring rotation ----
            for ring in self.rings:
                start = (ring["base"] + self.angle * ring["speed"]) % 360.0
                self.canvas.itemconfig(ring["item"], start=start)

            # ---- core breathe ----
            breath = 1.0 + pulse * math.sin(self.phase)
            self._circle(self.core_glow4, self.R * 0.44 * breath)
            self._circle(self.core_glow3, self.R * 0.34 * breath)
            self._circle(self.core_outer, self.R * 0.26 * breath)
            self._circle(self.core_mid,   self.R * 0.185 * breath)
            self._circle(self.core_inner, self.R * 0.12 * breath)
            self._circle(self.core_glint, self.R * 0.05)

            # ---- particles ----
            c = self.cx
            cy = self.cy
            for idx, p in enumerate(self._particles):
                p.step()
                # respawn if dead or drifted outside disc
                if p.life >= p.max_life or math.hypot(p.x - c, p.y - cy) > self.R * 0.88:
                    p.__init__(c, cy, self.R)  # type: ignore[misc]
                alpha = p.alpha()
                pr = p.r * alpha
                if pr < 0.5:
                    self.canvas.itemconfig(self._particle_items[idx], state="hidden")
                else:
                    self.canvas.coords(self._particle_items[idx],
                                       p.x - pr, p.y - pr, p.x + pr, p.y + pr)
                    self.canvas.itemconfig(self._particle_items[idx], state="normal")

            # ---- speaking: waveform bars ----
            if self.state == "speaking":
                for bar in self._bars:
                    h = self.R * (0.08 + 0.26 * abs(math.sin(self.phase * 1.3 + bar["phase"])))
                    bx = bar["x"]
                    bar_w = max(2, int(self.R * 0.06))
                    self.canvas.coords(bar["item"],
                                       bx - bar_w / 2, cy - h, bx + bar_w / 2, cy + h)
                    self.canvas.itemconfig(bar["item"], state="normal")
                for item in self.ripples:
                    self.canvas.itemconfig(item, state="hidden")
                self.canvas.itemconfig(self.pulse_ring, state="hidden")
            else:
                for bar in self._bars:
                    self.canvas.itemconfig(bar["item"], state="hidden")

            # ---- listening: radial ripples ----
            if self.state in ("listening", "followup"):
                for i, item in enumerate(self.ripples):
                    t = ((self.phase * 0.30 + i / len(self.ripples)) % 1.0)
                    rr = self.R * (0.30 + 0.65 * t)
                    self._circle(item, rr)
                    self.canvas.itemconfig(item, state="normal",
                                           outline=self._fade(col["bright"], t))
                # outer pulse ring
                pr_size = self.R * (1.0 + 0.12 * abs(math.sin(self.phase * 0.5)))
                self._circle(self.pulse_ring, pr_size)
                self.canvas.itemconfig(self.pulse_ring, state="normal",
                                       outline=self._fade(col["accent"], 0.3))
            elif self.state != "speaking":
                for item in self.ripples:
                    self.canvas.itemconfig(item, state="hidden")
                self.canvas.itemconfig(self.pulse_ring, state="hidden")

            # ---- error: slow red throb ----
            if self.state == "error":
                flash = abs(math.sin(self.phase * 0.4))
                self.canvas.itemconfig(self.disc, outline=self._fade(col["bright"], 1.0 - flash))

        except tk.TclError:
            return
        self.root.after(_MS, self._tick)

    def _circle(self, item: int, r: float) -> None:
        self.canvas.coords(item, self.cx - r, self.cy - r, self.cx + r, self.cy + r)

    @staticmethod
    def _fade(hex_color: str, t: float) -> str:
        """Blend a colour towards the disc background (dark) as t goes 0 -> 1."""
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
        except Exception:  # noqa: BLE001
            return hex_color
        br, bg_, bb = 6, 10, 16
        return "#%02x%02x%02x" % (
            int(r + (br - r) * t), int(g + (bg_ - g) * t), int(b + (bb - b) * t))

    # -------------------------------------------------------------- interaction
    def _bind(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Double-Button-1>", lambda e: self._fire(self.on_listen))

        self.menu = tk.Menu(self.root, tearoff=0, bg="#06101a", fg="#8ae8ff",
                            activebackground="#0e2838", activeforeground="#ffffff",
                            relief="flat", bd=0, font=("Consolas", 9))
        self.menu.add_command(label="  \u25b6  Listen now   (Ctrl+Space)",
                              command=lambda: self._fire(self.on_listen))
        self.menu.add_command(label="  \u25a0  Mute / unmute   (Ctrl+Alt+J)",
                              command=lambda: self._fire(self.on_mute))
        self.menu.add_separator()
        self.menu.add_command(label="  \u25cd  Hide orb", command=self.hide)

    # -------------------------------------------------------------- visibility
    def hide(self) -> None:
        try:
            self.root.withdraw()
        except tk.TclError:
            pass

    def show(self) -> None:
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass

    @staticmethod
    def _fire(callback: Callable[[], None] | None) -> None:
        if callback:
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("orb callback failed")

    def _press(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())
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
            self._fire(self.on_listen)
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
            self._apply_state(state)
        else:
            try:
                self.root.after(0, self._apply_state, state)
            except (tk.TclError, RuntimeError):
                pass

    def _apply_state(self, state: str) -> None:
        if state != self.state:
            self._col_current = self._lerp_col()
            self._col_target = dict(COLORS.get(state, COLORS["idle"]))
            self._lerp_t = 0.0
        self.state = state

    def close(self) -> None:
        try:
            self.root.after(0, self.root.destroy)
        except (tk.TclError, RuntimeError):
            pass

    def run(self) -> None:
        self.root.mainloop()
