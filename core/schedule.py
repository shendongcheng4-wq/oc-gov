"""AI 日程：把「已经写在别处的时间」排成一条时间线。

**这个模块不产生新的事实。** 每一条日程都必须能顺着 `ref` 点回到它的出处：

- 会议 → `data/meetings.json` 的 `held_at`（有具体钟点）
- 督办时限 → 台账里那条事项的 `deadline`
- 待签收 / 待审核 / 逾期 / 停滞 → 台账状态机算出来的，不是这里判的
- 数据披露 → `mockdata.DISCLOSURE_DAY`（每月 10 日披露上一个月报）

所以它不做「手工建日程」这件事。手工排期等于再造一份只有自己知道的日历，
和台账、会议记录三份时间表迟早对不上；真要加，就得先决定谁是唯一真相。
现在只有一个方向：**账上有时间的事，才会出现在日程里。**

**模型只做两件事**：写一句今日重点，给今天的事排个理由。
它不能新增事项、不能改时间、不能算天数——凡出现清单里没有的数字，整段丢弃
（和专家解读同一道闸）。模型不可用时退回规则文案，页面上如实标注。

身份口径与工作台完全一致（`core/people.py`）：分管单位派生 + 本人手动跟进。
两边如果各写一套过滤，同一个人会在工作台看到 5 件、在日程看到 3 件，没人能解释差在哪。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "oversight", ROOT / "core" / "meeting"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import llmclient  # noqa: E402
import mockdata as md  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
from ledger import list_meetings  # noqa: E402

WINDOW_DEFAULT = 14
WINDOW_MAX = 60

# 事件的种类与配色。tone 直接对应前端 .cal-tag 的修饰类，不在这里写颜色值。
KINDS: dict[str, tuple[str, str]] = {
    "meeting": ("会议", "brand"),
    "sign": ("待签收", "warn"),
    "review": ("待审核", "warn"),
    "overdue": ("已逾期", "danger"),
    "nudge": ("补报进展", "warn"),
    "deadline": ("办结时限", "info"),
    "disclosure": ("数据披露", "muted"),
}

# 今日事项的紧急度基准分。逾期压过会议：会可以迟到，时限过了就是过了。
_SCORE = {"overdue": 95, "sign": 88, "review": 82, "deadline_today": 90, "nudge": 74, "meeting": 70, "disclosure": 30}

_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _now() -> datetime:
    """和督办共用一把钟：`OVERSIGHT_TODAY` 固定日期时，两边的「今天」必须是同一天，
    否则会出现「台账说已逾期、日程说还有 1 天」这种自相矛盾的画面。"""
    return ov._now()  # noqa: SLF001


def _d(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _dt(s: str) -> datetime | None:
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------
# 身份过滤：与 workbench.follow_items 同一口径
# --------------------------------------------------------------------------
def _scope(person: dict[str, Any]) -> tuple[set[str], set[str]]:
    return set(u for u in (person.get("units") or []) if u), set(i for i in (person.get("follow") or []) if i)


def _visible(item: dict[str, Any], units: set[str], manual: set[str]) -> bool:
    if item["id"] in manual:
        return True
    if not units:  # 全市身份：不筛
        return True
    return item["assignee"] in units or bool(units & set(item["co_assignees"]))


def _meeting_visible(m: dict[str, Any], person: dict[str, Any]) -> bool:
    """这场会要不要出现在这个人的日程上。

    `host` 是职务（"常务副市长"）不是单位，所以除了参会单位，还要拿本人的 post 比一次：
    常务副市长主持的会，按单位筛是永远匹配不上的 —— 他会看不到自己主持的会。
    """
    units = set(u for u in (person.get("units") or []) if u)
    if not units:
        return True
    host = (m.get("host") or "").strip()
    post = (person.get("post") or "").strip()
    if host and post and host == post:
        return True
    if host and host in units:
        return True
    attendees = set(m.get("attendees") or [])
    # 没填参会单位的会按「全市性会议」处理 —— 不能因为记录不全就默认领导不用去
    return not attendees or bool(units & attendees)


# --------------------------------------------------------------------------
# 事件生成
# --------------------------------------------------------------------------
def _ev(
    kind: str,
    day: str,
    title: str,
    sub: str,
    *,
    time_s: str = "",
    ref: dict[str, str] | None = None,
    why: str = "",
    score: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label, tone = KINDS[kind]
    return {
        "id": "",
        "kind": kind,
        "kind_label": label,
        "tone": tone,
        "date": day,
        "time": time_s,
        "title": title,
        "sub": sub,
        "why": why,
        "ref": ref or {},
        "score": score,
        "extra": extra or {},
    }


def _today_action(it: dict[str, Any], today: date) -> dict[str, Any] | None:
    """一条在办事项今天要做的动作。一条事项最多出一个动作，取最靠前的那种。

    为什么要合并：同一条事项可能既逾期又停滞，拆成两条在「今天」段里连着出现两次，
    看起来像两件不同的事，用户还得自己对一遍编号。
    """
    ref = {"type": "oversight", "id": it["id"]}
    who = it["assignee"]
    if it["status"] == ov.PENDING:
        return _ev(
            "sign",
            today.isoformat(),
            it["title"],
            f"{who} · 交办未签收" + (f" · 已催 {it['remind_count']} 次" if it["remind_count"] else ""),
            ref=ref,
            why="交办后 24 小时内必须签收，未签收台账无法进入办理状态",
            score=_SCORE["sign"],
        )
    if it["status"] == ov.REVIEW:
        return _ev(
            "review",
            today.isoformat(),
            it["title"],
            f"{who} · 已提交办结，待审核",
            ref=ref,
            why="审核通过才算办结；压着不审，台账上的时限会先到",
            score=_SCORE["review"],
        )
    if it["days_left"] < 0:
        return _ev(
            "overdue",
            today.isoformat(),
            it["title"],
            f"{who} · 原定 {it['deadline']} 办结，已逾 {-it['days_left']} 天 · 进度 {it['progress']}%",
            ref=ref,
            why="逾期事项每个工作日都要有书面进展，否则只能靠催办推动",
            score=_SCORE["overdue"],
            extra={"origin_deadline": it["deadline"]},
        )
    if it["risk"] == ov.RISK_STALL:
        return _ev(
            "nudge",
            today.isoformat(),
            it["title"],
            f"{who} · 已 {it['idle_days']} 天没有新进展 · 进度 {it['progress']}%",
            ref=ref,
            why="停滞超过一周，进度数据已经不能反映现场",
            score=_SCORE["nudge"],
        )
    return None


def _events(person: dict[str, Any], days: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    now = _now()
    today = now.date()
    horizon = today + timedelta(days=max(0, days - 1))
    units, manual = _scope(person)
    out: list[dict[str, Any]] = []

    items = [it for it in ov.list_items() if it["status"] != ov.CLOSED and _visible(it, units, manual)]
    acted: set[str] = set()
    for it in items:
        act = _today_action(it, today)
        if act is None:
            continue
        # 今天已经排了动作的事项，如果时限也在今天，就只留动作那一条 ——
        # 同一件事在同一天出现两行，会被当成两件事。时限在别的日子照常出现：
        # 「今天先签收」和「20 号要办结」是两个不同的义务日，不是重复。
        due = _d(it["deadline"])
        if due == today:
            acted.add(it["id"])
        out.append(act)

    for it in items:
        if it["id"] in acted:
            continue
        due = _d(it["deadline"])
        if due is None or due < today or due > horizon:
            continue
        left = (due - today).days
        is_today = left == 0
        out.append(
            _ev(
                "deadline",
                due.isoformat(),
                it["title"],
                f"{it['assignee']} · {it['status']} · 进度 {it['progress']}%",
                ref={"type": "oversight", "id": it["id"]},
                why=("今天到期，尚未办结" if is_today else f"距时限还有 {left} 天，当前进度 {it['progress']}%"),
                score=_SCORE["deadline_today"] if is_today else 40 + (days - left),
                extra={"days_left": left},
            )
        )

    # 会议：已经结束的不排（14:30 的会 16:40 还挂在「今天」上，只会让人怀疑日程没刷新）
    for m in list_meetings():
        start = _dt(m.get("held_at") or "")
        if start is None or not _meeting_visible(m, person):
            continue
        if start + timedelta(hours=2) < now:
            continue
        if start.date() > horizon:
            continue
        pending = m.get("needs_review_count") or 0
        bits = [f"主持 {m['host']}" if m.get("host") else ""]
        if m.get("attendees"):
            bits.append("参会 " + "、".join(m["attendees"][:4]) + ("等" if len(m["attendees"]) > 4 else ""))
        if m.get("item_count"):
            bits.append(f"{m['item_count']} 条议定事项" + (f"、{pending} 条待核实" if pending else ""))
        mins = int((start - now).total_seconds() // 60)
        why = (
            f"{mins} 分钟后开始"
            if 0 <= mins <= 120
            else ("正在进行" if mins < 0 else "会议时间已定，材料需提前准备")
        )
        out.append(
            _ev(
                "meeting",
                start.date().isoformat(),
                m["title"],
                " · ".join([b for b in bits if b]),
                time_s=start.strftime("%H:%M"),
                ref={"type": "meeting", "id": m["id"]},
                why=why,
                score=_SCORE["meeting"] + (30 if 0 <= mins <= 120 else 0),
                extra={"minutes_to": mins},
            )
        )

    # 数据披露：每月 10 日披露上一个月报。这是唯一一个「周期节点」，
    # 不挂在任何一条事项上，但确实是领导要等的时间。
    disc = _next_disclosure(today)
    if disc <= horizon:
        y, mo = (disc.year, disc.month - 1) if disc.month > 1 else (disc.year - 1, 12)
        left = (disc - today).days
        out.append(
            _ev(
                "disclosure",
                disc.isoformat(),
                f"{mo} 月统计月报披露",
                "市统计局 · 《本市统计月报》按月披露，次月 10 日发布",
                why=("今天可查上月数据" if left == 0 else f"{left} 天后可查上月数据，问数会用到这一期"),
                score=_SCORE["disclosure"],
                extra={"days_left": left},
            )
        )

    for i, e in enumerate(out):
        e["id"] = f"{e['kind']}::{e['ref'].get('id') or e['date']}::{i}"
    out.sort(key=lambda e: (e["date"], -e["score"], e["time"] or "00:00"))
    agenda = _group(out, today)
    return out, agenda


def _next_disclosure(today: date) -> date:
    d = date(today.year, today.month, md.DISCLOSURE_DAY)
    if d > today:
        return d
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, md.DISCLOSURE_DAY)


def _group(events: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_date.setdefault(e["date"], []).append(e)
    for day in sorted(by_date):
        d = _d(day)
        if d is None:
            continue
        delta = (d - today).days
        label = "今天" if delta == 0 else "明天" if delta == 1 else "后天" if delta == 2 else _WEEK[d.weekday()]
        groups.append(
            {
                "date": day,
                "label": label,
                "weekday": _WEEK[d.weekday()],
                "month_day": f"{d.month}月{d.day}日",
                "is_today": delta == 0,
                "days_from_today": delta,
                "items": by_date[day],
            }
        )
    return groups


# --------------------------------------------------------------------------
# 对外：日程本身
# --------------------------------------------------------------------------
def build(person_id: str | None = "mayor", days: int = WINDOW_DEFAULT) -> dict[str, Any]:
    person = people.meta(person_id)
    days = max(1, min(WINDOW_MAX, int(days or WINDOW_DEFAULT)))
    events, groups = _events(person, days)
    now = _now()
    today = now.date()
    todays = [e for e in events if e["date"] == today.isoformat()]
    units, _ = _scope(person)
    return {
        "city": "溧阳市",
        "person": {k: person[k] for k in ("id", "name", "post", "scope", "units", "demo")},
        "today": today.isoformat(),
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "window": {"days": days, "from": today.isoformat(), "to": (today + timedelta(days=days - 1)).isoformat()},
        "groups": groups,
        "summary": {
            "total": len(events),
            "today": len(todays),
            "overdue": sum(1 for e in events if e["kind"] == "overdue"),
            "meetings": sum(1 for e in events if e["kind"] == "meeting"),
            "days_with_events": len(groups),
            # 今天第一条就是最该先办的：排序已经把逾期、签收、临近会议顶到前面
            "lead": todays[0]["title"] if todays else "",
        },
        # 口径写在数据里，前端照抄，不在两处各写一份文案
        "basis": ("分管单位：" + "、".join(units)) if units else "全市（不按单位筛）",
        "rule": "日程只收已有时间的事：督办时限与状态、会议时间、统计数据披露日。没有手工排期。",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "mock": True,
    }


# --------------------------------------------------------------------------
# 对外：AI 简报（模型只写话，不排期）
# --------------------------------------------------------------------------
_SYS = """你是市长办公室的日程参谋。输入是一份已经算好的日程清单（JSON），你要做两件事：

1. 写一句「今日重点」，不超过 60 个字，说清今天先办哪一件、为什么。
2. 给「今天」的每件事写一句不超过 30 个字的理由，说清它在今天的排序位置。

硬规则（违反任何一条，你的输出会被整段丢弃）：
- 只能使用清单里出现过的事项、单位、日期、状态、进度。
- 只能使用清单里出现过的数字，不许自己算天数、算比例、算合计。
- 不许新增清单里没有的事项或建议去问别人。
- 不用「建议」「可以考虑」「或许」这类词，直接下判断。
- 只输出 JSON：{"brief": "...", "why": {"<事件id>": "..."}}"""


def _nums(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text or ""))


def _rule_brief(agenda: dict[str, Any]) -> dict[str, Any]:
    """模型不在时的兜底：只复述排序结果，不解释、不评价。"""
    s = agenda["summary"]
    todays = agenda["groups"][0]["items"] if agenda["groups"] and agenda["groups"][0]["is_today"] else []
    if not todays:
        nxt = agenda["groups"][0] if agenda["groups"] else None
        text = (
            f"今天没有排期。下一件是 {nxt['month_day']} 的「{nxt['items'][0]['title']}」。"
            if nxt
            else "未来这段时间没有排期。"
        )
        return {"brief": text, "why": {}, "source": "rule"}
    head = todays[0]
    text = f"今天 {len(todays)} 件事。先办「{head['title']}」——{head['why']}。"
    return {"brief": text, "why": {e["id"]: e["why"] for e in todays}, "source": "rule"}


def brief(person_id: str | None = "mayor", days: int = WINDOW_DEFAULT, *, use_model: bool = True) -> dict[str, Any]:
    agenda = build(person_id, days)
    rule = _rule_brief(agenda)
    base = {
        "ok": True,
        "brief": rule["brief"],
        "why": rule["why"],
        "source": "rule",
        "degraded": True,
        "model": None,
        "agenda": {"today": agenda["today"], "summary": agenda["summary"]},
    }
    if not use_model or not llmclient.available():
        base["note"] = "未配置模型，以上按台账时限排序" if not llmclient.available() else "本次只取规则排序"
        return base

    todays = agenda["groups"][0]["items"] if agenda["groups"] and agenda["groups"][0]["is_today"] else []
    if not todays:
        base["note"] = "今天没有排期，不需要模型"
        return base

    # 只喂今天 + 最近几天，别把两周的清单整包塞进去：上下文越长，「编一条」的概率越高
    picked = [e for g in agenda["groups"] for e in g["items"]][:12]
    payload = {
        "today": agenda["today"],
        "now": agenda["now"],
        "counts": {"today": agenda["summary"]["today"], "overdue": agenda["summary"]["overdue"]},
        "events": [
            {
                "id": e["id"],
                "date": e["date"],
                "time": e["time"],
                "kind": e["kind_label"],
                "title": e["title"],
                "detail": e["sub"],
                "reason": e["why"],
            }
            for e in picked
        ],
    }
    allowed: set[str] = set()
    for e in picked:
        allowed |= _nums(" ".join([e["title"], e["sub"], e["why"], e["date"], e["time"]]))
    allowed |= {str(v) for v in payload["counts"].values()}
    allowed |= _nums(agenda["today"])

    try:
        res = llmclient.chat_json(_SYS, json.dumps(payload, ensure_ascii=False), timeout=45, max_tokens=500)
    except (llmclient.ModelUnavailable, llmclient.ModelError) as exc:
        base["note"] = f"模型调用失败（{type(exc).__name__}），已退回规则排序"
        return base

    data = res.get("data") or {}
    text = str(data.get("brief") or "").strip()
    why_in = data.get("why") if isinstance(data.get("why"), dict) else {}
    ids = {e["id"] for e in picked}

    dropped: list[str] = []
    if not text or _nums(text) - allowed:
        # 数字闸：宁可显示规则文案，也不显示一个「3 天前该办完」这种模型自己算出来的话
        base["note"] = "模型输出含清单外的数字，已丢弃" if text else "模型未给出简报，已退回规则排序"
        if text:
            dropped.append("brief")
        return base

    why: dict[str, str] = {}
    for k, v in why_in.items():
        v = str(v or "").strip()
        if k not in ids or not v or _nums(v) - allowed:
            if k in ids and v:
                dropped.append(k)
            continue
        why[k] = v
    # 模型漏掉的今天事项用规则理由补上，不留空行
    for e in todays:
        why.setdefault(e["id"], e["why"])

    return {
        **base,
        "brief": text,
        "why": why,
        "source": "model",
        "degraded": False,
        "model": res.get("model"),
        "model_ref": res.get("model_ref"),
        "dropped": dropped,
    }
