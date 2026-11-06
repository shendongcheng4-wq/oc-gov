"""查知识库原文（只读）。

返回的是**逐字原文和出处**，不是模型复述。和 ``/api/knowledge`` 走同一套
检索（``core/knowledge.search``），向量可用走语义，不可用退 bigram。
"""

from __future__ import annotations

import json
from typing import Any

from octop_harness.plugins import PluginContext

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ensure_paths() -> None:
    """把 core 及其子模块放进 sys.path。

    自包含：不依赖 app/main.py 事先铺好路径，任何加载器（octop_harness 或内置
    轻量宿主）直接 import 本插件都能跑。
    """
    for sub in ("core", "core/metrics", "core/oversight", "core/meeting"):
        p = ROOT / sub
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))



def _payload(data: Any, text: str) -> str:
    return json.dumps(
        {"octop_ui": {"renderer": "knowledge_hits", "version": 1}, "data": data, "text": text},
        ensure_ascii=False,
    )


async def search_knowledge(query: str, limit: int = 6) -> str:
    """在指标口径 / 督办事项 / 会议记录里检索原文。"""
    import knowledge  # 由 toolhost 放进 sys.path

    q = (query or "").strip()
    if not q:
        return _payload({"error": "query_required"}, "请给出要查的内容。")

    n = max(1, min(int(limit or 6), 20))
    hits = knowledge.search(q, limit=n)
    if not hits:
        return _payload(
            {"query": q, "hits": []},
            f"知识库里没有和「{q}」相关的记录。",
        )

    lines = [f"命中 {len(hits)} 条："]
    for h in hits:
        src = h.get("source") or {}
        where = src.get("department") or ""
        report = str(src.get("report") or "").strip()
        when = h.get("when") or ""
        head = f"· [{h.get('kind_label') or h.get('kind')}] {h.get('title')}"
        tail = "｜".join(x for x in (where, report, str(when)) if x)
        if tail:
            head += f"｜{tail}"
        quote = (h.get("quote") or h.get("text") or "").strip().replace("\n", " ")
        lines.append(f"{head}\n  {quote[:160]}")
    return _payload({"query": q, "hits": hits}, "\n".join(lines))


def setup(ctx: PluginContext) -> None:
    _ensure_paths()
    ctx.tool(
        "search_knowledge",
        search_knowledge,
        description=(
            "在本市知识库（指标口径、督办事项、会议记录与议定）里检索原文，"
            "返回逐字引句和出处。limit 为最多条数（默认 6，1–20）。只读。"
        ),
    )
