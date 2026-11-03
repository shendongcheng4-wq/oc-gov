"""会话存储：thread + 消息流，供多轮对话使用。

结构照抄 octop 的骨架（一条 thread 头 + 消息按 `seq` 单调递增追加），但刻意简化：

- **没有 checkpoint / projection 双来源**。octop 那套是为了兼容老数据的迁移产物，
  新项目只有一份存储，不需要 cursor 和回填状态。
- **流式过程中不写库**，一轮结束后一次性 append（user + assistant 两条）。
  octop 也是这个策略：几百次小写入除了拖慢磁盘没别的好处。

## 回灌给模型的历史里为什么没有数字

`context()` 拼给模型的 assistant 摘要**只有指标名和意图，不含任何数值**。
这是本项目的安全边界：模型一旦在上下文里看见「8.9%」，下一轮就更可能在自然语言
里复述甚至改动这个数字。

真正给人看的 headline 存在消息的 `content` 里，只在界面上渲染，永远不进模型。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import store

FILE = "chats"
VERSION = 1
MAX_TITLE = 24

# 多轮上下文预算。一轮问答（问题 + 不含数值的摘要）约 40-60 token，
# 600 大概够 10 轮；真超了就先丢最老的那轮。这个数比"6 条消息"更接近真实约束：
# 一条问题可以很长，按条数算等于让长问题挤掉整整一轮。
_CONTEXT_TOKENS = 600

# 会话搜索最多给几条。多到几十条时用户也不会往下翻，不如让他改词。
_SEARCH_LIMIT = 40


def _est_tokens(text: str) -> int:
    """汉字粗估 0.6 token/字。不追求准，只追求"永远不低估长度"。"""
    return max(1, int(len(str(text or "")) * 0.6))


def _now() -> int:
    return int(time.time())


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "threads": []}


def _cfg() -> dict[str, Any]:
    raw = store.read(FILE)
    if not isinstance(raw, dict) or not isinstance(raw.get("threads"), list):
        return _empty()
    raw.setdefault("version", VERSION)
    return raw


def _find(cfg: dict[str, Any], thread_id: str) -> dict[str, Any] | None:
    return next((t for t in cfg["threads"] if t["id"] == thread_id), None)


# ── 读 ──────────────────────────────────────────────────────────────────


def _snippet(msgs: list[dict[str, Any]], needle: str, width: int = 44) -> str | None:
    """命中位置前后各截一段。搜索要回答的是"为什么这条会出来"，
    只给标题的话，按内容命中的结果看起来像是乱入。"""
    for msg in msgs:
        text = str(msg.get("content") or "")
        pos = text.lower().find(needle)
        if pos < 0:
            continue
        start = max(0, pos - width // 2)
        end = min(len(text), pos + len(needle) + width // 2)
        return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")
    return None


def list_threads(query: str | None = None, owner: str | None = None) -> list[dict[str, Any]]:
    """列表只回头部信息，不把全部消息带上（一个长会话能到几十 KB）。

    带 `query` 时在标题和全部消息正文里找，命中返回一条 `snippet`。
    为什么不在前端过滤：前端只有 last_preview（最后一条消息前 60 字），
    搜"上次开会定了什么"这种按内容找的，必然搜不到。

    `owner` 给定时只回该身份的会话；传 None 表示不过滤（内部统计用）。
    **默认必须传**：以前不按身份过滤，切到副市长看到的还是市长那一屏会话。
    """
    needle = str(query or "").strip().lower()
    owner_key = None if owner is None else str(owner).strip()
    items = []
    for t in _cfg()["threads"]:
        if owner_key is not None and (t.get("owner") or "") != owner_key:
            continue
        msgs = t.get("messages") or []
        title = t.get("title") or "未命名对话"
        snippet = None
        if needle:
            if needle in title.lower():
                snippet = None
            else:
                snippet = _snippet(msgs, needle)
                if snippet is None:
                    continue
        last = msgs[-1] if msgs else None
        item = {
            "id": t["id"],
            "title": title,
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "model_ref": t.get("model_ref"),
            "message_count": len(msgs),
            "last_preview": (last.get("content") or "")[:60] if last else "",
            "last_role": last.get("role") if last else None,
        }
        if needle:
            item["snippet"] = snippet
        items.append(item)
    items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return items[:_SEARCH_LIMIT] if needle else items


def get(thread_id: str, owner: str | None = None) -> dict[str, Any] | None:
    """按 id 取会话。给了 `owner` 时，不属于他的会话一律当「不存在」。

    返回 404 而不是 403：403 等于告诉对方「这个 id 是真的，只是不给你看」，
    拿 id 逐个试就能探出别人的会话数量。会话 id 是随机的，但没必要给这个口子。
    """
    found = _find(_cfg(), thread_id)
    if found is None:
        return None
    if owner is not None and (found.get("owner") or "") != str(owner).strip():
        return None
    return found


# ── 写 ──────────────────────────────────────────────────────────────────


def create(
    title: str | None = None,
    model_ref: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    thread = {
        "id": f"th_{secrets.token_hex(6)}",
        "title": (title or "").strip()[:MAX_TITLE],
        # 归属。空串只可能出现在历史数据里（见 own_orphans），新建一律带身份
        "owner": str(owner or "").strip(),
        "created_at": _now(),
        "updated_at": _now(),
        "model_ref": model_ref,
        "messages": [],
    }

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["threads"].append(thread)
        return cfg

    store.update(FILE, _do, _empty())
    return thread


def ensure(
    thread_id: str | None,
    model_ref: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """有就用，没有（或已被删、或不属于这个人）就新建。接口层直接用这个，省一次判空。

    「不属于自己」也走新建而不是报错：切换身份后前端可能还揣着上一个人的
    thread_id，报错只会让用户莫名其妙。但**不能**静默复用它 —— 那等于把
    别人的会话接着写下去。
    """
    if thread_id:
        found = get(thread_id, owner=owner)
        if found:
            return found
    return create(model_ref=model_ref, owner=owner)


def own_orphans(owner: str) -> int:
    """把还没有 `owner` 字段的老会话一次性划给某个身份，返回改动条数。

    为什么不"无主 = 所有人可见"：那等于隔离没做。为什么不直接删：用户的会话
    不是垃圾，删了没法解释。这批数据本来就是演示阶段大家共用的，归给默认身份
    是唯一不丢数据、又能让隔离真正生效的做法。

    只认「字段缺失」，不认空串：空串是显式写入的值，不该每启动一次就被重新认领。
    """
    changed = {"n": 0}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        for t in cfg["threads"]:
            if "owner" not in t:
                t["owner"] = str(owner or "").strip()
                changed["n"] += 1
        return cfg

    store.update(FILE, _do, _empty())
    return changed["n"]


def append(
    thread_id: str,
    role: str,
    content: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """追加一条消息，seq 取 max+1。线程不存在返回 None。"""
    added: dict[str, Any] = {}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is None:
            return cfg
        msgs = thread.setdefault("messages", [])
        seq = max((m.get("seq") or 0 for m in msgs), default=0) + 1
        msg = {
            "id": f"m_{secrets.token_hex(6)}",
            "seq": seq,
            "role": role,
            "content": content,
            "ts": _now(),
        }
        if meta:
            msg["meta"] = meta
        msgs.append(msg)
        thread["updated_at"] = msg["ts"]
        # 首条用户消息自动当标题，省一个「请命名」的步骤
        if role == "user" and not thread.get("title"):
            thread["title"] = content.strip().replace("\n", " ")[:MAX_TITLE]
        added.update(msg)
        return cfg

    store.update(FILE, _do, _empty())
    return added or None


def rename(thread_id: str, title: str, owner: str | None = None) -> bool:
    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is not None:
            thread["title"] = title.strip()[:MAX_TITLE]
        return cfg

    if get(thread_id, owner=owner) is None:
        return False
    store.update(FILE, _do, _empty())
    return True


def delete(thread_id: str, owner: str | None = None) -> bool:
    if get(thread_id, owner=owner) is None:
        return False

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["threads"] = [t for t in cfg["threads"] if t["id"] != thread_id]
        return cfg

    store.update(FILE, _do, _empty())
    return get(thread_id) is None


def drop_last(thread_id: str) -> dict[str, Any] | None:
    """删掉最后一条消息，返回被删的那条。

    只给"重试"用：上一轮被用户停掉时落了一句「已停止生成」占位，重新问同一句时
    这条占位应当被真正的答案顶掉 —— 留着它，会话里就会出现
    问 → 已停止 → 问 → 答案，同一句话连着问了两遍。
    """
    removed: dict[str, Any] = {}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is None:
            return cfg
        msgs = thread.get("messages") or []
        if msgs:
            removed.update(msgs.pop())
            thread["updated_at"] = _now()
        return cfg

    store.update(FILE, _do, _empty())
    return removed or None


def set_model_ref(thread_id: str, model_ref: str | None) -> None:
    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is not None:
            thread["model_ref"] = model_ref
        return cfg

    store.update(FILE, _do, _empty())


def clear_model_refs(prefix: str) -> int:
    """删除 provider 后，把会话里指向它的 model_ref 置空。返回改动条数。"""
    changed = {"n": 0}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        for t in cfg["threads"]:
            ref = t.get("model_ref")
            if ref and ref.startswith(prefix):
                t["model_ref"] = None
                changed["n"] += 1
        return cfg

    store.update(FILE, _do, _empty())
    return changed["n"]


# ── 给模型用的上下文 ────────────────────────────────────────────────────


def _summary(meta: dict[str, Any] | None) -> str:
    """把上一条回答压成一句不含数值的摘要。

    只保留「问了哪几个指标、什么意图」，数字一律剔除 —— 见模块开头的说明。
    """
    if not meta:
        return "（上一轮没有可用的摘要）"

    if meta.get("cancelled"):
        return "上一轮我按用户要求中止了，没有给出答案。"

    if meta.get("refused"):
        return "上一轮我拒答了：问题对应的指标未注册。"

    # 建会草稿不是一次问答：它没有数值也没有指标，硬套下面的模板会得到
    # 「我meet，但没有命中任何已注册指标」——一句在中文里根本不成立的话。
    if meta.get("meet_draft"):
        d = meta["meet_draft"]
        return f"上一轮我在约会议（{d.get('title') or '未定议题'}，{d.get('held_at') or '未定时间'}），只出了草稿，等用户确认。"

    names = meta.get("metric_names") or []
    intent = meta.get("intent") or "value"
    intent_cn = {
        "value": "取值",
        "trend": "看走势",
        "breakdown": "看分解",
        "compare": "做对比",
        "definition": "问口径",
    }.get(intent, intent)

    if not names:
        return f"上一轮我{intent_cn}，但没有命中任何已注册指标。"
    return f"上一轮我{intent_cn}，用的是这些已注册指标：{'、'.join(names)}。具体数值见界面，不在此重复。"


def context(
    thread_id: str | None,
    token_budget: int | None = None,
    max_turns: int = 10,
) -> list[dict[str, str]]:
    """取最近若干轮对话，转成 OpenAI messages 格式，供意图解析参考指代。

    按 **token 预算** 取，不按条数：原来写死 `[:-6]`（3 轮），第 4 轮开始
    「那上个月呢」「跟去年比呢」这类指代就断，而问数天生是长会话。
    现在从尾部按「一轮」（user + assistant 成对）往回收，超出预算就停。

    预算按字符数粗估（中文约 0.6 token/字），宁可少带一轮，也不要让 prompt
    超长被服务端拒掉 —— 少一轮最多少理解一个指代，超长是整个问题都答不了。
    """
    if not thread_id:
        return []
    thread = get(thread_id)
    if not thread:
        return []

    entries: list[tuple[str, str]] = []
    for msg in thread.get("messages") or []:
        role = msg.get("role")
        if role == "user":
            entries.append(("user", (msg.get("content") or "").strip()))
        elif role == "assistant":
            entries.append(("assistant", _summary(msg.get("meta"))))

    # 成对：assistant 紧跟 user 才算一轮；落单的（比如上一轮被停止）自己算一轮
    turns: list[list[tuple[str, str]]] = []
    i = len(entries) - 1
    while i >= 0:
        if entries[i][0] == "assistant" and i - 1 >= 0 and entries[i - 1][0] == "user":
            turns.append(entries[i - 1 : i + 1])
            i -= 2
        else:
            turns.append([entries[i]])
            i -= 1
    turns.reverse()

    budget = _CONTEXT_TOKENS if token_budget is None else int(token_budget)
    picked: list[list[tuple[str, str]]] = []
    used = 0
    for turn in reversed(turns):
        if len(picked) >= max_turns:
            break
        cost = sum(_est_tokens(text) for _, text in turn)
        # 至少带最近一轮：一句“那上个月呢”没有上一轮就完全没法解析
        if picked and used + cost > budget:
            break
        picked.append(turn)
        used += cost
    picked.reverse()

    return [
        {"role": role, "content": text}
        for turn in picked
        for role, text in turn
    ]


def known_metrics(thread_id: str | None, limit: int = 12) -> list[str]:
    """本会话至今涉及过的指标名（按时间先后，去重）。

    给 planner 的「常驻记忆」：历史窗口会被预算截断，但「这个指标」指谁
    这件事不该跟着一起被截掉。只取名字，不取数值 —— 与 context 的安全边界一致。
    """
    if not thread_id:
        return []
    thread = get(thread_id)
    if not thread:
        return []

    names: list[str] = []
    for msg in reversed(thread.get("messages") or []):
        meta = msg.get("meta") or {}
        for name in reversed(meta.get("metric_names") or []):
            name = str(name or "").strip()
            if name and name not in names:
                names.append(name)
    names.reverse()
    return names[-limit:] if limit else names


def usage_total(thread: dict[str, Any] | None) -> dict[str, int]:
    """本会话累计用量。老会话没有 meta.usage，返回全 0 而不是报错。"""
    total = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for msg in (thread or {}).get("messages") or []:
        usage = (msg.get("meta") or {}).get("usage") or {}
        for key in total:
            total[key] += int(usage.get(key) or 0)
    return total


def stats(owner: str | None = None) -> dict[str, Any]:
    """会话与消息总数。带 owner 时只数这个人的 —— 界面上的「共 N 条」如果
    数的是别人的会话，切换身份后数字对不上，反而会让人以为数据丢了。"""
    cfg = _cfg()
    owner_key = None if owner is None else str(owner).strip()
    threads = [
        t for t in cfg["threads"] if owner_key is None or (t.get("owner") or "") == owner_key
    ]
    return {
        "threads": len(threads),
        "messages": sum(len(t.get("messages") or []) for t in threads),
    }
