"""督办事项：建档 → 流转 → 催办 → 成报。

三条设计原则，和问数原型保持一致：

1. **状态和数字由代码算，模型只负责组织语言。**
   模型可以写周报的分析段落，但不能决定某件事是不是"已办结"，也不能给出逾期天数。
2. **派生风险不可写入。** "逾期""临期""签收超时""停滞"都由日期和进展推导，
   没有哪个接口能把它设成某个值——否则督办就变成了自己给自己打分。
3. **每次流转都留痕。** 谁、什么时候、从什么状态到什么状态、依据什么理由，
   全部写进事件流，事后可追。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("OCGOV_DATA") or (Path(__file__).resolve().parents[2] / "data"))
STORE = DATA_DIR / "oversight-state.json"

# 台账是进程内共享的 dict，而 HTTP 是多线程的。一次流转要做「读状态 → 判守卫 →
# 改状态 → 追加事件 → 落盘」，中间被别的线程插进来就会出现两条签收、状态回退。
# 用可重入锁把整段包住；advance 内部会调 get()/assess()，可重入才不会自锁。
_LOCK = threading.RLock()

# ── 状态 ──────────────────────────────────────────────────────────────────
PENDING = "待签收"
WORKING = "办理中"
REVIEW = "待审核"
CLOSED = "已办结"

STATUSES = [PENDING, WORKING, REVIEW, CLOSED]

# 风险等级，用于排序和配色
RISK_OVERDUE = "逾期"
RISK_TODAY = "今日到期"
RISK_SOON = "临期"
RISK_STALL = "停滞"
RISK_UNSIGNED = "签收超时"
RISK_NONE = "正常"

SIGN_TIMEOUT_HOURS = 24  # 交办后多久没签收算超时
SOON_DAYS = 3            # 距时限几天内算临期
STALL_DAYS = 7           # 多久没有新进展算停滞


def _now() -> datetime:
    """当前时间。设 OVERSIGHT_TODAY 可固定"今天"，便于复现和截图。"""
    fixed = os.environ.get("OVERSIGHT_TODAY", "").strip()
    if fixed:
        d = date.fromisoformat(fixed)
        return datetime.combine(d, time(10, 0))
    return datetime.now()


def today() -> date:
    return _now().date()


# ── 数据结构 ──────────────────────────────────────────────────────────────
@dataclass
class Event:
    """事项事件流的一条。进展、流转、催办都记在这里，按时间顺排。"""

    ts: str
    type: str            # 创建 / 签收 / 进展 / 提交办结 / 确认办结 / 退回 / 催办
    actor: str
    text: str = ""
    from_status: str | None = None
    to_status: str | None = None
    percent: int | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass
class Item:
    id: str
    title: str
    source: str               # 事项来源：常务会议定事项 / 领导批示 / 12345 集中诉求 …
    source_ref: str           # 来源文号
    assignee: str             # 责任单位
    co_assignees: list[str]   # 协办单位
    issued_at: str            # 交办时间 "YYYY-MM-DD HH:MM"
    deadline: str             # 办结时限 "YYYY-MM-DD"
    standard: str             # 办结标准（可验证的一句话）
    priority: str             # 重点 / 一般
    status: str
    events: list[Event] = field(default_factory=list)

    # ── 派生量 ────────────────────────────────────────────────
    @property
    def deadline_date(self) -> date:
        return date.fromisoformat(self.deadline)

    @property
    def issued_dt(self) -> datetime:
        return datetime.fromisoformat(self.issued_at)

    @property
    def days_left(self) -> int:
        """距时限天数。负数=已逾期。"""
        return (self.deadline_date - today()).days

    @property
    def signed(self) -> bool:
        return any(e.type == "签收" for e in self.events)

    @property
    def last_activity(self) -> datetime:
        """最后一条非催办事件的时间。催办不算进展，否则永远"不停滞"。"""
        real = [e for e in self.events if e.type != "催办"]
        if not real:
            return self.issued_dt
        return datetime.fromisoformat(max(e.ts for e in real))

    @property
    def idle_days(self) -> int:
        return max(0, (_now() - self.last_activity).days)

    @property
    def progress_percent(self) -> int:
        pcts = [e.percent for e in self.events if e.type == "进展" and e.percent is not None]
        return pcts[-1] if pcts else 0

    @property
    def remind_count(self) -> int:
        return sum(1 for e in self.events if e.type == "催办")


@dataclass
class Risk:
    """一条事项的风险判定。全部由日期和事件推导，不接受外部赋值。"""

    level: str
    level_rank: int      # 越小越紧急，用于排序
    days_left: int
    idle_days: int
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess(item: Item) -> Risk:
    """判定风险等级。一条事项只取最严重的那个作为主等级，其余写进理由。"""
    reasons: list[str] = []
    days = item.days_left
    idle = item.idle_days

    if item.status == CLOSED:
        return Risk(RISK_NONE, 90, days, idle, ["已办结"])

    overdue = days < 0
    due_today = days == 0
    soon = 0 <= days <= SOON_DAYS
    unsigned = item.status == PENDING and (_now() - item.issued_dt) > timedelta(hours=SIGN_TIMEOUT_HOURS)
    stalled = item.status == WORKING and idle > STALL_DAYS

    if overdue:
        level, rank = RISK_OVERDUE, 0
        reasons.append(f"已逾期 {-days} 天")
    elif due_today:
        level, rank = RISK_TODAY, 1
        reasons.append("今日到期，尚未办结")
    elif soon:
        level, rank = RISK_SOON, 2
        reasons.append(f"距时限还有 {days} 天")
    elif unsigned:
        level, rank = RISK_UNSIGNED, 3
        reasons.append("交办超过 24 小时未签收")
    elif stalled:
        level, rank = RISK_STALL, 4
        reasons.append(f"已 {idle} 天没有新进展")
    else:
        level, rank = RISK_NONE, 90

    # 次级理由也一并列出，报告里更有说服力
    if unsigned and level != RISK_UNSIGNED:
        reasons.append("仍未签收")
    if stalled and level != RISK_STALL:
        reasons.append(f"{idle} 天无新进展")
    if item.remind_count >= 2 and level not in (RISK_NONE,):
        reasons.append(f"已催办 {item.remind_count} 次")

    return Risk(level, rank, days, idle, reasons)


# ── 流转守卫 ──────────────────────────────────────────────────────────────
# action: (允许的起始状态, 目标状态, 是否必须填理由)
TRANSITIONS: dict[str, tuple[set[str], str | None, bool]] = {
    "签收": ({PENDING}, WORKING, False),
    "反馈进展": ({WORKING}, WORKING, False),
    "提交办结": ({WORKING}, REVIEW, False),
    "确认办结": ({REVIEW}, CLOSED, False),
    "退回": ({REVIEW}, WORKING, True),
    "催办": ({PENDING, WORKING, REVIEW}, None, False),
}


def _guard(item: Item, action: str, reason: str, evidence: list[str], text: str) -> str | None:
    """返回拒绝原因；通过则返回 None。所有业务规则集中在这里。"""
    if action not in TRANSITIONS:
        return f"未知操作「{action}」，可用的是：{'、'.join(TRANSITIONS)}"

    allowed_from, _, need_reason = TRANSITIONS[action]
    if item.status not in allowed_from:
        return (
            f"当前状态为「{item.status}」，不能执行「{action}」。"
            f"该操作只允许在 {'、'.join(sorted(allowed_from))} 状态下发起。"
        )

    if need_reason and not reason.strip():
        return f"「{action}」必须写明理由，否则责任单位无法知道要补什么。"

    if action == "提交办结":
        progress = [e for e in item.events if e.type == "进展"]
        if not progress:
            return "提交办结前至少要有一条进展记录——否则无法说明办了什么。"
        if not evidence:
            return f"提交办结必须附佐证材料（对应办结标准：{item.standard}）。"
        if not text.strip():
            return "提交办结要写办结说明，说明如何达到办结标准。"

    if action == "反馈进展" and not text.strip():
        return "进展内容不能为空。"

    return None


def advance(
    item_id: str,
    action: str,
    actor: str,
    text: str = "",
    reason: str = "",
    evidence: list[str] | None = None,
    percent: int | None = None,
) -> dict[str, Any]:
    """执行一次流转。守卫不通过时返回 ok=False + 拒绝原因，且不落任何数据。"""
    if percent is not None:
        try:
            percent = max(0, min(100, int(percent)))
        except (TypeError, ValueError):
            percent = None

    with _LOCK:
        item = get(item_id)
        if item is None:
            return {"ok": False, "error": f"没有编号为 {item_id} 的事项。"}

        # 「退回」只认 reason，但调用方（HTTP 界面、MCP 工具、脚本）习惯把理由
        # 写在 text 里。补齐放在这里而不是每个入口各补一次——补漏一处，用户就会
        # 遇到"明明填了理由还被拒"。
        if text and not reason:
            reason = text

        evidence = evidence or []
        reject = _guard(item, action, reason, evidence, text)
        if reject:
            return {
                "ok": False,
                "error": reject,
                "item_id": item_id,
                "current_status": item.status,
                "allowed_actions": allowed_actions(item),
            }

        before = item.status
        event_type = action
        target = TRANSITIONS[action][1]
        if target:
            item.status = target

        if action == "催办":
            note = text or reason or "时限临近，请加快办理并反馈进展。"
            item.events.append(
                Event(ts=_now().strftime("%Y-%m-%d %H:%M"), type="催办", actor=actor, text=note)
            )
        elif action == "反馈进展":
            item.events.append(
                Event(
                    ts=_now().strftime("%Y-%m-%d %H:%M"),
                    type="进展",
                    actor=actor,
                    text=text,
                    percent=percent,
                    evidence=evidence,
                )
            )
        else:
            body = text or reason
            item.events.append(
                Event(
                    ts=_now().strftime("%Y-%m-%d %H:%M"),
                    type=event_type,
                    actor=actor,
                    text=body,
                    from_status=before,
                    to_status=item.status,
                    evidence=evidence,
                )
            )

        _save()
        return {
            "ok": True,
            "item_id": item_id,
            "action": action,
            "from_status": before,
            "to_status": item.status,
            "changed": before != item.status,
            "risk": assess(item).to_dict(),
        }


def allowed_actions(item: Item) -> list[str]:
    """当前状态下可执行的操作，前端据此决定按钮是否可点。"""
    if item.status == CLOSED:
        return []
    out = []
    for action, (allowed_from, _, _) in TRANSITIONS.items():
        if item.status in allowed_from:
            out.append(action)
    return out


# ── 建档与查询 ────────────────────────────────────────────────────────────
_ITEMS: dict[str, Item] = {}


def _save() -> None:
    """落盘。网页服务和 MCP 服务是两个进程，靠这个文件看到同一块台账。

    先写临时文件再 replace，避免读到写了一半的 JSON。
    """
    payload = {"items": [asdict(i) for i in _ITEMS.values()]}
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STORE)


def _load_or_seed() -> None:
    """有存档就接着用，没有（或强制重置）就用种子数据。"""
    if STORE.exists() and os.environ.get("OVERSIGHT_RESET") != "1":
        try:
            raw = json.loads(STORE.read_text(encoding="utf-8"))
            for row in raw.get("items", []):
                events = [Event(**e) for e in row.pop("events", [])]
                item = Item(**row, events=events)
                _ITEMS[item.id] = item
            if _ITEMS:
                return
        except Exception:
            pass  # 存档坏了就重新播种，不要因为演示数据损坏起不来
    _ITEMS.clear()
    _seed()
    _save()


def create_item(
    title: str,
    assignee: str,
    deadline: str,
    standard: str,
    source: str = "市政府办交办",
    source_ref: str = "",
    co_assignees: list[str] | None = None,
    priority: str = "一般",
    actor: str = "市政府办",
) -> dict[str, Any]:
    """新建督办事项。编号按年份自增，从 X001 开始。"""
    if not title.strip():
        return {"ok": False, "error": "事项标题不能为空。"}
    if not standard.strip():
        return {"ok": False, "error": "办结标准不能为空——没有可验证的办结标准，督办就没有终点。"}
    try:
        dl = date.fromisoformat(deadline)
    except ValueError:
        return {"ok": False, "error": f"时限格式应为 YYYY-MM-DD，收到 {deadline!r}。"}
    if dl < today():
        return {"ok": False, "error": f"时限 {deadline} 早于今天，新建事项不能一开始就是逾期状态。"}

    with _LOCK:
        year = today().year
        used = [
            int(i.id.split("-")[1])
            for i in _ITEMS.values()
            if i.id.startswith(f"DB{year}-") and i.id.split("-")[1].isdigit()
        ]
        seq = (max(used) + 1) if used else 1
        item_id = f"DB{year}-{seq:04d}"
        item = Item(
            id=item_id,
            title=title.strip(),
            source=source,
            source_ref=source_ref,
            assignee=assignee,
            co_assignees=co_assignees or [],
            issued_at=_now().strftime("%Y-%m-%d %H:%M"),
            deadline=deadline,
            standard=standard.strip(),
            priority=priority,
            status=PENDING,
            events=[
                Event(
                    ts=_now().strftime("%Y-%m-%d %H:%M"),
                    type="创建",
                    actor=actor,
                    text=f"交办至 {assignee}，要求 {deadline} 前达到：{standard.strip()}",
                    to_status=PENDING,
                )
            ],
        )
        _ITEMS[item_id] = item
        _save()
        return {"ok": True, "item_id": item_id, "status": item.status}


def get(item_id: str) -> Item | None:
    return _ITEMS.get(item_id)


def list_items(
    status: str | None = None,
    assignee: str | None = None,
    only_risk: bool = False,
) -> list[dict[str, Any]]:
    """按条件筛选，按风险紧急度排序。"""
    rows = []
    for item in _ITEMS.values():
        if status and item.status != status:
            continue
        if assignee and item.assignee != assignee and assignee not in item.co_assignees:
            continue
        risk = assess(item)
        if only_risk and risk.level == RISK_NONE:
            continue
        rows.append(_brief(item, risk))
    rows.sort(key=lambda r: (r["risk_rank"], r["days_left"]))
    return rows


def _brief(item: Item, risk: Risk) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "assignee": item.assignee,
        "co_assignees": item.co_assignees,
        "deadline": item.deadline,
        "priority": item.priority,
        "status": item.status,
        "progress": item.progress_percent,
        "days_left": item.days_left,
        "idle_days": item.idle_days,
        "remind_count": item.remind_count,
        "risk": risk.level,
        "risk_rank": risk.level_rank,
        "risk_reasons": risk.reasons,
        "last_progress_at": item.last_activity.strftime("%Y-%m-%d %H:%M"),
        "last_event_at": item.events[-1].ts if item.events else None,
        "allowed_actions": allowed_actions(item),
        # 来源两件套：材料里要写「这件是谁交办的」，会前材料还要按会议编号反查
        "source": item.source,
        "source_ref": item.source_ref,
    }


def detail(item_id: str) -> dict[str, Any] | None:
    item = get(item_id)
    if item is None:
        return None
    risk = assess(item)
    return {
        **_brief(item, risk),
        "source": item.source,
        "source_ref": item.source_ref,
        "issued_at": item.issued_at,
        "standard": item.standard,
        "risk_detail": risk.to_dict(),
        "events": [asdict(e) for e in item.events],
    }


# ── 催办扫描 ──────────────────────────────────────────────────────────────
def scan(days: int = SOON_DAYS) -> dict[str, Any]:
    """扫描需要催办的事项。这是定时任务每天该调用的那个函数。

    注意 days 只影响"临期"窗口，逾期和停滞的判定是固定的——
    不允许通过放大窗口来让逾期事项"消失"。
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        "逾期": [], "今日到期": [], "临期": [], "签收超时": [], "停滞": [],
    }
    for item in _ITEMS.values():
        if item.status == CLOSED:
            continue
        risk = assess(item)
        row = _brief(item, risk)
        d = item.days_left
        if d < 0:
            buckets["逾期"].append(row)
        elif d == 0:
            buckets["今日到期"].append(row)
        elif d <= days:
            buckets["临期"].append(row)
        if item.status == PENDING and (_now() - item.issued_dt) > timedelta(hours=SIGN_TIMEOUT_HOURS):
            buckets["签收超时"].append(row)
        if item.status == WORKING and item.idle_days > STALL_DAYS:
            buckets["停滞"].append(row)

    total = sum(len(v) for v in buckets.values())
    need = len({r["id"] for v in buckets.values() for r in v})
    return {
        "scanned_at": _now().strftime("%Y-%m-%d %H:%M"),
        "today": today().isoformat(),
        "window_days": days,
        "open_items": sum(1 for i in _ITEMS.values() if i.status != CLOSED),
        "hit_count": total,
        "item_count": need,
        "buckets": buckets,
        "headline": (
            f"扫描 {sum(1 for i in _ITEMS.values() if i.status != CLOSED)} 件在办事项，"
            f"{need} 件需要催办。"
        ),
    }


# ── 成报 ──────────────────────────────────────────────────────────────────
def stats(scope: set[str] | None = None) -> dict[str, Any]:
    """统计口径全部在代码里，不由模型生成。

    `scope` 是责任单位集合：传 None 就是全市台账（默认，行为不变），
    传分管单位就是那个人视野内的账。分管领导和市长看到的「逾期 2 项」
    本来就该不一样 —— 材料里如果两边都写全市数，局长的汇报会变成他不负责的事。
    """
    # 协办单位也算数：材料里写「本单位 1 项」，明细却列出 2 项，是最容易被当场问住的那种错
    items = [
        i for i in _ITEMS.values()
        if scope is None or i.assignee in scope or (scope & set(i.co_assignees))
    ]
    open_items = [i for i in items if i.status != CLOSED]
    closed = [i for i in items if i.status == CLOSED]

    by_status = {s: 0 for s in STATUSES}
    for i in items:
        by_status[i.status] = by_status.get(i.status, 0) + 1

    by_assignee: dict[str, dict[str, int]] = {}
    for i in items:
        row = by_assignee.setdefault(i.assignee, {"在办": 0, "已办结": 0, "逾期": 0})
        if i.status == CLOSED:
            row["已办结"] += 1
        else:
            row["在办"] += 1
            if i.days_left < 0:
                row["逾期"] += 1

    # 按期办结：实际办结日 <= 时限
    on_time = 0
    closed_on_time = []
    for i in closed:
        close_ev = next((e for e in i.events if e.type == "确认办结"), None)
        if close_ev and date.fromisoformat(close_ev.ts[:10]) <= i.deadline_date:
            on_time += 1
            closed_on_time.append(i.id)

    overdue = [i for i in open_items if i.days_left < 0]
    soon = [i for i in open_items if 0 <= i.days_left <= SOON_DAYS]
    stalled = [i for i in open_items if i.status == WORKING and i.idle_days > STALL_DAYS]

    return {
        "total": len(items),
        "open": len(open_items),
        "closed": len(closed),
        "by_status": by_status,
        "by_assignee": by_assignee,
        "on_time_closed": on_time,
        "on_time_rate": round(on_time / len(closed) * 100, 1) if closed else None,
        "overdue": len(overdue),
        "overdue_ids": [i.id for i in overdue],
        "due_soon": len(soon),
        "due_soon_ids": [i.id for i in soon],
        "stalled": len(stalled),
        "stalled_ids": [i.id for i in stalled],
        "avg_idle_days": round(sum(i.idle_days for i in open_items) / len(open_items), 1) if open_items else 0,
    }


def report(days: int = SOON_DAYS) -> dict[str, Any]:
    """督办周报的确定性部分：统计 + 逐项明细 + 风险清单。

    返回里没有一句话是模型写的。需要行文时由上层调模型，且模型只能引用这里的数据。
    """
    st = stats()
    sc = scan(days)
    rows = list_items()
    lines: list[str] = []

    lines.append(
        f"截至 {today().isoformat()}，在办督办事项 {st['open']} 件，"
        f"已办结 {st['closed']} 件，按期办结率 "
        f"{str(st['on_time_rate']) + '%' if st['on_time_rate'] is not None else '（暂无样本）'}。"
    )
    if st["overdue"]:
        names = "、".join(
            f"{i.assignee}（{i.id}）" for i in _ITEMS.values() if i.id in st["overdue_ids"]
        )
        lines.append(f"逾期 {st['overdue']} 件：{names}。")
    if st["due_soon"]:
        lines.append(f"{days} 日内到期 {st['due_soon']} 件，需提前介入。")
    if st["stalled"]:
        ids = "、".join(st["stalled_ids"])
        lines.append(f"{STALL_DAYS} 日以上无进展 {st['stalled']} 件：{ids}。")

    return {
        "period": f"{today().isoformat()} 周报",
        "generated_at": _now().strftime("%Y-%m-%d %H:%M"),
        "stats": st,
        "risk_scan": sc,
        "items": rows,
        "auto_lines": lines,
        "note": "以上数字与事实全部由督办系统按库内数据计算，未经模型改写。",
    }


# ── 种子数据 ──────────────────────────────────────────────────────────────
def _seed() -> None:
    """8 条示例事项，刻意覆盖每一类风险：逾期、今日到期、临期、停滞、签收超时、已办结。"""
    seed = [
        dict(
            id="DB2026-0018",
            title="第二批老旧小区改造项目开工",
            source="市政府第 9 次常务会议定事项",
            source_ref="常务会议纪要〔2026〕9 号 第 3 项",
            assignee="市住建局",
            co_assignees=["云山区政府", "临江区政府"],
            issued_at="2026-08-20 09:30",
            deadline="2026-09-30",
            standard="第二批 46 个小区全部进场施工，开工率达到 100%",
            priority="重点",
            status=CLOSED,
            events=[
                Event(ts="2026-08-20 09:30", type="创建", actor="市政府办",
                      text="交办至市住建局，要求 2026-09-30 前开工率 100%", to_status=PENDING),
                Event(ts="2026-08-20 14:10", type="签收", actor="市住建局 王建国",
                      text="已收悉，本周内完成施工图审查", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-09-05 17:20", type="进展", actor="市住建局 王建国",
                      text="30 个小区已进场，其余 16 个正在办理施工许可", percent=65),
                Event(ts="2026-09-24 16:40", type="进展", actor="市住建局 王建国",
                      text="46 个小区全部进场施工，开工率 100%", percent=100,
                      evidence=["开工项目清单.xlsx", "现场照片合集.pdf"]),
                Event(ts="2026-09-25 10:05", type="提交办结", actor="市住建局 王建国",
                      text="46 个小区已全部进场，符合办结标准", from_status=WORKING, to_status=REVIEW,
                      evidence=["开工项目清单.xlsx", "现场照片合集.pdf"]),
                Event(ts="2026-09-26 09:15", type="确认办结", actor="市政府办 李敏",
                      text="现场抽查 6 个小区，均已开工，同意办结", from_status=REVIEW, to_status=CLOSED),
            ],
        ),
        dict(
            id="DB2026-0025",
            title="城市运行管理服务平台数据接入",
            source="市政府主要领导批示",
            source_ref="批示件〔2026〕187 号",
            assignee="市大数据局",
            co_assignees=["市城管局", "市交通运输局", "市水务局"],
            issued_at="2026-09-28 10:00",
            deadline="2026-11-15",
            standard="城管、交通、水务三个部门实时数据接入平台，接口联通率 100%",
            priority="重点",
            status=WORKING,
            events=[
                Event(ts="2026-09-28 10:00", type="创建", actor="市政府办",
                      text="交办至市大数据局，要求 2026-11-15 前三部门数据全量接入", to_status=PENDING),
                Event(ts="2026-09-28 15:30", type="签收", actor="市大数据局 陈涛",
                      text="已收悉，需协调三个部门开放接口", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-10-09 11:20", type="进展", actor="市大数据局 陈涛",
                      text="城管已接入 12 个接口；交通、水务仍在协调数据共享协议", percent=35),
                Event(ts="2026-10-20 16:00", type="进展", actor="市大数据局 陈涛",
                      text="城管接口全部联通；交通反馈需上级审批，水务称数据在省级系统", percent=45),
                Event(ts="2026-10-26 09:00", type="催办", actor="市政府办 李敏",
                      text="距时限 20 天，进度 45%，请说明交通、水务接入的具体障碍和时间安排"),
            ],
        ),
        dict(
            id="DB2026-0027",
            title="高铁东站周边路网配套工程",
            source="市政府第 12 次常务会议定事项",
            source_ref="常务会议纪要〔2026〕12 号 第 7 项",
            assignee="市住建局",
            co_assignees=["市交通运输局", "云山区政府"],
            issued_at="2026-09-18 09:00",
            deadline="2026-10-28",
            standard="东站北广场及 4 条接驳道路完成交工验收",
            priority="重点",
            status=WORKING,
            events=[
                Event(ts="2026-09-18 09:00", type="创建", actor="市政府办",
                      text="交办至市住建局，要求 2026-10-28 前完成交工验收", to_status=PENDING),
                Event(ts="2026-09-18 16:20", type="签收", actor="市住建局 王建国",
                      text="已收悉，立即组织施工", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-10-05 15:00", type="进展", actor="市住建局 王建国",
                      text="北广场路面基层完成，4 条道路中 2 条进入沥青摊铺", percent=60),
                Event(ts="2026-10-21 10:30", type="进展", actor="市住建局 王建国",
                      text="受管线迁改影响，2 条道路进度滞后约 15 天", percent=72),
                Event(ts="2026-10-29 09:00", type="催办", actor="市政府办 李敏",
                      text="事项已逾期，请立即报送滞后原因及补救方案"),
                Event(ts="2026-10-31 17:00", type="催办", actor="市政府办 李敏",
                      text="第二次催办：未见书面说明，将于本周调度会通报"),
            ],
        ),
        dict(
            id="DB2026-0029",
            title="12345 高频诉求「停车难」专项治理",
            source="12345 集中诉求分析",
            source_ref="12345 月报〔2026〕10 期",
            assignee="市城管局",
            co_assignees=["市公安局交警支队", "云山区政府"],
            issued_at="2026-10-12 11:00",
            deadline="2026-11-03",
            standard="新增泊位 1200 个，3 个重点片区高峰停车等候时间下降 30%",
            priority="重点",
            status=REVIEW,
            events=[
                Event(ts="2026-10-12 11:00", type="创建", actor="市政府办",
                      text="交办至市城管局，要求 2026-11-03 前新增 1200 个泊位", to_status=PENDING),
                Event(ts="2026-10-12 14:40", type="签收", actor="市城管局 赵雪",
                      text="已收悉", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-10-20 09:30", type="进展", actor="市城管局 赵雪",
                      text="已新增泊位 680 个，3 个片区完成施划方案", percent=55),
                Event(ts="2026-10-30 16:20", type="进展", actor="市城管局 赵雪",
                      text="累计新增泊位 1215 个，超目标 15 个", percent=100,
                      evidence=["泊位施划台账.xlsx"]),
                Event(ts="2026-10-31 10:10", type="提交办结", actor="市城管局 赵雪",
                      text="新增泊位 1215 个，泊位指标已完成；等候时间数据待交警提供",
                      from_status=WORKING, to_status=REVIEW, evidence=["泊位施划台账.xlsx"]),
            ],
        ),
        dict(
            id="DB2026-0030",
            title="秋季校园食品安全专项检查",
            source="市政府办专项部署",
            source_ref="专项部署〔2026〕41 号",
            assignee="市市场监管局",
            co_assignees=["市教育局"],
            issued_at="2026-10-08 09:00",
            deadline="2026-11-01",
            standard="全市中小学食堂检查覆盖率 100%，问题整改率 100%",
            priority="一般",
            status=WORKING,
            events=[
                Event(ts="2026-10-08 09:00", type="创建", actor="市政府办",
                      text="交办至市市场监管局，要求 2026-11-01 前完成全覆盖检查", to_status=PENDING),
                Event(ts="2026-10-08 13:30", type="签收", actor="市市场监管局 周颖",
                      text="已收悉，已制定检查方案", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-10-18 15:40", type="进展", actor="市市场监管局 周颖",
                      text="已检查学校 412 所，覆盖 78%，发现问题 63 项", percent=78),
                Event(ts="2026-10-29 17:10", type="进展", actor="市市场监管局 周颖",
                      text="覆盖 96%，剩余 21 所民办学校在检查中", percent=96),
            ],
        ),
        dict(
            id="DB2026-0031",
            title="重大项目「容缺受理」清单落地",
            source="市政府第 12 次常务会议定事项",
            source_ref="常务会议纪要〔2026〕12 号 第 2 项",
            assignee="市发改委",
            co_assignees=["市自然资源局", "市生态环境局"],
            issued_at="2026-10-15 09:00",
            deadline="2026-11-30",
            standard="首批 32 项容缺受理事项全部上线并完成 1 个实操案例",
            priority="一般",
            status=WORKING,
            events=[
                Event(ts="2026-10-15 09:00", type="创建", actor="市政府办",
                      text="交办至市发改委，要求 2026-11-30 前清单上线并跑通案例", to_status=PENDING),
                Event(ts="2026-10-15 11:20", type="签收", actor="市发改委 孙浩",
                      text="已收悉", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-10-27 14:00", type="进展", actor="市发改委 孙浩",
                      text="32 项清单已完成合法性审查，正在征求两个协办部门意见", percent=40),
            ],
        ),
        dict(
            id="DB2026-0033",
            title="入河排污口排查整治",
            source="市政府主要领导批示",
            source_ref="批示件〔2026〕203 号",
            assignee="市生态环境局",
            co_assignees=["市水务局"],
            issued_at="2026-10-30 09:00",
            deadline="2026-11-20",
            standard="完成全市 187 个入河排污口排查建档，问题排口整治率 60%",
            priority="重点",
            status=PENDING,
            events=[
                Event(ts="2026-10-30 09:00", type="创建", actor="市政府办",
                      text="交办至市生态环境局，要求 2026-11-20 前完成排查建档", to_status=PENDING),
                Event(ts="2026-10-31 16:00", type="催办", actor="市政府办 李敏",
                      text="交办超过 24 小时未签收，请尽快确认接收"),
            ],
        ),
        dict(
            id="DB2026-0035",
            title="冬季供暖保障预案报备",
            source="市政府办专项部署",
            source_ref="专项部署〔2026〕45 号",
            assignee="市城管局",
            co_assignees=["市住建局", "市应急管理局"],
            issued_at="2026-10-31 15:00",
            deadline="2026-11-10",
            standard="供暖应急预案报市应急局备案，抢修队伍和物资清单落实到位",
            priority="一般",
            status=PENDING,
            events=[
                Event(ts="2026-10-31 15:00", type="创建", actor="市政府办",
                      text="交办至市城管局，要求 2026-11-10 前完成预案报备", to_status=PENDING),
            ],
        ),
    ]
    for row in seed:
        item = Item(**row)
        _ITEMS[item.id] = item


_load_or_seed()
