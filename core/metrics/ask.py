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
from datetime import date
from pathlib import Path
from typing import Any

# core/ 要进路径：专家团是 core 层模块，不是 metrics 子目录里的
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import experts as experts_mod  # noqa: E402
import llm
import mockdata as md

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


def _source_brief(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "department": source["department"],
        "report": source["report"],
        "period": source["period"],
        "published_at": source["published_at"],
        "api": source["api"],
        "definition": source["definition"],
    }


def _plan_question(
    question: str,
    today: date,
    history: list[dict[str, Any]] | None = None,
    model_ref: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """优先用模型做意图解析；不可用或失败就退回规则。"""
    if not llm.available():
        return None, "rules"
    try:
        plan = llm.plan(question, today.isoformat(), history=history, model_ref=model_ref)
        return plan, "llm"
    except Exception as exc:  # 网络/额度/解析失败都不该让问数不可用
        return None, f"llm_failed: {exc}"


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


def ask(
    question: str,
    today: date | None = None,
    history: list[dict[str, Any]] | None = None,
    model_ref: str | None = None,
    on_step: Any = None,
    expert_ids: list[str] | None = None,
) -> dict[str, Any]:
    result = _ask_impl(question, today, history, model_ref, on_step)
    for i, step in enumerate(result.get("trace") or [], 1):
        step["step"] = i

    # 给会话层用的结构化摘要：只有指标名，不含数值（见 core/chat.py 的说明）
    codes = _codes_from_result(result)
    result["metric_codes"] = codes
    result["metric_names"] = [md.METRICS[c]["name"] for c in codes]

    # 专家解读放在最后：答案已经是成品，专家只能补「怎么看」，改不了任何数值。
    # 任何一位专家失败都只影响他自己那一段，不牵连答案。
    if expert_ids:
        result["expert_views"] = experts_mod.interpret(result, expert_ids, model_ref, on_step=on_step)
    return result


def _ask_impl(
    question: str,
    today: date | None = None,
    history: list[dict[str, Any]] | None = None,
    model_ref: str | None = None,
    on_step: Any = None,
) -> dict[str, Any]:
    today = today or date.today()
    trace: list[dict[str, Any]] = []
    question = question.strip()

    plan, planner = _plan_question(question, today, history, model_ref)

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
            },
        )

    # 拒答：不编
    if not hits:
        trace.append(
            {
                "step": len(trace) + 1,
                "tool": "refuse",
                "call": "未匹配 → 返回已注册指标清单",
                "status": "refused",
                "summary": f"已注册 {len(md.METRICS)} 个指标，均未命中",
            }
        )
        return {
            "refused": True,
            "question": question,
            "planner": planner,
            "headline": "这个问题对应的指标还没有注册，我不能给出数字。",
            "detail": "问数只能回答已接入指标接口的问题。没注册的指标我不会凭印象估算，"
                      "请先在指标中台注册，或换一个已接入的指标来问。",
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
        _delta_text(result["yoy"], result["unit"], "同比"),
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
    if result["yoy"] is not None:
        facts.append({"label": "同比", "value": _delta_text(result["yoy"], result["unit"], ""), "raw": result["yoy"]})

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
