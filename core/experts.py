"""可召唤的专家团：角色卡 + 一段「不带数字」的定性解读。

**专家不参与取数。** 数值、口径、出处仍然由 `core/metrics/ask.py` 走指标接口
拼出来；专家只在这之后再补一段「怎么看」。这是刻意划的线：如果让专家自己去查数，
同一场会里经济参谋说 8.9%、民生参谋说 9.1%，谁也说不清该信谁 —— 而数字只能有
一个来源，就是指标接口。

所以这里有一道硬闸：**解读里出现阿拉伯数字或百分号，整段丢弃并注明原因。**
唯一的例外是指标名里本来就带的数字（「12345 群众诉求按期办结率」），
校验前先把这几个片段剔掉，免得冤枉。

召唤多个专家 = 同一个问题、同一组数字，各自给一段角度不同的看法。**不做合并总结**：
合并必须再调一次模型，而那次调用不会产生任何新信息，只多一次编数字的机会。

模型没配好时不让整条问答失败：专家段标注「未配置模型」，数值答案照常返回。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import llmclient  # noqa: E402

# 一次会诊最多召唤几位。这是**并发成本上限**（一位 = 一次模型调用、一份 token），
# 与 EXPERTS 里有几位专家无关 —— 专家名单可以扩到十几个，但一次别全叫上。
# 接口的 max 和后端的截断都引用这一个常量，别在别处再写死数字。
MAX_PER_CALL = 3

# 专家名单持久化到 data/experts.json（缺省回退到下面的内置默认）。
EXPERTS_FILE = ROOT / "data" / "experts.json"

# 六位专家（一期五大板块 + 总参谋）的差别是**关注面**，不是取数逻辑：
# 同一份数字，看的角度不同。这里是内置默认，用户可在专家管理面板里增删改，
# 编辑后写进 data/experts.json，下次启动优先读它。
_DEFAULT_EXPERTS: tuple[dict[str, str], ...] = (
    {
        "id": "chief",
        "name": "总参谋",
        "avatar": "总",
        "brief": "统筹五大板块，把握全局",
        "focus": "统筹五大板块，先定看什么、再看怎么看",
        "angle": "先把问题归到对应的板块，判断这是总量问题还是结构问题，再给最该盯的一两个风险点，不逐个罗列。",
    },
    {
        "id": "economy",
        "name": "经济运行参谋",
        "avatar": "经",
        "brief": "精通全市经济数值与局势",
        "focus": "工业、投资、财政、市场主体",
        "angle": "先看总量与增速是否匹配年度节奏，再看结构（投资/工业/消费）有没有拖后腿的单项。",
    },
    {
        "id": "investment",
        "name": "招商引资参谋",
        "avatar": "商",
        "brief": "熟悉在谈项目与签约落地",
        "focus": "在谈项目、签约落地、开工转化",
        "angle": "看项目的签约到开工转化是否顺畅，警惕「签约多、落地少」或「开工慢、要素保障不到位」的苗头。",
    },
    {
        "id": "project",
        "name": "重大项目参谋",
        "avatar": "项",
        "brief": "掌握项目进度与要素保障",
        "focus": "重点项目进度、投资完成、要素保障",
        "angle": "盯重点项目进度与投资完成是否同步，要素保障（用地、资金、审批）有没有卡点拖慢整体节奏。",
    },
    {
        "id": "society",
        "name": "社会治理参谋",
        "avatar": "治",
        "brief": "紧盯12345诉求与公共安全",
        "focus": "12345 诉求、矛盾纠纷、公共安全",
        "angle": "从诉求办理质量和矛盾纠纷趋势入手，注意「办结率高但重复投诉多」或安全类问题抬头这类信号。",
    },
    {
        "id": "livelihood",
        "name": "民生保障参谋",
        "avatar": "民",
        "brief": "关注就业教育与民生实事",
        "focus": "就业、教育、医疗、民生实事",
        "angle": "先看群众诉求的办理质量，再看就业与民生实事的兑现进度，注意「办结率高但满意度低」的组合。",
    },
)

_SYSTEM = """你是{name}（{brief}），在市政府常务会上旁听，被市长点名说两句。
你关注：{focus}。你的看问题方式：{angle}

硬性要求：
1. **不得出现任何阿拉伯数字或百分号**（不要写"8.9%""3 个百分点""前三季度"这类）。要表达程度就用「偏高/偏低/明显/略有」。
2. 只能基于下面给出的指标名称和方向信号判断，不得引用没列出的指标，不得推测具体数值。
3. 三句话以内，直接给结论和风险提示，不要客套话、不要复述问题，不要用任何格式标记或引号包裹。
4. 如果给定信息不足以判断，就写「信息不足，需要补充哪一类口径」。
"""


# 哪些字段属于「专家身份」，持久化和对外输出都只保留这些。
# brief 参与提示词但也可空；angle 是看问题方式，编辑面板的核心。
_EXPERT_KEYS = ("id", "name", "avatar", "brief", "focus", "angle")


def _load() -> list[dict[str, str]]:
    """从 data/experts.json 读名单；没有或读坏就回退内置默认。"""
    try:
        if EXPERTS_FILE.is_file():
            raw = json.loads(EXPERTS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                out = []
                seen = set()
                for e in raw:
                    if not isinstance(e, dict) or not str(e.get("id") or "").strip():
                        continue
                    eid = str(e["id"]).strip()
                    if eid in seen:
                        continue
                    seen.add(eid)
                    item = {k: str(e.get(k) or "").strip() for k in _EXPERT_KEYS}
                    if not item["name"]:
                        item["name"] = eid
                    out.append(item)
                if out:
                    return out
    except Exception:  # noqa: BLE001
        pass
    return [dict(e) for e in _DEFAULT_EXPERTS]


def all_experts() -> list[dict[str, str]]:
    return _load()


def find(eid: str | None) -> dict[str, str] | None:
    for e in _load():
        if e["id"] == eid:
            return dict(e)
    return None


def save_experts(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """整体替换名单并落盘。校验字段后写 data/experts.json，返回规范化后的名单。"""
    clean: list[dict[str, str]] = []
    seen = set()
    for e in items or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "").strip()
        if not eid or eid in seen:
            continue
        seen.add(eid)
        item = {k: str(e.get(k) or "").strip() for k in _EXPERT_KEYS}
        if not item["name"]:
            item["name"] = eid
        if not item["focus"]:
            item["focus"] = item["angle"] or "综合"
        if not item["angle"]:
            item["angle"] = item["focus"] or "从整体看趋势与风险"
        clean.append(item)
    try:
        EXPERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        EXPERTS_FILE.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass
    return clean


# 落库时只保留回放要用的字段：回看历史不需要知道「是谁算的」，
# 存 model/model_ref 只会让 chats.json 白白变胖。
META_KEYS = ("id", "name", "avatar", "brief", "focus", "ok", "text", "why")


def to_meta(views: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把解读裁成可落库的样子。存进去的必须是**裁剪后**的结果，
    否则下一轮读历史时字段忽多忽少，前端渲染会不一致。"""
    return [{k: v for k, v in ev.items() if k in META_KEYS} for ev in (views or [])]


def _allowed_numeric_tokens(names: list[str]) -> list[str]:
    """指标名里自带的数字（如 12345）不算"专家在编数字"。"""
    tokens: list[str] = []
    for name in names:
        tokens.extend(re.findall(r"\d+", name or ""))
    return sorted(set(tokens), key=len, reverse=True)


def _has_forbidden_number(text: str, allowed: list[str]) -> bool:
    probe = text
    for token in allowed:
        probe = probe.replace(token, "")
    return bool(re.search(r"[0-9０-９%％]", probe))


def _signals(result: dict[str, Any]) -> list[str]:
    """把数值折成方向（升/降/持平），这样专家拿到的输入里没有数字。

    两条取数路径都得覆盖，否则专家会「有数看不见」：

    - `intent=value`（单指标）：数值在 `facts`，同比环比是两个独立条目，
      且都属于同一个指标，用 `metric_names[0]` 当标签是对的。
    - `intent=compare`（多指标）：数值在 `compare_rows`，**`facts` 是空列表**。
      这里每行自带 name 和 yoy，标签必须逐行取 —— 用 names[0] 统一套会把
      A 指标的方向安到 B 头上，比没有信号更危险。
    """
    out: list[str] = []

    for row in result.get("compare_rows") or []:
        yoy = row.get("yoy")
        if isinstance(yoy, (int, float)):
            tone = "上升" if yoy > 0 else "下降" if yoy < 0 else "持平"
            out.append(f"{row.get('name') or '指标'} 同比{tone}")

    if out:
        return out

    label = "指标"
    names = result.get("metric_names") or []
    if names:
        label = names[0]
    for f in result.get("facts") or []:
        raw = f.get("raw")
        if f.get("label") in ("环比", "同比") and isinstance(raw, (int, float)):
            tone = "上升" if raw > 0 else "下降" if raw < 0 else "持平"
            out.append(f"{label} {f['label']}{tone}")
    return out


def _interpret_one(
    expert: dict[str, str],
    user_content: str,
    allowed: list[str],
    model_ref: str | None,
) -> dict[str, Any]:
    """一位专家的完整一轮：调模型 → 过数字闸 → 给出 ok/why。

    单独拆出来是为了让 `interpret` 的循环只管「报进度 + 收集结果」，
    失败分支再多也不会漏掉一次 done 事件。
    """
    view: dict[str, Any] = {
        "id": expert["id"],
        "name": expert["name"],
        "avatar": expert["avatar"],
        "brief": expert.get("brief", ""),
        "focus": expert["focus"],
        "ok": False,
        "text": "",
        "why": "",
    }
    if not llmclient.available():
        view["why"] = "未配置模型，专家解读需要模型；数值答案不受影响。"
        return view
    try:
        resp = llmclient.chat_text(
            _SYSTEM.format(**expert),
            user_content,
            model_ref=model_ref,
            temperature=0.4,
            # deepseek-v4-pro 这类推理模型会把 token 花在 thinking 上，320 会被
            # 偶发吃光、导致 content 为空。这里给足额度，最终文本仍在下面按 260 字截。
            max_tokens=2000,
        )
    except llmclient.ModelUnavailable:
        view["why"] = "未配置模型。"
        return view
    except llmclient.ModelError as exc:
        view["why"] = f"模型调用失败：{str(exc)[:80]}"
        return view

    text = str((resp.get("text") or "")).strip()
    view["model"] = resp.get("model")
    view["model_ref"] = resp.get("model_ref")
    if not text:
        view["why"] = "模型没有给出内容。"
    elif _has_forbidden_number(text, allowed):
        # 不是「修正」，是丢弃：数字只要出现过，就没法判断它是不是编的
        view["why"] = "解读里出现了数字，已丢弃（专家不报数，数值以指标接口为准）。"
    else:
        view["ok"] = True
        view["text"] = text[:260]
    return view


def interpret(
    result: dict[str, Any],
    expert_ids: list[str],
    model_ref: str | None = None,
    on_step: Any = None,
) -> list[dict[str, Any]]:
    """给一份已经算好的答案配专家解读。

    返回值永远是列表，每项都带 ok：失败的那一项说明原因，不影响其它项，
    也绝不影响答案本身。

    `on_step` 用来报「现在轮到谁」：三位专家是串行调模型的，累起来要好几秒，
    没有这一步的流式界面会从「解析完成」直接卡到出结果，看着像死了。
    """
    ids = [str(x) for x in (expert_ids or [])][:MAX_PER_CALL]  # 一次会诊不该变成刷模型
    if not ids:
        return []

    names = list(result.get("metric_names") or [])
    signals = _signals(result)
    allowed = _allowed_numeric_tokens(names)
    question = str(result.get("question") or "").strip()

    user_content = "\n".join(
        [
            f"市长问的是：{question}",
            f"涉及指标：{'、'.join(names) if names else '（未命中具体指标）'}",
            f"方向信号：{'；'.join(signals) if signals else '（无同比环比数据）'}",
            "按你的角色说三句话以内。注意：不许出现任何数字。",
        ]
    )

    def emit(phase: str, expert: dict[str, str], i: int, ok: bool | None = None) -> None:
        if not on_step:
            return
        payload: dict[str, Any] = {"phase": phase, "name": expert["name"], "index": i, "total": len(ids)}
        if ok is not None:
            payload["ok"] = ok
        on_step("experts", payload)

    views: list[dict[str, Any]] = []
    for i, eid in enumerate(ids, 1):
        expert = find(eid)
        if expert is None:
            continue
        emit("start", expert, i)
        view = _interpret_one(expert, user_content, allowed, model_ref)
        emit("done", expert, i, view["ok"])
        views.append(view)

    return views
