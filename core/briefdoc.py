"""材料助手：把已核实的数字和台账事实装配成一份公文骨架。

**这个模块不写事实，只排事实。** 材料里的每一处数字要么来自 `mockdata` 的指标值
（连部门、报表名、披露日、口径一起取），要么来自督办台账和会议记录；模型只写最后
一节「下一步工作建议」，且出现装配清单之外的数字就整段丢弃。

为什么不让模型写整篇：汇报材料里最危险的错误不是句子不通，而是把「1—9 月累计」写成
「9 月单月」、把「预计」写成「实绩」、把去年的数当今年数。模型看到的是字符串，它没有
办法区分这些。而这类错误一旦写进上报材料，基本没人会回头复核——写的人自己也不确定。

**三种骨架**（都只是章节组合，数据来自同一批）：
- 工作汇报：指标运行 + 重点工作 + 会议落实 + 下一步
- 会前材料：这场会的基本情况 + 相关指标 + 该会交办事项 + 披露情况
- 督办通报：台账统计 + 逾期与临期清单 + 单位办理情况 + 下一步

**只读，不改台账、不发起动作。** 材料是投影，跟日程同一个立场：同一个事实只在一个
地方维护，别处只能引用。点材料里的台账编号会回到督办详情，而不是在材料里签收。

**没有存档。** 材料不落盘：一旦存起来就会出现「文件里的数和台账不一致」的问题，
而那正是这类系统最容易失信的地方。要留存就导出 Markdown / 打印。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "oversight", ROOT / "core" / "meeting"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ask as ask_mod  # noqa: E402
import llmclient  # noqa: E402
import mockdata as md  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
import schedule  # noqa: E402
from ledger import get as meeting_get  # noqa: E402
from ledger import list_meetings  # noqa: E402

CITY = "溧阳市"

# 指标条数上限：11 个指标全塞进一张表就没人看了，材料不是字典。
MAX_METRICS = 12
# 明细行上限：超过 8 行就该另附清单，正文里只放最要紧的。
MAX_ROWS = 8
# 会议材料里指标的下限：参会单位匹配的指标往往只有一两个
MIN_METRICS = 4

SECTIONS: dict[str, str] = {
    "metrics": "主要指标运行情况",
    "oversight": "重点工作推进情况",
    "meeting": "会议交办事项落实情况",
    "pending": "数据报送与披露情况",
    "next": "下一步工作建议",
}

PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "report",
        "label": "工作汇报",
        "hint": "指标运行 + 重点工作 + 会议落实",
        "sections": ["metrics", "oversight", "meeting", "next"],
    },
    {
        "id": "minutes",
        "label": "会议落实材料",
        "hint": "以一场会为单位，汇总该会交办事项的办理情况",
        "sections": ["meeting", "metrics", "pending", "next"],
    },
    {
        "id": "bulletin",
        "label": "督办通报",
        "hint": "台账统计 + 逾期临期清单 + 单位办理情况",
        "sections": ["oversight", "next"],
    },
)
PRESET_MAP = {p["id"]: p for p in PRESETS}

RISK_TONE = {
    "逾期": "danger",
    "今日到期": "danger",
    "临期": "warn",
    "停滞": "warn",
    "签收超时": "warn",
    "正常": "muted",
}

_CN = ("一", "二", "三", "四", "五", "六", "七", "八")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _nums(text: str) -> set[str]:
    """抽取文本里的数字。和日程、专家解读用同一套抽法，闸门才是同一道。"""
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def _tone(risk: str) -> str:
    return RISK_TONE.get(risk, "muted")


# --------------------------------------------------------------------------
# 指标：数字、说法、出处三者必须一起到手
# --------------------------------------------------------------------------
def _value_text(r: dict[str, Any]) -> str:
    return ask_mod.fmt_value(r.get("value"), r.get("unit") or "%")


def _yoy_text(r: dict[str, Any]) -> str:
    """同比怎么写，取决于指标的计量单位。

    百分比类指标（增速、比率）的「同比」是百分点差；总量类指标（亿元、户、项）
    才是「同比增长 x%」。这两种说法在材料里混用，读的人会直接算错。

    真实年度值（如实测 GDP）只有可比价增速、没有上年同期绝对量，`yoy` 为
    None —— 这时候总量类指标照样能出「同比增长 x%」，不能因为 `yoy` 空着
    就退成「—」。所以先按单位分岔，再看各自那个字段。
    """
    if r.get("unit") == "%":
        v = r.get("yoy")
        return ask_mod.delta_text(v, "%", "同比") if v is not None else "—"
    p = r.get("yoy_pct")
    if p is None:
        return "—"
    if p == 0:
        return "同比持平"
    return f"同比{'增长' if p > 0 else '下降'} {abs(p):g}%"


def _mom_text(r: dict[str, Any]) -> str:
    note = r.get("mom_note")
    if note:
        return note
    v = r.get("mom")
    if v is None:
        return "—"
    return ask_mod.delta_text(v, r.get("unit") or "%", "环比")


def _target_text(r: dict[str, Any]) -> str:
    """年度目标只做「达没达」的判断，不算差额 —— 差额是另一处新数，容易被引错。"""
    t = r.get("target")
    if t is None:
        return "—"
    shown = ask_mod.fmt_value(t, r.get("unit") or "%")
    value = r.get("value")
    if value is None:
        return shown
    return f"{shown}（{'已达' if value >= t else '未达'}）"


def _metrics_block(ctx: dict[str, Any]) -> dict[str, Any] | None:
    codes = ctx["codes"]
    if not codes:
        return None

    rows: list[dict[str, str]] = []
    sources: list[dict[str, str]] = []
    missing: list[str] = []
    seen_dep: set[str] = set()

    for code in codes:
        r = md.query_metric(code, ctx["period"], 1, ctx["today"])
        if not r.get("ok"):
            # 未披露就是没数，不能拿上期顶上——那会变成「用旧数冒充新数」
            missing.append(md.METRICS[code]["name"])
            continue
        src = r["source"]
        rows.append(
            {
                "code": code,
                "name": r["name"],
                "value": _value_text(r),
                "yoy": _yoy_text(r),
                "mom": _mom_text(r),
                "target": _target_text(r),
                "source": src["department"],
            }
        )
        key = f"{src['department']}|{src['report']}"
        if key not in seen_dep:
            seen_dep.add(key)
            sources.append(
                {
                    "department": src["department"],
                    "report": src["report"],
                    "period": src["period"],
                    "published_at": src["published_at"],
                    "definition": src["definition"],
                }
            )

    if not rows:
        return None

    # 部门要去重再数：统计局的月报和 GDP 核算月报是两份报表，但对外是同一个部门，
    # 写成「来自 市统计局、市统计局」是一眼可见的糙
    deps: list[str] = []
    for s in sources:
        if s["department"] not in deps:
            deps.append(s["department"])
    lead = (
        f"本期列 {len(rows)} 项指标，来自 {len(deps)} 个部门"
        f"（{'、'.join(deps[:3])}{'等' if len(deps) > 3 else ''}），口径见文末资料来源。"
    )
    note = "累计口径指标的「本期」为该期累计值，同比按累计数对比；比率类指标的同比为百分点差。"
    if missing:
        note += f" 未列入（本期未披露）：{'、'.join(missing)}。"

    return {
        "id": "metrics",
        "title": SECTIONS["metrics"],
        "lead": lead,
        "kind": "table",
        "columns": [
            {"k": "name", "label": "指标"},
            {"k": "value", "label": "本期"},
            {"k": "yoy", "label": "同比"},
            {"k": "mom", "label": "环比"},
            {"k": "target", "label": "年度目标"},
            {"k": "source", "label": "数据来源"},
        ],
        "rows": rows,
        "note": note,
        "sources": sources,
    }


# --------------------------------------------------------------------------
# 重点工作：统计口径随身份收窄
# --------------------------------------------------------------------------
def _oversight_rows(items: list[dict[str, Any]], limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    rows = []
    for it in items[:limit]:
        rows.append(
            {
                "title": it["title"],
                "assignee": it["assignee"],
                "deadline": it["deadline"],
                "status": it["status"],
                "risk": it["risk"],
                "tone": _tone(it["risk"]),
                "ref": f"oversight:{it['id']}",
                "code": it["id"],
            }
        )
    return rows


def _oversight_block(ctx: dict[str, Any]) -> dict[str, Any] | None:
    st = ctx["stats"]
    visible = ctx["visible"]

    if ctx["kind"] == "bulletin":
        # 通报看全量在办，不只看有风险的：没风险也得知道在办几件
        pool = [it for it in visible if it["status"] != ov.CLOSED]
        rows = _oversight_rows(pool)
        lead = f"台账共 {st['total']} 项，在办 {st['open']} 项，已办结 {st['closed']} 项。"
    else:
        pool = [it for it in visible if it["risk"] != ov.RISK_NONE]
        rows = _oversight_rows(pool)
        lead = f"台账共 {st['total']} 项，在办 {st['open']} 项，其中逾期 {st['overdue']} 项、临界 {st['due_soon']} 项。"
        if not pool:
            lead = f"台账共 {st['total']} 项，在办 {st['open']} 项，目前没有逾期、临界或停滞事项。"

    stats_rows = [
        {"label": "台账事项", "value": f"{st['total']} 项"},
        {"label": "在办", "value": f"{st['open']} 项"},
        {"label": "已办结", "value": f"{st['closed']} 项"},
        {"label": "逾期", "value": f"{st['overdue']} 项", "tone": "danger" if st["overdue"] else "muted"},
        {"label": "临界（{0} 天内到期）".format(ov.SOON_DAYS), "value": f"{st['due_soon']} 项", "tone": "warn" if st["due_soon"] else "muted"},
        {"label": "停滞（超 {0} 天未报进展）".format(ov.STALL_DAYS), "value": f"{st['stalled']} 项", "tone": "warn" if st["stalled"] else "muted"},
    ]
    if st["on_time_rate"] is not None:
        stats_rows.append({"label": "按期办结率", "value": f"{st['on_time_rate']}%（已办结 {st['closed']} 项中 {st['on_time_closed']} 项按期）"})
    else:
        stats_rows.append({"label": "按期办结率", "value": "—（暂无已办结事项）"})

    block: dict[str, Any] = {
        "id": "oversight",
        "title": SECTIONS["oversight"],
        "lead": lead,
        "kind": "oversight",
        "stats": stats_rows,
        "rows": rows,
        "note": f"统计范围：{ctx['scope_text']}。风险等级、天数、按期率均由台账状态机判定，不是模型估算。",
    }

    if ctx["kind"] == "bulletin" and st["by_assignee"]:
        block["assignees"] = [
            {"unit": unit, "open": v["在办"], "closed": v["已办结"], "overdue": v["逾期"]}
            for unit, v in sorted(st["by_assignee"].items(), key=lambda kv: (-kv[1]["逾期"], -kv[1]["在办"], kv[0]))
        ]
    return block


# --------------------------------------------------------------------------
# 会议：材料里出现哪几场会，看这个人该不该知道
# --------------------------------------------------------------------------
def _meeting_block(ctx: dict[str, Any]) -> dict[str, Any] | None:
    chosen = ctx["meeting"]
    if ctx["kind"] == "minutes":
        # 系统里只有「已经开过的会」（会后录入纪要），没有「即将召开的会」。
        # 所以这份材料的用途不是「会前准备」，而是「这个会定的事，现在办到哪了」——
        # 交办事项如果已经落库，就带台账状态；没落库就如实写「未落库」，不能假装在办。
        if not chosen:
            return None
        items = ctx["meeting_items"]
        if items:
            rows = _oversight_rows(items, limit=99)
            for r in rows:
                r["source"] = "台账"
            lead = f"{chosen['title']}共交办 {len(items)} 项，均已进台账，下面逐项列办理情况。"
            note = "事项办理状态来自督办台账，材料内不可修改；要签收或报进展请去台账操作。"
        else:
            raw = chosen.get("items") or []
            rows = [
                {
                    "title": i.get("title") or "",
                    "assignee": i.get("assignee") or "",
                    "deadline": i.get("deadline") or "",
                    "status": "未落库",
                    "risk": "",
                    "tone": "muted",
                    "ref": "",
                    "code": "",
                    "source": "会议记录",
                }
                for i in raw
            ]
            lead = (
                f"{chosen['title']}已抽取 {len(raw)} 项交办内容，尚未勾选落库，因此台账里查不到办理情况。"
                if raw
                else f"{chosen['title']}没有抽取到交办内容。"
            )
            note = "未落库条目只有会议记录里的原始表述，没有责任分解和时限，不能当办理进度看。"
        return {
            "id": "meeting",
            "title": SECTIONS["meeting"],
            "lead": lead,
            "kind": "meeting",
            "meeting": {
                "id": chosen["id"],
                "title": chosen["title"],
                "held_at": chosen.get("held_at") or "",
                "host": chosen.get("host") or "",
                "attendees": chosen.get("attendees") or [],
            },
            "rows": rows,
            "note": note,
        }

    meetings = ctx["meetings"][:5]
    if not meetings:
        return None
    rows = [
        {
            "title": m["title"],
            "held_at": m.get("held_at") or "",
            "host": m.get("host") or "",
            "attendees": "、".join(m.get("attendees") or []) or "未填参会单位",
            "items": m.get("item_count", 0),
            "committed": m.get("committed_count", 0),
        }
        for m in meetings
    ]
    return {
        "id": "meeting",
        "title": SECTIONS["meeting"],
        "lead": f"近期可查 {len(rows)} 场会议，其中 {sum(1 for r in rows if r['committed'])} 场已有交办事项落库。",
        "kind": "list",
        "rows": rows,
        "note": "会议记录是输入，不是模型产物：纪要原文由人工录入，交办事项逐条勾选后才进台账。",
    }


# --------------------------------------------------------------------------
# 披露：没到日子的数据要说明白，不能让读者以为是漏了
# --------------------------------------------------------------------------
def _pending_block(ctx: dict[str, Any]) -> dict[str, Any] | None:
    pend = md.pending_periods(ctx["today"])
    if not pend:
        return None
    rows = []
    for p in pend[:2]:
        due = md.disclosed_on(p)
        rows.append(
            {
                "period": ask_mod.period_text({"code": "", "period_label": f"{p[:4]}年{int(p[5:7])}月"}),
                "due": due.isoformat(),
            }
        )
    first = rows[0]
    return {
        "id": "pending",
        "title": SECTIONS["pending"],
        "lead": f"{first['period']}数据按月度披露规则于 {first['due']} 发布，本材料未含该期。",
        "kind": "pending",
        "rows": rows,
        "note": f"披露日为每月 {md.DISCLOSURE_DAY} 日（遇节假日顺延的处理尚未实现）。",
    }


# --------------------------------------------------------------------------
# 下一步：唯一由模型写的段落
# --------------------------------------------------------------------------
def _rule_next(ctx: dict[str, Any]) -> str:
    """模型不在时的兜底。只复述台账里已经算出来的事实，不评价、不提新事。"""
    st = ctx["stats"]
    parts: list[str] = []
    if st["overdue"]:
        parts.append(f"对逾期 {st['overdue']} 项逐项督办，要求责任单位说明原因并重新明确办结时间。")
    if st["due_soon"]:
        parts.append(f"{st['due_soon']} 项将在 {ov.SOON_DAYS} 天内到期，提前介入，避免到期才算账。")
    if st["stalled"]:
        parts.append(f"{st['stalled']} 项超过 {ov.STALL_DAYS} 天没有报进展，按周通报到责任单位。")
    if not parts:
        parts.append("台账内暂无逾期、临界和停滞事项，按既定节点推进。")
    return "".join(parts)


_SYS = """你是市政府办公室的文字参谋。下面是一份已经装配好的材料事实（JSON）。你只写最后一节「下一步工作建议」。

硬规则（违反任何一条，你这段会被整段丢弃，材料退回规则文本）：
- 不超过 220 字，3 到 5 句，每句落到具体动作。
- 只能使用下面出现过的数字，不许自己算比例、算天数、算合计、算排名。
- 不许新增下面没有的事项、单位、指标、会议。
- 不用「高度重视」「狠抓落实」「进一步加强」这类没有动作的套话。
- 只输出 JSON：{"next": "..."}"""


def _facts_for_model(blocks: list[dict[str, Any]], ctx: dict[str, Any]) -> dict[str, Any]:
    facts: list[str] = []
    for b in blocks:
        if b["kind"] == "table":
            for r in b["rows"]:
                facts.append(f"{r['name']}：{r['value']}，{r['yoy']}，{r['mom']}，目标 {r['target']}")
        elif b["kind"] == "oversight":
            for s in b["stats"]:
                facts.append(f"{s['label']}：{s['value']}")
            for r in b["rows"][:6]:
                facts.append(f"{r['title']}（{r['assignee']}，{r['deadline']} 前，{r['status']}，{r['risk']}）")
        elif b["kind"] == "meeting":
            facts.append(f"会议：{b['meeting']['title']}，{b['meeting']['held_at']}，主持人 {b['meeting']['host']}")
            for r in b["rows"][:6]:
                facts.append(f"交办：{r['title']}（{r['assignee']}）")
        elif b["kind"] == "list":
            for r in b["rows"]:
                facts.append(f"{r['title']}，{r['held_at']}，交办 {r['items']} 项，落库 {r['committed']} 项")
        elif b["kind"] == "pending":
            for r in b["rows"]:
                facts.append(f"{r['period']}数据将于 {r['due']} 披露，本材料未含")
    return {
        "period": ctx["period_label"],
        "scope": ctx["scope_text"],
        "facts": facts,
    }


def _enrich_next(blocks: list[dict[str, Any]], ctx: dict[str, Any], *, use_model: bool) -> dict[str, Any]:
    """给「下一步」块补模型文本。失败一律保留规则文本，并如实记录原因。"""
    target = next((b for b in blocks if b["id"] == "next"), None)
    if target is None:
        return {"source": "rule", "note": "本章节未选用", "degraded": True}

    rule = target["text"]
    if not use_model:
        return {"source": "rule", "note": "本次只出规则文本（未调用模型）", "degraded": True}
    if not llmclient.available():
        return {"source": "rule", "note": "未配置模型，本节按台账事实生成", "degraded": True}

    payload = _facts_for_model(blocks, ctx)
    allowed: set[str] = set()
    for line in payload["facts"]:
        allowed |= _nums(line)
    allowed |= _nums(ctx["period_label"]) | _nums(ctx["today"].isoformat())

    try:
        res = llmclient.chat_json(_SYS, json.dumps(payload, ensure_ascii=False), timeout=45, max_tokens=400)
    except (llmclient.ModelUnavailable, llmclient.ModelError) as exc:
        return {"source": "rule", "note": f"模型调用失败（{type(exc).__name__}），已退回规则文本", "degraded": True}

    text = str((res.get("data") or {}).get("next") or "").strip()
    if not text:
        return {"source": "rule", "note": "模型未给出内容，已退回规则文本", "degraded": True}
    if len(text) > 260:
        return {"source": "rule", "note": "模型输出超过一段的长度，已丢弃", "degraded": True}
    extra = _nums(text) - allowed
    if extra:
        return {"source": "rule", "note": f"模型输出含事实清单外的数字（{'、'.join(sorted(extra)[:3])}），已丢弃", "degraded": True}

    target["text"] = text
    target["source"] = "model"
    # 页脚那行注释要跟着换，否则纸面上写着「本节由台账事实生成」，实际是模型写的
    target["note"] = "本节由模型根据上文事实撰写，数字与事项均取自材料本身。"
    return {"source": "model", "note": "本节由模型根据上述事实撰写，数字均取自材料", "degraded": False}


# --------------------------------------------------------------------------
# Markdown：导出用，服务端生成，避免前后端两份格式
# --------------------------------------------------------------------------
def _markdown(meta: dict[str, Any], blocks: list[dict[str, Any]]) -> str:
    L: list[str] = [f"# {meta['title']}", ""]
    mt = meta.get("meeting")
    if mt:
        L.append(f"**会议**：{mt['title']}（{mt.get('held_at') or '时间待定'}）　　**范围**：{meta['scope_text']}　　**生成时间**：{meta['generated_at']}")
        L.append(f"**指标期次**：{meta['period_label']}")
    else:
        L.append(f"**报告期**：{meta['period_label']}　　**范围**：{meta['scope_text']}　　**生成时间**：{meta['generated_at']}")
    L.append("")
    if meta.get("mock"):
        L.append("> 演示数据：本材料取自内置样例库，未接入真实数据源，不可用于对外报送。")
        L.append("")

    for b in blocks:
        L.append(f"## {b['no']}、{b['title']}")
        L.append("")
        if b.get("lead"):
            L.extend([b["lead"], ""])

        if b["kind"] == "table":
            cols = b["columns"]
            L.append("| " + " | ".join(c["label"] for c in cols) + " |")
            L.append("|" + "|".join("---" for _ in cols) + "|")
            for r in b["rows"]:
                L.append("| " + " | ".join(str(r.get(c["k"]) or "—") for c in cols) + " |")
            L.append("")
        elif b["kind"] == "oversight":
            for s in b["stats"]:
                L.append(f"- **{s['label']}**：{s['value']}")
            L.append("")
            if b["rows"]:
                L.append("| 事项 | 责任单位 | 时限 | 状态 | 风险 | 台账编号 |")
                L.append("|---|---|---|---|---|---|")
                for r in b["rows"]:
                    L.append(f"| {r['title']} | {r['assignee']} | {r['deadline'] or '—'} | {r['status']} | {r['risk'] or '—'} | {r['code'] or '未落库'} |")
                L.append("")
            if b.get("assignees"):
                L.append("| 责任单位 | 在办 | 已办结 | 逾期 |")
                L.append("|---|---|---|---|")
                for a in b["assignees"]:
                    L.append(f"| {a['unit']} | {a['open']} | {a['closed']} | {a['overdue']} |")
                L.append("")
        elif b["kind"] == "meeting":
            m = b["meeting"]
            L.append(f"- **会议**：{m['title']}（{m['id']}）")
            L.append(f"- **时间**：{m['held_at'] or '待定'}")
            L.append(f"- **主持人**：{m['host'] or '未填'}")
            L.append(f"- **参会单位**：{'、'.join(m['attendees']) or '未填'}")
            L.append("")
            if b["rows"]:
                L.append("| 交办事项 | 责任单位 | 时限 | 状态 |")
                L.append("|---|---|---|---|")
                for r in b["rows"]:
                    L.append(f"| {r['title']} | {r['assignee']} | {r['deadline'] or '—'} | {r['status']} |")
                L.append("")
        elif b["kind"] == "list":
            L.append("| 会议 | 时间 | 主持人 | 参会单位 | 交办 | 已落库 |")
            L.append("|---|---|---|---|---|---|")
            for r in b["rows"]:
                L.append(f"| {r['title']} | {r['held_at']} | {r['host']} | {r['attendees']} | {r['items']} | {r['committed']} |")
            L.append("")
        elif b["kind"] == "pending":
            L.append("| 期次 | 预计披露日 |")
            L.append("|---|---|")
            for r in b["rows"]:
                L.append(f"| {r['period']} | {r['due']} |")
            L.append("")
        elif b["kind"] == "text":
            L.extend([b["text"], ""])

        if b.get("note"):
            L.extend([f"*{b['note']}*", ""])

    sources = meta.get("sources") or []
    if sources:
        L.extend(["---", "", "**资料来源**", ""])
        for s in sources:
            # report 形如「《本市统计月报》2026年9月」，书名号和期次都已经在里面了
            L.append(f"- {s['department']}　{s['report']}　{s['published_at']} 披露")
        L.append("")

    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------
# 对外
# --------------------------------------------------------------------------
def options(pid: str | None = None) -> dict[str, Any]:
    person = people.meta(pid)
    periods = md.available_periods()
    watched = person["watch"]
    return {
        "ok": True,
        "city": CITY,
        "person": {k: person[k] for k in ("id", "name", "post", "scope", "units", "demo")},
        "presets": [
            {"id": p["id"], "label": p["label"], "hint": p["hint"], "sections": list(p["sections"])}
            for p in PRESETS
        ],
        "sections": [{"id": k, "name": v} for k, v in SECTIONS.items()],
        "periods": [
            {"value": p, "label": f"{p[:4]}年{int(p[5:7])}月"}
            for p in reversed(periods)
        ],
        "default_period": periods[-1] if periods else None,
        "metrics": md.list_metrics(),
        "default_metrics": list(watched),
        "meetings": [
            {"id": m["id"], "title": m["title"], "held_at": m["held_at"], "host": m["host"]}
            for m in list_meetings()
        ],
        "max_metrics": MAX_METRICS,
        "mock": True,
    }


def compose(
    pid: str | None = "mayor",
    *,
    kind: str = "report",
    period: str | None = None,
    sections: list[str] | None = None,
    codes: list[str] | None = None,
    meeting_id: str | None = None,
    use_model: bool = True,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    person = people.meta(pid)
    preset = PRESET_MAP.get(kind) or PRESET_MAP["report"]
    kind = preset["id"]

    # 期次：只能取已披露的。传了未披露的期次就退回最新一期，并在 warnings 里说清楚
    available = md.available_periods(today)
    warnings: list[str] = []
    if not available:
        return {"ok": False, "error": "no_disclosed_period"}
    if period and period not in available:
        warnings.append(f"所选期次 {period} 尚未到披露日，已改取 {available[-1]}。")
        period = None
    period = period or available[-1]
    period_label = f"{period[:4]}年{int(period[5:7])}月"

    units, manual = schedule.scope_of(person)
    # 「含协办」要说出来：局长协办的项目会算进他的台账，不说清就会出现
    # 「统计 2 项、明细里却有别的单位」这种对不上的印象
    scope_text = ("、".join(sorted(units)) + "（含协办）") if units else "全市"

    meetings = [m for m in list_meetings() if schedule.meeting_visible(m, person)]
    chosen = None
    if kind == "minutes":
        # 要取完整记录而不是列表摘要：摘要里只有条目数量，没有条目本身。
        # 之前这里直接用 list_meetings() 的元素，会写成「没有抽取到交办内容」——数据明明在。
        mid = meeting_id or (meetings[0]["id"] if meetings else None)
        chosen = meeting_get(mid) if mid else None
        # 可见性再判一次：指定了别人的会，不能靠前端不显示来兜底
        if chosen and not schedule.meeting_visible(chosen, person):
            warnings.append("指定的会议不在你的参会范围，已改用最近一场。")
            mid = meetings[0]["id"] if meetings else None
            chosen = meeting_get(mid) if mid else None
        if chosen is None:
            warnings.append("没有可用的会议记录，会议落实材料退化为工作汇报。")

    # 指标：明确指定（含空数组）> 会议材料按参会单位匹配 > 身份关注。
    # 空数组和 None 要分开：前者是「我不要指标」，后者是「你替我挑」
    picked: list[str] = []
    if codes is not None and isinstance(codes, list):
        for c in codes:
            c = str(c or "").strip()
            if c in md.METRICS and c not in picked:
                picked.append(c)
    elif kind == "minutes" and chosen:
        attend = [u for u in (chosen.get("attendees") or []) if u]
        for code, meta in md.METRICS.items():
            if any(u in meta["department"] for u in attend) and code not in picked:
                picked.append(code)
        # 参会单位不一定家家发指标（市城管局就没有），只有一两项材料会太薄，
        # 用本人关注的指标补到 4 项，补上来的同样是带出处的最新一期
        for code in person["watch"]:
            if len(picked) >= MIN_METRICS:
                break
            if code in md.METRICS and code not in picked:
                picked.append(code)
    else:
        picked = [c for c in person["watch"] if c in md.METRICS]
    picked = picked[:MAX_METRICS]

    all_items = ov.list_items()
    visible = [it for it in all_items if schedule.item_visible(it, units, manual)]
    st = ov.stats(units or None)

    meeting_items: list[dict[str, Any]] = []
    if kind == "minutes" and chosen:
        # 取全量而不是可见范围：会议材料讲的是「这个会交办了什么」，
        # 有的事项落在别的单位，漏掉就变成了「这个会只交办了半件事」
        meeting_items = [it for it in all_items if it.get("source_ref") == chosen["id"]]

    ctx: dict[str, Any] = {
        "person": person,
        "today": today,
        "period": period,
        "period_label": period_label,
        "kind": kind,
        "codes": picked,
        "units": units,
        "manual": manual,
        "scope_text": scope_text,
        "stats": st,
        "visible": visible,
        "meetings": meetings,
        "meeting": chosen,
        "meeting_items": meeting_items,
    }

    want = _norm_sections(sections, preset["sections"])
    builders = {
        "metrics": _metrics_block,
        "oversight": _oversight_block,
        "meeting": _meeting_block,
        "pending": _pending_block,
    }

    blocks: list[dict[str, Any]] = []
    sources: list[dict[str, str]] = []
    for sid in want:
        if sid == "next":
            continue
        b = builders[sid](ctx)
        if b is None:
            continue
        blocks.append(b)
        for s in b.get("sources") or []:
            if s not in sources:
                sources.append(s)

    if "next" in want:
        # 顺序上「下一步」永远在最后，不跟着勾选顺序飘
        blocks.append(
            {
                "id": "next",
                "title": SECTIONS["next"],
                "lead": "",
                "kind": "text",
                "text": _rule_next(ctx),
                "source": "rule",
                "note": "本节由台账事实生成；模型可改写，但数字必须来自本材料。",
            }
        )

    for i, b in enumerate(blocks):
        b["no"] = _CN[i] if i < len(_CN) else str(i + 1)

    model = _enrich_next(blocks, ctx, use_model=use_model) if any(b["id"] == "next" for b in blocks) else {"source": "rule", "note": "未选用「下一步」章节", "degraded": True}

    titles = {"report": "工作汇报", "minutes": "会议落实材料", "bulletin": "重点工作督办通报"}
    meta = {
        "kind": kind,
        "kind_label": preset["label"],
        "title": f"{CITY}{titles[kind]}",
        "subtitle": f"{period_label}　{scope_text}",
        "city": CITY,
        "person": {k: person[k] for k in ("id", "name", "post", "scope")},
        "period": period,
        "period_label": period_label,
        "generated_at": _now().strftime("%Y-%m-%d %H:%M"),
        "scope_text": scope_text,
        "sources": sources,
        # 实际用了哪些指标要回传：前端据此回显，否则「自动挑的」和「我挑的」分不清
        "metric_codes": picked,
        "mock": True,
    }
    if chosen:
        meta["meeting"] = {
            "id": chosen["id"],
            "title": chosen["title"],
            "held_at": chosen.get("held_at") or "",
            "host": chosen.get("host") or "",
            "attendees": chosen.get("attendees") or [],
        }

    if not blocks:
        warnings.append("按当前选择没有可写入的内容：请检查关注指标、督办范围和期次。")
    elif "metrics" in want and not picked:
        warnings.append("没有选择指标，材料不含指标章节。")

    return {
        "ok": True,
        "meta": meta,
        "blocks": blocks,
        "warnings": warnings,
        "model": model,
        "markdown": _markdown(meta, blocks),
    }


def _norm_sections(raw: Any, default: list[str]) -> list[str]:
    if raw is None:
        return list(default)
    if not isinstance(raw, list):
        return list(default)
    out: list[str] = []
    for s in raw:
        k = str(s or "").strip()
        if k in SECTIONS and k not in out:
            out.append(k)
    # 注意不能 `or default`：空数组是用户「一节都不要」，回落默认会让"取消全选"
    # 反而变成"全选"。没传（None）才用骨架默认。
    return out
