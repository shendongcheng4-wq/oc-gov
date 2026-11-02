"""会议台账：会议主档 + 议定事项 + 落督办。

数据形状（`data/meetings.json`）：

    {"version": 1, "seq": 3, "meetings": [
        {"id": "MT2026-0001", "title": "...", "held_at": "2026-11-02 09:00",
         "host": "市长", "attendees": ["市发改委", ...], "source": "manual",
         "text": "会议记录原文", "status": "parsed", "items": [
            {"id": "MI-01", "title": "...", "assignee": "...", "deadline": "...",
             "standard": "...", "quote": "...", "verify": {...}, "oversight_id": null}
         ]}
    ]}

三个约束，都是有意的：

1. **原文不可变**。解析后改原文等于换了核对基准，已通过校验的引文会全部失效。
   要改就得重新解析，这里直接拒绝。
2. **已落库的事项不可重新抽取**。督办台账里已经有的事件链不能被一次重新解析抹掉。
3. **落库是显式动作**，不在解析时自动发生。`verify.status == "ok"` 只是"可以落"，
   不是"已经落"——人工勾选这一步不能省。

存储走 `core/store.py`（全量替换 + 文件锁），和其他运行时数据保持一致。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import store
from extract import known_units, verify_item

import oversight as ov

FILE = "meetings"
VERSION = 1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "seq": 0, "meetings": []}


def _doc() -> dict[str, Any]:
    d = store.read(FILE, None)
    if not isinstance(d, dict) or not isinstance(d.get("meetings"), list):
        return _empty()
    d.setdefault("version", VERSION)
    d.setdefault("seq", len(d["meetings"]))
    return d


def _find(doc: dict, mid: str) -> dict | None:
    return next((m for m in doc["meetings"] if m.get("id") == mid), None)


def _counts(m: dict) -> dict[str, int]:
    items = m.get("items") or []
    return {
        "items": len(items),
        "committed": sum(1 for i in items if i.get("oversight_id")),
        "needs_review": sum(1 for i in items if (i.get("verify") or {}).get("status") == "needs_review"),
    }


def brief(m: dict) -> dict[str, Any]:
    c = _counts(m)
    return {
        "id": m["id"],
        "title": m.get("title") or "",
        "held_at": m.get("held_at") or "",
        "host": m.get("host") or "",
        "attendees": m.get("attendees") or [],
        "source": m.get("source") or "manual",
        "status": m.get("status") or "draft",
        "created_at": m.get("created_at"),
        "parsed_at": m.get("parsed_at"),
        "item_count": c["items"],
        "committed_count": c["committed"],
        "needs_review_count": c["needs_review"],
    }


def list_meetings() -> list[dict[str, Any]]:
    doc = _doc()
    # 新会议在前：开会的人关心的是"最近这次会"，不是"第一次会"。
    # created_at 只精确到分钟，同一分钟连建两场会时排序键会相等，而 sorted 是稳定排序
    # —— 相等就保持原顺序，结果「最近一场」反而选到旧的那场。补个 id 作次级键兜住。
    return [
        brief(m)
        for m in sorted(
            doc["meetings"],
            key=lambda x: (x.get("created_at") or "", x.get("id") or ""),
            reverse=True,
        )
    ]


def get(mid: str) -> dict[str, Any] | None:
    m = _find(_doc(), mid)
    return dict(m) if m else None


def create(
    title: str,
    text: str,
    *,
    held_at: str = "",
    host: str = "",
    attendees: list[str] | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    title = (title or "").strip()
    text = (text or "").strip()
    if not title:
        raise ValueError("会议标题不能为空。")
    if not text:
        raise ValueError("会议记录正文不能为空——没有原文就无从核对引文。")

    box: dict[str, Any] = {}

    def _do(doc: dict) -> dict:
        doc.setdefault("meetings", [])
        year = date.today().year
        used = [
            int(m["id"].split("-")[1])
            for m in doc["meetings"]
            if str(m.get("id", "")).startswith(f"MT{year}-") and m["id"].split("-")[1].isdigit()
        ]
        doc["seq"] = (max(used) + 1) if used else 1
        meeting = {
            "id": f"MT{year}-{doc['seq']:04d}",
            "title": title,
            "held_at": (held_at or _now()).strip(),
            "host": (host or "").strip(),
            "attendees": [a.strip() for a in (attendees or []) if str(a).strip()],
            "source": source,
            "text": text,
            "status": "draft",
            "created_at": _now(),
            "parsed_at": None,
            "model_ref": None,
            "warnings": [],
            "items": [],
        }
        doc["meetings"].append(meeting)
        box["meeting"] = meeting
        return doc

    store.update(FILE, _do, default=_empty())
    return dict(box["meeting"])


def delete(mid: str) -> bool:
    box = {"ok": False}

    def _do(doc: dict) -> dict:
        before = len(doc["meetings"])
        doc["meetings"] = [m for m in doc["meetings"] if m.get("id") != mid]
        box["ok"] = len(doc["meetings"]) < before
        return doc

    store.update(FILE, _do, default=_empty())
    return box["ok"]


def save_parse(mid: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """把抽取结果落到会议记录上。items 整体替换。"""
    box: dict[str, Any] = {"ok": False}

    def _do(doc: dict) -> dict:
        m = _find(doc, mid)
        if m is None:
            box["error"] = "meeting_not_found"
            return doc
        if any(i.get("oversight_id") for i in (m.get("items") or [])):
            box["error"] = "已经落过督办，不能重新抽取——督办台账里的事件链会被打乱。"
            return doc
        m["items"] = [
            {
                "id": f"MI-{i:02d}",
                "title": it["title"],
                "assignee": it.get("assignee"),
                "deadline": it.get("deadline"),
                "standard": it.get("standard"),
                "quote": it.get("quote"),
                "confidence": it.get("confidence") or "low",
                "verify": it.get("verify") or {"status": "needs_review", "issues": []},
                "oversight_id": None,
            }
            for i, it in enumerate(parsed.get("items") or [], 1)
        ]
        for it in m["items"]:
            it["manual"] = []
        m["status"] = "parsed"
        m["parsed_at"] = _now()
        m["model_ref"] = parsed.get("model_ref")
        m["warnings"] = parsed.get("warnings") or []
        box["ok"] = True
        box["meeting"] = m
        return doc

    store.update(FILE, _do, default=_empty())
    return box


ALLOWED_PATCH = ("title", "assignee", "deadline", "standard")


def update_item(mid: str, iid: str, patch: dict[str, Any]) -> dict[str, Any]:
    """人工修正一条事项，改完**原地重新校验**。

    只允许改 title / assignee / deadline / standard，不允许改 quote —— 引文是
    核对基准，人能改引文就等于人能把校验关掉。要换引文就重新解析。
    """
    box: dict[str, Any] = {"ok": False}

    def _do(doc: dict) -> dict:
        m = _find(doc, mid)
        if m is None:
            box["error"] = "meeting_not_found"
            return doc
        item = next((i for i in (m.get("items") or []) if i.get("id") == iid), None)
        if item is None:
            box["error"] = "item_not_found"
            return doc
        if item.get("oversight_id"):
            box["error"] = "已落督办的事项不能再改，请到督办台账里操作。"
            return doc
        for key in ALLOWED_PATCH:
            if key in patch:
                value = patch[key]
                item[key] = (str(value).strip() or None) if key in ("assignee", "deadline") else str(value or "").strip()
                if key not in item.setdefault("manual", []):
                    item["manual"].append(key)
        item["verify"] = verify_item(
            item,
            m.get("text") or "",
            units=known_units(),
            manual=set(item.get("manual") or []),
        )
        box["ok"] = True
        box["item"] = item
        return doc

    store.update(FILE, _do, default=_empty())
    return box


def commit(mid: str, item_ids: list[str], actor: str = "市政府办") -> dict[str, Any]:
    """把选中的事项写进督办台账。

    先全量校验再动手：任何一条不满足条件就整体拒绝，避免"写进去一半"——
    督办台账是有人真的要去办的东西，半截写入比不写更难收拾。
    """
    doc = _doc()
    m = _find(doc, mid)
    if m is None:
        return {"ok": False, "error": "meeting_not_found"}

    wanted = [str(i) for i in (item_ids or [])]
    if not wanted:
        return {"ok": False, "error": "没有选择要落库的事项。"}

    by_id = {i["id"]: i for i in (m.get("items") or [])}
    chosen: list[dict] = []
    problems: list[dict[str, str]] = []
    already: list[str] = []

    for iid in wanted:
        item = by_id.get(iid)
        if item is None:
            problems.append({"id": iid, "error": "这条事项不存在。"})
            continue
        if item.get("oversight_id"):
            already.append(item["oversight_id"])
            continue
        status = (item.get("verify") or {}).get("status")
        if status != "ok":
            reasons = "；".join(i.get("detail", "") for i in (item.get("verify") or {}).get("issues") or [])
            problems.append({"id": iid, "error": reasons or "未通过校验。"})
            continue
        chosen.append(item)

    if problems:
        return {"ok": False, "error": "有事项未通过校验，一条都没写入。", "problems": problems}
    if not chosen:
        return {"ok": True, "created": [], "skipped": already, "note": "选中的事项都已落过督办。"}

    created: list[dict[str, str]] = []
    for item in chosen:
        res = ov.create_item(
            title=item["title"],
            assignee=item["assignee"],
            deadline=item["deadline"],
            standard=item["standard"],
            source=f"会议：{m.get('title') or m['id']}",
            source_ref=m["id"],
            priority="一般",
            actor=actor,
        )
        if not res.get("ok"):
            # 走到这里说明校验和 create_item 的规则不同步，属于代码 bug，
            # 但已经写进去的不能回滚（督办台账没有事务），所以如实报告。
            return {
                "ok": False,
                "error": f"写督办时被拒绝：{res.get('error')}",
                "created": created,
                "problems": [{"id": item["id"], "error": str(res.get("error"))}],
            }
        created.append({"item_id": item["id"], "oversight_id": res["item_id"], "title": item["title"]})

    # 回填督办号
    lookup = {c["item_id"]: c["oversight_id"] for c in created}

    def _do(d: dict) -> dict:
        mm = _find(d, mid)
        if mm:
            for it in mm.get("items") or []:
                if it["id"] in lookup:
                    it["oversight_id"] = lookup[it["id"]]
            if all(i.get("oversight_id") for i in mm.get("items") or []):
                mm["status"] = "committed"
        return d

    store.update(FILE, _do, default=_empty())

    m = _find(_doc(), mid) or {}
    return {
        "ok": True,
        "created": created,
        "skipped": already,
        "status": m.get("status"),
        "counts": _counts(m),
    }


def stats() -> dict[str, int]:
    doc = _doc()
    meetings = doc["meetings"]
    return {
        "meetings": len(meetings),
        "parsed": sum(1 for m in meetings if m.get("status") in ("parsed", "committed")),
        "items": sum(len(m.get("items") or []) for m in meetings),
        "committed": sum(1 for m in meetings for i in (m.get("items") or []) if i.get("oversight_id")),
    }
