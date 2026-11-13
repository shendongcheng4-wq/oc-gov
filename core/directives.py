"""指示：受令 → 拟办 → 分办 → 回执。

## 为什么单独立这一层

系统里原本没有「领导说了什么」这个对象。督办事项的 `source` 是一句自由填的
说明，建一条只能有人在台账页手工填表；对话层能催办已有事项，却不能把一句话
变成几件交办。于是「上面说了、下面分头办、办得怎么样再报上来」这条链断在
第一环 —— 秘书团只会解读，不会受令。

这一层只补第一环，不多做：

1. **令是原文，不可修改。** 说错了就再说一条，不去覆盖旧记录。政务里「我什么
   时候说过什么」必须留得住，可编辑的原始记录等于没有原始记录。
2. **拟办是草稿，不落库。** 可以反复生成、反复改；落库只发生在「下发」那一次。
3. **下发必须过人的身份。** 逐条走 `authz.can(OVERSIGHT_CREATE)`，模型手上没有
   session，所以它永远只能拟、不能发。
4. **执行情况不另存状态。** 签收、进展、逾期全部从督办台账派生。另存一份必然
   会分叉，分叉之后「令说已办结、台账说逾期」，谁也说不清哪个算数。

## 一条令的生命周期

    拟办 ──（人点下发）──> 已下发 ──（台账事件流）──> 执行中 ──> 已办结
      │                        │
      └ 草稿，可弃            └ 每条任务拿到一个 DB 编号，责任单位在台账里签收

「执行中/已办结」是**派生**的，不是存的：只要有任何一条任务没办结，这条令就
没办结。存了就会出现「令已办结、任务还在办」这种自相矛盾。
"""

from __future__ import annotations

import os
import re
import threading
from datetime import date, datetime
from typing import Any, Callable

import directory
import oversight as ov
import store

FILE = "directives"
VERSION = 1

STATUS_DRAFT = "拟办"
STATUS_ISSUED = "已下发"

MAX_TASKS = 6            # 一条令最多拆几件事。再多就不是"一句话交办"，是任务书
MAX_TEXT = 4000          # 指示原文上限
MAX_STANDARD = 200
MIN_STANDARD = 6         # 少于这个字数的办结标准基本不可验证（「开工率100%」=7 字，要能过）

_LOCK = threading.RLock()


def _now() -> datetime:
    fixed = os.environ.get("OVERSIGHT_TODAY", "").strip()
    if fixed:
        return datetime.combine(date.fromisoformat(fixed), datetime.min.time()).replace(hour=10)
    return datetime.now()


def today() -> date:
    return _now().date()


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------
def _empty() -> dict[str, Any]:
    return {"version": VERSION, "items": []}


def _cfg() -> dict[str, Any]:
    raw = store.read(FILE, None)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return _empty()
    raw.setdefault("version", VERSION)
    return raw


def _save(doc: dict[str, Any]) -> None:
    store.write(FILE, doc)


# --------------------------------------------------------------------------
# 单位与时限：把口语收敛成系统认得的值
# --------------------------------------------------------------------------
def units() -> list[str]:
    return list(directory.UNITS)


def _unit_in(text: str) -> str:
    """从句子里认出责任单位。长名优先，避免「市城管局」被「城管局」截成半个。"""
    best = ""
    best_len = 0
    for name in directory.UNITS:
        if name in text and len(name) > best_len:
            best, best_len = name, len(name)
    if best:
        return best
    for alias, full in sorted(directory.UNIT_ALIAS.items(), key=lambda kv: -len(kv[0])):
        if alias in text:
            return full
    return ""


# --------------------------------------------------------------------------
# 这句话是「让谁去办」还是「问办得怎样」
# --------------------------------------------------------------------------
#
# 只服务一件事：交办句不许被知识库用一段台账摘录顶掉（见 core/metrics/ask.py
# 的 `_order_guard`）。领导说「让住建局 11 月 20 日前把 46 个小区全部开工」，
# 要的是一张能下发的拟办卡；系统回「检索到 2 条记录」，形式正常、内容全错。
#
# 判据必须**两条同时成立**：点到具体单位 + 带交办动作。只看动词会把
# 「推进会上说了什么」判成交办；只看单位会把「住建局有几件事」判成交办。
_ORDER_VERBS = (
    "牵头", "负责", "分头办", "承办", "限期", "务必", "落实", "完成",
    "开工", "建成", "整改", "清零", "接入", "交付", "办结", "销号", "排查",
)
_ORDER_LEADS = ("让", "叫", "请", "要求", "通知", "督促", "安排")
# 问句标记：命中任意一条就不算交办。
#
# 这里的取舍是单向的 —— 漏判退回「知识库照常答」，误判会把
# 「交办给住建局的事办到哪了」当成一句新交办，再建一条台账、责任单位多签一次字。
# 所以宁可多列几个词，把「情况」「进展」「如何」这类全算成问句。
_QUERY_MARKS = (
    "办到哪", "到哪了", "进展", "怎么样", "如何", "是否", "有没有",
    "多少", "几个", "几条", "哪些", "谁在办", "多久", "状态", "情况",
    "完成率", "进度", "汇报", "报告", "吗", "呢", "？", "?",
)


def looks_like_order(text: str) -> bool:
    """这句话像不像「让谁去干什么」，而不是「问一件事办得怎样」。"""
    s = (text or "").strip()
    if not s:
        return False
    if any(m in s for m in _QUERY_MARKS):
        return False
    if not _unit_in(s):
        return False
    if any(s.startswith(x) for x in _ORDER_LEADS):
        return True
    if any(v in s for v in _ORDER_VERBS):
        return True
    return "前" in s and ("完成" in s or "办结" in s)


_MONTH_DAY = re.compile(r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
_MONTH_END = re.compile(r"(\d{1,2})\s*月底")
_THIS_MONTH_END = re.compile(r"(?:本|这个|当)?月底")
_QUARTER_END = re.compile(r"[第]?([一二三四1-4])\s*季度末")
_YEAR_END = re.compile(r"(年底|年内|年末)")


def parse_deadline(text: str, base: date | None = None) -> str:
    """把「11 月 15 日前」「月底前」「年底前」换算成 YYYY-MM-DD。认不出返回空串。

    跨年的处理：12 月说「1 月 10 日前」，字面日期在今年已经过去了。差 300 天以上
    就按明年算 —— 这不是猜，是这句话唯一说得通的读法。反过来，11 月说「10 月 1 日
    前」差 40 天，照原样返回，由调用方按「时限已过」拒掉，而不是悄悄挪到明年。
    """
    base = base or today()
    s = (text or "").strip()
    if not s:
        return ""

    m = _MONTH_DAY.search(s)
    if m:
        year = int(m.group(1)) if m.group(1) else base.year
        try:
            d = date(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return ""
        if not m.group(1) and (base - d).days > 300:
            try:
                d = date(base.year + 1, d.month, d.day)
            except ValueError:
                return ""
        return d.isoformat()

    m = _MONTH_END.search(s)
    if m:
        month = int(m.group(1))
        return _month_end(base.year, month)

    if _THIS_MONTH_END.search(s):
        return _month_end(base.year, base.month)

    m = _QUARTER_END.search(s)
    if m:
        q = {"一": 1, "二": 2, "三": 3, "四": 4}.get(m.group(1), int(m.group(1)) if m.group(1).isdigit() else 0)
        month = q * 3
        return _month_end(base.year, month) if month else ""

    if _YEAR_END.search(s):
        return date(base.year, 12, 31).isoformat()

    # 已经是 ISO 写法
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return ""
    return ""


def _month_end(year: int, month: int) -> str:
    if month == 12:
        return date(year, 12, 31).isoformat()
    try:
        return date.fromordinal(date(year, month + 1, 1).toordinal() - 1).isoformat()
    except ValueError:
        return ""


_EMPTY_WORDS = (
    "加强", "切实", "高度", "认真", "重视", "确保", "进一步", "全力", "扎实",
    "有效", "严格", "牢牢", "坚决", "深入", "统筹", "推进", "做好", "抓好",
    "落实", "监管", "工作", "有关", "相关", "各项", "力度", "到位",
)


def vague_standard(text: str) -> bool:
    """办结标准是不是一句空话。

    判断方式很土但有效：把常见套话词和虚词抠掉，剩下的实词不足 4 个字，就说明
    这句话只表了态，没说法子。宁可让人补一句「新增泊位 1200 个」，也不要让一条
    永远无法验收的事项进台账 —— 那种事项最后只能靠"再催一次"结案。
    """
    s = (text or "").strip()
    if len(s) < MIN_STANDARD:
        return True
    rest = s
    for w in _EMPTY_WORDS:
        rest = rest.replace(w, "")
    rest = re.sub(r"[\s，,。；;、的和及与等对在为一二三四五六七八九十]", "", rest)
    return len(rest) < 4


# --------------------------------------------------------------------------
# 拟办：把一句话拆成可交办的事项
# --------------------------------------------------------------------------
_SPLIT = re.compile(r"[；;。\n]+")
_LEAD = re.compile(r"^(?:请|要求|需要|务必|抓紧|立即|马上|要|由)\s*")


def _clean_title(text: str) -> str:
    s = _LEAD.sub("", (text or "").strip())
    s = re.sub(r"^(?:市)?(?:政府)?(?:办)?(?:公室)?[:：]\s*", "", s)
    s = re.sub(r"\s+", "", s)
    return s[:60]


def _trim_unit(title: str, unit: str) -> str:
    """标题开头的单位名去掉。标题栏已经有「责任单位」了，正文再念一遍单位很蠢。"""
    if not unit or not title.startswith(unit):
        return title
    rest = title[len(unit):]
    rest = re.sub(r"^(?:牵头|负责|主办|承办|具体负责)?[，,：:、\s]*", "", rest)
    return rest or title


_MEASURE_TOKEN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|个|项|条|公里|天|户|家|人次|万元|亿元|台|套|处|座|所|间|平方米|亩|件)"
)
_DONE_TOKEN = re.compile(r"验收|交付|开工|接入|清零|办结|销号|通车|投入运行|投入使用|全部完成")

# 「11 月 15 日前」「月底前」这类时限短语。标题里已经有独立的时限字段了，
# 正文再念一遍会让 20 字的标题里 8 个字在说日期。
_DEADLINE_PHRASE = re.compile(
    r"(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]?\s*前?"
    r"|\d{1,2}\s*月底前?"
    r"|(?:本|这个|当)?月底前?"
    r"|年底前?|年内|季度末前?"
)


def _strip_deadline(text: str) -> str:
    out = _DEADLINE_PHRASE.sub("", text or "")
    out = re.sub(r"[，,]\s*(?:完成|办结|落实|前|之前)$", "", out)
    out = re.sub(r"[，,、]{2,}", "，", out).strip("，,、 ")
    return re.sub(r"\s{2,}", " ", out)


def _strip_unit(text: str, unit: str) -> str:
    """去掉句首的单位名和「负责/牵头」这类动词。单位另有字段，不必在正文重复。"""
    if not unit or not text.startswith(unit):
        return text
    rest = text[len(unit):]
    return re.sub(r"^(?:牵头|负责|主办|承办|具体负责)?[，,：:、\s]*", "", rest) or text


def _measure(text: str) -> str:
    """从原话里摘一句能量化验收的话当办结标准。

    只在没有模型时用。**摘，不是编**：句子里的数字和动作都是原文的，摘不出来
    就留空让人补。宁可让他多写一句，也不要让系统替他发明一个验收标准。
    """
    best = ""
    for clause in re.split(r"[，,。；;]", text or ""):
        c = _strip_unit(_strip_deadline(clause.strip()), _unit_in(clause))
        if not c:
            continue
        if _MEASURE_TOKEN.search(c) or _DONE_TOKEN.search(c):
            best = c
    return best[:MAX_STANDARD] if len(best) >= MIN_STANDARD else ""


def split_text(text: str) -> list[str]:
    """规则拆句。不做语义理解，只按分号和句号切，再把没有单位的短句并回上一句。

    这条路径只在没有模型时用。产出一定不完整（认不出隐含的责任单位），所以
    调用方要把 issues 带去界面，让人补，而不是假装拆好了。
    """
    raw = [s.strip() for s in _SPLIT.split(text or "") if s.strip()]
    out: list[str] = []
    for seg in raw:
        if out and (len(seg) < 10 or not _unit_in(seg)):
            # 没有单位、又很短：多半是上一句的补充说明（「并明确整改时限」）
            out[-1] = out[-1] + "，" + seg
            continue
        out.append(seg)
    return out[:MAX_TASKS]


_SYSTEM = """你是市政府办公室的拟办秘书。领导给一句话，或者转来一份上级来文，
你要把它拆成可以交办、可以验收的事项。

今天是 {today}。原文只说「11 月 15 日」这种不带年份的日期，一律按 {year} 年换算；
算出来已经过去的（比如 12 月说 1 月 10 日），按 {next_year} 年算，并在 assumptions 里
写一句。

每条只回答四件事：办什么（title）、谁办（assignee）、什么时候办完（deadline）、
办到什么程度算办完（standard）。

硬规矩：
1. assignee 只能从这份单位清单里选，一字不差：{units}
   拆不出责任单位的，assignee 留空字符串，**不要猜**。
2. deadline 用 YYYY-MM-DD。原文没写时限，留空字符串，**不要自己编一个**。
3. standard 必须可验证：有数量、有完成状态、有可检查的对象。「加强监管」
   「高度重视」「切实推进」这类不算，遇到就留空。
4. 一条指示最多拆 5 条。原文只说一件事就只给 1 条，不要为了显得勤快硬拆。
   同一单位、同一时限的相邻内容，如果后一半只是前一半的验收口径（「下降 30%」
   「达到 1200 个」「开工率 100%」这类纯指标），并进前一条的 standard，**不要单列一条**。
   单列会变成两份交办、两次签收，责任单位要跑两趟。
5. 原文里的数字、单位、时间原样保留，不要改写，也不要替原文加码。

只输出 JSON：
{{"tasks":[{{"title":"","assignee":"","deadline":"","standard":""}}],
"issues":["原文没说清的（没说谁办、没说时限、没说验收标准）"],
"assumptions":["你替原文补的，没有就空"]}}
"""


def _model_split(text: str, model_ref: str | None, base: date) -> dict[str, Any] | None:
    import llmclient  # noqa: PLC0415  # 没配模型时这里会抛 ModelUnavailable

    system = _SYSTEM.format(
        today=base.isoformat(),
        year=base.year,
        next_year=base.year + 1,
        units="、".join(directory.UNITS),
    )
    out = llmclient.chat_json(system, text, model_ref=model_ref, temperature=0.0, max_tokens=1200)
    data = out.get("data") or {}
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return None
    return {
        "tasks": tasks,
        "issues": [str(x) for x in (data.get("issues") or []) if str(x).strip()],
        "assumptions": [str(x) for x in (data.get("assumptions") or []) if str(x).strip()],
        "model": out.get("model"),
    }


# 同一处缺项会被说三遍：模型说一遍、规则兜底说一遍、清空猜测再补一句。卡上只留
# 信息量最大的那条，否则一条指示下面压五六行提示，用户反而看不见真正要补的。
_WHO_KEYS = ("谁办", "责任单位", "牵头")
_WHEN_KEYS = ("时限", "deadline", "日期", "办结日")
_HOW_KEYS = ("验收", "办结标准", "办到什么程度")


def _dedupe_issues(issues: list[str]) -> list[str]:
    """三类缺项各留一条。先出现的优先 —— 模型的原话最具体。"""
    groups = ((_WHO_KEYS, "who"), (_WHEN_KEYS, "when"), (_HOW_KEYS, "how"))
    seen: set[str] = set()
    out: list[str] = []
    for s in issues:
        tag = next((name for keys, name in groups if any(k in s for k in keys)), None)
        if tag and tag in seen:
            continue
        if tag:
            seen.add(tag)
        out.append(s)
    return out


def _normalize_task(row: dict[str, Any], base: date) -> tuple[dict[str, Any] | None, list[str]]:
    """把一条拟办整理成系统认得的形状，并记下为什么不能用。

    这里**不抛异常、不静默修**：认不出的单位就留空并说明，过期的时限就留着让
    下发时拒。拟办是给人看的草稿，能改；下发才是硬约束。
    """
    issues: list[str] = []
    title = _clean_title(str(row.get("title") or ""))
    if not title:
        return None, ["有一条没写清办什么，已跳过"]

    raw_unit = str(row.get("assignee") or "").strip()
    unit = directory.normalize_unit(raw_unit)
    if raw_unit and not unit:
        issues.append(f"「{title}」的责任单位「{raw_unit}」不在单位目录里，请重选")
    elif not raw_unit:
        issues.append(f"「{title}」没说谁办，请补责任单位")
    if unit:
        title = _trim_unit(title, unit)

    raw_dl = str(row.get("deadline") or "").strip()
    deadline = parse_deadline(raw_dl, base) if raw_dl else ""
    if raw_dl and not deadline:
        issues.append(f"「{title}」的时限「{raw_dl}」认不出，请写成 2026-11-15")
    elif not raw_dl:
        issues.append(f"「{title}」没写时限，请补一个办结日期")
    elif deadline:
        # 时限已经单独成字段，标题里再留一遍「11 月 15 日前」纯属占字数
        title = _strip_deadline(title) or title
        if date.fromisoformat(deadline) < base:
            # 模型偶尔会把年份算错（实测把 2026 写成 2025）。不静默改 ——
            # 改对了没人知道，改错了就是把「明年」办成了「去年」。
            issues.append(f"「{title}」的时限 {deadline} 已经过去，请改成将来的日期")

    standard = str(row.get("standard") or "").strip()[:MAX_STANDARD]
    if not standard:
        issues.append(f"「{title}」缺可验证的办结标准（做到什么程度算办完）")
    elif vague_standard(standard):
        issues.append(f"「{title}」的办结标准太笼统（「{standard}」），验收不了")
        standard = ""

    co: list[str] = []
    for name in row.get("co_assignees") or []:
        u = directory.normalize_unit(str(name))
        if u and u != unit and u not in co:
            co.append(u)

    return (
        {
            "title": title,
            "assignee": unit or "",
            "co_assignees": co,
            "deadline": deadline,
            "standard": standard,
        },
        issues,
    )


def draft(
    text: str,
    *,
    base: date | None = None,
    use_model: bool = True,
    model_ref: str | None = None,
) -> dict[str, Any]:
    """生成一份拟办草稿。**不落库**，也不校验权限 —— 那是下发时要过的关。

    没有模型、或模型调不通时退回规则拆句：给出一条按原话记的事项，责任单位和
    时限留空并写进 issues。降级不是失败，编一个单位才是。
    """
    raw = (text or "").strip()[:MAX_TEXT]
    if not raw:
        return {"ok": False, "error": "指示原文不能为空。"}

    base = base or today()
    planner = "rule"
    issues: list[str] = []
    assumptions: list[str] = []
    model_name = None
    rows: list[dict[str, Any]] = []

    if use_model:
        try:
            got = _model_split(raw, model_ref, base)
        except Exception:  # noqa: BLE001 模型不可用不该让拟办整条挂掉
            got = None
        if got and got.get("tasks"):
            planner = "model"
            model_name = got.get("model")
            rows = [r for r in got["tasks"] if isinstance(r, dict)][:MAX_TASKS]
            issues.extend(got.get("issues") or [])
            assumptions.extend(got.get("assumptions") or [])

    if not rows:
        rows = [
            {
                "title": _clean_title(seg),
                "assignee": _unit_in(seg),
                "deadline": parse_deadline(seg, base),
                # 摘一句原文里能量化的话当验收标准；摘不出来就留空，等人在卡上补
                "standard": _measure(seg),
            }
            for seg in split_text(raw)
        ]
        if planner == "model":
            planner = "rule"  # 模型给了空任务，等于没给
        issues.append("按原话拆的，单位和办结标准可能不全，请逐条核一遍")

    tasks: list[dict[str, Any]] = []
    # 模型有时一边在 issues 里写「未明确责任单位」，一边还是把单位填上了。填的那个
    # 是它替领导定的，不是原文说的。原文没点名就清空，留给人来选 —— 交办错单位的
    # 代价，比在卡上多点一次大得多。
    hedged = any(
        k in "".join(issues)
        for k in ("未明确责任单位", "没说谁办", "未指定责任单位", "未明确牵头", "未指定牵头")
    )
    for row in rows:
        if hedged:
            raw_unit = str(row.get("assignee") or "").strip()
            fixed = directory.normalize_unit(raw_unit) or raw_unit
            if raw_unit and not directory.mentioned_in(raw, fixed):
                row = {**row, "assignee": ""}
                note = f"（模型原填「{raw_unit}」已清空，请确认后重选）"
                title = _clean_title(str(row.get("title") or ""))
                for i, s in enumerate(issues):
                    if any(k in s for k in _WHO_KEYS):
                        if "已清空" not in s:
                            issues[i] = f"{s}{note}"
                        break
                else:
                    issues.append(f"「{title}」原文没点谁牵头" + note)
        task, why = _normalize_task(row, base)
        if task:
            task["no"] = len(tasks) + 1
            tasks.append(task)
        issues.extend(why)

    if not tasks:
        return {"ok": False, "error": "这段原文里没有可交办的事项。", "issues": issues}

    return {
        "ok": True,
        "text": raw,
        "tasks": tasks[:MAX_TASKS],
        "issues": _dedupe_issues(issues)[:8],
        "assumptions": assumptions[:4],
        "planner": planner,
        "model": model_name,
    }


# --------------------------------------------------------------------------
# 下发
# --------------------------------------------------------------------------
def _new_id(doc: dict[str, Any], base: date) -> str:
    used = []
    for d in doc.get("items") or []:
        did = str(d.get("id") or "")
        if did.startswith(f"ZS{base.year}-"):
            tail = did.split("-", 1)[1]
            if tail.isdigit():
                used.append(int(tail))
    return f"ZS{base.year}-{(max(used) + 1) if used else 1:03d}"


def validate_tasks(tasks: list[dict[str, Any]], base: date | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """下发前的硬校验。返回（可用的任务，问题清单）。有任何一个问题就不许下发。"""
    base = base or today()
    out: list[dict[str, Any]] = []
    problems: list[str] = []
    if not isinstance(tasks, list) or not tasks:
        return [], ["至少要有一条交办事项。"]
    if len(tasks) > MAX_TASKS:
        return [], [f"一次最多下发 {MAX_TASKS} 条，收到 {len(tasks)} 条。"]

    for idx, row in enumerate(tasks, 1):
        if not isinstance(row, dict):
            problems.append(f"第 {idx} 条格式不对")
            continue
        title = _clean_title(str(row.get("title") or ""))
        if not title:
            problems.append(f"第 {idx} 条没写办什么")
            continue
        unit = directory.normalize_unit(str(row.get("assignee") or ""))
        if not unit:
            problems.append(f"第 {idx} 条的责任单位「{row.get('assignee') or '空'}」不在单位目录里")
            continue
        dl = parse_deadline(str(row.get("deadline") or ""), base)
        if not dl:
            problems.append(f"第 {idx} 条没有合法时限")
            continue
        if date.fromisoformat(dl) < base:
            problems.append(f"第 {idx} 条时限 {dl} 早于今天，交办出去就是逾期")
            continue
        standard = str(row.get("standard") or "").strip()[:MAX_STANDARD]
        if not standard:
            problems.append(f"第 {idx} 条缺办结标准")
            continue
        if vague_standard(standard):
            problems.append(f"第 {idx} 条的办结标准验收不了（「{standard}」）")
            continue
        co = []
        for name in row.get("co_assignees") or []:
            u = directory.normalize_unit(str(name))
            if not u:
                problems.append(f"第 {idx} 条的协办单位「{name}」不在单位目录里")
                continue
            if u != unit and u not in co:
                co.append(u)
        out.append(
            {
                "no": idx,
                "title": title,
                "assignee": unit,
                "co_assignees": co,
                "deadline": dl,
                "standard": standard,
            }
        )
    return out, problems


def _recent_duplicate(text: str, tasks: list[dict[str, Any]]) -> str:
    """30 分钟内下过一模一样的一条吗。

    为什么需要：拟办卡挂在消息上，刷新一次会重新画出来。没有这道闸，用户看到
    卡片、「刚才是不是没点成」，再点一次，责任单位就收到两份一模一样的交办。
    只认「原文 + 三条关键字段完全相同」且 30 分钟内 —— 过了这个窗口，同样的
    话再交办一次是合理的新指令，不该挡。
    """
    now = _now()
    for d in _cfg().get("items") or []:
        if d.get("status") != STATUS_ISSUED:
            continue
        if str(d.get("text") or "").strip() != text.strip():
            continue
        try:
            issued = datetime.strptime(str(d.get("issued_at") or ""), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if (now - issued).total_seconds() > 1800:
            continue
        old = {(t.get("title"), t.get("assignee"), t.get("deadline")) for t in d.get("tasks") or []}
        new = {(t.get("title"), t.get("assignee"), t.get("deadline")) for t in tasks}
        if old and old == new:
            return str(d.get("id"))
    return ""


def issue(
    text: str,
    tasks: list[dict[str, Any]],
    *,
    person: str,
    from_name: str = "",
    source: str = "市长口述",
    source_ref: str = "",
    origin: dict[str, str] | None = None,
    can_fn: Callable[[tuple[str, ...]], tuple[bool, str]] | None = None,
    base: date | None = None,
) -> dict[str, Any]:
    """把一份拟办落成「令 + N 条督办事项」。要么全成，要么一条都不留。

    `can_fn` 由调用方（app/main.py）注入，签名是 (units) -> (allowed, why)。
    不在这里 import authz 是为了让这个模块能被单测直接驱动，不必造 session。

    `origin` 是这条令的出生地（thread_id / message_id）：领导在对话里说的那句话。
    存它不是为了做审计留痕，是因为**同一句话可以说两遍**，回放时只有 id 能分清
    哪张卡对应哪条令，比原文更可靠。
    """
    base = base or today()
    raw = (text or "").strip()[:MAX_TEXT]
    if not raw:
        return {"ok": False, "error": "指示原文不能为空。"}
    clean, problems = validate_tasks(tasks, base)
    if problems:
        return {"ok": False, "error": "；".join(problems[:4]), "problems": problems}

    dup = _recent_duplicate(raw, clean)
    if dup:
        return {
            "ok": False,
            "error": f"这条指示刚下发过（{dup}），没有重复建单。",
            "duplicate": dup,
        }

    # 权限：逐条判，一条不过就整批不办。半份交办比不办更糟 ——
    # 责任单位收到的那几条会在办，没收到的那几条谁也不知道还没下发。
    if can_fn is not None:
        for t in clean:
            allowed, why = can_fn(tuple([t["assignee"], *t["co_assignees"]]))
            if not allowed:
                return {
                    "ok": False,
                    "error": f"第 {t['no']} 条不能交办给{t['assignee']}：{why}",
                    "problems": [f"第 {t['no']} 条：{why}"],
                }

    with _LOCK:
        doc = _cfg()
        did = _new_id(doc, base)
        stamp = _now().strftime("%Y-%m-%d %H:%M")
        record: dict[str, Any] = {
            "id": did,
            "text": raw,
            "source": (source or "市长口述").strip()[:40],
            "source_ref": (source_ref or "").strip()[:80],
            "from": person,
            "from_name": (from_name or person).strip()[:20],
            "created_at": stamp,
            "issued_at": stamp,
            "status": STATUS_ISSUED,
            "tasks": clean,
        }
        # 出生地：哪次对话的哪条消息。字段全空就不写，别在数据里留个空壳。
        if origin and (origin.get("thread_id") or origin.get("message_id")):
            record["origin"] = {
                "thread_id": str(origin.get("thread_id") or "")[:64],
                "message_id": str(origin.get("message_id") or "")[:64],
            }

        created: list[str] = []
        for t in clean:
            res = ov.create_item(
                title=t["title"],
                assignee=t["assignee"],
                deadline=t["deadline"],
                standard=t["standard"],
                source="领导指示",
                source_ref=f"{did}（{record['from_name']}）",
                co_assignees=t["co_assignees"],
                priority="重点" if len(clean) == 1 else "一般",
                actor=person,
            )
            if not res.get("ok"):
                # 走到这里说明校验和建档的规则不一致了。回滚已建的，绝不留下半份。
                for iid in created:
                    ov.remove_item(iid, actor=person)
                return {
                    "ok": False,
                    "error": f"第 {t['no']} 条没建起来：{res.get('error') or '未知原因'}",
                    "problems": [str(res.get("error") or "")],
                }
            item_id = str(res.get("item_id") or "")
            created.append(item_id)
            t["item_id"] = item_id

        doc.setdefault("items", []).append(record)
        _save(doc)

    return {
        "ok": True,
        "id": did,
        "status": STATUS_ISSUED,
        "issued_at": stamp,
        "item_ids": [t["item_id"] for t in clean],
        "tasks": clean,
    }


# --------------------------------------------------------------------------
# 查询与回报（执行情况全部从台账派生）
# --------------------------------------------------------------------------
def get(did: str) -> dict[str, Any] | None:
    for d in _cfg().get("items") or []:
        if str(d.get("id")) == str(did):
            return d
    return None


def _visible(d: dict[str, Any], scope: dict[str, Any] | None) -> bool:
    """谁能看这条令。

    市领导看全部；其他人只看「动到我分管的单位」或「我自己下的」。
    这条收窄不是可选项：指示原文里往往有还没公开的判断，一个局长不该能翻
    市长所有口头交办。
    """
    if not scope:
        return False
    if scope.get("is_citywide"):
        return True
    if str(d.get("from")) == str(scope.get("person")):
        return True
    mine = {str(u) for u in (scope.get("units") or ())}
    for t in d.get("tasks") or []:
        if str(t.get("assignee")) in mine:
            return True
        if mine & {str(x) for x in (t.get("co_assignees") or [])}:
            return True
    return False


def _task_state(t: dict[str, Any]) -> dict[str, Any]:
    iid = str(t.get("item_id") or "")
    item = ov.get(iid) if iid else None
    if item is None:
        return {
            "item_id": iid,
            "status": "未落库",
            "risk": "—",
            "days_left": None,
            "percent": 0,
            "signed": False,
        }
    return {
        "item_id": iid,
        "status": item.status,
        "risk": ov.assess(item).level,
        "days_left": item.days_left,
        "percent": item.progress_percent,
        "signed": item.signed,
    }


def summary(did: str, scope: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """一条令的执行情况。状态、逾期、签收率全部现算，不读缓存字段。"""
    d = get(did)
    if d is None or not _visible(d, scope):
        return None
    rows: list[dict[str, Any]] = []
    for t in d.get("tasks") or []:
        st = _task_state(t)
        rows.append(
            {
                "no": t.get("no"),
                "title": t.get("title"),
                "assignee": t.get("assignee"),
                "co_assignees": t.get("co_assignees") or [],
                "deadline": t.get("deadline"),
                "standard": t.get("standard"),
                **st,
            }
        )
    total = len(rows)
    signed = sum(1 for r in rows if r["signed"])
    closed = sum(1 for r in rows if r["status"] == ov.CLOSED)
    overdue = sum(1 for r in rows if r["risk"] == ov.RISK_OVERDUE)
    unsigned = sum(1 for r in rows if r["status"] == ov.PENDING)
    return {
        "id": d.get("id"),
        "text": d.get("text"),
        "source": d.get("source"),
        "source_ref": d.get("source_ref"),
        "from_name": d.get("from_name"),
        "issued_at": d.get("issued_at"),
        "status": d.get("status"),
        "phase": _phase(total, closed, overdue),
        "total": total,
        "signed": signed,
        "unsigned": unsigned,
        "closed": closed,
        "overdue": overdue,
        "tasks": rows,
    }


def _phase(total: int, closed: int, overdue: int) -> str:
    if total and closed >= total:
        return "已办结"
    if closed:
        return "部分办结"
    if overdue:
        return "有逾期"
    return "执行中"


def list_all(scope: dict[str, Any] | None = None, limit: int = 20) -> list[dict[str, Any]]:
    rows = [d for d in (_cfg().get("items") or []) if _visible(d, scope)]
    rows.sort(key=lambda d: str(d.get("issued_at") or d.get("created_at") or ""), reverse=True)
    return [
        {
            "id": d.get("id"),
            "text": d.get("text"),
            "from_name": d.get("from_name"),
            "issued_at": d.get("issued_at"),
            "status": d.get("status"),
            "total": len(d.get("tasks") or []),
        }
        for d in rows[:limit]
    ]


def scan(scope: dict[str, Any] | None = None, days: int = 7) -> dict[str, Any]:
    """最近下发的令及其执行情况，供对话层回答「我上次那个指示怎么样了」。

    只回最近 `days` 天，且只挑有动静的（有逾期、有未签收、有未办结），
    否则会把半年的陈年交办一股脑端上来。全文不截断，但最多 5 条。
    """
    base = today()
    rows = []
    for d in _cfg().get("items") or []:
        if not _visible(d, scope):
            continue
        s = summary(str(d.get("id")), scope)
        if not s:
            continue
        issued = str(d.get("issued_at") or "")[:10]
        if issued:
            try:
                if (base - date.fromisoformat(issued)).days > days:
                    continue
            except ValueError:
                pass
        if s["closed"] >= s["total"] and s["total"]:
            continue
        rows.append(s)
    rows.sort(key=lambda s: str(s.get("issued_at") or ""), reverse=True)
    return {
        "directives": rows[:5],
        "touched": len(rows),
        "window_days": days,
        "as_of": base.isoformat(),
    }
