"""外部数据导入：把用户上传的表格，对到指标库上做同期对照。

**它解决什么问题**：领导手里常有一份自己的表 —— 统计局发来的月报、部门报的
进度表、从系统里导出的 csv。以前这份表只能当「参考材料」读，模型看了也不许
用它的数字，用户想问「你系统里的数和这份表对得上吗」只能自己一格一格比。

**它不做什么**（这是边界，不是没做完）：
1. 不把外部数字写进结论正文。headline / facts / 专家解读里的数**只能**来自
   指标库 —— 外部表格没有经过核验，一旦混进正文，用户分不清哪个可信。
   对照区是并列展示，两边各自带出处，判断权在用户。
2. 不做单位换算、不做小数位补全、不做插值。表里写 6.8 就是 6.8；认不出单位
   的列直接不比对，而不是猜一个。
3. 不猜期次。只认写明年月的（2026-08 / 2026年8月 / 2026/8 / 202608）。
   只有「8月」没有年份的列，宁可不比 —— 错一年比不对比更糟。

**为什么放在 metrics 这一层**：它要读指标库（mockdata）的序列和口径，也要给
问数结果用。放进 core/knowledge.py 会让知识库反向依赖指标库，那条 import 边
是错的。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import mockdata as md

# 一张表最多读多少个数据点。政务月报常见的规模是 12~24 期 × 几列，
# 上限给 400 足够；再大说明用户传的是原始明细，那不是「对照」而是「建模」，
# 这一层不做，也不该假装能做。
_MAX_POINTS = 400
# 每个指标在对照区里最多列几期。列 24 期会把气泡撑成一张报表，
# 而用户要的是「对得上吗」，不是通读全年。
_MAX_ROWS = 8

# 表头里能剥掉的单位，用来判断「这一列和指标口径是不是一个单位」。
# 只认三种最常见的：% / 亿元 / 万元。其余（元、户、项、个、人次）不剥，
# 让它们按「不带单位」处理 —— 剥错比不剥更危险。
_UNIT_PAT = re.compile(r"[（(]\s*(%|％|亿元|万元|个|户|项|人次)\s*[）)]")
# 表头里的数字：多数是编号（「12345 办结率」）或版本号，匹配指标名时先摘掉
_DIGITS = re.compile(r"[\d.]+")


def _cells(line: str) -> list[str]:
    """切一行表格。

    顺序有讲究：制表符优先。政务系统导出的「csv」里常有单元格本身带逗号
    （「1,234」），先按逗号切会把一个数切成两半，而 tab 分隔的表不会。
    """
    raw = line.rstrip("\n")
    if "\t" in raw:
        parts = raw.split("\t")
    elif "," in raw:
        parts = raw.split(",")
    elif "，" in raw:
        parts = raw.split("，")
    elif "|" in raw:
        parts = raw.strip().strip("|").split("|")
    else:
        parts = re.split(r"\s{2,}", raw.strip())
    return [p.strip().strip('"').strip("'").strip() for p in parts]


_PERIOD_PATTERNS = [
    re.compile(r"^(\d{4})\s*[-/.]\s*(\d{1,2})$"),
    re.compile(r"^(\d{4})\s*年\s*(\d{1,2})\s*月?$"),
    re.compile(r"^(\d{4})(\d{2})$"),
]


def parse_period(text: str) -> str | None:
    """把各种写法的月份读成 2026-08。读不出月份就返回 None。

    只认「年月」粒度。「2026年」「2026-Q3」「上半年」一律不认 —— 年度值和
    月度序列不是一回事，硬对上去会得出「12 月和全年差很多」这种假差异。
    """
    t = (text or "").strip().replace(" ", "")
    for pat in _PERIOD_PATTERNS:
        m = pat.match(t)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    return None


def _number(text: str) -> tuple[float, str] | None:
    """读一个数，返回 (数值, 原文写法)。`—` `-` `…` 之类返回 None。

    原文写法要留着：表里写「6.80%」，界面上就该显示 6.80%，而不是被我们
    规整成 6.8 —— 对照区的可信度有一半来自「原样」这两个字。
    """
    t = (text or "").strip()
    if not t or t in {"—", "-", "--", "…", "/", "N/A", "n/a", "NA"}:
        return None
    # 全角数字与全角百分号：从 PDF 复制出来的表很常见
    t = t.translate(str.maketrans("０１２３４５６７８９．％", "0123456789.%"))
    m = re.match(r"^[约≈]?\s*(-?\d[\d,]*(?:\.\d+)?)\s*(%|％)?", t)
    if not m:
        return None
    body = m.group(1).replace(",", "")
    try:
        val = float(body)
    except ValueError:
        return None
    written = body + ("%" if m.group(2) else "")
    return val, written


def _normalize_header(text: str) -> str:
    t = (text or "").strip().replace(" ", "").replace("\u3000", "")
    return _UNIT_PAT.sub("", t)


def _header_unit(text: str) -> str:
    m = _UNIT_PAT.search(text or "")
    if not m:
        return ""
    return "%" if m.group(1) in ("%", "％") else m.group(1)


def _metric_index() -> list[tuple[str, str]]:
    """(可匹配的名字, code)，长的排前面 —— 「工业增加值」要赢过「工业」。"""
    out: list[tuple[str, str]] = []
    for code, meta in md.METRICS.items():
        for name in [meta.get("name") or ""] + list(meta.get("aliases") or []):
            n = _normalize_header(name)
            if n:
                out.append((n, code))
    out.sort(key=lambda x: -len(x[0]))
    return out


_INDEX: list[tuple[str, str]] | None = None


def match_metric(header: str) -> str | None:
    """把一列表头认成指标 code。认不出就返回 None，不猜。

    三级，从严到宽：
    1. 精确相等（别名表就是干这个的）；
    2. 表头去掉数字后再精确相等 —— 「12345按期办结率」的别名是「按期办结率」，
       前面那个编号是单位名，不是指标名的一部分；
    3. 表头包含别名（别名覆盖表头六成以上），或表头是某个指标名的前缀
       （「规模以上工业增加值」之于「规模以上工业增加值增速」）。

    第 3 级的六成闸不能松：「规上工业总产值」包含别名「规上工业」，恰好五成，
    放行就会把产值当增加值去比 —— 两个不同的东西摆在一起说「差 3%」，
    比不显示糟得多。
    """
    global _INDEX
    if _INDEX is None:
        _INDEX = _metric_index()
    h = _normalize_header(header)
    if not h:
        return None
    for name, code in _INDEX:
        if h == name:
            return code
    hd = _DIGITS.sub("", h)
    if hd and hd != h:
        for name, code in _INDEX:
            if hd == name:
                return code
    best: tuple[int, str] | None = None
    for name, code in _INDEX:
        if len(name) < 4:
            continue
        if name in h and len(name) / len(h) >= 0.6:
            cand = len(name)
        elif len(h) >= 6 and h in name:
            cand = len(h)
        else:
            continue
        if best is None or cand > best[0]:
            best = (cand, code)
    return best[1] if best else None


def _unit_conflict(header: str, code: str) -> str:
    """表头写了单位、又和指标口径不一致时，返回一句原因；一致或没写返回空串。"""
    want = _header_unit(header)
    if not want:
        return ""
    have = md.METRICS[code].get("unit") or ""
    if have.replace("％", "%") != want.replace("％", "%"):
        return f"单位是「{want}」，指标口径是「{have}」"
    return ""


def parse_table(text: str, name: str = "") -> dict[str, Any]:
    """从一份文件里读出 (code, 期次, 值) 数据点。

    支持两种版式，靠表头判断，不靠文件名：
      A. 期次在行：`期次,规上工业增加值,按期办结率` + 每行一个月份
      B. 期次在列：`指标,2026-07,2026-08,2026-09` + 每行一个指标
    B 在统计月报里很常见（月份横着排），但代码里 A 更自然，两种都得认。
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    # 这份文件到底像不像表格。不像表格的（政策通知、纪要正文）不该在界面上
    # 报「没找到表头」—— 用户本来就没打算让它当数据表，报错只是噪音。
    tabular = any(
        len(_cells(ln)) >= 2 and any(ch in ln for ch in "\t,，|") for ln in lines[:40]
    )
    if not lines:
        return {
            "points": [],
            "unmatched": [],
            "skipped": [],
            "tabular": False,
            "note": "",
        }

    # 找表头：不能取「第一个能对上指标的行」—— 期次横排的月报里，第一行是
    # `指标,2026-07,2026-08`，第二行数据 `规上工业增加值,8.0,7.2` 也能对上指标。
    # 按「这一行有几格像指标名、几格像期次」打分取最高分，才不会把数据行当表头。
    head_i = -1
    head: list[str] = []
    best_score = 0
    for i, ln in enumerate(lines[:10]):
        cells = _cells(ln)
        if len(cells) < 2:
            continue
        score = sum(1 for c in cells if match_metric(c)) + sum(
            1 for c in cells[1:] if parse_period(c)
        )
        # 期次列本身不算数据，但它是表头的标志：「月份 / 期次 / 时间」这种列名
        # 出现在两列表里时，光靠「对上一个指标」只有 1 分，会被下面的阈值挡掉
        if any(re.search(r"期次|时间|月份|月度|统计期|指标|项目", c) for c in cells):
            score += 1
        if score > best_score:
            best_score, head_i, head = score, i, cells
    if head_i < 0 or best_score < 2:
        return {
            "points": [],
            "unmatched": [],
            "skipped": [],
            "tabular": tabular,
            "note": "没找到表头：表里没有一列能对上已注册指标" if tabular else "",
        }

    # 表头里期次占多数的列 → 期次横排（版式 B）
    period_cols = sum(1 for c in head[1:] if parse_period(c))
    metric_cols = sum(1 for c in head[1:] if match_metric(c))
    transposed = period_cols >= 1 and metric_cols == 0

    points: list[dict[str, Any]] = []
    unmatched: list[str] = []
    skipped: list[dict[str, str]] = []
    rows = lines[head_i + 1:]

    if transposed:
        periods = [parse_period(c) for c in head[1:]]
        for ln in rows:
            cells = _cells(ln)
            if len(cells) < 2:
                continue
            code = match_metric(cells[0])
            if not code:
                if cells[0] and not _number(cells[0]):
                    unmatched.append(cells[0])
                continue
            why = _unit_conflict(cells[0], code)
            if why:
                skipped.append({"column": cells[0], "why": why})
                continue
            for j, period in enumerate(periods, start=1):
                if not period or j >= len(cells):
                    continue
                got = _number(cells[j])
                if got is None:
                    continue
                points.append({"code": code, "period": period, "value": got[0], "written": got[1]})
    else:
        # 哪一列是期次：优先表头写了「期次/时间/月份」的那列，否则认第一列
        pcol = 0
        for j, c in enumerate(head):
            if re.search(r"期次|时间|月份|月度|统计期", c):
                pcol = j
                break
        cols: list[tuple[int, str]] = []
        for j, c in enumerate(head):
            if j == pcol or not c:
                continue
            code = match_metric(c)
            if not code:
                if not parse_period(c):
                    unmatched.append(c)
                continue
            why = _unit_conflict(c, code)
            if why:
                skipped.append({"column": c, "why": why})
                continue
            cols.append((j, code))

        for ln in rows:
            cells = _cells(ln)
            if len(cells) <= pcol:
                continue
            period = parse_period(cells[pcol])
            if not period:
                continue
            for j, code in cols:
                if j >= len(cells):
                    continue
                got = _number(cells[j])
                if got is None:
                    continue
                points.append({"code": code, "period": period, "value": got[0], "written": got[1]})

    if len(points) > _MAX_POINTS:
        points = points[:_MAX_POINTS]
    # 去重：同一个 code + 期次出现两次（合并单元格、重复表头）时以最后一行为准，
    # 因为月报里后出现的通常是「修正后」的数
    dedup: dict[tuple[str, str], dict[str, Any]] = {}
    for p in points:
        dedup[(p["code"], p["period"])] = p
    return {
        "points": list(dedup.values()),
        "unmatched": list(dict.fromkeys([u for u in unmatched if u]))[:6],
        "skipped": skipped[:6],
        "tabular": tabular,
        "note": "",
        "file": name,
    }


def _decimals(written: str) -> int:
    m = re.search(r"\.(\d+)", written or "")
    return len(m.group(1)) if m else 0


def _len(unit: str) -> str:
    return "个百分点" if unit in ("%", "％") else (unit or "")


def _verdict(rows: list[dict[str, Any]], unit: str, name: str) -> str:
    same = [r for r in rows if r["state"] == "same"]
    check = [r for r in rows if r["state"] == "check"]
    diff = [r for r in rows if r["state"] == "diff"]
    if not diff and not check:
        return f"{len(rows)} 期全部一致（{rows[0]['label']}–{rows[-1]['label']}）"
    if check and not diff:
        return f"{len(rows)} 期里有 {len(check)} 期量级差得太多，多半是当月值和累计值混用"
    if not same and not check:
        worst = max(diff, key=lambda r: abs(r["diff"]))
        return f"{len(rows)} 期都不一致，差异最大的是 {worst['period']}（{worst['diff']:+g}）"
    parts = [f"{len(rows)} 期里 {len(same)} 期一致"]
    if diff:
        worst = max(diff, key=lambda r: abs(r["diff"]))
        side = "高" if worst["diff"] > 0 else "低"
        parts.append(
            f"{worst['label']} 外部比系统{side} {abs(worst['diff']):g}{_len(unit)}"
        )
    if check:
        parts.append(f"{len(check)} 期量级差得太多，请确认是不是同一个口径")
    return "；".join(parts)


def analyze(attachments: list[dict[str, Any]] | None, today: date | None = None) -> dict[str, Any] | None:
    """把本轮附件里能读成表格的部分，与指标库序列逐期对照。

    没有任何可对照的数据时返回 None —— 调用方据此决定「不显示这一块」。
    宁可没有，也不显示一个「0 期可对照」的空壳。
    """
    if not attachments:
        return None

    by_code: dict[str, list[dict[str, Any]]] = {}
    files: list[str] = []
    unmatched: list[str] = []
    skipped: list[dict[str, str]] = []
    notes: list[str] = []
    tabular = False

    for a in attachments:
        got = parse_table(str(a.get("text") or ""), str(a.get("name") or ""))
        tabular = tabular or bool(got.get("tabular"))
        if got.get("note"):
            notes.append(f"{got.get('file') or '文件'}：{got['note']}")
        if got["points"]:
            files.append(got.get("file") or "")
        for p in got["points"]:
            by_code.setdefault(p["code"], []).append({**p, "file": got.get("file") or ""})
        for u in got["unmatched"]:
            if u not in unmatched:
                unmatched.append(u)
        skipped.extend(got["skipped"])

    if not by_code:
        why = []
        if tabular:
            why.append("附件里没有能和指标库对上的表格数据（需要「期次 + 数值」两列）。")
        if skipped:
            why.append(
                "有 %d 列因为单位或口径不一致没有参与对照：%s"
                % (len(skipped), "、".join(f"{s['column']}（{s['why']}）" for s in skipped[:3]))
            )
        if unmatched:
            why.append("表头里认不出的列：" + "、".join(unmatched[:4]))
        return {
            "ok": False,
            "files": list(dict.fromkeys(f for f in files if f)),
            "metrics": [],
            "unmatched": unmatched[:6],
            "skipped": skipped[:6],
            "tabular": tabular,
            "notes": notes[:2] + why,
        }

    metrics: list[dict[str, Any]] = []
    for code, pts in by_code.items():
        meta = md.METRICS[code]
        unit = meta.get("unit") or ""
        inner = md.query_metric(code, None, md.TOTAL_MONTHS, today)
        if not inner.get("ok"):
            continue
        series = {p["period"]: p for p in inner.get("series") or []}
        rows: list[dict[str, Any]] = []
        outside = 0
        for p in pts:
            ip = series.get(p["period"])
            if not ip:
                outside += 1
                continue
            # 一致 = 在外部数据自己的显示精度下相同。表里写到 6.4，系统是 6.43，
            # 用户看到的就是两个 6.4，不该被判成不一致。
            d = _decimals(p["written"])
            same = round(float(ip["value"]), d) == round(float(p["value"]), d)
            diff = round(float(p["value"]) - float(ip["value"]), 2)
            # 累计口径的指标（财政收入、投资完成率），外部表里常给「当月」。
            # 两者差一个数量级时判「不一致」是假结论 —— 标成 check，让人去确认
            # 口径，而不是让人以为系统数据错了。
            state = "same" if same else "diff"
            if not same and meta.get("cumulative"):
                base = abs(float(ip["value"])) or 1.0
                if abs(diff) > 0.3 * base:
                    state = "check"
            rows.append(
                {
                    "period": p["period"],
                    "label": ip.get("label") or md._period_label(p["period"]),
                    "external": p["written"],
                    "internal": str(ip["value"]),
                    "diff": 0.0 if same else diff,
                    "state": state,
                    "file": p["file"],
                }
            )
        if not rows:
            if outside:
                notes.append(f"「{meta['name']}」的 {outside} 行期次不在系统数据范围内，未参与对照。")
            continue
        rows.sort(key=lambda r: r["period"])
        shown = rows[-_MAX_ROWS:]
        metrics.append(
            {
                "code": code,
                "name": meta["name"],
                "unit": unit,
                "cumulative": bool(meta.get("cumulative")),
                "rows": shown,
                "compared": len(rows),
                "same": sum(1 for r in shown if r["state"] == "same"),
                "undisclosed": outside,
                "caveat": (
                    "该指标是累计口径（年初至今），外部数据要是同期累计值才可比。"
                    if meta.get("cumulative")
                    else ""
                ),
                "verdict": _verdict(shown, unit, meta["name"]),
            }
        )

    if not metrics:
        notes.append("附件里的数据期次都不在系统现有区间内，无法对照。")
        return {
            "ok": False,
            "files": list(dict.fromkeys(f for f in files if f)),
            "metrics": [],
            "unmatched": unmatched[:6],
            "skipped": skipped[:6],
            "tabular": tabular,
            "notes": notes[:4] or (["附件里的期次都不在系统现有区间内，无法对照。"] if tabular else []),
        }

    metrics.sort(key=lambda m: -m["compared"])
    return {
        "ok": True,
        "files": list(dict.fromkeys(f for f in files if f))[:4],
        # 一次最多 3 个指标：4 个 × 3 期就把对话气泡撑成一张报表，
        # 用户要的是「对上了吗」，不是通读。想细看可以只传那一个指标的表。
        "metrics": metrics[:3],
        "unmatched": unmatched[:6],
        "skipped": skipped[:6],
        "tabular": tabular,
        "notes": notes[:3],
    }
