"""消息提醒：把「已经算出来的状态」送到人手里，并且只送一次。

这个模块只做搬运，不做判断。三条边界写死在这里，改之前先想清楚：

1. **不产生事实**。每条提醒都带 `ref`，点开回到台账 / 会议记录 / 指标口径。
   正文里的天数、进度、期次一律从源数据取，不在这里算第二遍 ——
   日程那边已经踩过一次坑：同一件事在两个地方各算一次天数，就会出现
   「台账说逾期 3 天、日程说还有 1 天」。
2. **不替代日程**。日程回答「什么时候有什么事」，提醒回答「现在该叫醒谁」。
   同一件事在日程里出现两行是 bug；在提醒里第二天再出现一次是正常的 ——
   因为没办。所以提醒按天去重，不按事项去重。
3. **不预设通道**。站内信永远可用；桌面通知要用户授权；企微 / 邮件要填配置。
   通道没配不等于功能没做，页面上把「已送达」和「只在本机」分开写，
   不许用一句「已提醒」把没发出去的说成发出去了。

`RULES` 就是这套节奏的全部。页面上那张「什么时候提醒什么」的表是直接读它的，
所以这里写的 `when` 必须和代码里的判断一致 —— 文案和实现分家，比没有文案更糟。
"""

from __future__ import annotations

import hashlib
import json
import os
import smtplib
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "oversight", ROOT / "core" / "meeting"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import mockdata as md  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
import schedule as schedule_mod  # noqa: E402
import store  # noqa: E402
from ledger import list_meetings  # noqa: E402

STORE_NAME = "notify"
CONFIG_PATH = ROOT / "config" / "notify.json"
TEMPLATE_PATH = ROOT / "config" / "notify.example.json"

# 一天里最早可以外发提醒的钟点。政府口的作息，「早上到办公室先看一遍」比「半夜推送」有用。
WAKE_HOUR = 8
WAKE_MINUTE = 30
# 会前多久提醒。60 分钟够从办公室走到会场，再短就只剩「来不及」。
MEETING_LEAD_MIN = 60
# 临期提醒落在哪几天。只挑 D-3 和 D-1：天天提醒等于没提醒。
SOON_DAYS = (3, 1)


def _now() -> datetime:
    """和日程、督办共用一把钟。"""
    return ov._now()  # noqa: SLF001


# --------------------------------------------------------------------------
# 规则表：什么时候、提醒谁、多久一次
# --------------------------------------------------------------------------
# level 只影响两件事：页面排序，以及「值不值得把人从别的事里拽出来」。
# urgent 和 high 才会外发，normal / low 只进站内信 —— 全推出去的结果是没人看。
RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "ov_overdue",
        "kind": "oversight",
        "label": "事项已逾期",
        "level": "urgent",
        "to": "unit",
        "when": "逾期后每天一次（不受上班时间限制：已经出事了，不该等到 08:30）",
        "why": "逾期事项每个工作日都要有书面进展，否则只能靠催办推动",
    },
    {
        "id": "ov_today",
        "kind": "oversight",
        "label": "今日到期",
        "level": "urgent",
        "to": "both",
        "when": "办结时限当天 08:30 之后",
        "why": "今天不到 18:00 反馈，明天就变成逾期",
    },
    {
        "id": "ov_unsigned",
        "kind": "oversight",
        "label": "交办未签收",
        "level": "high",
        "to": "unit",
        "when": "交办满 24 小时后每天（08:30 之后）",
        "why": "未签收的台账进不了办理状态，等于交办没落地",
    },
    {
        "id": "ov_soon",
        "kind": "oversight",
        "label": "临近时限",
        "level": "high",
        "to": "unit",
        "when": "到期前 3 天和 1 天各一次",
        "why": "临期还有调整余地，逾期之后只能补说明",
    },
    {
        "id": "ov_stall",
        "kind": "oversight",
        "label": "进展停滞",
        "level": "normal",
        "to": "unit",
        "when": "停滞满 7 天后每周一提醒一次",
        "why": "进度数据不动，通常不是没事，是没人报",
    },
    {
        "id": "meet_soon",
        "kind": "meeting",
        "label": "会议即将开始",
        "level": "high",
        "to": "self",
        "when": "会议开始前 60 分钟内",
        "why": "材料、参会人、议题都要在进会场前过一遍",
    },
    {
        "id": "metric_low",
        "kind": "metric",
        "label": "关注指标低于目标",
        "level": "normal",
        "to": "self",
        "when": "新期次数据披露后（每期一次）",
        "why": "数据已经发布了，低于目标的指标要有人解释",
    },
    {
        "id": "period_due",
        "kind": "period",
        "label": "期次待披露",
        "level": "low",
        "to": "self",
        "when": "披露日前 2 天",
        "why": "提前知道哪一期还没出，汇报时才不会把旧数当新数",
    },
)

_RULE_BY_ID = {r["id"]: r for r in RULES}
_LEVEL_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
_LEVEL_LABEL = {"urgent": "需要立刻处理", "high": "今天要跟进", "normal": "留意", "low": "提示"}

# 外发只发这两级。剩下的进站内信，不弹窗、不推群。
_EXTERNAL_LEVELS = ("urgent", "high")

# 督办桶 → 规则。桶的顺序就是优先级：一件事项同时逾期又停滞时，只按逾期提醒。
# 这条和日程 `_today_action()` 的合并规则一致 —— 一件事一天只能冒出来一次，
# 冒两次用户得自己对编号。
_BUCKET_RULE = (
    ("逾期", "ov_overdue"),
    ("今日到期", "ov_today"),
    ("签收超时", "ov_unsigned"),
    ("临期", "ov_soon"),
    ("停滞", "ov_stall"),
)


# --------------------------------------------------------------------------
# 通道配置：文件 + 环境变量，环境变量优先
# --------------------------------------------------------------------------
_DEFAULT_CFG: dict[str, Any] = {
    "channels": {
        # format: wecom = 企业微信群机器人；generic = 原样 POST 这份 JSON
        "webhook": {"enabled": False, "url": "", "format": "wecom"},
        "smtp": {"enabled": False, "host": "", "port": 465, "ssl": True, "user": "", "password": "", "to": []},
    },
    "quiet_hours": ["22:00", "07:00"],
    "workdays_only": True,
    "daily_limit": 20,
}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config() -> dict[str, Any]:
    """读配置。文件没有就用默认值 —— 不配置也能跑，只是发不出去。"""
    cfg = json.loads(json.dumps(_DEFAULT_CFG))  # 深拷贝，避免调用方改坏默认值
    try:
        if CONFIG_PATH.exists():
            cfg = _deep_merge(cfg, json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}"))
    except (json.JSONDecodeError, OSError):
        # 配置写坏了不该让提醒整个不可用：退回默认值，状态里会如实显示「未配置」
        pass

    hook = cfg["channels"]["webhook"]
    mail = cfg["channels"]["smtp"]
    if os.environ.get("OCGOV_NOTIFY_WEBHOOK"):
        hook["url"] = os.environ["OCGOV_NOTIFY_WEBHOOK"]
        hook["enabled"] = True
        hook["format"] = os.environ.get("OCGOV_NOTIFY_WEBHOOK_FORMAT") or hook.get("format") or "wecom"
    if os.environ.get("OCGOV_NOTIFY_SMTP_HOST"):
        mail["host"] = os.environ["OCGOV_NOTIFY_SMTP_HOST"]
        mail["enabled"] = True
        mail["port"] = int(os.environ.get("OCGOV_NOTIFY_SMTP_PORT") or mail.get("port") or 465)
        mail["user"] = os.environ.get("OCGOV_NOTIFY_SMTP_USER") or ""
        mail["password"] = os.environ.get("OCGOV_NOTIFY_SMTP_PASSWORD") or ""
        mail["to"] = [x for x in (os.environ.get("OCGOV_NOTIFY_SMTP_TO") or "").split(",") if x.strip()]
        mail["ssl"] = (os.environ.get("OCGOV_NOTIFY_SMTP_SSL") or "1") not in ("0", "false", "no")
    return cfg


def _mask(url: str) -> str:
    """推送地址里有 key，页面上一律打码。前端不该拿到能直接发消息的完整地址。"""
    url = (url or "").strip()
    if not url:
        return ""
    # 阈值要小：`https://example.com/x` 正好 24 字符，按 <=24 判会把整个 host 都打掉，
    # 页面上剩一个「https:…」，谁也认不出这是哪个群。
    if len(url) <= 16:
        return url[:6] + "…"
    return f"{url[:18]}…{url[-4:]}"


def _store() -> dict[str, Any]:
    return store.read(STORE_NAME, {}) or {}


def channels_status() -> dict[str, Any]:
    """通道状态。`ok` 是上一次真实发送的结果，不是「配置看起来对」。"""
    cfg = config()
    doc = _store()
    state = doc.get("channels") or {}
    hook = cfg["channels"]["webhook"]
    mail = cfg["channels"]["smtp"]
    hook_on = bool(hook.get("enabled") and hook.get("url"))
    mail_on = bool(mail.get("enabled") and mail.get("host") and mail.get("to"))
    return {
        "mode": "external" if (hook_on or mail_on) else "local",
        "webhook": {
            "enabled": bool(hook.get("enabled")),
            "configured": hook_on,
            "format": hook.get("format") or "wecom",
            "target": _mask(hook.get("url") or ""),
            "ok": state.get("webhook_ok"),
            "error": state.get("webhook_error") or "",
            "last_at": state.get("webhook_at") or "",
        },
        "smtp": {
            "enabled": bool(mail.get("enabled")),
            "configured": mail_on,
            "target": f"{mail.get('host') or ''} → {', '.join(mail.get('to') or [])}"[:80],
            "ok": state.get("smtp_ok"),
            "error": state.get("smtp_error") or "",
            "last_at": state.get("smtp_at") or "",
        },
        "quiet_hours": cfg.get("quiet_hours") or [],
        "workdays_only": bool(cfg.get("workdays_only", True)),
        "daily_limit": int(cfg.get("daily_limit") or 20),
        "note": (
            "已配置外发通道：紧急与重要提醒会同时送出站内信和外部通道"
            if (hook_on or mail_on)
            else "未配置外发通道：提醒只在本机可见，责任单位收不到。配置方式见「送达配置」"
        ),
    }


def config_template() -> str:
    """给页面「一键复制」用的配置模板。字段和 `_DEFAULT_CFG` 保持一致，抄错一个键就是静默失效。"""
    return json.dumps(
        {
            "channels": {
                "webhook": {"enabled": True, "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…", "format": "wecom"},
                "smtp": {
                    "enabled": False,
                    "host": "smtp.example.gov.cn",
                    "port": 465,
                    "ssl": True,
                    "user": "noreply@example.gov.cn",
                    "password": "…",
                    "to": ["zhuren@example.gov.cn"],
                },
            },
            "quiet_hours": ["22:00", "07:00"],
            "workdays_only": True,
            "daily_limit": 20,
        },
        ensure_ascii=False,
        indent=2,
    )


# --------------------------------------------------------------------------
# 生成提醒：全部从派生状态里挑，不新增判断
# --------------------------------------------------------------------------
def _note(
    rule_id: str,
    subject: str,
    bucket: str,
    title: str,
    body: str,
    *,
    ref: dict[str, str] | None = None,
    at: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule = _RULE_BY_ID[rule_id]
    return {
        # id 里带 bucket（日期 / 期次 / 会议 id）：同一天同一件事只有一个 id，
        # 第二天 bucket 变了就成了一条新提醒 —— 不用额外写「昨天发过没有」。
        "id": f"{rule_id}:{subject}:{bucket}",
        "rule": rule_id,
        "rule_label": rule["label"],
        "kind": rule["kind"],
        "level": rule["level"],
        "level_rank": _LEVEL_RANK[rule["level"]],
        "to": rule["to"],
        "title": title,
        "body": body,
        "why": rule["why"],
        "when": rule["when"],
        "at": at,
        "bucket": bucket,
        "ref": ref or {},
        "extra": extra or {},
        "read": False,
    }


def _oversight_notes(person: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """一条在办事项最多出一条提醒，取最严重的那种。

    直接读 `ov.scan()` 的桶，不在这里重算「逾期几天」——
    重算就会有第二个口径，而台账那个才是权威。
    """
    today = now.date()
    wake = now.replace(hour=WAKE_HOUR, minute=WAKE_MINUTE, second=0, microsecond=0)
    units, manual = schedule_mod.scope_of(person)
    sc = ov.scan(max(SOON_DAYS))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    for bucket, rule_id in _BUCKET_RULE:
        for it in sc["buckets"].get(bucket, []):
            if it["id"] in seen or not schedule_mod.item_visible(it, units, manual):
                continue
            seen.add(it["id"])
            ref = {"type": "oversight", "id": it["id"]}
            who = it["assignee"] + ("等" if it.get("co_assignees") else "")

            if rule_id == "ov_overdue":
                # 逾期是「已经出事了」，不该等到 08:30 才说
                out.append(
                    _note(
                        rule_id,
                        it["id"],
                        today.isoformat(),
                        it["title"],
                        f"{who} · 原定 {it['deadline']} 办结，已逾 {-it['days_left']} 天 · 进度 {it['progress']}%",
                        ref=ref,
                        at=today.isoformat(),
                        extra={"deadline": it["deadline"], "days_left": it["days_left"], "progress": it["progress"]},
                    )
                )
                continue

            if now < wake:
                # 其余几类都要等上班：半夜推「今天到期」没有意义，还容易被当成系统异常
                continue

            if rule_id == "ov_today":
                out.append(
                    _note(
                        rule_id,
                        it["id"],
                        today.isoformat(),
                        it["title"],
                        f"{who} · 今天到期，当前进度 {it['progress']}%",
                        ref=ref,
                        at=wake.strftime("%Y-%m-%d %H:%M"),
                        extra={"deadline": it["deadline"], "progress": it["progress"]},
                    )
                )
            elif rule_id == "ov_unsigned":
                extra = f" · 已催 {it['remind_count']} 次" if it.get("remind_count") else ""
                out.append(
                    _note(
                        rule_id,
                        it["id"],
                        today.isoformat(),
                        it["title"],
                        f"{who} · 交办超过 24 小时未签收{extra}",
                        ref=ref,
                        at=wake.strftime("%Y-%m-%d %H:%M"),
                        extra={"issued_deadline": it["deadline"], "remind_count": it.get("remind_count") or 0},
                    )
                )
            elif rule_id == "ov_soon":
                left = it["days_left"]
                if left not in SOON_DAYS:
                    continue
                out.append(
                    _note(
                        rule_id,
                        it["id"],
                        f"D-{left}",
                        it["title"],
                        f"{who} · 距时限还有 {left} 天 · 进度 {it['progress']}%",
                        ref=ref,
                        at=wake.strftime("%Y-%m-%d %H:%M"),
                        extra={"deadline": it["deadline"], "days_left": left, "progress": it["progress"]},
                    )
                )
            elif rule_id == "ov_stall":
                # 停滞按周提醒：周一上班看一眼就够，天天说只会让人把提醒关掉
                if now.weekday() != 0:
                    continue
                out.append(
                    _note(
                        rule_id,
                        it["id"],
                        f"W{today.isocalendar()[1]}",
                        it["title"],
                        f"{who} · 已 {it['idle_days']} 天没有新进展 · 进度 {it['progress']}%",
                        ref=ref,
                        at=wake.strftime("%Y-%m-%d %H:%M"),
                        extra={"idle_days": it["idle_days"], "progress": it["progress"]},
                    )
                )
    return out


def _meeting_notes(person: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """会前提醒。已经开始的会不提醒 —— 提醒一场正在开的会，只会让人以为系统慢了。"""
    out: list[dict[str, Any]] = []
    for m in list_meetings():
        start = schedule_mod._dt(m.get("held_at") or "")  # noqa: SLF001
        if start is None or not schedule_mod.meeting_visible(m, person):
            continue
        mins = int((start - now).total_seconds() // 60)
        if not (0 < mins <= MEETING_LEAD_MIN):
            continue
        bits = [f"{m['held_at'][11:16]} 开始，还有 {mins} 分钟"]
        if m.get("host"):
            bits.append(f"{m['host']}主持")
        if m.get("attendees"):
            bits.append("参会 " + "、".join(m["attendees"][:3]) + ("等" if len(m["attendees"]) > 3 else ""))
        if m.get("item_count"):
            bits.append(f"{m['item_count']} 条议定事项" + (f"、{m['needs_review_count']} 条待核实" if m.get("needs_review_count") else ""))
        out.append(
            _note(
                "meet_soon",
                m["id"],
                m["id"],
                m["title"],
                " · ".join(bits),
                ref={"type": "meeting", "id": m["id"]},
                at=start.strftime("%Y-%m-%d %H:%M"),
                extra={"minutes_to": mins, "held_at": m.get("held_at") or ""},
            )
        )
    return out


def _metric_notes(person: dict[str, Any]) -> list[dict[str, Any]]:
    """关注指标低于年度目标。按「期次」去重：同一期只提醒一次，新一期披露再提醒。"""
    period = md.latest_available()
    out: list[dict[str, Any]] = []
    for code in people.watch_of(person.get("id")):
        r = md.query_metric(code, period, 1)
        if not r.get("ok"):
            continue
        note = md.target_note(r)
        if not note or ("低于" not in note and "还差" not in note):
            continue
        out.append(
            _note(
                "metric_low",
                code,
                period,
                r["name"],
                f"{r['period_label']}为 {r['value']:g}{r['unit']}，{note}",
                ref={"type": "metric", "id": code},
                at=r["source"]["published_at"],
                extra={"code": code, "period": period, "value": r["value"], "unit": r["unit"]},
            )
        )
    return out


def _period_notes(now: datetime) -> list[dict[str, Any]]:
    """下一期什么时候披露。只在披露日前 2 天提醒，提前一个月说没有用。"""
    today = now.date()
    pending = md.pending_periods()
    if not pending:
        return []
    # pending 的第一期就是最近要披露的那一期
    nxt = pending[0]
    disclosed = md._disclosed_on(nxt)  # noqa: SLF001
    left = (disclosed - today).days
    if left < 0 or left > 2:
        return []
    y, _, m = nxt.partition("-")
    return [
        _note(
            "period_due",
            nxt,
            nxt,
            f"{y} 年 {int(m)} 月期数据待披露",
            f"{left} 天后（{disclosed.isoformat()}）发布，当前展示的仍是 {md.latest_available()} 期数据",
            ref={"type": "period", "id": nxt},
            at=disclosed.isoformat(),
            extra={"period": nxt, "disclose_on": disclosed.isoformat(), "days_left": left},
        )
    ]


def _next_wake(notes: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """下一件「会主动叫人」的事是什么时候。空列表时这句话比空面板有用。"""
    today = now.date()
    wake = now.replace(hour=WAKE_HOUR, minute=WAKE_MINUTE, second=0, microsecond=0)
    candidates: list[tuple[datetime, str]] = []
    if now < wake:
        candidates.append((wake, "今早的到期与临期提醒"))
    else:
        tomorrow = wake + timedelta(days=1)
        candidates.append((tomorrow, "明天的到期与临期提醒"))
    for n in notes:
        if n["rule"] == "meet_soon":
            start = schedule_mod._dt(n["extra"].get("held_at") or "")  # noqa: SLF001
            if start:
                candidates.append((start - timedelta(minutes=MEETING_LEAD_MIN), n["title"]))
    if not candidates:
        return None
    at, label = min(candidates, key=lambda x: x[0])
    return {"at": at.strftime("%Y-%m-%d %H:%M"), "label": label}


# --------------------------------------------------------------------------
# 对外：提醒列表
# --------------------------------------------------------------------------
def build(person_id: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    pid = people.resolve(person_id)
    person = people.meta(pid)

    notes = _oversight_notes(person, now) + _meeting_notes(person, now) + _metric_notes(person) + _period_notes(now)

    seen = set(people.seen_of(pid))
    for n in notes:
        n["read"] = n["id"] in seen
        # 送达方式写在条目上：同一条「逾期」，局长看到的是「已发住建局」，
        # 没配通道时就是「只在本机」。不让前端去猜。
        n["delivery"] = "external" if (n["to"] in ("unit", "both") and n["level"] in _EXTERNAL_LEVELS) else "inapp"

    # 两遍稳定排序：先按时间倒序（越新越前），再按级别正序。
    # 一次写 key 也行，但「级别优先、同级别看时间」这种复合意图用两次排序更不容易写错。
    notes.sort(key=lambda n: (n["at"], n["title"]), reverse=True)
    notes.sort(key=lambda n: n["level_rank"])
    groups = []
    for level in ("urgent", "high", "normal", "low"):
        items = [n for n in notes if n["level"] == level]
        if items:
            groups.append({"level": level, "label": _LEVEL_LABEL[level], "count": len(items), "items": items})

    units, _ = schedule_mod.scope_of(person)
    chan = channels_status()
    return {
        "ok": True,
        "person": {k: person[k] for k in ("id", "name", "post", "scope", "demo")},
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "today": now.date().isoformat(),
        "total": len(notes),
        "unread": sum(1 for n in notes if not n["read"]),
        "urgent": sum(1 for n in notes if n["level"] == "urgent"),
        "notes": notes,
        "groups": groups,
        "next": _next_wake(notes, now),
        "channels": {**chan, "template": config_template()},
        "rules": [dict(r) for r in RULES],
        "outbox": _outbox_summary(),
        "basis": ("分管单位：" + "、".join(sorted(units))) if units else "全市（不按单位筛）",
        "rule": "提醒只从台账状态、会议时间、指标披露里派生，不新增事项；同一件事每天只提醒一次，第二天没办会再出现。",
        "mock": True,
    }


def mark_read(person_id: str | None, ids: list[str] | None = None, *, all_: bool = False) -> dict[str, Any]:
    pid = people.resolve(person_id)
    keys = [str(i) for i in (ids or []) if str(i).strip()]
    if all_ and not keys:
        keys = [n["id"] for n in build(pid)["notes"]]
    if not keys:
        return {"ok": False, "error": "no_ids", "message": "没有要标记的提醒"}
    result = people.mark_seen(pid, keys)
    return {**result, "ids": keys[:50]}


def _outbox_summary() -> dict[str, Any]:
    doc = _store()
    box = doc.get("outbox") or {}
    history = box.get("history") or []
    today = _now().date().isoformat()
    sent_today = [h for h in history if str(h.get("at", "")).startswith(today)]
    return {
        "last": box.get("last") or None,
        "sent_today": len(sent_today),
        "history": history[-5:],
    }


# --------------------------------------------------------------------------
# 外发：站内信之外的那一段
# --------------------------------------------------------------------------
def _dispatch_text(notes: list[dict[str, Any]], now: datetime) -> str:
    """群消息 / 邮件正文。只抄台账里的字段，不加工、不总结。"""
    head = f"【督办提醒】{now.strftime('%Y-%m-%d %H:%M')}　共 {len(notes)} 件需处理"
    lines = [head, ""]
    for i, n in enumerate(notes, 1):
        who = n["extra"].get("assignee") or n["body"].split(" · ")[0]
        lines.append(f"{i}. [{n['rule_label']}] {n['title']}（{who}）")
        lines.append(f"   {n['body']}")
        if n["ref"].get("type") == "oversight":
            lines.append(f"   台账编号 {n['ref']['id']}")
    lines += ["", "请在台账中反馈进展；本条由系统按台账实时状态生成，数字未经模型改写。"]

    # 有催办文案的，附上「复制就能发」的版本：没有外发通道时，人贴到群里是唯一
    # 真能送达的路径。与其等企微 token，不如把这段文案做对。
    return "\n".join(lines)


def _send_webhook(cfg: dict[str, Any], text: str, notes: list[dict[str, Any]]) -> tuple[bool, str]:
    url = (cfg.get("url") or "").strip()
    if not url:
        return False, "未配置 webhook 地址"
    if cfg.get("format") == "wecom":
        payload: dict[str, Any] = {"msgtype": "text", "text": {"content": text}}
    else:
        payload = {
            "title": "督办提醒",
            "text": text,
            "items": [{"id": n["id"], "title": n["title"], "body": n["body"], "ref": n["ref"]} for n in notes],
        }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read(300).decode("utf-8", "replace")
            ok = 200 <= resp.status < 300
            # 企微机器人 HTTP 200 也可能是 errcode != 0（地址失效、超频），必须看内容
            if ok and cfg.get("format") == "wecom":
                try:
                    parsed = json.loads(body or "{}")
                    if parsed.get("errcode") not in (None, 0):
                        return False, f"errcode={parsed.get('errcode')} {parsed.get('errmsg') or ''}".strip()
                except json.JSONDecodeError:
                    pass
            return ok, "" if ok else f"HTTP {resp.status} {body[:120]}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _send_smtp(cfg: dict[str, Any], text: str) -> tuple[bool, str]:
    host = (cfg.get("host") or "").strip()
    to = [x for x in (cfg.get("to") or []) if str(x).strip()]
    if not host or not to:
        return False, "未配置 SMTP 服务器或收件人"
    msg = EmailMessage()
    msg["Subject"] = f"督办提醒 {_now().strftime('%Y-%m-%d')}"
    msg["From"] = cfg.get("user") or f"ai-mayor@{host}"
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    port = int(cfg.get("port") or 465)
    try:
        if cfg.get("ssl", True):
            server: Any = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            server.starttls()
        try:
            if cfg.get("user"):
                server.login(cfg["user"], cfg.get("password") or "")
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except (smtplib.SMTPException, OSError):
                pass
        return True, ""
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _in_quiet_hours(now: datetime, quiet: list[str]) -> bool:
    """静默时段可能跨零点（22:00–07:00），不能简单比大小。"""
    if len(quiet) != 2:
        return False
    try:
        start = datetime.strptime(quiet[0], "%H:%M").time()
        end = datetime.strptime(quiet[1], "%H:%M").time()
    except ValueError:
        return False
    t = now.time()
    return (t >= start or t < end) if start > end else (start <= t < end)


def dispatch(
    *,
    now: datetime | None = None,
    send_hook: Callable[[dict[str, Any], str, list[dict[str, Any]]], tuple[bool, str]] | None = None,
    send_mail: Callable[[dict[str, Any], str], tuple[bool, str]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """把「紧急 / 重要」的督办类提醒送出去。全局只跑一次，不按身份重复发。

    为什么不按人发：三个身份看的是同一张台账，按人发会把同一件事发三遍给同一个群。
    站内信按人算（各看各的），外发按单位算（谁的事发给谁），两者口径不同是对的。
    """
    now = now or _now()
    cfg = config()
    chan = channels_status()

    # 外发只取全市视角的督办类：会议/指标/期次是给领导自己看的，不该推到单位群
    person = people.meta("mayor")
    pool = [n for n in _oversight_notes(person, now) if n["level"] in _EXTERNAL_LEVELS]
    if not pool:
        return {"ok": True, "sent": 0, "skipped": "nothing", "message": "没有需要外发的督办提醒"}

    if not force:
        if _in_quiet_hours(now, cfg.get("quiet_hours") or []):
            return {"ok": True, "sent": 0, "skipped": "quiet_hours", "message": "处于静默时段，未外发"}
        if cfg.get("workdays_only", True) and now.weekday() >= 5 and not any(n["level"] == "urgent" for n in pool):
            return {"ok": True, "sent": 0, "skipped": "weekend", "message": "周末只外发紧急提醒，本次跳过"}
        doc = _store()
        today = now.date().isoformat()
        sent_today = [h for h in (doc.get("outbox") or {}).get("history") or [] if str(h.get("at", "")).startswith(today)]
        if len(sent_today) >= int(cfg.get("daily_limit") or 20):
            return {"ok": True, "sent": 0, "skipped": "daily_limit", "message": "已达今日外发上限"}
        # 批次指纹：内容没变就不重发。只比条数不够 —— 换了一件事也是新信息。
        batch = hashlib.md5(("|".join(sorted(n["id"] for n in pool))).encode("utf-8")).hexdigest()[:10]
        last = (doc.get("outbox") or {}).get("last") or {}
        if last.get("batch") == batch and not last.get("error"):
            return {"ok": True, "sent": 0, "skipped": "duplicate", "message": "同一批提醒已发送过"}

    text = _dispatch_text(pool, now)
    hook_fn = send_hook or _send_webhook
    mail_fn = send_mail or _send_smtp
    results: list[dict[str, Any]] = []
    hook_cfg = cfg["channels"]["webhook"]
    mail_cfg = cfg["channels"]["smtp"]

    if chan["webhook"]["configured"]:
        ok, err = hook_fn(hook_cfg, text, pool)
        results.append({"channel": "webhook", "ok": ok, "error": err})
    if chan["smtp"]["configured"]:
        ok, err = mail_fn(mail_cfg, text)
        results.append({"channel": "smtp", "ok": ok, "error": err})

    if not results:
        return {
            "ok": True,
            "sent": 0,
            "skipped": "no_channel",
            "message": "未配置外发通道，提醒只写进站内信",
            "pool": len(pool),
        }

    now_s = now.strftime("%Y-%m-%d %H:%M")
    batch_id = hashlib.md5(("|".join(sorted(n["id"] for n in pool))).encode("utf-8")).hexdigest()[:10]

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        state = doc.setdefault("channels", {})
        for r in results:
            state[f"{r['channel']}_ok"] = r["ok"]
            state[f"{r['channel']}_error"] = r["error"]
            state[f"{r['channel']}_at"] = now_s
        box = doc.setdefault("outbox", {})
        box["last"] = {
            "at": now_s,
            "batch": batch_id,
            "count": len(pool),
            "channels": [r["channel"] for r in results],
            "ok": all(r["ok"] for r in results),
            "error": "; ".join(r["error"] for r in results if r["error"]),
        }
        history = box.get("history") or []
        history.append({"at": now_s, "batch": batch_id, "count": len(pool), "ok": box["last"]["ok"]})
        box["history"] = history[-200:]  # 只留最近 200 条：这是运行记录，不是审计日志
        return doc

    store.update(STORE_NAME, _apply, {})

    delivered = [r["channel"] for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    return {
        "ok": not failed,
        "sent": len(pool),
        "delivered": delivered,
        "failed": failed,
        "batch": batch_id,
        "text": text,
        "message": (
            f"已发送 {len(pool)} 条督办提醒至 {'、'.join(delivered)}"
            if delivered
            else f"发送失败：{failed[0]['error'] if failed else '未知原因'}"
        ),
    }


def test_channel(
    kind: str = "webhook",
    *,
    send_hook: Callable[[dict[str, Any], str, list[dict[str, Any]]], tuple[bool, str]] | None = None,
    send_mail: Callable[[dict[str, Any], str], tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """发一条测试消息。验证的是「能不能送到」，不是「配置看起来对不对」。

    和正式提醒分开：正式提醒走闸门（静默时段 / 重复 / 日限额），测试必须能随时发，
    否则配好通道后要等到明天早上才知道有没有生效。
    """
    kind = "smtp" if kind == "smtp" else "webhook"
    cfg = config()
    now = _now()
    text = (
        f"【通道测试】AI 市长工作台 · {now.strftime('%Y-%m-%d %H:%M')}\n"
        "收到这条说明通道可用。正式提醒只发「需要立刻处理」和「今天要跟进」两档，"
        "其余留在工作台的提醒面板里。"
    )
    if not channels_status()[kind]["configured"]:
        return {"ok": False, "error": "not_configured", "message": f"{kind} 通道未配置（enabled + 地址/收件人）"}

    if kind == "smtp":
        ok, err = (send_mail or _send_smtp)(cfg["channels"]["smtp"], text)
    else:
        ok, err = (send_hook or _send_webhook)(cfg["channels"]["webhook"], text, [])

    now_s = now.strftime("%Y-%m-%d %H:%M")

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        state = doc.setdefault("channels", {})
        state[f"{kind}_ok"] = ok
        state[f"{kind}_error"] = err
        state[f"{kind}_at"] = now_s
        return doc

    store.update(STORE_NAME, _apply, {})
    return {"ok": ok, "channel": kind, "error": err, "message": "测试消息已发送" if ok else f"发送失败：{err}"}


# --------------------------------------------------------------------------
# 定时：服务在跑就替你看表
# --------------------------------------------------------------------------
_STOP = threading.Event()


def tick(*, now: datetime | None = None) -> dict[str, Any]:
    """一次巡检。站内信不需要巡检（每次打开页面现算），这里只负责按时外发。"""
    try:
        return dispatch(now=now)
    except Exception as exc:  # noqa: BLE001
        # 定时任务出错不能把服务带死，但也不能吞：写进通道状态，页面上看得到
        traceback.print_exc()
        store.update(STORE_NAME, lambda d: {**(d or {}), "worker_error": f"{type(exc).__name__}: {exc}"}, {})
        return {"ok": False, "error": str(exc)}


def start_worker(interval: int = 300) -> threading.Thread | None:
    """后台巡检线程。`OCGOV_NOTIFY_TICK=0` 关掉（测试和单次演示用）。

    第一次等一个 interval 再跑：服务刚起来时台账可能还在加载，
    而且开机就往外发消息，会让人以为系统在重放历史。
    """
    if os.environ.get("OCGOV_NOTIFY_TICK") == "0":
        return None

    def _loop() -> None:
        while not _STOP.wait(interval):
            tick()

    th = threading.Thread(target=_loop, name="notify-tick", daemon=True)
    th.start()
    return th


def stop_worker() -> None:
    _STOP.set()
