"""材料依据：写的时候就该把原文摆到手边。

材料助手的正文是装配出来的，每一句都能追到某个字段。但字段是压缩过的——
台账里存着「受管线迁改影响，2 条道路进度滞后约 15 天」这句话（谁报的、哪天报的、
原话怎么说），到了纸面上只剩「逾期 1 项」。写材料的人真正需要的是那句原话，
而这句话一直躺在系统里，只是写作的时候调不出来。

所以这个模块只做一件事：**按章节把对应的原文摘出来，附在纸面旁边**。
它不生成任何新文字，不改材料，也不进正文（理由见文件末尾）。

取法分两路，这一点决定了它靠不靠得住：

- **按编号精确取**：台账事项 id → 办理经过，会议 id → 议定事项原文，
  指标 code → 口径提示。这一路不需要检索——关系本来就存在，编号是唯一的，
  取出来的东西不可能「差一点」。
- **按主题检索**：只有资料库走这一路。人工上传的政策文件、上级通知没有编号
  挂在某条台账上，只能靠相似度去找。

**也不进模型上下文。** 依据是给人核对的。塞给模型，它会把原话改写一遍再写出来，
而原话的价值恰恰在于它没被改过——「受管线迁改影响，2 条道路进度滞后约 15 天」
被模型润色成「部分道路因管线迁改略有滞后」，责任就说不清了。

为什么不对知识库再跑一遍关键词检索：知识库那三类条目（指标口径、督办事项、
会议记录）正是上面「按编号精确取」的同一批数据。再检索一次只会把同一段话
换个排序摆第二遍，还会因为通用词虚高混进不相关的条目。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "oversight", ROOT / "core" / "meeting"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import mockdata as md  # noqa: E402
import oversight as ov  # noqa: E402
from ledger import get as meeting_get  # noqa: E402

# 一节最多摆几条。依据是核对用的，不是把台账整个搬过来——
# 超过四条，人就不看了，等于没给。
MAX_PER_BLOCK = 4
# 单条摘录上限。原话要能一眼看完，看不完的部分去台账点开看，
# 材料里截断比整段贴进来更诚实：「…」明确告诉人有下文。
MAX_QUOTE = 160
MAX_QUOTE_LONG = 220
# 资料库 bigram 降级时的分数门槛（向量路在 rag 内部已卡 0.55）
RAG_LEX_FLOOR = 0.40
RAG_MAX = 2

# 台账动作 → 人话。找不到就如实写「台账记录」，不猜。
_WHY = {
    "进展": "最近一次进展反馈",
    "催办": "最近一次催办记录",
    "签收": "责任单位的签收回复",
    "创建": "交办时的原始要求",
    "办结": "办结时报送的说明",
    "退回": "退回意见",
    "退回补充": "退回意见",
}

# 各节检索资料库时怎么问。写成短词而不是一句话：bigram 按二字组算覆盖率，
# 问句越长分母越大，「入河排污口排查整治 办理要求 时限 销号 整改」这种拼盘
# 反而一个片段都命中不了。宁可问两句短的，不要问一句长的。
def _doc_queries(block: dict[str, Any]) -> list[str]:
    bid = str(block.get("id") or "")
    if bid == "metrics":
        names = [str(r.get("name") or "") for r in (block.get("rows") or [])[:2]]
        return [f"{n} 口径" for n in names if n]
    if bid == "oversight":
        # 事项节列的是若干件事，逐条去资料库问一次。一条事项一个问句，
        # 拼成一句会把覆盖率摊薄；只问头两条又会漏掉排后面的那件事——
        # 而「入河排污口整治」正好可能是第五条。
        return [str(r.get("title") or "") for r in (block.get("rows") or [])[:8] if r.get("title")]
    if bid == "meeting":
        title = str((block.get("meeting") or {}).get("title") or "")
        return [title] if title else []
    return []


def _clip(text: Any, limit: int = MAX_QUOTE) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _join(parts: list[str]) -> str:
    """把几句口径连成一段。上一句已经收尾（。；！？）就不再补标点——
    否则会出现「不做环比。；总量为现价」这种一眼可见的糙。"""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and out[-1] not in "。；！？：，、）":
            out += "；"
        out += p
    return out


def _item(
    *,
    kind: str,
    kind_label: str,
    title: str,
    quote: str,
    why: str,
    when: str = "",
    by: str = "",
    ref: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "kind_label": kind_label,
        "title": title,
        "quote": quote,
        "why": why,
        "when": when,
        "by": by,
        "ref": ref or {},
    }


# --------------------------------------------------------------------------
# 台账：这条事项现在办到哪一步，谁说的
# --------------------------------------------------------------------------
def _from_oversight(block: dict[str, Any]) -> list[dict[str, Any]]:
    rows = list(block.get("rows") or [])
    # 有风险的先摆：材料里写「逾期 2 项」，读者第一反应就是「哪两项、怎么说」。
    # 稳定排序，同档内保持台账原顺序，不制造「顺序每次都不一样」的错觉。
    rows.sort(key=lambda r: 0 if r.get("risk") else 1)

    out: list[dict[str, Any]] = []
    for r in rows:
        code = str(r.get("code") or "").strip()
        if not code or code == "未落库":
            continue
        detail = ov.detail(code)
        if not detail:
            continue
        ref = {"type": "oversight", "id": code}
        events = detail.get("events") or []
        if not events:
            # 待签收的事项只有编号和交办要求。这本身就是事实：
            # 「交办了但没人接」，写材料时正该看见。
            out.append(
                _item(
                    kind="oversight",
                    kind_label="台账",
                    title=str(r.get("title") or code),
                    quote=_clip(detail.get("standard") or ""),
                    why="交办要求（尚无办理记录）",
                    when=str(detail.get("issued_at") or ""),
                    ref=ref,
                )
            )
        else:
            ev = events[-1]
            text = str(ev.get("text") or "").strip()
            if not text:
                continue
            out.append(
                _item(
                    kind="oversight",
                    kind_label="台账",
                    title=str(r.get("title") or code),
                    quote=_clip(text),
                    why=_WHY.get(str(ev.get("type") or ""), "台账记录"),
                    when=str(ev.get("ts") or ""),
                    by=str(ev.get("actor") or ""),
                    ref=ref,
                )
            )
        if len(out) >= MAX_PER_BLOCK:
            break
    return out


# --------------------------------------------------------------------------
# 会议：这场会到底定了什么，原话
# --------------------------------------------------------------------------
def _meeting_quotes(mid: str, limit: int) -> list[dict[str, Any]]:
    full = meeting_get(mid) if mid else None
    if not full:
        return []
    ref = {"type": "meeting", "id": mid}
    held = str(full.get("held_at") or "")
    out: list[dict[str, Any]] = []
    for it in (full.get("items") or [])[:limit]:
        quote = _clip(it.get("quote") or it.get("title") or "")
        if not quote:
            continue
        out.append(
            _item(
                kind="meeting",
                kind_label="会议纪要",
                title=str(it.get("title") or "议定事项"),
                quote=quote,
                why="议定事项原文",
                when=held,
                ref=ref,
            )
        )
    return out


def _from_meeting(block: dict[str, Any]) -> list[dict[str, Any]]:
    if block.get("kind") == "meeting":
        m = block.get("meeting") or {}
        mid = str(m.get("id") or "")
        out = _meeting_quotes(mid, MAX_PER_BLOCK - 1)
        if len(out) < MAX_PER_BLOCK:
            full = meeting_get(mid) if mid else None
            body = _clip((full or {}).get("text") or "", MAX_QUOTE_LONG)
            # 议定事项的原句本来就在纪要原文里，两者都有就别重复摆一遍
            if body and not out:
                out.append(
                    _item(
                        kind="meeting",
                        kind_label="会议纪要",
                        title=f"{m.get('title') or mid}（纪要原文）",
                        quote=body,
                        why="会议纪要原文",
                        when=str(m.get("held_at") or ""),
                        ref={"type": "meeting", "id": mid},
                    )
                )
        return out

    # 「会议落实」这一节列的是几场会。摆最近一场有议定事项的会的原话，
    # 比把每场会都摆一遍更有用——写材料时一般只引用最近那次。
    out = []
    for r in block.get("rows") or []:
        ref = str(r.get("ref") or "")
        if not ref.startswith("meeting:"):
            continue
        got = _meeting_quotes(ref.split(":", 1)[1], 2 - len(out))
        out.extend(got)
        if len(out) >= 2:
            break
    return out


# --------------------------------------------------------------------------
# 指标：口径里最容易被误读的那句
# --------------------------------------------------------------------------
def _from_metrics(block: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in block.get("rows") or []:
        code = str(r.get("code") or "")
        if code not in md.METRICS:
            continue
        d = md.get_metric(code)
        caveats = [str(c).strip() for c in (d.get("caveats") or []) if str(c).strip()]
        if not caveats:
            # 没有提示项的指标只摆定义，等于把「地区生产总值」四个字再说一遍，
            # 不占这个位置
            continue
        out.append(
            _item(
                kind="metric",
                kind_label="指标口径",
                title=str(d.get("name") or code),
                quote=_clip(_join(caveats)),
                why=f"{d.get('department') or ''} {d.get('report') or ''}".strip(),
                ref={"type": "metric", "id": code},
            )
        )
        if len(out) >= 3:
            break
    return out


# --------------------------------------------------------------------------
# 资料库：唯一真的需要检索的一路
# --------------------------------------------------------------------------
def _doc_ready() -> dict[str, Any]:
    """资料库里有没有东西可查。没有就别假装查过。"""
    try:
        import rag  # noqa: PLC0415  —— 只有这一路用到，放在顶层会拖慢装配

        st = rag.stats()
        bases = [b["id"] for b in rag.list_bases()] if st.get("documents") else []
        return {"documents": int(st.get("documents") or 0), "bases": bases}
    except Exception:  # 资料库坏了不能连带材料生成也失败
        return {"documents": 0, "bases": []}


def _from_docs(block: dict[str, Any], ready: dict[str, Any]) -> list[dict[str, Any]]:
    if not ready["bases"]:
        return []
    queries = _doc_queries(block)
    if not queries:
        return []

    try:
        import rag  # noqa: PLC0415

        hits: list[tuple[float, str, dict[str, Any]]] = []
        for q in queries[:8]:
            for h in rag.search(ready["bases"], q, k=RAG_MAX):
                hits.append((float(h.get("score") or 0), q, h))
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    hits.sort(key=lambda t: t[0], reverse=True)
    for score, query, h in hits:
        if h.get("method") != "vector" and score < RAG_LEX_FLOOR:
            # 降级检索没有绝对阈值，只有覆盖率。这里再卡一道：
            # 宁可这一节没有外部依据，也不要摆一段似是而非的政策原文。
            # 实测「12345 停车难专项治理」能召回海绵城市文件，覆盖率 0.067——
            # 这种就是必须挡掉的。
            continue
        key = (str(h.get("doc_id") or ""), int(h.get("ordinal") or 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _item(
                kind="doc",
                kind_label="上传文档",
                title=str(h.get("doc_name") or ""),
                quote=_clip(h.get("text") or ""),
                # 检索词要留出来：一件事挂回来一段政策原文，得说清是照着哪件事找的，
                # 否则读者不知道这段和纸面哪一行有关系
                why=f"{h.get('kb_name')}｜第 {int(h.get('ordinal') or 0) + 1} 段｜检索词「{query}」",
                ref={"type": "rag", "id": f"{h.get('kb_id')}:{h.get('doc_id')}"},
            )
        )
        if len(out) >= RAG_MAX:
            break
    return out


# --------------------------------------------------------------------------
# 对外：给每一节挂上依据
# --------------------------------------------------------------------------
_HANDLERS = {
    "table": _from_metrics,      # 目前只有指标节是 table
    "oversight": _from_oversight,
    "meeting": _from_meeting,
    "list": _from_meeting,
}


def for_block(block: dict[str, Any], ready: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    ready = ready if ready is not None else _doc_ready()
    fn = _HANDLERS.get(str(block.get("kind") or ""))
    items = fn(block) if fn else []
    items.extend(_from_docs(block, ready))

    # 同一条原文可能被两路取到（比如会议议定事项和资料库里同一份纪要）。
    # 去重看「哪句话」，不看 id——来源不同但话一样，摆两遍就是噪音。
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for it in items:
        key = it["quote"][:48]
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq[: MAX_PER_BLOCK + RAG_MAX]


def attach(blocks: list[dict[str, Any]], ready: dict[str, Any] | None = None) -> dict[str, Any]:
    """就地把依据挂到每一节上，返回整体统计。

    只在有依据时才写 `evidence` 键——空数组会让前端摆一个「依据 0 条」的
    空壳，看着像功能坏了。
    """
    ready = ready if ready is not None else _doc_ready()
    kinds: dict[str, int] = {}
    total = 0
    sections = 0
    for b in blocks:
        items = for_block(b, ready)
        if not items:
            continue
        b["evidence"] = items
        sections += 1
        total += len(items)
        for it in items:
            kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1

    return {
        "sections": sections,
        "items": total,
        "kinds": kinds,
        # 资料库为空要说出来：用户看到「没有外部依据」才知道该去传文件，
        # 而不是以为检索没找到
        "docs": ready["documents"],
        "note": "依据取自台账办理经过、会议纪要原文和指标口径，均为系统内原始记录，未改写；"
                "只用于核对，不进正文、不随导出和打印带走。",
    }
