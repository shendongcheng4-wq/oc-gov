"""腾讯会议接入：官方 MCP + 个人 Token（和 Octop 的连接器同一条路）。

**鉴权是个人 Token，不是企业自建应用。** 登录 https://meeting.tencent.com/ai-skill.html
复制页面上的 Token 填进 `config/wemeet.json` 就能用 —— 没有企业管理员开应用、没有
权限点申请、没有「高级账号 × 月」的建会配额、也不需要手工换 STS-Token。代价是它只
看得见这个 Token 所属账号的会议。

协议（MCP / JSON-RPC 2.0 over HTTP）：

    POST  <url>  initialize            → 拿到 Mcp-Session-Id（有的服务端不给）
    POST  <url>  notifications/initialized（尽力而为，失败不拦）
    POST  <url>  tools/list            → 工具名 + description + inputSchema
    POST  <url>  tools/call            → 结果在 result.content[] 里

响应可能是普通 JSON，也可能是 SSE（`data: {...}` 一行一帧）——**两种都认**。
Head 照 Octop 的做法带 `X-Tencent-Meeting-Token` 与 `X-Skill-Version`。

三条设计约束：

1. **工具名与参数一律不硬编码**。这个服务端给什么工具就是什么，`tools/list`
   取回来原样交给界面。写死 `create_meeting(name, start_time)` 这种签名，换个
   版本就静默失效，而界面上只会显示「调用失败」。
2. **调用与落库分开**。`call_tool()` 只把结果拿回来并**认一认**里面有没有会议号 /
   会议原文；写进会议主档是另一个动作（`ledger.set_online` / `set_transcript`），
   由人点。工具返回一段文本就自动当成纪要落库，等于让远端服务往督办台账里灌字。
3. **配置只在文件里**，没有 HTTP 写入口（同提醒通道）。Token 等同口令，能打开
   页面就能换掉别人的会议账号，比没有这个功能更糟。
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "wemeet.json"
EXAMPLE_PATH = ROOT / "config" / "wemeet.example.json"

# Octop 用的就是这两个值（core/infra/connectors/builder.py），跟着走省得自己发明
DEFAULT_URL = "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
DEFAULT_SKILL_VERSION = "v1.0.1"
PROTOCOL_VERSION = "2024-11-05"

TIMEOUT = 20.0
# tools/list 缓存：同一个 Token 下工具清单不会每轮都变，而每次打开会议都要列一遍
# 太浪费。只缓存成功结果，失败不缓存（配错了要能立刻重试）。
_TOOLS_TTL = 120.0
_tools_cache: dict[str, Any] = {"key": "", "at": 0.0, "tools": [], "server": {}}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def config_path() -> Path:
    env = os.environ.get("OCGOV_WEMEET_CONFIG")
    return Path(env).expanduser() if env else CONFIG_PATH


def config() -> dict[str, Any]:
    """读配置。环境变量优先（`OCGOV_WEMEET_TOKEN` 等）。

    读不到、读坏了都返回空 dict —— 调用方用 `ready()` 判，不靠异常。
    """
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}

    for key, env in (
        ("token", "OCGOV_WEMEET_TOKEN"),
        ("url", "OCGOV_WEMEET_URL"),
        ("skill_version", "OCGOV_WEMEET_SKILL_VERSION"),
    ):
        value = os.environ.get(env)
        if value:
            data[key] = value
    return data


def _s(cfg: dict, key: str, default: str = "") -> str:
    return str(cfg.get(key) or default).strip()


def ready(cfg: dict[str, Any] | None = None) -> bool:
    """能不能真的发请求。只差一个 Token —— 这正是这条路线的全部门槛。"""
    cfg = config() if cfg is None else cfg
    if cfg.get("enabled") is False:
        return False
    return bool(_s(cfg, "token"))


def missing() -> list[str]:
    """还缺什么。只会有两项，且第一项几乎永远是它。"""
    cfg = config()
    lack = []
    if not _s(cfg, "token"):
        lack.append("Token（登录 meeting.tencent.com/ai-skill.html 复制）")
    return lack


def public_config() -> dict[str, Any]:
    """给界面看的配置状态。**Token 本身永不回传**，只说有没有、尾号四位。"""
    cfg = config()
    token = _s(cfg, "token")
    try:
        template = EXAMPLE_PATH.read_text(encoding="utf-8")
    except OSError:
        template = ""
    return {
        "ready": ready(cfg),
        "missing": missing(),
        "token_tail": token[-4:] if len(token) >= 8 else "",
        "url": _s(cfg, "url", DEFAULT_URL),
        "skill_version": _s(cfg, "skill_version", DEFAULT_SKILL_VERSION),
        "config_path": str(config_path()),
        "template": template,
        "auth_url": "https://meeting.tencent.com/ai-skill.html",
    }


# --------------------------------------------------------------------------
# JSON-RPC over HTTP
# --------------------------------------------------------------------------
def _headers(cfg: dict, *, session: str = "") -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        # Streamable HTTP 的服务端按这个 Accept 决定回 JSON 还是回 SSE。
        # 两个都写上，省得先猜再重试。
        "Accept": "application/json, text/event-stream",
        "X-Tencent-Meeting-Token": _s(cfg, "token"),
        "X-Skill-Version": _s(cfg, "skill_version", DEFAULT_SKILL_VERSION),
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    return headers


def _parse_body(raw: str, content_type: str, want_id: int | None) -> Any:
    """从响应体里取出 JSON-RPC 报文。

    SSE 帧长这样（可能有 event: / id: 行）：
        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}

    一帧一个报文，可能还夹杂心跳。挑 id 对得上的那一帧；对不上就取最后一个
    带 result/error 的 —— 服务端偶尔不回 id。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if "text/event-stream" in (content_type or "").lower() or text.startswith("event:") or text.startswith("data:"):
        picked: Any = None
        for block in re.split(r"\r?\n\r?\n", text):
            datas = [
                line[5:].strip()
                for line in block.splitlines()
                if line.startswith("data:")
            ]
            if not datas:
                continue
            payload = "\n".join(datas)
            if payload in ("", "[DONE]"):
                continue
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if want_id is not None and isinstance(msg, dict) and msg.get("id") == want_id:
                picked = msg
            elif picked is None and isinstance(msg, dict) and ("result" in msg or "error" in msg):
                picked = msg
        return picked
    try:
        return json.loads(text)
    except ValueError:
        return None


def _rpc(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    session: str = "",
    cfg: dict | None = None,
    notify: bool = False,
) -> dict[str, Any]:
    """发一个 JSON-RPC 报文。返回 {ok, result, session, error, code, detail}。

    永远不往外抛异常：对端超时、回 HTML、回 401 都在这里变成一句人话，
    上层接口不该因为腾讯会议抖一下就 500。
    """
    cfg = config() if cfg is None else cfg
    if not ready(cfg):
        return {"ok": False, "error": "not_configured",
                "detail": "腾讯会议还没配好：" + "、".join(missing()), "missing": missing()}

    url = _s(cfg, "url", DEFAULT_URL)
    msg_id = 0 if notify else int(time.time() * 1000) % 2_000_000_000
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if not notify:
        payload["id"] = msg_id
    if params is not None:
        payload["params"] = params

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_headers(cfg, session=session),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            ctype = resp.headers.get("Content-Type") or ""
            new_session = resp.headers.get("Mcp-Session-Id") or session
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except OSError:
            pass
        ctype = exc.headers.get("Content-Type") if exc.headers else ""
        msg = _parse_body(raw, ctype or "", None)
        detail = ""
        if isinstance(msg, dict):
            detail = str((msg.get("error") or {}).get("message") or "") if isinstance(msg.get("error"), dict) else ""
        if not detail:
            try:
                body = json.loads(raw)
                if isinstance(body, dict):
                    for key in ("show_msg", "message", "desc", "detail", "error"):
                        val = body.get(key)
                        if isinstance(val, str) and val.strip() and not val.strip().isdigit():
                            detail = val.strip()
                            break
            except ValueError:
                detail = ""
        if exc.code in (401, 403):
            return {"ok": False, "error": "wemeet_auth", "status": exc.code,
                    "detail": detail or "Token 被拒（401/403）：去授权页重新复制一个。"}
        return {"ok": False, "error": "wemeet_http", "status": exc.code,
                "detail": detail or f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": "wemeet_unreachable",
                "detail": f"连不上腾讯会议 MCP（{type(exc).__name__}）："
                          f"要么内网没放行 {url.split('/')[2]}，要么要走代理。"}

    if notify:
        return {"ok": True, "result": None, "session": new_session}

    msg = _parse_body(raw, ctype, msg_id)
    if not isinstance(msg, dict):
        return {"ok": False, "error": "wemeet_bad_response",
                "detail": f"不是 JSON-RPC 报文（Content-Type: {ctype or '?'}）：{raw[:160]}"}
    if isinstance(msg.get("error"), dict):
        err = msg["error"]
        return {"ok": False, "error": "wemeet_rpc", "code": err.get("code"),
                "detail": str(err.get("message") or err.get("data") or "服务端返回了错误")}
    return {"ok": True, "result": msg.get("result") or {}, "session": new_session}


# --------------------------------------------------------------------------
# 会话：initialize → initialized → tools/*
# --------------------------------------------------------------------------
def _open(cfg: dict) -> dict[str, Any]:
    """握手。返回 {ok, session, server}；会话 id 可能是空串（服务端无状态）。"""
    res = _rpc("initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "oc-gov", "version": "1.0"},
    }, cfg=cfg)
    if not res.get("ok"):
        return res

    session = str(res.get("session") or "")
    server = {}
    info = (res.get("result") or {}).get("serverInfo")
    if isinstance(info, dict):
        server = {"name": str(info.get("name") or ""), "version": str(info.get("version") or "")}

    # 通知报文没有 id、也不一定有响应体。失败了不拦：有的服务端不认这一步，
    # 但 tools/list 照样能过。
    _rpc("notifications/initialized", {}, cfg=cfg, session=session, notify=True)
    return {"ok": True, "session": session, "server": server}


def list_tools(*, cfg: dict | None = None, fresh: bool = False) -> dict[str, Any]:
    """列工具。带 2 分钟缓存（只缓存成功）。"""
    cfg = config() if cfg is None else cfg
    key = _s(cfg, "url") + "|" + _s(cfg, "token")[-8:]
    now = time.time()
    if not fresh and _tools_cache["key"] == key and now - _tools_cache["at"] < _TOOLS_TTL:
        return {"ok": True, "tools": _tools_cache["tools"],
                "server": _tools_cache["server"], "cached": True}

    opened = _open(cfg)
    if not opened.get("ok"):
        return opened

    res = _rpc("tools/list", {}, cfg=cfg, session=opened.get("session", ""))
    if not res.get("ok"):
        return res

    tools: list[dict[str, Any]] = []
    for item in (res.get("result") or {}).get("tools") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        tools.append({
            "name": name,
            "description": str(item.get("description") or ""),
            # 原样带出去给界面渲染表单；这里不改 schema，改了就对不上服务端
            "input_schema": item.get("inputSchema") or item.get("input_schema") or {},
        })
    tools.sort(key=lambda t: t["name"])

    _tools_cache.update({"key": key, "at": now, "tools": tools, "server": opened.get("server") or {}})
    return {"ok": True, "tools": tools, "server": opened.get("server") or {}, "cached": False}


def call_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """调一个工具。返回 {ok, text, payload, raw, is_error}。

    真服务端的返回是**三层**，这里替上层拆平：

        content[0].text = '{"_meta":{...},"data":{"status_code":200,"body":"<json>"}}'

    - `text`：原样的文本（界面要显示给人看）
    - `payload`：`data.body` 解析出来的业务对象（认会议号/原文只看它，
      不看信封 —— 信封里有 headers、trace id，混进来全是噪音）
    - `raw`：整条外层 JSON，给「原始 JSON」折叠区用
    """
    cfg = config()
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "error": "bad_param", "detail": "没给工具名。"}

    opened = _open(cfg)
    if not opened.get("ok"):
        return opened

    call_args = _with_client_info(name, args or {})

    res = _rpc("tools/call", {"name": name, "arguments": call_args},
               cfg=cfg, session=opened.get("session", ""))
    if not res.get("ok"):
        return res

    result = res.get("result") or {}
    chunks: list[str] = []
    for part in result.get("content") or []:
        if isinstance(part, dict):
            if part.get("type") == "text" and part.get("text"):
                chunks.append(str(part["text"]))
            elif part.get("type") == "resource" and isinstance(part.get("resource"), dict):
                chunks.append(str(part["resource"].get("text") or ""))
    text = "\n".join(c for c in chunks if c).strip()
    structured = result.get("structuredContent")
    return {
        "ok": True,
        "is_error": bool(result.get("isError")),
        "text": text,
        "payload": _payload_of(structured if structured is not None else text),
        "raw": structured if structured is not None else _json_or_text(text),
    }


def _json_or_text(text: str) -> Any:
    """文本能整段解析成 JSON 就是 JSON，否则原样返回。"""
    stripped = (text or "").strip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def _payload_of(node: Any) -> Any:
    """把 MCP 的信封剥到业务对象。

    真实返回长这样（text 里是一整段 JSON 字符串）：

        {"_meta": {...}, "data": {"status_code": 200, "headers": {...}, "body": "<json>"}}

    `body` 才是腾讯会议的 API 响应。信封留着只会让「认结果」把 headers 里的
    日期和 trace id 当会议原文，所以这里一路剥到底，剥不动就原样返回。
    """
    cur = node
    for _ in range(4):
        if isinstance(cur, str):
            s = cur.strip()
            if not s.startswith(("{", "[")):
                return cur
            try:
                cur = json.loads(s)
            except ValueError:
                return cur
            continue
        if isinstance(cur, dict) and "data" in cur and isinstance(cur.get("data"), (dict, str)):
            data = cur["data"]
            if isinstance(data, dict) and "body" in data:
                cur = data["body"]
                continue
            return cur
        return cur
    return cur


def _with_client_info(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """给声明了 `_client_info` 的工具自动补上客户端信息。

    服务端把它标成「必填-系统参数，由模型自动填入」——我们不是模型，就得自己填，
    否则调用会因缺参被拒。只在 schema 声明了才填，别往不认识的工具里塞字段。
    """
    if "_client_info" in args:
        return args
    try:
        tools = list_tools()
        schema = next((t["input_schema"] for t in tools.get("tools") or [] if t["name"] == name), {}) or {}
        props = schema.get("properties") or {}
    except Exception:  # noqa: BLE001 补参数失败不该让调用本身挂掉
        props = {}
    if "_client_info" not in props:
        return args
    out = dict(args)
    out["_client_info"] = {
        "os": platform.system() or "Windows",
        "agent": "oc-gov",
        "model": "oc-gov",
    }
    return out


# --------------------------------------------------------------------------
# 认结果：会议号 / 会议原文
# --------------------------------------------------------------------------
# 只认这几个键名。不认识的键一律不猜 —— 把 `id` 或 `code` 当会议号，
# 会往会议主档里写一个进不去的号。
_ONLINE_KEYS = {
    "meeting_code": "meeting_code",
    "meetingcode": "meeting_code",
    "meeting_no": "meeting_code",
    "join_url": "join_url",
    "joinurl": "join_url",
    "meeting_id": "meeting_id",
    "meetingid": "meeting_id",
    "password": "password",
}
_TEXT_KEYS = {"transcript", "transcripts", "minutes", "minute", "summary", "content", "text", "asr", "sentences"}


def _walk(node: Any, depth: int = 0):
    """深度优先遍历 JSON，吐出 (key, value)。"""
    if depth > 8:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k), v
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, depth + 1)


def extract_online(payload: Any) -> dict[str, str] | None:
    """从工具结果里认出「这场会的会议号 / 入会链接」。认不出返回 None。"""
    found: dict[str, str] = {}
    for key, value in _walk(payload):
        slot = _ONLINE_KEYS.get(key.replace("-", "_").lower())
        if not slot or slot in found:
            continue
        if isinstance(value, (str, int)) and str(value).strip():
            text = str(value).strip()
            # 纯数字才像会议号；URL 必须真的是 http(s)
            if slot == "join_url" and not text.startswith("http"):
                continue
            found[slot] = text
    if not (found.get("meeting_code") or found.get("join_url") or found.get("meeting_id")):
        return None
    return found


def extract_text(payload: Any, *, min_chars: int = 40) -> str:
    """从业务对象里认出「会议原文」：转写 / 智能纪要 / 正文。

    先按段落结构拼（`paragraphs[].sentences[].text`，带发言人）—— 转写详情就是
    这个形状，逐句取会碎成一句一段，逐段取才对。拼不出来再退回「够长的字符串」。
    """
    built = extract_transcript(payload)
    if built:
        return built

    parts: list[str] = []

    def take(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if len(text) >= min_chars and text not in parts:
                parts.append(text)
        elif isinstance(value, list):
            for v in value:
                take(v)
        elif isinstance(value, dict):
            for k in ("text", "content", "sentence", "summary"):
                if k in value:
                    take(value[k])

    for key, value in _walk(payload):
        if key.replace("-", "_").lower() in _TEXT_KEYS:
            take(value)
    return "\n\n".join(parts).strip()


def _find_list(node: Any, names: set[str], depth: int = 0) -> list | None:
    if depth > 8:
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in names and isinstance(v, list):
                return v
            found = _find_list(v, names, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_list(v, names, depth + 1)
            if found:
                return found
    return None


def extract_transcript(payload: Any) -> str:
    """把转写的段落拼成「发言人：正文」的逐段文本。认不出结构返回空串。"""
    paras = _find_list(payload, {"paragraphs", "paragraph_list"})
    if not paras:
        return ""
    lines: list[str] = []
    for p in paras:
        if not isinstance(p, dict):
            continue
        speaker = ""
        info = p.get("speaker_info") or p.get("speaker") or {}
        if isinstance(info, dict):
            speaker = str(info.get("user_name") or info.get("nick_name") or "")
        body = "".join(
            str(s.get("text") or "") for s in (p.get("sentences") or []) if isinstance(s, dict)
        )
        if not body:
            body = str(p.get("text") or "")
        body = body.strip()
        if body:
            lines.append(f"{speaker}：{body}" if speaker else body)
    return "\n".join(lines)


def extract_record_file_id(payload: Any) -> str:
    """找录制文件 ID。只认 `record_file_id` 这个键，别的一概不猜。"""
    for key, value in _walk(payload):
        if key.replace("-", "_").lower() == "record_file_id" and str(value or "").strip():
            return str(value).strip()
    return ""


# --------------------------------------------------------------------------
# 预设动作：建会 / 取纪要
# --------------------------------------------------------------------------
# 这两条是把「工具名 + 参数怎么拼」写下来的地方。写死是**故意的**：它们对参数
# 有硬要求（ISO 时间、pid、record_file_id 要先查），让通用面板去手填，等于把
# 一串串咒语推给人。工具不在清单里时下面的函数会明说，不会静默失效。
TOOL_SCHEDULE = "schedule_meeting"
TOOL_RECORDS = "get_records_list"
TOOL_TRANSCRIPT = "get_transcripts_details"
TOOL_SEARCH = "search_meetings"
TOOL_PARTICIPANTS = "get_meeting_participants"
TOOL_MINUTES = "get_smart_minutes"


def _meeting_start(meeting: dict[str, Any]) -> datetime | None:
    raw = str(meeting.get("held_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _iso(dt: datetime) -> str:
    """服务端要 ISO 8601（`2026-11-21T09:00:00+08:00`），不是时间戳。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _tools_guard(names: tuple[str, ...]) -> dict[str, Any] | None:
    """预设动作依赖的工具必须在清单里。缺了就明说缺哪个、现有的是哪些。"""
    res = list_tools()
    if not res.get("ok"):
        return res
    have = {t["name"] for t in res.get("tools") or []}
    lack = [n for n in names if n not in have]
    if lack:
        return {"ok": False, "error": "tool_missing",
                "detail": "服务端没有这些工具：" + "、".join(lack)
                          + "（现有：" + "、".join(sorted(have)[:12]) + "…）"}
    return None


def preset_schedule(meeting: dict[str, Any], *, minutes: int = 60) -> dict[str, Any]:
    """一键建会：会议主档的标题 + 时间 → `schedule_meeting`。"""
    guard = _tools_guard((TOOL_SCHEDULE,))
    if guard:
        return guard
    start = _meeting_start(meeting)
    if start is None:
        return {"ok": False, "error": "bad_time",
                "detail": "这场会没有可用的开始时间。腾讯会议要求起止时间，先把时间补上。"}
    end = start + timedelta(minutes=max(15, int(minutes or 60)))
    res = call_tool(TOOL_SCHEDULE, {
        "subject": str(meeting.get("title") or "会议"),
        "start_time": _iso(start),
        "end_time": _iso(end),
        "time_zone": "Asia/Shanghai",
    })
    if not res.get("ok"):
        return res
    payload = res.get("payload")
    return {
        "ok": True,
        "is_error": res.get("is_error"),
        "tool": TOOL_SCHEDULE,
        "text": res.get("text") or "",
        "payload": payload,
        "raw": res.get("raw"),
        "online": extract_online(payload) or {},
        "sent": {"subject": meeting.get("title"), "start_time": _iso(start), "end_time": _iso(end)},
    }


def preset_transcript(meeting: dict[str, Any]) -> dict[str, Any]:
    """一键取纪要：录制列表 → 转写详情。会议号/会议 ID 从主档的 online 里取。"""
    guard = _tools_guard((TOOL_RECORDS, TOOL_TRANSCRIPT))
    if guard:
        return guard
    online = meeting.get("online") or {}
    anchor = _meeting_start(meeting) or datetime.now()
    args: dict[str, Any] = {
        "start_time": _iso(anchor - timedelta(days=1)),
        "end_time": _iso(anchor + timedelta(days=31)),
    }
    if online.get("meeting_id"):
        args["meeting_id"] = str(online["meeting_id"])
    elif online.get("meeting_code"):
        args["meeting_code"] = str(online["meeting_code"])

    recs = call_tool(TOOL_RECORDS, args)
    if not recs.get("ok"):
        return recs
    payload = recs.get("payload")
    rfid = extract_record_file_id(payload)
    if not rfid:
        return {"ok": False, "error": "no_recording",
                "detail": "腾讯会议那边没有可用的录制文件（没开云录制，或者文件还在生成）。",
                "raw": recs.get("raw")}

    det = call_tool(TOOL_TRANSCRIPT, {"record_file_id": rfid, "pid": "0"})
    body = det.get("payload") if det.get("ok") else None
    text = extract_text(body) if body else ""
    source = "transcript"
    tool = TOOL_TRANSCRIPT

    # 没有转写、但有智能纪要：退一步，**但要说清这是摘要不是逐字稿**。
    # 摘要当原文用，引文闸核的还是这份摘要，条目会比逐字稿少 —— 这属于
    # 「来源不同」，不是「校验通过」，界面上必须显式标出来。
    if not text:
        mins = call_tool(TOOL_MINUTES, {"record_file_id": rfid})
        if mins.get("ok"):
            mbody = mins.get("payload")
            mtext = extract_text(mbody) or (mins.get("text") or "" if len(mins.get("text") or "") >= 40 else "")
            if mtext:
                text, body, source, tool = mtext, mbody, "minutes", TOOL_MINUTES

    if not text:
        return {"ok": False, "error": "no_transcript",
                "detail": "这场会有录制文件，但既没有转写也没有智能纪要（可能还在生成）。",
                "raw": (det.get("raw") or "")}

    return {
        "ok": True,
        "is_error": bool(det.get("is_error")),
        "tool": tool,
        "text": det.get("text") or "",
        "payload": body,
        "raw": det.get("raw"),
        "record_file_id": rfid,
        "transcript": text,
        "chars": len(text),
        "source": source,
    }


def extract_meetings(payload: Any) -> list[dict[str, Any]]:
    """从搜索结果里挑出会议行。只认「有 meeting_id/meeting_code 且有 subject」的对象。"""
    rows: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            if ("meeting_id" in node or "meeting_code" in node) and "subject" in node:
                rows.append(node)
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        mid = str(r.get("meeting_id") or "")
        code = str(r.get("meeting_code") or "")
        key = mid or code
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append({
            "subject": str(r.get("subject") or "（没有主题）"),
            "meeting_code": code,
            "meeting_id": mid,
            "start": str(r.get("scheduled_start_time") or r.get("start_time") or ""),
            "end": str(r.get("scheduled_end_time") or r.get("end_time") or ""),
            "status": str(r.get("status") or ""),
            "has_transcript": bool(r.get("has_transcript")),
            "has_minutes": bool(r.get("has_smart_minutes")),
            "has_recording": bool(r.get("has_recording")),
        })
    return out


def extract_people(payload: Any) -> list[str]:
    """从参会成员结果里挑出人名。只认 user_name / nick_name 这类字段。"""
    names: list[str] = []
    for key, value in _walk(payload):
        if key.lower() in ("user_name", "nick_name", "display_name") and str(value or "").strip():
            name = str(value).strip()
            if name not in names:
                names.append(name)
    return names


def preset_recent(days: int = 30) -> dict[str, Any]:
    """查最近的会（只读）。领导视角：最近一个月开过什么会、有没有纪要。"""
    guard = _tools_guard((TOOL_SEARCH,))
    if guard:
        return guard
    end = datetime.now()
    start = end - timedelta(days=max(1, min(365, int(days or 30))))
    res = call_tool(TOOL_SEARCH, {
        "from": _iso(start), "to": _iso(end), "page_size": 20, "is_compact": True,
    })
    if not res.get("ok"):
        return res
    payload = res.get("payload")
    meetings = extract_meetings(payload)
    return {
        "ok": True,
        "tool": TOOL_SEARCH,
        "meetings": meetings,
        "total": len(meetings),
        "payload": payload,
        "raw": res.get("raw"),
        "text": res.get("text") or "",
    }


def preset_participants(meeting_id: str) -> dict[str, Any]:
    """看某场会的参会人（只读）。"""
    guard = _tools_guard((TOOL_PARTICIPANTS,))
    if guard:
        return guard
    mid = str(meeting_id or "").strip()
    if not mid:
        return {"ok": False, "error": "bad_param", "detail": "没给腾讯会议的会议 ID。"}
    res = call_tool(TOOL_PARTICIPANTS, {"meeting_id": mid, "page_size": 100})
    if not res.get("ok"):
        return res
    payload = res.get("payload")
    names = extract_people(payload)
    return {
        "ok": True,
        "tool": TOOL_PARTICIPANTS,
        "people": names,
        "total": len(names),
        "payload": payload,
        "raw": res.get("raw"),
        "text": res.get("text") or "",
    }


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------
def probe(*, fresh: bool = True) -> dict[str, Any]:
    """握手 + 列工具。这是最轻的一次调用：不建会、不改任何东西。"""
    res = list_tools(fresh=fresh)
    if not res.get("ok"):
        return res
    tools = res.get("tools") or []
    server = res.get("server") or {}
    who = " ".join(x for x in (server.get("name"), server.get("version")) if x)
    return {
        "ok": True,
        "tools": tools,
        "tool_count": len(tools),
        "server": server,
        "detail": f"连接可用{('（' + who + '）') if who else ''}：这个服务端提供 {len(tools)} 个工具。",
    }
