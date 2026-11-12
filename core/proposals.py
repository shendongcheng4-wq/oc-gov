"""待确认的动作：模型提议 → 人确认 → 才执行。

## 为什么需要这一层

对话层原来只能查。写能力（催办、签收、办结、建会）全在 UI 后面，因为那些接口
要过 `WRITE_POLICY` 的闸和 `authz.can` 的行级判定 —— 而模型发起调用时手上没有
session。

直接把写工具塞给模型是不可接受的：模型看错一条事项就去催办，责任落在谁头上
说不清。所以中间加一层**提议**：

- 模型只能 `propose`：算出要做什么、动哪一条，落成一条待确认记录；
- 人点「确认」，服务端**用这个人的身份**去过原来的闸、走原来的状态机；
- 中间任何一步不成立，什么都不写。

这不是把安全边界磨掉，是把边界从「模型不许碰写」换成「模型只能提议，执行要
人的身份」。前者限制的是能力，后者限制的是责任 —— 后者才是真正要守的那条。

## 几条硬规矩

1. **归属到人**：每条提议记 `person`，确认时必须同一个人。换个身份点不了。
2. **一次性**：`claim()` 在同一个锁里把状态从 pending 改成 used，双击不会执行两遍。
3. **会过期**：默认 30 分钟。昨天的提议今天还在，等于给了一条绕过「再确认一次」的路。
4. **不落业务数据**：这里只存"打算做什么"，真正的写入仍然由调用方（main.py）
   带着 session 去调 `oversight.advance` 等原始入口。
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import store

FILE = "proposals"
VERSION = 1
TTL = 30 * 60          # 一条提议的有效期
KEEP_USED = 60 * 60    # 用完的留一小时，便于回查「刚才确认过什么」


def _now() -> int:
    return int(time.time())


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "items": []}


def _cfg() -> dict[str, Any]:
    raw = store.read(FILE)
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return _empty()
    raw.setdefault("version", VERSION)
    return raw


def _alive(p: dict[str, Any]) -> bool:
    return p.get("status") == "pending" and int(p.get("expires_at") or 0) > _now()


def _prune(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清掉过期的待确认，和用完超过一小时的记录。

    不清的话这个文件会一直长 —— 每问一句「帮我催一下」就多一条，而其中
    绝大多数永远没人点。
    """
    keep = []
    for p in items:
        if p.get("status") == "pending":
            if int(p.get("expires_at") or 0) > _now():
                keep.append(p)
            continue
        if _now() - int(p.get("resolved_at") or p.get("created_at") or 0) < KEEP_USED:
            keep.append(p)
    return keep


def create(
    *,
    kind: str,
    person: str,
    action: str,
    item_id: str,
    title: str,
    text: str = "",
    extra: dict[str, Any] | None = None,
    thread_id: str | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """登记一条待确认动作。`person` 为空时抛 ValueError（没有身份就没有责任人）。

    `extra` 是动作自己的附加参数（退回理由、办结佐证、进展百分比）。放在一个
    dict 里而不是给每个动作开一个字段：确认时原样转交给 `oversight.advance`，
    这里不需要知道每种动作要什么 —— 知道了就得跟着状态机一起改。
    """
    owner = str(person or "").strip()
    if not owner:
        raise ValueError("没有登录身份，不能生成待确认的操作。")
    proposal = {
        "id": f"pr_{secrets.token_hex(6)}",
        # kind 是「哪一类动作」，action 是状态机里的中文动作名。
        # 两个都要：前者决定界面画成什么样，后者直接喂给 oversight.advance。
        "kind": str(kind),
        "action": str(action),
        "person": owner,
        "thread_id": str(thread_id or ""),
        "message_id": str(message_id or ""),
        "item_id": str(item_id),
        "title": str(title or ""),
        "text": str(text or ""),
        "extra": dict(extra or {}),
        "status": "pending",
        "created_at": _now(),
        "expires_at": _now() + TTL,
    }

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        items = _prune(cfg.get("items") or [])
        items.append(proposal)
        cfg["items"] = items
        return cfg

    store.update(FILE, _do, _empty())
    return proposal


def get(proposal_id: str, person: str | None = None) -> dict[str, Any] | None:
    """取一条还活着的提议。`person` 给定时，不属于他的当「不存在」处理。

    和 chat.get 同一个理由：说「这不是你的」等于确认了这个 id 是真的。
    """
    for p in _cfg()["items"]:
        if p.get("id") != proposal_id or not _alive(p):
            continue
        if person is not None and (p.get("person") or "") != str(person).strip():
            return None
        return p
    return None


def claim(proposal_id: str, person: str) -> tuple[dict[str, Any] | None, str]:
    """占用一条提议，返回 (proposal, 失败原因)。

    占用和执行必须分开：先 claim 再执行，双击的第二次会拿到 None，
    而不是再催一遍。失败原因分三种（不存在 / 不是你的 / 过期），是因为
    界面上该说的话不同 —— 都不说，用户只会以为按钮坏了。
    """
    owner = str(person or "").strip()
    if not owner:
        return None, "未登录：请先在右上角登录身份。"
    found: dict[str, Any] = {}
    reason = {"text": ""}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        items = _prune(cfg.get("items") or [])
        for p in items:
            if p.get("id") != proposal_id:
                continue
            if (p.get("person") or "") != owner:
                reason["text"] = "这条操作不是你发起的，不能替你确认。"
                break
            if not _alive(p):
                reason["text"] = "这条操作已经失效或已经被确认过了。"
                break
            p["status"] = "used"
            p["resolved_at"] = _now()
            found.update(p)
            break
        cfg["items"] = items
        return cfg

    store.update(FILE, _do, _empty())
    if found:
        return found, ""
    return None, reason["text"] or "这条操作已经失效或已经被确认过了。"


def cancel(proposal_id: str, person: str) -> bool:
    owner = str(person or "").strip()
    done = {"ok": False}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        items = _prune(cfg.get("items") or [])
        for p in items:
            if p.get("id") != proposal_id or not _alive(p):
                continue
            if (p.get("person") or "") != owner:
                break
            p["status"] = "cancelled"
            p["resolved_at"] = _now()
            done["ok"] = True
            break
        cfg["items"] = items
        return cfg

    store.update(FILE, _do, _empty())
    return done["ok"]
