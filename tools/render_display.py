#!/usr/bin/env python3
"""Render the status screens of the LCD (160 x 80) for the documentation.

Uses the real 5x7 font of the firmware (firmware/components/display/font5x7.c), the colours and
layout of firmware/printdrive/main/ui.c and the mascot sprite sheets from
firmware/printdrive/main/anim/. The result is a picture of what the display shows, not a photo.

    python tools/render_display.py docs/images      (needs Pillow)

Writes display-zustaende.png (all states, still) and display-zustaende.gif (animated).
Addresses use the documentation range 192.0.2.x, no real device data.
"""
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
W, H, SCALE = 160, 80, 3


def rgb565(r, g, b):
    """Colour as the panel shows it (RGB565, expanded back to 8 bit)."""
    r5, g6, b5 = r >> 3, g >> 2, b >> 3
    return (r5 << 3 | r5 >> 2, g6 << 2 | g6 >> 4, b5 << 3 | b5 >> 2)


BLACK, WHITE = (0, 0, 0), (255, 255, 255)
GREY, RED, GREEN = rgb565(110, 110, 110), rgb565(220, 30, 30), rgb565(30, 170, 60)
ORANGE, YELLOW, CYAN = rgb565(240, 140, 0), rgb565(240, 220, 0), rgb565(0, 190, 210)

FONT = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})",
        (ROOT / "firmware/components/display/font5x7.c").read_text(encoding="utf-8").split("{", 1)[1])]
CP437 = {"ä": 0x84, "ö": 0x94, "ü": 0x81, "Ä": 0x8E, "Ö": 0x99, "Ü": 0x9A, "ß": 0xE1, "°": 0xF8}


def text(img, x, y, s, fg, bg, scale=1):
    px = img.load()
    for ch in s:
        if x >= W:
            break
        code = ord(ch) if ord(ch) < 0x80 else CP437.get(ch, ord("?"))
        glyph = FONT[code * 5:code * 5 + 5]
        for col in range(6):
            bits = glyph[col] if col < 5 else 0
            for row in range(8):
                c = fg if (bits >> row) & 1 else bg
                for dy in range(scale):
                    for dx in range(scale):
                        X, Y = x + col * scale + dx, y + row * scale + dy
                        if 0 <= X < W and 0 <= Y < H:
                            px[X, Y] = c
        x += 6 * scale


def header_fg(bg):
    r, g, b = bg
    return BLACK if (r * 3 + g * 6 + b) / 10 >= 120 else WHITE


def hexcol(h):
    h = h.lstrip("#")
    return rgb565(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def sheet(name):
    return Image.open(next((ROOT / "firmware/printdrive/main/anim").glob(f"{name}_spritesheet_*.png"))).convert("RGB")


IP, HOST = "192.0.2.152", "datadrive.local"

# (sprite, title, wifi tag, lines [(y, text, colour)], extras, meaning DE)
STATES = [
    ("01_ready", "BEREIT", "WLAN", [(19, IP, YELLOW), (29, HOST, GREY)], "store", "Kein USB-Gerät",
     "Bereit: Dateiverwaltung aktiv, Upload im Browser möglich"),
    ("02_upload", "UPLOAD", "WLAN", [(19, IP, YELLOW), (29, HOST, GREY)], "store", "upload",
     "Upload läuft, mit Fortschritt und Dateiname"),
    ("03_download", "USB", "WLAN", [(19, IP, YELLOW), (29, HOST, GREY)], "store", "Gerät liest...",
     "Am USB-Gerät: es liest oder schreibt gerade"),
    ("04_processing", "BEREIT", "WLAN", [(19, IP, YELLOW), (29, HOST, GREY)], "store", "Kein USB-Gerät",
     "Arbeitet: Kopieren, Formatieren oder Update"),
    ("05_success", "BEREIT", "WLAN", [(19, IP, YELLOW), (29, HOST, GREY)], "store", "Kein USB-Gerät",
     "Entsperrt (einmal nach dem Entsperren)"),
    ("06_connecting", "WLAN", "AP", [(19, "DataDrive-A1B2", CYAN), (29, "PW k7m2x9q4ra", CYAN)], "store",
     "Kein USB-Gerät", "Einrichtungs-WLAN aktiv: Name und Passwort zum Verbinden"),
    ("07_disconnected", "BEREIT", "----", [(19, "WLAN verbinde...", GREY)], "store", "Kein USB-Gerät",
     "Kein WLAN: sucht die gespeicherten Netze"),
    ("08_locked", "GESPERRT", "WLAN", [(19, IP, YELLOW), (29, HOST, GREY),
                                       (40, "Im Browser", YELLOW), (50, "entsperren", YELLOW)], "", "",
     "Gesperrt: im Browser mit dem Laufwerks-Passwort entsperren"),
    ("09_error", "FEHLER", "WLAN", [(19, IP, YELLOW), (29, HOST, GREY),
                                    (40, "Kein Dateisystem!", RED), (50, "Im Web formatieren", GREY)], "",
     "Kein USB-Gerät", "Fehler, z. B. Karte ohne Dateisystem oder Schlüsselbereich beschädigt"),
]


def screen(state, accent, frame):
    sprite, title, tag, lines, extra, host, _ = state
    img = Image.new("RGB", (W, H), BLACK)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, 15], fill=accent)
    text(img, 3, 1, title, header_fg(accent), accent, 2)
    text(img, W - 6 * len(tag) - 3, 4, tag, header_fg(accent), accent, 1)
    for y, s, c in lines:
        text(img, 3, y, s, c, BLACK)
    if extra == "store":
        text(img, 3, 40, "SD 57.9/58.2GB", WHITE, BLACK)
        d.rectangle([3, 50, 3 + 102 - 1, 54], fill=GREY)
        d.rectangle([3, 50, 3 + 1, 54], fill=GREEN)
    if host == "upload":
        text(img, 3, 59, " 42% Benchy.3mf", ORANGE, BLACK)
        d.rectangle([3, 67, 3 + (W - 6) * 42 // 100 - 1, 68], fill=ORANGE)
    elif host:
        text(img, 3, 59, host, WHITE, BLACK)
    if sprite == "08_locked":
        text(img, 3, 70, "verschlüsselt", GREY, BLACK)
    elif sprite != "09_error":
        text(img, 3, 70, "12 Datei(en)", GREY, BLACK)
    img.paste(sheet(sprite).crop((frame * 48, 0, frame * 48 + 48, 40)), (108, 18))
    return img


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "docs/images")
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / "firmware/printdrive/main/anim/manifest.json").read_text(encoding="utf-8"))
    accent = {s["name"]: hexcol(s["accent"]) for s in manifest["states"]}
    try:
        label_font = ImageFont.truetype("segoeui.ttf", 15)
    except OSError:
        label_font = ImageFont.load_default()
    cols, gap, label_h, bezel = 3, 18, 0, 6
    cw, ch = W * SCALE + 2 * bezel, H * SCALE + 2 * bezel
    rows = (len(STATES) + cols - 1) // cols
    frames = []
    for f in range(8):
        canvas = Image.new("RGB", (cols * cw + (cols + 1) * gap, rows * (ch + label_h) + (rows + 1) * gap),
                           (13, 17, 23))
        for i, st in enumerate(STATES):
            x = gap + (i % cols) * (cw + gap)
            y = gap + (i // cols) * (ch + label_h + gap)
            ImageDraw.Draw(canvas).rounded_rectangle([x, y, x + cw - 1, y + ch - 1], 8, fill=(40, 44, 52))
            canvas.paste(screen(st, accent[st[0]], f).resize((W * SCALE, H * SCALE), Image.NEAREST),
                         (x + bezel, y + bezel))
        frames.append(canvas)
    frames[0].save(out / "display-zustaende.png", optimize=True)
    frames[0].save(out / "display-zustaende.gif", save_all=True, append_images=frames[1:], duration=100,
                   loop=0, optimize=True)
    for st in STATES:
        print(f"{st[0]:16} {st[1]:9} {st[-1]}")


if __name__ == "__main__":
    main()
