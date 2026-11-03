"""收藏问答：把一条回答钉进「我的关注」。

**收藏的是原文，不是摘要。** `id` 就是那条 assistant 消息的 id，点开回到原会话的
原位。所以这里写的时候**不信前端传的正文**，只收 `(thread_id, message_id)`，
由本模块回 `chats` 取问题与回答 —— 否则收藏夹可以塞进任何一句话，
"这是我查过的依据"就成了自说自话。

和关注指标的关系：指标关注是「我要盯着这个数」，收藏问答是「我要留着这个结论」。
两者的 id 空间、上限、取消方式都不同，硬塞进 `watch`（一个 code 数组）会把
`people.set_watch` 的整份替换语义弄脏，所以单独一个 `saved` 键、单独一套接口。

按人存，和 watch/seen/layout 同一个文件（`data/workspaces.json`）：

    {"people": {"mayor": {"saved": [{"id": "m_xxx", "thread_id": "th_xxx", ...}]}}}
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import chat  # noqa: E402
import people  # noqa: E402
import store  # noqa: E402

STORE_NAME = "workspaces"
# 上限 24：收藏夹不是归档。到顶了让人先清，比无限长出一个"再也不打开"的列表诚实。
MAX_SAVED = 24
# 正文截断：列表里一行放不下全文，点开才是原文。存全文只会把 workspaces.json
# 撑成第二个 chats.json —— 那两份数据一旦不同步，"哪份是真的"就说不清了。
HEADLINE_MAX = 240
QUESTION_MAX = 80


def _items_of(pid: str | None) -> list[dict[str, Any]]:
    """某人已收藏的条目（原始顺序：最近的在前）。"""
    key = people.resolve(pid)
    doc = store.read(STORE_NAME, {}) or {}
    people_map = doc.get("people") if isinstance(doc, dict) else None
    stored = (people_map or {}).get(key) or {}
    raw = stored.get("saved")
    if not isinstance(raw, list):
        return []
    out = []
    for it in raw:
        if isinstance(it, dict) and isinstance(it.get("id"), str) and it["id"]:
            out.append(it)
    return out


def _squash(text: str, limit: int) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _locate(thread_id: str, message_id: str, owner: str | None = None) -> tuple[dict[str, Any], dict[str, Any]] | str:
    """回到会话里找那条回答，顺带把它的提问也捞出来。找不到返回中文原因。

    `owner` 必须传：收藏是往**自己**名下写的，但会话可能不是自己的。不核归属的话，
    拿到别人的 thread_id + message_id 就能把对方的回答收进自己的收藏夹 —— 会话层
    已经一律 404 了，这里敞着就是一致性缺口。原因文案跟「会话不存在」保持一致，
    不额外透露「这会话存在但不是你的」。
    """
    thread = chat.get(thread_id, owner=owner)
    if not thread:
        return "会话不存在（可能已被删除）。"
    msgs = thread.get("messages") or []
    idx = next((i for i, m in enumerate(msgs) if m.get("id") == message_id), -1)
    if idx < 0:
        return "这条回答不在该会话里。"
    msg = msgs[idx]
    if msg.get("role") != "assistant":
        return "只能收藏回答，不能收藏提问。"
    question = ""
    for prev in reversed(msgs[:idx]):
        if prev.get("role") == "user":
            question = str(prev.get("content") or "")
            break
    return thread, {**msg, "question": question}


def list_of(pid: str | None) -> list[dict[str, Any]]:
    """给界面看的列表：只留还活着的（会话或消息被删了就不返回）。

    **不在读的时候顺手清理存储**：一次 GET 不该写盘，否则「打开工作台」会变成
    一次静默的删除操作，出了问题没人能复现。真正的清理在 `purge_thread()`。
    """
    out = []
    for it in _items_of(pid):
        # 带上 owner：修复写入端之前存进来的条目可能指向别人的会话，读的时候就别
        # 再把它显示出来了（否则等于用列表标题把别人的会话名报出来）。
        thread = chat.get(it.get("thread_id") or "", owner=pid)
        if not thread:
            continue
        if not any(m.get("id") == it["id"] for m in (thread.get("messages") or [])):
            continue
        out.append(
            {
                "id": it["id"],
                "thread_id": it["thread_id"],
                "question": it.get("question") or "",
                "headline": it.get("headline") or "",
                "codes": it.get("codes") or [],
                "at": it.get("at") or "",
                "ts": it.get("ts") or 0,
                # 会话还在，但标题可能改过：列表显示现名，不显示收藏当时的快照
                "thread_title": thread.get("title") or "未命名对话",
            }
        )
    return out


def _put(people_map: dict[str, Any], key: str, items: list[dict[str, Any]]) -> None:
    """把某人名下的 saved 写回去，顺带收拾空壳。

    空列表不留：`saved: []` 和「从没收藏过」在行为上一样，留在文件里只会让人以为
    这个人配过东西。写完只剩一个 updated_at 的条目更是如此（同 people.set_watch）。
    """
    prev = people_map.get(key) if isinstance(people_map.get(key), dict) else {}
    if not items:
        prev.pop("saved", None)
        if set(prev) <= {"updated_at"}:
            people_map.pop(key, None)
        return
    people_map[key] = {**prev, "saved": items, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def add(pid: str | None, thread_id: Any, message_id: Any) -> dict[str, Any]:
    """收藏一条回答。重复收藏是幂等的（返回 `duplicate=True`，不报错）。"""
    key = people.resolve(pid)
    tid = str(thread_id or "").strip()
    mid = str(message_id or "").strip()
    if not tid or not mid:
        return {"ok": False, "error": "缺少 thread_id 或 message_id。"}

    found = _locate(tid, mid, key)
    if isinstance(found, str):
        return {"ok": False, "error": found}
    thread, msg = found

    current = _items_of(key)
    if any(it["id"] == mid for it in current):
        return {"ok": True, "duplicate": True, "count": len(current), "max": MAX_SAVED, "items": list_of(key)}
    if len(current) >= MAX_SAVED:
        return {"ok": False, "error": f"收藏夹最多 {MAX_SAVED} 条，先删几条再收。"}

    meta = msg.get("meta") or {}
    item = {
        "id": mid,
        "thread_id": thread["id"],
        "question": _squash(msg.get("question") or "", QUESTION_MAX),
        "headline": _squash(msg.get("content") or "", HEADLINE_MAX),
        "codes": [c for c in (meta.get("metric_codes") or []) if isinstance(c, str)][:4],
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "ts": int(msg.get("ts") or 0),
    }

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        people_map = doc.get("people") if isinstance(doc.get("people"), dict) else {}
        prev = people_map.get(key) if isinstance(people_map.get(key), dict) else {}
        saved = [it for it in (prev.get("saved") or []) if isinstance(it, dict) and it.get("id") != mid]
        # 合并写：这人身下还挂着 watch/seen/layout，直接替换会一并抹掉
        _put(people_map, key, [item, *saved])
        return {"people": people_map}

    store.update(STORE_NAME, _apply, {})
    return {"ok": True, "duplicate": False, "item": item, "count": len(current) + 1, "max": MAX_SAVED, "items": list_of(key)}


def remove(pid: str | None, message_id: Any) -> dict[str, Any]:
    """取消收藏。本来就没收过也算成功（重复点叉不该弹错）。"""
    key = people.resolve(pid)
    mid = str(message_id or "").strip()
    if not mid:
        return {"ok": False, "error": "缺少 message_id。"}
    removed = {"n": 0}

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        people_map = doc.get("people") if isinstance(doc.get("people"), dict) else {}
        prev = people_map.get(key) if isinstance(people_map.get(key), dict) else {}
        before = [it for it in (prev.get("saved") or []) if isinstance(it, dict)]
        after = [it for it in before if it.get("id") != mid]
        removed["n"] = len(before) - len(after)
        if before:
            _put(people_map, key, after)
        return {"people": people_map}

    store.update(STORE_NAME, _apply, {})
    return {"ok": True, "removed": removed["n"], "count": len(_items_of(key)), "max": MAX_SAVED, "items": list_of(key)}


def purge_thread(thread_id: str) -> int:
    """删会话时把指向它的收藏一并清掉（所有身份）。

    不这么做的话，收藏夹里会留下点开是空白的条目 —— 而且 `list_of` 只在读的时候
    过滤，存储里的死条目会一直躺着，越攒越多。
    """
    gone = {"n": 0}

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        people_map = doc.get("people") if isinstance(doc.get("people"), dict) else {}
        for key, val in list(people_map.items()):
            if not isinstance(val, dict):
                continue
            before = [it for it in (val.get("saved") or []) if isinstance(it, dict)]
            if not before:
                continue
            after = [it for it in before if it.get("thread_id") != thread_id]
            if len(after) != len(before):
                gone["n"] += len(before) - len(after)
                _put(people_map, key, after)
        return {"people": people_map}

    store.update(STORE_NAME, _apply, {})
    return gone["n"]


def to_meta(pid: str | None) -> dict[str, Any]:
    items = list_of(pid)
    return {"count": len(items), "max": MAX_SAVED, "items": items}
