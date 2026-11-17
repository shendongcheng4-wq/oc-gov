"""生成合成内容标识（GB 45438—2025，强制性国标，2025-09-01 起施行）。

跑法::

    .venv\\Scripts\\python.exe tests\\aigc_mark.py

这条国标要两样东西，缺一样都不算标了：

- **显式标识**：人一眼能看见的文字提示（``本文件由人工智能生成，请核对后使用。``）；
- **隐式标识**：文件元数据里的机读标记（docx 的 ``keywords=AIGC``、html 的 ``<meta name="AIGC">``）。

断言八件事：

1. docx 默认两条标识都有，且 XML 合法；
2. ``aigc=False`` 的一条都没有 —— 反过来标上去也是一种不实，
   红头督办通报是用户填表、代码排版，本来就不含 AI 生成内容；
3. 材料（briefdoc）的 Word 与 Markdown 两个出口都带；
4. 文书（wenshu）的 Word 与 Markdown 两个出口都带 —— 它的「缘由段」是模型写的，
   这份稿子要送签，标识就是那句「哪一部分不是人写的」；
5. 会议通知不带：``notice.build`` 是纯函数拼装，不碰模型；
6. 离线快照 html 带 meta 与页脚；
7. 三种出口说的是同一句话（措辞不一致会让人以为其中一份没标）；
8. 界面每条回答都挂标识 —— 包括没有用量的那条（规则兜底、纯检索），
   那恰恰是最容易被漏掉的一类。

失败即非零退出。不写数据目录，纯内存。
"""

from __future__ import annotations

import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "meeting"))

import briefdoc  # noqa: E402
import docxgen as dg  # noqa: E402
import notice  # noqa: E402
import wenshu  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok  {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def parts(blob: bytes) -> tuple[str, str]:
    z = zipfile.ZipFile(io.BytesIO(blob))
    return (z.read("word/document.xml").decode("utf-8"),
            z.read("docProps/core.xml").decode("utf-8"))


def t_docx() -> None:
    print("[1] docx 两条标识")
    d = dg.Doc(title="关于 X 的汇报", author="某市人民政府办公室")
    d.para("正文一段。")
    body, core = parts(d.build())
    check("正文里有显式标识", dg.AIGC_TEXT in body)
    check("元数据里有 description", dg.AIGC_TEXT in core)
    check("元数据里有 keywords=AIGC", f"<cp:keywords>{dg.AIGC_KEYWORD}</cp:keywords>" in core)
    check("元数据里有 category", "人工智能生成内容" in core)
    check("core.xml 是合法 XML", isinstance(ET.fromstring(core), ET.Element))
    # 子元素顺序按 CT_CoreProperties：顺序错了 Word 不报错但会丢字段，
    # 这类问题只有机器能提前发现
    order = [t for t in re.findall(r"<(?:cp|dc|dcterms):(\w+)", core) if t != "coreProperties"]
    want = ["category", "created", "creator", "description", "keywords",
            "lastModifiedBy", "modified", "title"]
    check("core.xml 子元素按 schema 顺序", order == want, " → ".join(order))
    # 同一份 Doc 导出两次（预览一次、下载一次）不该叠出两行提示 —— build()
    # 往正文里补标识，不记状态的话第二次就多一行。
    again, _ = parts(d.build())
    check("重复 build 不叠标识", again.count(dg.AIGC_TEXT) == 1, str(again.count(dg.AIGC_TEXT)))


def t_optout() -> None:
    print("[2] aigc=False 反向：不该标的别标")
    d = dg.Doc(title="通报", author="某市政府办", aigc=False)
    d.para("用户填的事实。")
    body, core = parts(d.build())
    check("正文里没有标识", dg.AIGC_TEXT not in body)
    check("元数据里没有 AIGC", "AIGC" not in core and "人工智能" not in core)

    blob = briefdoc.bulletin_docx({"task": "重点项目建设", "unit": "市发改委",
                                   "owner": "某主任", "org": "某市政府办"})
    body, core = parts(blob)
    check("红头督办通报不标（无 AI 内容）", dg.AIGC_TEXT not in body and "AIGC" not in core)


def t_brief() -> None:
    print("[3] 材料：Word 与 Markdown")
    meta = {"title": "月度工作汇报", "city": "某市", "scope_text": "全市",
            "period_label": "2026年9月", "generated_at": "2026-11-17 09:00",
            "sources": []}
    body, core = parts(briefdoc.to_docx(meta, []))
    check("Word 带显式标识", dg.AIGC_TEXT in body)
    check("Word 带隐式标识", "AIGC" in core)
    md = briefdoc._markdown(meta, [])
    check("Markdown 带标识", dg.AIGC_TEXT in md)


def t_wenshu() -> None:
    print("[4] 文书：Word 与 Markdown")
    meta = {"generated_at": "2026-11-17 09:00", "mock": False}
    doc = {"title": "关于申请专项经费的请示", "org": "某市人民政府办公室",
           "no": "某政〔2026〕__号", "to": "市人民政府",
           "paras": [{"text": "为推进有关工作，现请示如下。"}],
           "tail": "以上请示，请予批准。", "sign": "某市人民政府办公室",
           "date": "2026年11月17日", "facts": []}
    body, core = parts(wenshu.to_docx(meta, doc))
    check("Word 带显式标识", dg.AIGC_TEXT in body)
    check("Word 带隐式标识", "AIGC" in core)
    md = wenshu.markdown(meta, doc)
    check("Markdown 带标识", dg.AIGC_TEXT in md)


def t_notice() -> None:
    print("[5] 会议通知：不碰模型，不标")
    got = notice.build({"title": "全市经济运行调度会", "held_at": "2026-11-20 09:00",
                        "place": "市行政中心 3 楼会议室", "host": "某副市长",
                        "attendees": ["市发改委", "市统计局"], "topics": ["通报1—10月运行情况"]})
    check("通知正文不含 AI 标识", dg.AIGC_TEXT not in got["text"])
    check("通知仍然成文", "会议时间" in got["text"] and "有关要求" in got["text"])


def t_offline() -> None:
    print("[6] 离线快照 html")
    # 导脚本会 import app/main.py（它自己要把 core 塞进 sys.path）。先把数据目录
    # 指到测试目录，否则一次测试就把真实 store 目录建出来。
    import os
    os.environ.setdefault("OCGOV_DATA", str(ROOT / "data" / "_test_aigc"))
    sys.path.insert(0, str(ROOT / "tools"))
    import export_offline
    page = export_offline.build_html({}, {"generated": "2026-11-17 09:00",
                                         "period": "2026年9月", "city": "某市",
                                         "disclosed_on": "", "person": {"name": "市长"},
                                         "threads": False, "endpoints": 0})
    check("有隐式标识 meta", '<meta name="AIGC" content="1">' in page)
    check("有提供者 meta", 'name="AIGC-Provider"' in page)
    check("有显式标识页脚", export_offline.AIGC_TEXT in page)
    check("标识插在 charset 之后、正文脚本之前",
          page.index('name="AIGC"') > page.index("<meta charset"))


def t_one_sentence() -> None:
    print("[7] 三种出口同一句话")
    texts = {dg.AIGC_TEXT, export_offline_text(), front_text()}
    check("docx / html / 前端措辞一致", len(texts) == 1, " | ".join(sorted(texts)))


def export_offline_text() -> str:
    import export_offline
    return export_offline.AIGC_TEXT


def front_text() -> str:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'const AIGC_TEXT = "([^"]+)"', html)
    return m.group(1) if m else ""


def t_ui() -> None:
    print("[8] 界面")
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("导出的 Markdown 抬头有标识", "> **${AIGC_TEXT}**" in html)
    check("标识无条件挂在用量行（不是有内容才显示）",
          "${AIGC_PILL}" in html and "const AIGC_PILL" in html)
    # 用量行原来的条件是「有调用/有上下文/有附件才渲染」——那种写法会让
    # 规则兜底的回答一条标识都没有，而这恰恰是最需要标的一类
    check("用量行不再因空而整块消失", "const usageBlock = `<div class=\"ai-usage\">" in html)


def main_() -> int:
    for fn in (t_docx, t_optout, t_brief, t_wenshu, t_notice, t_offline,
               t_one_sentence, t_ui):
        fn()
    print()
    if FAILS:
        print(f"有失败：{len(FAILS)} 项 → " + "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
