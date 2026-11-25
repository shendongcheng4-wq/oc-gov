#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成演示用的佐证原件（4-4 要上传的那两个文件）。

为什么要现做：
- attach.put 按 sha1 去重，同一份文件第二次上传会因为「已存在」而不新增条目，
  正式录制时画面就看不出「刚传上去」这件事。所以素材每次都得是新的。
- 文件名要短，台账列表一行放得下；扩展名必须落在 ALLOWED_EXT 里。

输出到 桌面/演示佐证/：
  监理月报11月.pdf   一页公文，能打开、有内容（对应 4-4「现在收的是原件」）
  销号台账.xlsx      真表格，呼应 4-3 那句「打上'销号台账.xlsx'就算附了佐证」

用法：python tools/make_evidence.py
"""

import os
import sys
import zipfile
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont

DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
OUTDIR = os.path.join(DESKTOP, "演示佐证")

FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"


def F(size, bold=False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


# ── 一页 A4 的监理月报 ────────────────────────────────────────────────
def make_pdf(path, preview=None):
    W, H = 1654, 2339          # A4 @ 200dpi
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    M = 130                    # 页边距

    d.text((W // 2, M), "高铁东站周边路网配套工程", font=F(46, True), fill="#111", anchor="ma")
    d.text((W // 2, M + 70), "监 理 月 报", font=F(34, True), fill="#111", anchor="ma")
    d.text((W // 2, M + 130), "（2026 年 11 月）", font=F(26), fill="#333", anchor="ma")
    d.line((M, M + 200, W - M, M + 200), fill="#111", width=3)

    y = M + 240
    rows = [
        ("工程部位", "站前广场段、东环路交叉口至迎宾大道"),
        ("本月完成", "沥青下面层摊铺 1.8 公里；雨污水管道 620 米；交通导改护栏安装完毕"),
        ("累计形象进度", "路基 100%，雨污水 92%，路面 61%，交安 35%"),
        ("质量情况", "抽检压实度 12 点，合格 12 点；沥青混合料温度记录齐全"),
        ("安全文明", "围挡连续封闭，夜间爆闪灯 24 盏；本月无安全事故"),
        ("存在问题", "东环路交叉口东侧 3 户商铺雨污分流未接入，影响该段闭水试验"),
    ]
    for k, v in rows:
        d.text((M, y), k, font=F(24, True), fill="#1e3a5f")
        # 正文折行
        line = ""
        yy = y
        for ch in v:
            if d.textlength(line + ch, font=F(24)) > W - M * 2 - 190:
                d.text((M + 190, yy), line, font=F(24), fill="#222")
                line, yy = ch, yy + 40
            else:
                line += ch
        d.text((M + 190, yy), line, font=F(24), fill="#222")
        y = max(yy + 40, y + 46) + 26

    y += 20
    d.text((M, y), "监理意见", font=F(24, True), fill="#1e3a5f")
    y += 44
    for t in ["本月进度满足节点计划，路面工程已进入下面层，建议同步启动上面层备料。",
              "东侧商铺雨污接入问题已发监理通知单（编号 JL-2026-118），请建设单位协调属地。",
              "下月重点：完成迎宾大道段沥青上面层，组织交安设施预验收。"]:
        d.text((M + 20, y), "· " + t, font=F(23), fill="#222")
        y += 42

    y = H - M - 210
    d.line((M, y, W - M, y), fill="#bbb", width=2)
    d.text((W - M, y + 30), "监理单位（盖章）", font=F(24), fill="#333", anchor="ra")
    d.text((W - M, y + 80), "总监理工程师：", font=F(24), fill="#333", anchor="ra")
    d.text((W - M, y + 130), "2026 年 11 月 20 日", font=F(24), fill="#333", anchor="ra")

    # 工程量表 + 附件：一页纸只有半页字，点开容易让人以为「就这些？」
    ty = H - M - 760
    d.text((M, ty), "本月主要工程量", font=F(24, True), fill="#1e3a5f")
    ty += 46
    colx = [M + 20, M + 470, M + 700, M + 960]
    for i, h in enumerate(["项目", "单位", "本月完成", "累计完成"]):
        d.text((colx[i], ty), h, font=F(22, True), fill="#333")
    ty += 34
    d.line((M, ty, W - M, ty), fill="#999", width=1)
    ty += 14
    for row in [["沥青下面层", "公里", "1.8", "4.6"],
                ["雨污水管道", "米", "620", "3980"],
                ["交通导改护栏", "米", "1200", "1200"],
                ["压实度抽检", "点", "12", "86"]]:
        for i, v in enumerate(row):
            d.text((colx[i], ty), v, font=F(21), fill="#222")
        ty += 34
    ty += 30
    d.text((M, ty), "附件：现场影像 6 张；沥青混合料检测报告 2 份；监理通知单 1 份。",
           font=F(21), fill="#444")

    im.save(path, "PDF", resolution=200)
    if preview:
        im.save(preview)
    return path


# ── 最小可用的 xlsx（手写 OOXML，不依赖 openpyxl） ────────────────────
CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

WB = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="销号台账" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def sheet_xml(rows, widths):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<cols>']
    for i, w in enumerate(widths, 1):
        out.append('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i, i, w))
    out.append('</cols><sheetData>')
    for r, row in enumerate(rows, 1):
        out.append('<row r="%d" ht="24" customHeight="1">' % r)
        for c, val in enumerate(row):
            col = chr(65 + c)
            style = ' s="1"' if r == 1 else ""
            out.append('<c r="%s%d" t="inlineStr"%s><is><t>%s</t></is></c>'
                       % (col, r, style, escape(str(val))))
        out.append('</row>')
    out.append('</sheetData></worksheet>')
    return "".join(out)


STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="微软雅黑"/></font>
<font><b/><sz val="11"/><name val="微软雅黑"/></font></fonts>
<fills count="1"><fill><patternFill patternType="none"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="2"><xf/><xf fontId="1" applyFont="1"/></cellXfs>
</styleSheet>"""


def make_xlsx(path):
    rows = [
        ["序号", "事项", "责任单位", "整改措施", "责任人", "完成时间", "销号情况"],
        ["1", "站前广场段级配碎石含水量偏高", "施工单位", "翻晒后重新碾压，复检压实度", "张建国", "2026-11-06", "已销号"],
        ["2", "东环路 K1+200 处井盖高出路面", "施工单位", "调整井筒标高，重新摊铺", "李海涛", "2026-11-12", "已销号"],
        ["3", "围挡夜间反光标识缺失", "施工单位", "补装反光条 36 处、爆闪灯 8 盏", "王志强", "2026-11-15", "已销号"],
        ["4", "东侧 3 户商铺雨污分流未接入", "属地街道", "协调入户改造，预计 11 月底完成", "周敏", "2026-11-30", "未销号"],
        ["5", "迎宾大道段沥青温度记录缺 2 车次", "监理单位", "补齐测温记录并复测", "陈立", "2026-11-19", "已销号"],
    ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("xl/workbook.xml", WB)
        z.writestr("xl/_rels/workbook.xml.rels", WB_RELS)
        z.writestr("xl/styles.xml", STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml(rows, [6, 34, 12, 34, 10, 13, 10]))
    return path


def check(path):
    """按 core/oversight/attach.py 的规则预检，别等录制时才发现传不上去。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.oversight import attach
    name = os.path.basename(path)
    size = os.path.getsize(path)
    ext = os.path.splitext(name)[1].lower()
    ok_ext = ext in attach.ALLOWED_EXT
    ok_size = 0 < size <= attach.MAX_BYTES
    print("  %-16s %7.1f KB  %-6s 扩展名%s 大小%s"
          % (name, size / 1024, ext,
             "✓" if ok_ext else "✗ 不在白名单",
             "✓" if ok_size else "✗ 超 9MB"))
    return ok_ext and ok_size


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    pdf = os.path.join(OUTDIR, "监理月报11月.pdf")
    xls = os.path.join(OUTDIR, "销号台账.xlsx")
    make_pdf(pdf)
    make_xlsx(xls)
    print("输出目录：%s\n" % OUTDIR)
    ok = all([check(pdf), check(xls)])
    print("\n预检 %s" % ("通过" if ok else "未通过"))
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
