#!/usr/bin/env python3
"""Draws the Lagscope icon (dark tile with a frame-time line that spikes) and writes lagscope/web/icon.ico.

No image library needed. Sizes 16, 24, 32, 48, 64, 256 (32-bit BGRA BMP, 256 px as PNG) and icon.png (favicon)."""
import math
import os
import struct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BG = (0x16, 0x1a, 0x22)
LINE = (0x6c, 0x9c, 0xff)
SPIKE = (0xff, 0x63, 0x69)
# Linie in Einheitskoordinaten (0..1): ruhig, dann ein Ruckler-Ausschlag
PTS = [(0.10, 0.62), (0.30, 0.62), (0.38, 0.55), (0.46, 0.62), (0.54, 0.62), (0.62, 0.18), (0.70, 0.82),
       (0.78, 0.62), (0.90, 0.62)]


def dist_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


def render(n):
    px = []
    r = n * 0.2           # Eckenradius
    w = max(1.2, n * 0.075)  # Linienbreite
    for y in range(n):
        row = []
        for x in range(n):
            cx, cy = x + 0.5, y + 0.5
            # abgerundetes Quadrat (Kantenglättung über Abstand)
            qx, qy = max(r - cx, 0, cx - (n - r)), max(r - cy, 0, cy - (n - r))
            d = math.hypot(qx, qy) - r
            a_bg = max(0.0, min(1.0, 0.5 - d))
            col = BG
            best, seg_i = 1e9, 0
            for i in range(len(PTS) - 1):
                (ax, ay), (bx, by) = PTS[i], PTS[i + 1]
                dd, _ = dist_seg(cx, cy, ax * n, ay * n, bx * n, by * n)
                if dd < best:
                    best, seg_i = dd, i
            a_line = max(0.0, min(1.0, w / 2 + 0.5 - best))
            lc = SPIKE if 4 <= seg_i <= 6 else LINE
            col = tuple(round(c0 * (1 - a_line) + c1 * a_line) for c0, c1 in zip(col, lc))
            row.append((col[2], col[1], col[0], round(255 * a_bg)))  # BGRA
        px.append(row)
    return px


def bmp_entry(n):
    pix = render(n)
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    data = b"".join(bytes(c for p in row for c in p) for row in reversed(pix))
    mask_row = ((n + 31) // 32) * 4
    return header + data + b"\0" * (mask_row * n)


def png_entry(n):
    import zlib
    pix = render(n)
    raw = b"".join(b"\0" + bytes(c for (b_, g, r, a) in row for c in (r, g, b_, a)) for row in pix)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    sizes = [16, 24, 32, 48, 64, 256]
    images = [png_entry(s) if s >= 256 else bmp_entry(s) for s in sizes]
    with open(os.path.join(ROOT, "lagscope", "web", "icon.png"), "wb") as f:
        f.write(png_entry(64))
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for s, img in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    out += b"".join(images)
    path = os.path.join(ROOT, "lagscope", "web", "icon.ico")
    with open(path, "wb") as f:
        f.write(out)
    print(f"{path}: {len(out)} Bytes")


if __name__ == "__main__":
    main()
