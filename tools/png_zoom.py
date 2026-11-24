"""把剪贴板截图放大一块看细节：没有 PIL，就自己解 PNG。

只支持 8 位 RGB/RGBA、非隔行 —— Windows 剪贴板截图正好是这种。
用途是诊断界面像素级问题（哪一格被裁了、省略号在不在），不是通用图像工具。
"""
from __future__ import annotations

import struct
import sys
import zlib


def read_png(path: str):
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    pos = 8
    idat = b""
    w = h = bd = ct = 0
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", body)
            assert bd == 8 and il == 0 and ct in (2, 6), f"不支持：bd={bd} ct={ct} il={il}"
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    bpp = 4 if ct == 6 else 3
    raw = zlib.decompress(idat)
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return w, h, bpp, bytes(out)


def write_png(path: str, w: int, h: int, bpp: int, pix: bytes) -> None:
    ct = 6 if bpp == 4 else 2
    raw = bytearray()
    stride = w * bpp
    for y in range(h):
        raw.append(0)
        raw += pix[y * stride:(y + 1) * stride]

    def chunk(typ: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + typ + body
                + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
    open(path, "wb").write(data)


def crop_zoom(src: str, dst: str, box: tuple[int, int, int, int], k: int = 5) -> None:
    w, h, bpp, pix = read_png(src)
    x0, y0, x1, y1 = box
    x1, y1 = min(x1, w), min(y1, h)
    cw, ch = x1 - x0, y1 - y0
    ow, oh = cw * k, ch * k
    out = bytearray(ow * oh * bpp)
    for oy in range(oh):
        sy = y0 + oy // k
        row = bytearray(ow * bpp)
        for ox in range(ow):
            sx = x0 + ox // k
            si = (sy * w + sx) * bpp
            row[ox * bpp:(ox + 1) * bpp] = pix[si:si + bpp]
        out[oy * ow * bpp:(oy + 1) * ow * bpp] = row
    write_png(dst, ow, oh, bpp, bytes(out))
    print(f"{src} {w}x{h} -> {dst} {ow}x{oh} (crop {box} x{k})")


if __name__ == "__main__":
    if len(sys.argv) < 7:
        raise SystemExit("用法：python tools/png_zoom.py <src.png> <dst.png> x0 y0 x1 y1 [放大倍数]")
    src, dst = sys.argv[1], sys.argv[2]
    x0, y0, x1, y1 = (int(v) for v in sys.argv[3:7])
    k = int(sys.argv[7]) if len(sys.argv) > 7 else 5
    crop_zoom(src, dst, (x0, y0, x1, y1), k)
