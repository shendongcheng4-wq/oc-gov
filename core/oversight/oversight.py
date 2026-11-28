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

try:  # 同目录模块：core/oversight 已在 sys.path 上（见 app/main.py 的注册）
    import attach
except ImportError:  # pragma: no cover - 只有以包方式导入时才会走到
    from . import attach  # type: ignore[no-redef]

try:  # 授权词表在 core/ 下，同一条 sys.path
    import authz
except ImportError:  # pragma: no cover
    from .. import authz  # type: ignore[no-redef]



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
    type: str            # 创建 / 签收 / 进展 / 提交办结 / 确认办结 / 退回 / 催办 / 补录佐证
    actor: str
    text: str = ""
    from_status: str | None = None
    to_status: str | None = None
    percent: int | None = None
    # 佐证材料。每项是 {"id": "ev_x", "name": "…", "size": n}（上传的原件），
    # 或 {"id": "", "name": "…", "note_only": True}（手填的备注）。
    # 老存档里是纯字符串，`__post_init__` 统一补齐成这个形状 —— 否则前端和
    # 守卫要各写一遍「这到底是文件还是备注」的判断。
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.evidence = [
            x if isinstance(x, dict)
            else {"id": "", "name": str(x), "size": 0, "note_only": True}
            for x in (self.evidence or [])
        ]


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
        """最后一条「真的办了什么」的事件时间。

        排除两类：**催办**（天天催办的事项不该永远"不停滞"，那是拿催办把问题刷绿），
        **补录佐证**（补一份事后凭证不是办事进展 —— 停滞 34 天的事项，补一张照片
        就变回 0 天，等于用补凭证抹掉拖延痕迹）。
        """
        real = [e for e in self.events if e.type not in ("催办", "补录佐证")]
        if not real:
            return self.issued_dt
        return datetime.fromisoformat(max(e.ts for e in real))

    @property
    def idle_days(self) -> int:
        return max(0, (_now() - self.last_activity).days)

    @property
    def progress_percent(self) -> int:
        """当前进度。已提交办结（待审核 / 已办结）就是 100。

        只看「进展」事件会出一个自相矛盾的数字：最后一次报送可能停在 72%，
        可「提交办结」这个动作本身已经表示余下的都走完了。台账上出现
        「已办结 · 进度 72%」，任务节点自然也永远算不出全部完成。
        """
        if self.status in (REVIEW, CLOSED):
            return 100
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


def _guard(
    item: Item,
    action: str,
    reason: str,
    evidence: list[dict[str, Any]],
    text: str,
    stale: list[str] | None = None,
) -> str | None:
    """返回拒绝原因；通过则返回 None。所有业务规则集中在这里。

    `evidence` 已经过 `_resolve_evidence`：上传成功的带 id，手填的只有名字。
    `stale` 是「调用方报了 id、但仓库里找不到」的那些 —— 传上来之前被删、
    或者根本不是这条事项的文件。
    """
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
        # 佐证必须是**上传的原件**。之前只判「非空」，于是手打一句
        # 「销号台账.xlsx」就算附了佐证 —— 领导点开核对时什么都没有。
        # 备注留着不删（有的材料确实只能口头说明），但不能当佐证用。
        if any(x.get("id") for x in evidence):
            pass
        elif stale:
            return (
                f"有 {len(stale)} 份佐证的原件已不存在（可能被删掉了），"
                "请重新上传后再提交办结 —— 引用一份没有的文件，等于没有佐证。"
            )
        elif evidence:
            names = "、".join(f"「{str(x.get('name') or '')}」" for x in evidence[:3])
            return (
                f"佐证材料要上传原件：手填的{names}只是备注，不构成佐证。"
                "点「上传佐证」把文件传上来，再提交办结。"
            )
        else:
            return f"提交办结必须附佐证材料（对应办结标准：{item.standard}）。"
        if not text.strip():
            return "提交办结要写办结说明，说明如何达到办结标准。"

    if action == "反馈进展" and not text.strip():
        return "进展内容不能为空。"

    return None


def _resolve_evidence(
    item_id: str, raw: list[Any] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """把调用方给的佐证清单落到这条事项上。

    入参是混合的，因为三个入口各传各的：
    - HTTP 界面：上传得到的 `ev_xxxxxx` id + 输入框里手填的备注；
    - 智能体（`core/agent.py`）：从台账详情里读到的同一批 id；
    - 老脚本 / curl：直接写文件名。

    处理规则：`ev_` 开头的一律当 id 去仓库取，取不到记进 stale（不静默变成
    一条叫 `ev_ab12cd` 的备注 —— 那会让人以为文件在）；其余当备注。
    名字以**仓库里存的为准**，不信调用方传过来的：客户端可以把一份照片的
    显示名改成「竣工验收报告.pdf」，台账上就会出现一份不存在的报告。
    """
    entries: list[dict[str, Any]] = []
    stale: list[str] = []
    seen: set[str] = set()
    for x in raw or []:
        if isinstance(x, dict):
            eid = str(x.get("id") or "").strip()
            name = str(x.get("name") or "").strip()
        else:
            s = str(x or "").strip()
            eid, name = (s, "") if s.startswith("ev_") else ("", s)
        if eid:
            got = attach.get(item_id, eid)
            if got is None:
                stale.append(eid)
                continue
            meta, _path = got
            if eid in seen:
                continue
            seen.add(eid)
            entries.append({"id": meta["id"], "name": meta["name"], "size": int(meta.get("size") or 0)})
        elif name:
            key = f"note:{name}"
            if key in seen:
                continue
            seen.add(key)
            entries.append({"id": "", "name": attach.clean_name(name), "size": 0, "note_only": True})
    return entries, stale


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

        evidence, stale = _resolve_evidence(item_id, evidence)
        reject = _guard(item, action, reason, evidence, text, stale)
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


def allowed_actions(item: Item, auth_scope: dict[str, Any] | None = None) -> list[str]:
    """当前状态下可执行的操作，前端据此决定按钮是否可点。

    有两层：**状态**允不允许（TRANSITIONS），**这个人**允不允许（authz）。
    只判前者会画出点下去必然 403 的按钮 —— 协办单位的局长打开「待签收」的
    事项，状态上能签收，但签收只能是主办单位做的。

    `auth_scope` 传 `directory.person_scope()` 的形状；不传就只按状态算
    （列表接口、模型工具那些地方用不上按钮，也不必为每一行算一遍授权）。
    """
    if item.status == CLOSED:
        return []
    out = []
    for action, (allowed_from, _, _) in TRANSITIONS.items():
        if item.status in allowed_from:
            out.append(action)
    if auth_scope is None or not out:
        return out
    return [a for a in out if _permitted(item, a, auth_scope)]


def _permitted(item: Item, action: str, auth_scope: dict[str, Any]) -> bool:
    """这个身份现在对这条事项能不能做这个流转动作。"""
    az_action = authz.ACTION_FROM_ZH.get(action)
    if az_action is None:  # 授权词表里没有的流转动作：不在这一层拦
        return True
    resource = {
        "host": item.assignee,
        "units": tuple([item.assignee, *item.co_assignees]),
        "status": item.status,
    }
    return authz.can(auth_scope, az_action, resource)[0]


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


def remove_item(item_id: str, actor: str = "") -> bool:
    """撤掉一条**刚建、还没被任何人动过**的事项。只给回滚用。

    为什么需要：一次下发要把一条指示拆成几条事项，几条必须同进同退。第 3 条
    建失败而前 2 条留在台账里，责任单位就会收到半份交办 —— 那比一条没有更糟。

    为什么加守卫：这个函数不对外暴露（不在 WRITE_POLICY 里），但将来有人手滑
    接到接口上，就能把已经签收、已经办结的记录抹掉，事件流也跟着没了。
    所以只允许删「状态还是待签收、事件只有一条创建」的条目。
    """
    with _LOCK:
        item = _ITEMS.get(item_id)
        if item is None:
            return False
        if item.status != PENDING or len(item.events) > 1:
            return False
        _ITEMS.pop(item_id, None)
        _save()
    return True


def get(item_id: str) -> Item | None:
    return _ITEMS.get(item_id)


def list_items(
    status: str | None = None,
    assignee: str | None = None,
    only_risk: bool = False,
    scope: set[str] | None = None,
) -> list[dict[str, Any]]:
    """按条件筛选，按风险紧急度排序。

    `scope` 是责任单位集合（主办 ∪ 协办都算）：传了就只返回落在该集合里的事项，
    传 None 是全市台账（默认，行为不变）。这是「读收窄」的落点——局长调
    list_items 时带上自己的分管单位，就只能看到本单位的事项，看不到别的局的账。
    """
    rows = []
    for item in _ITEMS.values():
        if status and item.status != status:
            continue
        if assignee and item.assignee != assignee and assignee not in item.co_assignees:
            continue
        if scope is not None and item.assignee not in scope and not (scope & set(item.co_assignees)):
            continue
        risk = assess(item)
        if only_risk and risk.level == RISK_NONE:
            continue
        rows.append(_brief(item, risk))
    rows.sort(key=lambda r: (r["risk_rank"], r["days_left"]))
    return rows


def _brief(item: Item, risk: Risk) -> dict[str, Any]:
    # 最近一次催办：投递侧（notify）要判断「今天有没有人催过这一条」，而事件流
    # 只在 detail() 里。在这里给一份，省得 notify 自己翻一遍 events —— 同一个
    # 口径算两遍，迟早一边改了另一边没改。
    urges = [e for e in item.events if e.type == "催办"]
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
        # 谁、什么时候、说了什么。count 用于「同一天催两次要提醒两次」的去重桶。
        "last_urge": (
            {"ts": urges[-1].ts, "actor": urges[-1].actor, "text": urges[-1].text, "count": len(urges)}
            if urges
            else None
        ),
        # 来源两件套：材料里要写「这件是谁交办的」，会前材料还要按会议编号反查
        "source": item.source,
        "source_ref": item.source_ref,
    }


def detail(
    item_id: str,
    scope: set[str] | None = None,
    auth_scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """单条详情。

    `scope` 管**看不看得见**（主办 ∪ 协办），`auth_scope` 管**按钮画不画**
    （签收/进展/办结只认主办）。两者分开传，是因为演示态（未登录）要能看全量，
    而登录后按钮得跟着身份收窄。
    """
    item = get(item_id)
    if item is None:
        return None
    # 行级拒绝：给了 scope 就校验事项的责任单位是否落在里面，不在就返回 None，
    # 和「事项不存在」表现一致——不向越权者泄露这条事项是否存在。
    if scope is not None and item.assignee not in scope and not (scope & set(item.co_assignees)):
        return None
    risk = assess(item)
    return {
        **_brief(item, risk),
        "allowed_actions": allowed_actions(item, auth_scope),
        "source": item.source,
        "source_ref": item.source_ref,
        "issued_at": item.issued_at,
        "standard": item.standard,
        "risk_detail": risk.to_dict(),
        # 事件里的佐证补上文件信息（原名、大小、是否还在）。补在这一层而不是
        # 存进事件，是因为文件可以被删、也可以被重新上传，事物流水不该跟着变。
        "events": [
            {**asdict(e), "evidence": attach.describe(item.id, e.evidence)}
            for e in item.events
        ],
        # 已收下但还没随任何一次报送提交的文件（前端拿来渲染「待提交的佐证」）
        "evidence_files": evidence_files(item),
    }


def evidence_files(item: Item) -> list[dict[str, Any]]:
    """这条事项收过的原件，带 `used`（是否已被某条事件引用）。

    `used=False` 的是「传上来了、还没提交」——它们只存在仓库里，台账上看不到。
    刷新页面后这些文件仍然在，所以前端不必把上传结果只放在内存里。
    """
    used: set[str] = set()
    for e in item.events:
        for x in e.evidence:
            if isinstance(x, dict) and x.get("id"):
                used.add(str(x["id"]))
    out: list[dict[str, Any]] = []
    for meta in attach.list_of(item.id):
        out.append(
            {
                "id": meta["id"],
                "name": meta.get("name") or "",
                "size": int(meta.get("size") or 0),
                "type": meta.get("type") or "",
                "at": meta.get("at") or "",
                "by": meta.get("by") or "",
                "used": meta["id"] in used,
            }
        )
    return out


# ── 佐证材料：收原件 / 删原件 ─────────────────────────────────────────────
def add_evidence(item_id: str, filename: str, content: bytes, *, actor: str = "") -> dict[str, Any]:
    """收一份佐证原件。写操作的前置条件（状态、权限）由调用方判，这里只判状态。

    为什么状态要在这里判：`待审核` 时审核的正是「已提交的那份材料」，这时候
    换掉一份文件，审核的人批的和看到的就不是一份东西了。这一条不论从哪个入口
    （界面、脚本、以后的接口）进来都必须成立，所以放在核心而不是 HTTP 层。
    """
    with _LOCK:
        item = get(item_id)
        if item is None:
            return {"ok": False, "code": "not_found", "error": f"没有编号为 {item_id} 的事项。"}
        if item.status == CLOSED:
            return {"ok": False, "code": "closed", "error": "这条事项已办结，不能再补佐证材料。"}
        if item.status == REVIEW:
            return {
                "ok": False,
                "code": "locked",
                "error": "待审核中不能更换佐证：审核的是已提交的那份材料。要补请等退回后再传。",
            }
        meta, err = attach.put(item_id, filename, content, by=actor, at=_now().strftime("%Y-%m-%d %H:%M"))
        if meta is None:
            return {"ok": False, "code": "rejected", "error": err}
        return {"ok": True, "file": meta, "evidence_files": evidence_files(item)}


def remove_evidence(item_id: str, eid: str) -> dict[str, Any]:
    """删一份还没报送过的原件。

    已经被事件引用过的不许删：删掉之后那条「提交办结」就指向一份不存在的文件，
    台账从此无法自证。要换材料只能等退回后重新报送。
    """
    with _LOCK:
        item = get(item_id)
        if item is None:
            return {"ok": False, "code": "not_found", "error": f"没有编号为 {item_id} 的事项。"}
        for e in item.events:
            if any(isinstance(x, dict) and str(x.get("id") or "") == eid for x in e.evidence):
                return {
                    "ok": False,
                    "code": "in_use",
                    "error": (
                        f"这份佐证已经随 {e.ts} 的「{e.type}」报送过，不能删 —— "
                        "删了那条记录就没有依据了。要换请在退回后重新报送。"
                    ),
                }
        removed = attach.remove(item_id, eid)
        if not removed:
            return {"ok": False, "code": "not_found", "error": "没有这份佐证材料（可能已经删掉了）。"}
        return {"ok": True, "removed": True, "evidence_files": evidence_files(item)}


def backfill_evidence(
    item_id: str,
    index: int,
    note: str,
    eid: str = "",
    *,
    filename: str = "",
    content: bytes = b"",
    actor: str = "",
) -> dict[str, Any]:
    """给一条历史事件里的「仅备注」补挂原件。

    解决的是这样一类存量问题：早期佐证是一个手打的文件名（`["销号台账.xlsx"]`），
    系统里没有文件。那些记录今天点开仍然是空的，而它们恰恰是最需要凭证的部分 ——
    领导确认办结、事后倒查，看的都是这些。

    三条边界：
    - **只补备注，不改结论**：换掉的是一个名字条目，事件的 ts / type / 文本 /
      状态流转一律不动。补进去的原件由仓库按 sha1 校验，名字以仓库为准。
    - **必须留痕**：补完追加一条「补录佐证」事件，写明给哪条、补了什么、谁补的。
      偷偷把历史改干净，比留着一份没有凭证的记录更糟。
    - **不改状态**：这不是流转动作，`status` 一个字都不动。已办结的补完还是已办结。

    两个入口：`eid`（从已上传的待提交清单里挑一份）或 `filename` + `content`
    （当场传）。为什么支持后者：**已办结的事项 `add_evidence` 一律拒收**，
    只能走「带文件直接补」这一条路 —— 否则「已办结允许补」就是一句空话。

    权限由调用方判（`authz.OVERSIGHT_BACKFILL`）；这里只兜「待审核不许动」，
    理由和 `add_evidence` 里那条一样。
    """
    with _LOCK:
        item = get(item_id)
        if item is None:
            return {"ok": False, "code": "not_found", "error": f"没有编号为 {item_id} 的事项。"}
        if item.status == REVIEW:
            return {
                "ok": False,
                "code": "locked",
                "error": "待审核中不能补挂佐证：审核的是已提交的那份材料，等批完或退回后再补。",
            }
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < len(item.events)):
            return {"ok": False, "code": "bad_event", "error": "找不到要补挂的那条事件（列表可能已刷新，请重开详情）。"}

        ev = item.events[index]
        note = attach.clean_name(note)
        if not note:
            return {"ok": False, "code": "bad_note", "error": "没有指定要补哪一条备注。"}

        # 只认「同一个名字、且还是备注」的条目。同名只可能有一条（_resolve_evidence
        # 按 note:name 去过重），所以不会补错。
        target = next(
            (x for x in ev.evidence if x.get("note_only") and str(x.get("name")) == note),
            None,
        )
        if target is None:
            already = next((x for x in ev.evidence if str(x.get("name")) == note), None)
            if already is not None:
                return {
                    "ok": False,
                    "code": "already",
                    "error": f"「{note}」已经挂上原件了，不用再补。",
                }
            return {
                "ok": False,
                "code": "no_note",
                "error": f"{ev.ts} 的「{ev.type}」里没有叫「{note}」的备注。",
            }

        if content:
            meta, err = attach.put(
                item_id, filename or note, content, by=actor, at=_now().strftime("%Y-%m-%d %H:%M")
            )
            if meta is None:
                return {"ok": False, "code": "rejected", "error": err}
            eid = meta["id"]
        else:
            got = attach.get(item_id, eid)
            if got is None:
                return {"ok": False, "code": "file_missing", "error": "这份原件不在了（可能已被删掉），请重新上传。"}
            meta, _path = got

        ts = _now().strftime("%Y-%m-%d %H:%M")
        entry = {"id": meta["id"], "name": meta["name"], "size": int(meta.get("size") or 0)}
        target.clear()
        target.update(entry)
        item.events.append(
            Event(
                ts=ts,
                type="补录佐证",
                actor=actor,
                text=(
                    f"给 {ev.ts} 的「{ev.type}」补挂原件：{note} → {meta['name']}"
                    f"（{_human_size(int(meta.get('size') or 0))}）"
                ),
                # 新事件必须带上这份原件：否则它可能被当成「没报送过」而被删掉，
                # 补完的历史又会指向一份不存在的文件。
                evidence=[dict(entry)],
            )
        )
        _save()
        return {
            "ok": True,
            "item_id": item_id,
            "event_index": index,
            "event_ts": ev.ts,
            "event_type": ev.type,
            "eid": meta["id"],
            "name": meta["name"],
            "note": note,
            "at": ts,
        }


def _human_size(n: int) -> str:
    """给台账正文用的文件大小。事件流里「1024」不如「1.0KB」好读。"""
    if n <= 0:
        return "0B"
    for unit, base in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= base:
            return f"{n / base:.1f}{unit}"
    return f"{n}B"


# ── 催办扫描 ──────────────────────────────────────────────────────────────
def scan(days: int = SOON_DAYS, scope: set[str] | None = None) -> dict[str, Any]:
    """扫描需要催办的事项。这是定时任务每天该调用的那个函数。

    注意 days 只影响"临期"窗口，逾期和停滞的判定是固定的——
    不允许通过放大窗口来让逾期事项"消失"。

    `scope` 同 list_items：给了就只扫责任单位落在集合里的事项。
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        "逾期": [], "今日到期": [], "临期": [], "签收超时": [], "停滞": [],
    }
    for item in _ITEMS.values():
        if item.status == CLOSED:
            continue
        if scope is not None and item.assignee not in scope and not (scope & set(item.co_assignees)):
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

    def _in_scope(i: Item) -> bool:
        return scope is None or i.assignee in scope or (scope & set(i.co_assignees))

    open_count = sum(1 for i in _ITEMS.values() if i.status != CLOSED and _in_scope(i))
    return {
        "scanned_at": _now().strftime("%Y-%m-%d %H:%M"),
        "today": today().isoformat(),
        "window_days": days,
        "open_items": open_count,
        "hit_count": total,
        "item_count": need,
        "buckets": buckets,
        "headline": (
            f"扫描 {open_count} 件在办事项，"
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
    """8 条示例事项，全部取自溧阳市政府门户网站公开材料。

    数据来源（均为 www.liyang.gov.cn 公开发布，2026-11 核对）：

    - 《2026 年度溧阳市民生实事项目责任分解落实方案》
      市政府办公室 2026-01-30 印发，八大类 20 项经市十七届人大五次会议票决。
      索引号 014143344/2026-00010 前后，栏目「市政府办公室文件」。
      https://www.liyang.gov.cn/html/czly/2026/OPEHQFEM_0317/34760.html
    - 《2026 年〈政府工作报告〉重点任务分解落实方案》
      溧政发〔2026〕1 号，2026-03-24 印发、2026-04-10 公开，索引号 014143344/2026-00018。
      https://www.liyang.gov.cn/html/czly/2026/OPEQFEL_0420/34900.html
    - 市人大常委会视察 2026 年重点民生实事票决项目实施情况（2026-11-25），
      含 3 所学校心理辅导室改造完成、事故处理中心受理 412 起 / 调解成功率 97.6%、
      三和王家村新增车位 55 个等现场核实数据。
      https://www.liyang.gov.cn/html/czly/2026/IQKPQDDP_1125/242986.html

    事项标题、责任单位、办结标准、量化指标都是材料原文；交办时间、进展节点、
    催办记录是按演示需要编排的，不表示真实办理过程。八条刻意覆盖每一类风险：
    逾期、临期、停滞、签收超时、待审核、已办结。
    """
    seed = [
        dict(
            id="DB2026-0018",
            title="全溧优学工程：3 所学校心理辅导室标准化改造",
            source="2026 年度民生实事项目责任分解",
            source_ref="市府办 2026-01-30 印发 · 第 2 类「全溧优学工程」",
            assignee="市教育局",
            co_assignees=["市融媒体中心", "市妇联"],
            issued_at="2026-02-06 09:00",
            deadline="2026-11-20",
            standard="3 所学校心理辅导室完成标准化改造并投用，接待室、个辅室、团辅室全部提档升级",
            priority="重点",
            status=CLOSED,
            events=[
                Event(ts="2026-02-06 09:00", type="创建", actor="市政府办",
                      text="交办至市教育局，要求 2026-11-20 前完成 3 所学校心理辅导室标准化改造", to_status=PENDING),
                Event(ts="2026-02-06 14:30", type="签收", actor="市教育局 王莉",
                      text="已收悉，按校逐间排查功能区域缺口", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-06-30 16:00", type="进展", actor="市教育局 王莉",
                      text="1 所学校接待室、个辅室改造完成，其余 2 所完成方案设计", percent=40),
                Event(ts="2026-11-18 15:20", type="进展", actor="市教育局 王莉",
                      text="3 所学校全部完成改造，燕湖小学「湖心屿」心理辅导室已投用", percent=100,
                      evidence=["心理辅导室改造验收单.pdf"]),
                Event(ts="2026-11-19 10:00", type="提交办结", actor="市教育局 王莉",
                      text="3 所学校心理辅导室均已建成投用，申请核验", from_status=WORKING, to_status=REVIEW,
                      evidence=["心理辅导室改造验收单.pdf"]),
                Event(ts="2026-11-26 09:10", type="确认办结", actor="市政府办 李敏",
                      text="11 月 25 日随市人大常委会视察燕湖小学，3 所学校已全部完成改造，同意办结",
                      from_status=REVIEW, to_status=CLOSED),
            ],
        ),
        dict(
            id="DB2026-0025",
            title="全溧安居工程：三和王家村城中村环境综合整治",
            source="2026 年度民生实事项目责任分解",
            source_ref="市府办 2026-01-30 印发 · 第 5 类「全溧安居工程」",
            assignee="市住建局",
            co_assignees=["市城管局"],
            issued_at="2026-02-06 09:00",
            deadline="2026-12-10",
            standard="城中村环境综合整治完成：改造建设机动车停车位 110 个、新增车位 55 个，配套公厕和蓝盾驿站建成投用",
            priority="重点",
            status=WORKING,
            events=[
                Event(ts="2026-02-06 09:00", type="创建", actor="市政府办",
                      text="交办至市住建局，要求 2026-12-10 前完成整治并补齐便民服务设施", to_status=PENDING),
                Event(ts="2026-02-07 11:00", type="签收", actor="市住建局 王建国",
                      text="已收悉，先行开展停车泊位施划和公厕选址", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-09-15 17:00", type="进展", actor="市住建局 王建国",
                      text="机动车停车位改造完成 110 个、新增 55 个，公厕进场施工", percent=45),
                Event(ts="2026-11-24 16:30", type="进展", actor="市住建局 王建国",
                      text="停车位全部投用；公厕主体封顶，蓝盾驿站装修收尾", percent=80),
            ],
        ),
        dict(
            id="DB2026-0027",
            title="全溧颐养工程：老年人智能技术培训 4000 人次",
            source="2026 年度民生实事项目责任分解",
            source_ref="市府办 2026-01-30 印发 · 第 4 类「全溧颐养工程」",
            assignee="市民政局",
            co_assignees=["各镇（街道）"],
            issued_at="2026-03-02 09:00",
            deadline="2026-11-20",
            standard="全年免费提供老年人运用智能技术培训服务 4000 人次，同步开展反诈防骗宣传",
            priority="一般",
            status=WORKING,
            events=[
                Event(ts="2026-03-02 09:00", type="创建", actor="市政府办",
                      text="交办至市民政局，要求 2026-11-20 前完成 4000 人次培训", to_status=PENDING),
                Event(ts="2026-03-02 15:40", type="签收", actor="市民政局 张敏",
                      text="已收悉，联合各镇（街道）布点开班", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-07-31 17:10", type="进展", actor="市民政局 张敏",
                      text="累计培训 1680 人次，下半年按镇街排班加密", percent=42),
                Event(ts="2026-10-15 16:20", type="进展", actor="市民政局 张敏",
                      text="累计培训 2720 人次，四季度还有 6 个镇街未开班", percent=68),
                Event(ts="2026-11-21 09:00", type="催办", actor="市政府办 李敏",
                      text="事项已逾期，剩余 1280 人次请报送补班计划与完成时间"),
            ],
        ),
        dict(
            id="DB2026-0029",
            title="全溧畅达工程：市道路交通事故一体化处理服务中心运行",
            source="2026 年度民生实事项目责任分解",
            source_ref="市府办 2026-01-30 印发 · 第 8 类「全溧畅达工程」",
            assignee="市公安局",
            co_assignees=["市法院", "市司法局"],
            issued_at="2026-02-06 09:00",
            deadline="2026-11-30",
            standard="中心投入运行，事故受理、责任认定、调解、审判、保险理赔「只进一扇门，办结所有事」",
            priority="重点",
            status=REVIEW,
            events=[
                Event(ts="2026-02-06 09:00", type="创建", actor="市政府办",
                      text="交办至市公安局，要求 2026-11-30 前建成运行", to_status=PENDING),
                Event(ts="2026-02-09 10:20", type="签收", actor="市公安局 陈锋",
                      text="已收悉，会同法院、司法局确定常驻轮驻单位", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-06-30 15:00", type="进展", actor="市公安局 陈锋",
                      text="场地改造完成，三级进驻机制和保险理赔窗口方案已定", percent=40),
                Event(ts="2026-11-02 09:00", type="进展", actor="市公安局 陈锋",
                      text="中心投入试运行，实行常驻、轮驻、随驻三级进驻", percent=100),
                Event(ts="2026-11-24 11:30", type="提交办结", actor="市公安局 陈锋",
                      text="试运行以来累计受理案件 412 起，调解成功率 97.6%，申请核验",
                      from_status=WORKING, to_status=REVIEW, evidence=["试运行情况报告.docx"]),
            ],
        ),
        dict(
            id="DB2026-0030",
            title="全溧健康工程：60—74 周岁常住居民结直肠癌筛查",
            source="2026 年度民生实事项目责任分解",
            source_ref="市府办 2026-01-30 印发 · 第 3 类「全溧健康工程」",
            assignee="市卫健局",
            co_assignees=["各镇（街道）卫生院"],
            issued_at="2026-02-06 09:00",
            deadline="2026-12-31",
            standard="完成 60—74 周岁常住居民结直肠癌危险因素调查、高危人群评估与筛查，高危人群随访率 100%",
            priority="一般",
            status=WORKING,
            events=[
                Event(ts="2026-02-06 09:00", type="创建", actor="市政府办",
                      text="交办至市卫健局，要求年内完成目标人群筛查", to_status=PENDING),
                Event(ts="2026-02-10 09:30", type="签收", actor="市卫健局 周敏",
                      text="已收悉，筛查任务分解到 12 家基层医疗机构", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-06-30 17:00", type="进展", actor="市卫健局 周敏",
                      text="完成危险因素调查 2.1 万人，高危人群评估同步开展", percent=35),
                Event(ts="2026-10-31 16:00", type="进展", actor="市卫健局 周敏",
                      text="筛查覆盖率 72%，高危人群随访率 88%", percent=72),
            ],
        ),
        dict(
            id="DB2026-0031",
            title="拓宽多元富民渠道：全年新增城镇就业 1.5 万人",
            source="《2026 年〈政府工作报告〉重点任务分解落实方案》",
            source_ref="溧政发〔2026〕1 号 第 10 项",
            assignee="市人社局",
            co_assignees=["市发改委", "市工信局", "市商务局", "各镇（区、街道）"],
            issued_at="2026-04-10 09:00",
            deadline="2026-12-31",
            standard="全年新增城镇就业 1.5 万人，重点群体就业帮扶覆盖率 100%",
            priority="重点",
            status=WORKING,
            events=[
                Event(ts="2026-04-10 09:00", type="创建", actor="市政府办",
                      text="交办至市人社局，要求按季度报送新增城镇就业进度", to_status=PENDING),
                Event(ts="2026-04-10 14:00", type="签收", actor="市人社局 孙浩",
                      text="已收悉，已把 1.5 万人任务分解到镇街和重点企业", from_status=PENDING, to_status=WORKING),
                Event(ts="2026-07-15 16:00", type="进展", actor="市人社局 孙浩",
                      text="上半年新增城镇就业 7800 人，完成年度目标 52%", percent=52),
                Event(ts="2026-10-20 15:30", type="进展", actor="市人社局 孙浩",
                      text="累计新增 11700 人，完成 78%；高校毕业生帮扶台账在核", percent=78),
            ],
        ),
        dict(
            id="DB2026-0033",
            title="全溧颐养工程：228 名银发顾问技能提升培训",
            source="2026 年度民生实事项目责任分解",
            source_ref="市府办 2026-01-30 印发 · 第 4 类「全溧颐养工程」",
            assignee="市民政局",
            co_assignees=["各镇（街道）"],
            issued_at="2026-11-25 16:00",
            deadline="2026-12-20",
            standard="228 名银发顾问全部完成技能提升培训并考核合格，建成专业化养老服务队伍",
            priority="一般",
            status=PENDING,
            events=[
                Event(ts="2026-11-25 16:00", type="创建", actor="市政府办",
                      text="交办至市民政局，要求 2026-12-20 前完成 228 人培训考核", to_status=PENDING),
                Event(ts="2026-11-27 09:10", type="催办", actor="市政府办 李敏",
                      text="交办超过 24 小时未签收，请尽快确认接收"),
            ],
        ),
        dict(
            id="DB2026-0035",
            title="全溧护育工程：41 个「爱心暖托班」开办",
            source="2026 年度民生实事项目责任分解",
            source_ref="市府办 2026-01-30 印发 · 第 1 类「全溧护育工程」",
            assignee="团市委",
            co_assignees=["市教育局", "市总工会", "市妇联", "市残联"],
            issued_at="2026-11-27 10:00",
            deadline="2026-12-31",
            standard="开办 41 个为期不低于 28 天的「爱心暖托班」，直接惠及 1400 户家庭",
            priority="一般",
            status=PENDING,
            events=[
                Event(ts="2026-11-27 10:00", type="创建", actor="市政府办",
                      text="交办至团市委，要求 2026-12-31 前完成 41 个班次开办", to_status=PENDING),
            ],
        ),
    ]
    for row in seed:
        item = Item(**row)
        _ITEMS[item.id] = item

_load_or_seed()
