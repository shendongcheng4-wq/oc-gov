# -*- coding: utf-8 -*-
"""按新 SRT 重压全片（两遍法胶囊字幕，样式沿用成片）。

流程：
  1. 每段把文案渲染到 1920x920 黑底，读灰度墨迹算 bbox（不用 PIL）
  2. 生成圆角胶囊 ASS（品牌蓝 #1A63C9 12% + 白字雅黑 50 粗 + fad 150ms）
  3. 源片 scale+pad 到 1920x920 后烧字幕，统一 x264 / aac 参数
  4. concat 拼接成整片

用法：
  python tools/burn_v3.py            # 全片
  python tools/burn_v3.py 01 03      # 只跑指定段（调试）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_subtitle_v2 import SEGS  # noqa: E402

FF = r"D:/workbuddy-workspace/ffmpeg/bin/ffmpeg.exe"
W, H = 1920, 920
FS = 50
PAD_X, PAD_Y, BOTTOM_GAP = 46, 17, 42
BLUE = "&H00C9631A"   # #1A63C9 (BGR)
ALPHA = "&H1E&"
SRC = Path(r"C:/Users/Administrator/Desktop/市长工作台视频录制")
TMP = Path(r"E:/octop-dashan/oc-gov/.workbuddy/tmp/burn")
OUT = SRC / "成片_第三人称"

HEAD = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: T,Microsoft YaHei,{FS},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

TABLE = bytes(1 if i > 40 else 0 for i in range(256))


def ts(t: float) -> str:
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def ink_box(one_ass: Path) -> tuple[int, int, int, int]:
    """渲染一行到黑底，返回 (x0, x1, y0, y1) 的墨迹范围。"""
    p = subprocess.run(
        [FF, "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d=0.2",
         "-vf", "ass=one.ass", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True, cwd=str(one_ass.parent),
    )
    data = p.stdout.translate(TABLE)
    if len(data) < W * H:
        raise RuntimeError("raw 帧尺寸不对：%d" % len(data))

    y0 = y1 = None
    x0, x1 = W, -1
    for y in range(H):
        row = data[y * W:(y + 1) * W]
        a = row.find(b"\x01")
        if a < 0:
            continue
        b = row.rfind(b"\x01")
        if y0 is None:
            y0 = y
        y1 = y
        if a < x0:
            x0 = a
        if b > x1:
            x1 = b
    if y0 is None:
        raise RuntimeError("空墨迹")
    return x0, x1, y0, y1


def build_ass(sid: str, cues) -> tuple[Path, list[str]]:
    d = TMP / sid
    d.mkdir(parents=True, exist_ok=True)
    geo, report = [], []

    for i, (st, en, text) in enumerate(cues):
        ass_text = text.replace("\n", "\\N")
        one = [HEAD, "Dialogue: 0,0:00:00.00,0:00:00.50,T,,0,0,0,,"
                     "{\\an5\\pos(%d,%d)}%s" % (W // 2, H // 2, ass_text)]
        (d / "one.ass").write_text("\n".join(one), encoding="utf-8")
        x0, x1, y0, y1 = ink_box(d / "one.ass")
        iw, ih = x1 - x0 + 1, y1 - y0 + 1
        off = (y0 + y1) / 2.0 - H / 2.0
        offx = (x0 + x1) / 2.0 - W / 2.0
        geo.append((st, en, ass_text, iw, ih, off, offx))
        report.append("  %5.2f–%5.2f ink=%4dx%-3d %s" % (st, en, iw, ih, text.replace("\n", " / ")))

    ev = [HEAD]
    for st, en, text, iw, ih, off, offx in geo:
        w = iw + PAD_X * 2
        h = ih + PAD_Y * 2
        r = h / 2.0
        x0 = (W - w) / 2.0
        y0 = H - BOTTOM_GAP - h
        p = []
        p.append("m %.1f %.1f" % (x0 + r, y0))
        p.append("l %.1f %.1f" % (x0 + w - r, y0))
        p.append("b %.1f %.1f %.1f %.1f %.1f %.1f" % (x0 + w, y0, x0 + w, y0, x0 + w, y0 + r))
        p.append("l %.1f %.1f" % (x0 + w, y0 + h - r))
        p.append("b %.1f %.1f %.1f %.1f %.1f %.1f" % (x0 + w, y0 + h, x0 + w, y0 + h, x0 + w - r, y0 + h))
        p.append("l %.1f %.1f" % (x0 + r, y0 + h))
        p.append("b %.1f %.1f %.1f %.1f %.1f %.1f" % (x0, y0 + h, x0, y0 + h, x0, y0 + h - r))
        p.append("l %.1f %.1f" % (x0, y0 + r))
        p.append("b %.1f %.1f %.1f %.1f %.1f %.1f" % (x0, y0, x0, y0, x0 + r, y0))
        path = " ".join(p)

        ev.append("Dialogue: 0,%s,%s,T,,0,0,0,,{\\an7\\pos(0,0)\\p1\\1c%s\\1a%s\\bord0\\shad0"
                  "\\fad(150,150)}%s" % (ts(st), ts(en), BLUE, ALPHA, path))
        ev.append("Dialogue: 0,%s,%s,T,,0,0,0,,{\\an5\\pos(%.1f,%.1f)\\fad(150,150)}%s"
                  % (ts(st), ts(en), W / 2.0 - offx, y0 + h / 2.0 - off, text))

    out = d / "sub.ass"
    out.write_text("\n".join(ev), encoding="utf-8")
    return out, report


def burn(sid: str, name: str, src: str, cues) -> Path:
    ass, report = build_ass(sid, cues)
    dst = TMP / f"{sid}.mp4"
    cmd = [FF, "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(SRC / src),
           "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,ass={ass.name}",
           "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", str(dst)]
    print(f"[{sid}] {name} 压字幕（{len(cues)} 条）")
    print("\n".join(report))
    subprocess.run(cmd, check=True, cwd=str(ass.parent))
    return dst


def main() -> None:
    only = set(sys.argv[1:])
    TMP.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    parts = []
    for sid, name, src, dur, who, tagline, cues, note in SEGS:
        if only and sid not in only:
            continue
        parts.append(burn(sid, name, src, cues))

    if only:
        print("调试模式，不拼接；产物：" + ", ".join(str(p) for p in parts))
        return

    lst = TMP / "list.txt"
    lst.write_text("".join("file '%s'\n" % str(p).replace("'", "'\\''") for p in parts),
                   encoding="utf-8")
    final = OUT / "完整_成片_第三人称.mp4"
    print("拼接 →", final)
    subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", "-movflags", "+faststart", str(final)], check=True)

    ffprobe = str(Path(FF).with_name("ffprobe.exe"))
    d = subprocess.run([ffprobe, "-v", "error",
                        "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                       capture_output=True, text=True, check=True).stdout.strip()
    print("完成：%s  %.1f 秒" % (final, float(d)))


if __name__ == "__main__":
    main()
