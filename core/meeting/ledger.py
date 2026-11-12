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
        "kind": m.get("kind") or "record",
        "held_at": m.get("held_at") or "",
        "host": m.get("host") or "",
        "attendees": m.get("attendees") or [],
        "place": m.get("place") or "",
        "topics": m.get("topics") or [],
        "source": m.get("source") or "manual",
        "status": m.get("status") or "draft",
        # 线上会议（腾讯会议）的绑定信息。没有就是 None，不编一个空对象 ——
        # 界面靠「有没有这个键」区分「没接」和「接了但没建成功」。
        "online": m.get("online") or None,
        # 原文从哪来：manual（人写的）/ wemeet（转写拉下来的）。
        # 这个字段决定「能不能被下一次转写覆盖」——见 set_transcript()。
        "text_source": m.get("text_source") or "manual",
        # 哪一条问答产出这场会（幂等键，也用来在会话回放里标「已建会」）
        "from_message": m.get("from_message") or "",
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
    """取一条完整会议。

    `online` / `text_source` 是后加的字段，老数据里没有。这里补默认值而不是让
    调用方各自 `m.get(...) or ...`：接口返回的形状要稳定，前端才敢直接读。
    """
    m = _find(_doc(), mid)
    if m is None:
        return None
    out = dict(m)
    out.setdefault("online", None)
    out.setdefault("text_source", "manual")
    return out


def create(
    title: str,
    text: str,
    *,
    held_at: str = "",
    host: str = "",
    attendees: list[str] | None = None,
    source: str = "manual",
    kind: str = "record",
    place: str = "",
    topics: list[str] | None = None,
    from_message: str = "",
    planned_by: str = "",
) -> dict[str, Any]:
    """建一场会。

    两种 kind，语义完全不同，不能混：

    - `record`（会后记录）：正文是纪要原文，必填。没有原文就无从核对引文，
      后面「解析议定事项 → 落督办」整条链都挂在原文上。
    - `plan`（待召开的会）：正文允许为空（还没开，哪来的纪要），但**必须有时间**
      —— 没有时间的会议进不了日程，也提醒不了谁。

    把预约的会也放这个文件里，是刻意的：日程、会前提醒读的都是 `list_meetings()`
    的 `held_at`，只要进同一张表，那两处一行都不用改就能用。另起一个实体，
    就得让日程和提醒各吃两套数据源，迟早对不上。
    """
    title = (title or "").strip()
    text = (text or "").strip()
    if kind not in ("record", "plan"):
        raise ValueError(f"未知的会议类型：{kind}")
    if not title:
        raise ValueError("会议标题不能为空。")
    if kind == "record":
        if not text:
            raise ValueError("会议记录正文不能为空——没有原文就无从核对引文。")
    else:
        if not (held_at or "").strip():
            raise ValueError("预约会议必须写明时间——没有时间进不了日程，也没法提醒。")

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
            "kind": kind,
            "held_at": (held_at or _now()).strip(),
            "host": (host or "").strip(),
            "attendees": [a.strip() for a in (attendees or []) if str(a).strip()],
            "place": (place or "").strip(),
            "topics": [t.strip() for t in (topics or []) if str(t).strip()],
            "source": source,
            "text": text,
            # 原文从哪来（人写的 / 腾讯会议转写）。set_transcript() 靠它决定能不能覆盖
            "text_source": "manual",
            # 线上会议绑定信息，没接腾讯会议时是 None
            "online": None,
            # 待召开的会不该有「待解析」这个状态：还没开，没有东西可解析
            "status": "planned" if kind == "plan" else "draft",
            "created_at": _now(),
            "parsed_at": None,
            "model_ref": None,
            "warnings": [],
            "items": [],
            # 幂等键：同一条问答里的同一张草稿卡，只能变成一场会
            "from_message": from_message,
            "planned_by": planned_by,
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
        if (m.get("kind") or "record") == "plan":
            # 待召开的会没有纪要原文 —— 这时候「解析」只能解析出模型编的东西
            box["error"] = "这是一场还没召开的会，没有原文可解析。开完把纪要粘进正文再来解析。"
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


# --------------------------------------------------------------------------
# 腾讯会议：绑定会议号 / 把转写落成原文
# --------------------------------------------------------------------------
def set_online(mid: str, online: dict[str, Any]) -> dict[str, Any]:
    """把腾讯会议的会议号与入会链接挂到这场会上。

    可以重复调用：重新预约就覆盖上一次（旧会议号在腾讯会议那边仍然有效，我们
    这边只记最新一次 —— 记两场会反而让人不知道该进哪一个）。
    """
    box: dict[str, Any] = {"ok": False}

    def _do(doc: dict) -> dict:
        m = _find(doc, mid)
        if m is None:
            box["error"] = "meeting_not_found"
            return doc
        old = m.get("online") or {}
        m["online"] = {
            "platform": "wemeet",
            "meeting_id": str(online.get("meeting_id") or ""),
            "meeting_code": str(online.get("meeting_code") or ""),
            "join_url": str(online.get("join_url") or ""),
            "password": str(online.get("password") or ""),
            "subject": str(online.get("subject") or m.get("title") or ""),
            "start_time": str(online.get("start_time") or ""),
            "end_time": str(online.get("end_time") or ""),
            "bound_at": _now(),
            # 重新预约时，上一场的录制信息就作废了
            "record_file_id": old.get("record_file_id") if old.get("meeting_id") == str(online.get("meeting_id") or "") else "",
            "transcript_at": old.get("transcript_at") if old.get("meeting_id") == str(online.get("meeting_id") or "") else None,
        }
        box["ok"] = True
        box["meeting"] = m
        return doc

    store.update(FILE, _do, default=_empty())
    return box


def set_transcript(mid: str, text: str, *, record_file_id: str = "",
                   truncated: bool = False) -> dict[str, Any]:
    """把腾讯会议的录制转写落成会议原文。

    「原文不可变」这条规则在这里照样有效，只开一个**窄口子**：如果原文本身就是
    转写拉下来的（`text_source == "wemeet"`），允许重拉覆盖 —— 转写可能分页、
    可能第一次没生成完。但：

    - 已经有人工粘贴的原文（`text_source != "wemeet"`）→ 拒绝。人和接口各写一版，
      下一轮解析到底以哪版为准就说不清了。
    - 已经有事项落了督办 → 拒绝。原文一换，那些引文就悬空了。
    - 已解析出的事项可以重来（它们是同一份转写的产物），但要如实告诉调用方
      「重置了几条」，别让人以为还在。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "空转写不能当原文。"}
    box: dict[str, Any] = {"ok": False, "replaced_items": 0}

    def _do(doc: dict) -> dict:
        m = _find(doc, mid)
        if m is None:
            box["error"] = "meeting_not_found"
            return doc
        committed = [i for i in (m.get("items") or []) if i.get("oversight_id")]
        if committed:
            box["error"] = (
                f"这场会已经有 {len(committed)} 条事项落了督办，换原文会让那些引文悬空。"
                "要重来请新建一场会议。"
            )
            return doc
        existing = (m.get("text") or "").strip()
        if existing and (m.get("text_source") or "manual") != "wemeet":
            box["error"] = (
                "这场会已经有手工填写的原文，不能被转写覆盖。"
                "要改用转写，请新建一场会再拉。"
            )
            return doc

        dropped = len(m.get("items") or [])
        if dropped:
            m["items"] = []
            box["replaced_items"] = dropped
        m["text"] = text
        m["text_source"] = "wemeet"
        # 开了会、有了原文，它就不再是「待召开」了
        m["kind"] = "record"
        m["status"] = "draft"
        m["parsed_at"] = None
        online = dict(m.get("online") or {})
        online.update({
            "platform": "wemeet",
            "record_file_id": str(record_file_id or ""),
            "transcript_at": _now(),
            "transcript_chars": len(text),
            "transcript_truncated": bool(truncated),
        })
        m["online"] = online
        # 旧提醒（比如上一次截断）不能留着让人以为这次也截断了
        m["warnings"] = [w for w in (m.get("warnings") or []) if "截断" not in str(w)]
        if truncated:
            m["warnings"].append(
                f"转写过长，落库时截到 {len(text)} 字；解析只读前 12000 字，完整内容请回腾讯会议查。"
            )
        box["ok"] = True
        box["meeting"] = m
        return doc

    store.update(FILE, _do, default=_empty())
    return box


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
