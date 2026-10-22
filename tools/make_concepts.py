"""Procedural Jarvis visual identity: HUD wallpaper, assistant orb, tray icon.

No external assets, no paid APIs. Run:
    python tools/make_concepts.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets" / "concepts"
OUT.mkdir(parents=True, exist_ok=True)

CYAN = (0, 209, 255)
TEAL = (0, 120, 160)
WHITE = (210, 245, 255)


def float_canvas(w: int, h: int, top: tuple[int, int, int], bottom: tuple[int, int, int]):
    """Vertical gradient base as float32 RGB in 0..255."""
    yy = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    top_a = np.array(top, dtype=np.float32)[None, None, :]
    bot_a = np.array(bottom, dtype=np.float32)[None, None, :]
    base = top_a + (bot_a - top_a) * yy
    return np.repeat(base, w, axis=1)


def add_glow(canvas: np.ndarray, cx: float, cy: float, radius: float,
             color: tuple[int, int, int], strength: float, power: float = 2.2) -> None:
    """Additive radial glow, in place."""
    h, w = canvas.shape[:2]
    ys = np.arange(h, dtype=np.float32)[:, None] - cy
    xs = np.arange(w, dtype=np.float32)[None, :] - cx
    dist = np.sqrt(xs * xs + ys * ys)
    falloff = np.exp(-np.power(dist / max(radius, 1.0), power)) * strength
    canvas += falloff[:, :, None] * np.array(color, dtype=np.float32)[None, None, :]


def add_ring_glow(canvas: np.ndarray, cx: float, cy: float, radius: float,
                  width: float, color: tuple[int, int, int], strength: float) -> None:
    """Glow concentrated on a circle of given radius."""
    h, w = canvas.shape[:2]
    ys = np.arange(h, dtype=np.float32)[:, None] - cy
    xs = np.arange(w, dtype=np.float32)[None, :] - cx
    dist = np.sqrt(xs * xs + ys * ys)
    band = np.exp(-np.power(np.abs(dist - radius) / max(width, 1.0), 2.0)) * strength
    canvas += band[:, :, None] * np.array(color, dtype=np.float32)[None, None, :]


def vignette(canvas: np.ndarray, strength: float = 0.75, power: float = 2.4) -> None:
    h, w = canvas.shape[:2]
    ys = (np.arange(h, dtype=np.float32)[:, None] / h) - 0.5
    xs = (np.arange(w, dtype=np.float32)[None, :] / w) - 0.5
    d = np.sqrt(xs * xs + ys * ys) / 0.7071
    v = 1.0 - strength * np.power(np.clip(d, 0, 1), power)
    canvas *= v[:, :, None]


def hex_grid_overlay(w: int, h: int, spacing: int, color: tuple[int, int, int, int]):
    """Faint hexagonal technical grid."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = spacing / 2.0
    dx = spacing * 0.8660254
    dy = spacing * 1.5
    cols = int(w / dx) + 2
    rows = int(h / dy) + 2
    for row in range(rows):
        for col in range(cols):
            cx = col * dx + (dx / 2 if row % 2 else 0) - dx
            cy = row * dy - spacing
            pts = [(cx + r * math.cos(math.radians(60 * i)),
                    cy + r * math.sin(math.radians(60 * i))) for i in range(6)]
            d.polygon(pts, outline=color)
    return layer


def scanlines(img: Image.Image, gap: int = 3, darken: float = 0.93) -> Image.Image:
    arr = np.asarray(img).astype(np.float32)
    arr[::gap, :, :3] *= darken
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def particles(w: int, h: int, count: int, seed: int = 7) -> Image.Image:
    rng = random.Random(seed)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        a = rng.randint(12, 90)
        r = rng.choice([0.6, 0.6, 0.9, 1.4])
        d.ellipse([x - r, y - r, x + r, y + r], fill=(150, 230, 255, a))
    return layer.filter(ImageFilter.GaussianBlur(0.6))


def bloom(img: Image.Image, threshold: int = 120, radius: int = 18, strength: float = 0.85) -> Image.Image:
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    lum = arr.max(axis=2)
    mask = np.clip((lum - threshold) / (255 - threshold), 0, 1)[:, :, None]
    bright = Image.fromarray(np.clip(arr * mask, 0, 255).astype(np.uint8), "RGB")
    blurred = bright.filter(ImageFilter.GaussianBlur(radius))
    out = img.convert("RGB").copy()
    out = Image.fromarray(np.clip(np.asarray(out).astype(np.float32)
                                  + np.asarray(blurred).astype(np.float32) * strength, 0, 255).astype(np.uint8), "RGB")
    return out.convert("RGBA")


def draw_hud(w: int, h: int) -> Image.Image:
    canvas = float_canvas(w, h, (7, 10, 16), (1, 2, 4))
    cx, cy = w * 0.5, h * 0.48

    # ambient depth
    add_glow(canvas, cx, cy, w * 0.52, (6, 26, 38), 1.0, 2.0)
    add_glow(canvas, cx, cy, 520, (0, 70, 96), 0.55, 2.6)
    add_glow(canvas, cx, cy, 210, (0, 130, 175), 0.5, 2.4)
    add_glow(canvas, cx, cy, 90, (120, 235, 255), 0.85, 2.0)
    for r, s in ((470, 0.5), (330, 0.55), (196, 0.75), (128, 0.9)):
        add_ring_glow(canvas, cx, cy, r, 6 if r > 300 else 4, CYAN, s)
    vignette(canvas, 0.8, 2.2)

    base = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

    base = Image.alpha_composite(base, hex_grid_overlay(w, h, 58, (0, 190, 255, 12)))

    hud = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(hud)

    # concentric rings
    for r, a, width in ((486, 40, 1), (470, 90, 2), (392, 34, 1), (330, 80, 2),
                        (255, 30, 1), (196, 150, 3), (128, 170, 2)):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(0, 209, 255, a), width=width)

    # dashed orbital arcs
    for r, dash, gap, a in ((470, 3.0, 3.2, 120), (392, 1.4, 2.2, 90), (330, 5.0, 2.4, 110)):
        ang = 0.0
        while ang < 360:
            d.arc([cx - r, cy - r, cx + r, cy + r], ang, ang + dash, fill=(0, 209, 255, a), width=2)
            ang += dash + gap

    # tick marks
    for i in range(120):
        ang = math.radians(i * 3)
        long = (i % 10 == 0)
        r1 = 500 if long else 494
        r2 = 516 if long else 504
        x1, y1 = cx + r1 * math.cos(ang), cy + r1 * math.sin(ang)
        x2, y2 = cx + r2 * math.cos(ang), cy + r2 * math.sin(ang)
        d.line([x1, y1, x2, y2], fill=(0, 220, 255, 150 if long else 60), width=2 if long else 1)

    # arc reactor core
    d.ellipse([cx - 118, cy - 118, cx + 118, cy + 118], outline=(140, 240, 255, 200), width=3)
    d.ellipse([cx - 96, cy - 96, cx + 96, cy + 96], outline=(0, 209, 255, 120), width=2)
    for i in range(6):
        a0 = i * 60 + 15
        d.arc([cx - 74, cy - 74, cx + 74, cy + 74], a0, a0 + 34, fill=(0, 230, 255, 220), width=5)
    # inner triangular reactor
    tri = [(cx + 44 * math.cos(math.radians(-90 + i * 120)),
            cy + 44 * math.sin(math.radians(-90 + i * 120))) for i in range(3)]
    d.polygon(tri, outline=(190, 250, 255, 235), width=3)
    d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=(230, 252, 255, 255))

    # corner brackets
    m, L = 54, 96
    for (x, y, sx, sy) in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
        d.line([x, y, x + sx * L, y], fill=(0, 209, 255, 110), width=2)
        d.line([x, y, x, y + sy * L], fill=(0, 209, 255, 110), width=2)

    # side data columns
    for i in range(11):
        y = 300 + i * 34
        ln = random.Random(i * 13).randint(20, 90)
        d.line([120, y, 120 + ln, y], fill=(0, 209, 255, 45), width=1)
        d.line([w - 120 - ln, y, w - 120, y], fill=(0, 209, 255, 45), width=1)

    base = Image.alpha_composite(base, hud)
    base = Image.alpha_composite(base, particles(w, h, 420))
    base = bloom(base, threshold=110, radius=22, strength=0.55)
    base = scanlines(base, 3, 0.94)
    return base.convert("RGB")


def draw_orb(size: int = 1024) -> Image.Image:
    ss = size * 2  # supersample
    canvas = np.zeros((ss, ss, 3), dtype=np.float32)
    cx = cy = ss / 2.0
    add_glow(canvas, cx, cy, ss * 0.46, (0, 55, 80), 0.9, 2.1)
    add_glow(canvas, cx, cy, ss * 0.20, (0, 150, 200), 0.9, 2.0)
    add_glow(canvas, cx, cy, ss * 0.085, (170, 245, 255), 1.0, 2.0)

    img = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    d = ImageDraw.Draw(img)
    R = ss * 0.30

    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(0, 215, 255, 210), width=max(2, ss // 220))
    d.ellipse([cx - R * 0.82, cy - R * 0.82, cx + R * 0.82, cy + R * 0.82],
              outline=(0, 190, 255, 120), width=max(1, ss // 360))
    for i in range(5):
        a0 = i * 72 + 12
        d.arc([cx - R * 0.66, cy - R * 0.66, cx + R * 0.66, cy + R * 0.66], a0, a0 + 40,
              fill=(120, 240, 255, 235), width=max(3, ss // 150))
    for i in range(72):
        ang = math.radians(i * 5)
        r1, r2 = R * 1.06, R * (1.14 if i % 6 == 0 else 1.10)
        d.line([cx + r1 * math.cos(ang), cy + r1 * math.sin(ang),
                cx + r2 * math.cos(ang), cy + r2 * math.sin(ang)],
               fill=(0, 220, 255, 130 if i % 6 == 0 else 55), width=max(1, ss // 400))
    d.ellipse([cx - R * 0.20, cy - R * 0.20, cx + R * 0.20, cy + R * 0.20], fill=(235, 253, 255, 255))

    # Straight-alpha from a clean intensity field: crisp cyan rings, no milky fog.
    arr = np.asarray(img).astype(np.float32)
    lum = arr[:, :, :3].max(axis=2)
    lum = lum / max(float(lum.max()), 1.0)
    alpha = np.clip((lum - 0.015) / 0.985, 0.0, 1.0) ** 1.2
    cyan = np.array([0, 209, 255], dtype=np.float32)
    white = np.array([225, 250, 255], dtype=np.float32)
    t = np.clip((alpha - 0.5) / 0.5, 0.0, 1.0)[:, :, None]
    arr[:, :, :3] = cyan[None, None, :] * (1.0 - t) + white[None, None, :] * t
    # fade the halo to zero before the canvas edge so no square shows on desktop
    ys = np.arange(ss, dtype=np.float32)[:, None] - ss / 2.0
    xs = np.arange(ss, dtype=np.float32)[None, :] - ss / 2.0
    dist = np.sqrt(xs * xs + ys * ys)
    edge = np.clip((ss * 0.47 - dist) / (ss * 0.05), 0.0, 1.0)
    arr[:, :, 3] = alpha * edge * 255
    img = Image.fromarray(arr.astype(np.uint8), "RGBA")
    img = img.filter(ImageFilter.GaussianBlur(0.5))
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    random.seed(11)
    hud = draw_hud(1920, 1080)
    hud.save(OUT / "bg_hud_1920x1080.png", optimize=True)
    print(f"hud  -> {OUT / 'bg_hud_1920x1080.png'}")

    orb = draw_orb(1024)
    orb.save(OUT / "orb_1024.png", optimize=True)
    orb.resize((256, 256), Image.LANCZOS).save(OUT / "orb_256.png", optimize=True)
    orb.resize((64, 64), Image.LANCZOS).save(OUT / "tray_64.png", optimize=True)
    print(f"orb  -> {OUT / 'orb_1024.png'} (+256, 64)")


if __name__ == "__main__":
    main()
