"""审校：材料出手前的核查。

**审校不让模型参与判断。** 材料里每一处数字、每一个结论，都应该能追到指标库、
督办台账或会议记录里的某一条；审校就是把这些引用逐条回查一遍。让模型「通读一遍
说哪里不对」，同一个问题问两次会给出两套意见，而审校结论必须可复现——更糟的是
它会给出一堆似是而非的建议，把真正的问题淹掉。

**审校不拿纸面自己当标准。** 所有事实都从数据源重新取一遍：拿材料里写的数
和「此刻的指标库」比、拿台账编号和「此刻的台账」比。这样能发现两类错误：
写错了（和事实不符），以及**过期了**（生成之后台账动了，材料还是旧的）。
后者比前者更常见，也更容易蒙过人——毕竟材料是自己系统生成的，看起来最可信。

四道核查：

1. 数字：叙述里出现的每个数字都要在事实池里，池子由指标真值、台账统计、期次、
   结构计数构成；查不到出处的数字一律报「无法溯源」。指标表的单元格则逐格与
   口径表当期值比对，比「在池子里」更严。
2. 引用：督办行能回查到台账事项且状态一致；会议能查到记录；指标当期确实已披露。
3. 过期：台账统计数与本刻重算的统计数是否一致。不一致说明材料生成后台账动了。
4. 体例：章节序号、日期写法、单位称谓、承诺性表述、半角标点。这一类不改事实，
   但逐条都会被办公室退回来。

输出可定位：每条问题都带 block id + 原文片段，前端据此滚到那一段并高亮。
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "oversight", ROOT / "core" / "meeting"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import briefdoc as bd  # noqa: E402
import mockdata as md  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
import schedule  # noqa: E402
from ledger import get as meeting_get  # noqa: E402
from ledger import list_meetings  # noqa: E402

ERROR = "error"
WARN = "warn"
INFO = "info"

_LEVEL_TEXT = {ERROR: "必须改", WARN: "建议改", INFO: "可斟酌"}
_LEVEL_WEIGHT = {ERROR: 10, WARN: 3, INFO: 1}

# 政务材料里一旦写下去就要担责的措辞。不是错别字，但办公室会划掉——
# 「确保」「坚决」「杜绝」是向领导作出的承诺，材料里写满这些，出事时句句都是把柄。
_COMMIT_WORDS = (
    "确保", "坚决", "务必", "杜绝", "彻底", "绝对", "一律",
    "必须完成", "全面完成", "限期完成", "清零", "零容忍",
)

# 中文语境里出现半角标点。表格里的英文缩写不算，只查叙述句。
_HALF_PUNCT = re.compile(r"[\u4e00-\u9fa5][,;:!?]|[,;:!?][\u4e00-\u9fa5]")
_HALF_PAREN = re.compile(r"[\u4e00-\u9fa5]\([^)]{1,12}\)")
_ISO_DATE = re.compile(r"\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}")


def _nums(text: str) -> set[str]:
    """和材料装配用同一套抽法，闸门才是同一道。"""
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def _norm(text: Any) -> str:
    """比对用：去掉空白和全角半角差异，但不改数字本身。"""
    s = str(text if text is not None else "")
    s = s.replace("\u3000", " ").replace("\xa0", " ")
    for a, b in (("％", "%"), ("．", "."), ("－", "-"), ("—", "-"), ("～", "~")):
        s = s.replace(a, b)
    return " ".join(s.split())


def _snippet(text: str, needle: str, width: int = 18) -> str:
    """把命中处前后各截一小段，前端直接展示，不用自己再找。"""
    i = (text or "").find(needle)
    if i < 0:
        return (text or "")[: width * 2]
    a, b = max(0, i - width), min(len(text), i + len(needle) + width)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def _where(block: dict[str, Any]) -> str:
    no = block.get("no") or ""
    title = block.get("title") or ""
    return f"{no}、{title}" if no else title


def _issue(
    level: str,
    rule: str,
    where: str,
    detail: str,
    *,
    block: str = "",
    snippet: str = "",
    fix: str = "",
) -> dict[str, str]:
    return {
        "level": level,
        "level_text": _LEVEL_TEXT.get(level, level),
        "rule": rule,
        "where": where,
        "block": block,
        "snippet": snippet,
        "detail": detail,
        "fix": fix,
    }


# --------------------------------------------------------------------------
# 真值：一切都重新取一遍
# --------------------------------------------------------------------------
def _truth(meta: dict[str, Any], *, today: date) -> dict[str, Any]:
    """从数据源重建事实。纸面上写了什么，在这里不算数。"""
    period = str(meta.get("period") or "")
    codes = [c for c in (meta.get("metric_codes") or []) if isinstance(c, str)]

    metrics: dict[str, dict[str, Any]] = {}
    missing: list[tuple[str, str]] = []
    for code in codes:
        name = (md.METRICS.get(code) or {}).get("name") or code
        if code not in md.METRICS:
            missing.append((code, "指标库里没有这个指标"))
            continue
        r = md.query_metric(code, period, 1, today)
        if not r.get("ok"):
            missing.append((name, f"{period} 期尚未披露"))
            continue
        metrics[code] = r

    # 台账：按本人范围重算，材料里的统计数要和这个对上
    pid = ((meta.get("person") or {}).get("id")) or "mayor"
    try:
        person = people.meta(pid)
        units, manual = schedule.scope_of(person)
    except Exception:  # 人员表变动时不能让审校整个崩掉
        person, units, manual = {"id": pid}, set(), set()

    all_items = ov.list_items()
    try:
        visible = [it for it in all_items if schedule.item_visible(it, units, manual)]
        st = ov.stats(units or None)
    except Exception:
        visible, st = all_items, {}

    meetings = list_meetings()

    pool: set[str] = set()
    for r in metrics.values():
        # 数字池用「装配时那套格式化」的结果建：审校和装配各写一套格式化，
        # 就会出现「8.9%」对「8.9 %」这种假错。
        pool |= _nums(bd._value_text(r)) | _nums(bd._yoy_text(r))
        pool |= _nums(bd._mom_text(r)) | _nums(bd._target_text(r))
    for it in visible:
        pool |= _nums(str(it.get("deadline") or "")) | _nums(str(it.get("id") or ""))
    for k in ("total", "open", "closed", "overdue", "due_soon", "stalled",
              "on_time_closed", "on_time_rate"):
        pool |= _nums(str(st.get(k)))
    pool |= _nums(str(ov.SOON_DAYS)) | _nums(str(ov.STALL_DAYS))
    # 机构名里的数字（「12345 热线中心」）是名字的一部分，不是数据。把它们当
    # 「无法溯源」报出来，会让人一眼就觉得这套审校在瞎报，后面真的问题也没人看了。
    for m in md.METRICS.values():
        pool |= _nums(str(m.get("department") or "")) | _nums(str(m.get("name") or ""))
    for it in visible:
        pool |= _nums(str(it.get("assignee") or "")) | _nums(str(it.get("title") or ""))
    for m in meetings:
        for u in m.get("attendees") or []:
            pool |= _nums(str(u))
    for s in meta.get("sources") or []:
        if isinstance(s, dict):
            pool |= _nums(str(s.get("department") or "")) | _nums(str(s.get("report") or ""))
    for key in ("period", "period_label", "generated_at", "scope_text", "title"):
        pool |= _nums(str(meta.get(key) or ""))
    mt = meta.get("meeting") or {}
    for key in ("id", "held_at"):
        pool |= _nums(str(mt.get(key) or ""))

    return {
        "period": period,
        "metrics": metrics,
        "missing_metrics": missing,
        "items": {it.get("id"): it for it in all_items},
        "visible": visible,
        "stats": st,
        "meetings": meetings,
        "units": units,
        "manual": manual,
        "pool": pool,
    }


# --------------------------------------------------------------------------
# 一、指标表：逐格对照口径表
# --------------------------------------------------------------------------
def _check_metrics(block: dict[str, Any], truth: dict[str, Any], issues: list[dict[str, str]]) -> None:
    where = _where(block)
    rows = block.get("rows") or []
    cols = {c.get("k"): c.get("label") for c in (block.get("columns") or [])}

    for row in rows:
        code = row.get("code")
        r = truth["metrics"].get(code)
        if not r:
            issues.append(_issue(
                ERROR, "ref-metric", where,
                f"指标「{row.get('name') or code}」在指标库里查不到当期披露值，这一行没有出处。",
                block=block.get("id") or "", snippet=str(row.get("name") or code),
                fix="从指标选择里去掉这一项，或改到已披露的期次。",
            ))
            continue
        pairs = (
            ("value", bd._value_text(r), "本期值"),
            ("yoy", bd._yoy_text(r), "同比"),
            ("mom", bd._mom_text(r), "环比"),
            ("target", bd._target_text(r), "年度目标"),
        )
        for key, expect, label in pairs:
            if key not in row:
                continue
            got = _norm(row.get(key))
            want = _norm(expect)
            if got == want:
                continue
            issues.append(_issue(
                ERROR, "num-value", where,
                f"「{row.get('name')}」的{label}写的是「{got}」，口径表当期值是「{want}」。",
                block=block.get("id") or "", snippet=f"{row.get('name')}／{label}",
                fix="按口径表改正；若两个数本来就不同口径，不能放在同一格里。",
            ))

    # 表里点了几行，正文就说几行——「列 4 项指标」但表里 5 行，是读者一眼能看出的破绽
    lead = block.get("lead") or ""
    m = re.search(r"列\s*(\d+)\s*项指标", lead)
    if m and int(m.group(1)) != len(rows):
        issues.append(_issue(
            ERROR, "num-selfcontradict", where,
            f"正文说「列 {m.group(1)} 项指标」，表里实际是 {len(rows)} 行。",
            block=block.get("id") or "", snippet=_snippet(lead, m.group(0)),
            fix="按表里的实际行数改正文。",
        ))


# --------------------------------------------------------------------------
# 二、台账：状态、单位、时限必须和台账此刻一致
# --------------------------------------------------------------------------
def _check_oversight(block: dict[str, Any], truth: dict[str, Any], issues: list[dict[str, str]]) -> None:
    where = _where(block)
    items = truth["items"]

    for row in block.get("rows") or []:
        code = str(row.get("code") or "").strip()
        if not code:
            # 未落库条目本来就没有台账编号，不算错——但要说清楚它不是台账事实
            if row.get("status") == "未落库":
                continue
            issues.append(_issue(
                WARN, "ref-orphan", where,
                f"「{row.get('title')}」这一行没有台账编号，读者点不回去核对。",
                block=block.get("id") or "", snippet=str(row.get("title") or ""),
                fix="在会议助手里勾选落库后再进材料。",
            ))
            continue
        it = items.get(code)
        if it is None:
            issues.append(_issue(
                ERROR, "ref-item", where,
                f"台账编号 {code} 在台账里查不到，这一行是孤儿。",
                block=block.get("id") or "", snippet=f"{row.get('title')}／{code}",
                fix="删掉这一行，或确认台账事项是否被删除。",
            ))
            continue
        for key, now, label in (
            ("status", it.get("status"), "状态"),
            ("assignee", it.get("assignee"), "责任单位"),
            ("deadline", it.get("deadline"), "时限"),
        ):
            if key not in row:
                continue
            if _norm(row.get(key)) != _norm(now):
                issues.append(_issue(
                    ERROR, "ref-state", where,
                    f"{code}「{row.get('title')}」的{label}写的是「{_norm(row.get(key))}」，台账当前是「{_norm(now)}」。",
                    block=block.get("id") or "", snippet=f"{code}／{label}",
                    fix="重新装配这一节；材料不能自带一份台账副本。",
                ))

    # 统计数过期：材料是投影，生成之后台账动了，材料就该重出。
    # 正文和统计卡说的是同几件事，按「哪一项统计」合并，一个根因只报一条——
    # 一次台账变动刷出五条「已变动」，人会以为系统在抽风。
    st = truth["stats"] or {}
    lead = block.get("lead") or ""
    stat_label = {
        "total": "台账总数", "open": "在办数", "closed": "已办结数",
        "overdue": "逾期数", "due_soon": "临界数", "stalled": "停滞数",
    }
    stale: dict[str, tuple[str, Any]] = {}

    for pattern, key in (
        (r"台账共\s*(\d+)\s*项", "total"),
        (r"在办\s*(\d+)\s*项", "open"),
        (r"已办结\s*(\d+)\s*项", "closed"),
        (r"逾期\s*(\d+)\s*项", "overdue"),
        (r"临界\s*(\d+)\s*项", "due_soon"),
    ):
        m = re.search(pattern, lead)
        if m and st.get(key) is not None and int(m.group(1)) != int(st[key]):
            stale.setdefault(key, (m.group(1), st[key]))

    # 统计卡片逐张比：逾期、临界、停滞都会随台账动，只看总数不够——
    # 「总数没变但停滞从 4 变 3」照样是过期材料，而且更难发现。
    stat_keys = (
        ("台账事项", "total"), ("在办", "open"), ("已办结", "closed"),
        ("逾期", "overdue"), ("临界", "due_soon"), ("停滞", "stalled"),
    )
    for srow in block.get("stats") or []:
        label = str(srow.get("label") or "")
        val = str(srow.get("value") or "")
        m = re.match(r"^(-?\d+)\s*项$", val.strip())
        if not m:
            continue
        key = next((k for prefix, k in stat_keys if label.startswith(prefix)), None)
        if key and st.get(key) is not None and int(m.group(1)) != int(st[key]):
            stale.setdefault(key, (m.group(1), st[key]))

    for key, (written, now) in stale.items():
        issues.append(_issue(
            WARN, "num-stale", where,
            f"材料写「{stat_label.get(key, key)} {written} 项」，台账此刻是 {now} 项——材料生成后台账有变动。",
            block=block.get("id") or "", snippet=f"{stat_label.get(key, key)} {written} 项",
            fix="重新生成材料。过期数字比错数字更危险：它看起来就是自己系统出的。",
        ))


# --------------------------------------------------------------------------
# 三、会议
# --------------------------------------------------------------------------
def _check_meeting(
    meta: dict[str, Any], block: dict[str, Any], truth: dict[str, Any], issues: list[dict[str, str]]
) -> None:
    where = _where(block)
    m = block.get("meeting") or meta.get("meeting") or {}
    mid = str(m.get("id") or "").strip()
    if not mid:
        return
    elif meeting_get(mid) is None:
        issues.append(_issue(
            ERROR, "ref-meeting", where,
            f"会议 {mid} 在会议记录里查不到。",
            block=block.get("id") or "", snippet=mid,
            fix="重新选会议；会议记录被删掉时材料不能继续引用。",
        ))
        return

    # 会里交办几项、其中几项已落库，要和台账此刻对得上
    rows = block.get("rows") or []
    lead = block.get("lead") or ""
    mm = re.search(r"共交办\s*(\d+)\s*项", lead)
    if mm and int(mm.group(1)) != len(rows):
        issues.append(_issue(
            ERROR, "num-selfcontradict", where,
            f"正文说「共交办 {mm.group(1)} 项」，下面列了 {len(rows)} 项。",
            block=block.get("id") or "", snippet=_snippet(lead, mm.group(0)),
            fix="按实际条目数改正。",
        ))


def _check_meeting_meta(meta: dict[str, Any], issues: list[dict[str, str]]) -> None:
    mt = meta.get("meeting") or {}
    mid = str(mt.get("id") or "").strip()
    if mid and meeting_get(mid) is None:
        issues.append(_issue(
            ERROR, "ref-meeting", "文头",
            f"文头引用的会议 {mid} 在会议记录里查不到。",
            fix="重新选择会议后生成。",
        ))


# --------------------------------------------------------------------------
# 四、数字溯源：叙述里出现的数字，必须能从事实池里找到
# --------------------------------------------------------------------------
def _narrations(meta: dict[str, Any], blocks: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """(block_id, 位置名, 文本)。只取叙述性文本——表格里的台账原文不算编造。"""
    out: list[tuple[str, str, str]] = []
    for b in blocks:
        where = _where(b)
        if b.get("lead"):
            out.append((b.get("id") or "", where, str(b["lead"])))
        if b.get("text"):
            out.append((b.get("id") or "", where, str(b["text"])))
        if b.get("note"):
            out.append((b.get("id") or "", where + "（注）", str(b["note"])))
    return out


def _check_numbers(
    meta: dict[str, Any], blocks: list[dict[str, Any]], truth: dict[str, Any], issues: list[dict[str, str]]
) -> dict[str, int]:
    pool = set(truth["pool"])
    # 结构计数：行数、列数、部门数、来源数，都是本章节自己的事实
    for b in blocks:
        for key in ("rows", "columns", "stats", "assignees", "sources"):
            v = b.get(key)
            if isinstance(v, list):
                pool.add(str(len(v)))
        pool |= _nums(str(len(str(b.get("title") or ""))))
        # 统计卡与分单位表上的数，正文常会复述一遍。它们的真伪由 num-stale / ref-state
        # 逐张核对，不该再按「编造」记一笔——同一个根因报两条，人只会记住噪音那一条。
        for srow in b.get("stats") or []:
            pool |= _nums(str(srow.get("value") or ""))
        for arow in b.get("assignees") or []:
            for key in ("open", "closed", "overdue"):
                pool |= _nums(str(arow.get(key)))
        # 「其中 N 场已有交办事项落库」这类是行内数值的聚合，不是另取的数
        rows = b.get("rows") or []
        for key in ("items", "committed", "open", "closed", "overdue"):
            vals = [r.get(key) for r in rows if isinstance(r.get(key), (int, float))]
            if vals:
                pool.add(str(sum(vals)))
            hits = [r.get(key) for r in rows if r.get(key)]
            if hits:
                pool.add(str(len(hits)))

    total = 0
    bad: list[str] = []
    for bid, where, text in _narrations(meta, blocks):
        # ISO 日期会碎成 2026／12／31 三个数，那不是三个数，是一种写法问题——
        # 交给 sty-date 报，不要在这里再刷三条「无法溯源」把真问题挤下去
        for n in sorted(_nums(_ISO_DATE.sub(" ", text))):
            total += 1
            if n in pool:
                continue
            if len(n) == 1:
                # 单个数字多数是序号或量词（「3 个部门」里的 3 确实要核，但也有
                # 「一、二、」之外的零星用法），误报代价高于漏报，放成提示
                issues.append(_issue(
                    INFO, "num-offlist", where,
                    f"数字 {n} 在事实池里找不到直接出处，请确认它是不是手写的。",
                    block=bid, snippet=_snippet(text, n),
                    fix="能删就删；要保留就补一句出处。",
                ))
            else:
                bad.append(n)
                issues.append(_issue(
                    ERROR, "num-offlist", where,
                    f"数字 {n} 在指标库、台账、会议记录里都查不到，属于无法溯源的数。",
                    block=bid, snippet=_snippet(text, n),
                    fix="删掉，或改成材料里已有的数——上报材料里的每个数都要能点回出处。",
                ))
    return {"numbers": total, "untraced": len(bad)}


# --------------------------------------------------------------------------
# 五、体例
# --------------------------------------------------------------------------
def _check_style(meta: dict[str, Any], blocks: list[dict[str, Any]], truth: dict[str, Any],
                 issues: list[dict[str, str]]) -> None:
    # 序号连续性：一、二、三……跳号或重号，是最容易被一眼退回的地方
    seen: dict[str, str] = {}
    for i, b in enumerate(blocks):
        no = str(b.get("no") or "")
        want = bd._CN[i] if i < len(bd._CN) else str(i + 1)
        if no != want:
            issues.append(_issue(
                ERROR, "sty-seq", _where(b),
                f"章节序号是「{no}」，按顺序应该是「{want}」。",
                block=b.get("id") or "",
                fix="重新生成；手改序号很容易漏掉一处。",
            ))
        if no in seen:
            issues.append(_issue(
                ERROR, "sty-seq", _where(b),
                f"章节序号「{no}」重复出现。", block=b.get("id") or "",
                fix="重新生成。",
            ))
        seen[no] = _where(b)

    # 承诺性表述：不改事实，但逐句都是担责的话
    for bid, where, text in _narrations(meta, blocks):
        hit = [w for w in _COMMIT_WORDS if w in text]
        if hit:
            issues.append(_issue(
                WARN, "sty-commit", where,
                f"出现承诺性表述「{'、'.join(hit[:3])}」，材料里写下去就是对本级政府的承诺。",
                block=bid, snippet=_snippet(text, hit[0]),
                fix="改为「力争」「推动」「督促」「协调」等推进性表述。",
            ))
        m = _ISO_DATE.search(text)
        if m:
            issues.append(_issue(
                WARN, "sty-date", where,
                f"日期写成「{m.group(0)}」，公文正文应为「2026年11月7日」这种写法。",
                block=bid, snippet=_snippet(text, m.group(0)),
                fix="改成「YYYY年M月D日」。表格里的台账时限是数据原文，可以不改。",
            ))
        m = _HALF_PUNCT.search(text) or _HALF_PAREN.search(text)
        if m:
            issues.append(_issue(
                WARN, "sty-hw", where,
                f"中文句子里用了半角标点「{m.group(0)}」。",
                block=bid, snippet=_snippet(text, m.group(0)),
                fix="换成全角。半角标点在 Word 里会撑出不均匀的字距。",
            ))

    # 单位称谓：同一家单位在材料里出现全称和简称两种写法
    units: set[str] = set()
    for it in truth.get("visible") or []:
        if it.get("assignee"):
            units.add(str(it["assignee"]))
    for m in truth.get("meetings") or []:
        for u in m.get("attendees") or []:
            units.add(str(u))
    body = "\n".join(t for _, _, t in _narrations(meta, blocks))
    for u in sorted(units):
        if not (u.startswith("市") and len(u) > 3):
            continue
        short = u[1:]
        # 「市生态环境局」本身就包含「生态环境局」，直接 `in` 判断会把全称当成两种写法，
        # 每份提到市属单位的材料都会被报一遍。要用「后面不跟着市」来认简称。
        has_short = re.search(r"(?<!市)" + re.escape(short), body) is not None
        if u in body and has_short:
            issues.append(_issue(
                WARN, "sty-unit", "全文",
                f"「{u}」和「{short}」两种写法同时出现。",
                snippet=u,
                fix="首次出现用全称，后文统一用简称。",
            ))


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def review(
    meta: dict[str, Any],
    blocks: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """对一份已装配的材料做审校。只读：不改 blocks，不写任何存储。"""
    if not isinstance(meta, dict) or not isinstance(blocks, list) or not blocks:
        return {"ok": False, "error": "no_material"}

    today = today or date.today()
    truth = _truth(meta, today=today)
    issues: list[dict[str, str]] = []

    for name, msg in truth["missing_metrics"]:
        issues.append(_issue(
            ERROR, "ref-metric", "文头",
            f"材料声明使用指标「{name}」，但{msg}。",
            fix="从指标选择里去掉，或改到已披露的期次。",
        ))
    _check_meeting_meta(meta, issues)

    for b in blocks:
        bid = b.get("id")
        if bid == "metrics":
            _check_metrics(b, truth, issues)
        elif bid == "oversight":
            _check_oversight(b, truth, issues)
        elif bid == "meeting":
            _check_meeting(meta, b, truth, issues)
        if b.get("kind") == "text" and not str(b.get("text") or "").strip():
            issues.append(_issue(
                ERROR, "sty-empty", _where(b),
                "这一节没有内容。", block=bid or "",
                fix="去掉这一节，或补上内容。",
            ))

    counts = _check_numbers(meta, blocks, truth, issues)
    _check_style(meta, blocks, truth, issues)

    order = {str(b.get("id") or ""): i for i, b in enumerate(blocks)}
    level_rank = {ERROR: 0, WARN: 1, INFO: 2}
    issues.sort(key=lambda x: (level_rank.get(x["level"], 3), order.get(x["block"], -1)))

    tally = {ERROR: 0, WARN: 0, INFO: 0}
    for it in issues:
        tally[it["level"]] = tally.get(it["level"], 0) + 1

    penalty = sum(_LEVEL_WEIGHT.get(it["level"], 0) for it in issues)
    score = max(0, 100 - penalty)
    verdict = "block" if tally[ERROR] else ("review" if tally[WARN] else "pass")

    # 引用回溯率：表格行里能点回台账/会议/指标的比例。材料最值钱的就是这个数
    traceable = traced = 0
    for b in blocks:
        for row in b.get("rows") or []:
            traceable += 1
            if row.get("code") or row.get("ref") or row.get("source"):
                traced += 1

    return {
        "ok": True,
        "verdict": verdict,
        "verdict_text": {"block": "有必须改的问题，暂不能出手",
                         "review": "没有硬错，但有需要斟酌的表述",
                         "pass": "未发现问题，可以出手"}[verdict],
        "score": score,
        "summary": {
            "total": len(issues),
            "error": tally[ERROR],
            "warn": tally[WARN],
            "info": tally[INFO],
            "numbers": counts["numbers"],
            "untraced": counts["untraced"],
            "rows": traceable,
            "rows_traced": traced,
        },
        "issues": issues,
        "checked": {
            "period": truth["period"],
            "metrics": len(truth["metrics"]),
            "items": len(truth["items"]),
            "at": today.isoformat(),
        },
    }
