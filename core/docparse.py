"""把 Office 文件读成纯文本，只用标准库。

为什么不用 openpyxl / python-docx
    「零第三方依赖、标准库即可跑」不是洁癖，是这个项目能在内网机器上直接
    `python run.cmd` 的前提。为读一份月报引入一棵依赖树，等于把部署条件从
    「有 Python」改成「能 pip install」—— 内网往往不能。

为什么自己写不算难
    xlsx / docx 都是 zip 包里的 XML，结构由 ECMA-376 定死。
    xlsx 要的信息只有三个部件：workbook（有哪些表）、sharedStrings（字符串池）、
    worksheet（单元格）。真正难的不是解压，是**「存的值」和「看到的值」不是一回事**
    —— 见 _render_number 那段。
    docx 要的是 word/document.xml 一棵树，难点在**哪些节点算正文**：
    修订删除的旧字、隐藏文字、域代码都不能读进来 —— 见 _docx_blocks 那段。

不支持就明说
    .xls / .doc（OLE2 二进制，和 xlsx / docx 毫无关系）、加了密码的 Office 文件
    （也是 OLE2 容器，只是扩展名还叫 xlsx / docx）、PDF（要解析库，而且排版格式
    取出的数字经常错行）。这些都返回明确的错误码，不猜、不硬读。
"""

from __future__ import annotations

import datetime as _dt
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_RELNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKGREL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# 解压限额。xlsx 是 zip，公开接口收外部文件，不设限就等于收 zip 炸弹。
_MAX_ENTRY = 24 * 1024 * 1024
_MAX_TOTAL = 64 * 1024 * 1024
_MAX_ENTRIES = 2000

# ECMA-376 的内置数字格式。自定义格式在 styles.xml 的 numFmts 里，会覆盖这里。
_BUILTIN = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00",
    9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?", 13: "# ??/??",
    14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy",
    18: "h:mm AM/PM", 19: "h:mm:ss AM/PM", 20: "h:mm", 21: "h:mm:ss",
    22: "m/d/yy h:mm", 37: "#,##0 ;(#,##0)", 38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)", 40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 48: "##0.0E+0", 49: "@",
}


def _qn(tag: str) -> str:
    return _MAIN + tag


def _err(code: str, msg: str) -> dict:
    return {"ok": False, "code": code, "error": msg}


# ---------------------------------------------------------------- 显示值渲染


def _plain_fmt(fmt: str) -> str:
    """去掉格式串里的装饰：引号内的字面量、[] 里的颜色/条件/区域、反斜杠转义。

    `0.00"%"` 去掉引号段后是 `0.00`，这样它就不会被误判成百分比格式 ——
    这也是为什么要先剥装饰再判断类型。
    """
    s = re.sub(r'"[^"]*"', "", fmt)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"\\.", "", s)
    return s


def _is_date_fmt(fmt: str) -> bool:
    s = _plain_fmt(fmt).lower()
    if not re.search(r"[ymdhs]", s):
        return False
    # 带数字占位符的是数值格式（`0.00`、`#,##0`），不是日期
    return not re.search(r"[#0]", s)


def _serial_to_dt(v: float, date1904: bool) -> _dt.datetime | None:
    """Excel 的日期是「从某天起的第几天」，不是日期本身。

    1900 系统里微软故意保留了 1900-02-29（这天不存在）来兼容 Lotus，所以
    序列号 ≥ 60 的要整体减一天。少这一步，2026 年的日期会全部差一天。
    """
    if v < 0 or v > 2958465:          # 9999-12-31 之后的不认
        return None
    if date1904:
        base = _dt.datetime(1904, 1, 1)
    else:
        base = _dt.datetime(1899, 12, 31) if v < 60 else _dt.datetime(1899, 12, 30)
    return base + _dt.timedelta(days=v)


def _fmt_date(v: float, s: str, date1904: bool) -> str | None:
    dt = _serial_to_dt(v, date1904)
    if dt is None:
        return None
    has_date = bool(re.search(r"[yd]", s))
    has_time = bool(re.search(r"[hs]", s))
    if has_date and has_time:
        return dt.strftime("%Y-%m-%d %H:%M")
    if has_date:
        return dt.strftime("%Y-%m-%d")
    if has_time:
        return dt.strftime("%H:%M:%S") if "s" in s else dt.strftime("%H:%M")
    return dt.strftime("%Y-%m")


def _fmt_general(v: float) -> str:
    if v != v or v in (float("inf"), float("-inf")):
        return str(v)
    if v == int(v) and abs(v) < 1e16:
        return str(int(v))
    return f"{v:.15g}"            # 顺手把 0.30000000000000004 收成 0.3


def _render_number(raw: str, fmt: str | None, date1904: bool) -> str:
    """把单元格的**存储值**渲染成**人在 Excel 里看到的字符串**。

    这是整个解析器最关键的一段，也是不做这一步就会埋雷的地方：

        xlsx 里 `7.2%` 存的是 `0.072` + 一个「0.00%」的数字格式；
        `2026-09-30` 存的是序列号 `45292`。

    直接读 `<v>` 喂给模型，模型看到的是「45292」；更要命的是材料对照那条
    原文核验闸是按 token 精确匹配的 —— 材料里写 `7.2`，我们读出 `0.072`，
    闸直接判「材料里没有这个数」，对照块静默消失，而且没人知道为什么。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return raw or ""
    if not fmt or fmt.lower() == "general":
        return _fmt_general(v)

    s = _plain_fmt(fmt)
    if _is_date_fmt(fmt):
        out = _fmt_date(v, s.lower(), date1904)
        return out if out is not None else _fmt_general(v)

    upper = s.upper()
    if "E+" in upper or "E-" in upper:
        m = re.search(r"\.([0#]+)", s)
        dec = len(m.group(1)) if m else 2
        return f"{v:.{dec}E}"

    pct = "%" in s
    x = v * 100 if pct else v
    m = re.search(r"\.([0#]+)", s)
    dec = len(m.group(1)) if m else 0
    grp = "," in s
    body = f"{x:,.{dec}f}" if grp else f"{x:.{dec}f}"

    cur = re.search(r"[¥￥$€£]", fmt)
    if cur:
        body = cur.group(0) + body
    if pct:
        body += "%"
    return body


# ---------------------------------------------------------------- 部件读取


def _guard(zf: zipfile.ZipFile) -> dict | None:
    infos = zf.infolist()
    if len(infos) > _MAX_ENTRIES:
        return _err("too_big", f"压缩包条目过多（{len(infos)} 个），拒绝解析")
    total = 0
    for i in infos:
        if i.file_size > _MAX_ENTRY:
            return _err("too_big", f"内部成员过大（{i.filename}），拒绝解析")
        total += i.file_size
        if total > _MAX_TOTAL:
            return _err("too_big", "解压后体积超过上限，拒绝解析")
    return None


def _si_text(si: ET.Element) -> str:
    """一个 sharedStrings 条目可能被拆成多个 run（改过字体颜色就会拆）。

    只取直接子 `<t>` 和 `<r>` 里的 `<t>`：`<rPh>` 是日文注音，混进来会变成
    单元格里凭空多出来的假名。
    """
    parts: list[str] = []
    for ch in si:
        if ch.tag == _qn("t"):
            parts.append(ch.text or "")
        elif ch.tag == _qn("r"):
            for t in ch.iter(_qn("t")):
                parts.append(t.text or "")
    return "".join(parts)


def _shared_strings(zf: zipfile.ZipFile, names: set[str]) -> list[str]:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        return []
    return [_si_text(si) for si in root.iter(_qn("si"))]


def _cell_formats(zf: zipfile.ZipFile, names: set[str]) -> list[str | None]:
    """cellXfs 的下标 → 格式串。单元格的 `s` 属性就是这个下标。"""
    if "xl/styles.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except ET.ParseError:
        return []
    custom: dict[int, str] = {}
    for nf in root.iter(_qn("numFmt")):
        try:
            custom[int(nf.get("numFmtId") or 0)] = nf.get("formatCode") or ""
        except (TypeError, ValueError):
            continue
    out: list[str | None] = []
    xfs = root.find(_qn("cellXfs"))
    if xfs is None:
        return out
    for xf in xfs.findall(_qn("xf")):
        try:
            nid = int(xf.get("numFmtId") or 0)
        except (TypeError, ValueError):
            nid = 0
        out.append(custom.get(nid, _BUILTIN.get(nid)))
    return out


def _col_index(ref: str) -> int:
    """`AB12` → 28。"""
    m = re.match(r"([A-Z]+)", (ref or "").upper())
    if not m:
        return 1
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n


def _cell_text(
    c: ET.Element, sst: list[str], xfs: list[str | None], date1904: bool
) -> str:
    t = c.get("t")
    v = c.find(_qn("v"))

    if t == "s":
        try:
            return sst[int(v.text)] if v is not None and v.text else ""
        except (ValueError, IndexError, TypeError):
            return ""
    if t == "inlineStr":
        node = c.find(_qn("is"))
        return _si_text(node) if node is not None else ""
    if t == "str":
        return (v.text or "") if v is not None else ""
    if t == "b":
        return "TRUE" if (v is not None and (v.text or "").strip() == "1") else "FALSE"
    if t == "e":
        return (v.text or "") if v is not None else "#ERR"
    if v is None or v.text is None:
        return ""                     # 公式格没有缓存值（文件没在 Excel 里算过）

    fmt = None
    try:
        idx = int(c.get("s") or 0)
        if 0 <= idx < len(xfs):
            fmt = xfs[idx]
    except (TypeError, ValueError):
        pass
    return _render_number(v.text, fmt, date1904)


def _sheet_paths(zf: zipfile.ZipFile, names: set[str]) -> tuple[list[tuple[str, str]], bool]:
    """从 workbook.xml + rels 拿到 [(表名, 包内路径)]，并读 date1904。"""
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
    except ET.ParseError:
        return [], False

    date1904 = False
    pr = wb.find(_qn("workbookPr"))
    if pr is not None and (pr.get("date1904") or "").strip().lower() in ("1", "true"):
        date1904 = True

    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        try:
            root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for r in root:
                if r.tag != _PKGREL + "Relationship":
                    continue
                tgt = r.get("Target") or ""
                if not tgt:
                    continue
                path = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath("xl/" + tgt)
                rels[r.get("Id") or ""] = path
        except ET.ParseError:
            pass

    out: list[tuple[str, str]] = []
    for i, s in enumerate(wb.iter(_qn("sheet")), start=1):
        name = (s.get("name") or f"Sheet{i}").strip() or f"Sheet{i}"
        rid = s.get(_RELNS + "id") or ""
        path = rels.get(rid) or f"xl/worksheets/sheet{i}.xml"
        out.append((name, path))
    return out, date1904


def _sheet_rows(
    zf: zipfile.ZipFile, path: str, sst: list[str], xfs: list[str | None], date1904: bool
) -> tuple[list[dict[int, str]], int, list[str], int] | None:
    """读一张表 → (行, 最大列号, 合并区, 无缓存值的公式格数)。读不了返回 None。"""
    try:
        root = ET.fromstring(zf.read(path))
    except (KeyError, ET.ParseError):
        return None

    rows: list[dict[int, str]] = []
    maxcol = 0
    blank_formula = 0
    sd = root.find(_qn("sheetData"))
    if sd is None:
        return rows, 0, [], 0

    for row in sd.findall(_qn("row")):
        cells: dict[int, str] = {}
        auto = 1
        for c in row.findall(_qn("c")):
            ref = c.get("r") or ""
            col = _col_index(ref) if ref else auto
            auto = col + 1
            txt = _cell_text(c, sst, xfs, date1904)
            if not txt and c.find(_qn("f")) is not None:
                # 公式格但包里没有 <v>：文件是用程序写的、没在 Excel 里算过。
                # 这类格读出来就是空，必须说出来，否则用户以为这一行本来就是空的。
                blank_formula += 1
            # 单元格里可以有 Alt+Enter 换行、也可以有制表符（粘贴进来的数据常有）。
            # 不清掉的话，TSV 的行列会在这一格断开 —— 后面所有行整体错位。
            txt = re.sub(r"[\t\r\n\v\f]+", " ", txt).strip()
            if txt:
                cells[col] = txt
        if cells:
            maxcol = max(maxcol, max(cells))
            rows.append(cells)
        elif rows and rows[-1]:
            # 空行保留一行：报表里空行常是分节线，全删掉会让两段数据挤在一起，
            # 模型会以为它们是连着的。连续多个空行折成一个，省额度。
            rows.append({})

    merges = []
    mc = root.find(_qn("mergeCells"))
    if mc is not None:
        for m in mc.findall(_qn("mergeCell")):
            ref = (m.get("ref") or "").strip()
            if ref:
                merges.append(ref)
    return rows, maxcol, merges, blank_formula


def _parse_xlsx(zf: zipfile.ZipFile, names: set[str], limit: int) -> dict:
    sheets, date1904 = _sheet_paths(zf, names)
    if not sheets:
        return _err("no_sheet", "工作簿里没有工作表")
    sst = _shared_strings(zf, names)
    xfs = _cell_formats(zf, names)

    blocks: list[str] = []
    meta: list[dict] = []
    used = 0
    truncated = False
    total_rows = 0
    merge_hits: list[str] = []
    blank_formula = 0

    for name, path in sheets:
        got = _sheet_rows(zf, path, sst, xfs, date1904)
        if got is None:
            continue
        rows, maxcol, merges, blanks = got
        blank_formula += blanks
        if not rows:
            meta.append({"name": name, "rows": 0, "cols": 0})
            continue
        if merges:
            merge_hits.append(f"{name}（{len(merges)} 处）")
        total_rows += len(rows)
        body: list[str] = []
        for r in rows:
            if not r:
                body.append("")
                continue
            last = max(r)
            body.append("\t".join(r.get(i, "") for i in range(1, last + 1)))
        chunk = f"## {name}\n" + "\n".join(body)
        if used + len(chunk) > limit:
            room = limit - used
            if room > 40:
                blocks.append(chunk[:room] + "\n…（已截断）")
                used = limit
            truncated = True
            meta.append({"name": name, "rows": len(rows), "cols": maxcol})
            break
        blocks.append(chunk)
        used += len(chunk) + 1
        meta.append({"name": name, "rows": len(rows), "cols": maxcol})

    text = "\n".join(blocks)
    notes: list[str] = []
    if truncated:
        notes.append("内容超过额度，后面的表和行没有读入")
    if merge_hits:
        notes.append("有合并单元格（" + "、".join(merge_hits) + "），合并区只保留左上角的值")
    if blank_formula:
        notes.append(f"{blank_formula} 个公式格没有计算结果")
    return {
        "ok": True,
        "kind": "xlsx",
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "sheets": meta,
        "rows": total_rows,
        "note": "；".join(notes),
    }


# ---------------------------------------------------------------- Word（docx）
#
# docx 比 xlsx 简单的地方：文本就是文本，没有「存 0.072 显示 7.2%」这种陷阱。
# docx 比 xlsx 难的地方：**哪些节点算正文**。一棵 document.xml 里同时住着
#
#   · 修订删除的旧字（w:del 里的 w:delText）—— 读进来会把「删掉的旧数字」当正文
#   · 隐藏文字（w:vanish）—— 有人会把敏感数或草稿段设成隐藏，打印时看不见
#   · 域代码（w:instrText）—— 是 PAGE / MERGEFORMAT 这类指令，不是给人看的字
#   · 表格的合并单元格（gridSpan / vMerge）—— 不补空位，整张表会错列
#
# 这几条都不会报错，只会让读出来的东西「看起来对、其实不是原文」。


def _w(tag: str) -> str:
    return _WORD + tag


# 装内容的容器节点：段落/表格可能被包在内容控件(sdt)、自定义 XML 里
_WRAP = ("sdtContent", "customXml", "smartTag", "ins", "moveTo")


def _is_on(el: ET.Element) -> bool:
    """OOXML 的开关属性：不写 val 就是开，`0/false/off` 才是关。"""
    return (el.get(_w("val")) or "1").strip().lower() not in ("0", "false", "off")


def _docx_styles(zf: zipfile.ZipFile, names: set[str]) -> dict[str, int]:
    """styleId → 标题级别（0 是最高级）。只收「能确定是标题」的样式。

    标题级别记在 styles.xml 的 `w:outlineLvl` 里，不在段落上 —— 只读段落的话
    标题和正文长得一模一样，一份红头文件的层级就整个丢了。中文模板的样式名
    有叫「标题 1」的，也有 styleId 直接是 "Heading1" 的，两种都认。
    """
    if "word/styles.xml" not in names:
        return {}
    try:
        root = ET.fromstring(zf.read("word/styles.xml"))
    except ET.ParseError:
        return {}

    out: dict[str, int] = {}
    for st in root.iter(_w("style")):
        sid = st.get(_w("styleId")) or ""
        if not sid:
            continue
        lvl: int | None = None
        ppr = st.find(_w("pPr"))
        if ppr is not None:
            ol = ppr.find(_w("outlineLvl"))
            if ol is not None:
                try:
                    lvl = int(ol.get(_w("val")) or "")
                except (TypeError, ValueError):
                    lvl = None
        if lvl is None:
            nm = st.find(_w("name"))
            label = (nm.get(_w("val")) if nm is not None else "") or ""
            m = re.match(r"^(?:heading|标题)\s*([1-9])$", label.strip(), re.I) or re.match(
                r"^heading\s*([1-9])$", sid, re.I
            )
            if m:
                lvl = int(m.group(1)) - 1
        if lvl is not None and 0 <= lvl <= 8:
            out[sid] = lvl
    return out


def _outline_of(p: ET.Element, styles: dict[str, int]) -> int | None:
    ppr = p.find(_w("pPr"))
    if ppr is not None:
        ol = ppr.find(_w("outlineLvl"))   # 段落上直接写了级别的优先
        if ol is not None:
            try:
                return int(ol.get(_w("val")) or "")
            except (TypeError, ValueError):
                pass
        ps = ppr.find(_w("pStyle"))
        if ps is not None:
            return styles.get(ps.get(_w("val")) or "")
    return None


def _run_hidden(r: ET.Element) -> bool:
    rpr = r.find(_w("rPr"))
    if rpr is None:
        return False
    v = rpr.find(_w("vanish"))
    return v is not None and _is_on(v)


def _para_hidden(p: ET.Element) -> bool:
    ppr = p.find(_w("pPr"))
    if ppr is not None:
        rpr = ppr.find(_w("rPr"))
        if rpr is not None:
            v = rpr.find(_w("vanish"))
            if v is not None and _is_on(v):
                return True
        ps = ppr.find(_w("pStyle"))
        if ps is not None and (ps.get(_w("val")) or "").lower() in ("hidden", "隐藏"):
            return True
    return False


def _docx_walk(node: ET.Element, parts: list[str], st: dict) -> None:
    for ch in node:
        tag = ch.tag
        if tag in (_w("del"), _w("moveFrom")):
            # 修订里「删掉的」旧内容。它还在文件里（为了能接受/拒绝修订），
            # 但打印出来看不见 —— 读进来就是拿旧数字当新数字。
            continue
        if tag == _w("t"):
            parts.append(ch.text or "")
        elif tag == _w("tab"):
            parts.append("\t")
        elif tag in (_w("br"), _w("cr")):
            parts.append("\n")
        elif tag == _w("r"):
            if _run_hidden(ch):
                st["hidden"] += 1
                continue
            _docx_walk(ch, parts, st)
        else:
            _docx_walk(ch, parts, st)


def _para_text(p: ET.Element, st: dict) -> str:
    parts: list[str] = []
    _docx_walk(p, parts, st)
    t = "".join(parts).replace("\r", "").replace("\v", " ")
    # 段内制表符是缩进，硬换行（Shift+Enter）是折行，都不是新段落。
    # 统一折成空格：留着 \t 的话，材料进 TSV 那条路会多出一个假列。
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    return t.replace("\n", " ").strip()


def _docx_cell_text(tc: ET.Element, styles: dict[str, int], st: dict) -> str:
    parts: list[str] = []
    for ch in tc:
        if ch.tag == _w("p"):
            parts.append(_para_text(ch, st))
        elif ch.tag == _w("tbl"):
            # 表格套表格：内层每一行折成一行，否则外层这一格会把表撑成好几行
            nested = _table_rows(ch, styles, st)
            parts.append(" / ".join(" ".join(c for c in r if c) for r in nested))
        elif ch.tag == _w("sdt"):
            box = ch.find(_w("sdtContent"))
            if box is not None:
                parts.append(_docx_cell_text(box, styles, st))
        elif ch.tag in (_w("sdtContent"), _w("customXml")):
            parts.append(_docx_cell_text(ch, styles, st))
    t = " ".join(x for x in parts if x)
    return re.sub(r"[\t\r\n]+", " ", t).strip()


def _table_rows(tbl: ET.Element, styles: dict[str, int], st: dict) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in tbl.findall(_w("tr")):
        row: list[str] = []
        for tc in tr.findall(_w("tc")):
            tpr = tc.find(_w("tcPr"))
            span, cont = 1, False
            if tpr is not None:
                gs = tpr.find(_w("gridSpan"))
                if gs is not None:
                    try:
                        span = max(1, min(64, int(gs.get(_w("val")) or 1)))
                    except (TypeError, ValueError):
                        span = 1
                vm = tpr.find(_w("vMerge"))
                # 纵向合并：续格的 val 是空（或 continue）。补空串占位，
                # 不补的话续行会整体左移一列 —— 政务报表里最常见的就是这种表头。
                cont = vm is not None and (vm.get(_w("val")) or "continue") != "restart"
            row.append("" if cont else _docx_cell_text(tc, styles, st))
            row.extend([""] * (span - 1))
        rows.append(row)
        st["table_rows"] += 1
    return rows


def _docx_blocks(container: ET.Element, styles: dict[str, int], lines: list[str], st: dict) -> None:
    for ch in container:
        tag = ch.tag
        if tag == _w("p"):
            if _para_hidden(ch):
                st["hidden"] += 1
                continue
            txt = _para_text(ch, st)
            if not txt:
                # 空段落多是排版留白。连续多个折成一个，省额度但不丢分段。
                if lines and lines[-1] != "":
                    lines.append("")
                continue
            lvl = _outline_of(ch, styles)
            lines.append(("#" * min(6, lvl + 1) + " " + txt) if lvl is not None else txt)
            st["paras"] += 1
        elif tag == _w("tbl"):
            st["tables"] += 1
            rows = _table_rows(ch, styles, st)
            if rows:
                if lines and lines[-1] != "":
                    lines.append("")
                for r in rows:
                    lines.append("\t".join(r).rstrip("\t"))
                lines.append("")
        elif tag == _w("sdt"):
            box = ch.find(_w("sdtContent"))
            if box is not None:
                _docx_blocks(box, styles, lines, st)
        elif tag != _w("sectPr") and ch.tag.rsplit("}", 1)[-1] in _WRAP:
            _docx_blocks(ch, styles, lines, st)


def _parse_docx(zf: zipfile.ZipFile, names: set[str], limit: int) -> dict:
    try:
        root = ET.fromstring(zf.read("word/document.xml"))
    except (KeyError, ET.ParseError):
        return _err("bad_docx", "word/document.xml 读不出来，文件可能损坏")

    body = root.find(_w("body"))
    if body is None:
        return _err("bad_docx", "文档里没有正文（word/document.xml 缺 body）")

    st = {"paras": 0, "tables": 0, "table_rows": 0, "hidden": 0}
    lines: list[str] = []
    _docx_blocks(body, _docx_styles(zf, names), lines, st)

    images = sum(1 for _ in body.iter(_w("drawing"))) + sum(1 for _ in body.iter(_w("pict")))
    text = "\n".join(lines).strip("\n")
    truncated = False
    if len(text) > limit:
        text = text[:limit].rstrip() + "\n…（已截断）"
        truncated = True

    if not text.strip():
        # 空文档要分成两种情况说：整份都是图片（扫描件）、真的没写东西。
        # 混成一句「没有文字」，用户不知道该去截图还是该去改文件。
        if images:
            return _err(
                "no_text",
                f"这份文档里没有文字，只有 {images} 张图片 —— 图片要 OCR，本机没有",
            )
        return _err("no_text", "这份文档里没有文字")

    notes: list[str] = []
    if truncated:
        notes.append("内容超过额度，后面的段落没有读入")
    if st["tables"]:
        notes.append(f"含 {st['tables']} 个表格，按制表符一行一行列出")
    if images:
        notes.append(f"{images} 张图片没有读入（没有 OCR）")
    if st["hidden"]:
        notes.append(f"跳过 {st['hidden']} 处隐藏文字（Word 里看不见的不当正文）")

    return {
        "ok": True,
        "kind": "docx",
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "paras": st["paras"],
        "tables": st["tables"],
        "rows": st["table_rows"],
        "images": images,
        "note": "；".join(notes),
    }


def _parse_zip(data: bytes, limit: int) -> dict:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return _err("bad_zip", "文件损坏，或者不是 Office 文件（zip 打不开）")

    with zf:
        bad = _guard(zf)
        if bad:
            return bad
        names = set(zf.namelist())
        # 不看扩展名，看包里有没有那个部件 —— 有人把 .docx 改名成 .zip 发过来，
        # 也有人把 .xlsx 直接改后缀叫 .docx。扩展名是猜的，包结构不是。
        if "xl/workbook.xml" in names:
            return _parse_xlsx(zf, names, limit)
        if "word/document.xml" in names:
            return _parse_docx(zf, names, limit)
        if "ppt/presentation.xml" in names:
            return _err("pptx", "PPT 还没接（先把要用的几页复制到 Word 或存成 PDF 截图）")
        return _err("not_office", "这是个 zip 包，但里面没有 Excel / Word 的正文部件")


def parse_book(data: bytes, name: str = "", limit: int = 12000) -> dict:
    """解析入口。按文件头分派，认不出就明说，不猜格式。"""
    if not data:
        return _err("empty", "文件是空的")
    head = data[:8]
    if head[:2] == b"PK":
        return _parse_zip(data, limit)
    if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        # OLE2 是老 Office 的容器，也是「加了密码的新版 Office」的容器。
        # 两种都读不了，但出路不同，按扩展名把话说准。
        low = (name or "").lower()
        if low.endswith(".doc"):
            return _err(
                "ole2",
                "这是旧版 Word（.doc）或者加了密码的文件，都不支持 —— "
                "用 Word / WPS 另存为 .docx 并去掉密码再来",
            )
        return _err(
            "ole2",
            "这是 .xls（旧版 Excel）或者加了密码的文件，都不支持 —— "
            "用 Excel / WPS 另存为 .xlsx 并去掉密码再来",
        )
    if head[:4] == b"%PDF":
        return _err("pdf", "PDF 还没接（要解析库，且排版格式取出的表格容易错行）")
    return _err("unknown", "认不出这个格式（文件头不是 zip，也不是 Office / PDF）")
