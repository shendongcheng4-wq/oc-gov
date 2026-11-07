"""AI 市长工作台 —— 把指标罗盘、问数、督办预警拼到一个页面上。

只做聚合与转发，不产生任何数值：
- 罗盘数字   → workbench.kpis() / workbench.boards()  → mockdata.query_metric
- 问数对话   → core/metrics/ask.ask()                 → 模型解析 + 指标接口取数
- 追踪预警   → workbench.alerts()                     → oversight.scan()

HTTP 层只管三件事：校验入参、调 core、把异常映射成状态码。业务规则不写在这里。

会话与模型：
- 会话存在 core/chat.py，一轮结束一次性落库（流式过程中不写）
- 模型配置在 core/modelcfg.py，标识统一为 "provider/model"
- `/api/ask/stream` 是 SSE，推的是**真实阶段**（start / plan / done），
  不是把答案按字切开的假打字机 —— 模型只输出结构化计划，没有可读的正文流

会议助手：
- 会议主档与议定事项在 core/meeting/ledger.py，抽取校验在 core/meeting/extract.py
- 解析（parse）和落库（commit）是两个独立请求，中间隔着人工确认这一步
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import workbench as wb

APP = Path(__file__).resolve().parent
ROOT = APP.parent
WEB = APP / "web"
CORE = ROOT / "core"
METRICS_DIR = CORE / "metrics"
MEETING_DIR = CORE / "meeting"
CONFIG_ENV = ROOT / "config" / "local.env"

# 进行中轮次的「停止」开关。key = turn_id（每次流式请求生成一个），value = 登记时间。
#
# 为什么不用「写 socket 失败」来判断：客户端关页面和点停止在传输层是同一件事，
# 而这两者的语义**相反** —— 关页面要照常算完落库（断开不杀 turn），点停止要真的停。
# 靠 BrokenPipe 分不出来，所以改成前端显式调 /api/ask/cancel。
#
# 用时间戳而不是布尔：停止请求可能早于轮次登记、也可能晚于轮次结束，
# 留一个 TTL 窗让两端都能对上，过期的当作没发生，不会误杀下一轮。
_CANCELS: dict[str, float] = {}
_CANCEL_LOCK = threading.Lock()
CANCEL_TTL = 300.0


def _load_local_env() -> None:
    """读取 config/local.env；同名环境变量优先。"""
    if not CONFIG_ENV.exists():
        return
    try:
        lines = CONFIG_ENV.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_local_env()

sys.path.insert(0, str(CORE))
sys.path.insert(0, str(METRICS_DIR))
sys.path.insert(0, str(MEETING_DIR))
sys.path.insert(0, str(CORE / "oversight"))

import ask as ask_mod  # noqa: E402
import chat  # noqa: E402
import docparse  # noqa: E402
import extract as meeting_extract  # noqa: E402
import ledger as meetings  # noqa: E402
import llm  # noqa: E402
import llmclient  # noqa: E402
import memory as memory_mod  # noqa: E402
import modelcfg  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
import saved as saved_mod  # noqa: E402
import schedule as schedule_mod  # noqa: E402
import notify as notify_mod  # noqa: E402
import briefdoc as brief_mod  # noqa: E402
import proofread as proofread_mod  # noqa: E402
import wenshu as wenshu_mod  # noqa: E402
import docxgen as docx_mod  # noqa: E402
import knowledge as knowledge_mod  # noqa: E402
import experts as expert_mod  # noqa: E402
import meetplan as meetplan_mod  # noqa: E402
import rag as rag_mod  # noqa: E402
import harness as harness_mod  # noqa: E402
import auth  # noqa: E402
import audit  # noqa: E402
import authz  # noqa: E402
import credentials  # noqa: E402
import directory  # noqa: E402


def _plugin_command(question: str) -> tuple[str, str] | None:
    """识别「/插件名 参数」形式的显式插件调用。

    返回 (plugin_id, 参数文本)；不是插件命令返回 None。斜杠全角半角都认。
    只匹配「已启用」的插件，停用或未识别的都不拦，让问题照常走主链路。
    """
    q = (question or "").strip()
    if not q or (q[0] != "/" and q[0] != "／"):
        return None
    rest = q[1:].strip()
    if not rest:
        return None
    # 第一个空白之前是插件标识；之后是参数（可能为空）
    parts = rest.split(None, 1)
    token = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    for p in harness_mod.list_plugins():
        if not p.get("enabled", True):
            continue
        # 用 id、name 匹配；name 里可能带「·」或空格，也拿归一化后比一下
        name_norm = (p.get("name") or "").strip().lower().replace("·", "").replace(" ", "")
        if token == p["id"].lower() or token == name_norm or token == (p.get("name") or "").lower():
            return p["id"], arg
    return None


def _harness_fallback(question: str, result: dict) -> dict | None:
    """问数/知识库/资料库都没命中（refused）时，用 octop_harness 引擎兜底。

    只在装了 octop_harness 时才走这条路；没装或调用失败都返回 None，让上层
    保持原拒答结果不变。HarnessAgent 是异步的，这里用 asyncio.run 在请求线程
    里起一个独立事件循环跑（ThreadingHTTPServer 每请求一线程，互不干扰）。
    """
    if not result.get("refused"):
        return None
    if not harness_mod.available():
        return None
    try:
        import asyncio
        ans = asyncio.run(harness_mod.ask(question))
    except Exception:  # noqa: BLE001 —— 引擎兜底失败绝不打断主链路
        return None
    if not ans or not ans.get("text"):
        return None
    return {
        "refused": False,
        "question": question,
        "planner": "harness",
        "intent": "harness",
        "headline": ans["text"],
        "harness": {
            "plugin_count": ans.get("plugin_count", 0),
        },
        "sources": [],
        "notes": [],
        "trace": [
            {
                "step": len(result.get("trace") or []) + 1,
                "tool": "harness.ask",
                "call": f"HarnessAgent.call({question!r})",
                "status": "ok",
                "summary": "octop_harness 引擎兜底回答",
            }
        ],
    }


def _run_plugin_command(plugin_id: str, arg: str, question: str) -> dict | None:
    """执行「/插件名 参数」：只挂该插件的工具，把参数交给它回答。失败返回 None。"""
    if not harness_mod.available():
        return None
    try:
        import asyncio
        ans = asyncio.run(harness_mod.run_plugin(plugin_id, arg))
    except Exception:  # noqa: BLE001
        return None
    if not ans or not ans.get("text"):
        return None
    return {
        "refused": False,
        "question": question,
        "planner": "harness",
        "intent": "harness",
        "headline": ans["text"],
        "harness": {
            "plugin_id": plugin_id,
            "explicit": True,
        },
        "sources": [],
        "notes": [],
        "trace": [
            {
                "step": 1,
                "tool": "harness.run_plugin",
                "call": f"run_plugin({plugin_id!r}, {arg!r})",
                "status": "ok",
                "summary": f"显式调用插件 {plugin_id}",
            }
        ],
    }


def request_cancel(turn_id: str) -> None:
    turn_id = str(turn_id or "").strip()
    if not turn_id:
        return
    now = time.time()
    with _CANCEL_LOCK:
        # 顺手清过期的：这张表只有一个写入点，在这里清最省事
        for key in [k for k, ts in _CANCELS.items() if now - ts > CANCEL_TTL]:
            _CANCELS.pop(key, None)
        _CANCELS[turn_id] = now


def cancel_requested(turn_id: str) -> bool:
    with _CANCEL_LOCK:
        return turn_id in _CANCELS


def clear_cancel(turn_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCELS.pop(turn_id, None)


# ── 轮次事件日志（断线续流） ──────────────────────────────────────────────
#
# 以前每轮只往 socket 写 data:，没有 id:，也没留底 —— 手机切个网、笔记本合盖再
# 打开，前端就永远停在「正在解析」。turn 本身还在后台跑完并落库，但用户看到的
# 是一段死界面，只能在侧栏重新点一次会话才看得到答案。
#
# 现在每个事件都进这个内存表，并在 SSE 里带 `id: {turn_id}-{seq}`。前端断线后
# 从最后收到的 seq 接着拉，不必重算，也不会漏掉 done（答案就在 done 里）。
#
# 只放内存不落盘：一轮的事件顶多十几条、几 KB，服务重启后本来也没有「正在跑的
# 轮次」可续；需要看历史结果就走 /api/threads/<id>。
_TURNS: dict[str, dict] = {}
_TURN_LOCK = threading.Lock()
TURN_TTL = 600.0      # 一轮结束后事件留多久（够一次断线重连和切后台回来）
TURN_MAX = 40         # 同时留存的轮次数上限，防止长时间运行把内存吃满


def turn_open(thread_id: str, owner: str) -> str:
    turn_id = secrets.token_hex(8)
    now = time.time()
    with _TURN_LOCK:
        for key in [k for k, v in _TURNS.items() if now - v["ts"] > TURN_TTL]:
            _TURNS.pop(key, None)
        # 还是太多就丢最老的：续流是尽力而为，不能为了它把内存拖垮
        while len(_TURNS) >= TURN_MAX:
            _TURNS.pop(min(_TURNS, key=lambda k: _TURNS[k]["ts"]), None)
        _TURNS[turn_id] = {
            "thread_id": thread_id,
            "owner": owner,
            "events": [],
            "done": False,
            "ts": now,
        }
    return turn_id


def turn_push(turn_id: str, payload: dict) -> int:
    """记一条事件并返回它的 seq（1 起）。turn 已被清理时返回 0。"""
    with _TURN_LOCK:
        turn = _TURNS.get(turn_id)
        if turn is None:
            return 0
        turn["events"].append(payload)
        turn["ts"] = time.time()
        return len(turn["events"])


def turn_close(turn_id: str) -> None:
    with _TURN_LOCK:
        turn = _TURNS.get(turn_id)
        if turn is not None:
            turn["done"] = True
            turn["ts"] = time.time()


def turn_events(turn_id: str, after: int, owner: str) -> dict | None:
    """取 seq > after 的事件。turn 不存在或不属于这个人时返回 None。"""
    with _TURN_LOCK:
        turn = _TURNS.get(turn_id)
        if turn is None or (owner and turn["owner"] and turn["owner"] != owner):
            return None
        events = list(turn["events"])
        return {
            "thread_id": turn["thread_id"],
            "done": turn["done"],
            "last": len(events),
            "events": events[max(0, int(after)) :],
        }


EXAMPLES = [
    {"q": "今年1-9月全市规上工业增加值是多少？同比增速如何？", "tag": "图上这句"},
    {"q": "重大项目投资完成率有多少，跟时序进度比怎么样", "tag": "对比时序"},
    {"q": "12345 办结率最近三个月什么走势", "tag": "走势"},
    {"q": "哪个区县的项目推进最慢", "tag": "分解"},
    {"q": "今年财政收入增长多少", "tag": "已注册"},
    {"q": "全市 GDP 是多少", "tag": "总量"},
]


def _tail(path: str, prefix: str) -> str:
    """取路径最后一段，但不接受空值（/api/models/ 不算合法 id）。"""
    if not path.startswith(prefix):
        return ""
    rest = path[len(prefix):].lstrip("/")
    return rest if rest and "/" not in rest else ""


def _parts(path: str, prefix: str) -> list[str]:
    """按段切开，用于 /api/meetings/<id>/items/<iid> 这类多段路径。"""
    if not path.startswith(prefix):
        return []
    return [p for p in path[len(prefix):].split("/") if p]


def _expert_ids(body: dict) -> list[str]:
    """召唤了哪几位专家。传单个 `expert` 或数组 `experts` 都认，最多三位。"""
    raw = body.get("experts")
    if not isinstance(raw, list):
        single = str(body.get("expert") or "").strip()
        raw = [single] if single else []
    ids = [str(x).strip() for x in raw if str(x).strip()]
    return ids[:3]


_MAX_ATT_FILES = 5
_MAX_ATT_EACH = 12000      # 单份材料最多带多少字
_MAX_ATT_TOTAL = 30000     # 合计上限，超了就不再收

# 请求体上限。分两档：普通 JSON 和文件上传（base64 比原始字节胖约 33%）。
_MAX_JSON_BODY = 4 * 1024 * 1024
_MAX_UPLOAD_BODY = 16 * 1024 * 1024
_MAX_UPLOAD_FILE = 9 * 1024 * 1024     # 单个文件解码后的大小
_DRAIN_MAX = 24 * 1024 * 1024          # 超限时最多还愿意读掉多少字节再回 413


class _TooBig(Exception):
    """请求体超过上限。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"body 超过 {limit} 字节")


# ── 写接口登记表（(方法, 路径正则, 动作)）────────────────────────────────
# 为什么要有这张表：do_POST/do_PATCH/do_DELETE 是靠一串 if 路由的。闸门如果
# 也写成「想起来就在 handler 里调一次 _require」，那新加接口忘了调 = 默认
# 可匿名调用 —— 这正是这张表出现之前的样子（11 个写口裸着）。这里反过来：
# **表里没登记的写路径一律 403**，忘了登记得到的是「被拒」，不是「裸奔」。
#
# 第三列三档：
#   None                → 演示态放行。不登录也能用：问数、会话、工作台、日程、
#                         材料装配、文件解析。这是「双击打开就能演示」的底线，
#                         写死在这个白名单里，不靠默认值。
#   authz.GATE_SESSION  → 只要求登录。行级权限业务层 _require 再判一次 ——
#                         「能登录」不等于「能签收某个单位的事项」。
#   <动作名>            → 交给 authz.can()：配置类=市领导，资料库写=登录即可。
#
# 路径按 re.fullmatch 匹配，末尾斜杠会被规范化掉（/api/models/ 等同 /api/models）。
WRITE_POLICY: tuple[tuple[str, str, str | None], ...] = (
    # ── 演示态放行（PUBLIC）──
    ("POST", r"/api/auth/login", None),          # 登录自己当然不能要求先登录
    ("POST", r"/api/auth/logout", None),         # 幂等，且过期 sid 也要能退
    ("POST", r"/api/attachments/parse", None),   # 纯解析，不落盘
    ("POST", r"/api/ask", None),
    ("POST", r"/api/ask/stream", None),
    ("POST", r"/api/ask/cancel", None),          # handler 内按 turn 归属自查
    ("POST", r"/api/schedule/brief", None),
    ("POST", r"/api/briefdoc", None),
    ("POST", r"/api/briefdoc/docx", None),      # 只生成字节流，不落盘
    ("POST", r"/api/briefdoc/review", None),    # 只读：把材料回查一遍，不写存储
    ("POST", r"/api/wenshu", None),             # 文种稿：装配 + 体例检查，不落盘
    ("POST", r"/api/wenshu/docx", None),        # 只生成字节流
    ("POST", r"/api/wenshu/review", None),      # 只读：体例检查
    ("POST", r"/api/threads", None),
    ("POST", r"/api/workspace/(watch|follow|layout|feed-seen)", None),
    ("POST", r"/api/saved", None),
    ("POST", r"/api/notify/read", None),
    ("PATCH", r"/api/threads/[^/]+", None),      # handler 内按 owner 过滤
    ("DELETE", r"/api/threads/[^/]+", None),

    # ── 办公室务：登录 + 行级判定 ──
    ("POST", r"/api/oversight", authz.GATE_SESSION),
    ("POST", r"/api/oversight/urge", authz.GATE_SESSION),
    ("POST", r"/api/oversight/[^/]+/action", authz.GATE_SESSION),

    # ── 会议：市级动作，只有市领导 ──
    ("POST", r"/api/meetings", authz.MEETING_COMMIT),
    ("POST", r"/api/meetings/[^/]+/parse", authz.MEETING_COMMIT),
    ("POST", r"/api/meetings/[^/]+/commit", authz.MEETING_COMMIT),
    ("POST", r"/api/meetplan", authz.MEETING_COMMIT),
    ("PATCH", r"/api/meetings/[^/]+/items/[^/]+", authz.MEETING_EDIT),
    ("DELETE", r"/api/meetings/[^/]+", authz.MEETING_EDIT),

    # ── 配置类：全局共享状态，只有市领导 ──
    ("POST", r"/api/models", authz.CONFIG_MODEL),
    ("POST", r"/api/models/test", authz.CONFIG_MODEL),
    ("POST", r"/api/models/fetch", authz.CONFIG_MODEL),
    ("POST", r"/api/active-model", authz.CONFIG_MODEL),
    ("DELETE", r"/api/models/[^/]+", authz.CONFIG_MODEL),
    ("POST", r"/api/experts", authz.CONFIG_EXPERT),
    ("POST", r"/api/plugins/toggle", authz.CONFIG_PLUGIN),

    # ── 群发：真的会往外发消息，只有市领导 ──
    ("POST", r"/api/notify/dispatch", authz.NOTIFY_DISPATCH),
    ("POST", r"/api/notify/test", authz.NOTIFY_DISPATCH),

    # ── 共享资料库：读不限，写要登录 ──
    ("POST", r"/api/rag/bases", authz.KNOWLEDGE_WRITE),
    ("POST", r"/api/rag/bases/[^/]+/upload", authz.KNOWLEDGE_WRITE),
    ("DELETE", r"/api/rag/bases/[^/]+", authz.KNOWLEDGE_WRITE),
    ("DELETE", r"/api/rag/bases/[^/]+/doc", authz.KNOWLEDGE_WRITE),
)


def _match_policy(method: str, path: str) -> tuple[bool, str | None]:
    """查表。返回 (是否已登记, 动作)。动作 None = PUBLIC。"""
    norm = path.rstrip("/") or "/"
    for m, pattern, action in WRITE_POLICY:
        if m == method and re.fullmatch(pattern, norm):
            return True, action
    return False, None


def _attachments(body: dict) -> list[dict]:
    """校验并裁剪前端送来的参考材料。

    前端已经按同样的额度截过一遍，这里仍然要再截一次：`/api/ask/stream` 是
    公开接口，curl 直接塞一个 200MB 的字符串进来，前端那层挡不住。

    只留 name/text/chars/truncated。text 会进 planner 的 prompt，但**不进**
    会话记录（落库只写清单）—— 否则一份一万多字的材料会被每一轮上下文重复
    带一遍，几十轮之后光历史就能把 prompt 预算吃干净。
    """
    raw = body.get("attachments")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    total = 0
    for item in raw[:_MAX_ATT_FILES]:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        room = _MAX_ATT_TOTAL - total
        if room <= 0:
            break
        cap = min(_MAX_ATT_EACH, room)
        cut = len(text) > cap
        text = text[:cap]
        total += len(text)
        name = str(item.get("name") or "未命名").strip()[:120] or "未命名"
        out.append(
            {
                "name": name,
                "text": text,
                "chars": len(text),
                "truncated": bool(cut or item.get("truncated")),
            }
        )
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "OcGovWorkbench/0.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        # 注意：send_error() 也会回调到这里，此时 args[0] 是状态码（int），
        # 直接 `"/api/" in args[0]` 会抛 TypeError。所以先格式化成字符串再判断。
        try:
            line = fmt % args if args else str(fmt)
        except Exception:
            return
        if "/api/" in line:
            print(f"[workbench] {line}", flush=True)

    # ---------------- 基础设施 ----------------
    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self, max_bytes: int = _MAX_JSON_BODY) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > max_bytes:
            # 超限但不算离谱：把 body 读完再回 413。不读就关连接，Windows 会给
            # 对端发 RST，浏览器那边看到的是「连接被重置」而不是一句人话。
            # 真离谱的（几十上百 MB）不替它读，直接断。
            if length <= _DRAIN_MAX:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
            else:
                self.close_connection = True
            raise _TooBig(max_bytes)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _person(self, body: dict | None = None) -> str:
        """当前身份：session（可信）> 请求头/请求体/查询串（演示）> 默认。

        第三批的身份统一：有有效 session 就用 session 里的身份 —— 这是后端签发、
        前端改不了的。没有 session 才回落到旧的 X-Person-Id 头（演示身份，谁都能
        声称），保持「打开就能看」的体验。这样登录后，工作台/日程/通知/知识库/材料
        这些读口的身份就统一到不可伪造的 session 上，读收窄全面生效；未登录的
        纯演示浏览不受影响。

        写操作不走这里（走 _require → _session_scope），这里是读口的身份来源。
        """
        s = self._session_scope()
        if s is not None:
            pid = str(s.get("person") or "")
            return people.resolve(pid)
        pid = str(self.headers.get("X-Person-Id") or "").strip()
        if not pid and isinstance(body, dict):
            pid = str(body.get("person") or "").strip()
        if not pid:
            try:
                q = parse_qs(urlparse(self.path).query)
                pid = str((q.get("person") or [""])[0]).strip()
            except Exception:
                pid = ""
        return people.resolve(pid)

    def _session_scope(self) -> dict | None:
        """从请求里读 session，返回授权用的 scope（含 person/role/units）。

        凭据从两个头里找，按顺序：`Authorization: Bearer <sid>` 或 `X-Session: <sid>`。
        没有有效 session 返回 None —— 这和 `_person()` 的「回落默认身份」是本质区别：
        写操作需要的是「能证明的身份」，不是「前端声称的身份」。

        返回的 scope 是 directory.person_scope 的形状，直接喂 authz.can。
        """
        sid = ""
        auth_h = str(self.headers.get("Authorization") or "").strip()
        if auth_h.lower().startswith("bearer "):
            sid = auth_h[7:].strip()
        if not sid:
            sid = str(self.headers.get("X-Session") or "").strip()
        if not sid:
            return None
        s = auth.resolve(sid)
        if s is None:
            return None
        return directory.person_scope(s.get("person") or "")

    def _require(self, action: str, resource: dict | None = None) -> tuple[dict | None, bool, str]:
        """写操作鉴权闸门。返回 (scope, allowed, reason)。

        没有 session → (None, False, 未登录)。有 session 但越权 → (scope, False, reason)。
        调用方拿到 allowed=False 要立即 401/403，绝不落到业务层。
        """
        scope = self._session_scope()
        if scope is None:
            return None, False, "未登录：请先在右上角登录身份。"
        allowed, reason = authz.can(scope, action, resource)
        return scope, allowed, reason

    def _gate_write(self, method: str) -> dict | None:
        """写接口总闸门。返回 None = 放行；返回 dict = 调用方直接 403 回出去。

        这是**粗粒度**闸门：只回答「这个身份能不能碰这一类接口」。细粒度
        （能不能动这个单位的这一条）仍在业务层用 `_require(action, resource)`
        判 —— 闸门拿不到 units，硬把行级判定塞进来就变成两处各判一半。

        三个方法都在路由之前调它一次。没登记的写路径一律拒绝：新增接口必须
        先来这张表登记，忘了就是「被拒」，而不是「匿名可调」。
        """
        path = urlparse(self.path).path
        registered, action = _match_policy(method, path)
        if not registered:
            audit.user_event(
                None, "access.denied",
                target="write.unregistered",
                payload={"method": method, "path": path},
            )
            return {
                "ok": False,
                "error": f"未登记的写接口：{method} {path}",
                "code": "write_unregistered",
            }
        if action is None:
            return None  # 演示态放行

        scope, allowed, why = self._require(action)
        if allowed:
            return None
        audit.user_event(
            scope.get("person") if scope else None, "access.denied",
            target=action,
            payload={"method": method, "path": path, "reason": why},
        )
        return {"ok": False, "error": why, "code": "write_denied", "action": action}

    def _read_scope(self) -> tuple[str | None, set[str] | None]:
        """读收窄用的 (pid, scope)。和写操作不同，读是「有身份就收窄、没身份保持现状」。

        - 有有效 session：返回 (person_id, scope)。scope 是责任单位集合；
          市领导（is_citywide）返回 None = 全量，局长/分管领导返回 set(units)。
        - 没有 session：返回 (None, None) = 不收窄，保持演示全量现状。

        这样第一批「写口子必须 session」落地后，读这一批是渐进式：登录了的人
        只能看到分管范围的账，没登录的（纯演示浏览）仍能看到全量，不打断现有体验。
        """
        scope = self._session_scope()
        if scope is None:
            return None, None
        if scope.get("is_citywide"):
            return scope.get("person"), None
        units = set(scope.get("units") or ())
        return scope.get("person"), (units or None)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _delete_ok(self, ok: bool, **extra) -> None:
        self._json({"ok": ok, **extra})

    def _parse_attachments(self, body: dict) -> None:
        """把上传的 Office 文件转成纯文本，交回前端走原有的材料通道。

        为什么在服务端做：xlsx / docx 都是 zip + XML，前端要写一遍解压和格式还原，
        服务端再写一遍 —— 两份实现迟早跑偏，而且是「一边读出 7.2%、另一边读出
        0.072」这种最难发现的跑偏。这里只留一份。

        返回的是**文本**，不是答案：前端拿到之后和读本地 txt 的结果走同一条路，
        额度截断、进 planner、材料对照都不用改。
        """
        raw = body.get("files")
        if not isinstance(raw, list):
            one = {"name": body.get("name"), "data": body.get("data")}
            raw = [one] if body.get("data") else []
        if not raw:
            return self._json({"ok": False, "error": "no_file"}, 400)
        if len(raw) > _MAX_ATT_FILES:
            return self._json(
                {"ok": False, "error": "too_many_files", "limit": _MAX_ATT_FILES}, 400
            )

        items: list[dict] = []
        for it in raw[:_MAX_ATT_FILES]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("name") or "未命名").strip()[:120] or "未命名"
            b64 = it.get("data")
            if not isinstance(b64, str) or not b64.strip():
                items.append({"name": name, "ok": False, "error": "文件是空的"})
                continue
            # base64 比原始字节胖约 4/3，超长的不解码，省一次大内存分配
            if len(b64) > _MAX_UPLOAD_FILE * 4 // 3 + 4096:
                items.append({"name": name, "ok": False, "error": "文件超过 9MB"})
                continue
            try:
                blob = base64.b64decode(b64, validate=False)
            except (ValueError, TypeError):
                items.append(
                    {"name": name, "ok": False, "error": "base64 解不开，文件可能在传输中损坏"}
                )
                continue
            if len(blob) > _MAX_UPLOAD_FILE:
                items.append({"name": name, "ok": False, "error": "文件超过 9MB"})
                continue

            out = docparse.parse_book(blob, name, limit=_MAX_ATT_EACH)
            if not out.get("ok"):
                items.append(
                    {
                        "name": name,
                        "ok": False,
                        "error": out.get("error") or "解析失败",
                        "code": out.get("code") or "",
                    }
                )
                continue
            items.append(
                {
                    "name": name,
                    "ok": True,
                    "kind": out.get("kind") or "",
                    "text": out["text"],
                    "chars": out["chars"],
                    "truncated": out["truncated"],
                    "sheets": out.get("sheets") or [],
                    "rows": out.get("rows") or 0,
                    "paras": out.get("paras") or 0,
                    "tables": out.get("tables") or 0,
                    "note": out.get("note") or "",
                }
            )
        self._json({"ok": True, "items": items})

    # ---------------- GET ----------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            return self._serve_file(WEB / "index.html", "text/html; charset=utf-8")
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        try:
            if path == "/api/health":
                return self._json(
                    {
                        "ok": True,
                        "city": wb.CITY,
                        "metrics": wb.overview()["metric_count"],
                        "planner": llm.llm_info(),
                        "model_ready": modelcfg.available(),
                        "harness": harness_mod.status(),
                        "mock": True,
                    }
                )

            if path == "/api/plugins":
                return self._json({"ok": True, **harness_mod.status()})

            if path == "/api/overview":
                return self._json({"ok": True, **wb.overview()})

            if path == "/api/focus":
                # 民情焦点明细：罗盘卡只给标题和标签，弹窗要详情/工单/渠道/焦点词/走势。
                # 与 /api/overview 里的 focus 同源（都是 workbench.focus()），不会两处不一致。
                return self._json({"ok": True, **wb.focus()})

            if path == "/api/workspace":
                return self._json({"ok": True, **wb.workspace(self._person())})

            if path == "/api/people":
                return self._json(
                    {
                        "ok": True,
                        "people": people.all_people(),
                        "current": people.resolve(self._person()),
                        "max_watch": people.MAX_WATCH,
                        "demo": True,
                    }
                )

            if path == "/api/auth/whoami":
                return self._whoami()

            if path == "/api/experts":
                return self._json(
                    {
                        "ok": True,
                        "experts": expert_mod.all_experts(),
                        "max": expert_mod.MAX_PER_CALL,
                        "model_ready": expert_mod.llmclient.available(),
                        "rule": "专家只给解读，不报数字；解读里出现数字会被整段丢弃。"
                        "参谋会检索本域内的记录作为依据，但记录里的数字同样不许复述。",
                    }
                )

            # ---- 督办台账 ----
            # 读和写都在这一块：预警条上的每一条都能点开、能签收、能催办。
            # 风险等级不在这里算，全部来自 core/oversight 的 assess()。
            if path == "/api/oversight":
                q = parse_qs(urlparse(self.path).query)
                try:
                    window = max(0, min(30, int((q.get("window") or ["5"])[0])))
                except ValueError:
                    window = 5
                _pid, scope = self._read_scope()
                return self._json(
                    {
                        "ok": True,
                        "items": ov.list_items(
                            status=(q.get("status") or [""])[0] or None,
                            assignee=(q.get("assignee") or [""])[0] or None,
                            only_risk=(q.get("only_risk") or [""])[0] in ("1", "true", "yes"),
                            scope=scope,
                        ),
                        "stats": ov.stats(scope),
                        "scan": ov.scan(window, scope=scope),
                        "statuses": ov.STATUSES,
                        "today": ov.today().isoformat(),
                    }
                )

            parts = _parts(path, "/api/oversight/")
            if len(parts) == 1:
                _pid, scope = self._read_scope()
                one = ov.detail(parts[0], scope=scope)
                if one is None:
                    return self._json({"ok": False, "error": "item_not_found", "item_id": parts[0]}, 404)
                return self._json({"ok": True, "item": one})

            # ---- AI 日程 ----
            # 全部由已有时间派生（督办时限/状态、会议时间、披露日），这里不生成任何新事实。
            # 模型简报走单独的 POST：日程本身要先秒开，不能让模型拖住整个弹窗。
            if path == "/api/schedule":
                q = parse_qs(urlparse(self.path).query)
                try:
                    days = int((q.get("days") or [str(schedule_mod.WINDOW_DEFAULT)])[0])
                except ValueError:
                    days = schedule_mod.WINDOW_DEFAULT
                return self._json({"ok": True, **schedule_mod.build(self._person(), days)})

            # ---- 消息提醒 ----
            # 每次现算，不落盘：提醒是台账状态的投影，存一份就会有「面板说逾期、
            # 台账说已办结」这种对不上的画面。已读状态另存，只影响显示。
            if path == "/api/notify":
                return self._json(notify_mod.build(self._person()))

            # ---- 材料助手 ----
            # 材料是只读装配：数字带出处、事项带台账编号，服务端算好后前端只负责排版。
            # options 单独一个端点，是因为弹窗要先秒开配置项，不能让模型拖住。
            if path == "/api/briefdoc/options":
                return self._json(brief_mod.options(self._person()))

            # ---- 开文种：请示 / 通知 ----
            # 文种的可选项很少（两个文种、几类锚点），一次性给全比按文种分次取省事，
            # 也省掉「切文种要等一次网络」的停顿。
            if path == "/api/wenshu/options":
                return self._json(wenshu_mod.options())

            # ---- 知识库 ----
            # 只读检索：返回的是原文条目，不是模型生成的答案。
            if path == "/api/knowledge":
                qs = parse_qs(urlparse(self.path).query)
                q = str((qs.get("q") or [""])[0])
                try:
                    limit = max(1, min(20, int((qs.get("limit") or ["6"])[0])))
                except ValueError:
                    limit = 6
                hits = knowledge_mod.search(q, people.meta(self._person()), limit)
                return self._json(
                    {
                        "ok": True,
                        "query": q,
                        "total": len(hits),
                        "index_size": len(knowledge_mod.index(people.meta(self._person()))),
                        "hits": knowledge_mod.to_meta(hits),
                    }
                )

            # ---- 资料库（通用文档 RAG）----
            # 只读检索：返回的是上传文档里的原文片段，不是模型生成的答案。
            if path == "/api/rag":
                qs = parse_qs(urlparse(self.path).query)
                q = str((qs.get("q") or [""])[0]).strip()
                ids = [str(x).strip() for x in (qs.get("kb") or []) if str(x).strip()]
                try:
                    k = max(1, min(20, int((qs.get("k") or [str(rag_mod.DEFAULT_K)])[0])))
                except ValueError:
                    k = rag_mod.DEFAULT_K
                if not q:
                    return self._json({"ok": False, "error": "q_required"}, 400)
                if not ids:
                    ids = [b["id"] for b in rag_mod.list_bases()]
                hits = rag_mod.search(ids, q, k=k)
                return self._json(
                    {
                        "ok": True,
                        "query": q,
                        "total": len(hits),
                        "vector": rag_mod.embedding_available(),
                        "hits": hits,
                    }
                )

            if path == "/api/rag/bases":
                return self._json({"ok": True, "bases": rag_mod.list_bases(), "stats": rag_mod.stats()})

            if path == "/api/rag/bases/new":
                return self._json({"ok": True, "stats": rag_mod.stats()})

            parts = _parts(path, "/api/rag/bases/")
            if len(parts) == 1:
                base = rag_mod.get_base(parts[0])
                if base is None:
                    return self._json({"ok": False, "error": "base_not_found"}, 404)
                return self._json(
                    {
                        "ok": True,
                        "base": {
                            "id": base["id"],
                            "name": base["name"],
                            "description": base.get("description") or "",
                            "created_at": base.get("created_at"),
                        },
                        "documents": rag_mod.list_documents(base["id"]),
                    }
                )
            if len(parts) == 2 and parts[1] == "search":
                qs = parse_qs(urlparse(self.path).query)
                q = str((qs.get("q") or [""])[0]).strip()
                if not q:
                    return self._json({"ok": False, "error": "q_required"}, 400)
                hits = rag_mod.search([parts[0]], q)
                return self._json({"ok": True, "query": q, "total": len(hits), "hits": hits})

            if path.startswith("/api/metric/"):
                code = path.rsplit("/", 1)[-1].upper()
                detail = wb.metric_detail(code)
                if detail is None:
                    return self._json({"ok": False, "error": "unknown_metric", "code": code}, 404)
                return self._json({"ok": True, **detail})

            if path == "/api/ask/examples":
                return self._json({"ok": True, "items": EXAMPLES})

            # 断线续流：取某一轮已经产生、但客户端还没收到的事件。
            # 优先级：`?after=N` > 标准头 `Last-Event-ID: turn-N` > 从头给。
            # 支持标准头是因为浏览器 EventSource 重连时只发这个，不认自定义参数。
            turn_id = _tail(path, "/api/ask/turn/")
            if turn_id:
                qs = parse_qs(urlparse(self.path).query)
                after = str((qs.get("after") or [""])[0]).strip()
                if not after:
                    last_id = self.headers.get("Last-Event-ID") or ""
                    after = last_id.rsplit("-", 1)[-1] if "-" in last_id else ""
                try:
                    after_n = max(0, int(after or 0))
                except ValueError:
                    after_n = 0
                found = turn_events(turn_id, after_n, self._person())
                if found is None:
                    # 过期、或者不是自己的轮次 —— 两种情况回同一个错，别让人拿 id
                    # 挨个试就能探出「哪些轮次存在过」。前端按 404 收场即可。
                    return self._json(
                        {"ok": False, "error": "turn_not_found", "turn_id": turn_id}, 404
                    )
                return self._json({"ok": True, "turn_id": turn_id, **found})

            # ---- 模型配置 ----
            if path == "/api/models":
                providers = modelcfg.list_providers()
                resolved = modelcfg.resolved()
                return self._json(
                    {
                        "ok": True,
                        "providers": providers,
                        "resolved": resolved,
                        "active": modelcfg.active_ref(),
                        "ready": bool(resolved),
                    }
                )

            if path == "/api/models/resolved":
                # 选择器专用，不含任何 provider 内部信息
                return self._json({"ok": True, "items": modelcfg.resolved(), "active": modelcfg.active_ref()})

            # ---- 会话 ----
            if path == "/api/threads":
                qs = parse_qs(urlparse(self.path).query)
                q = str((qs.get("q") or [""])[0]).strip()
                # 只回当前身份的会话。以前不分身份，切到副市长看到的还是市长
                # 那一屏 —— 演示能糊过去，接权限就是事故。
                pid = self._person()
                return self._json(
                    {
                        "ok": True,
                        "query": q,
                        "owner": pid,
                        "items": chat.list_threads(q or None, owner=pid),
                        "stats": chat.stats(pid),
                    }
                )

            tid = _tail(path, "/api/threads/")
            if tid:
                thread = chat.get(tid, owner=self._person())
                if thread is None:
                    return self._json({"ok": False, "error": "thread_not_found"}, 404)
                # 回放时补「这张草稿卡已经建过会」——否则刷新后又变成可点按钮
                meetplan_mod.annotate(thread["id"], thread.get("messages") or [])
                return self._json(
                    {"ok": True, "thread": thread, "usage": chat.usage_total(thread)}
                )

            # ---- 会议助手 ----
            if path == "/api/meetings":
                return self._json(
                    {
                        "ok": True,
                        "items": meetings.list_meetings(),
                        "stats": meetings.stats(),
                        "units": meeting_extract.known_units(),
                        "model_ready": modelcfg.available(),
                    }
                )

            parts = _parts(path, "/api/meetings/")
            if len(parts) == 1:
                one = meetings.get(parts[0])
                if one is None:
                    return self._json({"ok": False, "error": "meeting_not_found"}, 404)
                return self._json({"ok": True, "meeting": one, "units": meeting_extract.known_units()})

            self.send_error(404)
        except Exception as exc:  # noqa: BLE001 统一兜住，别让异常变成空响应
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        limit = _MAX_UPLOAD_BODY if path == "/api/attachments/parse" else _MAX_JSON_BODY
        try:
            body = self._body(limit)
        except _TooBig as exc:
            return self._json(
                {"ok": False, "error": "body_too_large", "limit": exc.limit}, 413
            )

        # 闸门在路由之前：先判「这类接口你能不能碰」，再进具体 handler 判
        # 「这一条你能不能动」。顺序不能反 —— 反了就是先干活再拦人。
        denied = self._gate_write("POST")
        if denied is not None:
            return self._json(denied, 403)

        try:
            if path == "/api/auth/login":
                return self._login(body)

            if path == "/api/auth/logout":
                return self._logout()

            if path == "/api/attachments/parse":
                return self._parse_attachments(body)

            if path == "/api/ask":
                return self._ask(body)

            if path == "/api/ask/stream":
                return self._ask_stream(body)

            # 停止一轮。只登记标记，真正的停发生在 ask 的下一个步骤边界；
            # 返回 ok 只代表"收到了"，不代表"已经停了"——界面不该等这个结果。
            if path == "/api/ask/cancel":
                turn_id = str(body.get("turn_id") or "").strip()
                if not turn_id:
                    return self._json({"ok": False, "error": "turn_id_required"}, 400)
                # 只允许停自己的轮次。turn_id 随机且短命，但「能停别人的」这件事
                # 没有任何正当代价 —— 别处都在按身份隔离，这里留个口子没道理。
                seen = turn_events(turn_id, 0, self._person(body))
                if seen is None:
                    return self._json({"ok": False, "error": "turn_not_found"}, 404)
                request_cancel(turn_id)
                return self._json({"ok": True})

            if path == "/api/plugins/toggle":
                plugin_id = str(body.get("id") or "").strip()
                enabled = bool(body.get("enabled"))
                if not plugin_id:
                    return self._json({"ok": False, "error": "id_required"}, 400)
                # 只允许切换「已经识别到的」插件，避免把任意字符串写进状态文件
                known = {p["id"] for p in harness_mod.list_plugins()}
                if plugin_id not in known:
                    return self._json({"ok": False, "error": "plugin_not_found"}, 404)
                harness_mod.set_enabled(plugin_id, enabled)
                return self._json({"ok": True, "id": plugin_id, "enabled": enabled})

            # 专家名单整体替换（增/删/改一体）。前端把编辑后的完整名单 POST 回来，
            # 后端校验字段后整体落盘。id 留空 = 新增（这里给个兜底 id），
            # 名单里缺了谁 = 删除。
            if path == "/api/experts":
                items = body.get("experts")
                if not isinstance(items, list):
                    return self._json({"ok": False, "error": "experts_required"}, 400)
                if len(items) > 20:
                    return self._json({"ok": False, "error": "too_many"}, 400)
                saved = expert_mod.save_experts(items)
                return self._json({"ok": True, "experts": saved})

            if path == "/api/models":
                provider = modelcfg.upsert_provider(body)
                return self._json(
                    {
                        "ok": True,
                        "provider": next((p for p in modelcfg.list_providers() if p["id"] == provider.get("id")), None),
                        "resolved": modelcfg.resolved(),
                        "active": modelcfg.active_ref(),
                    }
                )

            if path == "/api/models/test":
                return self._json(modelcfg.probe(body))

            if path == "/api/models/fetch":
                return self._json(modelcfg.fetch_models(body))

            if path == "/api/active-model":
                ref = str(body.get("ref") or "").strip()
                if not ref:
                    return self._json({"ok": False, "error": "ref_required"}, 400)
                modelcfg.set_active(ref)
                return self._json({"ok": True, "active": modelcfg.active_ref()})

            # ---- 资料库（通用文档 RAG）----
            if path == "/api/rag/bases":
                name = str(body.get("name") or "").strip()
                if not name:
                    return self._json({"ok": False, "error": "name_required"}, 400)
                base = rag_mod.create_base(name, body.get("description") or "")
                return self._json({"ok": True, "base": base, "bases": rag_mod.list_bases()})

            parts = _parts(path, "/api/rag/bases/")
            if len(parts) == 2 and parts[1] == "upload":
                base = rag_mod.get_base(parts[0])
                if base is None:
                    return self._json({"ok": False, "error": "base_not_found"}, 404)
                name = str(body.get("name") or "未命名").strip()[:120] or "未命名"
                b64 = body.get("data")
                if not isinstance(b64, str) or not b64.strip():
                    return self._json({"ok": False, "error": "文件是空的"}, 400)
                if len(b64) > rag_mod.MAX_DOC_BYTES * 4 // 3 + 4096:
                    return self._json({"ok": False, "error": "文件超过 9MB"}, 400)
                try:
                    blob = base64.b64decode(b64, validate=False)
                except (ValueError, TypeError):
                    return self._json({"ok": False, "error": "base64 解不开，文件可能在传输中损坏"}, 400)
                if len(blob) > rag_mod.MAX_DOC_BYTES:
                    return self._json({"ok": False, "error": "文件超过 9MB"}, 400)
                try:
                    doc = rag_mod.upload_document(parts[0], name, blob, content_type=str(body.get("content_type") or ""))
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                except LookupError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 404)
                return self._json({"ok": True, "doc": doc, "documents": rag_mod.list_documents(parts[0])})

            # ---- 会话 ----
            if path == "/api/threads":
                thread = chat.create(
                    title=body.get("title"),
                    model_ref=body.get("model_ref") or modelcfg.active_ref(),
                    # 归属必须在这里就给：漏了的话新会话 owner=""，谁都读不到，
                    # 建完立刻 404 —— 比不隔离更让人摸不着头脑
                    owner=self._person(body),
                )
                return self._json({"ok": True, "thread": thread})

            # ---- 个人工作台：关注列表整份替换 ----
            # 校验全在 core/people.set_watch 里，这里只把中文原因映射成 400。
            if path == "/api/workspace/watch":
                result = people.set_watch(self._person(body), body.get("codes"))
                if not result.get("ok"):
                    return self._json(result, 400)
                return self._json({"ok": True, **result, "person_meta": people.meta(result["person"])})

            # ---- 个人工作台：手动跟进清单整份替换 ----
            # 和关注列表一样的形状：前端手里有完整清单，整份写是幂等的。
            if path == "/api/workspace/follow":
                result = people.set_follow(self._person(body), body.get("ids"))
                if not result.get("ok"):
                    return self._json(result, 400)
                return self._json({"ok": True, **result, "person_meta": people.meta(result["person"])})

            # ---- 个人工作台：收藏 / 取消收藏一条回答 ----
            # 只收 (thread_id, message_id)，正文由 core/saved.py 回 chats 取 ——
            # 信前端传来的正文，收藏夹就能塞进任何一句话，那就不是"依据"了。
            if path == "/api/saved":
                action = str(body.get("action") or "add").strip()
                if action == "remove":
                    result = saved_mod.remove(self._person(body), body.get("message_id"))
                else:
                    result = saved_mod.add(self._person(body), body.get("thread_id"), body.get("message_id"))
                if not result.get("ok"):
                    return self._json(result, 400)
                return self._json({"ok": True, **result})

            # ---- 个人工作台：板块顺序与显隐 ----
            if path == "/api/workspace/layout":
                result = people.set_layout(self._person(body), body.get("blocks"))
                if not result.get("ok"):
                    return self._json(result, 400)
                return self._json({"ok": True, **result, "person_meta": people.meta(result["person"])})

            # ---- 个人工作台：把提醒标为已读 ----
            # 不传 keys 就是"当前这几条全读了"。已读只是显示状态，不改任何数据口径，
            # 所以这里不做权限判断（本来也没有权限）。
            if path == "/api/workspace/feed-seen":
                pid = self._person(body)
                posted = body.get("keys")
                if not isinstance(posted, list) or not posted:
                    posted = [f["key"] for f in wb.workspace(pid)["feed"]]
                result = people.mark_seen(pid, posted)
                if not result.get("ok"):
                    return self._json(result, 400)
                ws = wb.workspace(pid)
                return self._json({"ok": True, **result, "unread": ws["unread"], "feed": ws["feed"]})

            # ---- 消息提醒：标已读 ----
            # 和工作台的 feed-seen 分开存：那边是「关注动态」，这边是「待办提醒」，
            # 混在一个列表里，标了这边那边也变已读，人会对不上自己到底看过什么。
            if path == "/api/notify/read":
                pid = self._person(body)
                result = notify_mod.mark_read(pid, body.get("ids"), all_=body.get("all") is True)
                if not result.get("ok"):
                    return self._json(result, 400)
                view = notify_mod.build(pid)
                return self._json({"ok": True, **result, "unread": view["unread"], "total": view["total"]})

            # ---- 消息提醒：现在执行一次巡检 ----
            # 走全部闸门（静默时段、重复批次、日限额）。这里不提供 force：
            # 页面没有登录态，一个能强制群发的按钮等于把责任单位的群交给任何人。
            if path == "/api/notify/dispatch":
                return self._json(notify_mod.dispatch())

            # ---- 消息提醒：通道连通性测试 ----
            # 发一条固定文案，不掺业务内容。配好通道后先点这个，别拿真提醒试错。
            if path == "/api/notify/test":
                return self._json(notify_mod.test_channel(str(body.get("channel") or "webhook")))

            # ---- 督办台账 ----
            if path == "/api/oversight":
                # 建档（交办）是写操作，必须登录 + 有权限。资源单位取 assignee ∪ co_assignees。
                co = [str(x) for x in (body.get("co_assignees") or []) if str(x).strip()]
                assignee = str(body.get("assignee") or "").strip()
                scope, allowed, why = self._require(
                    authz.OVERSIGHT_CREATE, {"units": tuple([assignee] + co)}
                )
                if not allowed:
                    audit.user_event(
                        scope.get("person") if scope else None, "access.denied",
                        target="oversight.create", payload={"reason": why},
                    )
                    return self._json({"ok": False, "error": why}, 403)
                res = ov.create_item(
                    title=str(body.get("title") or ""),
                    assignee=assignee,
                    deadline=str(body.get("deadline") or ""),
                    standard=str(body.get("standard") or ""),
                    source=str(body.get("source") or "市政府办交办"),
                    source_ref=str(body.get("source_ref") or ""),
                    co_assignees=co,
                    priority=str(body.get("priority") or "一般"),
                    # actor 由登录身份定，不再信前端字符串
                    actor=scope["person"],
                )
                audit.user_event(scope["person"], "oversight.create",
                                 target=res.get("item_id"), payload={"ok": res.get("ok")})
                return self._json(res, 200 if res.get("ok") else 400)

            if path == "/api/oversight/urge":
                return self._urge(body)

            # ---- AI 日程：今日简报 ----
            # 模型只写一句重点和几条理由；事项、时间全部由服务端重算，前端传什么都不作数。
            if path == "/api/schedule/brief":
                try:
                    days = int(body.get("days") or schedule_mod.WINDOW_DEFAULT)
                except (TypeError, ValueError):
                    days = schedule_mod.WINDOW_DEFAULT
                return self._json(
                    schedule_mod.brief(self._person(body), days, use_model=body.get("use_model") is not False)
                )

            # ---- 材料助手：装配 ----
            # 骨架、章节、期次、指标都由请求指定，但每处数字仍由服务端重新查一遍，
            # 前端传进来的只有「选什么」，传不进「写什么」。
            if path == "/api/briefdoc":
                return self._json(
                    brief_mod.compose(
                        self._person(body),
                        kind=str(body.get("kind") or "report"),
                        period=(str(body["period"]) if body.get("period") else None),
                        sections=body.get("sections"),
                        codes=body.get("codes"),
                        meeting_id=(str(body["meeting_id"]) if body.get("meeting_id") else None),
                        use_model=body.get("use_model") is not False,
                    )
                )

            parts = _parts(path, "/api/oversight/")
            if len(parts) == 2 and parts[1] == "action":
                return self._advance(parts[0], body)

            # ---- 材料助手：出 .docx ----
            # 两个出口（Markdown / Word）共用同一次 compose()。如果各自查一遍数，
            # 迟早出现「纸面 8.9、Word 里 8.5」——这类不一致比排版难看严重得多。
            # 这里只做排版，不取数、不校验事实。
            if path == "/api/briefdoc/docx":
                source = str(body.get("source") or "compose")
                if source == "bulletin":
                    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                    if not str(fields.get("task") or "").strip():
                        return self._json({"ok": False, "error": "缺 task：红头公文必须写明督办事项"}, 400)
                    data = brief_mod.bulletin_docx(fields)
                    day = str(fields.get("date_text") or "").strip() or "draft"
                    name = f"督办通报-{day}.docx"
                elif isinstance(body.get("meta"), dict) and isinstance(body.get("blocks"), list):
                    # 纸面回灌：前端把刚刚「服务端算出来的」那份 meta/blocks 原样送回来排版。
                    # 为什么允许：材料是投影，导出的 Word 必须和屏幕上一字不差；重新调一次
                    # 模型会让「下一步」那段换一种说法，用户看到的就是「文件跟纸面不一样」。
                    # 这里不产生任何事实，只把这些字段排进 docx，所以不存在「前端改数」的风险。
                    try:
                        data = brief_mod.to_docx(body["meta"], body["blocks"])
                    except (KeyError, TypeError, AttributeError, ValueError):
                        return self._json({"ok": False, "error": "blocks 结构不对，无法排版"}, 400)
                    name = brief_mod.docx_filename(body["meta"])
                else:
                    res = brief_mod.compose(
                        self._person(body),
                        kind=str(body.get("kind") or "report"),
                        period=(str(body["period"]) if body.get("period") else None),
                        sections=body.get("sections"),
                        codes=body.get("codes"),
                        meeting_id=(str(body["meeting_id"]) if body.get("meeting_id") else None),
                        use_model=body.get("use_model") is not False,
                    )
                    if not res.get("ok"):
                        return self._json(res, 400)
                    data = brief_mod.to_docx(res["meta"], res["blocks"])
                    name = brief_mod.docx_filename(res["meta"])
                return self._json({
                    "ok": True,
                    "filename": name,
                    "bytes": len(data),
                    "docx": docx_mod.to_base64(data),
                })

            # ---- 材料助手：审校 ----
            # 审的是「当前纸面」，所以收的是前端手上的 meta/blocks，而不是重新 compose：
            # 重新装配会得到另一份材料，审出来的问题跟用户看着的那一份对不上。
            # 只读：不写任何存储，也不改请求里的内容。
            if path == "/api/briefdoc/review":
                meta = body.get("meta")
                blocks = body.get("blocks")
                if not isinstance(meta, dict) or not isinstance(blocks, list):
                    return self._json({"ok": False, "error": "审校要传 meta 和 blocks"}, 400)
                try:
                    res = proofread_mod.review(meta, blocks)
                except (KeyError, TypeError, AttributeError, ValueError) as exc:
                    return self._json({"ok": False, "error": f"材料结构不对，无法审校（{type(exc).__name__}）"}, 400)
                if not res.get("ok"):
                    return self._json(res, 400)
                return self._json(res)

            # ---- 开文种：装配 ----
            # 前端只交「要素」和「勾了哪几个编号」。事实句由服务端按编号重新取一遍，
            # 所以前端传不进一段自己编的话——这是文种稿唯一要紧的防线。
            if path == "/api/wenshu":
                fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                return self._json(
                    wenshu_mod.build(
                        str(body.get("kind") or "qingshi"),
                        fields,
                        use_model=body.get("use_model") is not False,
                    )
                )

            # ---- 开文种：出 .docx ----
            # 和材料出口同一个立场：把刚排好的那份 doc 原样回灌，
            # 不重新装配。重新装配会让文件里的措辞和屏幕上的对不上，
            # 而这份稿子是要打印出来送签的。
            if path == "/api/wenshu/docx":
                meta = body.get("meta")
                doc = body.get("doc")
                if not isinstance(meta, dict) or not isinstance(doc, dict):
                    return self._json({"ok": False, "error": "导出要传 meta 和 doc"}, 400)
                try:
                    data = wenshu_mod.to_docx(meta, doc)
                except (KeyError, TypeError, AttributeError, ValueError):
                    return self._json({"ok": False, "error": "doc 结构不对，无法排版"}, 400)
                return self._json({
                    "ok": True,
                    "filename": wenshu_mod.filename(meta),
                    "bytes": len(data),
                    "docx": docx_mod.to_base64(data),
                })

            # ---- 开文种：体例检查 ----
            # 审的是屏幕上这一份。和材料审校一样只读，不改请求里的内容。
            if path == "/api/wenshu/review":
                meta = body.get("meta")
                doc = body.get("doc")
                if not isinstance(meta, dict) or not isinstance(doc, dict):
                    return self._json({"ok": False, "error": "体例检查要传 meta 和 doc"}, 400)
                try:
                    res = wenshu_mod.review(meta, doc)
                except (KeyError, TypeError, AttributeError, ValueError) as exc:
                    return self._json({"ok": False, "error": f"文稿结构不对，无法检查（{type(exc).__name__}）"}, 400)
                return self._json(res)

            # ---- 对话里约会议：确认草稿才落库 ----
            # 单独一个写口，不并进 POST /api/meetings：那条是「粘纪要建记录」，
            # 正文必填；这条是「确认草稿建待召开的会」，正文必须为空。
            # 两个语义塞一个入口，将来谁看一眼都分不清哪个字段对哪种会有效。
            if path == "/api/meetplan":
                # 对话里约会议是写操作，需登录 + 领导权限。
                scope, allowed, why = self._require(authz.MEETING_COMMIT)
                if not allowed:
                    audit.user_event(scope.get("person") if scope else None, "access.denied",
                                     target="meeting.commit", payload={"reason": why})
                    return self._json({"ok": False, "error": why}, 403)
                draft = body.get("draft") or {}
                if not isinstance(draft, dict):
                    return self._json({"ok": False, "error": "draft 必须是对象"}, 400)
                res = meetplan_mod.commit(
                    draft,
                    thread_id=str(body.get("thread_id") or ""),
                    message_id=str(body.get("message_id") or ""),
                    # 谁约的记成身份职务（「市长」），不是 internal id：
                    # 这条要显示在会议详情里给人看，myor/mayor 那种值没用
                    actor=str(body.get("actor") or people.meta(scope["person"]).get("post") or ""),
                )
                audit.user_event(scope["person"], "meeting.commit",
                                 target=(res.get("meeting") or {}).get("id") if res.get("ok") else None,
                                 payload={"ok": res.get("ok")})
                if not res.get("ok"):
                    return self._json(res, 400)
                res["meeting"] = meetings.brief(res["meeting"])
                res["items"] = meetings.list_meetings()
                return self._json(res, 200)

            # ---- 会议助手 ----
            if path == "/api/meetings":
                # 建会议（粘纪要）是写操作，需登录 + 领导权限。
                scope, allowed, why = self._require(authz.MEETING_COMMIT)
                if not allowed:
                    audit.user_event(scope.get("person") if scope else None, "access.denied",
                                     target="meeting.create", payload={"reason": why})
                    return self._json({"ok": False, "error": why}, 403)
                meeting = meetings.create(
                    title=str(body.get("title") or ""),
                    text=str(body.get("text") or ""),
                    held_at=str(body.get("held_at") or ""),
                    host=str(body.get("host") or ""),
                    attendees=body.get("attendees") or [],
                    source=str(body.get("source") or "manual"),
                )
                audit.user_event(scope["person"], "meeting.create",
                                 target=meeting.get("id"), payload={"ok": True})
                return self._json({"ok": True, "meeting": meetings.brief(meeting)})

            parts = _parts(path, "/api/meetings/")
            if len(parts) == 2 and parts[1] == "parse":
                return self._parse_meeting(parts[0], body)
            if len(parts) == 2 and parts[1] == "commit":
                # 议定事项落库也是写操作，需登录 + 领导权限。
                scope, allowed, why = self._require(authz.MEETING_COMMIT)
                if not allowed:
                    audit.user_event(scope.get("person") if scope else None, "access.denied",
                                     target="meeting.commit_items", payload={"reason": why})
                    return self._json({"ok": False, "error": why}, 403)
                result = meetings.commit(parts[0], body.get("item_ids") or [], actor=scope["person"])
                audit.user_event(scope["person"], "meeting.commit_items",
                                 target=parts[0], payload={"ok": result.get("ok")})
                return self._json(result, 200 if result.get("ok") else 409)

            self.send_error(404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- DELETE ----------------
    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        denied = self._gate_write("DELETE")
        if denied is not None:
            return self._json(denied, 403)
        try:
            pid = _tail(path, "/api/models/")
            if pid:
                existing = next((p for p in modelcfg.list_providers(True) if p["id"] == pid), None)
                if existing is None:
                    return self._delete_ok(False, error="provider_not_found")
                removed = modelcfg.delete_provider(pid)
                touched = modelcfg.prune_thread_refs(existing["name"])
                return self._delete_ok(removed, threads_reset=touched, active=modelcfg.active_ref())

            tid = _tail(path, "/api/threads/")
            if tid:
                # 先判存在 + 归属：chat.delete 对不存在的 id 也返回 True（"删完确实没有"），
                # 直接透出会让前端把"别人已经删了"显示成"删除成功"，列表白等一次刷新
                if chat.get(tid, owner=self._person()) is None:
                    return self._json({"ok": False, "error": "thread_not_found"}, 404)
                removed = chat.delete(tid, owner=self._person())
                # 会话没了，指向它的收藏也得清 —— 否则收藏夹里留点点开是空白的条目
                if removed:
                    saved_mod.purge_thread(tid)
                return self._delete_ok(removed)

            parts = _parts(path, "/api/meetings/")
            if len(parts) == 1:
                return self._delete_ok(meetings.delete(parts[0]))

            # ---- 资料库：删文档（库/文档两级）----
            parts = _parts(path, "/api/rag/bases/")
            if len(parts) == 1:
                return self._delete_ok(rag_mod.delete_base(parts[0]))
            if len(parts) == 2 and parts[1] == "doc":
                # 文档 id 在查询串里：/api/rag/bases/<kb>/doc?doc=<id>
                qs = parse_qs(urlparse(self.path).query)
                doc_id = str((qs.get("doc") or [""])[0]).strip()
                if not doc_id:
                    return self._delete_ok(False, error="doc_required")
                return self._delete_ok(rag_mod.delete_document(parts[0], doc_id))

            self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- PATCH ----------------
    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        denied = self._gate_write("PATCH")
        if denied is not None:
            return self._json(denied, 403)
        try:
            # ---- 会话重命名 ----
            tid = _tail(path, "/api/threads/")
            if tid:
                title = str(body.get("title") or "").strip()
                if not title:
                    return self._json({"ok": False, "error": "empty_title"}, 400)
                pid = self._person(body)
                if not chat.rename(tid, title, owner=pid):
                    return self._json({"ok": False, "error": "thread_not_found"}, 404)
                # 回显截断后的标题：MAX_TITLE=24，前端要按实际值改 DOM，
                # 否则它会显示自己提交的全文，刷新后才突然变短
                return self._json(
                    {"ok": True, "id": tid, "title": (chat.get(tid, owner=pid) or {}).get("title")}
                )

            parts = _parts(path, "/api/meetings/")
            if len(parts) == 3 and parts[1] == "items":
                result = meetings.update_item(parts[0], parts[2], body)
                if not result.get("ok"):
                    return self._json({"ok": False, "error": result.get("error")}, 404 if "not_found" in str(result.get("error")) else 409)
                return self._json({"ok": True, "item": result["item"]})
            self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- 督办台账 ----------------
    def _advance(self, iid: str, body: dict) -> None:
        """单条流转。守卫规则全在 core/oversight，这里只整形入参、映射状态码。

        不在这里复刻「什么状态能做什么」——两份规则一定会漂移，届时页面允许
        而接口拒绝，或者反过来跳过守卫。

        鉴权闸门在业务守卫之前：先判「这个人能不能对这个单位做这个动作」，
        再判「这个状态能不能做这个动作」。两道都过才落数据。
        """
        item = ov.get(iid)
        if item is None:
            return self._json({"ok": False, "error": "item_not_found", "item_id": iid}, 404)

        action = str(body.get("action") or "").strip()
        if not action:
            one = ov.detail(iid) or {}
            allowed = one.get("allowed_actions") or []
            return self._json(
                {
                    "ok": False,
                    "error": "action_required",
                    "detail": f"要执行哪个操作？当前可选：{'、'.join(allowed) or '（无可执行操作）'}",
                    "allowed_actions": allowed,
                },
                400,
            )

        # 动作 → 授权动作名（authz.ACTION_FROM_ZH），资源单位 = 主办 ∪ 协办。
        # 签收/进展/办结按「主办单位」判（不能替协办签收）；催办/退回按「涉及单位」判。
        az_action = authz.ACTION_FROM_ZH.get(action)
        if az_action is None:
            return self._json({"ok": False, "error": f"未注册的操作：{action}"}, 400)
        resource_units = tuple([item.assignee] + list(item.co_assignees))
        scope, allowed, why = self._require(az_action, {"units": resource_units})
        if not allowed:
            audit.user_event(
                scope.get("person") if scope else None, "access.denied",
                target=az_action, payload={"item_id": iid, "reason": why},
            )
            return self._json({"ok": False, "error": why}, 403)

        text = str(body.get("text") or "").strip()
        reason = str(body.get("reason") or "").strip()
        # 界面上只有一个输入框（"办理说明 / 退回理由"），用户不会区分 text 和 reason。
        # 补齐由 core/oversight.advance 统一做，这里不再复制一份，免得两处规则漂移。

        evidence = body.get("evidence") or []
        if isinstance(evidence, str):
            evidence = re.split(r"[,，、;；\n]", evidence)
        evidence = [str(x).strip() for x in evidence if str(x).strip()]

        result = ov.advance(
            iid,
            action,
            actor=scope["person"],  # actor 由登录身份定，不再信前端
            text=text,
            reason=reason,
            evidence=evidence,
            percent=body.get("percent"),
        )
        audit.user_event(scope["person"], az_action,
                         target=iid, payload={"ok": result.get("ok")})
        if not result.get("ok"):
            return self._json(result, 409)
        return self._json({**result, "item": ov.detail(iid), "stats": ov.stats()})

    def _urge(self, body: dict) -> None:
        """批量催办。

        和会议 commit 相反，这里**不整体回滚**：催办只是往事件流里记一条提醒，
        部分成功没有副作用；硬要原子性反而会让能催的也催不出去。逐条报结果，
        失败的写清原因，由调用方决定要不要重试。

        鉴权：催办是写操作，需登录；逐条按「涉及单位」判，非分管单位的不催。
        """
        scope = self._session_scope()
        if scope is None:
            return self._json({"ok": False, "error": "未登录：请先在右上角登录身份。"}, 401)

        ids = [str(i) for i in (body.get("ids") or []) if str(i).strip()]
        if not ids:
            return self._json({"ok": False, "error": "没有选择要催办的事项。"}, 400)
        if len(ids) > 200:
            return self._json({"ok": False, "error": f"一次最多催办 200 条，收到 {len(ids)} 条。"}, 400)

        actor = scope["person"]  # 由登录身份定，不再信前端
        custom = str(body.get("text") or "").strip()
        done: list[dict] = []
        failed: list[dict] = []

        for iid in ids:
            one = ov.detail(iid)
            if one is None:
                failed.append({"item_id": iid, "error": "没有这件事项。"})
                continue
            # 先判授权，再判状态
            units = tuple([one.get("assignee") or ""] + [str(x) for x in (one.get("co_assignees") or [])])
            allowed, why = authz.can(scope, authz.OVERSIGHT_URGE, {"units": units})
            if not allowed:
                audit.user_event(scope["person"], "access.denied",
                                 target=authz.OVERSIGHT_URGE, payload={"item_id": iid, "reason": why})
                failed.append({"item_id": iid, "error": why})
                continue
            if "催办" not in (one.get("allowed_actions") or []):
                failed.append({"item_id": iid, "error": f"当前状态「{one.get('status')}」不能催办。"})
                continue
            # 默认文案带上 assess() 算出来的真实理由，不是套模板；这些字由代码拼，
            # 模型没有参与，所以不存在"催办内容里的数字被编造"的可能。
            reasons = one.get("risk_reasons") or []
            text = custom or (f"{'；'.join(reasons)}，请加快办理并反馈进展。" if reasons else "请反馈当前办理进展和下一步安排。")
            res = ov.advance(iid, "催办", actor=actor, text=text)
            if res.get("ok"):
                audit.user_event(scope["person"], "oversight.urge", target=iid, payload={"ok": True})
                done.append({"item_id": iid, "assignee": one.get("assignee"), "text": text})
            else:
                failed.append({"item_id": iid, "error": res.get("error")})

        return self._json(
            {
                "ok": True,
                "urged": done,
                "failed": failed,
                "count": len(done),
                "stats": ov.stats(),
                "scan": ov.scan(),
            }
        )

    # ---------------- 认证 ----------------
    def _login(self, body: dict) -> None:
        """登录：白名单身份 + 正确口令才签发 session。

        body: {"person": "mayor", "password": "..."}。返回 {ok, session} 或
        {ok:false, error}。口令校验走 credentials.verify，空口令/错口令一律拒绝。
        返回的 session 含 person/role/units，前端存 sid，写操作和读收窄都凭它。
        """
        pid = str(body.get("person") or "").strip()
        password = str(body.get("password") or "")
        if not pid:
            return self._json({"ok": False, "error": "person_required"}, 400)
        if not password:
            return self._json({"ok": False, "error": "password_required"}, 400)
        s = auth.login(pid, password)
        if s is None:
            # 不区分「没有这个身份」和「口令错」，避免泄露身份是否存在。
            audit.user_event(pid if people.find(pid) else None, "auth.login_failed")
            return self._json({"ok": False, "error": "身份或口令不正确"}, 401)
        audit.user_event(pid, "auth.login", payload={"role": s.get("role")})
        return self._json({"ok": True, "session": s})

    def _whoami(self) -> None:
        """当前会话身份。前端用它判断是否已登录、显示谁。无 session 返回未登录。"""
        s = self._session_scope()
        if s is None:
            return self._json({"ok": True, "authed": False})
        return self._json(
            {
                "ok": True,
                "authed": True,
                "person": s.get("person"),
                "role": s.get("role"),
                "units": list(s.get("units") or ()),
            }
        )

    def _logout(self) -> None:
        """登出：吊销当前 session。"""
        sid = ""
        auth_h = str(self.headers.get("Authorization") or "").strip()
        if auth_h.lower().startswith("bearer "):
            sid = auth_h[7:].strip()
        if not sid:
            sid = str(self.headers.get("X-Session") or "").strip()
        if not sid:
            return self._json({"ok": False, "error": "no_session"}, 400)
        s = auth.resolve(sid)
        if s:
            audit.user_event(s.get("person") or "", "auth.logout")
        auth.revoke(sid)
        return self._json({"ok": True})

    # ---------------- 会议助手 ----------------
    def _parse_meeting(self, mid: str, body: dict) -> None:
        """抽取议定事项。

        模型不可用时**直接 503，不退规则**。这里和问数不一样：问数能用规则兜底是
        因为它只做指标匹配，错了最多答非所问；纪要抽取要产出"责任单位 + 时限"，
        规则猜出来的东西会直接变成督办台账里的硬约束，比没有更危险。
        """
        meeting = meetings.get(mid)
        if meeting is None:
            return self._json({"ok": False, "error": "meeting_not_found"}, 404)
        if not modelcfg.available():
            return self._json(
                {
                    "ok": False,
                    "error": "model_unavailable",
                    "detail": "没有可用的模型，无法抽取议定事项。请先在模型配置里添加服务。",
                },
                503,
            )
        try:
            parsed = meeting_extract.extract(
                meeting.get("text") or "",
                title=meeting.get("title") or "",
                held_at=meeting.get("held_at") or "",
                attendees=meeting.get("attendees") or [],
                model_ref=body.get("model_ref") or meeting.get("model_ref"),
            )
        except llmclient.ModelError as exc:
            return self._json({"ok": False, "error": "model_error", "detail": str(exc)}, 502)

        saved = meetings.save_parse(mid, parsed)
        if not saved.get("ok"):
            return self._json({"ok": False, "error": saved.get("error")}, 409)
        return self._json(
            {
                "ok": True,
                "meeting": meetings.get(mid),
                "warnings": parsed.get("warnings") or [],
                "usage": parsed.get("usage") or {},
            }
        )

    # ---------------- 问数 ----------------
    def _prepare(self, body: dict) -> tuple[dict, list, str, dict, list]:
        """建/取会话、取历史、落用户消息。返回 (thread, history, question, ctx, atts)。

        `ctx` 是本轮上下文的裁剪统计（总几轮、带了几轮、裁掉几轮）。它会一路
        传到回答的 meta 里 —— 界面必须把「哪几轮已经不在上下文」说出来。
        `atts` 是本轮随问题带上来的参考材料（已裁剪），只喂 planner，不落库正文。
        """
        question = str(body.get("question") or "").strip()
        if not question:
            raise ValueError("empty_question")
        atts = _attachments(body)

        thread = chat.ensure(
            body.get("thread_id"), model_ref=body.get("model_ref"), owner=self._person(body)
        )

        # 会话粘性：新会话先钉住当前默认模型，后续轮次沿用（对应 octop 的
        # threads.model_ref）。这样用户之后改了全局默认，老会话不会被悄悄换掉。
        pinned = body.get("model_ref") or thread.get("model_ref")
        if not pinned:
            pinned = modelcfg.active_ref()
        if pinned and pinned != thread.get("model_ref"):
            chat.set_model_ref(thread["id"], pinned)
            thread = chat.get(thread["id"]) or thread

        # 先取历史再落本条，否则当前问题会在 history 和 messages 里各出现一次。
        # 预算按会话钉住那个模型的窗口算（chat.history_budget），系统段用量把本轮
        # system 文本一起递进去 —— 界面要能显示「系统占 1100、历史占 850」，
        # 只报一个总数看不出大头其实是固定的系统提示。
        ctx: dict = {}
        conn = modelcfg.resolve(pinned)
        window = (conn or {}).get("context_window")
        system_text = llm.system_prompt_text()

        # 材料要按窗口再裁一遍：_attachments 那层「合计 3 万字」是请求体安全线，
        # 跟模型是谁无关。8k 的内网模型上，3 万字中文约 1.9 万 token，一份就能
        # 把窗口撑爆，而失败会被当成普通模型故障最终退回规则解析 —— 用户只看到
        # 答案突然变糙，不知道是自己传的文件太大。裁完的占用要从历史里扣，
        # 两者抢的是同一个窗口。
        atts, att_tokens = chat.clip_materials(atts, window)

        def _read_history() -> list[dict[str, str]]:
            # 出参是同一个 dict，重取前先清空，否则上一次的 dropped/tokens 会残留
            ctx.clear()
            return chat.context(
                thread["id"],
                out=ctx,
                window=window,
                system_text=system_text,
                reserved=att_tokens,
            )

        history = _read_history()

        # 重试：可能停在两种状态上，都要认成"同一轮重来"而不是新的一问：
        #   1) 上一轮失败：只落了 user 消息，最后一条就是这次的问题；
        #   2) 上一轮被用户停掉：最后一条是「已停止生成」占位，它前面才是这次的问题。
        # 不这么判的话，重试按钮会污染会话记录（同一条问题连问两遍）。
        msgs = thread.get("messages") or []
        last = msgs[-1] if msgs else None
        prev = msgs[-2] if len(msgs) >= 2 else None
        same_q = lambda m: (m.get("content") or "").strip() == question if m else False
        is_retry = bool(body.get("retry")) and last is not None and (
            (last.get("role") == "user" and same_q(last))
            or (
                last.get("role") == "assistant"
                and (last.get("meta") or {}).get("cancelled")
                and prev is not None
                and prev.get("role") == "user"
                and same_q(prev)
            )
        )
        if is_retry:
            if last.get("role") == "assistant":
                # 「已停止」占位让位给这次真正的答案
                chat.drop_last(thread["id"])
                # 重取一次：上一条已经撤了，统计要跟着变，否则会多算一整轮
                history = _read_history()
            if history and history[-1].get("role") == "user":
                history = history[:-1]
        else:
            chat.append(thread["id"], "user", question)

        # 跨会话记忆：新会话（或隔了很久）开问时，把长期关注背景拼在历史前面，
        # 让模型规划查询时知道领导平时盯哪些指标/维度/主题。只含名称不含数值，
        # 与 chat.context 的边界一致；没装记忆库时返回空列表，不改变原行为。
        # 放在 retry/append 之后拼，是为了不干扰上面的重试裁剪逻辑。
        mem_bg = memory_mod.background(question)
        if mem_bg:
            history = mem_bg + history

        return thread, history, question, ctx, atts

    def _persist_answer(self, thread_id: str, result: dict) -> dict | None:
        """落一轮回答。

        meta 里存 sources/notes/facts 是为了**历史消息能重新渲染成同样的样式**；
        不存 trace 是因为回看历史不需要调用链路，省一半体积。给模型用的上下文
        只取 metric_names/intent（见 core/chat.py），这些字段不会进模型。

        唯一的例外是 usage：它不在 trace 里另存，而是单独算一份放 meta。trace
        可以不看，账不能不算 —— 不存的话，这一轮花掉多少 token 就永久丢了。
        """
        info = result.get("planner_info") or {}
        # 专家解读要跟着落库，否则回看历史时那段凭空消失 —— 现在专家是「对话中」
        # 的常驻角色，缺失比以前的临时勾选更刺眼。裁剪规则见 experts.to_meta：
        # 只留回放字段。模型上下文回灌的是 headline 原文（core/chat.py），
        # 所以这些回放字段只要不混进 content，就不会进模型。
        views = expert_mod.to_meta(result.get("expert_views"))
        return chat.append(
            thread_id,
            "assistant",
            str(result.get("headline") or ""),
            meta={
                "refused": bool(result.get("refused")),
                "planner": result.get("planner"),
                # 这一轮是不是降级出来的（换模型 / 退回规则）。不落库的话，
                # 刷新之后答案还在、提示没了 —— 用户会以为这就是模型给的结论。
                "degraded": result.get("degraded"),
                "intent": result.get("intent"),
                # detail 是「为什么没答 / 这个数怎么读」的补充说明。不落库的话，
                # 实时看有、刷新就没 —— 拒答那条尤其明显：只剩一句干巴巴的"没有对应指标"
                "detail": result.get("detail"),
                "model": info.get("model"),
                "metric_codes": result.get("metric_codes") or [],
                "metric_names": result.get("metric_names") or [],
                "sources": result.get("sources") or [],
                "notes": result.get("notes") or [],
                "facts": result.get("facts") or [],
                # 走势图要跟着落库：回看历史时图突然消失、只剩一行字，
                # 是比"从来没有图"更奇怪的体验。序列最多 24 期，体积可接受。
                "series": result.get("series") or [],
                "series_title": result.get("series_title"),
                # 拒答时给的可问清单也要落：刷新之后那 11 个胶囊凭空消失，
                # 用户会以为系统"这次答不上就说没了"。每条约 40 字，可控。
                "suggestions": (result.get("suggestions") or [])[:12],
                "knowledge": knowledge_mod.to_meta(result.get("knowledge")),
                "rag": result.get("rag") or [],
                "harness": result.get("harness") or {},
                "expert_views": views,
                # 建会草稿要跟着落库：回放时要能重新画出那张卡（含当时的假设与缺项），
                # 只留一句 headline 的话，用户回看时不知道自己当时确认了什么
                "meet_draft": meetplan_mod.to_meta(result.get("meet_draft")),
                # 用量：调用链（trace）不落库，如果这里也不存，本轮花掉多少 token
                # 就永久丢失了 —— 回看历史、做预算都无从统计。几十字节，值得存。
                # 上下文统计跟着落库：刷新后用量行还在，说明也该还在
                "context": result.get("context") or {},
                # 附件清单（只有名字和字数，正文不进历史）：回看时说得清
                # "这条结论当时参考了什么"，而不用把一万字材料存两遍
                "attachments": result.get("attachments") or [],
                # 材料值 × 库值的对照（B 档）。必须落库：用户拿材料来核对的
                # 那次，往往就是他最想回头再看一眼的一次
                "material_compare": result.get("material_compare") or [],
                "usage": result.get("usage") or {},
            },
        )

    def _ask(self, body: dict) -> None:
        try:
            thread, history, question, ctx, atts = self._prepare(body)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

        # 「/插件名 参数」：显式调用插件，优先于问数/知识库/资料库链路。
        # known_metrics 带 owner：跨会话记忆按身份取（昨天问过的指标今天接着问），
        # 见 chat.known_metrics —— 只传 thread_id 时退回本会话。
        plugin_cmd = _plugin_command(question)
        if plugin_cmd is not None:
            plugin_id, arg = plugin_cmd
            result = _run_plugin_command(plugin_id, arg, question)
            if result is None:
                # 引擎不可用或调用失败：回落到主链路，别把这条命令静默吞掉
                result = ask_mod.ask(
                    question,
                    history=history,
                    model_ref=thread.get("model_ref"),
                    expert_ids=_expert_ids(body),
                    person_id=self._person(body),
                    known_metrics=chat.known_metrics(
                        thread["id"], owner=self._person(body)
                    ),
                    attachments=atts,
                )
                result = _harness_fallback(question, result) or result
        else:
            result = ask_mod.ask(
                question,
                history=history,
                model_ref=thread.get("model_ref"),
                expert_ids=_expert_ids(body),
                person_id=self._person(body),
                known_metrics=chat.known_metrics(
                    thread["id"], owner=self._person(body)
                ),
                attachments=atts,
            )
            # 问数/知识库/资料库都没命中时，用 octop_harness 引擎（含插件工具）兜底。
            # 只在装了 harness 且结果 refused 时才走，否则保持原结果不变。
            fb = _harness_fallback(question, result)
            if fb is not None:
                result = fb
        result["planner_info"] = llm.llm_info()
        # 本轮上下文的裁剪统计。前端在用量行里写出来，让「它怎么把刚才的忘了」
        # 有一个可核对的解释，而不是让人怀疑系统不稳定。
        result["context"] = ctx
        # 落库只留清单，不留全文：材料正文进 prompt 就够，进历史是纯占地
        result["attachments"] = [
            {"name": a["name"], "chars": a["chars"], "truncated": a["truncated"]}
            for a in atts
        ]
        msg = self._persist_answer(thread["id"], result)
        # 跨会话记忆：一轮结束就把「关注了什么」沉淀进去（指标/维度/主题），
        # 只记名称不记数值。失败静默，绝不影响这轮回答的返回。
        memory_mod.record(thread["id"], question, result)
        self._json(
            {
                "ok": True,
                "thread_id": thread["id"],
                "message_id": (msg or {}).get("id"),
                "seq": (msg or {}).get("seq"),
                **result,
            }
        )

    def _ask_stream(self, body: dict) -> None:
        try:
            thread, history, question, ctx, atts = self._prepare(body)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

        # 这一轮的身份证。前端点「停止」时带着它来取消 —— 只认这个，
        # 不认 socket 断没断（关页面和点停止在传输层长得一样，见 _CANCELS 的说明）。
        # 同时它也是续流的句柄：断线后前端拿它回放已经产生的事件。
        # 必须在发响应头之前就建好，下面 X-Turn-Id 要用。
        turn_id = turn_open(thread["id"], self._person(body))

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        # 也放在响应头里：万一第一个事件块就丢了，前端仍然知道该拿哪个 id 去续流。
        # 只靠事件里的 start，断在最前面那一瞬间就彻底接不上了。
        self.send_header("X-Turn-Id", turn_id)
        self.end_headers()
        self.close_connection = True

        def push(payload: dict) -> None:
            # 先记日志再写 socket：断线时这一步就是唯一能把结果留下的地方。
            # 没有它，重连只能靠「再问一遍」，而那一遍又要重新烧 token。
            seq = turn_push(turn_id, payload)
            frame = f"id: {turn_id}-{seq}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            try:
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                # 客户端中途关页面时 wfile 会抛 BrokenPipe。这一轮仍然跑完并落库
                # （对应 octop 的「断开不杀 turn」语义），只是没人接了，不该因此报错。
                pass

        push(
            {
                "type": "start",
                "turn_id": turn_id,
                "thread_id": thread["id"],
                "model_ref": thread.get("model_ref"),
                "turns": len(history) // 2,
            }
        )

        def step(name: str, data: dict) -> None:
            push({"type": name, **data})

        try:
            result = ask_mod.ask(
                question,
                history=history,
                model_ref=thread.get("model_ref"),
                on_step=step,
                expert_ids=_expert_ids(body),
                person_id=self._person(body),
                known_metrics=chat.known_metrics(thread["id"], owner=self._person(body)),
                attachments=atts,
                should_stop=lambda: cancel_requested(turn_id),
            )
            # 问数/知识库/资料库都没命中时，用 octop_harness 引擎兜底（同 _ask）
            fb = _harness_fallback(question, result)
            if fb is not None:
                result = fb
            result["planner_info"] = llm.llm_info()
            # 本轮上下文的裁剪统计。前端在用量行里写出来，让「它怎么把刚才的忘了」
            # 有一个可核对的解释，而不是让人怀疑系统不稳定。
            result["context"] = ctx
            # 落库只留清单，不留全文：材料正文进 prompt 就够，进历史是纯占地
            result["attachments"] = [
                {"name": a["name"], "chars": a["chars"], "truncated": a["truncated"]}
                for a in atts
            ]
            msg = self._persist_answer(thread["id"], result)
            memory_mod.record(thread["id"], question, result)
            push(
                {
                    "type": "done",
                    "thread_id": thread["id"],
                    "message_id": (msg or {}).get("id"),
                    "seq": (msg or {}).get("seq"),
                    "result": result,
                }
            )
        except ask_mod.TurnCancelled as exc:
            # 停止也要留痕：只落一条 user 提问、没有任何回答，刷新后像是"卡住了"。
            # 落一句明确的「已停止」，并把已经花掉的 token 记上（模型那一步是计费的）。
            msg = chat.append(
                thread["id"],
                "assistant",
                "已停止生成，这一轮没有产出答案。",
                meta={"cancelled": True, "usage": exc.usage or {}},
            )
            push(
                {
                    "type": "cancelled",
                    "turn_id": turn_id,
                    "thread_id": thread["id"],
                    "message_id": (msg or {}).get("id"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            push({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            # 记下「这一轮结束了」。续流的一端靠这个字段决定还要不要继续追问 ——
            # 只按「有没有新事件」判断的话，跑完那一刻会多等一个轮询周期。
            turn_close(turn_id)
            # 这一轮结束了，取消标记留着没有意义；不清的话下次同 key 的请求
            # （虽然 turn_id 是随机的）会白查一次表
            clear_cancel(turn_id)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090

    # 口令引导：代码里没有内置口令（仓库公开，写死的默认口令等于没有口令）。
    # 环境变量 > 已有记录 > 全新安装随机生成一个、只在这里打印一次。
    generated = credentials.bootstrap([p["id"] for p in people.all_people()])
    if generated:
        print(f"首次启动：已生成登录口令 {generated}", flush=True)
        print(
            "  请记下它。忘了可删除 data/credentials.json 重新生成，"
            "或设置环境变量 OCGOV_PASSWORD 覆盖（后者会重新派生全部口令）。",
            flush=True,
        )
    elif not credentials.has_records():
        print("警告：尚无口令记录，登录会被拒绝。请检查 data/ 目录是否可写。", flush=True)

    # 老会话没有 owner 字段（隔离是后加的）。一次性划给默认身份，而不是当成
    # "所有人可见"——那等于隔离没做；也不是直接丢弃——用户的会话不该莫名消失。
    # 只认「字段缺失」，重复启动不会反复改写。
    claimed = chat.own_orphans(people.resolve(None))

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    info = modelcfg.llm_info()
    print(f"AI 市长工作台已启动： http://127.0.0.1:{port}", flush=True)
    if claimed:
        print(f"会话归属：{claimed} 条历史会话已归入默认身份（{people.resolve(None)}）", flush=True)
    print(f"数据期次 {wb.md.latest_available()}；待披露 {wb.md.pending_periods() or '无'}", flush=True)
    print(
        f"模型 {info['ref'] or '未配置'}（{'可用' if info['available'] else '不可用，问数将退回规则解析'}）",
        flush=True,
    )
    # 提醒巡检：服务在跑才有人替你看表。没配外发通道时它什么都不做（也不会报错），
    # 配了之后每 5 分钟看一眼该发的发了没有。OCGOV_NOTIFY_TICK=0 可关。
    chan = notify_mod.channels_status()
    worker = notify_mod.start_worker()
    print(
        f"提醒巡检 {'已启动（每 5 分钟）' if worker else '已关闭（OCGOV_NOTIFY_TICK=0）'}；"
        f"外发通道：{chan['mode']}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
