#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 docs/演示视频分镜单.md 渲染成逐段 PNG，供录制现场照着看。

分镜单是「无声字幕版」：表格第四列是字幕，用 ‖ 分隔多条。
本脚本除了出图，还会逐条核对字幕字数（≤ 18 字）和平均停留时长。

用法：python tools/gen_storyboard.py
输出：docs/分镜图/00-速查.png + 01-开场.png ... 07-收尾.png
依赖：本机 Edge（headless 截图）+ Pillow（裁掉底部空白）
"""

import io
import os
import re
import shutil
import subprocess
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MD = os.path.join(ROOT, "docs", "演示视频分镜单.md")
OUT = os.path.join(ROOT, "docs", "分镜图")
TMP = os.path.join(ROOT, ".tmp_storyboard")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

WIDTH = 1240          # 版心宽度（A4 竖版 150dpi）
SCALE = 2             # 输出 2480px 宽，打印/远看都清楚
TALL = 3200           # 先给足高度，截完再裁掉底部白边

MAX_CHARS = 18        # 单条字幕上限

SECTIONS = [
    ("01", "开场"),
    ("02", "罗盘"),
    ("03", "问政"),
    ("04", "督办"),
    ("05", "单位"),
    ("06", "交办"),
    ("07", "收尾"),
]

CSS = """
*{box-sizing:border-box}
html,body{overflow:hidden}
body{margin:0;width:%(w)dpx;background:#fff;color:#1a1a1a;
     font-family:"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif;
     -webkit-font-smoothing:antialiased}
.head{background:#1e3a5f;color:#fff;padding:30px 40px 26px}
.head .no{font-size:15px;letter-spacing:3px;opacity:.7;font-weight:600}
.head h1{margin:8px 0 0;font-size:38px;font-weight:600;letter-spacing:1px}
.head .meta{margin-top:12px;font-size:16px;opacity:.88}
.head .meta b{font-weight:600}
.head .meta .warn{color:#ffd8a8}
table{width:calc(100%% - 80px);margin:0 40px;border-collapse:collapse}
th{background:#eef2f7;color:#44546a;font-size:15px;font-weight:600;text-align:left;
   padding:10px 14px;border-bottom:2px solid #cfd8e3;letter-spacing:.5px}
td{vertical-align:top;padding:18px 14px;border-bottom:1px solid #e7ecf2;
   font-size:21px;line-height:1.7}
tr:nth-child(even) td{background:#fafbfc}
td.no{width:74px;color:#94a0b0;font-size:17px;font-weight:700}
td.tm{width:150px;color:#1e3a5f;font-weight:700;font-size:19px}
td.hand{width:360px;color:#44546a;font-size:19px}
td.hand strong{color:#1e3a5f;font-weight:700}
td.sub{color:#111;font-weight:500;font-size:20px;line-height:1.55}
td.sub .s{display:block;padding:4px 0 4px 12px;border-left:3px solid #cfe0f5;
          margin:2px 0}
td.sub .s b{color:#b42318;font-weight:700}
td.sub .s.over{border-left-color:#e8590c;color:#b45309}
td.sub .blank{color:#9aa5b1;font-style:normal;font-size:18px}
.note{margin:20px 40px 30px;padding:16px 20px;background:#fff8e6;
      border-left:5px solid #d99100;font-size:17px;line-height:1.85;color:#5c4a1a}
.note strong{color:#8a5b00}
.note p{margin:0 0 8px}
.note p:last-child{margin:0}
.foot{margin:0 40px 26px;font-size:14px;color:#9aa5b1}
"""


def md_inline(t):
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return t.replace("|", "｜")


def cn(s):
    return len(re.findall(r"[\u4e00-\u9fa5]", s))


def parse():
    src = io.open(MD, encoding="utf-8").read()
    blocks = re.split(r"\n## ", src)
    sections, cuts, rails, spec = [], [], [], []

    for blk in blocks[1:]:
        title = blk.split("\n")[0].strip()
        key = title[:2]
        m = re.search(r"成片 (\d+:\d+)[–-](\d+:\d+)", title)
        rows = []
        for line in blk.split("\n"):
            if re.match(r"\| \d+-\d+ \|", line):
                c = [x.strip() for x in line.split("|")]
                rows.append({"no": c[1], "tm": c[2], "hand": c[3],
                             "sub": c[4] if len(c) > 4 else ""})
        if rows and m:
            notes = [x[1:].strip() for x in blk.split("\n")
                     if x.startswith("> ") and x[1:].strip()]
            sections.append({"key": key, "title": title, "start": m.group(1),
                             "end": m.group(2), "rows": rows, "notes": notes})
        elif "剪点速查" in title:
            for line in blk.split("\n"):
                if re.match(r"\| [③⑤⑥]", line):
                    c = [x.strip() for x in line.split("|")]
                    cuts.append(c[1:5])
        elif "分轨录制" in title:
            for line in blk.split("\n"):
                if re.match(r"\| [A-D] \|", line):
                    c = [x.strip() for x in line.split("|")]
                    rails.append(c[1:4])
        elif "录制前设定" in title:
            for line in blk.split("\n"):
                if line.startswith("| ") and not line.startswith("| 项 ") \
                        and "---" not in line:
                    c = [x.strip() for x in line.split("|")]
                    spec.append(c[1:3])
    return sections, cuts, rails, spec


def secs(t):
    a, b = t.split(":")
    return int(a) * 60 + int(b)


def subs_of(row):
    """一格里的多条字幕。括号开头的整格是备注（留白/不出字），不算字幕。"""
    raw = (row.get("sub") or "").strip()
    body = re.sub(r"^（[^）]*）", "", raw).strip()
    if not body:
        return []
    return [s.strip() for s in body.split("‖") if s.strip()]


def stats(rows):
    items = []
    for r in rows:
        for s in subs_of(r):
            items.append(s)
    dur = sum(secs(r["tm"].split("–")[1]) - secs(r["tm"].split("–")[0])
              for r in rows)
    n = len(items)
    return {
        "n": n,
        "dur": dur,
        "avg": round(dur / n, 1) if n else 0,
        "longest": max(cn(s) for s in items) if items else 0,
        "over": [s for s in items if cn(s) > MAX_CHARS],
        "chars": sum(cn(s) for s in items),
    }


def html_for(sec, idx, total):
    r0, r1 = sec["start"], sec["end"]
    st = stats(sec["rows"])
    if st["avg"] and st["avg"] < 2.8:
        state = "偏密，每条别超 18 字"
    elif st["avg"] and st["avg"] > 4.6:
        state = "从容"
    else:
        state = "合适"
    body = []
    for r in sec["rows"]:
        items = subs_of(r)
        if items:
            cells = []
            for s in items:
                cls = "s over" if cn(s) > MAX_CHARS else "s"
                cells.append('<span class="%s">%s</span>' % (cls, md_inline(s)))
            sub = "".join(cells)
        else:
            sub = '<span class="blank">%s</span>' % md_inline(
                (r["sub"] or "（无字幕）").strip())
        body.append(
            "<tr><td class=no>%s</td><td class=tm>%s</td>"
            "<td class=hand>%s</td><td class=sub>%s</td></tr>"
            % (r["no"], r["tm"].replace("–", "–"), md_inline(r["hand"]), sub)
        )
    notes = "".join("<p>%s</p>" % md_inline(n) for n in sec["notes"])
    warn = ""
    if st["over"]:
        warn = '　·　<span class="warn">%d 条超 18 字</span>' % len(st["over"])
    return """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s</style></head><body>
<div class=head>
  <div class=no>工作台演示 · 分镜 %(idx)s / %(tot)s　·　无声字幕版</div>
  <h1>%(title)s</h1>
  <div class=meta>成片 <b>%(r0)s – %(r1)s</b>　·　%(dur)s 秒　·　字幕 <b>%(n)d</b> 条　·　平均每条 <b>%(avg)s</b> 秒　·　%(state)s%(warn)s</div>
</div>
<table><thead><tr><th>#</th><th>时间</th><th>手（只做这一列）</th><th>字幕（后期一条一屏）</th></tr></thead>
<tbody>%(body)s</tbody></table>
%(note)s
<div class=foot>窗口 1920×920 贴左上角，底部 160px 留给字幕。每步做完停 1 秒。</div>
</body></html>""" % dict(
        css=CSS % {"w": WIDTH}, idx="%02d" % idx, tot="%02d" % total,
        title=re.sub(r"成片.*$", "", sec["title"]).replace("　", " ").strip(),
        r0=r0, r1=r1, dur=st["dur"], n=st["n"], avg=st["avg"],
        state=state, warn=warn,
        body="".join(body), note=("<div class=note>%s</div>" % notes) if notes else "",
    )


def html_overview(sections, cuts, rails, spec):
    # 成片时长、镜头数、素材总长一律现算，不写死在这里：
    # 上一版写死 6:15 / 37 个镜头，③ 加参谋后全错。
    n_shots = sum(len(x["rows"]) for x in sections)
    end = sections[-1]["end"]
    raw = io.open(MD, encoding="utf-8").read()
    m = re.search(r"素材总长预计\s*\*\*(\d+)\s*分\s*(\d+)\s*秒\*\*", raw)
    mat = ("%d:%02d" % (int(m.group(1)), int(m.group(2)))) if m else "—"
    sp_rows = []
    tn = tc = 0
    for i, sec in enumerate(sections, 1):
        st = stats(sec["rows"])
        tn += st["n"]
        tc += st["chars"]
        stt = "偏密" if st["avg"] < 2.8 else ("从容" if st["avg"] > 4.6 else "合适")
        sp_rows.append(
            "<tr><td>%s%s</td><td>%ds</td><td>%d 条</td><td>%s 秒</td><td>%s</td></tr>"
            % (sec["key"], SECTIONS[i - 1][1], st["dur"], st["n"], st["avg"], stt))
    sp = "".join(sp_rows)
    ct = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % tuple(
        md_inline(x) for x in row) for row in cuts)
    rl = "".join("<tr><td><b>%s</b></td><td>%s</td><td>%s</td></tr>" % (
        a, md_inline(b), md_inline(c)) for a, b, c in rails)
    sc = "".join("<tr><td>%s</td><td>%s</td></tr>" % (
        md_inline(a), md_inline(b)) for a, b in spec)
    return """<!doctype html><html><head><meta charset="utf-8">
<style>%(css)s
h2{font-size:26px;margin:34px 40px 14px;color:#1e3a5f}
h2 span{font-size:15px;color:#8b95a3;font-weight:400;margin-left:10px}
table.mini td,table.mini th{font-size:19px;padding:13px 14px;line-height:1.6}
table.mini td:first-child{width:210px;font-weight:700;color:#1e3a5f}
</style></head><body>
<div class=head>
  <div class=no>工作台演示 · 录制速查　·　无声字幕版</div>
  <h1>先看这张</h1>
  <div class=meta>成片 <b>%(end)s</b>（%(mins)d 分）　·　素材约 <b>%(mat)s</b>　·　%(ns)d 个镜头　·　字幕 <b>%(tn)d</b> 条 / %(tc)d 字</div>
</div>
<h2>开录前<span>这一栏不照做，后面全白干</span></h2>
<table class=mini><thead><tr><th>项</th><th>值</th></tr></thead><tbody>%(sc)s</tbody></table>
<h2>分轨录制<span>别一条录到底，四轨分开录，剪辑时拼</span></h2>
<table class=mini><thead><tr><th>轨</th><th>内容</th><th>起止身份</th></tr></thead><tbody>%(rl)s</tbody></table>
<h2>字幕密度<span>没有旁白，字幕就是唯一的信息通道</span></h2>
<table class=mini><thead><tr><th>段落</th><th>时长</th><th>字幕</th><th>每条停留</th><th>状态</th></tr></thead><tbody>%(sp)s</tbody></table>
<h2>剪点<span>剪掉的长度取整秒，相位才接得上</span></h2>
<table class=mini><thead><tr><th>位置</th><th>素材</th><th>剪成</th><th>规则</th></tr></thead><tbody>%(ct)s</tbody></table>
<div class=note><p><strong>三条铁律</strong></p>
<p>一、问数必须逐字打，不能粘贴。二、20 秒等待要等满，手离开鼠标别切窗口，答案出来再录 3 秒。三、一条字幕一屏只出一句，超 18 字就断条。</p>
<p><strong>字幕在剪完之后才加。</strong>按剪前素材排字幕，一切点就全错位了。</p></div>
<div class=foot>工作台演示 · 录制速查 · 2026-11-25</div>
</body></html>""" % dict(css=CSS % {"w": WIDTH}, sp=sp, ct=ct, rl=rl, sc=sc,
                                 tn=tn, tc=tc, end=end, mat=mat, ns=n_shots,
                                 mins=round(secs(end) / 60))


def shoot(html_path, png_path):
    subprocess.run([
        EDGE, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
        "--user-data-dir=" + os.path.join(TMP, "prof").replace("\\", "/"),
        "--force-device-scale-factor=%d" % SCALE,
        "--window-size=%d,%d" % (WIDTH, TALL),
        "--screenshot=%s" % png_path.replace("\\", "/"),
        "file:///" + html_path.replace("\\", "/"),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=120)


def trim(png_path):
    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    px = im.load()
    bottom = h
    for y in range(h - 1, -1, -1):
        for x in range(0, w, 9):
            r, g, b = px[x, y]
            if r + g + b < 720:
                bottom = min(h, y + 60)
                break
        if bottom != h:
            break
    im.crop((0, 0, w, bottom)).save(png_path, optimize=True)
    return w, bottom


def main():
    if not os.path.exists(EDGE):
        sys.exit("找不到 Edge：" + EDGE)
    sections, cuts, rails, spec = parse()
    if len(sections) != 7:
        sys.exit("解析到 %d 段，应为 7 段——分镜单格式变了？" % len(sections))
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(TMP, exist_ok=True)

    jobs = [("00", "速查", html_overview(sections, cuts, rails, spec))]
    for i, sec in enumerate(sections, 1):
        jobs.append(("%02d" % i, SECTIONS[i - 1][1], html_for(sec, i, 7)))

    for num, name, html in jobs:
        hp = os.path.join(TMP, "sb_%s.html" % num)
        raw = os.path.join(TMP, "sb_%s.png" % num)
        pp = os.path.join(OUT, "%s-%s.png" % (num, name))
        io.open(hp, "w", encoding="utf-8").write(html)
        shoot(hp, raw)
        w, h = trim(raw)
        shutil.move(raw, pp)
        print("%-18s %4d x %4d  %6.0f KB" % (os.path.basename(pp), w, h,
                                             os.path.getsize(pp) / 1024))

    # 核对：字幕条数、超长条、平均停留
    print("\n字幕核对（上限 %d 字/条）" % MAX_CHARS)
    bad = 0
    for i, sec in enumerate(sections, 1):
        st = stats(sec["rows"])
        flag = ""
        if st["over"]:
            bad += len(st["over"])
            flag = "  超长：" + " / ".join("%s(%d字)" % (s, cn(s)) for s in st["over"])
        print("  %s %s  %2d 条  %d 字  平均 %.1fs%s"
              % (sec["key"], SECTIONS[i - 1][1], st["n"], st["chars"], st["avg"], flag))
    if bad:
        print("  共 %d 条超长，改分镜单后重跑。" % bad)


if __name__ == "__main__":
    main()
