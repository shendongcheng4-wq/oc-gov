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


def list_threads() -> list[dict[str, Any]]:
    """列表只回头部信息，不把全部消息带上（一个长会话能到几十 KB）。"""
    items = []
    for t in _cfg()["threads"]:
        msgs = t.get("messages") or []
        last = msgs[-1] if msgs else None
        items.append(
            {
                "id": t["id"],
                "title": t.get("title") or "未命名对话",
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
                "model_ref": t.get("model_ref"),
                "message_count": len(msgs),
                "last_preview": (last.get("content") or "")[:60] if last else "",
                "last_role": last.get("role") if last else None,
            }
        )
    items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return items


def get(thread_id: str) -> dict[str, Any] | None:
    return _find(_cfg(), thread_id)


# ── 写 ──────────────────────────────────────────────────────────────────


def create(title: str | None = None, model_ref: str | None = None) -> dict[str, Any]:
    thread = {
        "id": f"th_{secrets.token_hex(6)}",
        "title": (title or "").strip()[:MAX_TITLE],
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


def ensure(thread_id: str | None, model_ref: str | None = None) -> dict[str, Any]:
    """有就用，没有（或已被删）就新建。接口层直接用这个，省一次判空。"""
    if thread_id:
        found = get(thread_id)
        if found:
            return found
    return create(model_ref=model_ref)


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


def rename(thread_id: str, title: str) -> bool:
    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        thread = _find(cfg, thread_id)
        if thread is not None:
            thread["title"] = title.strip()[:MAX_TITLE]
        return cfg

    store.update(FILE, _do, _empty())
    return bool(get(thread_id))


def delete(thread_id: str) -> bool:
    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["threads"] = [t for t in cfg["threads"] if t["id"] != thread_id]
        return cfg

    store.update(FILE, _do, _empty())
    return get(thread_id) is None


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


def context(thread_id: str | None, limit: int = 6) -> list[dict[str, str]]:
    """取最近 N 条消息，转成 OpenAI messages 格式，供意图解析参考指代。

    limit 按「条」算，取偶数条能保证 user/assistant 成对出现。
    """
    if not thread_id:
        return []
    thread = get(thread_id)
    if not thread:
        return []

    msgs = thread.get("messages") or []
    tail = msgs[-limit:] if limit else msgs
    out = []
    for m in tail:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            out.append({"role": "assistant", "content": _summary(m.get("meta"))})
    return out


def stats() -> dict[str, Any]:
    cfg = _cfg()
    return {
        "threads": len(cfg["threads"]),
        "messages": sum(len(t.get("messages") or []) for t in cfg["threads"]),
    }
