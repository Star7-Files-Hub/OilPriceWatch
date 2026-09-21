"""纯标准库生成 PWA 所需的 PNG 图标（无第三方依赖）。

蓝底白圆 + 蓝圆，和 icon.svg 视觉一致。仅用于安装图标，质量够用；
生产如需更精致可替换为设计稿。运行：python gen_icons.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_png(size: int) -> bytes:
    bg = (15, 17, 21, 255)      # #0f1115
    disc = (76, 141, 255, 255)  # #4c8dff
    cx = cy = size // 2
    r = size * 0.40
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0
        for x in range(size):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            px = disc if d < r else bg
            raw += bytes(px)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), 9)
    return sig + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def main() -> None:
    out = Path(__file__).resolve().parent / "static"
    out.mkdir(exist_ok=True)
    for s in (192, 512):
        (out / f"icon-{s}.png").write_bytes(make_png(s))
        print(f"wrote static/icon-{s}.png")


if __name__ == "__main__":
    main()
