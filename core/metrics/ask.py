"""问数解析层：自然语言 → 指标 → 接口 → 带出处的答案。

生产环境里这一层是"大模型 + 指标字典"的组合：模型负责理解意图，
系统负责把指标绑定到已注册接口、把数值原样搬进答案。原型的机器上没有模型服务，
所以这里用确定性规则替代模型，好处是链路可复现、可断言——
只要这个骨架在，把"规则"换成"模型决策"不需要动下面的取值和出处逻辑。

三条硬规则：
1. 数值只能来自接口返回，不允许推算、插值、凭印象补。
2. 每个数值必须带上出处（部门 / 报表 / 期次 / 披露日 / 接口路径）。
3. 对不上指标就明说没注册，并给出已注册清单，不猜。
"""

from __future__ import annotations

import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

# core/ 要进路径：专家团是 core 层模块，不是 metrics 子目录里的
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chitchat as chitchat_mod  # noqa: E402
import experts as experts_mod  # noqa: E402
import extdata as extdata_mod
import knowledge as knowledge_mod
import llm
import llmclient
import meetplan as meetplan_mod
import mockdata as md
import modelcfg
import people as people_mod
import rag as rag_mod

# --------------------------------------------------------------------------
# 指标匹配
# --------------------------------------------------------------------------

_ALIAS_INDEX: list[tuple[str, str]] = sorted(
    ((alias, code) for code, meta in md.METRICS.items() for alias in meta["aliases"]),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def match_metrics(question: str) -> list[dict[str, Any]]:
    """按别名长度从长到短匹配，命中就记录下来（支持一句话里多个指标）。"""
    hits: list[dict[str, Any]] = []
    consumed: list[str] = []
    for alias, code in _ALIAS_INDEX:
        if alias in question and code not in {h["code"] for h in hits}:
            # 更长的别名已覆盖的片段不再重复计分
            if any(alias in c for c in consumed):
                continue
            hits.append({"code": code, "alias": alias, "name": md.METRICS[code]["name"]})
            consumed.append(alias)
    return hits


# --------------------------------------------------------------------------
# 期次解析
# --------------------------------------------------------------------------

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
           "十一": 11, "十二": 12}


def _num(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token)


def parse_period(question: str, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    latest = md.latest_available(today)
    latest_y, latest_m = int(latest[:4]), int(latest[5:7])

    m = re.search(r"(?:(\d{4})年)?(\d{1,2}|十[一二]?|[一二三四五六七八九])月", question)
    if m:
        year = int(m.group(1)) if m.group(1) else latest_y
        month = _num(m.group(2))
        if month and 1 <= month <= 12:
            period = f"{year:04d}-{month:02d}"
            if not m.group(1) and month > today.month:
                # 没写年份又说了一个还没到的月份，理解为去年
                period = f"{year - 1:04d}-{month:02d}"
            elif not m.group(1) and month <= today.month:
                # 本月/上个月这类说法按今年算；若该月尚未披露，交给 resolve_period 说明
                period = f"{today.year:04d}-{month:02d}"
            return {"period": period, "months": 1, "why": f"问题里指定了 {m.group(0)}"}

    if any(w in question for w in ("本月", "这个月", "当月")):
        return {"period": f"{today.year:04d}-{today.month:02d}", "months": 1, "why": "问的是本月"}

    if any(w in question for w in ("上月", "上个月")):
        prev_y, prev_m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        return {"period": f"{prev_y:04d}-{prev_m:02d}", "months": 1, "why": "问的是上月"}

    if "去年同期" in question or "同比" in question:
        y, mo = int(latest[:4]) - 1, int(latest[5:7])
        return {"period": f"{y:04d}-{mo:02d}", "months": 1, "why": "问的是去年同期"}

    m = re.search(r"近\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*(?:个)?月", question)
    if m and _num(m.group(1)):
        return {"period": None, "months": min(24, _num(m.group(1)) or 12), "why": f"取最近 {m.group(1)} 个月"}

    if "上半年" in question:
        return {"period": None, "months": 6, "why": "看近半年走势"}
    if "今年" in question or "本年" in question:
        return {"period": None, "months": latest_m, "why": "看今年以来的走势"}

    return {"period": None, "months": 1, "why": f"未指定期次，取最新可得期（{md._period_label(latest)}）"}


# --------------------------------------------------------------------------
# 意图识别
# --------------------------------------------------------------------------

_DIM_WORDS = {
    "区县": ["区县", "各区", "分区", "县区", "板块"],
    "责任单位": ["部门", "单位", "局", "承办"],
}


def parse_intent(question: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    if any(k in question for k in ("口径", "怎么算", "怎么统计", "定义", "什么指标", "包括哪些", "统计范围")):
        return {"intent": "definition", "why": "问的是指标口径/定义"}
    if any(k in question for k in ("对比", "比较", "相比", "谁高", "哪个高", "横向")):
        return {"intent": "compare", "why": "要横向对比"}
    if any(k in question for k in ("排名", "排序", "最高", "最低", "哪些", "倒数", "差距")):
        return {"intent": "breakdown", "why": "要分维度排序"}
    for dim, words in _DIM_WORDS.items():
        if any(w in question for w in words):
            return {"intent": "breakdown", "dim": dim, "why": f"要看{dim}分解"}
    if any(k in question for k in ("趋势", "走势", "变化", "最近", "近几", "这半年", "曲线")):
        return {"intent": "trend", "why": "要看时间序列"}
    if len(hits) >= 2:
        return {"intent": "compare", "why": "一句话里提到了多个指标"}
    return {"intent": "value", "why": "问单个数值"}


# --------------------------------------------------------------------------
# 答案组织
# --------------------------------------------------------------------------


def _fmt(value: float | None, unit: str) -> str:
    """按单位出数。之前只写了「户」和默认「%」两个分支，
    新增的亿元/个/项会被当成百分比，出现「1286.5%」这种硬错。"""
    if value is None:
        return "—"
    if unit == "%":
        return f"{value:g}%"
    if unit == "亿元":
        return f"{value:,.1f} 亿元"
    return f"{value:,.0f} {unit}".strip()


def _period_text(result: dict[str, Any]) -> str:
    """累计口径的指标不能只说「2026年9月」，要说清是 1—9 月累计。"""
    meta = md.METRICS.get(result.get("code", ""), {})
    label = result.get("period_label", "")
    if not meta.get("cumulative") or len(label) < 7:
        return label
    try:
        year = int(label[:4])
        month = int(label[5:-1])
    except (ValueError, TypeError):
        return label
    if month == 12:
        return f"{year}年全年"
    return f"{year}年1—{month}月累计"


def _delta_text(delta: float | None, unit: str, label: str) -> str:
    """把差值写成话，而不是符号。

    之前是「较上月↑1.7个百分点」，箭头紧贴数字，读起来像「11.7」，
    在汇报材料里是会被当成错数的。这里改成动词式表达。
    """
    if delta is None:
        return ""
    if delta == 0:
        return f"{label}持平"
    if unit == "%":
        verb = "上升" if delta > 0 else "回落"
        return f"{label}{verb} {abs(delta):g} 个百分点"
    verb = "增加" if delta > 0 else "减少"
    suffix = {"亿元": "亿元", "户": "户", "项": "项", "个": "个"}.get(unit, unit)
    return f"{label}{verb} {abs(delta):g} {suffix}".strip()


def _yoy_text(result: dict[str, Any], label: str = "") -> str:
    """同比怎么说，取决于指标是「总量」还是「比率」。

    比率类（unit == "%"）的同比是**百分点差**，直接用 yoy；
    总量类（亿元/户/项/个）的同比是**增长率**，在 yoy_pct 里。
    真实值（如实测年度 GDP）只有可比价增速、没有上年同期绝对量，yoy 为 None，
    这时不能退回绝对增量，也不能拿总量除增速反推 —— 现价与可比价不能相互推算。

    这条规则和 workbench._fmt_yoy、briefdoc._yoy_text 是同一套，三处别各写一份。
    """
    if (result.get("unit") or "") == "%":
        return _delta_text(result.get("yoy"), "%", label)
    pct = result.get("yoy_pct")
    if pct is None:
        return ""
    if pct == 0:
        return f"{label}持平"
    return f"{label}{'增长' if pct > 0 else '下降'} {abs(pct):g}%"


def _source_brief(source: dict[str, Any]) -> dict[str, Any]:
    return {
        # 前端点来源胶囊要用它打开口径抽屉。老会话的 meta 里没这个字段，
        # 前端另有从 api 反解的兜底。
        "code": source.get("code"),
        "department": source["department"],
        "report": source["report"],
        "period": source["period"],
        "published_at": source["published_at"],
        "api": source["api"],
        "definition": source["definition"],
    }


def _same_number(a: object, b: object) -> bool:
    """两个数是不是同一个值（忽略 %, 千分位, 全角）。用于判断「材料与库一致」。"""

    def key(s: object) -> float | None:
        t = str(s or "").replace("％", "%").replace(",", "").replace(" ", "").rstrip("%")
        try:
            return float(t)
        except ValueError:
            return None

    ka, kb = key(a), key(b)
    return ka is not None and kb is not None and abs(ka - kb) < 1e-9


def _material_compare(refs: list[dict[str, Any]], result: dict[str, Any]) -> list[dict[str, Any]]:
    """把材料里的数值和本轮取的库值配成「对照」两行。

    B 档（2026-11-03 定）的落点：材料数字**只**出现在这里，不进 headline、
    不进 facts、不进专家解读。它解决的是用户拿着月报来问、发现跟系统说的不一样
    那种场景 —— 两个数各自带出处并排放，用户自己判断，而不是系统替他选一个。

    配不上就不配，理由是错配比不显示更糟：材料里的 7.2% 若是「1-9 月累计」，
    而库里 5.6% 是「9 月当月」，两个数都对，硬摆成"对不上"会让人以为数据错。
    所以只在单指标（value / trend）场景做，且材料指标必须是本轮的主指标。
    """
    if not refs or result.get("refused"):
        return []
    if result.get("intent") not in ("value", "trend"):
        return []
    codes = result.get("metric_codes") or []
    facts = result.get("facts") or []
    if not codes or not facts:
        return []
    lib_value = str(facts[0].get("value") or "")
    if not lib_value:
        return []
    src = (result.get("sources") or [{}])[0]
    out: list[dict[str, Any]] = []
    for r in refs:
        if r.get("code") != codes[0]:
            continue                     # 材料说的是另一个指标，不能并到这一行
        out.append(
            {
                "name": r.get("name"),
                "file": r.get("file"),
                "excerpt": r.get("excerpt"),
                "material_value": r.get("written"),
                "library_value": lib_value,
                "library_label": str(facts[0].get("label") or result.get("period_label") or ""),
                "library_department": str(src.get("department") or ""),
                "library_report": str(src.get("report") or ""),
                "same": _same_number(r.get("written"), lib_value),
            }
        )
    return out[:3]


def _plan_attempts(model_ref: str | None, limit: int = 3) -> list[str]:
    """这一轮可以试哪几个模型：先指定的（或全局默认），再其它可用模型。

    为什么不只试一个：planner 一挂就直接掉到规则解析，答案质量是断崖式的
    （规则只认别名，问法一变就落空），而用户界面上一模一样，看不出换了引擎。
    换个 provider 重试一次的成本远低于答错一次。
    """
    primary = model_ref or modelcfg.active_ref()
    refs = [primary] if primary else []
    for item in modelcfg.resolved():
        ref = item.get("ref")
        if ref and ref not in refs:
            refs.append(ref)
    return refs[:limit]


# 单次 planner 调用的超时。第一次给足（模型可能冷启动），重试收紧 ——
# 已经失败过一次了，再等 30 秒不如早点降级。
_PLAN_TIMEOUT_FIRST = 30.0
_PLAN_TIMEOUT_RETRY = 20.0
# 整段解析最多花多久。全部模型都网络超时时，不能让用户干等两分钟才看到规则答案。
_PLAN_BUDGET = 75.0


def _short_err(exc: BaseException) -> str:
    """把失败压成一句能给人看的话。

    原始异常里常带着上游的完整响应体（401 那句会回显 key 的后四位），而这些
    字符串会跟着 degraded 落进会话记录、也可能出现在界面上。诊断需要的只是
    「哪个模型、什么样的失败」，不需要把对方返回的 JSON 原样存下来。
    """
    text = str(exc)
    head = text.split("{", 1)[0].strip().rstrip(":")
    return head[:120] or type(exc).__name__


def _plan_question(
    question: str,
    today: date,
    history: list[dict[str, Any]] | None = None,
    model_ref: str | None = None,
    known_metrics: list[str] | None = None,
    should_stop: Any = None,
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
    """用模型做意图解析；失败按「同模型重试 → 换模型 → 退回规则」逐级降级。

    返回 (plan, planner, degraded)。`planner` 直接进 trace 和界面文案，
    所以取值要能自解释：llm / llm_retry / llm_fallback / rules / llm_failed。

    `degraded` 非空表示「这次的结果不是首选路径给的」。它必须能传到界面上：
    静默换引擎等于在用户不知情的情况下改变答案质量，这比慢两秒严重得多。
    """
    if not llm.available():
        return None, "rules", {}

    attempts = _plan_attempts(model_ref)
    if not attempts:
        return None, "rules", {}

    started = time.monotonic()
    errors: list[str] = []
    tried = 0

    for idx, ref in enumerate(attempts):
        # 每个模型最多两次：一次原样重试。再多了就是在赌对方恢复，赌注是用户的等待。
        for retry in range(2):
            if should_stop is not None and should_stop():
                raise TurnCancelled()
            if tried and time.monotonic() - started > _PLAN_BUDGET:
                errors.append("超出解析时间预算，放弃继续尝试")
                break
            timeout = _PLAN_TIMEOUT_FIRST if not tried else _PLAN_TIMEOUT_RETRY
            tried += 1
            try:
                plan = llm.plan(
                    question,
                    today.isoformat(),
                    history=history,
                    model_ref=ref,
                    known_metrics=known_metrics,
                    attachments=attachments,
                    timeout=timeout,
                )
            except llmclient.ModelUnavailable:
                return None, "rules", {}
            except Exception as exc:  # noqa: BLE001 网络/额度/解析失败都不该让问数不可用
                errors.append(f"{ref}: {_short_err(exc)}")
                # 网络类失败重试同一个模型基本是白等（换 provider 才有救），
                # 所以只对「可能有瞬时抖动」的情形再试一次。
                if not llmclient.retryable(exc) or getattr(exc, "kind", "") == "network":
                    break
                if retry == 0:
                    time.sleep(0.4)
                continue

            if idx == 0 and retry == 0:
                return plan, "llm", {}
            if idx == 0:
                return plan, "llm_retry", {
                    "kind": "retry",
                    "reason": f"模型第一次调用失败，重试后成功：{errors[-1][:160]}",
                    "model": ref,
                }
            return plan, "llm_fallback", {
                "kind": "fallback",
                "reason": "首选模型不可用，已改用备用模型解析",
                "from": attempts[0],
                "model": ref,
            }

        if time.monotonic() - started > _PLAN_BUDGET:
            break

    return None, "llm_failed: " + " / ".join(errors)[:200], {
        "kind": "rules",
        "reason": "模型解析失败，已退回规则解析（只认指标别名，问法一变就可能落空）",
        "errors": errors[:4],
    }


# 事项类话术。出现这些词，说明用户要的是「这件事办到哪了」，不是「这个数是多少」。
# 两者在指标字典里可能命中同一个指标（生态环境局既管事项也管指标），但答案完全不同。
_MATTER_WORDS = (
    "办到哪", "办理", "进展", "落实", "推进", "交办", "督办", "谁在办",
    "定了什么", "怎么定", "办结", "逾期", "签收", "台账", "开会", "会议",
)


def _wants_matter(question: str) -> bool:
    return any(w in str(question or "") for w in _MATTER_WORDS)


def _codes_from_result(result: dict[str, Any]) -> list[str]:
    """从 sources 的接口路径反推本轮用了哪些指标。

    比让每个 return 分支各自声明更省事，也不会漏分支；`api` 字段本来就要求
    「一个都不能少」（见 README 的接真实数据三条约束）。
    """
    codes: list[str] = []
    for source in result.get("sources") or []:
        match = re.search(r"/metrics/([A-Z0-9_]+)\s*$", str(source.get("api") or ""))
        if match:
            code = match.group(1)
            if code in md.METRICS and code not in codes:
                codes.append(code)
    return codes


def _expert_refs(
    question: str,
    result: dict[str, Any],
    person_id: str | None,
    expert_ids: list[str] | None,
) -> dict[str, list[dict[str, Any]]]:
    """每位参谋能读到的那几条知识库记录：`{专家 id: [条目]}`。

    **索引只建一次。** 三位参谋各查一次，但台账是同一份，建三遍纯属白等——
    现在几十条感觉不出来，等台账上千条就是三倍。所以先按身份建全域索引，
    再逐域过滤。顺序不能反：身份是「他能看什么」，域是「他关心什么」，
    能看的永远优先。

    检索句用「问句 + 指标名」而不是只用问句：问「经济形势怎么样」这种泛问句，
    光靠问句在口径条目上一个二字组都命中不了；加上指标名，至少能稳定命中
    《XX 口径》那一条，参谋才不至于两手空空。

    检索不到就返回空列表，由 `experts._refs_block` 如实写「本域内无记录」。
    这里不兜底去全域找 —— 让经济参谋拿河道巡查的记录来发挥，比让它闭嘴糟得多。
    """
    ids = [str(x) for x in (expert_ids or [])][: experts_mod.MAX_PER_CALL]
    if not ids:
        return {}
    person = people_mod.meta(person_id)
    terms = " ".join([str(question or "")] + [str(n) for n in (result.get("metric_names") or [])])
    entries = knowledge_mod.index(person)
    out: dict[str, list[dict[str, Any]]] = {}
    for eid in ids:
        expert = experts_mod.find(eid)
        if expert is None:
            continue
        domain = expert.get("domain")
        scoped = [e for e in entries if knowledge_mod.in_domain(e, domain)]
        out[eid] = knowledge_mod.search_in(scoped, terms, experts_mod.REF_LIMIT)
    return out


class TurnCancelled(Exception):
    """用户在客户端点了「停止」。

    只在「步骤边界」抛出（每次给前端推阶段之前）：模型请求本身是阻塞的
    urllib 调用，中途掐不断，但解析完立刻停，至少不会再串行喊三位专家。
    usage 挂在异常上，让上层能把已经花掉的 token 记进账里。
    """

    def __init__(self, usage: dict[str, Any] | None = None) -> None:
        super().__init__("turn_cancelled")
        self.usage = usage or {}


def ask(
    question: str,
    today: date | None = None,
    history: list[dict[str, Any]] | None = None,
    model_ref: str | None = None,
    on_step: Any = None,
    expert_ids: list[str] | None = None,
    person_id: str | None = None,
    known_metrics: list[str] | None = None,
    should_stop: Any = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`attachments` 是本轮用户上传的纯文本材料（已裁剪）。

    它只喂意图解析，不进指标取值：材料里的数字不能变成答案里的数字，
    否则「每个数都能追到出处」这条就断了。所以这个函数不把它塞进 question ——
    规则兜底那条路要靠 question 做关键词匹配，混进材料正文会误命中指标。
    """
    def check() -> None:
        # 停止信号只在这里判：分发阶段提示的那一处是整条流水线的唯一收窄口，
        # 模型/专家/取数都绕不过去，漏判的可能性最小。
        if should_stop is not None and should_stop():
            raise TurnCancelled()

    def step(name: str, data: dict[str, Any]) -> None:
        check()
        if on_step:
            on_step(name, data)

    with llmclient.track_usage() as usage_sink:
        try:
            extra: dict[str, Any] = {}
            result = _ask_impl(
                question, today, history, model_ref, step, person_id, known_metrics,
                should_stop,
                attachments=attachments,
                out=extra,
            )
            # 降级信息要跟着答案一起走：落库、回放、界面上都得看得见。
            # 只在实时那一条路径上提示的话，刷新一下就好像什么都没发生过。
            if extra.get("degraded"):
                result["degraded"] = extra["degraded"]
            for i, item in enumerate(result.get("trace") or [], 1):
                item["step"] = i

            # 给会话层用的结构化标签：只有指标名，不含数值。
            # 注意这不是回灌内容 —— 回灌的是 headline 原文（见 core/chat.py），
            # 这两个字段只是给 known_metrics 和界面用的索引。
            codes = _codes_from_result(result)
            result["metric_codes"] = codes
            result["metric_names"] = [md.METRICS[c]["name"] for c in codes]

            # 材料值 × 库值并列。只在这里赋值 —— headline / facts 已经生成完了，
            # 材料数字没有路径能进答案正文（B 档的边界靠这个结构保证，不靠自觉）。
            if extra.get("material_refs"):
                cmp_rows = _material_compare(extra["material_refs"], result)
                if cmp_rows:
                    result["material_compare"] = cmp_rows

            # 外部数据整合：附件里如果是「期次 + 数值」的表，就和指标库序列逐期对齐。
            # 这一步和上面那条 B 档边界不冲突 —— 它产出的仍然是**并列对照**，
            # 数字不回流进 headline/facts，只是把「对上了几期、差多少」算给用户看。
            # 放在 headline 生成之后，结构上就保证了它改不了答案正文。
            if attachments:
                ext = extdata_mod.analyze(attachments, today)
                if ext:
                    result["external"] = ext
                    # 附件里有能对上的指标、但这一问本身没命中指标（比如
                    # 「把这份月报和系统对一下」），拒答会让人以为文件白传了。
                    # 不改 headline（正文不能凭空变成答案），只补一句指路。
                    if ext.get("metrics") and result.get("refused"):
                        result.setdefault("notes", []).append(
                            "上传的表里有 %d 项能和指标库对上，逐期对照见下方。"
                            % len(ext["metrics"])
                        )

            # 专家解读放在最后：答案已经是成品，专家只能补「怎么看」，改不了任何数值。
            # 任何一位专家失败都只影响他自己那一段，不牵连答案。
            #
            # 寒暄那一轮没有可解读的对象：没有指标、没有数值、没有记录，
            # 叫参谋来说什么？真调了模型，本地 27B 会为一句「你好」花上
            # 2-5 秒 × 人数，产出还是「信息不足」。跳过就得说出来 —— 用户
            # 明明点了参谋，一声不响地什么都不给，看起来像功能坏了。
            check()
            if expert_ids and result.get("chitchat"):
                result.setdefault("notes", []).append(
                    "这轮没有指标或记录，参谋没有可解读的内容，所以没有调用模型。"
                )
            elif expert_ids:
                result["expert_views"] = experts_mod.interpret(
                    result,
                    expert_ids,
                    model_ref,
                    on_step=step,
                    refs_by_expert=_expert_refs(question, result, person_id, expert_ids),
                )
        except TurnCancelled as exc:
            exc.usage = llmclient.summarize(usage_sink)
            raise

        # 用量必须由流水线自己汇总后随结果带走：调用链（trace）不落库，
        # 唯一一次能把它写进历史的机会就是这里。
        result["usage"] = llmclient.summarize(usage_sink)
        return result


def _ask_impl(
    question: str,
    today: date | None = None,
    history: list[dict[str, Any]] | None = None,
    model_ref: str | None = None,
    on_step: Any = None,
    person_id: str | None = None,
    known_metrics: list[str] | None = None,
    should_stop: Any = None,
    attachments: list[dict[str, Any]] | None = None,
    out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`out` 是给调用方传回附加信息的口子（目前只放 degraded）。

    为什么用这个而不是让每个 return 都带上：这个函数有十来个提前返回
    （拒答、口径、对比、走势…），逐个加字段迟早漏一个，而漏掉的那个恰好
    就是降级路径 —— 用户看到一段规则答案，界面上却什么都没提示。
    """
    today = today or date.today()
    trace: list[dict[str, Any]] = []
    question = question.strip()

    # 约会议要抢在所有判断之前，包括模型规划。
    #
    # 两个原因：一是它既不是问数也不是查记录 —— 它要**写**；让模型先判意图，
    # 大概率被判成「取值」或「依据」，然后答一段不相干的东西。二是这里不需要模型：
    # 时间是规则算得出来的，让模型算日期反而是最不可靠的一环，还白花一次 token。
    #
    # 注意它排在拒答之前：这类问句在指标字典里一定匹配不到，不前置就会变成
    # 「没有对应的指标」——用户提了个能办的需求，系统回一句它办不了。
    if meetplan_mod.wants(question):
        person = people_mod.meta(person_id)
        draft = meetplan_mod.parse(question, today=today, person=person)
        return meetplan_mod.as_result(draft, question)

    # 寒暄排在规划之前，理由和上面不同：这类话在指标字典里必然匹配不到，
    # 走一遍模型规划只会得到一次空结果、一次白烧的本地推理（27B 要 2-5 秒），
    # 最后仍然落到拒答 —— 用户问「你能干什么」，系统回「没有对应的指标」。
    # 这里纯规则，不花 token，也就能在门口把话说清楚。
    #
    # 顺序放在 meetplan 之后：建会是有副作用的意图，不能被「你好」抢走。
    # 两者实际互斥（`chitchat.wants` 是整句匹配），但把有动作的那个放前面更安全。
    if chitchat_mod.wants(question):
        return chitchat_mod.as_result(question)

    plan, planner, degraded = _plan_question(
        question, today, history, model_ref, known_metrics, should_stop, attachments
    )
    if out is not None and degraded:
        out["degraded"] = degraded
    # 材料里核出来过的数值。装配留在外层：这个函数有十来个提前返回，
    # 逐个加字段迟早漏一个，而漏掉的那次恰好就是用户最想核对的那次。
    if out is not None and plan and plan.get("material_refs"):
        out["material_refs"] = plan["material_refs"]

    if plan is not None:
        hits = [
            {"code": c, "alias": "模型指定", "name": md.METRICS[c]["name"]}
            for c in plan["metric_codes"]
        ]
        usage = plan.get("usage") or {}
        trace.append(
            {
                "step": 1,
                "tool": "llm.plan",
                "call": f"POST /chat/completions · model={plan.get('model')} · temperature=0",
                "status": "ok" if hits else "empty",
                "summary": (plan.get("reason") or "模型给出查询计划")
                + (
                    f"（tokens {usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')}）"
                    if usage
                    else ""
                ),
            }
        )
        intent = {
            "intent": plan["intent"],
            "dim": plan.get("dim"),
            "why": plan.get("reason") or "模型判断",
        }
        period_info = {
            "period": plan.get("period"),
            "months": max(1, int(plan.get("months") or 1)),
            "why": plan.get("reason") or "模型判断",
        }
    else:
        if planner != "rules":
            trace.append(
                {
                    "step": 1,
                    "tool": "llm.plan",
                    "call": "POST /chat/completions",
                    "status": "error",
                    "summary": f"模型不可用，已退回规则解析（{planner}）",
                }
            )
        hits = match_metrics(question)
        trace.append(
            {
                "step": len(trace) + 1,
                "tool": "match_metric",
                "call": f"match_metric(question={question!r})",
                "status": "ok" if hits else "empty",
                "summary": "、".join(h["name"] for h in hits) if hits else "未命中任何已注册指标",
            }
        )
        if hits:
            intent = parse_intent(question, hits)
            trace.append(
                {
                    "step": len(trace) + 1,
                    "tool": "parse_intent",
                    "call": f"parse_intent(question) → {intent['intent']}",
                    "status": "ok",
                    "summary": intent["why"],
                }
            )
            period_info = parse_period(question, today)
            trace.append(
                {
                    "step": len(trace) + 1,
                    "tool": "resolve_period",
                    "call": f"resolve_period(period={period_info['period']!r}, months={period_info['months']})",
                    "status": "ok",
                    "summary": period_info["why"],
                }
            )
        else:
            intent = {"intent": "value"}
            period_info = {"period": None, "months": 1, "why": ""}

    # 阶段回调：给 SSE 用。这里是「意图已解析、即将取数」的唯一汇合点，
    # 两条分支（模型/规则）都汇到这里，不会漏推。
    if on_step:
        on_step(
            "plan",
            {
                "planner": planner,
                "intent": intent.get("intent"),
                "codes": [h["code"] for h in hits],
                "names": [h["name"] for h in hits],
                "period": period_info.get("period"),
                # 降级要让用户在等待期间就知道，而不是等答案出来再解释
                "degraded": degraded or None,
            },
        )

    # 拒答之前先查知识库。理由很实际：问「这个指标口径怎么定」「上次那个会定了什么」
    # 「生态环境局有几件事」的问题，在指标字典里天然匹配不到——它们问的不是数值，
    # 而是系统里已有的记录。直接拒答等于把最容易回答的一类问题挡在门外。
    #
    # 放在这里而不是更前面，是为了不抢问数的路：只要命中了指标，就该走取数和出处那一套，
    # 知识库不该有机会把「9 月工业增速多少」解释成一段口径说明。
    kb_miss = None
    if not hits:
        person = people_mod.meta(person_id)
        kb = knowledge_mod.answer(question, person)
        if not kb["refused"]:
            kb["trace"] = trace + kb["trace"]
            for i, step in enumerate(kb["trace"], 1):
                step["step"] = i
            return kb
        kb_miss = kb

    # 事项类问题要抢在问数前面。
    #
    # 「环保局那件事办到哪了」在指标字典里能匹配上——生态环境局确实管着空气质量。
    # 于是模型把意图判成取值，答出一个漂亮的「86.5%」，而用户问的根本不是这个。
    # 这是最坏的一种错：答非所问，但形式上完全正常，看的人不会起疑。
    # 所以只要问句里是「进展 / 办理 / 定了什么」这类话术，且知识库里确实有督办或会议记录，
    # 就以记录为准。
    if hits and _wants_matter(question):
        person = people_mod.meta(person_id)
        kb = knowledge_mod.answer(question, person)
        if not kb["refused"] and any(
            h["kind"] in ("oversight", "decision", "meeting") for h in kb["knowledge"]
        ):
            trace.append(
                {
                    "step": len(trace) + 1,
                    "tool": "knowledge.prefer",
                    "call": "问句问的是事项进展，改走知识库",
                    "status": "ok",
                    "summary": f"指标本可匹配 {'、'.join(h['name'] for h in hits)}，但问的是办理情况，不答数值",
                }
            )
            kb["trace"] = trace + kb["trace"]
            for i, step in enumerate(kb["trace"], 1):
                step["step"] = i
            return kb

    # 资料库（通用文档 RAG）：指标没命中、知识库也没命中时，最后再查一遍上传的
    # 政策文件/报告。它与知识库的分工：知识库只收系统自己产出的三类记录（口径、
    # 督办、会议），资料库收的是用户上传的外部文档。顺序在拒答之前、知识库之后，
    # 是为了不抢知识库的路 —— 系统内部记录比外部文档更权威。
    #
    # 边界继续守「模型不碰数字」：返回的永远是检索到的原文片段（带文档名/库名
    # 出处），由代码拼装展示，不让模型转述。向量/bigram 只是「怎么找到对的那段」。
    if not hits:
        rag_hits = rag_mod.search([b["id"] for b in rag_mod.list_bases()], question)
        if rag_hits:
            trace.append(
                {
                    "step": len(trace) + 1,
                    "tool": "rag.search",
                    "call": f"rag.search(q={question!r})",
                    "status": "ok",
                    "summary": "命中上传文档 " + "、".join(
                        f"《{h['doc_name']}》" for h in rag_hits[:4]
                    ),
                }
            )
            return {
                "refused": False,
                "question": question,
                "planner": planner,
                "intent": "rag",
                "headline": f"上传文档里检索到 {len(rag_hits)} 段相关内容，原文如下。",
                "rag": [
                    {
                        "kb_name": h["kb_name"],
                        "doc_name": h["doc_name"],
                        "text": h["text"],
                        "score": h["score"],
                        "method": h["method"],
                    }
                    for h in rag_hits
                ],
                "sources": [],
                "notes": [],
                "trace": trace,
            }

    # 拒答：不编
    if not hits:
        c = (kb_miss or {}).get("index_counts") or {}
        scope_txt = (
            f"指标字典里没有对应的指标；知识库现有 {kb_miss.get('index_size')} 条记录"
            f"（指标口径 {c.get('metric', 0)} 条、督办事项 {c.get('oversight', 0)} 条、"
            f"会议 {c.get('meeting', 0) + c.get('decision', 0)} 条），也没有对得上的"
            if kb_miss
            else "指标字典里没有对应的指标"
        )
        trace.append(
            {
                "step": len(trace) + 1,
                "tool": "refuse",
                "call": "指标与知识库均未命中 → 返回已注册指标清单",
                "status": "refused",
                "summary": f"已注册 {len(md.METRICS)} 个指标，均未命中",
            }
        )
        return {
            "refused": True,
            "question": question,
            "planner": planner,
            "headline": "这个问题没有对应的指标，也没有相关记录。",
            # 说清「两处都查过了」，否则用户会以为系统只会答指标题
            "detail": f"{scope_txt}。两处都没有的问题我不会凭印象估算——"
                      "请先在指标中台注册，或在台账、会议记录里补上原始记录。",
            "suggestions": [{"code": c, "name": m["name"], "department": m["department"]}
                            for c, m in md.METRICS.items()],
            "trace": trace,
            "sources": [],
            "notes": [],
        }

    primary = hits[0]["code"]
    secondary = hits[1]["code"] if len(hits) > 1 else None

    # ---- 口径类 ----
    if intent["intent"] == "definition":
        meta = md.get_metric(primary)
        trace.append(
            {
                "step": 4,
                "tool": "get_metric_definition",
                "call": f"get_metric_definition(code={primary!r})",
                "status": "ok",
                "summary": f"{meta['department']} · {meta['report']}",
            }
        )
        return {
            "refused": False,
            "question": question,
            "planner": planner,
            "intent": "definition",
            "headline": f"{meta['name']}：{meta['definition']}",
            "facts": [
                {"label": "责任部门", "value": meta["department"]},
                {"label": "数据来源", "value": meta["report"]},
                {"label": "更新频率", "value": meta["frequency"]},
                {"label": "可分解维度", "value": "、".join(meta["dimensions"]) or "仅全市汇总"},
            ],
            "sources": [
                {
                    "department": meta["department"],
                    "report": meta["report"],
                    "period": "指标字典",
                    "published_at": "—",
                    "api": meta["api"],
                    "definition": meta["definition"],
                }
            ],
            "notes": meta["caveats"],
            "trace": trace,
        }

    # ---- 对比类 ----
    if intent["intent"] == "compare" and secondary:
        result = md.compare_metrics([primary, secondary], period_info["period"], today)
        ok_rows = [r for r in result["rows"] if "value" in r]
        trace.append(
            {
                "step": 4,
                "tool": "compare_metrics",
                "call": f"compare_metrics(codes=[{primary!r}, {secondary!r}])",
                "status": "ok" if ok_rows else "empty",
                "summary": f"返回 {len(ok_rows)} 个指标同期数值",
            }
        )
        return {
            "refused": False,
            "question": question,
            "planner": planner,
            "intent": "compare",
            "headline": "、".join(r["name"] for r in ok_rows)
                        + f"在 {result['period_label']} 的同期表现为："
                        + "；".join(f"{r['name']} {_fmt(r['value'], r['unit'])}" for r in ok_rows)
                        + "。",
            "facts": [],
            "compare_rows": ok_rows,
            "sources": [_source_brief(r["source"]) for r in ok_rows],
            "notes": ["两个指标口径不同（一个是增速、一个是比率/总量），只能并排看，不能相加减。"],
            "trace": trace,
        }

    # ---- 分解类 ----
    if intent["intent"] == "breakdown":
        dim = intent.get("dim")
        if dim is None:
            dims = md._BREAKDOWNS.get(primary, {})
            dim = next(iter(dims), None)
        result = md.breakdown_metric(primary, dim, period_info["period"], today)
        trace.append(
            {
                "step": 4,
                "tool": "breakdown_metric",
                "call": f"breakdown_metric(code={primary!r}, dim={dim!r})",
                "status": "ok" if result.get("ok") else result.get("error", "empty"),
                "summary": (
                    f"返回 {len(result['rows'])} 行{dim}分解（{result['period_label']}）"
                    if result.get("ok")
                    else result.get("message", "无分解数据")
                ),
            }
        )
        if result.get("ok"):
            top, bottom = result["rows"][0], result["rows"][-1]
            return {
                "refused": False,
                "question": question,
                "planner": planner,
                "intent": "breakdown",
                "headline": (
                    f"{result['period_label']}，{result['name']}按{dim}看：{top['name']}最高 "
                    f"{_fmt(top['value'], result['unit'])}，{bottom['name']}最低 "
                    f"{_fmt(bottom['value'], result['unit'])}，相差 "
                    f"{_fmt(abs(top['value'] - bottom['value']), result['unit']).replace(' ', '')}。"
                ),
                "facts": [],
                "breakdown": {
                    "title": f"{result['name']} · {dim}分解 · {result['period_label']}",
                    "unit": result["unit"],
                    "rows": result["rows"],
                },
                "sources": [_source_brief(result["source"])],
                "notes": result["caveats"],
                "trace": trace,
            }
        # 没有分解接口就退回总量，并说明原因
        intent = {"intent": "value", "why": "该指标无分解数据，退回全市汇总"}

    # ---- 时序 / 单值 ----
    months = period_info["months"] if intent["intent"] in ("trend", "value") else 1
    if intent["intent"] == "trend":
        months = max(months, 6)
    result = md.query_metric(primary, period_info["period"], months if intent["intent"] == "trend" else 1, today)
    trace.append(
        {
            "step": 4,
            "tool": "query_metric",
            "call": f"query_metric(code={primary!r}, period={period_info['period']!r}, months={months})",
            "status": "ok" if result.get("ok") else result.get("error"),
            "summary": (
                f"返回 {result['period_label']} 数值 {_fmt(result['value'], result['unit'])}"
                if result.get("ok")
                else result.get("message", "取数失败")
            ),
        }
    )

    pending_note = None
    if not result.get("ok") and result.get("error") == "not_disclosed":
        # 问的期次还没披露：不编数，但也不让用户空手而归——退回最近可得期并说明
        pending_note = str(result.get("message") or "")
        result = md.query_metric(primary, None, months if intent["intent"] == "trend" else 1, today)
        trace.append(
            {
                "step": len(trace) + 1,
                "tool": "fallback_latest",
                "call": f"query_metric(code={primary!r}, period=None)",
                "status": "ok",
                "summary": f"改用最近可得期 {result.get('period_label')}，并在答案中标注期次差异",
            }
        )

    if not result.get("ok"):
        return {
            "refused": True,
            "question": question,
            "planner": planner,
            "headline": result.get("message", "接口没有返回数据。"),
            "detail": "按规则，拿不到披露数据时不回答数值，只说明可取范围。",
            "latest": result.get("latest"),
            "suggestions": [],
            "sources": [],
            "notes": [],
            "trace": trace,
        }

    delta_bits = [
        _delta_text(result["mom"], result["unit"], "环比"),
        _yoy_text(result, "同比"),
    ]
    # 「全市」+「市重大项目…」会拼成「全市市重大项目」，指标名自带「市」时不再加前缀
    scope = "" if result["name"].startswith("市") else "全市"
    ptext = _period_text(result)
    headline = (
        f"{ptext}，{scope}{result['name']}为 "
        f"{_fmt(result['value'], result['unit'])}"
        + ("，" + "，".join(b for b in delta_bits if b) if any(delta_bits) else "")
        + "。"
    )

    facts = [
        {"label": ptext, "value": _fmt(result["value"], result["unit"])},
    ]
    if result["mom"] is not None:
        facts.append({"label": "环比", "value": _delta_text(result["mom"], result["unit"], ""), "raw": result["mom"]})
    yoy_bit = _yoy_text(result, "")
    if yoy_bit:
        # raw 给专家链判方向用（只要正负），跟着「哪一路取的值」走：
        # 比率类是百分点差、总量类是增长率，别把两者混成同一个数
        raw_yoy = result.get("yoy") if result.get("unit") == "%" else result.get("yoy_pct")
        facts.append({"label": "同比", "value": yoy_bit, "raw": raw_yoy})

    # 时序类额外再取一次序列（单值查询只返回一期）
    payload: dict[str, Any] = {
        "refused": False,
        "question": question,
        "planner": planner,
        "intent": intent["intent"],
        "headline": headline,
        "facts": facts,
        "unit": result["unit"],
        "period_label": result["period_label"],
        "sources": [_source_brief(result["source"])],
        "period_note": pending_note,
        "notes": ([pending_note] if pending_note else []) + result["caveats"],
        "trace": trace,
    }
    if intent["intent"] == "trend":
        series = md.query_metric(primary, None, months, today)
        payload["series"] = series["series"]
        payload["series_title"] = f"{result['name']} · 近 {months} 个月"
        trace.append(
            {
                "step": 5,
                "tool": "query_metric",
                "call": f"query_metric(code={primary!r}, months={months})",
                "status": "ok",
                "summary": f"返回 {len(series['series'])} 期序列",
            }
        )
    return payload


# --------------------------------------------------------------------------
# 对外别名：材料装配等模块要复用同一套「数字怎么说」的实现
# --------------------------------------------------------------------------
# 材料的读者和问数的读者是同一批人。如果材料里写「9 月 6.4%」
# 而问数回答写「1—9 月累计 6.4%」，两份东西放在一张桌上就没人能解释差在哪。
# 所以这里把三个纯格式化函数公开出去，让上层直接调用而不是各写一份。
fmt_value = _fmt
delta_text = _delta_text
period_text = _period_text
