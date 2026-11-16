"""会话材料的正文仓库：一份材料上传一次，之后按 id 取。

## 为什么需要它

以前材料是「本轮可见」的：前端每轮把 `text` 塞进请求体，后端只喂这一轮的
planner 和模型，落库只留文件名和字数。**材料一过这一轮就没了。**

后果不是「体验差一点」，是答不出来：用户传了一份月报问「三季度累计多少」，
系统答完；接着问「表里第 7 行那个数怎么算的」，这一轮请求体里没有材料，
模型手里也没有，只能拒答或者回去查指标库 —— 而用户明明刚刚才把文件给它。

## 边界

- **正文不进 chats.json**：一份 1.2 万字的材料如果被每轮上下文重复带走，
  几十轮之后光历史就能把窗口吃干净。这里只存索引（名字、字数、sha1），
  正文按 id 单独落盘，用到才读。
- **按 owner 隔离**：材料常含未公开数据，只能被上传者本人的会话引用。
  取别人的 id 返回 None —— 和 chat 一样当作「不存在」，不回 403，
  否则响应本身就是一条信息（「这个 id 是存在的」）。
- **同文去重**：前端在「重新选同一个文件」和「刷新后重发」两种情况下都会把
  同一段文本再送一遍。按 (owner, sha1) 归一到同一个 id，不然一轮一份副本。
  去重键用正文而不是文件名：同一个 `月报.xlsx` 改了两个数再传，是两份材料。

## 存量

单机单进程、材料量在每人几十份的量级，所以索引还是一个 JSON（能直接用编辑器
打开、坏了能手工修）。正文单文件的好处是：删索引不会连带删正文，反过来也一样，
最坏情况是「索引里有一条、正文没了」——`get` 会发现并当作不存在。
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

import store

FILE = "materials"
VERSION = 1
BLOB_DIR = store.DATA_DIR / "materials"

# 单人保留上限与过期时间。材料是「上传那几天有用」，不是永久档案 ——
# 长期要留的东西走知识库（rag.py，有向量检索、有归属、有清单页）。
MAX_PER_OWNER = 80
TTL = 120 * 86400


def _now() -> int:
    return int(time.time())


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "items": []}


def _blob_path(mid: str) -> Path:
    return BLOB_DIR / f"{mid}.txt"


def _new_id() -> str:
    return f"mat_{secrets.token_hex(6)}"


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def _items() -> list[dict[str, Any]]:
    cfg = store.read(FILE, _empty()) or _empty()
    items = cfg.get("items")
    return items if isinstance(items, list) else []


def _write_blob(mid: str, text: str) -> None:
    """正文落盘。先写 tmp 再 rename：中途崩掉留下的是完整文件或没有文件，
    不会是半份材料 —— 半份比没有更坏，它会被当成「材料里没这个数」。"""
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    dest = _blob_path(mid)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)


def _gc(items: list[dict[str, Any]], owner: str) -> list[dict[str, Any]]:
    """丢掉这个身份下过期的、以及超出上限的（按最后引用时间从旧到新保留）。"""
    now = _now()
    mine = [x for x in items if (x.get("owner") or "") == owner]
    others = [x for x in items if (x.get("owner") or "") != owner]
    dead = [x for x in mine if now - int(x.get("used_at") or 0) > TTL]
    alive = [x for x in mine if x not in dead]
    if len(alive) > MAX_PER_OWNER:
        alive.sort(key=lambda x: int(x.get("used_at") or 0), reverse=True)
        dead += alive[MAX_PER_OWNER:]
        alive = alive[:MAX_PER_OWNER]
    for x in dead:
        try:
            _blob_path(str(x.get("id"))).unlink(missing_ok=True)
        except OSError:
            pass
    return others + alive


def save(
    owner: str | None,
    name: str,
    text: str,
    kind: str = "",
    truncated: bool = False,
) -> dict[str, Any]:
    """存一份材料，返回索引项（含 id）。同一 owner 下正文相同则复用原 id。"""
    who = str(owner or "")
    body = str(text or "")
    sha = _sha(body)
    label = str(name or "未命名").strip()[:120] or "未命名"
    now = _now()

    found: dict[str, Any] = {}

    def _do(cfg: dict[str, Any] | None) -> dict[str, Any]:
        cfg = cfg if isinstance(cfg, dict) else _empty()
        items = cfg.get("items")
        items = items if isinstance(items, list) else []
        for x in items:
            if (x.get("owner") or "") == who and x.get("sha") == sha:
                x["used_at"] = now
                # 名字可能改过（「月报(1).xlsx」重传），以最新一次为准
                x["name"] = label
                found.update(x)
                cfg["items"] = items
                return cfg
        mid = _new_id()
        rec = {
            "id": mid,
            "owner": who,
            "name": label,
            "chars": len(body),
            "truncated": bool(truncated),
            "kind": str(kind or ""),
            "sha": sha,
            "created_at": now,
            "used_at": now,
        }
        items.append(rec)
        cfg["items"] = _gc(items, who)
        found.update(rec)
        return cfg

    store.update(FILE, _do, _empty())
    if found.get("id"):
        _write_blob(str(found["id"]), body)
    return dict(found)


def get(mid: str, owner: str | None = None) -> dict[str, Any] | None:
    """取一份材料的正文。owner 给定时必须匹配，不匹配与不存在同样处理。"""
    want = str(mid or "").strip()
    if not want:
        return None
    who = None if owner is None else str(owner or "")
    rec = next((x for x in _items() if x.get("id") == want), None)
    if rec is None:
        return None
    if who is not None and (rec.get("owner") or "") != who:
        return None
    try:
        text = _blob_path(want).read_text(encoding="utf-8")
    except OSError:
        # 索引在、正文没了（手工清过目录）。当作不存在，让调用方丢掉这条引用。
        return None
    return {**rec, "text": text}


def get_many(ids: list[str] | None, owner: str | None = None) -> list[dict[str, Any]]:
    """按 id 顺序取，取不到的跳过。顺序有意义：越靠前越先占上下文预算。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mid in ids or []:
        key = str(mid or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        got = get(key, owner=owner)
        if got:
            out.append(got)
    return out


def touch(ids: list[str] | None, owner: str | None = None) -> None:
    """把这几份材料的「最后引用时间」往前推，GC 才不会把它们当过期清掉。"""
    keys = {str(x) for x in (ids or []) if x}
    if not keys:
        return
    who = None if owner is None else str(owner or "")
    now = _now()

    def _do(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
        cfg = cfg if isinstance(cfg, dict) else _empty()
        for x in cfg.get("items") or []:
            if x.get("id") in keys and (who is None or (x.get("owner") or "") == who):
                x["used_at"] = now
        return cfg

    store.update(FILE, _do, _empty())


def forget(mid: str, owner: str | None = None) -> bool:
    """删掉一份材料（正文 + 索引）。会话上的 id 由 chat 那边一并摘掉。"""
    want = str(mid or "").strip()
    if not want:
        return False
    who = None if owner is None else str(owner or "")
    hit = {"ok": False}

    def _do(cfg: dict[str, Any] | None) -> dict[str, Any]:
        cfg = cfg if isinstance(cfg, dict) else _empty()
        items = cfg.get("items") or []
        keep = []
        for x in items:
            if x.get("id") == want and (who is None or (x.get("owner") or "") == who):
                hit["ok"] = True
                continue
            keep.append(x)
        cfg["items"] = keep
        return cfg

    store.update(FILE, _do, _empty())
    if hit["ok"]:
        try:
            _blob_path(want).unlink(missing_ok=True)
        except OSError:
            pass
    return hit["ok"]


def describe(ids: list[str] | None, owner: str | None = None) -> list[dict[str, Any]]:
    """只要元数据（不含正文）。给会话回显「本会话挂着哪几份材料」用。"""
    keys = [str(x) for x in (ids or []) if x]
    want = set(keys)
    out: list[dict[str, Any]] = []
    for x in _items():
        if x.get("id") not in want:
            continue
        if owner is not None and (x.get("owner") or "") != str(owner or ""):
            continue
        out.append(
            {k: v for k, v in x.items() if k not in ("sha", "owner")}
        )
    order = {k: i for i, k in enumerate(keys)}
    out.sort(key=lambda x: order.get(str(x.get("id")), 1 << 30))
    return out


def prune() -> int:
    """清掉所有人的过期材料，返回删掉几条。启动时跑一次就够。"""
    before = _items()
    if not before:
        return 0
    owners = {(x.get("owner") or "") for x in before}
    after = list(before)
    for who in owners:
        after = _gc(after, who)
    if len(after) == len(before):
        return 0
    store.write(FILE, {"version": VERSION, "items": after})
    return len(before) - len(after)


def clear_all() -> None:
    """测试用：把索引和正文目录一起清掉。"""
    store.write(FILE, _empty())
    if BLOB_DIR.exists():
        shutil.rmtree(BLOB_DIR, ignore_errors=True)
