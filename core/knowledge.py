"""知识库：把系统里已经存在的口径与历史记录变成可检索的条目。

三个来源，全部是现成数据，不新增任何录入动作：

1. 指标口径 —— `md.METRICS` 里的定义、口径限制、责任部门、报表、频率、目标
2. 督办事项 —— 台账里每一件事的标题、状态、时限、责任单位、办理经过
3. 会议议定 —— 会议记录原文，以及每条议定事项在原文里的那句话（`quote`）

**为什么不做向量检索（RAG）**

两条理由，第二条是决定性的：

- 项目零第三方依赖，没有向量库。手写余弦在几百条上能跑，全市口径加历年事项上万条就退化，
  而且是那种「看着还能用、召回率悄悄掉一半」的退化，比直接报错更难发现。
- **可核性**。向量召回的东西交给模型复述，模型会把甲文件的话安到乙文件头上——在公文场景里，
  这比答不出来严重得多。所以这里的检索由代码做，返回的**永远是一段真实存在的原文**，
  模型不参与检索、也不参与复述。

**检索为什么用 bigram**

中文没有空格，标准库没有分词器，引 jieba 就破坏了「零依赖」。把问句和条目都切成相邻二字组
求覆盖率，是零依赖下最稳的做法。再叠一层术语加权：出现明确术语（指标别名、单位名、
台账编号、会议编号）比字面碰巧相似可信得多，所以术语命中直接加分，不参与覆盖率竞争。

阈值定在 0.18 是有意的偏低——这里不是搜索框，宁可比对出两三条相关的让用户自己看，
也不要因为差几个字就把人挡回去。真正的防线在后面：**每条结果都带出处，没有出处的条目
压根不会进索引**。
"""

from __future__ import annotations

import math
import re
from typing import Any

import embedding
import mockdata as md

import ledger
import oversight as ov
import schedule

# 问句里的这些词对检索没有信息量，先整词删掉再切二字组。
# 不删的话「怎么算的」会切出「怎么 / 么算 / 算的」，三个词条在文档里一个都不会出现，
# 白白把覆盖率的分母撑大，把真正相关的那条压到阈值以下。
_STOP_PHRASES = (
    "请问", "帮我", "帮忙", "查一下", "查查", "看一下", "看看", "介绍一下", "讲讲", "说说",
    "是怎么", "怎么", "如何", "什么", "哪些", "哪个", "多少", "为什么", "为何", "是否",
    "有没有", "是不是", "能不能", "可以", "一下", "有关", "关于", "相关", "情况", "怎么样",
)

# 单个虚字直接剔掉。它们参与二字组只会制造噪音（"的规"、"了什"）。
_STOP_CHARS = set("的了吗呢吧啊呀哦么是在有和与及或就都也还很太更最把被让给对从向往于着过")

# 「事项」「单位」是台账条目的模板词——每条记录的正文里都有「责任单位：」
# 和「事项状态」，用户也爱这么问。它们区分不出任何东西，留在问句里只会让
# 所有台账条目一起虚高。注意只从**问句**里剔，索引那边照留（正文可读性要完整）。
_DOC_NOISE = ("事项", "单位", "情况")

# 同义说法。这是真实存在的鸿沟，不是数据质量问题：用户会说「上次开会定了什么」，
# 台账里写的是「会议」；会说「环保局」，单位名是「生态环境局」。不做这层映射，
# 一半的问题会因为用词不同而检索不到，而且失败方式是「查无记录」——
# 看起来像系统没数据，实际是没对上词。
#
# 注意是**双向都试、取最高分**，不是单向替换：台账里写的恰恰是「市发改委」，
# 把它替换成「发展和改革委员会」反而命中不了。同理「城管局」。
_COLLOQUIAL = (
    ("开会", "会议"),
    ("碰头", "会议"),
    ("环保局", "生态环境局"),
    ("城管局", "城市管理局"),
    ("发改委", "发展和改革委员会"),
    ("规上", "规模以上"),
    ("办到哪", "进展"),
    ("办得怎么样", "进展"),
    ("谁在办", "责任单位"),
    ("什么时候到期", "时限"),
)

# 时间词。它们单独命中几乎没有信息量：「今天晚上吃什么」会撞上任意一条
# 标题带「今天」的记录（测试里就有一条「日程测试·今天到期」），
# 而「今天到期的有哪些」里的「今天」是真信号——区别在于后者还有「到期」。
# 所以规则不是删掉时间词，而是**全部分数都由时间词贡献时不算命中**。
_TIME_BG = {
    "今天", "明天", "昨天", "后天", "前天", "今年", "去年", "明年",
    "本月", "上月", "下月", "这周", "本周", "上周", "下周", "这个月",
}

# 命中阈值。低于它的不进结果，宁可说「没有相关记录」也不硬凑。
MIN_SCORE = 0.18

# 向量余弦相似度的召回下限。bge-small-zh 对「短查询 vs 长文档」会有一个
# 0.40~0.53 的「背景相似度」——语义完全不沾边的条目也落在这个区间（实测：
# 「进出口数据」与「停车难专项治理」余弦 0.52，「社会消费品零售总额」与
# 「规模以上工业增加值」0.52）。而真命中（语义相关）都在 0.60 以上（「停车难」
# →0.74、「空气质量」→0.65、带时间词的「上个月固定资产投资增速」→0.79）。
# 所以定 0.55：挡掉全部背景相似，同时给「蓝天保卫战→空气质量」这种同义改写
# （实测 0.60）留出余量。
MIN_VECTOR_SIM = 0.55

# 灰度区：余弦落在 [GRAY_VECTOR_FLOOR, MIN_VECTOR_SIM) 之间时，向量分本身
# 分不开「真命中」和「通用词碰巧相似」——实测「上次开会定了什么」对正确的
# 常务会议记录是 0.540，而「进出口数据」对无关的「平台数据接入」是 0.528，
# 只差 0.012，任何单一阈值都会误伤一边。
#
# 但两路的强弱差得很开：前者字面覆盖率 0.50（问句的二字组确实覆盖了记录），
# 后者只有 0.25（全落在「数据」这个通用词上）。所以在灰度区要求字面佐证，
# 用第二个独立信号消歧，而不是继续拧一个阈值。
# 低于 GRAY_VECTOR_FLOOR 的向量分连灰度区都不进——语义上确实不沾边。
GRAY_VECTOR_FLOOR = 0.50
GRAY_LEX_SUPPORT = 0.40


def _norm(text: str) -> str:
    """去掉问句里的疑问词与虚字，留下实词。"""
    s = str(text or "").strip().lower()
    for p in _STOP_PHRASES:
        s = s.replace(p, " ")
    for p in _DOC_NOISE:
        s = s.replace(p, " ")
    return "".join(c for c in s if c not in _STOP_CHARS)


def _bigrams(text: str) -> set[str]:
    s = re.sub(r"[\s\W_]+", "", _norm(text))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _variants(text: str) -> list[str]:
    """问句的几种说法：原话，以及把口语词换成正式词之后的版本。

    检索时逐个试、取最高分。所以「环保局」能找到「生态环境局」，
    而「发改委」也仍然能命中台账里写的「市发改委」——两个方向都不吃亏。
    """
    raw = str(text or "")
    out = [_norm(raw)]
    for a, b in _COLLOQUIAL:
        if a in raw:
            v = _norm(raw.replace(a, b))
            if v and v not in out:
                out.append(v)
    return out or [""]


def _terms() -> dict[str, str]:
    """术语 → 指标编码。别名也算术语，用户不会照着指标名念。"""
    out: dict[str, str] = {}
    for code, meta in md.METRICS.items():
        out[meta["name"]] = code
        for alias in meta.get("aliases") or []:
            out[alias] = code
    # 长术语优先匹配，避免「工业」抢先命中「工业增加值」的场景
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


_TERMS: dict[str, str] | None = None


def terms() -> dict[str, str]:
    global _TERMS
    if _TERMS is None:
        _TERMS = _terms()
    return _TERMS


def units() -> list[str]:
    """全部单位名，用于「这个局」「生态环境局」这类按部门问的检索。"""
    names: set[str] = set()
    for meta in md.METRICS.values():
        names.add(meta["department"])
    for person in _people():
        names.update(person.get("units") or [])
    for item in ov.list_items():
        names.add(item["assignee"])
        names.update(item.get("co_assignees") or [])
    return sorted(names, key=len, reverse=True)


def _people() -> list[dict[str, Any]]:
    import people as people_mod

    return list(people_mod.PEOPLE)


# 行政级别前缀。用户不会说「市生态环境局」，他会说「生态环境局」；
# 也不会说「市发改委」，会说「发改委」。只按全称匹配等于匹配不上。
_LEVEL_PREFIX = ("市", "县", "区", "省", "州", "旗", "镇", "乡")


def unit_variants(name: str) -> set[str]:
    """一个单位的全部叫法：全称 + 去掉行政级别前缀的简称。"""
    out = {name} if name else set()
    for p in _LEVEL_PREFIX:
        if name.startswith(p) and len(name) > len(p) + 2:
            out.add(name[len(p) :])
    return out


def unit_hit(text: str, unit: str) -> bool:
    """问句里是否明确提到了这个单位（含简称和俗称）。"""
    if not unit:
        return False
    names = unit_variants(unit)
    return any(v and any(n in v for n in names) for v in _variants(text))


# --------------------------------------------------------------------------
# 索引：三类条目，每条都必须带得出出处
# --------------------------------------------------------------------------


def _metric_entries() -> list[dict[str, Any]]:
    rows = []
    for code, meta in md.METRICS.items():
        detail = md.get_metric(code)
        body = [detail["definition"]]
        if meta.get("target") is not None:
            body.append(f"年度目标 {meta['target']}{detail['unit']}。")
        if meta.get("cumulative"):
            body.append("该指标按期累计口径发布，不是单月值。")
        for c in detail["caveats"]:
            body.append(c)
        rows.append(
            {
                "kind": "metric",
                "kind_label": "指标口径",
                "id": code,
                "title": detail["name"],
                "text": "".join(body),
                "quote": detail["definition"],
                # 口径是公共知识，不按身份收窄：局长问「这个数怎么算的」也该拿到同样答案
                "unit": detail["department"],
                "ref": {"type": "metric", "id": code},
                "source": {
                    "department": detail["department"],
                    "report": detail["report"],
                    "period": "指标字典",
                    # api 留着：会话摘要靠它反推「这一轮用了哪个指标」，
                    # 少了它，口径问答在会话列表里会显示成「没涉及指标」
                    "api": detail["api"],
                    "note": f"{detail['frequency']}度更新",
                },
                "when": None,
            }
        )
    return rows


def _last_event(events: list[dict[str, Any]]) -> str:
    """最后一笔办理经过，拼成一句可核的话。"""
    if not events:
        return ""
    ev = events[-1]
    ts = str(ev.get("ts") or "")
    act = str(ev.get("action") or ev.get("type") or "")
    text = str(ev.get("text") or "")
    head = f"{ts} {act}".strip()
    if text and head:
        return f"{head}：{text}"
    return head or text


def _oversight_entries(person: dict[str, Any]) -> list[dict[str, Any]]:
    scope, manual = schedule.scope_of(person)
    rows = []
    for item in ov.list_items():
        if not schedule.item_visible(item, scope, manual):
            continue
        detail = ov.detail(item["id"]) or {}
        bits = [
            f"责任单位：{item['assignee']}",
            f"状态：{item['status']}",
        ]
        if item.get("co_assignees"):
            bits.append("协办：" + "、".join(item["co_assignees"]))
        if item.get("deadline"):
            bits.append(f"时限：{item['deadline']}")
        if item.get("risk_reasons"):
            bits.append("风险：" + "；".join(item["risk_reasons"]))
        # 办理经过不进正文。一件事从交办到办结可能有十几笔流水（创建/签收/催办/
        # 进展/办结），全塞进来就是一屏放不下的流水账，而用户问「怎么样了」要的
        # 是「现在什么状况」。最后一笔单独当引句，正好是「最新进展」本身。
        events = detail.get("events") or []
        rows.append(
            {
                "kind": "oversight",
                "kind_label": "督办事项",
                "id": item["id"],
                "title": item["title"],
                "text": "。".join(b for b in bits if b) + "。",
                # 台账没有公文式原文，最后一笔办理经过就是可核的那句话
                "quote": _last_event(events),
                "unit": item["assignee"],
                # 协办单位也算落在域内：协办的那家才是真在办这事的人
                "units": [item["assignee"], *(item.get("co_assignees") or [])],
                "ref": {"type": "oversight", "id": item["id"]},
                "source": {
                    "department": item["assignee"],
                    "report": f"督办台账 {item['id']}",
                    "period": item["status"],
                    "note": item.get("last_progress_at") or "",
                },
                "when": item.get("last_event_at"),
            }
        )
    return rows


def _meeting_entries(person: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for brief in ledger.list_meetings():
        full = ledger.get(brief["id"]) or {}
        if not schedule.meeting_visible(full, person):
            continue
        text = str(full.get("text") or "")
        rows.append(
            {
                "kind": "meeting",
                "kind_label": "会议记录",
                "id": full["id"],
                "title": full.get("title") or "",
                "text": text,
                # 原文就是正文，不另给引句：前端会判重，与其让它判，不如这里就不给
                "quote": "",
                "unit": full.get("host") or "",
                # 会议的主持人（常常是「常务副市长」这种职务）不对应任何部门，
                # 只看 host 等于三域都读不到会。参会单位才是「这场会和谁有关」。
                "units": [full.get("host") or "", *(full.get("attendees") or [])],
                "ref": {"type": "meeting", "id": full["id"]},
                "source": {
                    "department": full.get("host") or "—",
                    "report": f"会议记录 {full['id']}",
                    "period": full.get("held_at") or "",
                    "note": "参会：" + "、".join((full.get("attendees") or [])[:6]),
                },
                "when": full.get("held_at") or "",
            }
        )
        # 每条议定事项单独成条：用户问的往往是「那件事定了什么」，
        # 而不是「那次会开了什么」。拆开之后 quote 就是纪要里的原句，可直接核对。
        for it in full.get("items") or []:
            rows.append(
                {
                    "kind": "decision",
                    "kind_label": "会议议定",
                    "id": f"{full['id']}#{it.get('id')}",
                    "title": it.get("title") or "",
                    "text": "。".join(
                        x
                        for x in [
                            f"会议：{full.get('title')}",
                            f"责任单位：{it.get('assignee') or '未定'}",
                            f"时限：{it.get('deadline') or '未定'}",
                            ("已落台账 " + str(it["oversight_id"]) if it.get("oversight_id") else "本议定事项未落台账"),
                        ]
                        if x
                    )
                    + "。",
                    "quote": it.get("quote") or "",
                    "unit": it.get("assignee") or "",
                    "units": [it.get("assignee") or ""],
                    "ref": {"type": "meeting", "id": full["id"]},
                    "source": {
                        "department": it.get("assignee") or "—",
                        "report": f"{full.get('title')}（{full['id']}）",
                        "period": full.get("held_at") or "",
                        "note": "议定事项原文见引句",
                    },
                    "when": full.get("held_at") or "",
                }
            )
    return rows


def _unit_bare(name: str) -> str:
    """去掉括号里的补充说明。

    「市政务服务和数据管理局（12345 热线中心）」和台账里写的
    「市政务服务和数据管理局」是同一个单位，但字面差着 11 个字。
    不先归一，这两个写法永远对不上。
    """
    return re.sub(r"（[^）]*）|\([^)]*\)", "", name or "").strip()


# 部门简称 → 标准名（都不带行政级别前缀）。
#
# 这张表是被数据逼出来的，不是设计出来的：台账 assignee 写「市发改委」
# 「市市场监管局」，而 `md.METRICS` 的 department 写的是「市发展和改革委员会」
# 「市市场监督管理局」。这些缩写靠字面推不出来（「发改委」和「发展和改革委员会」
# 没有一个共同的前缀），只能显式列。
#
# 不归一的后果不是报错，而是参谋域里**一条事项都读不到**，界面显示
# 「本域内无相关记录」—— 看起来像库里空着，实际是词没对上。
_UNIT_ALIAS = {
    "发改委": "发展和改革委员会",
    "市场监管局": "市场监督管理局",
    "城管局": "城市管理局",
    "住建局": "住房和城乡建设局",
    "大数据局": "数据局",
}


def _unit_key(name: str) -> str:
    """单位的归一形式：去括号、去行政级别前缀、查别名表。

    只用于**归属判断**，不用于展示 —— 界面上仍然显示台账里的原始写法。
    """
    s = _unit_bare(name)
    for p in _LEVEL_PREFIX:
        if s.startswith(p) and len(s) > len(p) + 1:
            s = s[len(p) :]
            break
    return _UNIT_ALIAS.get(s, s)


def _unit_related(unit: str, units: set[str]) -> bool:
    """条目的责任单位是否落在某个参谋域的单位集合里。

    判的是**前缀**，不是任意包含，这一点是被一次真实的串域逼出来的：
    经济参谋的「市统计局」如果按包含判，「国家统计局本市调查队」里就含「统计局」，
    于是民生参谋管的失业率事项会跑进经济参谋的材料里。前缀方向还能对上简称
    （台账写「生态环境局」、域里写「市生态环境局」），包含方向不能。
    """
    if not unit or not units:
        return False
    mine = {v for v in (_unit_key(x) for x in unit_variants(unit)) if v}
    for u in units:
        theirs = {v for v in (_unit_key(x) for x in unit_variants(u)) if v}
        for a in mine:
            for b in theirs:
                if a == b:
                    return True
                short, long_ = (a, b) if len(a) <= len(b) else (b, a)
                # 差 4 个字以内且短的是长的前缀：够「生态环境局 / 市生态环境局」
                # 这种带行政级别前缀的简称，不够「统计局 / 国家统计局本市调查队」。
                if long_.startswith(short) and len(long_) - len(short) <= 4:
                    return True
    return False


def in_domain(entry: dict[str, Any], domain: dict[str, Any] | None) -> bool:
    """一条记录是否落在某个参谋的域内。

    指标条目按**代码**归属，不按部门：一个指标只能有一个参谋读，否则口径会被
    两个域各引一半，又回到「同一场会两个说法」的老问题上。
    事项/会议条目按**责任单位**归属，因为它们本来就不按指标分类。

    域为空表示不限，但这只是给外部调用留的兜底 —— `EXPERTS` 里每位都必须写
    domain，参谋团没有「全域参谋」这种角色。
    """
    if not domain:
        return True
    codes = set(domain.get("codes") or ())
    units = set(domain.get("units") or ())
    if not codes and not units:
        return True
    if entry.get("kind") == "metric":
        return entry.get("id") in codes
    # 事项和会议都可能挂多个单位：督办有协办，会议有参会方。只看主办单位的话，
    # 「常务副市长主持、生态环境局参加」的会三个域都读不到 —— 而那恰恰是
    # 参谋最该看的东西。所以用 units 列表，任意一个命中就算落在域内。
    cands = list(entry.get("units") or [])
    if not cands and entry.get("unit"):
        cands = [entry["unit"]]
    return any(_unit_related(u, units) for u in cands)


def index(person: dict[str, Any] | None = None, domain: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """建立索引。调用方每次检索都会重建——数据量小（几十到几百条），
    换来的是「台账刚改完就能搜到」，不用处理缓存失效。

    `domain` 是参谋的关心范围（见 `in_domain`）。**先按身份收窄、再按域收窄**，
    顺序不能反：域是「他关心什么」，身份是「他能看什么」，能看的永远优先。
    """
    person = person or {}
    rows = _metric_entries() + _oversight_entries(person) + _meeting_entries(person)
    if domain:
        rows = [r for r in rows if in_domain(r, domain)]
    return rows


# 浏览清单时的分类次序。指标口径排第一是因为它条目数固定、天然像目录；
# 会议记录垫底是因为它最长，逐字原文摆在前面对翻目录的人没有帮助。
_BROWSE_KINDS = ("metric", "oversight", "decision", "meeting")


def browse(person: dict[str, Any] | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    """清单视图：把索引原样列出来，供知识库页面浏览。

    和 `search` 是两件事，不能合并：
    - **不设阈值**。检索不敢给低分条目，是因为它要在回答里当依据；翻目录正相反，
      漏掉一条就会让人以为库里没有。所以这里一条都不滤，只排序。
    - **不打分**。分数是「这条和那个问题有多像」，没有问题时它没有意义，
      算一遍还会让人以为清单是按相关度排的。

    `kind` 收窄到某一类（metric / oversight / meeting / decision）。
    """
    rows = index(person)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    # 三遍排序：先定次级键，再用稳定排序压主键。和 search_in 同一手法。
    # 指标没有 when，排到同类里按标题走；其余按时间新→旧。
    rows.sort(key=lambda e: e["title"])
    rows.sort(key=lambda e: str(e.get("when") or ""), reverse=True)
    rows.sort(key=lambda e: _BROWSE_KINDS.index(e["kind"]) if e["kind"] in _BROWSE_KINDS else 9)
    return rows


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------


def _lexical_score(query: str, entry: dict[str, Any]) -> float:
    """bigram 覆盖率得分。分母是问句的二字组数量，所以分值就是「问句被覆盖了多少」。

    除了覆盖率还要卡**命中个数**：只对上一个「建设」二字组就算相关，等于把
    「火星基地建设情况」也匹配到某个指标上——它确实含「建设」，但问的不是这件事。
    但门槛不能一刀切：「上次开会说了什么」只有 4 个二字组，去掉「什么」后能对上的
    就只有「会议」一个，硬要 2 个就等于把最自然的一种问法挡在门外。所以按问句长度
    缩放——长问句（≥5 个二字组）至少要命中 2 个，短的命中 1 个即可。

    这一路是**向量不可用时的兜底**。它的先天缺陷是「通用词虚高」——「数据」
    这类政务文档高频词会命中一堆无关条目（「进出口数据」召回「停车难」「平台数据
    接入」就是这个毛病）。向量可用时走 `_vector_score`，这一路只在降级时用。
    """
    doc = _bigrams(f"{entry['title']}{entry['text']}{entry.get('quote') or ''}")
    variants = _variants(query)
    base = 0.0
    for v in variants:
        q = _bigrams(v)
        if not q:
            continue
        inter = q & doc
        # 命中的全是「今天」「本月」这类时间词，等于什么都没说
        if not (inter - _TIME_BG):
            continue
        need = 2 if len(q) >= 5 else 1
        if len(inter) < need:
            continue
        base = max(base, len(inter) / len(q))
    if base <= 0:
        return 0.0

    return round(base, 4)


def _hard_signals(query: str, entry: dict[str, Any]) -> float:
    """术语 / 单位 / 编号 / 标题这些「硬信号」加分。

    与 bigram 覆盖率无关——它们是明确的字符串命中，比「字面碰巧相似」可信得多：
    指标别名命中、责任单位点名、台账/会议编号出现在问句里，都是用户意图的强证据。
    向量检索和 bigram 检索**都要**叠这一层，所以单独抽出来共用。
    """
    variants = _variants(query)
    low = str(query).lower()
    add = 0.0

    # 术语命中：指标别名。别名比字面相似可靠得多——用户念「规上工业」，
    # 指标名里一个字都没有。
    for term, code in terms().items():
        if term and any(term in v for v in variants):
            if entry["kind"] == "metric" and entry["id"] == code:
                add += 0.55
            break
    if unit_hit(query, entry.get("unit") or ""):
        add += 0.30
    if entry["kind"] == "oversight" and entry["id"].lower() in low:
        add += 1.0
    if entry["kind"] in ("meeting", "decision") and entry["id"].split("#")[0].lower() in low:
        add += 1.0
    title_bg = _bigrams(entry["title"])
    if title_bg and any(title_bg <= _bigrams(v) for v in variants if _bigrams(v)):
        add += 0.15
    return round(add, 4)


def score(query: str, entry: dict[str, Any]) -> float:
    """一条命中的总分数 = bigram 覆盖率 + 硬信号加分。

    保留原签名是为了不破坏外部调用（tests 与 `search_in` 的降级路径），
    内部就是 `_lexical_score + _hard_signals`。
    """
    base = _lexical_score(query, entry)
    if base <= 0:
        return 0.0
    return round(min(base + _hard_signals(query, entry), 2.0), 4)


# 同分时的阅读次序：先看这件事现在怎么样，再看它是谁定的，最后看背景。
# 不参与打分，只决定并列时谁在上面——「入河排污口排查」同时命中台账和会议记录时，
# 用户要的是当前状态，不是那次会的原文。
_KIND_ORDER = {"oversight": 0, "decision": 1, "metric": 2, "meeting": 3}


def _id_hit(query: str, entry: dict[str, Any]) -> bool:
    """问句里是否原样出现了这条记录的编号（DB2026-0033 / MT2026-0002）。

    编号是**唯一键**，命中就是命中，不该再被语义阈值筛一遍：用户把台账编号
    贴进搜索框，要的就是那一条，而一串编号在向量空间里几乎没有语义（实测
    余弦 0.2 上下），按阈值走会被整条挡掉，表现为「明明有这个编号，搜不到」。

    议定事项的编号是「会议号#事项号」，所以会议号命中时它的议定事项也一起放行
    —— 这是对的：问会议号的人，多半就是要看这场会定了什么。
    """
    low = str(query or "").lower()
    if not low:
        return False
    eid = str(entry.get("id") or "").lower()
    return bool(eid) and (eid in low or eid.split("#")[0] in low)


def search_in(entries: list[dict[str, Any]], query: str, limit: int = 6) -> list[dict[str, Any]]:
    """在一份**已经建好的**索引里检索。

    单独拆出来是为了参谋团：三位参谋各查一次，但索引只需要建一遍。
    重建三次在几十条数据上感觉不出来，等台账上千条就是三倍的白等。

    检索分两路：
    - **向量可用**：用 embedding 的余弦相似度当主信号，叠 `_hard_signals`
      （术语/单位/编号/标题）。语义相似能挡住 bigram 的「通用词虚高」——问
      「进出口数据」不会再因为「数据」二字召回「停车难」「平台数据接入」。
      余弦落在灰度区（见 `GRAY_VECTOR_FLOOR`）时要求字面佐证再放行：那个区间
      里真命中（0.540）和通用词假命中（0.528）只差 0.012，单靠一个阈值分不开。
    - **向量不可用**：退回原 bigram 覆盖率 + 硬信号，行为与以前完全一致。

    两路都有一条例外：**编号命中不看阈值**（见 `_id_hit`）。用户把 DB2026-0033
    贴进来时，语义分和字面覆盖率都可能很低，但那正是他最确定知道的一条。
    """
    q = str(query or "").strip()
    if not q:
        return []
    low = q.lower()

    vec_scores = _vector_scores(q, entries) if embedding.embedding_available() else None
    if not vec_scores:
        # 注意这里要退成 None 而不是留着空 dict。`_vector_scores` 在 embed() 运行期
        # 返回空（模型文件坏了、显存不够）时给的是 {}——那不是「算出来全是 0 分」，
        # 而是「这一路没跑通」，必须退回字面检索。否则每条都按 v=0 滤掉，
        # 问什么都是「没有相关记录」。
        vec_scores = None

    hits: list[dict[str, Any]] = []
    for entry in entries:
        hard = _hard_signals(q, entry)
        by_id = _id_hit(q, entry)
        if vec_scores is not None:
            # 向量分（0~1 的余弦）＋硬信号。向量分设一个下限，语义上完全不沾边的
            # 条目即使硬信号勉强命中也不放行——「数据」这种通用词不会再擦线进来。
            # 编号命中是例外：唯一键对上就是唯一键对上，语义阈值在这里只会帮倒忙。
            v = vec_scores.get(entry["id"], 0.0)
            if not by_id and v < MIN_VECTOR_SIM and not (
                v >= GRAY_VECTOR_FLOOR and _lexical_score(q, entry) >= GRAY_LEX_SUPPORT
            ):
                continue
            s = round(min(v + hard, 2.0), 4)
        else:
            # bigram 兜底：保留原 threshold 语义
            s = score(q, entry)
            if s < MIN_SCORE and not by_id:
                continue
            # 只有编号命中时 score() 是 0（bigram 覆盖率算不出来，硬信号被它连带丢掉），
            # 把它补回来，否则这条会被排到最后，等于搜了却看不见。
            s = max(s, hard) if by_id else s
        hits.append(
            {**entry, "score": s, "unit_hit": bool(entry.get("unit") and unit_hit(low, entry["unit"]))}
        )

    # 两遍排序：先按次级键（时间新、标题）定序，再按主键（单位命中、分数、类型次序）。
    # 一遍写不出「主键升序、次级键降序」，稳定排序正好用来做分层。
    hits.sort(key=lambda e: (str(e.get("when") or ""), e["title"]), reverse=True)
    hits.sort(key=lambda e: (not e["unit_hit"], -e["score"], _KIND_ORDER.get(e["kind"], 9)))
    return hits[:limit]


def _vector_scores(query: str, entries: list[dict[str, Any]]) -> dict[str, float]:
    """给每条 entry 算向量余弦相似度，key 是 entry id。

    注意 entries 里 metric/oversight/meeting 的 id 可能撞车？不会——metric 是
    指标 code（如 GY_VA_GROWTH），oversight 是 DB 编号，meeting 是 MT 编号，
    天然不重复。真撞了也只影响一条的召回，不影响正确性。
    """
    # 每条 entry 的可检索文本：标题 + 正文 + 引句，和 bigram 用同一份语料
    texts = [f"{e['title']}{e['text']}{e.get('quote') or ''}" for e in entries]
    qv = embedding.embed([query])
    dv = embedding.embed(texts)
    if not qv or not dv:
        return {}

    q = qv[0]
    qnorm = math.sqrt(sum(x * x for x in q))
    if qnorm == 0:
        return {}
    out: dict[str, float] = {}
    for e, vec in zip(entries, dv):
        if len(vec) != len(q):
            continue
        norm = math.sqrt(sum(x * x for x in vec))
        sim = 0.0 if norm == 0 else sum(a * b for a, b in zip(q, vec)) / (qnorm * norm)
        # 必须显式转 float：embedding 返回的是 numpy.float32，sum/除法一路下来仍是
        # np.float32，round() 也不改类型。这个数会顺着 score 进入 /api/ask 的响应，
        # json.dumps 直接抛 TypeError —— 表现为「检索到东西就 500」。
        out[e["id"]] = float(sim)
    return out


def search(
    query: str,
    person: dict[str, Any] | None = None,
    limit: int = 6,
    domain: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """检索。返回按分数降序的条目，每条都带 source 与 quote。

    `domain` 传进来就只在那个参谋的域内找（见 `index`）。
    """
    return search_in(index(person, domain), query, limit)


answer_kinds = {"metric": "指标口径", "oversight": "督办事项", "meeting": "会议记录", "decision": "会议议定"}


def to_meta(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """给会话落库用的裁剪版。

    和专家解读同一个理由：不存的话，切回历史对话时这一段就凭空消失了。
    去掉 score（回放不重新排序，存着只会让人以为这个分数是当时的判断依据）；
    其余字段一个不能少——前端要拿 ref 做跳转，拿 quote 做原文核对。
    """
    out: list[dict[str, Any]] = []
    for h in items or []:
        out.append(
            {
                "kind": h.get("kind"),
                "kind_label": h.get("kind_label"),
                "id": h.get("id"),
                "title": h.get("title"),
                "text": h.get("text"),
                "quote": h.get("quote"),
                "source": h.get("source"),
                "ref": h.get("ref"),
            }
        )
    return out


def answer(query: str, person: dict[str, Any] | None = None, limit: int = 6) -> dict[str, Any]:
    """把检索结果组装成问数接口同构的答案。

    这里**没有模型**。答案就是检索到的原文片段，抄下来即可核对。
    凡是需要「组织语言」的地方都由代码拼，拼的模板固定，不随内容变化。
    """
    hits = search(query, person, limit)
    trace = [
        {
            "step": 1,
            "tool": "knowledge.search",
            "call": f"knowledge.search(q={query!r})",
            "status": "ok" if hits else "empty",
            "summary": (
                "命中 " + "、".join(f"{h['kind_label']}「{h['title']}」" for h in hits[:4])
                if hits
                else f"索引内 {len(index(person))} 条记录均未命中"
            ),
        }
    ]

    if not hits:
        idx = index(person)
        counts = {k: sum(1 for e in idx if e["kind"] == k) for k in ("metric", "oversight", "meeting", "decision")}
        return {
            "refused": True,
            "question": query,
            "planner": "rules",
            "intent": "knowledge",
            "headline": "知识库里没有和这个问题对得上的记录。",
            # 把库里的规模说出来：用户才能判断是「确实没有」还是「词没对上」。
            # 只说「没有」，他会以为系统空着。
            "index_size": len(idx),
            "index_counts": counts,
            "detail": f"知识库现有 {len(idx)} 条记录（指标口径 {counts['metric']} 条、"
                      f"督办事项 {counts['oversight']} 条、会议记录 {counts['meeting']} 条、"
                      f"会议议定 {counts['decision']} 条），只收这三类已有记录。"
                      "没有记录的问题我不会拿概数或印象来答——需要的话请先在台账或会议记录里补上。",
            "sources": [],
            "notes": [],
            "trace": trace,
        }

    counts: dict[str, int] = {}
    for h in hits:
        counts[h["kind_label"]] = counts.get(h["kind_label"], 0) + 1
    headline = (
        f"检索到 {len(hits)} 条记录（"
        + "、".join(f"{k} {v} 条" for k, v in counts.items())
        + "），依据如下。"
    )

    return {
        "refused": False,
        "question": query,
        "planner": "rules",
        "intent": "knowledge",
        "headline": headline,
        # 条目原文，前端逐条渲染；quote 单独给一份，方便「展开原文」时高亮
        "knowledge": [
            {
                "kind": h["kind"],
                "kind_label": h["kind_label"],
                "id": h["id"],
                "title": h["title"],
                "text": h["text"],
                "quote": h["quote"],
                "source": h["source"],
                "ref": h["ref"],
                "score": h["score"],
            }
            for h in hits
        ],
        "facts": [],
        "sources": [h["source"] for h in hits if h["kind"] == "metric"],
        "notes": [],
        "trace": trace,
    }
