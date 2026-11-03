"""跨会话记忆：记住领导长期关注哪些指标、常看哪个维度、盯什么主题。

设计取舍（跟 octop 的 memory 是两回事）：

- **存什么**：只记「关注了什么」，不记「数值是多少」。指标名、区县维度、主题
  标签可以进记忆，任何数值（8.9%、1286.5 亿）都不落库 —— 这跟 core/chat.py
  里「模型不接触数字」是同一条边界，跨会话背景拼给模型时也只含名称不含数。
- **怎么存**：落在 octop-memory 的 sqlite 后端（data/memory.db）。octop-memory
  是零强制依赖的通用库（核心纯标准库 + SQLite/FTS5），store 走手动写入路径
  （extractor_version="manual/v1"），不调模型、不依赖 LLM。
- **懒加载 + 可降级**：octop-memory 没装、库坏了、FTS 不可用，都静默降级为
  「无记忆」，不影响问答主链路。oc-gov 原本零依赖能跑，加了这个也依然是
  可选增强，不是硬前提。
- **范围**：指标（metric_codes）+ 维度（问句里的区县/板块等词）+ 主题
  （指标责任部门 → 领域）。全部规则算得出来，不引入模型提炼，守住边界。

回灌给模型的方式不是「全量塞」，而是按当前问句 `recall()` 定向召回最相关的
几条，控制在一个小预算内 —— 否则记忆越大，上下文越脏，反而伤问答。
"""

from __future__ import annotations

import re
from typing import Any

import store

FILE = "memory"

# octop-memory 是可选的。没装时所有函数都返回无害的空结果，绝不抛异常。
try:
    from octop_memory import Memory as _OctoMemory

    _HAVE_MEMORY = True
except Exception:  # noqa: BLE001 —— 任何导入失败都当「没有记忆」
    _OctoMemory = None  # type: ignore[assignment]
    _HAVE_MEMORY = False

_NAMESPACE = "oc-gov"

# 维度词：问句里出现这些词，说明领导这轮想看这个维度的分解。
# 跟 ask.py 的 _DIM_WORDS 保持同一套口径，避免两处对不上。
_DIM_WORDS = {
    "区县": ["区县", "各区", "分区", "县区", "板块"],
    "责任单位": ["部门", "单位", "局", "承办"],
}

# 主题：由指标责任部门归到领域。部门名是 mockdata 里写死的，这里做一层
# 稳定映射，接真实数据时只改 mockdata 的 department，不用动这里的关键词。
_DEPT_TO_THEME = [
    ("统计局", "宏观经济"),
    ("调查队", "就业民生"),
    ("财政局", "财政税收"),
    ("发展和改革", "项目投资"),
    ("投资促进", "招商引资"),
    ("生态环境", "生态环境"),
    ("市场监督", "市场活力"),
    ("政务服务和数据", "政务服务"),
    ("热线", "政务服务"),
    ("政府办公室", "民生实事"),
    ("民生", "民生实事"),
]


def _theme_for_department(department: str | None) -> str | None:
    if not department:
        return None
    for key, theme in _DEPT_TO_THEME:
        if key in department:
            return theme
    return None


def _dims_for_question(question: str) -> list[str]:
    return [dim for dim, words in _DIM_WORDS.items() if any(w in question for w in words)]


def _db_path() -> str:
    """记忆库路径，跟 store 的 DATA_DIR 保持一致，落在 data/memory.db。"""
    return str(store.DATA_DIR / "memory.db")


def _client() -> Any | None:
    """取记忆客户端。没装 / 初始化失败都返回 None，调用方据此降级。"""
    if not _HAVE_MEMORY:
        return None
    try:
        return _OctoMemory(namespace=_NAMESPACE, backend="sqlite", backend_config={"db_path": _db_path()})
    except Exception:  # noqa: BLE001 —— 库坏了就当作没记忆
        return None


# ── 写 ──────────────────────────────────────────────────────────────────


def record(thread_id: str | None, question: str, result: dict[str, Any]) -> None:
    """一轮问答结束后，把「关注了什么」沉淀进记忆。失败静默，不打断主链路。

    - 指标：result["metric_names"]（已在 ask() 里注入，不含数值）
    - 维度：问句里出现的区县/责任单位等词
    - 主题：指标责任部门 → 领域（规则映射）

    每一条记忆是一枚 atom（leaf），topic 标类型、metadata 存指标 code 供精确
    取回。重复问同一个指标会攒多条 atom，召回时按频次自然加权 —— 这正是
    「长期关注」的朴素表达，不需要额外维护计数器。
    """
    if not _HAVE_MEMORY:
        return
    try:
        mem = _client()
        if mem is None:
            return
    except Exception:  # noqa: BLE001
        return

    names = result.get("metric_names") or []
    codes = result.get("metric_codes") or []

    # 指标记忆：一条指标一枚 atom，topic=「指标」，metadata 带 code。
    for name, code in zip(names, codes):
        try:
            mem.store(
                f"关注指标：{name}",
                topic="指标",
                conversation_id=thread_id,
                metadata={"kind": "metric", "code": code, "name": name},
            )
        except Exception:  # noqa: BLE001
            continue

    # 维度记忆
    for dim in _dims_for_question(question):
        try:
            mem.store(
                f"常看的分解维度：{dim}",
                topic="维度",
                conversation_id=thread_id,
                metadata={"kind": "dim", "dim": dim},
            )
        except Exception:  # noqa: BLE001
            continue

    # 主题记忆：由命中的指标责任部门归到领域。
    themes: set[str] = set()
    for code in codes:
        dept = None
        # 责任部门在 mockdata 的 METRICS 里，这里延迟导入避免循环依赖
        try:
            import metrics.mockdata as md  # noqa: F401

            meta = md.METRICS.get(code) or {}
            dept = meta.get("department")
        except Exception:  # noqa: BLE001
            dept = None
        theme = _theme_for_department(dept)
        if theme:
            themes.add(theme)

    for theme in themes:
        try:
            mem.store(
                f"长期关注的主题：{theme}",
                topic="主题",
                conversation_id=thread_id,
                metadata={"kind": "theme", "theme": theme},
            )
        except Exception:  # noqa: BLE001
            continue


# ── 读 ──────────────────────────────────────────────────────────────────


def background(question: str, limit: int = 4) -> list[dict[str, str]]:
    """新问答前，召回跨会话关注背景，转成 model messages 拼在 history 前面。

    定向召回：按当前问句全文搜索最相关的记忆，只取最相关的几条，避免把
    全部历史关注一股脑塞进去。返回的是 system 消息 —— 模型据此知道
    「领导长期关心这些」，但仍是它来规划查询，不是记忆替它决定。

    没记忆 / 没装库时返回空列表，调用方直接忽略即可。
    """
    if not _HAVE_MEMORY:
        return []
    try:
        mem = _client()
        if mem is None:
            return []
    except Exception:  # noqa: BLE001
        return []

    # 不靠 FTS 全文匹配（中文分词太粗，问「工业」只会命中带「工业」二字的那条，
    # 同属一轮关注的 GDP、区县维度全漏掉）。改成取最近 N 天全部 atom，按
    # 「指标 / 维度 / 主题」分桶统计频次，输出 Top 榜 —— 这才是「长期关注画像」，
    # 稳定、可预期，不随问句措辞漂移。
    try:
        from datetime import datetime, timedelta, timezone

        atoms = mem.search_atoms_by_time_range(
            start=datetime.now(timezone.utc) - timedelta(days=90),
            limit=500,
        ) or []
    except Exception:  # noqa: BLE001
        return []

    if not atoms:
        return []

    from collections import Counter

    metrics: Counter[str] = Counter()
    dims: Counter[str] = Counter()
    themes: Counter[str] = Counter()
    for a in atoms:
        text = getattr(a, "assertion", None) or ""
        if text.startswith("关注指标："):
            metrics[text[len("关注指标："):]] += 1
        elif text.startswith("常看的分解维度："):
            dims[text[len("常看的分解维度："):]] += 1
        elif text.startswith("长期关注的主题："):
            themes[text[len("长期关注的主题："):]] += 1

    # 拼成一句背景。指标优先、维度次之、主题垫底，各取最靠前的几项。
    parts: list[str] = []
    if metrics:
        parts.append("关注指标：" + "、".join(n for n, _ in metrics.most_common(limit)))
    if dims:
        parts.append("常看维度：" + "、".join(d for d, _ in dims.most_common(2)))
    if themes:
        parts.append("关注主题：" + "、".join(t for t, _ in themes.most_common(2)))

    if not parts:
        return []

    text = "以下是你长期关注的内容（跨会话记忆，供规划查询时参考）：" + "；".join(parts)
    return [{"role": "system", "content": text}]


def stats() -> dict[str, Any]:
    """记忆层状态，供 /api/health 或测试用。没装库返回 enabled=False。"""
    return {"enabled": _HAVE_MEMORY, "db": _db_path() if _HAVE_MEMORY else None}
