#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从分镜单抽出字幕文案，生成 docs/字幕文案.md。

用途：后期上字幕时照这份打，不用从分镜图里抄。
分镜单改了之后重跑一次即可（两份内容同源，不会对不上）。

用法：python tools/gen_subtitles.py
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gen_storyboard as g  # noqa: E402

ROOT = g.ROOT
OUT = os.path.join(ROOT, "docs", "字幕文案.md")


def main():
    sections, _, _, _ = g.parse()
    out = [
        "# 字幕文案（无声版）",
        "",
        "> 由 `tools/gen_subtitles.py` 从 `演示视频分镜单.md` 生成，**别手改这份**——改分镜单，重跑。",
        "> 一条一屏、每条 ≤ 18 字；序号是全局流水号，方便和剪辑对位。",
        "> 镜头号后面的时间是**成片时间**，素材剪完才对得上。`〔8〕` 这类占位数字录制时按屏幕现填。",
        "",
    ]
    n = 0
    total_chars = 0
    for i, sec in enumerate(sections, 1):
        st = g.stats(sec["rows"])
        out.append("## %s %s　成片 %s–%s　%d 条 / 每条均 %.1f 秒"
                   % (sec["key"].strip(), g.SECTIONS[i - 1][1], sec["start"], sec["end"],
                      st["n"], st["avg"]))
        out.append("")
        for r in sec["rows"]:
            items = g.subs_of(r)
            if not items:
                note = (r["sub"] or "").strip()
                out.append("**%s**　%s　——%s" % (r["no"], r["tm"], note or "（无字幕）"))
                out.append("")
                continue
            out.append("**%s**　%s" % (r["no"], r["tm"]))
            out.append("")
            for s in items:
                n += 1
                total_chars += g.cn(s)
                out.append("%03d　%s" % (n, s))
            out.append("")
    out.append("---")
    out.append("")
    out.append("全片 **%d 条字幕，%d 字**。" % (n, total_chars))
    out.append("")

    io.open(OUT, "w", encoding="utf-8").write("\n".join(out))
    print("写出 %s：%d 条，%d 字" % (os.path.basename(OUT), n, total_chars))


if __name__ == "__main__":
    main()
