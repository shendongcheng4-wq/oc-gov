"""模型调用层：用任意 OpenAI 兼容服务做意图理解。

关键设计：模型只输出"要查哪个指标、什么期次、什么意图"的结构化计划，
不负责生成数字。数值永远由 mockdata 的接口返回后原样拼装。
这样模型即使胡说，也不会污染数据 —— 政务场景里这条比模型有多聪明更重要。

连接参数由 `core/modelcfg.py` 解析（多 provider × 多模型、`provider/model` 标识、
三层回退）。本模块只负责「把计划问出来」，不关心凭据存在哪。

多轮：`plan(..., history=[...])` 会把最近的对话摘要作为前缀消息带上，让模型理解
"那上个月呢""换成区县看"这类指代。历史里的 assistant 摘要是**不含数值**的
（见 core/chat.py 的说明），所以模型不会因为看过数字而倾向复述数字。

模型不可用时 `available()` 返回 False，ask.py 自动退回规则解析。
"""

from __future__ import annotations

import llmclient
import mockdata as md
import modelcfg


def available() -> bool:
    return modelcfg.available()


def llm_info() -> dict[str, object]:
    """给界面显示当前生效的模型。键名沿用原型的 available/base_url/model/provider。"""
    info = modelcfg.llm_info()
    if not info.get("available"):
        return {"available": False, "base_url": None, "model": None, "provider": None, "ref": None}
    return {
        "available": True,
        "base_url": info["base_url"],
        "model": info["model"],
        "provider": info["provider"],
        "ref": info["ref"],
    }


def _catalog_prompt() -> str:
    lines = []
    for code, meta in md.METRICS.items():
        lines.append(
            f"- code={code} | 名称={meta['name']} | 单位={meta['unit']} | "
            f"责任部门={meta['department']} | 释义={meta['definition']} | "
            f"别名={'、'.join(meta['aliases'])}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = """你是政务数据问数系统的"意图解析器"，只做一件事：把用户的口语问题，
映射成对已注册指标接口的查询计划。

严格要求：
1. 只能从下面列出的指标里选，code 必须原样照抄。列表里没有的，metric_codes 返回空数组。
2. 不要回答数值，不要估算，不要补充任何事实。你只输出计划。
3. period 用 YYYY-MM 格式；用户没指定就返回 null。用户问"同比/去年同期"时，
   把 period 设为去年同月，并在 reason 里说明。
4. intent 只能是：value（问单个数）、trend（问走势/近几个月）、
   breakdown（要按区县/部门拆分或排名）、compare（多个指标对比）、definition（问口径/定义）。
5. months：intent=trend 时填用户要的月数（没说就 6），其余填 1。
6. dim：intent=breakdown 时，可填"区县"或"责任单位"，不确定就 null。
7. 如果这句话在接着上一轮问（例如"那上个月呢""这个指标再拆开看""换成区县看"
   "跟去年比呢"），参考对话历史把指代补全成完整的计划。但指标仍然只能从下面
   列表里选，历史里提到过的指标名可以直接沿用。
8. 历史消息里 assistant 的内容只有指标名、没有数值 —— 不要索要数值，也不要编造。

只输出 JSON，不要 markdown 代码块：
{"metric_codes":["..."],"intent":"...","period":null,"months":1,"dim":null,"reason":"..."}

已注册指标：
"""


def plan(
    question: str,
    today_iso: str,
    history: list[dict[str, str]] | None = None,
    model_ref: str | None = None,
    timeout: float = 30.0,
    known_metrics: list[str] | None = None,
) -> dict[str, object]:
    """调用模型得到查询计划。失败时抛异常，由调用方退回规则解析。"""
    conn = modelcfg.resolve(model_ref)
    if conn is None:
        raise RuntimeError("no_model_configured")

    # 历史条数由 core/chat.context 按 token 预算裁好，这里不再二次截断 ——
    # 原来写死 [-8:] 等于把上层的预算算完又砍回 4 轮，改预算也不会有效果。
    # known_metrics 是跨轮「已经问过哪些指标」的落水石：历史窗口再短，
    # 这句「那去年呢」也知道「那」指的是什么。
    memory = ""
    if known_metrics:
        memory = (
            "\n\n本会话此前已经涉及这些已注册指标（用户说「这个 / 它 / 再拆开看」时可沿用）：\n"
            + "\n".join(f"- {name}" for name in known_metrics[:12])
        )

    out = llmclient.chat_json(
        SYSTEM_PROMPT
        + _catalog_prompt()
        + memory
        + f"\n\n今天是 {today_iso}。最新可得期为 {md.latest_available()}。",
        question,
        history=history or [],
        model_ref=model_ref,
        timeout=timeout,
    )
    raw = out["data"]

    codes = [str(c).upper() for c in (raw.get("metric_codes") or [])]
    codes = [c for c in codes if c in md.METRICS]  # 模型编出来的 code 直接丢弃
    intent = str(raw.get("intent") or "value")
    if intent not in {"value", "trend", "breakdown", "compare", "definition"}:
        intent = "value"

    return {
        "metric_codes": codes,
        "intent": intent,
        "period": raw.get("period") or None,
        "months": int(raw.get("months") or 1) if str(raw.get("months") or "1").isdigit() else 1,
        "dim": raw.get("dim") or None,
        "reason": str(raw.get("reason") or ""),
        "usage": out["usage"],
        "model": out["model"],
        "model_ref": out["model_ref"],
        "fell_back": out["fell_back"],
    }
