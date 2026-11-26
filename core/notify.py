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
import re
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
from ledger import list_meetings, mark_notice_read, notices_for_person  # noqa: E402

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
        "id": "ov_urge",
        "kind": "oversight",
        "label": "被催办",
        "level": "high",
        "to": "unit",
        "when": "被催办后当天一次；再催一次算一条新提醒（同一事项当天仍只出现一条）",
        "why": "催办是市里对单位提的要求，必须送到经办人手里，而不是只写进台账",
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
        "id": "meet_notice",
        "kind": "meeting",
        "label": "会议通知",
        "level": "high",
        # 通知只走站内信：正文是公文，外发到群或邮箱等于替发文机关再发一次文，
        # 而系统并不知道那一边有没有盖章。所以这一条的 to 永远是 self。
        "to": "self",
        "when": "会议通知发出后（一场会一条，不按天重复，点开前一直留着）",
        "why": "时间、地点、议题、入会号要在一处看全 —— 靠口头转述一定会漏一项",
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

# 通道的对外说法。内部的 webhook / smtp 是给代码和配置看的，
# 提示语里要写成「群机器人」「邮件」——否则用户看到的是一句他不懂的话。
_CHANNEL_NAME = {"webhook": "群机器人", "smtp": "邮件"}
_NO_CHANNEL_MSG = "还没接群机器人或邮箱，提醒只留在工作台里"

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
            "已接上群机器人或邮箱：紧急与重要提醒会同时留在工作台并发送出去"
            if (hook_on or mail_on)
            else "还没接群机器人或邮箱：提醒只在本机可见，责任单位收不到。到「设置 → 提醒与送达」里填。"
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
# 通道配置：页面能改，不用去动文件
# --------------------------------------------------------------------------
# 早先只能改 config/notify.json + 重启（其实不用重启，config() 每次现读）。
# 结果是页面上写着「未配置外发通道」，却没有一个地方能填 —— 提醒一直留在本机，
# 而用户以为是自己没找到入口。所以补一个写入口径，规则只有三条：
#
# 1. **密钥字段空 = 不改**。页面回显的是打码值（`_mask`），拿打码值当新地址提交
#    会把真地址覆盖成一串省略号，群当场失联。要清空得显式传 `null`。
# 2. **其余字段以页面为准**（空 = 清空）。它们不是密钥，回显的是真值。
# 3. **先校验、后落盘，整体拒绝**。写坏一个键不只是那一个通道失效：
#    `config()` 里那个 except 会把整份文件当坏文件吃掉，回到默认值 ——
#    四个通道一起消失，比报一个错难查得多。
_CONFIG_LOCK = threading.Lock()
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _raw_config() -> dict[str, Any]:
    """磁盘上那一份，**不叠环境变量**。

    写回必须以它为基础：`config()` 已经把 OCGOV_NOTIFY_WEBHOOK 之类叠进来了，
    拿它的结果写文件，等于把环境变量里的密钥抄进磁盘，以后改环境变量也不生效。
    """
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def form_state() -> dict[str, Any]:
    """「设置 → 提醒与送达」表单的回显值。密钥只回「设没设过」，不回真值。

    页面拿到真值，就等于把群机器人的发消息口令放进浏览器内存和 network 面板 ——
    这个面板是多个人共看的，打码不是顺手，是必须。
    """
    cfg = config()
    hook = cfg["channels"]["webhook"]
    mail = cfg["channels"]["smtp"]
    return {
        "webhook": {
            "enabled": bool(hook.get("enabled")),
            "format": hook.get("format") or "wecom",
            "url_set": bool((hook.get("url") or "").strip()),
            "url_hint": _mask(hook.get("url") or ""),
        },
        "smtp": {
            "enabled": bool(mail.get("enabled")),
            "host": mail.get("host") or "",
            "port": int(mail.get("port") or 465),
            "ssl": bool(mail.get("ssl", True)),
            "user": mail.get("user") or "",
            "to": list(mail.get("to") or []),
            "password_set": bool(mail.get("password")),
        },
        "quiet_hours": list(cfg.get("quiet_hours") or []),
        "workdays_only": bool(cfg.get("workdays_only", True)),
        "daily_limit": int(cfg.get("daily_limit") or 20),
        # 环境变量优先级最高：写进文件也不生效，得如实说，不能让人改完以为成了。
        "env_override": {
            "webhook": bool(os.environ.get("OCGOV_NOTIFY_WEBHOOK")),
            "smtp": bool(os.environ.get("OCGOV_NOTIFY_SMTP_HOST")),
        },
    }


def _text(value: Any, *, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """把页面填的通道配置写进 `config/notify.json`。返回 {ok, message} 或 {ok:False, error}。

    写的时机没有缓存问题：`config()` 每次调用都重新读文件，所以保存后下一次
    巡检（默认 5 分钟内）就走新通道，不用重启服务。
    """
    patch = patch if isinstance(patch, dict) else {}
    raw = _raw_config()
    channels = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    raw["channels"] = channels
    hook = channels.get("webhook") if isinstance(channels.get("webhook"), dict) else {}
    mail = channels.get("smtp") if isinstance(channels.get("smtp"), dict) else {}
    channels["webhook"] = hook
    channels["smtp"] = mail

    # ── 群机器人 ──
    w = patch.get("webhook")
    if isinstance(w, dict):
        if "url" in w:
            url = w.get("url")
            if url is None:
                hook["url"] = ""
            elif isinstance(url, str):
                url = url.strip()
                # 空串 = 保持原值（页面只有一个空输入框，不是清空指令）
                if url:
                    if not url.lower().startswith(("http://", "https://")):
                        return {"ok": False, "error": "群机器人地址要以 http:// 或 https:// 开头"}
                    if len(url) > 500:
                        return {"ok": False, "error": "群机器人地址过长（超过 500 字符）"}
                    hook["url"] = url
            else:
                return {"ok": False, "error": "群机器人地址格式不对"}
        if "format" in w:
            fmt = _text(w.get("format"), limit=20)
            if fmt not in ("wecom", "generic"):
                return {"ok": False, "error": "消息格式只支持 wecom（企业微信）或 generic（原样 JSON）"}
            hook["format"] = fmt
        if "enabled" in w:
            hook["enabled"] = bool(w.get("enabled"))

    # ── 邮件 ──
    s = patch.get("smtp")
    if isinstance(s, dict):
        if "host" in s:
            host = _text(s.get("host"), limit=120)
            if host and not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
                return {"ok": False, "error": "SMTP 服务器只填域名或 IP，不要带 smtp:// 前缀"}
            mail["host"] = host
        if "port" in s and s.get("port") not in (None, ""):
            try:
                port = int(s.get("port"))
            except (TypeError, ValueError):
                return {"ok": False, "error": "SMTP 端口要填数字"}
            if not 1 <= port <= 65535:
                return {"ok": False, "error": "SMTP 端口要在 1–65535 之间"}
            mail["port"] = port
        if "ssl" in s:
            mail["ssl"] = bool(s.get("ssl"))
        if "user" in s:
            mail["user"] = _text(s.get("user"), limit=120)
        if "password" in s:
            pwd = s.get("password")
            if pwd is None:
                mail["password"] = ""
            elif isinstance(pwd, str) and pwd.strip():
                # 同 url：空串 = 不改，避免每次保存都把授权码抹成空
                mail["password"] = pwd.strip()
        if "to" in s:
            raw_to = s.get("to")
            if isinstance(raw_to, str):
                items = [x for x in re.split(r"[,，;；\s]+", raw_to) if x]
            elif isinstance(raw_to, list):
                items = [str(x).strip() for x in raw_to if str(x).strip()]
            elif raw_to is None:
                items = []
            else:
                return {"ok": False, "error": "收件人格式不对"}
            bad = [x for x in items if not _EMAIL.match(x)]
            if bad:
                return {"ok": False, "error": f"收件人不是有效邮箱：{bad[0]}"}
            mail["to"] = items[:20]
        if "enabled" in s:
            mail["enabled"] = bool(s.get("enabled"))

    # ── 发送策略（不分通道，全局一份）──
    if "quiet_hours" in patch:
        q = patch.get("quiet_hours")
        if isinstance(q, str):
            q = [x for x in re.split(r"[,，~\-–\s]+", q) if x]
        if q is None:
            q = []
        if not isinstance(q, list):
            return {"ok": False, "error": "静默时段格式不对"}
        hours = [str(x).strip() for x in q if str(x).strip()]
        if hours and len(hours) != 2:
            return {"ok": False, "error": "静默时段要填两个时间：开始和结束"}
        for h in hours:
            if not _HHMM.match(h):
                return {"ok": False, "error": f"时间要写成 HH:MM（如 22:00），收到「{h}」"}
        raw["quiet_hours"] = hours
    if "workdays_only" in patch:
        raw["workdays_only"] = bool(patch.get("workdays_only"))
    if "daily_limit" in patch and patch.get("daily_limit") not in (None, ""):
        try:
            limit = int(patch.get("daily_limit"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "日限额要填数字"}
        if not 1 <= limit <= 200:
            return {"ok": False, "error": "日限额要在 1–200 之间"}
        raw["daily_limit"] = limit

    try:
        with _CONFIG_LOCK:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_PATH)  # 同盘 rename 是原子的，读到的一半文件不会存在
    except OSError as exc:
        return {"ok": False, "error": f"写入 config/notify.json 失败：{exc}"}

    chan = channels_status()
    overridden = [name for name, on in (("群机器人", os.environ.get("OCGOV_NOTIFY_WEBHOOK")),
                                        ("邮件", os.environ.get("OCGOV_NOTIFY_SMTP_HOST"))) if on]
    if overridden:
        message = f"已写入 config/notify.json，但{'、'.join(overridden)}被环境变量接管，实际发送以环境变量为准"
    elif chan["mode"] == "external":
        message = "已保存：5 分钟内自动发送就会走这条通道"
    else:
        message = "已保存，但还没有能送出去的通道 —— 打开开关或填完地址才算配好"
    return {"ok": True, "message": message, "channels": {**chan, "form": form_state()},
            "env_override": overridden}


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
    level: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    rule = _RULE_BY_ID[rule_id]
    # level 默认取规则表的值。只有一处会覆盖：事项本来就逾期/今日到期时被催办，
    # 不能因为「今天多了一条催办」就从「需要立刻处理」掉到「今天要跟进」——
    # 催办说明有人盯着，不说明没那么急。
    lv = level or rule["level"]
    return {
        # id 里带 bucket（日期 / 期次 / 会议 id）：同一天同一件事只有一个 id，
        # 第二天 bucket 变了就成了一条新提醒 —— 不用额外写「昨天发过没有」。
        "id": f"{rule_id}:{subject}:{bucket}",
        "rule": rule_id,
        "rule_label": label or rule["label"],
        "kind": rule["kind"],
        "level": lv,
        "level_rank": _LEVEL_RANK[lv],
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


def _actor_name(actor: str) -> str:
    """事件里的 actor 换成人能读的称呼。

    台账里两种写法都有：走登录的操作写的是身份 id（`u-zhujian` / `secretary`），
    种子数据写的是「市政府办 李敏」这种人名。认得出就换名字，认不出原样给出 ——
    展示层不猜、也不编。
    """
    p = people.find(actor)
    return str(p.get("name") or actor) if p else actor


def _urged_today(person: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    """今天被催办、且不是本人催的事项：id → 台账行（行里带 last_urge）。

    为什么在 `ov.scan()` 之外单独扫一遍：scan 的桶只装风险（逾期/临期/停滞…），
    而催办可以落在任何未办结的事项上 —— 一件「状态正常」的事被催了，也必须
    进收件箱，否则就是「催了没人知道」。
    """
    today = now.date().isoformat()
    units, manual = schedule_mod.scope_of(person)
    out: dict[str, dict[str, Any]] = {}
    for it in ov.list_items():
        if it.get("status") == ov.CLOSED:
            continue
        urge = it.get("last_urge") or {}
        if not urge or not str(urge.get("ts") or "").startswith(today):
            continue
        if not schedule_mod.item_visible(it, units, manual):
            continue
        # 自己催的自己不用再收一条：收件箱不该变成「我发过什么」的台账
        if _actor_name(str(urge.get("actor") or "")) == str(person.get("name") or ""):
            continue
        out[it["id"]] = it
    return out


def _relation(it: dict[str, Any], units: Any) -> str:
    """这条事项对「当前这个人」是什么关系：主送 / 抄送 / 督办。

    主送＝本人所在（或分管）单位就是主办单位，这件事欠的是他的账；
    抄送＝只落在协办单位里，要配合但不是第一责任人；
    督办＝两边都不沾（市长、秘书这类全市视角，以及分管单位没被涉及的分管领导），
    他看到的是「谁在被催」，不是「我要办」。

    为什么要分：一件事主办、协办常常三四个单位，同一段催办原文原样群发，
    协办单位会以为自己被点名主责，主办单位又会以为有人在替他扛。
    """
    host = str(it.get("assignee") or "")
    co = {str(x) for x in (it.get("co_assignees") or [])}
    us = {str(x) for x in (units or ())}
    if host and host in us:
        return "主送"
    if us & co:
        return "抄送"
    return "督办"


def _urge_note(it: dict[str, Any], now: datetime, relation: str = "主送") -> dict[str, Any]:
    """把「今天被催办」变成一条能点回台账的提醒。

    去重桶里带催办次数：同一天被催第二次会生成新 id（再提醒一次），而不是被
    上一条的「已读」吃掉 ——「催了没看」和「看了没办」是两件事，不能合并。
    """
    urge = it["last_urge"]
    host = str(it.get("assignee") or "")
    co = [str(x) for x in (it.get("co_assignees") or [])]
    actor = _actor_name(str(urge.get("actor") or ""))
    said = str(urge.get("text") or "").strip()
    # 抬头按关系写，而不是一律「某某单位等」：抄送单位看到的必须先是
    # 「主办是谁」，否则他会把这条当成自己的主责件。
    if relation == "抄送":
        lead = f"【抄送·协办】主办单位 {host}"
    else:
        lead = host + ("等" if co else "")
    body = f"{lead} · {actor} 催办：{said}" if said else f"{lead} · {actor} 催办"
    # 风险理由一起带上：催办说的是「有人盯着」，不该把「已逾期 23 天」抹掉
    reasons = [r for r in (it.get("risk_reasons") or []) if r != "已办结"]
    if reasons:
        body += f"（当前：{'；'.join(reasons)}）"
    urgent = it.get("risk") in (ov.RISK_OVERDUE, ov.RISK_TODAY)
    if relation == "抄送":
        level, label = "normal", "被催办（抄送）"   # 抄送件不亮红灯
    elif relation == "督办":
        # 全市视角不是在「被催」，而是看见别人催了谁。写成「被催办」会让人
        # 以为有人在催他 —— 一条会让人误判自己的消息，比没有这条更糟。
        level = "urgent" if urgent else None
        label = "已催办"
    else:
        # 逾期件被催仍按「需要立刻处理」：催办是加压，不是降级
        level, label = ("urgent" if urgent else None), None
    return _note(
        "ov_urge",
        it["id"],
        f"{now.date().isoformat()}#{urge.get('count') or 1}",
        it["title"],
        body,
        ref={"type": "oversight", "id": it["id"]},
        at=str(urge.get("ts") or ""),
        extra={
            "assignee": it["assignee"],
            "co_assignees": co,
            "relation": relation,
            "urge_actor": actor,
            "remind_count": urge.get("count") or 1,
            "deadline": it["deadline"],
            "days_left": it["days_left"],
            "progress": it["progress"],
        },
        # 逾期件被催仍按「需要立刻处理」：催办是加压，不是降级。
        # 抄送单位不跟着亮红灯 —— 它不是第一责任人，把协办件标成紧急，
        # 只会让「紧急」这个词贬值。
        level=level,
        label=label,
    )


def _oversight_notes(person: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """一条在办事项最多出一条提醒，取最严重的那种；今天被催过的改说「被催办」。

    直接读 `ov.scan()` 的桶，不在这里重算「逾期几天」——
    重算就会有第二个口径，而台账那个才是权威。
    """
    today = now.date()
    wake = now.replace(hour=WAKE_HOUR, minute=WAKE_MINUTE, second=0, microsecond=0)
    units, manual = schedule_mod.scope_of(person)
    sc = ov.scan(max(SOON_DAYS))
    urged = _urged_today(person, now)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    for bucket, rule_id in _BUCKET_RULE:
        for it in sc["buckets"].get(bucket, []):
            if it["id"] in seen or not schedule_mod.item_visible(it, units, manual):
                continue
            seen.add(it["id"])
            # 今天被催过的：用「被催办」换掉同一天的状态提醒。一件事一天只出一条，
            # 但那条应该是「有人要你办」，而不是「它逾期了」—— 后者收件人早就知道。
            urged_row = urged.get(it["id"])
            if urged_row is not None:
                out.append(_urge_note(urged_row, now, _relation(urged_row, units)))
                continue
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

    # 状态正常、但今天被催办的事项：不在任何风险桶里，照样要送到。
    # 不补这一段，催办就只在「被催的恰好是逾期件」时才看得见 —— 而实际催得最多
    # 的往往正是那些不逾期、也没停滞、但进度不动的事。
    for iid, row in urged.items():
        if iid in seen:
            continue
        seen.add(iid)
        out.append(_urge_note(row, now, _relation(row, units)))
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


def _notice_notes(person: dict[str, Any]) -> list[dict[str, Any]]:
    """已经发到本人名下的会议通知。

    和 `_meeting_notes` 不是一回事：那是系统算出来的会前提醒（会开始就没了），
    这是人发出来的公文（发出即冻结，点开前一直在）。所以正文只给一句摘要 ——
    全文在会议详情里，站内信负责把人叫过去，不负责存公文的第二份。

    `read` 由通知记录的 `reads` 决定，不用 people.seen：回执是给发文的人看的，
    不能因为收件人清过本地已读记录就变回「未读」。
    """
    pid = str(person.get("id") or "")
    out: list[dict[str, Any]] = []
    for n in notices_for_person(pid):
        held = str(n.get("held_at") or "").strip()
        place = str(n.get("place") or "").strip()
        bits = [held or "时间另行通知"]
        bits.append(f"地点 {place}" if place else "地点另行通知")
        if n.get("uncovered"):
            bits.append("另有 " + str(len(n["uncovered"])) + " 家参会单位未收到")
        bits.append("点开看全文")
        out.append(
            _note(
                "meet_notice",
                n["id"],
                n["id"],
                n.get("title") or "会议通知",
                " · ".join(bits),
                ref={"type": "meeting", "id": n.get("meeting_id") or ""},
                at=str(n.get("sent_at") or ""),
                extra={
                    "notice_id": n["id"],
                    "meeting_id": n.get("meeting_id") or "",
                    "read_at": n.get("read_at") or "",
                    "sent_by": n.get("sent_by_name") or "",
                    "chars": len(n.get("text") or ""),
                },
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

    notes = (
        _oversight_notes(person, now)
        + _meeting_notes(person, now)
        + _notice_notes(person)
        + _metric_notes(person)
        + _period_notes(now)
    )

    seen = set(people.seen_of(pid))
    for n in notes:
        n["read"] = n["id"] in seen
        # 会议通知的已读以通知记录里的回执为准：本地「标记已读」被清掉时，
        # 不能让发文的人看到回执倒退。
        if n["rule"] == "meet_notice" and n["extra"].get("read_at"):
            n["read"] = True
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
    # 通知的「已读」还要写回通知记录：本地 seen 是给自己看的角标，回执是给发文人
    # 看的送达凭据，两份都要有。key 的形状是 meet_notice:<通知号>:<通知号>。
    for k in keys:
        if not k.startswith("meet_notice:"):
            continue
        parts = k.split(":")
        if len(parts) >= 2 and parts[1]:
            mark_notice_read(parts[1], pid)
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
        return {"ok": True, "sent": 0, "skipped": "nothing", "message": "现在没有需要发送的提醒"}

    # 通道一条都没接，就不用再看后面的闸门了：这时候说「静默时段，等结束会自动发送」
    # 或者「同一批已发送过」都是假话 —— 等到天亮、等到明天，也一样发不出去。
    if not (chan["webhook"]["configured"] or chan["smtp"]["configured"]):
        return {"ok": True, "sent": 0, "skipped": "no_channel",
                "message": _NO_CHANNEL_MSG, "pool": len(pool)}

    if not force:
        if _in_quiet_hours(now, cfg.get("quiet_hours") or []):
            return {"ok": True, "sent": 0, "skipped": "quiet_hours", "message": "现在是静默时段，等时段结束会自动发送"}
        if cfg.get("workdays_only", True) and now.weekday() >= 5 and not any(n["level"] == "urgent" for n in pool):
            return {"ok": True, "sent": 0, "skipped": "weekend", "message": "周末只发紧急提醒，这次跳过"}
        doc = _store()
        today = now.date().isoformat()
        sent_today = [h for h in (doc.get("outbox") or {}).get("history") or [] if str(h.get("at", "")).startswith(today)]
        if len(sent_today) >= int(cfg.get("daily_limit") or 20):
            return {"ok": True, "sent": 0, "skipped": "daily_limit", "message": "今天已经发到上限了"}
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
            "message": _NO_CHANNEL_MSG,
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
            f"已发送 {len(pool)} 条督办提醒到{'、'.join(_CHANNEL_NAME.get(c, c) for c in delivered)}"
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
        return {"ok": False, "error": "not_configured",
                "message": f"{_CHANNEL_NAME[kind]}还没配好：要打开开关并填完地址/收件人"}

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
