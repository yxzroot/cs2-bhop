"""Create the low-contrast Hello Kitty wallpaper used by the native Tk UI."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


WIDTH, HEIGHT = 1920, 1080
PIXELS = bytearray(WIDTH * HEIGHT * 3)


def blend(x, y, color, opacity=1.0):
    if not (0 <= x < WIDTH and 0 <= y < HEIGHT):
        return
    index = (y * WIDTH + x) * 3
    alpha = max(0.0, min(1.0, opacity))
    for channel, value in enumerate(color):
        PIXELS[index + channel] = int(PIXELS[index + channel] * (1 - alpha) + value * alpha)


def ellipse(cx, cy, radius_x, radius_y, color, opacity=1.0):
    left, right = max(0, int(cx - radius_x)), min(WIDTH, int(cx + radius_x + 1))
    top, bottom = max(0, int(cy - radius_y)), min(HEIGHT, int(cy + radius_y + 1))
    for y in range(top, bottom):
        dy = (y - cy) / radius_y
        for x in range(left, right):
            dx = (x - cx) / radius_x
            if dx * dx + dy * dy <= 1:
                blend(x, y, color, opacity)


def triangle(points, color, opacity=1.0):
    (x1, y1), (x2, y2), (x3, y3) = points
    left, right = max(0, int(min(x1, x2, x3))), min(WIDTH, int(max(x1, x2, x3) + 1))
    top, bottom = max(0, int(min(y1, y2, y3))), min(HEIGHT, int(max(y1, y2, y3) + 1))
    denominator = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if not denominator:
        return
    for y in range(top, bottom):
        for x in range(left, right):
            a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / denominator
            b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / denominator
            c = 1 - a - b
            if a >= 0 and b >= 0 and c >= 0:
                blend(x, y, color, opacity)


def line(x1, y1, x2, y2, color, width=2, opacity=1.0):
    steps = int(max(abs(x2 - x1), abs(y2 - y1), 1))
    radius = max(1, width // 2)
    for step in range(steps + 1):
        x = int(x1 + (x2 - x1) * step / steps)
        y = int(y1 + (y2 - y1) * step / steps)
        ellipse(x, y, radius, radius, color, opacity)


def cat_face(cx, cy, scale, opacity=1.0):
    ink = (232, 91, 140)
    lavender = (217, 94, 157)
    face = (255, 224, 237)
    white = (255, 250, 252)
    black = (61, 38, 49)
    bow = (232, 91, 140)

    triangle(((cx - 180 * scale, cy - 150 * scale), (cx - 95 * scale, cy - 285 * scale), (cx - 25 * scale, cy - 165 * scale)), face, 0.72 * opacity)
    triangle(((cx + 25 * scale, cy - 165 * scale), (cx + 95 * scale, cy - 285 * scale), (cx + 180 * scale, cy - 150 * scale)), face, 0.72 * opacity)
    ellipse(cx, cy, 210 * scale, 175 * scale, face, 0.72 * opacity)
    ellipse(cx - 72 * scale, cy - 12 * scale, 16 * scale, 25 * scale, white, 0.7 * opacity)
    ellipse(cx + 72 * scale, cy - 12 * scale, 16 * scale, 25 * scale, white, 0.7 * opacity)
    ellipse(cx - 72 * scale, cy - 9 * scale, 7 * scale, 13 * scale, black, 0.9 * opacity)
    ellipse(cx + 72 * scale, cy - 9 * scale, 7 * scale, 13 * scale, black, 0.9 * opacity)
    ellipse(cx, cy + 39 * scale, 14 * scale, 10 * scale, ink, 0.85 * opacity)
    line(cx - 100 * scale, cy + 40 * scale, cx - 185 * scale, cy + 25 * scale, lavender, max(1, int(3 * scale)), 0.55 * opacity)
    line(cx - 100 * scale, cy + 57 * scale, cx - 190 * scale, cy + 69 * scale, lavender, max(1, int(3 * scale)), 0.55 * opacity)
    line(cx + 100 * scale, cy + 40 * scale, cx + 185 * scale, cy + 25 * scale, lavender, max(1, int(3 * scale)), 0.55 * opacity)
    line(cx + 100 * scale, cy + 57 * scale, cx + 190 * scale, cy + 69 * scale, lavender, max(1, int(3 * scale)), 0.55 * opacity)
    ellipse(cx + 163 * scale, cy - 110 * scale, 58 * scale, 42 * scale, bow, 0.7 * opacity)
    ellipse(cx + 245 * scale, cy - 110 * scale, 58 * scale, 42 * scale, bow, 0.7 * opacity)
    ellipse(cx + 204 * scale, cy - 110 * scale, 26 * scale, 26 * scale, ink, 0.85 * opacity)


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def write_png(path: Path):
    raw = bytearray()
    for y in range(HEIGHT):
        raw.append(0)
        raw.extend(PIXELS[y * WIDTH * 3:(y + 1) * WIDTH * 3])
    payload = b"\x89PNG\r\n\x1a\n"
    payload += png_chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0))
    payload += png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    payload += png_chunk(b"IEND", b"")
    path.write_bytes(payload)


for y in range(HEIGHT):
    for x in range(WIDTH):
        horizontal = x / WIDTH
        vertical = y / HEIGHT
        PIXELS[(y * WIDTH + x) * 3:(y * WIDTH + x + 1) * 3] = bytes((
            int(251 + 3 * vertical + 1 * horizontal),
            int(242 + 7 * vertical + 3 * horizontal),
            int(247 + 5 * vertical + 3 * horizontal),
        ))

# A subdued repeating pattern keeps the wallpaper recognizable without competing
# with the controls layered above it.
cat_face(420, 310, 0.82, 0.24)
cat_face(1510, 780, 0.75, 0.16)
cat_face(1090, 205, 0.42, 0.12)
for x, y, radius in ((130, 150, 5), (760, 720, 7), (1750, 230, 4), (1320, 930, 6)):
    ellipse(x, y, radius, radius, (242, 167, 198), 0.35)

write_png(Path(__file__).with_name("hello_kitty_background.png"))
