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
import agent as agent_mod  # noqa: E402
import chat  # noqa: E402
import docparse  # noqa: E402
import directives as directives_mod  # noqa: E402
import extract as meeting_extract  # noqa: E402
import ledger as meetings  # noqa: E402
import notice as meeting_notice  # noqa: E402
import llm  # noqa: E402
import llmclient  # noqa: E402
import memory as memory_mod  # noqa: E402
import materials as materials_mod  # noqa: E402
import mockdata as md  # noqa: E402
import modelcfg  # noqa: E402
import polish as polish_mod  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
import proposals as proposals_mod  # noqa: E402
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
import wemeet as wemeet_mod  # noqa: E402
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


def _harness_fallback(question: str, result: dict, atts: list | None = None) -> dict | None:
    """问数/知识库/资料库都没命中（refused）时，用 octop_harness 引擎兜底。

    只在装了 octop_harness 时才走这条路；没装或调用失败都返回 None，让上层
    保持原拒答结果不变。HarnessAgent 是异步的，这里用 asyncio.run 在请求线程
    里起一个独立事件循环跑（ThreadingHTTPServer 每请求一线程，互不干扰）。

    **带了材料就不走这条**（含本会话早前挂着的、标了 `archived` 的那几份）。
    HarnessAgent 那条路没有 compare_material、也拿不到材料正文（`harness.ask`
    只收一个问题字符串），它给出的答案会和用户手里那份文件无关，甚至相反 ——
    而用户看到的是一个正常回答，以为系统读过文件。宁可保留「没查到」这句实话。要它也能对照，得先把材料接进 harness，那是另一件事。
    """
    if atts:
        return None
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


# ── 阶段流水（落库，供回放） ──────────────────────────────────────────────
#
# 阶段提示以前只活在 socket 里：前端 `stage()` 写一行字，下一行把它顶掉，刷新之后
# 一个字都不剩。用户能看到的只有答案，「这一轮到底做了什么」全靠回忆 —— 而事后
# 被问起的偏偏是后者（这份答案是查了指标还是查了台账？参谋到底发没发言？）。
#
# 所以每条阶段事件顺手收一条流水，随 done 一起落进 `meta.steps`。四条规矩：
#
# - **只收用户看得懂的阶段**（plan / experts / rewrite / tool），不收 start/done：
#   那两个是传输层的事（连上了、算完了），对读历史的人没有信息量。
# - **存事件，不存文案**。文案在 `_step_extra` 里都只挑字段，中文由前端
#   `stepText()` 一处生成 —— 后端也拼一份的话，实时和回放看到的措辞迟早不一样，
#   而「同一轮在两种视图里说法不同」比缺这个功能更糟。
# - **`t` 是相对这一轮开始的毫秒偏移**，`ms` 由 phase=end 补齐。绝对时间戳对回放
#   没用（看不出先后间隔），偏移才看得出「解析花了 1 秒、参谋花了 6 秒」。
# - **不进模型上下文**：`chat.context()` 只回灌 content，这份 meta 只有界面读。
#
# 体积：一轮最多 _STEP_MAX 条，每条几十字节，几 KB 封顶。
_STEP_KINDS = ("plan", "experts", "rewrite", "tool")
_STEP_MAX = 40          # 一轮最多记这么多条；再多说明有循环在刷，不该撑进会话文件
_STEP_NAMES = 4         # plan 事件里的指标名只留前几个，其余前端写「等 N 个」


def _step_extra(kind: str, ev: dict) -> dict:
    """挑该阶段要回放的那几个字段。

    白名单而不是整包塞进去：事件里还带着完整降级对象、期次明细这类东西，
    几十条累计下来体积翻倍，而界面上一个都不显示。
    """
    if kind == "plan":
        x: dict = {
            "planner": str(ev.get("planner") or ""),
            "intent": str(ev.get("intent") or ""),
            "names": [str(n) for n in (ev.get("names") or [])[:_STEP_NAMES] if n],
        }
        # 降级只留 kind（rules / fallback），前端据此写「规则解析」「备用模型」
        dg = ev.get("degraded")
        if isinstance(dg, dict) and dg.get("kind"):
            x["dg"] = str(dg["kind"])
        return x
    if kind == "experts":
        x = {"name": str(ev.get("name") or "")}
        if ev.get("index") is not None:
            x["index"] = int(ev.get("index") or 0)
            x["total"] = int(ev.get("total") or 0)
        return x
    if kind == "tool":
        return {"name": str(ev.get("name") or "")}
    return {}


class StepLog:
    """把一轮的阶段事件收成可落库的流水。只在单轮内使用，不加锁。

    start / end 按 `(kind, name)` 配对：同一个工具连查两次是两条，不是一条被
    覆盖 —— 用户点开「处理过程」看到的应该是「查了两次」，而不是「查了一次」。
    """

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self.items: list[dict] = []
        self._open: dict[tuple[str, str], int] = {}

    def _ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def close_open(self, *, cut: bool = False) -> None:
        """把还开着的步骤收尾。

        两种场景都要收：正常出结果时兜底（某个工具抛异常没发 end、事件丢了），
        以及用户点停止。`cut` 让前端写「已中断」而不是「进行中」—— 一轮已经结束
        了还挂着一行「进行中」，回看的人会以为系统卡在那儿。
        """
        now = self._ms()
        for idx in self._open.values():
            item = self.items[idx]
            item["ms"] = max(0, now - int(item["t"]))
            if cut:
                item["cut"] = True
        self._open.clear()

    def feed(self, ev: dict) -> None:
        kind = str(ev.get("type") or "")
        if kind not in _STEP_KINDS or len(self.items) >= _STEP_MAX:
            return
        now = self._ms()
        key = (kind, str(ev.get("name") or ""))
        phase = ev.get("phase")
        if phase == "start":
            self._open[key] = len(self.items)
            self.items.append({"k": kind, "t": now, "x": _step_extra(kind, ev)})
        elif phase == "end":
            idx = self._open.pop(key, None)
            if idx is None:
                # 没有配对的 start（事件丢了、或者这一条本来就是别的路径发的）。
                # 照样记下来：丢掉等于让回放少一步，而丢的那步恰好可能是有问题的那步。
                item = {"k": kind, "t": now, "ms": 0, "x": _step_extra(kind, ev)}
            else:
                item = self.items[idx]
                item["ms"] = max(0, now - int(item["t"]))
                item["x"].update(_step_extra(kind, ev))
            if "ok" in ev:
                # ok 放条目顶层：前端判颜色只认这一个字段，不去 x 里翻
                item["ok"] = bool(ev.get("ok"))
            if idx is None:
                self.items.append(item)
        else:
            # plan 这类一次性事件没有 phase。
            #
            # plan 到达 = 「意图已解析」，顺手把还开着的 rewrite 收掉：改写成功那条
            # 路上后端不发 end（没有第二个事件可发，直接往下走命中结果），不收的
            # 话回放里会永远挂着一行「进行中」—— 而它明明已经成功结束了。
            if kind == "plan":
                idx = self._open.pop(("rewrite", ""), None)
                if idx is not None:
                    item = self.items[idx]
                    item["ms"] = max(0, now - int(item["t"]))
                    item["ok"] = True
            item = {"k": kind, "t": now, "x": _step_extra(kind, ev)}
            if "ok" in ev:
                item["ok"] = bool(ev.get("ok"))
            self.items.append(item)


def _step_pusher(log: StepLog):
    """把 StepLog 包成 core 层要的 `on_step(name, data)` 回调。

    core 那边发的是 `(name, data)` 两个参数，SSE 这边发的是 `{"type": name, **data}`
    一个包 —— 两种形状之间的转换只写这一处，非流式那条路也能收同一份流水。
    """
    return lambda name, data=None: log.feed({"type": name, **(data or {})})


def _wemeet_display(payload: object, fallback: str) -> str:
    """给人看的结果文本：有业务对象就格式化它，没有才退回原串。

    真服务端的 content[].text 是三层信封（含 headers、trace id、版本提示），
    原样甩进界面等于让人读日志。信封留给「原始 JSON」折叠区。
    """
    if isinstance(payload, (dict, list)):
        try:
            return json.dumps(payload, ensure_ascii=False, indent=2)[:8000]
        except (TypeError, ValueError):
            pass
    return fallback


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
    {"q": "哪个镇街的项目推进最慢", "tag": "分解"},
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
    """召唤了哪几位专家。传单个 `expert` 或数组 `experts` 都认，最多三位。

    单选总参谋时由 `expand_for_chief` 展开成全部分管参谋 + 总参谋（排最后）。
    展开必须放在 [:3] **之后**：先展开再截断，7 个 id 会被这一步自己砍掉。

    **AI秘书团不是参谋团，一位参谋都不召唤。** 秘书受令办事（拟办、派活、催办、
    查指示），解读归参谋 —— 团模式下弹出三张参谋卡，用户会以为秘书在替他做判断，
    而且那三次调用没人看。传上来也不认：这一层返回空，交办权仍按 `_is_team` 判。
    """
    if _is_team(body):
        return []
    raw = body.get("experts")
    if not isinstance(raw, list):
        single = str(body.get("expert") or "").strip()
        raw = [single] if single else []
    ids = [str(x).strip() for x in raw if str(x).strip()][:3]
    return expert_mod.expand_for_chief(ids)


def _is_team(body: dict) -> bool:
    """这一轮是不是「AI秘书团」（团模式）。

    只看前端显式给的 `expert_mode`，不从 `experts` 的长度反推：团模式不召唤参谋
    （`experts` 是空数组），跟「用户没选参谋」在请求里长得一模一样，靠数量猜必然猜错。

    交办（拟办 / 查指示办理情况）只对团模式开放 —— 见 core/agent.py 的 _DIRECTIVE_TOOLS。
    前端漏传就是 False，宁可少给权限。
    """
    mode = str(body.get("expert_mode") or "").strip().lower()
    return mode in ("team", "__all", "all")


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
    ("POST", r"/api/attachments/parse", None),   # 解析并入库，返回材料 id
    ("POST", r"/api/materials", None),           # 纯文本材料入库（按 owner 隔离）
    ("POST", r"/api/ask", None),
    ("POST", r"/api/ask/stream", None),
    ("POST", r"/api/ask/cancel", None),          # handler 内按 turn 归属自查
    ("POST", r"/api/ask/polish", None),          # 只改写输入框里的文本，不落盘不取数
    ("POST", r"/api/schedule/brief", None),
    ("POST", r"/api/briefdoc", None),
    ("POST", r"/api/briefdoc/docx", None),      # 只生成字节流，不落盘
    ("POST", r"/api/briefdoc/review", None),    # 只读：把材料回查一遍，不写存储
    ("POST", r"/api/wenshu", None),             # 文种稿：装配 + 体例检查，不落盘
    ("POST", r"/api/wenshu/docx", None),        # 只生成字节流
    ("POST", r"/api/wenshu/review", None),      # 只读：体例检查
    ("POST", r"/api/threads", None),
    ("POST", r"/api/threads/[^/]+/pick", None),  # 换一版答案：只改 picks，不删消息
    ("POST", r"/api/workspace/(watch|follow|layout|feed-seen)", None),
    ("POST", r"/api/saved", None),
    ("POST", r"/api/notify/read", None),
    ("PATCH", r"/api/threads/[^/]+", None),      # handler 内按 owner 过滤
    ("DELETE", r"/api/threads/[^/]+", None),

    # ── 办公室务：登录 + 行级判定 ──
    ("POST", r"/api/oversight", authz.GATE_SESSION),
    ("POST", r"/api/oversight/urge", authz.GATE_SESSION),
    ("POST", r"/api/oversight/[^/]+/action", authz.GATE_SESSION),
    # 对话层生成的待确认动作。粗闸门只要求登录，行级判定在 handler 里按
    # 「这条提议动的是哪条事项」再过一次 —— 和 /api/oversight/[id]/action 同构。
    ("POST", r"/api/proposal/confirm", authz.GATE_SESSION),
    ("POST", r"/api/proposal/cancel", authz.GATE_SESSION),
    # 指示下发：一次落成「一条令 + N 条督办事项」。粗闸门只要求登录，
    # 逐条的行级判定在 handler 里做 —— 一条不过就整批不办，不留半份交办。
    ("POST", r"/api/directive", authz.GATE_SESSION),
    # 个人交办「标记已办」：只动自己那条，行级判定（本人 / 市领导）在 handler 里。
    ("POST", r"/api/assignment/done", authz.GATE_SESSION),

    # ── 会议：市级动作，市领导或秘书（秘书替领导排会、发通知是本职） ──
    ("POST", r"/api/meetings", authz.MEETING_COMMIT),
    ("POST", r"/api/meetings/[^/]+/parse", authz.MEETING_COMMIT),
    ("POST", r"/api/meetings/[^/]+/commit", authz.MEETING_COMMIT),
    # 调腾讯会议建会 / 拉转写：会真的往外部系统写东西，也真的把会议全文拉回来
    ("POST", r"/api/meetings/[^/]+/wemeet", authz.MEETING_COMMIT),
    # 凭证自检 / 调 MCP 工具。调用可能建会，所以按写操作管
    ("POST", r"/api/wemeet/test", authz.MEETING_COMMIT),
    ("POST", r"/api/wemeet/call", authz.MEETING_COMMIT),
    ("POST", r"/api/wemeet/preset", authz.MEETING_COMMIT),
    ("POST", r"/api/meetplan", authz.MEETING_COMMIT),
    ("PATCH", r"/api/meetings/[^/]+/items/[^/]+", authz.MEETING_EDIT),
    ("DELETE", r"/api/meetings/[^/]+", authz.MEETING_EDIT),
    # 导出通知会往本机磁盘写文件（默认桌面）。不往外发，但发文口径是市级的事。
    ("POST", r"/api/meetings/[^/]+/notice/export", authz.MEETING_NOTICE),

    # ── 配置类：全局共享状态，只有市领导 ──
    ("POST", r"/api/models", authz.CONFIG_MODEL),
    ("POST", r"/api/models/test", authz.CONFIG_MODEL),
    ("POST", r"/api/models/fetch", authz.CONFIG_MODEL),
    ("POST", r"/api/active-model", authz.CONFIG_MODEL),
    ("DELETE", r"/api/models/[^/]+", authz.CONFIG_MODEL),
    ("POST", r"/api/experts", authz.CONFIG_EXPERT),
    ("POST", r"/api/plugins/call", None),        # 只读工具直调，handler 内按白名单再挡一次
    ("POST", r"/api/plugins/toggle", authz.CONFIG_PLUGIN),

    # ── 群发：真的会往外发消息，只有市领导 ──
    ("POST", r"/api/notify/dispatch", authz.NOTIFY_DISPATCH),
    ("POST", r"/api/notify/test", authz.NOTIFY_DISPATCH),

    # ── 共享资料库：读不限，写要登录 ──
    ("POST", r"/api/rag/bases", authz.KNOWLEDGE_WRITE),
    ("POST", r"/api/rag/bases/[^/]+/upload", authz.KNOWLEDGE_WRITE),
    ("POST", r"/api/rag/bases/[^/]+/doc/validity", authz.KNOWLEDGE_WRITE),
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


def _att_meta(a: dict) -> dict:
    """落库用的材料清单项。

    带 id 是有用的：刷新之后会话里那一行「参考 N 份材料」还在，点开能看到
    是哪几份、还能不能追问；更重要的是前端的芯片要靠 id 重建 —— 只留名字的话，
    同一份文件改了两个数再传，两个芯片长得一模一样，用户分不出在说哪一份。
    `archived` 也留着：回看历史时能看出「这轮用的是早前传的那份」。
    """
    out = {
        "name": a.get("name"),
        "chars": a.get("chars"),
        "truncated": a.get("truncated"),
    }
    if a.get("id"):
        out["id"] = a["id"]
    if a.get("archived"):
        out["archived"] = True
    return out


def _drop_materials(body: dict) -> list[str]:
    """本轮要摘掉的材料 id（用户点了芯片上的 ✕）。

    只是从会话清单上摘掉，材料本身留在库里 —— 别的会话可能还在用，而且用户
    后悔了（切回来再点一次那份材料）不该需要重新上传一遍。
    """
    raw = body.get("drop_materials")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw[:20] if str(x).strip()]


def _attachments(body: dict, owner: str | None = None) -> list[dict]:
    """校验并裁剪前端送来的参考材料。

    前端已经按同样的额度截过一遍，这里仍然要再截一次：`/api/ask/stream` 是
    公开接口，curl 直接塞一个 200MB 的字符串进来，前端那层挡不住。

    **每份材料都进材料库、带一个 id 出去**（`core/materials.py`）。这是这一版
    最大的行为变化：以前材料只活一轮，追问「表里第 7 行」时后端手里已经没有了。
    现在正文落盘一次，会话记 id，之后按 `archived` 标记每轮带上。

    进来时有两种形状：
    - 带 `id`：前端已经存过了（上传时存 / 或这份是本会话早前传的），只按 id 取正文，
      请求体里那三万字的 text 就不必再传一遍；
    - 只有 `text`：老的调用方式（curl、离线脚本、旧前端），就地存一份换 id。
    """
    raw = body.get("attachments")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    total = 0
    for item in raw[:_MAX_ATT_FILES]:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        stored = materials_mod.get(mid, owner=owner) if mid else None
        text = (stored or {}).get("text") or item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        room = _MAX_ATT_TOTAL - total
        if room <= 0:
            break
        cap = min(_MAX_ATT_EACH, room)
        cut = len(text) > cap
        text = text[:cap]
        total += len(text)
        name = str(item.get("name") or (stored or {}).get("name") or "未命名").strip()[:120]
        name = name or "未命名"
        rec = {
            "name": name,
            "text": text,
            "chars": len(text),
            "truncated": bool(cut or item.get("truncated") or (stored or {}).get("truncated")),
        }
        if stored:
            rec["id"] = stored["id"]
            rec["kind"] = stored.get("kind") or ""
        else:
            saved = materials_mod.save(
                owner, name, text, kind=str(item.get("kind") or ""), truncated=rec["truncated"]
            )
            if saved.get("id"):
                rec["id"] = saved["id"]
                rec["kind"] = saved.get("kind") or ""
        out.append(rec)
    return out


# ---- 知识库清单里的「上传文档」 ----
# 知识库收系统自己产出的记录（knowledge.py），资料库收人上传的文件（rag.py），
# 两者在 core 层互不依赖 —— 谁也不该成为对方的下游。清单要合成一页，就在
# 这一层拼：多一条 import 边，好过让 core 里两个模块互相知道对方。

_KB_DOC_LABEL = "上传文档"


def _kb_doc_entry(doc: dict[str, Any], body: str) -> dict[str, Any]:
    """把一篇上传文档装成知识库条目的形状，好让前端一套渲染吃到两种来源。"""
    ts = int(doc.get("created_at") or 0)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"
    # 300 字节的文件写「1 KB」是假话，小文件保留一位小数
    kb_size = int(doc.get("byte_size") or 0) / 1024
    size = f"{kb_size:.1f} KB" if kb_size < 10 else f"{kb_size:.0f} KB"
    pages = doc.get("pages") or {}
    if pages.get("scanned"):
        # 扫描件索引是空的，「0 字 · 关键词索引」会让人以为是解析坏了。
        # 说清它是能看不能搜，才知道该拿它干什么。
        note = f"{int(pages.get('count') or 0)} 页 · 扫描件，仅页面图"
    else:
        note = f"{doc.get('chars') or 0} 字" + (" · 已向量化" if doc.get("embedded") else " · 关键词索引")
        if pages.get("count"):
            note = f"{int(pages['count'])} 页 · " + note
    v = rag_mod.validity(doc)
    # 时效与文号挤进备注：依据卡片、清单、落库回放三处都在读 `source.note`，
    # 塞进这里等于一次改完三处；另起字段就得三处各改一遍，还会漏。
    # 「现行有效」不写 —— 九成文件都是这个状态，写上去只是噪音；
    # 「已废止」「尚未施行」必须写，那是看的人唯一能立刻发现的信号。
    lead = []
    if v["state"] in ("expired", "upcoming"):
        lead.append(v["label"])
    if doc.get("doc_no"):
        lead.append(str(doc["doc_no"]))
    note = " · ".join(lead + [note])
    return {
        "kind": "doc",
        "kind_label": _KB_DOC_LABEL,
        "id": doc["id"],
        "title": doc["name"],
        "text": body,
        "quote": None,
        "validity": v,
        "source": {
            "department": doc.get("kb_name") or "",
            "report": size,
            "period": stamp,
            "chunks": doc.get("chunk_count") or 0,
            "note": note,
        },
        "ref": {"type": "rag", "kb_id": doc["kb_id"], "doc_id": doc["id"]},
    }


def _kb_doc_entries(q: str = "", limit: int = 0) -> list[dict[str, Any]]:
    """资料库里的文档，按知识库条目的形状列出来。

    - 不带 q：清单模式，取每篇开头三块当预览。**不设阈值、不过滤** —— 和
      knowledge.browse 同一个立场，翻目录漏一篇会让人以为文件没上传成功。
    - 带 q：走向量检索，命中的那一块当正文。**按文档去重** —— 一篇文件命中
      十块，列十遍没有意义；取分最高的那块，并把分数留在条目上供排序。
    """
    bases = rag_mod.list_bases()
    if not bases:
        return []

    if not q:
        return [
            _kb_doc_entry(d, "\n\n".join(rag_mod.preview_chunks(d["kb_id"], d["id"], 3)))
            for d in rag_mod.browse_documents()
        ]

    k = max(1, int(limit or rag_mod.DEFAULT_K))
    docs = rag_mod.browse_documents()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 文件名的字面命中先放进来，并给一个高于正文余弦的分：用户打全名时想找的
    # 就是这份文件，不该因为它排在正文命中后面而翻第二页。
    for d in docs:
        if rag_mod.name_hit(d["name"], q):
            e = _kb_doc_entry(d, "\n\n".join(rag_mod.preview_chunks(d["kb_id"], d["id"], 2)))
            e["score"] = 2.5
            out.append(e)
            seen.add(d["id"])

    hits = rag_mod.search([b["id"] for b in bases], q, k=k)
    if not hits:
        return out
    by_id = {d["id"]: d for d in docs}
    best: dict[str, dict[str, Any]] = {}
    for h in hits:  # search() 已按分降序，第一篇即为该文档的最高分
        if h["doc_id"] in best:
            continue
        best[h["doc_id"]] = h

    for doc_id, h in best.items():
        if doc_id in seen:
            continue
        doc = by_id.get(doc_id)
        if doc is None:  # 索引里有、控制面没有：已经删过，跳过
            continue
        e = _kb_doc_entry(doc, h["text"])
        e["score"] = h["score"]
        out.append(e)
    out.sort(key=lambda e: e["score"], reverse=True)
    return out


def _experts_payload(person_id: str | None) -> dict[str, Any]:
    """每位参谋「域内能读到几条」+ 分类库绑定情况。

    这个数一直只有跑测试的人知道：界面上只能看到参谋说「本域内无记录」，
    分不清是库里真没有、还是域配错了、还是库名对不上。把三件事拆开报出来：
    记录条数（按类型）、挂上的库和文档数、绑了但库里找不到的库名。

    现算不缓存：台账/会议/上传库随时在变，缓存一份就会出现「面板说 5 条、
    参谋说本域无记录」。这点数据量（几十条）重建索引是微秒级。
    """
    person = people.meta(person_id)
    idx = knowledge_mod.index(person)
    bases = rag_mod.list_bases()
    by_name = {b["name"]: b for b in bases}
    by_id = {b["id"]: b for b in bases}
    out: dict[str, Any] = {}
    for e in expert_mod.all_experts():
        dom = e.get("domain") or None
        rows = [r for r in idx if knowledge_mod.in_domain(r, dom)] if dom else idx
        counts = {k: 0 for k in ("metric", "oversight", "meeting", "decision")}
        for r in rows:
            if r.get("kind") in counts:
                counts[r["kind"]] += 1
        bound: list[str] = []
        missing: list[str] = []
        docs = 0
        for name in (dom or {}).get("bases") or []:
            base = by_name.get(name) or by_id.get(name)
            if base is None:
                missing.append(name)
                continue
            bound.append(base["name"])
            docs += int(base.get("doc_count") or 0)
        out[e["id"]] = {
            "total": len(rows),
            "counts": counts,
            "docs": docs,
            "bases": bound,
            "missing": missing,
        }
    return out


def _bases_brief() -> list[dict[str, Any]]:
    """面板里可勾的分类库清单。只给渲染要用的三个字段，不传正文。"""
    return [
        {"id": b["id"], "name": b["name"], "doc_count": b["doc_count"]}
        for b in rag_mod.list_bases()
    ]


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

    def _directive_scope(self) -> dict[str, object]:
        """指示可见性的输入。

        有 session → 用 session 里的角色与分管单位（可信）；没有 → 按演示全量，
        和台账/会议读口同口径。指示原文比台账更敏感，所以登录后是**收窄**的：
        局长只看得见动到本单位的令，看不见市长所有口头交办。
        """
        scope = self._session_scope()
        if scope is not None:
            return scope
        return {
            "person": self._person(),
            "role": directory.ROLE_LEADER,
            "units": (),
            "is_citywide": True,
        }

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
            text = str(out["text"] or "")
            saved = materials_mod.save(
                self._person(body), name, text, kind=str(out.get("kind") or ""),
                truncated=bool(out.get("truncated")),
            )
            items.append(
                {
                    "name": name,
                    "ok": True,
                    # 解析完就地入库：Office 文件离开原始字节就再也解析不出这份
                    # 正文了，解析是唯一能存住它的时刻。前端拿着这个 id 之后
                    # 每轮只发 id，不再把一万多字重复塞进请求体。
                    "id": saved.get("id") or "",
                    "kind": out.get("kind") or "",
                    "text": text,
                    "chars": out["chars"],
                    "truncated": out["truncated"],
                    "sheets": out.get("sheets") or [],
                    "rows": out.get("rows") or 0,
                    "paras": out.get("paras") or 0,
                    "tables": out.get("tables") or 0,
                    "pages": out.get("pages") or 0,
                    "note": out.get("note") or "",
                }
            )
        self._json({"ok": True, "items": items})

    def _materials_save(self, body: dict) -> None:
        """把纯文本材料存进材料库，返回 id。

        为什么要单独一个接口：txt / md / csv 是前端直接读的（要按 GBK 试解码，
        服务端拿不到原始字节的解码意图），读完如果要等「发送」时才上传，那
        用户删掉这份芯片、或者刷新页面，材料就没了 —— 上传那一刻入库，
        芯片上才有一个稳定的身份。
        """
        name = str(body.get("name") or "未命名").strip()[:120] or "未命名"
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._json({"ok": False, "error": "材料是空的"}, 400)
        if len(text) > _MAX_ATT_EACH:
            text = text[: _MAX_ATT_EACH]
        saved = materials_mod.save(
            self._person(body), name, text, kind=str(body.get("kind") or "")
        )
        if not saved.get("id"):
            return self._json({"ok": False, "error": "落盘失败"}, 500)
        self._json(
            {"ok": True, "id": saved["id"], "chars": saved.get("chars") or len(text)}
        )

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
                        # 域内条数 + 库绑定：面板据此显示「这位参谋现在能读到什么」，
                        # 不用再靠「本域内无记录」这句话猜是库空还是域配错了。
                        "knowledge": _experts_payload(self._person()),
                        "bases": _bases_brief(),
                        "rule": "专家可以引用给定数值，也可以写算式推算（如 1024.8 - 882.6 = 142.2）；"
                        "出现清单外、或等式不成立的数字会被整段丢弃。"
                        "记录正文里的数字仍不许复述。",
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

            # ---- 指示（受令 → 拟办 → 分办 → 回执）----
            # 可见性在 directives._visible 里按身份收窄：非分管的令连标题都不会
            # 出现在列表里，而不是"能看列表、点开 403"。未登录时按演示全量 ——
            # 和台账、会议那几处读口的既有口径一致，不搞第二套规矩。
            if path == "/api/directive":
                return self._json(
                    {
                        "ok": True,
                        "items": directives_mod.list_all(self._directive_scope()),
                        "units": directives_mod.units(),
                        # 能接个人交办的人（现在只有市长秘书）。卡片上这一列
                        # 既能填单位也能填人，下拉里两种情况都要有。
                        "people": people.assignable(),
                    }
                )
            dparts = _parts(path, "/api/directive/")
            if len(dparts) == 1:
                one = directives_mod.summary(dparts[0], self._directive_scope())
                if one is None:
                    return self._json(
                        {"ok": False, "error": "directive_not_found", "id": dparts[0]}, 404
                    )
                return self._json({"ok": True, "directive": one})

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
            # 不带 q 就是清单（`knowledge.browse`）：知识库页面先列目录再搜。
            # 两条路共用一个端点，因为对页面来说它们是同一个列表的两种填充方式，
            # 分成两个接口会让「搜完清空搜索框」变成一次额外的请求。
            if path == "/api/knowledge":
                qs = parse_qs(urlparse(self.path).query)
                q = str((qs.get("q") or [""])[0]).strip()
                kind = str((qs.get("kind") or [""])[0]).strip()
                try:
                    limit = max(1, min(200, int((qs.get("limit") or ["6"])[0])))
                except ValueError:
                    limit = 6
                person = people.meta(self._person())
                idx = knowledge_mod.index(person)
                counts: dict[str, int] = {}
                for e in idx:
                    counts[e["kind"]] = counts.get(e["kind"], 0) + 1
                # 上传文档不受身份收窄（沿用资料库原有的读口径：演示态不收窄），
                # 所以单独数一遍，不从 idx 里推。
                doc_total = len(rag_mod.browse_documents())
                if doc_total:
                    counts["doc"] = doc_total
                if not q:
                    items = [] if kind == "doc" else knowledge_mod.browse(person, kind or None)
                    if kind in ("", "doc"):
                        items += _kb_doc_entries()
                    return self._json(
                        {
                            "ok": True,
                            "query": "",
                            "kind": kind,
                            "total": len(items),
                            "index_size": len(idx),
                            "counts": counts,
                            "items": knowledge_mod.to_meta(items),
                            # 保留 hits 字段：老调用方（测试、脚本）读的是它。
                            # 清单不是检索结果，这里如实给空。
                            "hits": [],
                        }
                    )
                pool = [e for e in idx if e["kind"] == kind] if kind else idx
                items = [] if kind == "doc" else knowledge_mod.search_in(pool, q, limit)
                if kind in ("", "doc"):
                    # 两路分数不同源：系统记录是「余弦 + 硬信号」（编号精确命中能
                    # 加到 1.0），上传文档是纯余弦。合并排序时系统记录天然靠前，
                    # 这是想要的次序 —— 台账/口径是现成的答案，文件是要读的原文。
                    items = items + _kb_doc_entries(q, limit)
                    items.sort(key=lambda e: float(e.get("score") or 0), reverse=True)
                return self._json(
                    {
                        "ok": True,
                        "query": q,
                        "kind": kind,
                        "total": len(items),
                        "index_size": len(idx),
                        "counts": counts,
                        "items": knowledge_mod.to_meta(items),
                        "hits": knowledge_mod.to_meta(items),
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

            # 阅读视图：?doc=<id>&q=<关键词>。不给 q 是整篇（章节 + 全文），
            # 给 q 再附上命中章节 —— 页面要展示的是"这份文件的这一节"，
            # 不是一个 800 字的切片。
            if len(parts) == 2 and parts[1] == "doc":
                qs = parse_qs(urlparse(self.path).query)
                doc_id = str((qs.get("doc") or [""])[0]).strip()
                if not doc_id:
                    return self._json({"ok": False, "error": "doc_required"}, 400)
                q = str((qs.get("q") or [""])[0]).strip()
                view = rag_mod.doc_view(parts[0], doc_id, q)
                if view is None:
                    return self._json({"ok": False, "error": "doc_not_found"}, 404)
                return self._json(view)

            # 页面图：?doc=<id>&p=<页码>，回 PNG。
            # 前端 <img src> 直接指过来，所以失败时只能回状态码 —— 图片位置没法
            # 塞一段 JSON 进去。真要给人看的提示放在 doc_view 的 pages 里。
            if len(parts) == 2 and parts[1] == "page":
                qs = parse_qs(urlparse(self.path).query)
                doc_id = str((qs.get("doc") or [""])[0]).strip()
                try:
                    pno = int(str((qs.get("p") or ["1"])[0]))
                except ValueError:
                    pno = 1
                if not doc_id:
                    return self._json({"ok": False, "error": "doc_required"}, 400)
                if pno < 1:
                    return self._json({"ok": False, "error": "page_out_of_range"}, 400)
                try:
                    png = rag_mod.page_image(parts[0], doc_id, pno)
                except IndexError:
                    return self._json({"ok": False, "error": "page_out_of_range"}, 404)
                except RuntimeError as exc:  # 没装 PDF 组件
                    return self._json({"ok": False, "error": str(exc)}, 503)
                if png is None:
                    return self._json({"ok": False, "error": "doc_not_found"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                # 页面图是原文件的纯函数，同一份文件同一页不会变，可以缓。
                # 但删了文档要能失效，所以只给几分钟而不是 immutable。
                self.send_header("Cache-Control", "private, max-age=300")
                self.end_headers()
                self.wfile.write(png)
                return

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
                # 本会话挂着的材料（元数据，不含正文）。前端靠它重建输入框上那一排
                # 芯片 —— 刷新之后芯片凭空消失，用户就以为材料白传了，然后重传一遍。
                mats = materials_mod.describe(
                    thread.get("material_ids") or [], owner=self._person()
                )
                return self._json(
                    {
                        "ok": True,
                        # 只回屏幕上该显示的那些：每条问句 + 它当前选中的那一版答案。
                        # 同一问的其它几版仍在库里（切回去就还在），但一起发过去的话，
                        # 界面要么把同一问画两遍，要么自己再实现一遍"选哪版"——
                        # 两份选择逻辑必然走偏。多于两版时消息上带 alts={n,i}。
                        "thread": {**thread, "messages": chat.visible(thread)},
                        "usage": chat.usage_total(thread),
                        "materials": mats,
                    }
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
                        # 只回「有没有配好、缺哪项」，凭证本身永不出现在响应里
                        "wemeet": wemeet_mod.public_config(),
                    }
                )

            # 腾讯会议 MCP 的工具清单（含 inputSchema）。界面据此渲染参数表单 ——
            # 工具名与参数不硬编码，服务端给什么就是什么。
            if path == "/api/wemeet/tools":
                res = wemeet_mod.probe()
                if res.get("ok"):
                    return self._json(res)
                if res.get("error") == "not_configured":
                    return self._json({**res, "error": "wemeet_not_configured"}, 503)
                return self._json(res, 502)

            parts = _parts(path, "/api/meetings/")
            if len(parts) == 2 and parts[1] == "notice":
                one = meetings.get(parts[0])
                if one is None:
                    return self._json({"ok": False, "error": "meeting_not_found"}, 404)
                # 只读：纯拼装，不落盘、不改数据。导出才走 POST。
                return self._json({"ok": True, "notice": meeting_notice.build(one)})

            if len(parts) == 1:
                one = meetings.get(parts[0])
                if one is None:
                    return self._json({"ok": False, "error": "meeting_not_found"}, 404)
                return self._json({
                    "ok": True,
                    "meeting": one,
                    "units": meeting_extract.known_units(),
                    "wemeet": wemeet_mod.public_config(),
                })

            self.send_error(404)
        except Exception as exc:  # noqa: BLE001 统一兜住，别让异常变成空响应
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # 上传是 base64 包在 JSON 里的，9MB 的文件会变成 12MB 请求体 —— 按普通
        # JSON 的 4MB 卡会把「单文件不超过 9MB」这句承诺自己吃掉：3MB 以上的
        # 文件一律 413，而界面还写着 9MB。
        is_upload = path == "/api/attachments/parse" or (
            path.startswith("/api/rag/bases/") and path.endswith("/upload")
        )
        limit = _MAX_UPLOAD_BODY if is_upload else _MAX_JSON_BODY
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

            if path == "/api/materials":
                return self._materials_save(body)

            if path == "/api/ask":
                return self._ask(body)

            if path == "/api/ask/stream":
                return self._ask_stream(body)

            # 提问整理：只把输入框里那句话改写得更像指标问法，不发出去。
            if path == "/api/ask/polish":
                return self._ask_polish(body)

            # 腾讯会议自检：握手 + 列工具，不建会、不改任何东西
            if path == "/api/wemeet/test":
                return self._wemeet_test()

            # 调一个 MCP 工具。只回结果，落库走 /api/meetings/<id>/wemeet
            if path == "/api/wemeet/call":
                return self._wemeet_call(body)

            # 预设动作：一键建会 / 一键取纪要（参数拼装在服务端）
            if path == "/api/wemeet/preset":
                return self._wemeet_preset(body)

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

            # 直接调一个只读工具。给脚本/调试/前端按钮用。
            # 只放行 toolhost.READONLY_TOOLS 里登记过的工具：写操作必须走各自的
            # 写口（WRITE_POLICY + 状态机守卫），这里给的是「查」，不是「办」。
            if path == "/api/plugins/call":
                name = str(body.get("tool") or "").strip()
                args = body.get("args")
                if not name:
                    return self._json({"ok": False, "error": "tool_required"}, 400)
                if args is None:
                    args = {}
                if not isinstance(args, dict) or len(args) > 8:
                    return self._json({"ok": False, "error": "bad_args"}, 400)
                import toolhost as toolhost_mod  # noqa: PLC0415

                person = self._person()
                actor = str(person or "")
                res = toolhost_mod.call_sync(name, actor=actor, **args)
                return self._json(res, 200 if res.get("ok") else 400)

            if path == "/api/plugins/toggle":
                plugin_id = str(body.get("id") or "").strip()
                enabled = bool(body.get("enabled"))
                if not plugin_id:
                    return self._json({"ok": False, "error": "id_required"}, 400)
                # 只允许切换「已经识别到的」插件，避免把任意字符串写进状态文件
                known = {p["id"] for p in harness_mod.list_plugins()}
                if plugin_id not in known:
                    return self._json({"ok": False, "error": "plugin_not_found"}, 404)
                # 底线只读工具所在的插件不可停用。这里拦的是绕过前端直接 POST 的路。
                try:
                    harness_mod.set_enabled(plugin_id, enabled)
                except harness_mod.BaselineLocked:
                    return self._json(
                        {
                            "ok": False,
                            "error": "baseline_locked",
                            "message": "该插件提供底线只读工具，不允许停用",
                        },
                        403,
                    )
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
                # 指标代码写错（少一个字母、写成指标名）要当场拒，不能静默存进去：
                # 存进去之后这位参谋域内永远少一份口径，界面上只是「没读到」。
                unknown: set[str] = set()
                for e in items:
                    dom = e.get("domain") if isinstance(e, dict) else None
                    if not isinstance(dom, dict):
                        continue
                    codes = dom.get("codes") if isinstance(dom.get("codes"), list) else []
                    for c in codes:
                        if str(c).strip() and str(c).strip() not in md.METRICS:
                            unknown.add(str(c).strip())
                if unknown:
                    return self._json(
                        {
                            "ok": False,
                            "error": "未知指标代码：" + "、".join(sorted(unknown)),
                            "unknown_codes": sorted(unknown),
                        },
                        400,
                    )
                saved = expert_mod.save_experts(items)
                return self._json(
                    {
                        "ok": True,
                        "experts": saved,
                        # 保存后立刻把新映射的条数带回去，面板不用再拉一次
                        "knowledge": _experts_payload(self._person()),
                        "bases": _bases_brief(),
                    }
                )

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
                    doc = rag_mod.upload_document(
                        parts[0], name, blob,
                        content_type=str(body.get("content_type") or ""),
                        doc_no=str(body.get("doc_no") or ""),
                        effective_from=str(body.get("effective_from") or ""),
                        effective_to=str(body.get("effective_to") or ""),
                        superseded_by=str(body.get("superseded_by") or ""),
                    )
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                except LookupError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 404)
                return self._json({"ok": True, "doc": doc, "documents": rag_mod.list_documents(parts[0])})

            # ---- 资料库：政策时效 ----
            # 「这份文件还算不算数」不能靠上传那一刻定死：废止是后来才知道的。
            # 所以单开一个口子，随时能改，改完检索与依据立刻跟着变。
            if len(parts) == 3 and parts[1] == "doc" and parts[2] == "validity":
                if rag_mod.get_base(parts[0]) is None:
                    return self._json({"ok": False, "error": "base_not_found"}, 404)
                doc_id = str(body.get("doc") or "")
                if not doc_id:
                    return self._json({"ok": False, "error": "doc_required"}, 400)
                try:
                    doc = rag_mod.set_validity(
                        parts[0], doc_id,
                        effective_from=body.get("effective_from"),
                        effective_to=body.get("effective_to"),
                        superseded_by=body.get("superseded_by"),
                        doc_no=body.get("doc_no"),
                    )
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                if doc is None:
                    return self._json({"ok": False, "error": "doc_not_found"}, 404)
                return self._json(
                    {"ok": True, "doc": doc, "validity": rag_mod.validity(doc)}
                )

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

            # ---- 会话：换一版答案 ----
            # 同一问可以留几版（重答 / 重试都往后加一版），这里切换显示哪一版。
            # 只换"用哪一版"，一条消息都不删 —— 用户看过的那版随时能切回来，
            # 这是他敢去点「换一版」的前提。只允许最后一轮（core/chat.pick_answer）。
            parts = _parts(path, "/api/threads/")
            if len(parts) == 2 and parts[1] == "pick":
                got = chat.pick_answer(
                    parts[0], str(body.get("message_id") or ""), owner=self._person(body)
                )
                if not got:
                    # 中间轮不给换（换了之后后面几轮就是照着另一版说的，整段历史
                    # 自相矛盾），不是答案、不是这个人的会话，一律到这里。
                    return self._json({"ok": False, "error": "answer_not_switchable"}, 404)
                return self._json({"ok": True, **got})

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

            # ---- 对话层生成的待确认动作 ----
            if path == "/api/proposal/confirm":
                return self._confirm_proposal(body)
            if path == "/api/proposal/cancel":
                return self._cancel_proposal(body)

            # ---- 指示下发：一条令 + N 条督办事项，要么全成要么全不写 ----
            if path == "/api/directive":
                return self._issue_directive(body)

            # ---- 个人交办：标记已办 / 撤销 ----
            if path == "/api/assignment/done":
                return self._complete_assignment(body)

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
            if len(parts) == 3 and parts[1] == "notice" and parts[2] == "export":
                scope, allowed, why = self._require(authz.MEETING_NOTICE)
                if not allowed:
                    audit.user_event(scope.get("person") if scope else None, "access.denied",
                                     target="meeting.notice_export", payload={"reason": why})
                    return self._json({"ok": False, "error": why}, 403)
                one = meetings.get(parts[0])
                if one is None:
                    return self._json({"ok": False, "error": "meeting_not_found"}, 404)
                try:
                    res = meeting_notice.export(
                        one,
                        issuer=str(body.get("issuer") or "").strip() or meeting_notice.DEFAULT_ISSUER,
                        text=body.get("text") if isinstance(body.get("text"), str) else None,
                    )
                except OSError as exc:
                    # 桌面被安全软件锁了、路径不存在、磁盘满 —— 如实报，别假装导出成功
                    return self._json({"ok": False, "error": f"写入桌面失败：{exc}"}, 500)
                audit.user_event(scope["person"], "meeting.notice_export",
                                 target=one.get("id"), payload={"path": res.get("path")})
                return self._json({
                    "ok": True,
                    "path": res.get("path"),
                    "filename": res.get("filename"),
                    "warnings": res.get("warnings") or [],
                })

            if len(parts) == 2 and parts[1] == "parse":
                return self._parse_meeting(parts[0], body)
            if len(parts) == 2 and parts[1] == "wemeet":
                return self._wemeet_apply(parts[0], body)
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

    # ---------------- 对话层生成的待确认动作 ----------------
    def _confirm_proposal(self, body: dict) -> None:
        """确认一条待确认动作：用**点确认这个人**的身份去执行。

        顺序是「看提议 → 取事项 → 判授权 → 占用 → 执行」，每一步都排在
        `ov.advance` 之前。两个理由：

        - 越权要回 403 而不是「已失效」。先 claim 再判授权的话，越权点一次
          就把提议烧掉了，用户看到的是「按钮坏了」，不是「你没这个权限」。
        - `ov.advance` 自己有状态机守卫，被拒时不落任何数据。所以这里只要保证
          「授权过了、提议还在」，剩下那层不用复述。

        audit 里记 `via: agent` + `proposal` 编号：事后要能分清这条是模型提议、
        人确认的，还是人在页面上直接点的。
        """
        scope = self._session_scope()
        if scope is None:
            return self._json({"ok": False, "error": "未登录：请先在右上角登录身份。"}, 401)
        pid = str(body.get("proposal_id") or "").strip()
        if not pid:
            return self._json({"ok": False, "error": "没有给 proposal_id。"}, 400)

        person = scope["person"]
        peek = proposals_mod.get(pid, person)
        if peek is None:
            return self._json(
                {"ok": False, "error": "这条操作已经失效或已经被确认过了。"}, 409
            )
        one = ov.detail(str(peek.get("item_id") or ""))
        if one is None:
            return self._json({"ok": False, "error": "要操作的这件事项已经不在了。"}, 404)

        action = str(peek.get("action") or "")
        az_action = authz.ACTION_FROM_ZH.get(action)
        if az_action is None:
            return self._json({"ok": False, "error": f"未注册的操作：{action}"}, 400)
        units = tuple(
            [one.get("assignee") or ""] + [str(x) for x in (one.get("co_assignees") or [])]
        )
        allowed, why = authz.can(scope, az_action, {"units": units})
        if not allowed:
            audit.user_event(
                person, "access.denied", target=az_action,
                payload={"item_id": one.get("id"), "reason": why, "proposal": pid},
            )
            return self._json({"ok": False, "error": why}, 403)
        if action not in (one.get("allowed_actions") or []):
            return self._json(
                {
                    "ok": False,
                    "error": f"当前状态「{one.get('status')}」不能{action}，这条草稿已经过期了。",
                },
                409,
            )

        prop, why2 = proposals_mod.claim(pid, person)
        if prop is None:
            return self._json({"ok": False, "error": why2}, 409)

        extra = prop.get("extra") or {}
        res = ov.advance(
            str(prop.get("item_id") or ""),
            action,
            actor=person,
            text=str(prop.get("text") or ""),
            # 佐证和完成度原样转交：确认的是「这条草稿」，不是「这一类动作」。
            # 状态机守卫会在 advance 里再判一次（提交办结必须有佐证和说明）。
            evidence=[str(x) for x in (extra.get("evidence") or [])],
            percent=extra.get("percent"),
        )
        audit.user_event(
            person, az_action, target=prop.get("item_id"),
            payload={"ok": res.get("ok"), "via": "agent", "proposal": pid},
        )
        if not res.get("ok"):
            return self._json(res, 409)
        return self._json(
            {**res, "item": ov.detail(res.get("item_id") or ""), "stats": ov.stats()}
        )

    def _cancel_proposal(self, body: dict) -> None:
        """放弃一条待确认动作。什么都没写，所以不记审计。"""
        scope = self._session_scope()
        if scope is None:
            return self._json({"ok": False, "error": "未登录：请先在右上角登录身份。"}, 401)
        pid = str(body.get("proposal_id") or "").strip()
        ok = proposals_mod.cancel(pid, scope["person"]) if pid else False
        return self._json({"ok": True, "cancelled": ok})

    # ---------------- 指示：受令 → 拟办 → 分办 ----------------
    def _issue_directive(self, body: dict) -> None:
        """把一张拟办卡落成「令 + N 条督办事项」。

        三道关，顺序不能换：

        1. **登录 + 角色**：建档（交办）本来就是市级动作，未登录一律 401，局长 403。
        2. **逐条行级判定**：交给谁就要能管谁。常务副市长把事项派给教育局要被拒，
           而不是「整批建完再说」。一条不过就整批不办 —— 半份交办比不办更糟。
        3. **落库**：`directives.issue` 内部先全量校验再写，写台账失败会回滚已建条目。

        传进来的 tasks 是前端卡片上改过的，所以**不能信**：单位、时限、验收标准
        在 issue 里重校一遍，与页面上建档走同一套规则。
        """
        scope, allowed, why = self._require(authz.OVERSIGHT_CREATE, {"units": ()})
        if not allowed:
            audit.user_event(
                scope.get("person") if scope else None, "access.denied",
                target="directive.issue", payload={"reason": why},
            )
            return self._json({"ok": False, "error": why}, 401 if scope is None else 403)

        text = str(body.get("text") or "").strip()
        tasks = body.get("tasks")
        if not text:
            return self._json({"ok": False, "error": "指示原文不能为空。"}, 400)
        if not isinstance(tasks, list) or not tasks:
            return self._json({"ok": False, "error": "至少要有一条交办事项。"}, 400)

        # 派给个人（「让秘书 26 号前把材料整理好」）是另一道权限：不写台账、
        # 不走签收，等于把活直接放进某人的日程。只给市领导，单独判一次 ——
        # 混在 OVERSIGHT_CREATE 里判会漏（那是按单位判的行级授权）。
        wants_person = any(
            isinstance(t, dict)
            and not directory.normalize_unit(str(t.get("assignee") or ""))
            and (
                people.find_by_mention(str(t.get("assignee") or ""))
                or people.find(str(t.get("assignee_person") or ""))
            )
            for t in tasks
        )
        if wants_person:
            okp, whyp = authz.can(scope, authz.TASK_ASSIGN)
            if not okp:
                audit.user_event(scope.get("person"), "access.denied",
                                 target=authz.TASK_ASSIGN, payload={"reason": whyp})
                return self._json({"ok": False, "error": whyp}, 403)

        person = scope["person"]
        # 出生地：哪次对话的哪条消息。前端从卡片上带过来，只有前端知道自己在
        # 哪条气泡里点了按钮。拿不到就是空 —— 老消息、或从别的入口进来的下发，
        # 都允许没有来源，不能因为缺它就拒掉一次合法交办。
        origin = {
            "thread_id": str(body.get("thread_id") or "").strip(),
            "message_id": str(body.get("message_id") or "").strip(),
        }
        res = directives_mod.issue(
            text,
            tasks,
            person=person,
            from_name=str((people.find(person) or {}).get("name") or person),
            source=str(body.get("source") or "市长口述"),
            source_ref=str(body.get("source_ref") or ""),
            origin=origin,
            can_fn=lambda units: authz.can(scope, authz.OVERSIGHT_CREATE, {"units": units}),
            can_assign_fn=lambda: authz.can(scope, authz.TASK_ASSIGN),
        )
        audit.user_event(
            person, authz.OVERSIGHT_CREATE, target=res.get("id"),
            payload={
                "ok": res.get("ok"),
                "via": "directive",
                "items": res.get("item_ids") or [],
                "tasks": len(tasks),
                "denied": None if res.get("ok") else res.get("error"),
            },
        )
        if not res.get("ok"):
            return self._json(res, 403 if "不能交办" in str(res.get("error") or "") else 400)
        # 把令号钉回那条消息上。钉不上（消息被删、线程换了手机）不算失败 ——
        # 令已经建好了，回放时退回按原文匹配也就是了，不能因为回写没成功就报错。
        if origin["thread_id"] and origin["message_id"]:
            try:
                chat.set_message_meta(
                    origin["thread_id"],
                    origin["message_id"],
                    {"directive_id": res.get("id"), "directive_items": res.get("item_ids") or []},
                )
            except Exception:  # noqa: BLE001 回写是锦上添花，坏了不能影响已经落库的令
                pass
        return self._json({**res, "stats": ov.stats()})

    def _complete_assignment(self, body: dict) -> None:
        """把一条个人交办标成已办（或撤销）。

        只有两种人能操作：接办人本人（自己的待办自己清）、市领导（他派的活，
        他可以说不用办了）。别人替他把活标成办完，等于把待办从日程上抹掉 ——
        这比不让他操作危险得多。
        """
        scope, allowed, why = self._require(authz.GATE_SESSION)
        if not allowed:
            audit.user_event(scope.get("person") if scope else None, "access.denied",
                             target="assignment.done", payload={"reason": why})
            return self._json({"ok": False, "error": why}, 401 if scope is None else 403)

        did = str(body.get("directive_id") or "").strip()
        no = body.get("no")
        hit = directives_mod.person_task(did, no)
        if hit is None:
            return self._json({"ok": False, "error": "没找到这条个人交办。"}, 404)

        target = str(hit["task"].get("assignee_person") or "")
        if scope["person"] != target and not authz.can(scope, authz.TASK_ASSIGN)[0]:
            audit.user_event(scope["person"], "access.denied",
                             target="assignment.done", payload={"directive": did, "no": no})
            return self._json({"ok": False, "error": "只有接办人本人或市领导能标记这条。"}, 403)

        done = body.get("done") is not False
        res = directives_mod.complete_person_task(did, no, by=scope["person"], done=done)
        if res.get("ok"):
            audit.user_event(
                scope["person"], "assignment.done" if done else "assignment.reopen",
                target=f"{did}#{no}",
            )
        return self._json(res, 200 if res.get("ok") else 400)

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

    # ---------------- 腾讯会议 ----------------
    def _wemeet_test(self) -> None:
        """凭证自检。只读，不改任何数据，也不建会。"""
        scope, allowed, why = self._require(authz.MEETING_COMMIT)
        if not allowed:
            audit.user_event(scope.get("person") if scope else None, "access.denied",
                             target="meeting.wemeet_test", payload={"reason": why})
            return self._json({"ok": False, "error": why}, 403)

        res = wemeet_mod.probe()
        audit.user_event(scope["person"], "meeting.wemeet_test",
                         target="wemeet", payload={"ok": res.get("ok"), "error": res.get("error")})
        if res.get("ok"):
            return self._json(res)
        if res.get("error") == "not_configured":
            # 对外统一一个码：客户端级的 not_configured 只在 wemeet.py 内部用
            return self._json({**res, "error": "wemeet_not_configured"}, 503)
        return self._json(res, 502)

    def _wemeet_call(self, body: dict) -> None:
        """调一个腾讯会议 MCP 工具。**只回结果，不写任何本地数据。**

        为什么拆两步：服务端给什么工具、参数怎么填，我们事先并不知道（工具清单是
        运行时 list 出来的）。如果「调用」顺手把结果写进会议主档，等于让远端服务
        决定督办台账里出现什么字。所以这里只认一认结果里有没有会议号 / 会议原文，
        真正落库由人点「写进会议主档」「落成会议原文」。
        """
        scope, allowed, why = self._require(authz.MEETING_COMMIT)
        if not allowed:
            audit.user_event(scope.get("person") if scope else None, "access.denied",
                             target="meeting.wemeet_call", payload={"reason": why})
            return self._json({"ok": False, "error": why}, 403)

        name = str(body.get("tool") or body.get("name") or "").strip()
        raw_args = body.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        if not name:
            return self._json({"ok": False, "error": "bad_param", "detail": "没给工具名。"}, 400)

        res = wemeet_mod.call_tool(name, args)
        audit.user_event(scope["person"], "meeting.wemeet_call",
                         target=name, payload={"ok": res.get("ok"), "error": res.get("error")})
        if not res.get("ok"):
            if res.get("error") == "not_configured":
                return self._json({**res, "error": "wemeet_not_configured"}, 503)
            return self._json(res, 502)

        text = res.get("text") or ""
        # 认结果只看剥过信封的业务对象：信封里有 headers 和 trace id，
        # 混进来会把「Date: Thu, 12 Nov 2026」当成会议原文。
        payload = res.get("payload")
        online = wemeet_mod.extract_online(payload) or {}
        transcript = wemeet_mod.extract_text(payload) or ""
        # 有些工具只把整段东西塞在 content[].text 里（比如智能纪要的 markdown）
        if not transcript and len(text) >= 40:
            transcript = text
        return self._json({
            "ok": True,
            "is_error": res.get("is_error"),
            "tool": name,
            "text": transcript or _wemeet_display(payload, text),
            "raw": res.get("raw"),
            "found": {
                "online": online,
                # 全文一并回给界面：落库时前端要原样送回来，不自己拼文本
                "text": transcript,
                "text_chars": len(transcript),
                "text_preview": transcript[:160],
            },
            "hint": "结果没有写进任何地方。要落库请分别点「写进会议主档」「落成会议原文」。",
        })

    def _wemeet_preset(self, body: dict) -> None:
        """中文预设动作：订会议 / 取纪要 / 查最近的会 / 看参会人。

        这四个动作把「调哪个工具、参数怎么拼」都收在服务端（`wemeet.preset_*`）：
        时间是 ISO 8601、转写要先查 record_file_id、详情还要 pid —— 这些不该让
        用户看见。界面只给中文按钮，工具名和 schema 收进「高级」。

        结果形状仍分两类：写操作（订会）回会议号，读操作（查会/参会人）回列表，
        取纪要回文本。**都不落库**，落库仍要人再点一次。
        """
        scope, allowed, why = self._require(authz.MEETING_COMMIT)
        if not allowed:
            audit.user_event(scope.get("person") if scope else None, "access.denied",
                             target="meeting.wemeet_preset", payload={"reason": why})
            return self._json({"ok": False, "error": why}, 403)

        action = str(body.get("action") or "").strip().lower()
        if action not in ("schedule", "transcript", "recent", "participants"):
            return self._json({"ok": False, "error": "bad_action",
                               "detail": f"不认识的动作：{action}"
                                         "（只认 schedule / transcript / recent / participants）"}, 400)

        # ---- 只读：查最近的会 ----
        if action == "recent":
            raw_days = body.get("days")
            try:
                days = int(raw_days) if raw_days not in (None, "") else 30
            except (TypeError, ValueError):
                days = 30
            res = wemeet_mod.preset_recent(days)
            audit.user_event(scope["person"], "meeting.wemeet_preset",
                             target="wemeet", payload={"action": action, "ok": res.get("ok")})
            if not res.get("ok"):
                return self._preset_error(res)
            # 变量名别用 meetings：那是模块级的 ledger 导入，函数里一赋值就变成
            # 局部变量，后面 meetings.get(mid) 直接 UnboundLocalError。
            rows = res.get("meetings") or []
            return self._json({
                "ok": True,
                "preset": "recent",
                "meetings": rows,
                "raw": res.get("raw"),
                "summary": f"最近 {days} 天有 {len(rows)} 场会",
            })

        # ---- 只读：看参会人 ----
        if action == "participants":
            wid = str(body.get("wemeet_meeting_id") or "").strip()
            if not wid:
                return self._json({"ok": False, "error": "bad_param",
                                   "detail": "要看到参会人，得先给一场腾讯会议的会议 ID。"}, 400)
            res = wemeet_mod.preset_participants(wid)
            audit.user_event(scope["person"], "meeting.wemeet_preset",
                             target="wemeet", payload={"action": action, "ok": res.get("ok")})
            if not res.get("ok"):
                return self._preset_error(res)
            people = res.get("people") or []
            return self._json({
                "ok": True,
                "preset": "participants",
                "people": people,
                "raw": res.get("raw"),
                "summary": f"这场会查到 {len(people)} 位参会人",
            })

        # ---- 写/取：都需要一场本地会议做上下文 ----
        mid = str(body.get("meeting_id") or "").strip()
        one = meetings.get(mid) if mid else None
        if one is None:
            return self._json({"ok": False, "error": "meeting_not_found"}, 404)

        if action == "schedule":
            raw_minutes = body.get("minutes")
            try:
                minutes = int(raw_minutes) if raw_minutes not in (None, "") else 60
            except (TypeError, ValueError):
                minutes = 60
            res = wemeet_mod.preset_schedule(one, minutes=minutes)
        else:
            res = wemeet_mod.preset_transcript(one)

        audit.user_event(scope["person"], "meeting.wemeet_preset",
                         target=mid, payload={"action": action, "ok": res.get("ok")})
        if not res.get("ok"):
            return self._preset_error(res)

        online = res.get("online") if action == "schedule" else (wemeet_mod.extract_online(res.get("payload")) or {})
        transcript = res.get("transcript") if action == "transcript" else ""
        transcript = transcript or wemeet_mod.extract_text(res.get("payload")) or ""
        source = res.get("source") or ("transcript" if action == "transcript" else "")
        if action == "schedule":
            code = (online or {}).get("meeting_code") or ""
            summary = f"已订会，会议号 {code}" if code else "已订会"
        else:
            summary = f"取到 {len(transcript)} 字"
            if source == "minutes":
                summary += "（智能纪要摘要，不是逐字转写）"
        return self._json({
            "ok": True,
            "is_error": res.get("is_error"),
            "preset": action,
            "tool": res.get("tool"),
            "text": transcript or _wemeet_display(res.get("payload"), res.get("text") or ""),
            "raw": res.get("raw"),
            "found": {
                "online": online or {},
                "text": transcript,
                "text_chars": len(transcript),
                "text_preview": transcript[:160],
                "record_file_id": res.get("record_file_id") or "",
                "source": source,
            },
            "sent": res.get("sent") or None,
            "summary": summary,
            "hint": "结果没有写进任何地方。要落库请分别点「写进会议主档」「落成会议原文」。",
        })

    def _preset_error(self, res: dict) -> None:
        """预设动作的错映射。码不同，用户该做的事也不同。"""
        err = res.get("error")
        if err == "not_configured":
            return self._json({**res, "error": "wemeet_not_configured"}, 503)
        if err in ("bad_time", "tool_missing", "bad_param"):
            return self._json(res, 400)
        if err in ("no_recording", "no_transcript"):
            return self._json(res, 404)
        return self._json(res, 502)

    def _wemeet_apply(self, mid: str, body: dict) -> None:
        """把工具结果落到会议主档：写会议号 / 把文本落成会议原文。

        两个落点各自有边界，都由 ledger 判：会议号可以重复写（重新预约就覆盖）；
        原文只在「原本就是腾讯会议转写」时允许重拉，手工粘的原文、已落督办的事项
        一律拒绝 —— 前者会让两版原文并存，后者会让已通过的引文悬空。
        """
        scope, allowed, why = self._require(authz.MEETING_COMMIT)
        if not allowed:
            audit.user_event(scope.get("person") if scope else None, "access.denied",
                             target="meeting.wemeet_apply", payload={"reason": why})
            return self._json({"ok": False, "error": why}, 403)

        one = meetings.get(mid)
        if one is None:
            return self._json({"ok": False, "error": "meeting_not_found"}, 404)

        online = body.get("online")
        text = body.get("text")
        applied: list[str] = []
        replaced = 0

        if isinstance(online, dict) and online:
            saved = meetings.set_online(mid, online)
            if not saved.get("ok"):
                return self._json({"ok": False, "error": saved.get("error")}, 409)
            applied.append("online")

        if isinstance(text, str) and text.strip():
            saved = meetings.set_transcript(
                mid, text, record_file_id=str(body.get("record_file_id") or "")
            )
            if not saved.get("ok"):
                return self._json({"ok": False, "error": saved.get("error")}, 409)
            applied.append("text")
            replaced = saved.get("replaced_items") or 0

        if not applied:
            return self._json({
                "ok": False,
                "error": "nothing_to_apply",
                "detail": "既没给会议号也没给原文，没有可落库的东西。",
            }, 400)

        audit.user_event(scope["person"], "meeting.wemeet_apply", target=mid,
                         payload={"applied": applied})
        out: dict = {"ok": True, "meeting": meetings.get(mid), "applied": applied}
        if "text" in applied:
            out["replaced_items"] = replaced
            out["hint"] = "原文已落库，接着点「解析议定事项」—— 三道闸照旧。"
        else:
            out["hint"] = "会议号已写进主档，生成会议通知时会自动带上入会链接。"
        return self._json(out)

    # ---------------- 问数 ----------------
    def _prepare(self, body: dict) -> tuple[dict, list, str, dict, list, str | None]:
        """建/取会话、取历史、落用户消息。

        返回 `(thread, history, question, ctx, atts, reply_to)`。

        `ctx` 是本轮上下文的裁剪统计（总几轮、带了几轮、裁掉几轮）。它会一路
        传到回答的 meta 里 —— 界面必须把「哪几轮已经不在上下文」说出来。
        `atts` 是本轮随问题带上来的参考材料 + 本会话此前挂着的那几份（后者标
        `archived`）。正文一律从材料库按 id 取，会话记录里只留 id 和清单。
        `reply_to` 是这一版答案要挂到哪条问句上：新问句就是刚落的这条，重答
        则是原来那条 —— 答案落库时带上它，同一问的几版才认得出来是一家。
        """
        question = str(body.get("question") or "").strip()
        if not question:
            raise ValueError("empty_question")
        owner = self._person(body)
        atts = _attachments(body, owner=owner)

        thread = chat.ensure(
            body.get("thread_id"), model_ref=body.get("model_ref"), owner=owner
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

        # ── 材料：本轮新传的记进会话，往轮留下的跟着这一轮一起带上 ──
        #
        # 以前材料只活一轮：前端每轮把正文塞进请求体，落库只留文件名。用户传了
        # 月报问「三季度累计」，答完；再问「表里第 7 行怎么算的」，这一轮手里
        # 什么都没有，只能拒答。现在正文在材料库（core/materials.py）里按 id 取。
        #
        # 往轮材料标 `archived`：它要进模型上下文（不然追问翻不到原文），但它
        # **不是本轮用户新给的材料** —— 不标出来，外部数据对照（extdata）会把
        # 同一份表每轮重对一遍，问「固投」也给你贴一张工业增加值的对照表。
        fresh = [a["id"] for a in atts if a.get("id")]
        drops = _drop_materials(body)
        # 只在真的变了才写：`chat.attach_materials` 是一次「读整份 chats.json →
        # 改 → 整份写回」，每轮白跑一遍等于把会话文件多写一倍。绝大多数轮次
        # 既没新材料也没摘除，直接读现成的清单。
        if fresh or drops:
            kept_ids = chat.attach_materials(thread["id"], add=fresh, drop=drops)
        else:
            kept_ids = [str(x) for x in (thread.get("material_ids") or [])]
        if kept_ids:
            materials_mod.touch(kept_ids, owner=owner)
        fresh_set = set(fresh)
        older = materials_mod.get_many(
            [x for x in kept_ids if x not in fresh_set], owner=owner
        )
        for item in older:
            item["archived"] = True
        atts = atts + older

        # 材料要按窗口再裁一遍：_attachments 那层「合计 3 万字」是请求体安全线，
        # 跟模型是谁无关。8k 的内网模型上，3 万字中文约 1.9 万 token，一份就能
        # 把窗口撑爆，而失败会被当成普通模型故障最终退回规则解析 —— 用户只看到
        # 答案突然变糙，不知道是自己传的文件太大。裁完的占用要从历史里扣，
        # 两者抢的是同一个窗口。顺序也有意义：本轮新传的排在前面，预算不够时
        # 先保住这一轮的材料，往轮的先被裁掉。
        atts, att_tokens = chat.clip_materials(atts, window)

        def _read_history(drop_tail: int = 0) -> list[dict[str, str]]:
            # 出参是同一个 dict，重取前先清空，否则上一次的 dropped/tokens 会残留
            ctx.clear()
            return chat.context(
                thread["id"],
                out=ctx,
                window=window,
                system_text=system_text,
                reserved=att_tokens,
                drop_tail=drop_tail,
            )

        # ── 重答 / 重试：同一问再要一版，而不是把上一版删掉 ──
        #
        # 两种入口共用一条路，因为要判断的是同一件事：「这次问的还是不是上一轮
        # 那句话」。分开写两套的话，「停止后重试」那条迟早和「换一版」走偏。
        #
        # 落库时挂到**同一条**问句上（reply_to）：这一问下面于是有了两版答案，
        # 会话里仍然只有一条问句 —— 问句不重复记，也没有任何东西被删掉。以前
        # 这里调 `chat.drop_last()` 删上一条，删掉的是「这一轮试过什么」的记录，
        # 而用户想比较的正是上一版和这一版。
        #
        # 判断必须在取历史**之前**做：要把这一轮从上下文里摘掉（模型看到自己
        # 上一版的答案会顺着它复述，连数字一起），而取历史又必须赶在落新问句
        # 之前（否则同一句话在 history 和 question 里各出现一次）。
        redo = bool(body.get("retry") or body.get("regen"))
        turn = chat.last_turn(thread) if redo else None
        same_q = lambda m: (m.get("content") or "").strip() == question if m else False
        redoing = bool(turn and same_q(turn.get("user")))
        # 这一轮在上下文里占几条：问句一条；上一轮已经有答案（也就是正在重答）
        # 再加一条。固定写 2 会在"上一轮失败、只落了问句"时多摘一条 —— 把
        # **再上一轮**的问句摘成孤儿，模型会看到一句没有回答的提问。
        drop = (1 + (1 if (turn or {}).get("pick") else 0)) if redoing else 0
        history = _read_history(drop_tail=drop)

        if redoing:
            reply_to = str((turn["user"] or {}).get("id") or "") or None
        else:
            # 新的一问（也可能是不带 retry 的普通提问）。append 的返回值里带 id，
            # 这一版答案后面靠它挂上去。
            reply_to = (chat.append(thread["id"], "user", question) or {}).get("id")

        # 跨会话记忆：新会话（或隔了很久）开问时，把长期关注背景拼在历史前面，
        # 让模型规划查询时知道领导平时盯哪些指标/维度/主题。只含名称不含数值，
        # 与 chat.context 的边界一致；没装记忆库时返回空列表，不改变原行为。
        # 放在 retry/append 之后拼，是为了不干扰上面的重试裁剪逻辑。
        mem_bg = memory_mod.background(question)
        if mem_bg:
            history = mem_bg + history

        return thread, history, question, ctx, atts, reply_to

    def _persist_answer(
        self, thread_id: str, result: dict, reply_to: str | None = None
    ) -> dict | None:
        """落一轮回答。

        meta 里存 sources/notes/facts 是为了**历史消息能重新渲染成同样的样式**；
        不存 trace 是因为回看历史不需要调用链路，省一半体积。给模型用的上下文
        只取 metric_names/intent（见 core/chat.py），这些字段不会进模型。

        唯一的例外是 usage：它不在 trace 里另存，而是单独算一份放 meta。trace
        可以不看，账不能不算 —— 不存的话，这一轮花掉多少 token 就永久丢了。

        `reply_to` 是这一版答案挂在哪条问句上。重答时它不是刚刚落的那条问句，
        而是原来那条 —— 同一问下于是有了两版答案，界面上给「‹ 2/3 ›」切换。
        不传就由 chat.append 自己取最近一条 user，老调用方不受影响。
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
                # 期次要落：会话标题靠「期次 + 指标名」区分（core/chat.derive_title），
                # 只留指标名的话，十个会话问同一个指标，标题全长一样
                "period_label": result.get("period_label"),
                "sources": result.get("sources") or [],
                "notes": result.get("notes") or [],
                "facts": result.get("facts") or [],
                # 走势图要跟着落库：回看历史时图突然消失、只剩一行字，
                # 是比"从来没有图"更奇怪的体验。序列最多 24 期，体积可接受。
                "series": result.get("series") or [],
                "series_title": result.get("series_title"),
                # 分解表同理：问「哪个镇街最慢」的那一轮，回看时只剩一句
                # 「上黄镇最低」，其余 11 个镇街差多少就没了。行数是一二十行，可控。
                "breakdown": result.get("breakdown") or None,
                # 拒答时给的可问清单也要落：刷新之后那 11 个胶囊凭空消失，
                # 用户会以为系统"这次答不上就说没了"。每条约 40 字，可控。
                "suggestions": (result.get("suggestions") or [])[:12],
                # 近似命中的候选（「民生十事」→ 民生实事）。刷新后这几个胶囊
                # 要是没了，用户就只剩「没有对应指标」一句——他已经写错一次了，
                # 再让他自己想怎么改，这个提示就等于没给。
                "did_you_mean": (result.get("did_you_mean") or [])[:3],
                # 按近似匹配答的那一轮：答案讲的是「民生实事」，提问栏里写的是
                # 「民生十事」。不把「按什么理解的」一起落库，回看时就像答错了题。
                "interpreted_as": result.get("interpreted_as"),
                # 自动改写的那一轮：原文和改写后的问句都要留。「问题改过」这件事
                # 只要不落库，回看时答案就变成了「答非所问」——用户记得自己问的
                # 是什么，界面上却只剩答案。
                "rewrite": result.get("rewrite"),
                "knowledge": knowledge_mod.to_meta(result.get("knowledge")),
                "rag": result.get("rag") or [],
                "harness": result.get("harness") or {},
                "expert_views": views,
                # 建会草稿要跟着落库：回放时要能重新画出那张卡（含当时的假设与缺项），
                # 只留一句 headline 的话，用户回看时不知道自己当时确认了什么
                "meet_draft": meetplan_mod.to_meta(result.get("meet_draft")),
                # 指示拟办卡要跟着落库：回放时得能重新画出那张卡（含当时的拆解、
                # 标红的缺项）。不存的话刷新之后卡没了，正文字还写着「点下发才进台账」。
                # 已经下发过的，卡片回放会从 /api/directive 拿到令号，不靠这里。
                "directive_draft": result.get("directive_draft") or None,
                "directive_rows": result.get("directive_rows") or [],
                # 待确认动作与公文草稿也要跟着落库。不落的话，刷新之后卡片凭空消失，
                # 而正文里还留着「点『确认催办』才生效」——按钮没了，指示还在，
                # 这就是界面在说谎。回放时按「已失效」渲染，不假装还能点。
                "proposals": [
                    {k: p.get(k) for k in
                     ("id", "kind", "action", "item_id", "title", "text", "expires_at")}
                    for p in (result.get("proposals") or [])[:3]
                ],
                # 公文草稿带 meta/doc 一起存：回放时「导出 Word」仍然要导出当时那一份，
                # 不能重新装配（重新装配会和屏幕上看到的措辞对不上）。几 KB，值。
                "doc_draft": (
                    {k: v for k, v in (result.get("doc_draft") or {}).items() if k != "issues"}
                    if result.get("doc_draft") else None
                ),
                # 用量：调用链（trace）不落库，如果这里也不存，本轮花掉多少 token
                # 就永久丢失了 —— 回看历史、做预算都无从统计。几十字节，值得存。
                # 上下文统计跟着落库：刷新后用量行还在，说明也该还在
                "context": result.get("context") or {},
                # 附件清单（只有名字和字数，正文不进历史）：回看时说得清
                # "这条结论当时参考了什么"，而不用把一万字材料存两遍
                "attachments": result.get("attachments") or [],
                # 沿用了本会话早前的材料（这一轮没有新传）。不落库的话，回看时
                # 「这轮没附件，怎么答得出表里第 7 行」就永远解释不清
                "materials_reused": result.get("materials_reused") or 0,
                # 材料值 × 库值的对照（B 档）。必须落库：用户拿材料来核对的
                # 那次，往往就是他最想回头再看一眼的一次
                "material_compare": result.get("material_compare") or [],
                # 外部数据整合结果同理：这是「拿自己的表来核系统」的那一轮，
                # 刷新后对照表消失，等于让他重新传一次文件
                "external": result.get("external") or {},
                # 阶段流水（见 StepLog）：这一轮做过的每一步、各花多久。以前它只
                # 活在 socket 里，刷新即失 —— 而回看时最想知道的常常正是「它到底
                # 查了没有」，不是「它说了什么」。只存事件字段不存文案，中文由前端
                # stepText() 生成，实时和回放共用一份措辞。
                "steps": result.get("steps") or [],
                "usage": result.get("usage") or {},
            },
            reply_to=reply_to,
        )

    def _ask_polish(self, body: dict) -> None:
        """把一句口语提问整理成指标字典认得的问法。只改文本，不落库、不取数。

        为什么不复用 /api/ask：那一问会落会话、调模型、取数、叫参谋，
        副作用一串。而这里的语义是「先别发，看看这句话能不能被听懂」——
        用户可能连着点三次才满意，每次都落一轮会话的话，历史会被草稿淹没。
        """
        text = str(body.get("text") or "").strip()
        if not text:
            return self._json({"ok": False, "error": "text_required"}, 400)

        try:
            result = polish_mod.polish(text, model_ref=body.get("model") or None)
        except ValueError as exc:
            # empty_question / too_long：都是入参问题，不该 500
            return self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001 改写失败也不该影响下一步提问
            return self._json(
                {"ok": False, "error": "polish_failed", "message": str(exc)[:200]}, 500
            )

        return self._json(
            {
                "ok": True,
                "text": result["text"],
                "changed": result["changed"],
                "method": result["method"],
                "applied": result["applied"],
                "candidates": result["candidates"],
                "matched": result["matched"],
                "degraded": result["degraded"],
                "model": result["model"],
            }
        )

    def _converse(
        self,
        question: str,
        thread: dict,
        history: list,
        body: dict,
        atts: list,
        on_step: Any = None,
        should_stop: Any = None,
    ) -> dict | None:
        """先走对话层（core/agent.py），由模型自己决定查什么、要不要澄清。

        返回 None 表示这条路没走通，调用方走回原来的问数链路：

        - 没配模型：对话层的前提就是模型，规则链路照旧；
        - 模型一次都没调通、也没查到任何东西：没有可交付的结果，不如交回去。

        带材料也走这里：材料作为数据块进上下文，要核对就拿 `compare_material`
        把材料里的数和库值并列 —— 以前那道「按关键词猜他要不要对照」的分流已经删了。

        这里刻意不吞「已停止」：那是用户主动的操作，得让上层落一条「已停止生成」。
        """
        if not agent_mod.available():
            return None
        try:
            res = agent_mod.run(
                question,
                history=history,
                model_ref=thread.get("model_ref"),
                person_id=self._person(body),
                expert_ids=_expert_ids(body),
                team=_is_team(body),
                on_step=on_step,
                should_stop=should_stop,
                attachments=atts,
            )
        except ask_mod.TurnCancelled:
            raise
        except Exception:  # noqa: BLE001 对话层是加分项，不能成为单点故障
            traceback.print_exc()
            return None
        # 约会议现在也走对话层（工具面有 draft_meeting，能接着上一轮改）。
        # 但**没解析出草稿就交回老链路**：那句「帮我约个会」在原链路里是确定性的
        # 规则解析，而让模型用一段话回答它，最好的结果是它说办不到，最坏的是它
        # 编一个会议安排。草稿没出来时，宁可用那条更笨但不会编的路。
        if res is not None and meetplan_mod.wants(question) and not res.get("meet_draft"):
            return None
        return res

    def _ask(self, body: dict) -> None:
        try:
            thread, history, question, ctx, atts, reply_to = self._prepare(body)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

        # 非流式这条路也要收阶段流水：界面上虽然看不到过程，但落库的 meta.steps
        # 必须和流式一致 —— 同一轮会话，换个入口问就少了「处理过程」，回看起来
        # 像是这一轮系统没做任何事。核心链路一个参数就够（见 _step_pusher）。
        log = StepLog()

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
                    team=_is_team(body),
                    person_id=self._person(body),
                    known_metrics=chat.known_metrics(
                        thread["id"], owner=self._person(body)
                    ),
                    attachments=atts,
                    on_step=_step_pusher(log),
                )
                result = _harness_fallback(question, result, atts) or result
        else:
            # 先走对话层：模型自己决定查指标、扫台账、翻记录，还是反问一句澄清。
            # 它返回 None（没模型 / 没查通 / 带附件）时才落到原来的单向链路。
            result = self._converse(question, thread, history, body, atts, on_step=_step_pusher(log))
            if result is None:
                result = ask_mod.ask(
                    question,
                    history=history,
                    model_ref=thread.get("model_ref"),
                    expert_ids=_expert_ids(body),
                    team=_is_team(body),
                    person_id=self._person(body),
                    known_metrics=chat.known_metrics(
                        thread["id"], owner=self._person(body)
                    ),
                    attachments=atts,
                    on_step=_step_pusher(log),
                )
                # 问数/知识库/资料库都没命中时，用 octop_harness 引擎（含插件工具）兜底。
                # 只在装了 harness 且结果 refused 时才走，否则保持原结果不变。
                fb = _harness_fallback(question, result, atts)
                if fb is not None:
                    result = fb
        result["planner_info"] = llm.llm_info()
        # 本轮上下文的裁剪统计。前端在用量行里写出来，让「它怎么把刚才的忘了」
        # 有一个可核对的解释，而不是让人怀疑系统不稳定。
        result["context"] = ctx
        # 落库只留清单，不留全文：材料正文进 prompt 就够，进历史是纯占地
        result["attachments"] = [_att_meta(a) for a in atts if not a.get("archived")]
        result["materials_reused"] = len([a for a in atts if a.get("archived")])
        # 与 /api/ask/stream 同一条约定：处理过程跟着结果走、跟着落库
        log.close_open()
        result["steps"] = log.items
        msg = self._persist_answer(thread["id"], result, reply_to)
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
            thread, history, question, ctx, atts, reply_to = self._prepare(body)
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

        # 阶段流水。必须在第一个事件之前建 —— t 的零点是「这一轮开始」，晚建
        # 的话第一段的耗时会偏小，而第一段（解析意图）恰恰是最长的那一段之一。
        log = StepLog()

        def push(payload: dict) -> None:
            # 顺手收一条阶段流水（见 StepLog）。放在落日志之前：断线时这一轮仍然
            # 会跑完并落库，流水要跟着一起留下，不能只留给还在接的那个页面。
            log.feed(payload)
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
            # 对话层优先（同 /api/ask）：模型自己决定查什么、要不要澄清。
            result = self._converse(
                question,
                thread,
                history,
                body,
                atts,
                on_step=step,
                should_stop=lambda: cancel_requested(turn_id),
            )
            if result is None:
                result = ask_mod.ask(
                    question,
                    history=history,
                    model_ref=thread.get("model_ref"),
                    on_step=step,
                    expert_ids=_expert_ids(body),
                    team=_is_team(body),
                    person_id=self._person(body),
                    known_metrics=chat.known_metrics(thread["id"], owner=self._person(body)),
                    attachments=atts,
                    should_stop=lambda: cancel_requested(turn_id),
                )
                # 问数/知识库/资料库都没命中时，用 octop_harness 引擎兜底（同 _ask）
                fb = _harness_fallback(question, result, atts)
                if fb is not None:
                    result = fb
            result["planner_info"] = llm.llm_info()
            # 本轮上下文的裁剪统计。前端在用量行里写出来，让「它怎么把刚才的忘了」
            # 有一个可核对的解释，而不是让人怀疑系统不稳定。
            result["context"] = ctx
            # 落库只留清单，不留全文：材料正文进 prompt 就够，进历史是纯占地。
            # 只算**本轮新传**的那几份 —— 会话早前挂着的也进了 prompt，但它们
            # 不是这一轮参考的材料，算进来会让每轮都显示「参考 2 个附件」。
            result["attachments"] = [_att_meta(a) for a in atts if not a.get("archived")]
            # 沿用了早前的材料就单独记一笔：这一轮的答案确实看了那几份，
            # 界面要说出来，否则回看时「这轮没传附件，怎么答得出表里的数」
            result["materials_reused"] = len([a for a in atts if a.get("archived")])
            # 阶段流水跟着这一轮一起回去、一起落库：界面上的「处理过程」那一折
            # 就是它。放 result 里而不是单独推一个事件，是为了让 done / 落库 /
            # 离线快照三条路径拿到的是同一份东西。
            log.close_open()   # 兜底：异常路径上可能还有没发 end 的步骤
            result["steps"] = log.items
            msg = self._persist_answer(thread["id"], result, reply_to)
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
            #
            # 停在哪一步也要留痕：只写「已停止」的话，回看时分不清是模型没起来就
            # 停了，还是查了五步、答案都快好了才停的。cut=True 让前端把那一步写成
            # 「已中断」而不是「进行中」。
            log.close_open(cut=True)
            msg = chat.append(
                thread["id"],
                "assistant",
                "已停止生成，这一轮没有产出答案。",
                meta={"cancelled": True, "usage": exc.usage or {}, "steps": log.items},
                # 挂到这一轮的问句上：它是这一问的一版"答案"（内容是没有答案），
                # 重答之后它退成候选之一，随时能切回来看当时停在哪一步
                reply_to=reply_to,
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
    # 新加身份时这里只会打印新增的那几条（老身份的口令不动）。
    generated = credentials.bootstrap([p["id"] for p in people.all_people()])
    if generated:
        fresh = "首次启动" if len(generated) == len(people.all_people()) else "新身份补口令"
        for pid, pwd in generated.items():
            label = (people.find(pid) or {}).get("name") or pid
            print(f"{fresh}：已生成登录口令 {label}（{pid}） {pwd}", flush=True)
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

    # 材料库顺手清一遍过期件（120 天没被引用）。放在启动时做而不是每个请求：
    # 这是几毫秒的目录扫描，但每轮都扫一遍没有意义 —— 材料是按天变的。
    dropped = materials_mod.prune()
    if dropped:
        print(f"材料库：清掉 {dropped} 份过期材料", flush=True)

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
