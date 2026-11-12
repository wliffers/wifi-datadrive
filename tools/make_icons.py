"""Render the WiFi DataDrive app icons (PNG) for the web UI.

Usage (works from any current directory):

    python tools/make_icons.py

Writes icon-180.png, icon-192.png and icon-512.png to
firmware/printdrive/main/web/icons/ (resolved relative to the repo root).
Requires Pillow.

The icon matches the inline SVG logo of the UI (viewBox 0 0 32 32): two white
Wi-Fi arcs above a white rounded "drive" with a teal dot, on a full-bleed teal
square. The square has no rounded corners and no transparency, because iOS and
Android round the corners themselves. The 32-unit design is shrunk to 80 % of
the canvas around the centre, so content stays inside the maskable safe zone.
Each icon is drawn at 4x and downsampled with LANCZOS for smooth edges.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "firmware" / "printdrive" / "main" / "web" / "icons"
SIZES = (180, 192, 512)
SUPERSAMPLE = 4

TEAL = (0x09, 0x70, 0x6B)
WHITE = (255, 255, 255)

VIEW = 32.0       # design viewBox size
CONTENT = 1.20    # scale of the 32-unit design box; the drawn content covers the middle half of it
STROKE = 2.2      # Wi-Fi arc stroke width (design units)


def arc_centre(p0, p1, r):
    """Centre of the circle through p0 and p1 with radius r.

    For an arc bulging upwards the centre lies below the chord (y grows downwards).
    """
    (x0, y0), (x1, y1) = p0, p1
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    half = math.hypot(x1 - x0, y1 - y0) / 2
    d = math.sqrt(r * r - half * half)
    ux, uy = (x1 - x0) / (2 * half), (y1 - y0) / (2 * half)
    nx, ny = -uy, ux
    if ny < 0:
        nx, ny = -nx, -ny
    return mx + nx * d, my + ny * d


def draw_arc(draw, to_canvas, p0, p1, r):
    """Round-capped stroke along the upward arc from p0 to p1 with radius r."""
    cx, cy = arc_centre(p0, p1, r)
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    a1 = math.atan2(p1[1] - cy, p1[0] - cx)
    assert a0 < a1, "arc must run through the top (angles -180..0 degrees)"
    half_w = STROKE / 2
    steps = 240
    outer, inner = [], []
    for i in range(steps + 1):
        a = a0 + (a1 - a0) * i / steps
        ca, sa = math.cos(a), math.sin(a)
        outer.append(to_canvas(cx + (r + half_w) * ca, cy + (r + half_w) * sa))
        inner.append(to_canvas(cx + (r - half_w) * ca, cy + (r - half_w) * sa))
    draw.polygon(outer + inner[::-1], fill=WHITE)
    cap = half_w * to_canvas.scale
    for x, y in (p0, p1):
        px, py = to_canvas(x, y)
        draw.ellipse((px - cap, py - cap, px + cap, py + cap), fill=WHITE)


class _Mapper:
    """Maps design coordinates (viewBox 32) to supersampled canvas pixels."""

    def __init__(self, canvas):
        self.scale = canvas * CONTENT / VIEW
        self.offset = canvas * (1 - CONTENT) / 2

    def __call__(self, x, y):
        return (self.offset + x * self.scale, self.offset + y * self.scale)


def render(size):
    canvas = size * SUPERSAMPLE
    img = Image.new("RGB", (canvas, canvas), TEAL)
    draw = ImageDraw.Draw(img)
    to_canvas = _Mapper(canvas)

    # Wi-Fi arcs: chord endpoints and radius from the design spec
    draw_arc(draw, to_canvas, (9.5, 13.0), (22.5, 13.0), 9.2)
    draw_arc(draw, to_canvas, (12.7, 16.2), (19.3, 16.2), 4.7)

    # Drive: white rounded rectangle with a teal dot
    k = to_canvas.scale
    draw.rounded_rectangle(
        (*to_canvas(8, 19.5), *to_canvas(24, 25.5)), radius=2 * k, fill=WHITE
    )
    dx, dy = to_canvas(20.5, 22.5)
    dr = 1.1 * k
    draw.ellipse((dx - dr, dy - dr, dx + dr, dy + dr), fill=TEAL)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT_DIR / f"icon-{size}.png"
        render(size).save(path, optimize=True)
        print(f"{path} {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
