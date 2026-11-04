"""可召唤的专家团：角色卡 + 一段「不带数字」的定性解读。

**专家不参与取数。** 数值、口径、出处仍然由 `core/metrics/ask.py` 走指标接口
拼出来；专家只在这之后再补一段「怎么看」。这是刻意划的线：如果让专家自己去查数，
同一场会里经济参谋说 8.9%、民生参谋说 9.1%，谁也说不清该信谁 —— 而数字只能有
一个来源，就是指标接口。

所以这里有一道硬闸：**解读里出现阿拉伯数字或百分号，整段丢弃并注明原因。**
唯一的例外是指标名和记录名里本来就带的数字（「12345 群众诉求按期办结率」），
校验前先把这几个片段剔掉，免得冤枉。

召唤多个专家 = 同一个问题、同一组数字，各自给一段角度不同的看法。**不做合并总结**：
合并必须再调一次模型，而那次调用不会产生任何新信息，只多一次编数字的机会。

**专家名单是可配置的。** 内置默认写在这里，持久化到 `data/experts.json`
（启动优先读它），可在专家管理面板里增删改。用户自建的专家没有 `domain`
字段也能正常用 —— 只是拿不到域内依据，退回「只看方向信号」。

**专家读知识库，但只读自己域内的那几条。** 域写在每位专家的 `domain` 里
（指标代码 + 责任单位），由 `core/knowledge.index(person, domain)` 收窄。
读进来的是原文摘录，参谋可以据此判断好坏，但**仍然不许复述里面的数字** ——
数字只有一个来源，这条线没动。知识库补的是「判断的素材」，不是「报数的权限」。
域内一条都没有时如实写「本域内无相关记录」，不拿别域的记录凑数：那等于让经济参谋
去评价河道巡查，说得越像越糟。

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

# 每位专家最多带几条知识库记录进 prompt。
# 定 4 是按 token 算的：一条正文截 180 字，4 条 ≈ 720 字，加上指标信号和角色说明
# 仍在 1200 字以内。带再多，会议原文那种几千字的记录会把指标信号挤到无关紧要的位置。
REF_LIMIT = 4

# 每条记录的正文截多长。会议原文动辄数千字，整段塞进去只会淹没方向信号。
# 引句（quote）优先于正文（text）：议定事项的 quote 是纪要原句，督办条目的 quote
# 是最后一笔办理经过 —— 都比「拼装出来的摘要」更有信息量，也更难被误读。
REF_CHARS = 180

# 内置默认名单：总参谋统筹不绑域，六位板块参谋各管自己那摊。
# `domain.codes` 是这位参谋管的指标代码（一个指标只给一个参谋，否则口径会被
# 两个域各引一半）；`domain.units` 是责任单位全称（事项/会议按它归属，
# 简称由 knowledge.unit_variants 派生，两边都手写迟早分叉）。总参谋不绑域：
# 他是「先定看什么」的角色，依据由分管参谋各自提供，统一起来反而变成一份大杂烩。
_DEFAULT_EXPERTS: tuple[dict[str, Any], ...] = (
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
        "domain": {
            "codes": ["GY_VA_GROWTH", "GDP_TOTAL", "BUDGET_REV", "SS_ENTERPRISE"],
            "units": ["市统计局", "市财政局", "市市场监督管理局"],
        },
    },
    {
        "id": "investment",
        "name": "招商引资参谋",
        "avatar": "商",
        "brief": "熟悉在谈项目与签约落地",
        "focus": "在谈项目、签约落地、开工转化",
        "angle": "看项目的签约到开工转化是否顺畅，警惕「签约多、落地少」或「开工慢、要素保障不到位」的苗头。",
        "domain": {
            "codes": ["INVEST_SIGNED"],
            "units": ["市投资促进局"],
        },
    },
    {
        "id": "project",
        "name": "重大项目参谋",
        "avatar": "项",
        "brief": "掌握项目进度与要素保障",
        "focus": "重点项目进度、投资完成、要素保障",
        "angle": "盯重点项目进度与投资完成是否同步，要素保障（用地、资金、审批）有没有卡点拖慢整体节奏。",
        "domain": {
            "codes": ["FAI_PROGRESS", "FAI_GROWTH"],
            "units": ["市发展和改革委员会"],
        },
    },
    {
        "id": "society",
        "name": "社会治理参谋",
        "avatar": "治",
        "brief": "紧盯12345诉求与公共安全",
        "focus": "12345 诉求、矛盾纠纷、公共安全",
        "angle": "从诉求办理质量和矛盾纠纷趋势入手，注意「办结率高但重复投诉多」或安全类问题抬头这类信号。",
        "domain": {
            "codes": ["TZ_12345_CLOSURE"],
            "units": ["市政务服务和数据管理局（12345 热线中心）"],
        },
    },
    {
        "id": "livelihood",
        "name": "民生保障参谋",
        "avatar": "民",
        "brief": "关注就业教育与民生实事",
        "focus": "就业、教育、医疗、民生实事",
        "angle": "先看群众诉求的办理质量，再看就业与民生实事的兑现进度，注意「办结率高但满意度低」的组合。",
        "domain": {
            "codes": ["UR_RATE", "LIVELIHOOD_DONE"],
            "units": ["国家统计局本市调查队", "市政府办公室（民生实事专班）"],
        },
    },
    {
        "id": "eco",
        "name": "生态环境参谋",
        "avatar": "环",
        "brief": "把环境质量与经济增速放一起看",
        "focus": "空气质量、工业排放相关指标",
        "angle": "把空气质量和工业增速放在一起看：两者同向要警惕，反向要说明代价。",
        "domain": {
            "codes": ["AQI_GOOD_DAYS"],
            "units": ["市生态环境局"],
        },
    },
)

_SYSTEM = """你是{name}（{brief}），在市政府常务会上旁听，被市长点名说两句。
你关注：{focus}。你的看问题方式：{angle}

硬性要求：
1. **不得出现任何阿拉伯数字或百分号**（不要写"8.9%""3 个百分点""前三季度"这类）。要表达程度就用「偏高/偏低/明显/略有/接近时序/落后于时序」。
2. 只能基于下面给出的指标名称、方向信号和「可参考的记录」判断。记录里可能带具体数字，**你可以据此判断好坏，但一个字都不许复述那些数字**。不得引用没列出的指标。
3. 可以点名记录（如「河道巡查那件事」），那是可查的；但不能把记录里的话改写成一个新的数字或新的时间。
4. 三句话以内，直接给结论和风险提示，不要客套话、不要复述问题，不要用任何格式标记或引号包裹。
5. 如果给定信息不足以判断，就写「信息不足，需要补充哪一类口径」。
"""


# 哪些字段属于「专家身份」，持久化和对外输出都只保留这些（字符串字段）。
# brief 参与提示词但也可空；angle 是看问题方式，编辑面板的核心。
# domain 不是身份字段，是「这位参谋读哪块知识库」的能力配置，
# 单独走 _clean_domain 校验，面板编辑不带 domain 时保留原值（见 save_experts）。
_EXPERT_KEYS = ("id", "name", "avatar", "brief", "focus", "angle")


def _clean_domain(raw: Any) -> dict[str, list[str]] | None:
    """校验并规范化 domain；不合法返回 None（视为没绑域）。"""
    if not isinstance(raw, dict):
        return None
    codes = [str(c).strip() for c in raw.get("codes") or [] if str(c or "").strip()]
    units = [str(u).strip() for u in raw.get("units") or [] if str(u or "").strip()]
    if not codes and not units:
        return None
    return {"codes": codes, "units": units}


def _load() -> list[dict[str, Any]]:
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
                    dom = _clean_domain(e.get("domain"))
                    if dom:
                        item["domain"] = dom
                    out.append(item)
                if out:
                    return out
    except Exception:  # noqa: BLE001
        pass
    return [dict(e) for e in _DEFAULT_EXPERTS]


def all_experts() -> list[dict[str, Any]]:
    return _load()


def find(eid: str | None) -> dict[str, Any] | None:
    for e in _load():
        if e["id"] == eid:
            return dict(e)
    return None


def save_experts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """整体替换名单并落盘。校验字段后写 data/experts.json，返回规范化后的名单。

    编辑面板只提交身份字段；同 id 已有 domain 而提交里没带，**保留原 domain** ——
    否则在面板里改一句 focus 就会把这位参谋的知识域悄悄清空，界面上一无所有
    （解读退回无依据模式），用户完全察觉不到是编辑害的。
    """
    prev = {e["id"]: e for e in _load()}
    clean: list[dict[str, Any]] = []
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
        dom = _clean_domain(e.get("domain")) or (
            prev.get(eid, {}).get("domain") if "domain" not in e else None
        )
        if dom:
            item["domain"] = dom
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
META_KEYS = ("id", "name", "avatar", "brief", "focus", "ok", "text", "why", "refs", "skipped")

# 依据条目只存「哪一条」，不存正文：正文有几百字，而回放只需要显示
# 「参谋当时看了哪几条」并支持点击跳转。ref 留着，前端据此跳回台账或会议记录。
REF_META_KEYS = ("kind", "kind_label", "id", "title", "ref")


def to_meta(views: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把解读裁成可落库的样子。存进去的必须是**裁剪后**的结果，
    否则下一轮读历史时字段忽多忽少，前端渲染会不一致。"""
    return [{k: v for k, v in ev.items() if k in META_KEYS} for ev in (views or [])]


def _refs_meta(refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in REF_META_KEYS} for r in (refs or [])]


def _allowed_numeric_tokens(
    names: list[str], ref_titles: list[str] | None = None
) -> list[str]:
    """名字里自带的数字（如 12345、第 9 次常务会）不算"专家在编数字"。

    只放行**标题**里的数字，不放行正文：指标名和记录名是专有名词，专家提到
    「12345 那条」「第 9 次常务会」是在点名，不是在报数；而正文里的数字恰恰
    是这道闸要拦的东西。两者混在一个白名单里，闸就形同虚设了。
    """
    tokens: list[str] = []
    for name in list(names) + list(ref_titles or []):
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


def _refs_block(refs: list[dict[str, Any]] | None) -> str:
    """把域内检索到的记录拼成 prompt 里的那一段。

    每条都写明「是什么、谁的、原文摘录」：参谋要点名一件事，就得先知道它属于谁、
    原话怎么写。只给标题不给摘录的话，它只能凭标题猜内容，那和编没有区别。

    没有记录时**不省略这一段**，而是明写「本域内无相关记录」。省掉的话，
    模型会以为自己漏看了什么，反而更爱凭印象补一句。
    """
    rows = list(refs or [])
    if not rows:
        return "可参考的记录：本域内没有相关记录（只凭上面的指标信号判断，不要推测具体事项）。"
    lines = ["可参考的记录（原文摘录，其中带数字；你可以据此判断好坏，但不得复述那些数字）："]
    for r in rows:
        body = str(r.get("quote") or r.get("text") or "").strip()
        if len(body) > REF_CHARS:
            body = body[:REF_CHARS] + "…"
        src = r.get("source") or {}
        dept = str(src.get("department") or "").strip() or "—"
        lines.append(f"- 〔{r.get('kind_label') or '记录'}〕{r.get('title')}（{dept}）：{body}")
    return "\n".join(lines)


def _out_of_scope(expert: dict[str, Any], codes: list[str]) -> bool:
    """本轮问的指标，一个都不属于这位参谋的域。

    这时**不调模型**，直接说明「不是他关注的事」。理由是实测出来的：几位参谋
    一起叫上、问的是工业加空气质量时，民生参谋会逐字复述经济与生态的观点 ——
    那不是「三个角度」，是同一段话印了三遍，而市长会以为真有三个人在会诊。
    它之所以能复述，是因为方向信号是全组共享的；它之所以不该复述，是因为
    这些指标跟他的关注面一点关系都没有。

    只在**拿到指标代码**且该参谋有域时才判：知识库路径下 codes 是空的、
    自建专家没有 domain，那时没有依据判断相关性，交给 prompt 去说 ——
    宁可多一次调用，也不要因为判错而集体沉默。
    """
    dom = set((expert.get("domain") or {}).get("codes") or ())
    got = {str(c) for c in (codes or ())}
    if not dom or not got:
        return False
    return not (dom & got)


def _interpret_one(
    expert: dict[str, str],
    user_content: str,
    allowed: list[str],
    model_ref: str | None,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """一位专家的完整一轮：调模型 → 过数字闸 → 给出 ok/why。

    单独拆出来是为了让 `interpret` 的循环只管「报进度 + 收集结果」，
    失败分支再多也不会漏掉一次 done 事件。

    `refs` 无论成败都写进 view：参谋读了哪几条是**事实**，不因为这段解读
    被数字闸丢掉就跟着消失。用户看到一个「信息不足」，还得能知道它当时
    手上有什么，否则只会以为系统没检索。
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
        "refs": _refs_meta(refs),
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
    refs_by_expert: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """给一份已经算好的答案配专家解读。

    返回值永远是列表，每项都带 ok：失败的那一项说明原因，不影响其它项，
    也绝不影响答案本身。

    `on_step` 用来报「现在轮到谁」：专家是串行调模型的，累起来要好几秒，
    没有这一步的流式界面会从「解析完成」直接卡到出结果，看着像死了。

    `refs_by_expert` 是每位参谋的域内检索结果（`{专家 id: [条目]}`），由调用方
    先查好再传进来。**这里不做检索**：experts 只负责「拿到什么就说什么」，
    检索的范围和可见性交给 knowledge，两边各管一段，才不会出现「专家自己
    去库里翻，翻到了别人分管的事」。
    """
    ids = [str(x) for x in (expert_ids or [])][:MAX_PER_CALL]  # 一次会诊不该变成刷模型
    if not ids:
        return []

    names = list(result.get("metric_names") or [])
    codes = [str(c) for c in (result.get("metric_codes") or [])]
    signals = _signals(result)
    question = str(result.get("question") or "").strip()

    base = [
        f"市长问的是：{question}",
        f"涉及指标：{'、'.join(names) if names else '（未命中具体指标）'}",
        f"方向信号：{'；'.join(signals) if signals else '（无同比环比数据）'}",
    ]

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
        if _out_of_scope(expert, codes):
            # 进度事件照报：前端已经显示「正在看材料」，这一位不报 done 会永远停在那
            view: dict[str, Any] = {
                "id": expert["id"],
                "name": expert["name"],
                "avatar": expert["avatar"],
                "brief": expert.get("brief", ""),
                "focus": expert["focus"],
                "ok": False,
                "skipped": True,
                "text": "",
                "why": f"本轮问的不是你关注的事（{expert['focus']}），未作解读。",
                "refs": [],
            }
            emit("done", expert, i, False)
            views.append(view)
            continue
        refs = list((refs_by_expert or {}).get(eid) or [])
        # 白名单按人算：参谋各自读的记录不同，能点名的记录名也不同。
        # 用全组共用一份的话，经济参谋会被允许说出民生记录标题里的数字。
        allowed = _allowed_numeric_tokens(names, [str(r.get("title") or "") for r in refs])
        user_content = "\n".join(base + [_refs_block(refs), "按你的角色说三句话以内。注意：不许出现任何数字。"])
        view = _interpret_one(expert, user_content, allowed, model_ref, refs)
        emit("done", expert, i, view["ok"])
        views.append(view)

    return views
