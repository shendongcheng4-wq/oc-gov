"""对话里约会议：把一句话解析成一张「待确认的会议草稿」。

三条立场，都是为了不把风险藏起来：

1. **只草拟，不落库。** 写操作必须人点确认。这个项目没有登录态（身份是前端
   可切的），对话里直接建会等于任何打开页面的人都能以「市政府办」名义造一场
   会 —— 而且会立刻进日程、进会前提醒。草稿卡是那道闸。

2. **猜了要写出来。** 时间是怎么算的、主持人为什么按当前身份、哪几个单位没
   匹配上、同时间段有没有撞车，全摆在卡上。猜而不说的，等于让人替系统背锅。

3. **不发明主数据。** 参会单位按台账里真实出现过的责任单位匹配（外加演示身份
   的分管单位、已有会议的参会单位），匹配不上就原样保留并标出来。硬塞一个
   「市财政局」进白名单，会让系统看起来认识一个它其实没有的单位。

不确定性用 `missing`（缺必填）和 `assumptions`（我替你定了）两个字段分开表达，
前端据此决定「能不能建会」和「黄色提示怎么写」。
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "oversight",
           ROOT / "core" / "meeting"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# 同一天这两场会隔得太近就算撞车：会议默认按 2 小时估。
# 这不是「日历冲突检测」——系统里没有会议室资源也没有个人日历，
# 只能说「同一天还有另一场会」，不能说「张局长那个点没空」。
CLASH_MINUTES = 120

# --------------------------------------------------------------------------
# 这是不是「要建一场会」
# --------------------------------------------------------------------------
# 只看动词会误伤：「上次开会定了什么」也含「开」「会」，但它是查记录。
# 所以用「预约动作 + 会议名词」的组合模式，再排除明确的查询话术。
_WANT_PATTERNS = (
    re.compile(r"约[^。；;！？?]{0,14}(会|会议)"),
    re.compile(r"安排[^。；;！？?]{0,14}(会|会议)"),
    re.compile(r"(订|预订)[^。；;！？?]{0,10}(会|会议室)"),
    re.compile(r"组织[^。；;！？?]{0,14}(会|会议)"),
    re.compile(r"召集[^。；;！？?]{0,14}(会|会议)"),
    re.compile(r"筹备[^。；;！？?]{0,14}(会|会议)"),
    re.compile(r"排[^。；;！？?]{0,10}(会|会议)"),
    re.compile(r"开(个|一个|一场|一次|个场)?会"),
    re.compile(r"定[^。；;！？?]{0,8}(会|会议)室?"),
)

# 这些词一出现，问的是「已有的会」而不是「要开一场会」
_NOT_WANT = (
    "定了什么", "开了什么", "说了什么", "讲了什么", "有没有", "哪些", "哪个",
    "几点的", "安排了吗", "安排了没", "进展", "纪要", "记录", "原文",
    "谁在办", "怎么办", "怎么开", "为什么", "什么会", "上一次", "上次",
    "会议助手", "会议时间冲突", "取消", "改期",
)


def wants(question: str) -> bool:
    """问句是不是在要求「约一场会」。"""
    q = str(question or "").strip()
    if not q or len(q) > 120:
        return False
    if any(w in q for w in _NOT_WANT):
        return False
    return any(p.search(q) for p in _WANT_PATTERNS)


# --------------------------------------------------------------------------
# 中文数字
# --------------------------------------------------------------------------
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _num(text: str) -> int | None:
    s = str(text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        head, _, tail = s.partition("十")
        h = _CN.get(head, 1) if head else 1
        t = _CN.get(tail, 0) if tail else 0
        return h * 10 + t
    if len(s) == 1:
        return _CN.get(s)
    return None


_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
            "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# --------------------------------------------------------------------------
# 时间：把「下周三下午三点」算成 2026-11-11 15:00
# --------------------------------------------------------------------------
_RE_FULL_DATE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*[日号]?")
_RE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_RE_DAY_WORD = re.compile(r"(大后天|后天|明天|明日|今天|今日)")
_RE_WEEK = re.compile(r"(下下|下|本|这|上)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])")
_RE_MONTH_EDGE = re.compile(r"(本|这|下)?\s*月\s*(底|末|初)")

_DAY_WORD_OFFSET = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}


def _month_edge(today: date, which: str) -> date:
    """月底 / 月末 / 月初。三者在月末最后一天和次月第一天之间，语义是清楚的。"""
    if which == "初":
        return today.replace(day=1)
    nxt = today.replace(day=28) + timedelta(days=4)
    return nxt - timedelta(days=nxt.day)  # 本月最后一天


def _date_part(q: str, today: date) -> tuple[date | None, str, str]:
    """返回 (日期, 计算说明, 命中的原文片段)。"""
    m = _RE_FULL_DATE.search(q)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, f"按写明的日期 {d.isoformat()}", m.group(0)
        except ValueError:
            return None, f"{m.group(0)} 不是有效日期", m.group(0)

    m = _RE_MD.search(q)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        year = today.year
        try:
            d = date(year, mo, dy)
        except ValueError:
            return None, f"{m.group(0)} 不是有效日期", m.group(0)
        # 说「1月5日」而现在是 11 月，指的必然是明年 —— 但那是猜测，要说明。
        if d < today - timedelta(days=1):
            try:
                d = date(year + 1, mo, dy)
                return d, f"按 {mo} 月 {dy} 日算，已过今年，顺延到 {d.year} 年", m.group(0)
            except ValueError:
                return d, f"按 {today.year} 年 {mo} 月 {dy} 日算", m.group(0)
        return d, f"按 {year} 年 {mo} 月 {dy} 日算", m.group(0)

    m = _RE_DAY_WORD.search(q)
    if m:
        d = today + timedelta(days=_DAY_WORD_OFFSET[m.group(1)])
        return d, f"「{m.group(1)}」= {d.isoformat()}", m.group(0)

    m = _RE_WEEK.search(q)
    if m:
        prefix = m.group(1) or ""
        # 周一周日都按「周一是一周第一天」算，这是国内习惯；
        # 也因此「下周三」永远在「本周日」之后，不会出现跨周歧义。
        monday = today - timedelta(days=today.weekday())
        idx = _WEEKDAY[m.group(2)]
        if prefix in ("下下",):
            d = monday + timedelta(days=14 + idx)
            return d, f"「{m.group(0)}」按下下周算 = {d.isoformat()}", m.group(0)
        if prefix == "下":
            d = monday + timedelta(days=7 + idx)
            return d, f"「{m.group(0)}」按下周算 = {d.isoformat()}", m.group(0)
        if prefix in ("本", "这"):
            d = monday + timedelta(days=idx)
            return d, f"「{m.group(0)}」按本周算 = {d.isoformat()}", m.group(0)
        # 光说「周三」：本周还没到就是本周，已经过了就是下周
        d = monday + timedelta(days=idx)
        if d < today:
            d = d + timedelta(days=7)
            return d, f"「{m.group(0)}」本周已过，按下一{d.isoformat()[:0] or _WEEKDAY_CN[idx]}算 = {d.isoformat()}", m.group(0)
        return d, f"「{m.group(0)}」按本周算 = {d.isoformat()}", m.group(0)

    m = _RE_MONTH_EDGE.search(q)
    if m:
        if m.group(1) == "下":
            base = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            d = _month_edge(base, m.group(2))
        else:
            d = _month_edge(today, m.group(2))
        return d, f"「{m.group(0)}」= {d.isoformat()}", m.group(0)

    return None, "", ""


_RE_CLOCK = re.compile(
    r"(凌晨|早晨|早上|上午|中午|下午|傍晚|晚上)?\s*"
    r"([0-9]{1,2}|[一二三四五六七八九十两]{1,3})\s*[点时]"
    r"(半|(?:([0-9]{1,2}|[一二三四五六七八九十]{1,3})\s*分?)|一刻)?"
)
_RE_DIGIT_CLOCK = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)(?!\d)")

_PERIOD_DEFAULT = {"凌晨": 5, "早晨": 8, "早上": 8, "上午": 9, "中午": 12,
                   "下午": 15, "傍晚": 18, "晚上": 20}


def _time_part(q: str) -> tuple[time | None, str, list[str], str]:
    """返回 (时刻, 计算说明, 需要提醒的假设, 命中的原文)。"""
    notes: list[str] = []

    m = _RE_DIGIT_CLOCK.search(q)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        return time(h, mi), f"按写明的 {h:02d}:{mi:02d}", notes, m.group(0)

    m = _RE_CLOCK.search(q)
    if m:
        period, hh, tail = m.group(1), m.group(2), m.group(3)
        hour = _num(hh)
        if hour is None or hour > 24:
            return None, "", notes, ""
        minute = 0
        if tail == "半":
            minute = 30
        elif tail == "一刻":
            minute = 15
        elif m.group(4):
            minute = _num(m.group(4)) or 0

        if period in ("下午", "傍晚") and hour < 12:
            hour += 12
        elif period == "晚上":
            if hour < 12:
                hour += 12
            elif hour == 12:
                hour = 0
                notes.append("「晚上 12 点」按次日 00:00 算")
        elif period == "中午" and hour < 3:
            hour += 12
        elif period is None and 1 <= hour <= 6:
            # 「三点开会」在办公语境里几乎都是下午。这是猜，必须写出来。
            hour += 12
            notes.append(f"没说上午下午，「{hh}点」按下午 {hour} 点算")

        if hour >= 24:
            return None, "", notes, ""
        raw = m.group(0).strip()
        return time(hour, minute), f"「{raw}」= {hour:02d}:{minute:02d}", notes, raw

    if m_period := re.search(r"(凌晨|早晨|早上|上午|中午|下午|傍晚|晚上)", q):
        period = m_period.group(1)
        h = _PERIOD_DEFAULT[period]
        notes.append(f"只说了「{period}」，按 {h:02d}:00 算")
        return time(h, 0), f"只给了时段「{period}」", notes, period

    return None, "", notes, ""


def parse_when(q: str, today: date) -> dict[str, Any]:
    """把时间说法解析成 held_at。缺什么就报什么，不补一个默认时间糊过去。"""
    d, d_why, d_raw = _date_part(q, today)
    t, t_why, t_notes, t_raw = _time_part(q)

    assumptions: list[str] = list(t_notes)
    missing: list[str] = []
    notes: list[str] = []

    if d is None and t is None:
        return {"held_at": "", "when_text": "", "why": "", "assumptions": [],
                "missing": ["held_at"], "notes": []}

    if d is None:
        # 有时间没日期：今天的这个点还没过就是今天，过了就是明天
        today_at = datetime.combine(today, t)
        if today_at >= datetime.now().replace(second=0, microsecond=0):
            d = today
            assumptions.append("没说哪天，按今天算（这个点还没过）")
        else:
            d = today + timedelta(days=1)
            assumptions.append("没说哪天，今天这个点已过，按明天算")
        d_why = f"未指定日期 = {d.isoformat()}"

    if t is None:
        t = time(9, 0)
        assumptions.append("没说几点，按上午 9:00 占位，建会后请改")
        t_why = "未指定时刻"

    held = datetime.combine(d, t)
    if held < datetime.now() - timedelta(minutes=1):
        notes.append(f"算出来的时间（{held:%Y-%m-%d %H:%M}）已经过去了，确认一下是不是说错了")

    return {
        "held_at": held.strftime("%Y-%m-%d %H:%M"),
        "when_text": " ".join(x for x in (d_raw, t_raw) if x).strip(),
        "why": "；".join(x for x in (d_why, t_why) if x),
        "assumptions": assumptions,
        "missing": [],
        "notes": notes,
    }


# --------------------------------------------------------------------------
# 地点 / 主持人 / 议题
# --------------------------------------------------------------------------
_RE_PLACE = (
    # 「地点在 3 号楼会议室」「地点：市政府常务会议室」——连「会议室」一起留，
    # 只留「3号楼」就等于把楼号当成了地点，写通知时还得再猜一层
    re.compile(r"地点\s*[在是为：:]?\s*([^\s，。,；;]{2,24})"),
    re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,14}会议室)"),
    re.compile(r"在\s*([^\s，。,；;]{2,20}?)\s*(?:开会|召开|举行|碰头)"),
    re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{0,4}\d+号楼[^\s，。,；;]{0,10})"),
)

_RE_HOST = re.compile(
    r"([\u4e00-\u9fa5]{1,6}(?:市长|副市长|局长|主任|秘书长|部长|书记|区长|县长)|秘书长)\s*主持"
)

# 议题一定要在逗号处停：不停的话「议题是供暖保障，住建局和城管局参加」会把
# 参会单位、地点整段吞进标题里 —— 界面上就出现一个读起来像句子的会议名。
_RE_TOPIC = (
    re.compile(r"议\s*题\s*[是为：:]\s*([^。；;！？?，,]+)"),
    re.compile(r"主题\s*[是为：:]\s*([^。；;！？?，,]+)"),
    re.compile(r"关于\s*([^。；;！？?，,]{2,40}?)\s*的\s*(?:专题会|会议|会)"),
    re.compile(r"(?:讨论|研究|审议|部署|听取|商量)\s*([^。；;！？?，,]{2,40})"),
)


def parse_place(q: str) -> str:
    for pat in _RE_PLACE:
        m = pat.search(q)
        if m:
            # 「在市政府常务会议室」——介词不是地点的一部分，写通知时不能带上
            return re.sub(r"^[在于到]", "", m.group(1).strip(" 的"))
    return ""


def parse_host(q: str) -> str:
    m = _RE_HOST.search(q)
    return m.group(1).strip() if m else ""


def parse_topics(q: str) -> list[str]:
    for pat in _RE_TOPIC:
        m = pat.search(q)
        if m:
            text = m.group(1).strip(" 的，,。；;")
            # 「议题是供暖保障和管网排查」→ 两个议题
            parts = [x.strip() for x in re.split(r"[、和及与]", text) if x.strip()]
            return parts or [text]
    return []


# --------------------------------------------------------------------------
# 参会单位：按台账里真实出现过的单位匹配
# --------------------------------------------------------------------------
_RE_ATTEND_AFTER = (
    re.compile(r"(?:参会单位|参加单位|参会人员|与会人员)\s*[是为：:]?\s*([^。；;！？?]+)"),
    re.compile(r"(?:叫上|通知|邀请)\s*([^。；;！？?]+)"),
    # 兜底：光写「参会」「出席」，单位在后面
    re.compile(r"(?:参会|参加|出席|列席)\s*[是为：:]?\s*([^。；;！？?]+)"),
)
# 中文里更常见的说法是「住建局和城管局参加」——单位在动词**前面**。
# 只写「关键词 + 后面」这一种，这句话就一个单位都抓不到，而且不是报错，
# 是静默漏掉：草稿卡上显示「没说参会单位」，用户以为自己没写。
_RE_ATTEND_BEFORE = (
    re.compile(r"([^。；;！？?，,]{2,60}?)\s*(?:参加|参会|出席|列席)(?!单位|人员|人数)"),
)

_UNIT_NOISE = ("参加", "参会", "出席", "列席", "与会", "等", "一起", "都来", "也来")
_UNIT_LEAD = ("请", "由", "让", "叫上", "通知", "邀请", "安排", "要求")


def _bare(unit: str) -> str:
    return re.sub(r"^(市|县|区|省)", "", unit.strip())


def roster() -> list[str]:
    """参会单位的可选清单。

    不新建一份主数据：台账里出现过的责任单位 + 协办单位 + 演示身份的分管单位 +
    已有会议的参会单位，就是系统真正"认识"的单位。现算，不落盘。
    """
    units: set[str] = set()
    try:
        import oversight as ov  # noqa: PLC0415
        for it in ov.list_items():
            for u in [it.get("assignee")] + list(it.get("co_assignees") or []):
                if u:
                    units.add(str(u).strip())
    except Exception:  # 台账读不出来不该让约会议整体失败
        pass
    try:
        import people as people_mod  # noqa: PLC0415
        for p in people_mod.all_people():
            for u in p.get("units") or []:
                if u:
                    units.add(str(u).strip())
    except Exception:
        pass
    try:
        from ledger import list_meetings  # noqa: PLC0415
        for m in list_meetings():
            for u in m.get("attendees") or []:
                if u:
                    units.add(str(u).strip())
    except Exception:
        pass
    return sorted(units)


def canon_unit(unit: str, units: list[str]) -> str | None:
    """把一个说法对齐到清单里的全称。「住建局」→「市住建局」。

    只做「去行政级别前缀」这一种归一，不做模糊编辑距离匹配 ——
    「生态环保局」和「市生态环境局」差三个字，编辑距离会把它们算成同一个，
    但那是猜；猜错的单位会收到一场不属于它的会。
    """
    u = unit.strip()
    if not u:
        return None
    if u in units:
        return u
    b = _bare(u)
    for r in units:
        if _bare(r) == b:
            return r
    return None


def parse_attendees(q: str, units: list[str]) -> tuple[list[str], list[str], str]:
    """返回 (已对齐的单位, 没对齐的原话, 命中的原文片段)。"""
    raw_text, raw_span = "", ""
    for i, pat in enumerate(_RE_ATTEND_AFTER):
        m = pat.search(q)
        if not m:
            continue
        cap = m.group(1).strip(" 的，,。；;")
        if not cap:
            continue
        # 最后那条兜底（光写「参加」）最容易误抓：句子里「参加，地点在3号楼会议室」
        # 会把地点当成参会单位，而且不报错 —— 于是单位栏空了，用户以为自己没写。
        # 所以兜底要求捕获的片段里真带单位后缀，否则宁可往下走「单位在前」的写法。
        if i == len(_RE_ATTEND_AFTER) - 1 and (
            not re.search(r"(局|委|办|厅|政府|支队|中心|公司|医院|学校|银行)", cap)
            # 「在市政府常务会议室」里也有「政府」——地点常带单位名，
            # 所以还得把会议室/地点这类词排除掉，否则地点会被当成参会单位
            or re.search(r"(会议室|地点|会场)", cap)
        ):
            continue
        raw_text, raw_span = cap, m.group(0)
        break
    if not raw_text:
        for pat in _RE_ATTEND_BEFORE:
            m = pat.search(q)
            if m:
                raw_text, raw_span = m.group(1), m.group(0)
                break
    if not raw_text:
        return [], [], ""

    cands = [x.strip(" 的，,。；;") for x in re.split(r"[、，,/和及与]|\s+", raw_text)]
    cands = [c for c in cands if c]

    ok: list[str] = []
    unknown: list[str] = []
    for c in cands:
        c = c.strip()
        # 「请住建局」——动词和单位连在一起时，前缀得剥掉才能对上清单
        for lead in _UNIT_LEAD:
            if c.startswith(lead) and len(c) > len(lead):
                c = c[len(lead):]
        for noise in _UNIT_NOISE:
            if c.endswith(noise) and len(c) > len(noise):
                c = c[: -len(noise)]
        c = c.strip(" 的，,。；;")
        if not c:
            continue
        hit = canon_unit(c, units)
        if hit:
            if hit not in ok:
                ok.append(hit)
        elif re.search(r"(局|委|办|厅|政府|支队|中心|公司|医院|学校|银行)$", c):
            if c not in unknown:
                unknown.append(c)
    return ok, unknown, raw_span


# --------------------------------------------------------------------------
# 撞车：只能说「同一天还有另一场会」
# --------------------------------------------------------------------------
def clashes(held_at: str) -> list[dict[str, Any]]:
    try:
        from ledger import list_meetings  # noqa: PLC0415
        from schedule import _dt  # noqa: PLC0415, SLF001
    except Exception:
        return []
    try:
        target = datetime.strptime(held_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return []

    out: list[dict[str, Any]] = []
    for m in list_meetings():
        start = _dt(m.get("held_at") or "")
        if start is None:
            continue
        gap = abs(int((start - target).total_seconds() // 60))
        if gap <= CLASH_MINUTES:
            out.append({
                "id": m["id"],
                "title": m.get("title") or "",
                "held_at": m.get("held_at") or "",
                "minutes": gap,
                "kind": m.get("kind") or "record",
            })
    return out


# --------------------------------------------------------------------------
# 组装草稿
# --------------------------------------------------------------------------
def parse(question: str, *, today: date | None = None,
          person: dict[str, Any] | None = None) -> dict[str, Any]:
    """一句话 → 草稿。不写任何文件。"""
    today = today or date.today()
    q = str(question or "").strip()
    person = person or {}

    when = parse_when(q, today)
    units = roster()
    attendees, unknown_units, att_span = parse_attendees(q, units)
    topics = parse_topics(q)
    place = parse_place(q)
    host = parse_host(q)

    assumptions: list[str] = list(when.get("assumptions") or [])
    notes: list[str] = list(when.get("notes") or [])
    missing: list[str] = list(when.get("missing") or [])

    title = "、".join(topics) if topics else ""
    if not title:
        missing.append("title")
    if not attendees:
        missing.append("attendees")

    if not host:
        post = (person.get("post") or "").strip()
        if post:
            host = post
            assumptions.append(f"没说谁主持，按当前身份（{post}）填，确认时可以改")
        else:
            assumptions.append("没说谁主持，也没识别出当前身份")

    if not attendees:
        notes.append("没说参会单位 —— 谁要到场是会议通知的核心，不填就没法发通知")

    if unknown_units:
        notes.append(
            "这几个单位在台账里没出现过，按原话保留：" + "、".join(unknown_units)
            + "。建会不受影响，但督办匹配、通知名单可能对不上"
        )

    clash = clashes(when["held_at"]) if when.get("held_at") else []
    if clash:
        notes.append(
            f"同一时间段还有 {len(clash)} 场会："
            + "、".join(f"{c['held_at'][5:16]} {c['title']}" for c in clash[:3])
        )

    draft = {
        "title": title,
        "held_at": when.get("held_at") or "",
        "when_text": when.get("when_text") or "",
        "when_why": when.get("why") or "",
        "place": place,
        "host": host,
        "attendees": attendees,
        "unknown_units": unknown_units,
        "topics": topics,
        "missing": missing,
        "assumptions": assumptions,
        "notes": notes,
        "clashes": clash,
        "unit_options": units[:40],
        "ready": not missing,
    }
    return draft


def as_result(draft: dict[str, Any], question: str) -> dict[str, Any]:
    """包成 ask() 的返回形状 —— 前端只认这一套字段。"""
    title = draft.get("title") or "（还没说议题）"
    when = draft.get("held_at") or "（还没说时间）"
    if draft["ready"]:
        head = f"已草拟「{title}」，{when} 开会。确认后才会写入。"
    else:
        head = f"草拟了一场会（{title} · {when}），还差 {len(draft['missing'])} 项才能建。"

    return {
        "refused": False,
        "question": question,
        "planner": "rules",
        "intent": "meet",
        "headline": head,
        "detail": "这一步只解析你这句话，没有写任何文件。核对下面几项、补全缺的字段，点「建会」才落库；"
                  "建完会进日程和会前 60 分钟提醒。",
        "meet_draft": draft,
        "sources": [],
        "notes": [],
        "facts": [],
        "meet": True,
        "trace": [
            {
                "step": 1,
                "tool": "meetplan.parse",
                "call": "meetplan.parse(question)",
                "status": "ok",
                "summary": "话里有「约 / 安排 / 开一场会」的意思，走建会草稿；没调模型，也没查指标",
            }
        ],
    }


def commit(draft: dict[str, Any], *, thread_id: str = "", message_id: str = "",
           actor: str = "") -> dict[str, Any]:
    """确认后落库。同一条消息重复点只建一场（幂等）。

    幂等的依据是 `from_message`：同一条回答里的同一张草稿卡，无论点多少次、
    刷新后重开多少次，都只对应一场会。没有这道判断，双击就会造出两场会。
    """
    from ledger import create, list_meetings  # noqa: PLC0415

    title = str(draft.get("title") or "").strip()
    held_at = str(draft.get("held_at") or "").strip()
    if not title:
        return {"ok": False, "error": "missing_title", "message": "议题不能为空——没有议题的会议无法生成通知。"}
    if not held_at:
        return {"ok": False, "error": "missing_held_at", "message": "会议时间不能为空。"}

    ref = f"{thread_id}:{message_id}" if thread_id and message_id else ""
    if ref:
        for m in list_meetings():
            if (m.get("from_message") or "") == ref:
                return {"ok": True, "duplicate": True, "meeting": m}

    meeting = create(
        title,
        "",
        kind="plan",
        held_at=held_at,
        host=str(draft.get("host") or ""),
        attendees=[str(a) for a in (draft.get("attendees") or []) if a],
        place=str(draft.get("place") or ""),
        topics=[str(t) for t in (draft.get("topics") or []) if t],
        source="chat",
        from_message=ref,
        planned_by=actor,
    )
    return {"ok": True, "duplicate": False, "meeting": meeting}


def annotate(thread_id: str, messages: list[dict[str, Any]]) -> None:
    """会话回放时补一句「这张卡已经建过会了」。

    回放拿到的是当时落库的 meta，里面没有「后来点没点确认」。不补这一笔，
    刷新页面后那张卡又变成可点的按钮，用户再点一次就会以为系统重复建会
    （服务端有幂等挡住了，但界面得让人看出来，不能靠后端兜着就当没这事）。
    """
    try:
        from ledger import list_meetings  # noqa: PLC0415
    except Exception:
        return
    built = {
        m.get("from_message"): m["id"]
        for m in list_meetings()
        if m.get("from_message")
    }
    for msg in messages:
        draft = (msg.get("meta") or {}).get("meet_draft")
        if not isinstance(draft, dict):
            continue
        mid = built.get(f"{thread_id}:{msg.get('id')}")
        if mid:
            draft["meeting_id"] = mid


def to_meta(draft: dict[str, Any] | None) -> dict[str, Any] | None:
    """落进会话历史的裁剪版。

    去掉 `unit_options`（几十个单位的清单，回放时没用，占一半体积）
    和 `clashes` 的细节；保留判断依据，因为回看时要能看懂当时为什么这么排。
    """
    if not draft:
        return None
    keep = ("title", "held_at", "when_text", "when_why", "place", "host",
            "attendees", "unknown_units", "topics", "missing", "assumptions",
            "notes", "ready")
    out = {k: draft.get(k) for k in keep if draft.get(k) not in (None, "", [])}
    out["clash_count"] = len(draft.get("clashes") or [])
    return out
