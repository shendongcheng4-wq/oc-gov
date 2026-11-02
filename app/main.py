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

import json
import os
import re
import sys
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
import extract as meeting_extract  # noqa: E402
import ledger as meetings  # noqa: E402
import llm  # noqa: E402
import llmclient  # noqa: E402
import modelcfg  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
import saved as saved_mod  # noqa: E402
import schedule as schedule_mod  # noqa: E402
import notify as notify_mod  # noqa: E402
import briefdoc as brief_mod  # noqa: E402
import knowledge as knowledge_mod  # noqa: E402
import experts as expert_mod  # noqa: E402


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

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _person(self, body: dict | None = None) -> str:
        """当前身份：请求头 > 请求体 > 查询串 > 默认。

        这是个**演示身份**，不是登录态 —— 谁都能声称自己是市长。所有涉及身份的
        写操作都只能落到 core/people 的白名单里，未知 id 一律回落到默认身份，
        避免一个过期 localStorage 把数据写到不存在的键上。
        """
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
                        "mock": True,
                    }
                )

            if path == "/api/overview":
                return self._json({"ok": True, **wb.overview()})

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

            if path == "/api/experts":
                return self._json(
                    {
                        "ok": True,
                        "experts": expert_mod.all_experts(),
                        "max": expert_mod.MAX_PER_CALL,
                        "model_ready": expert_mod.llmclient.available(),
                        "rule": "专家只给解读，不报数字；解读里出现数字会被整段丢弃。",
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
                return self._json(
                    {
                        "ok": True,
                        "items": ov.list_items(
                            status=(q.get("status") or [""])[0] or None,
                            assignee=(q.get("assignee") or [""])[0] or None,
                            only_risk=(q.get("only_risk") or [""])[0] in ("1", "true", "yes"),
                        ),
                        "stats": ov.stats(),
                        "scan": ov.scan(window),
                        "statuses": ov.STATUSES,
                        "today": ov.today().isoformat(),
                    }
                )

            parts = _parts(path, "/api/oversight/")
            if len(parts) == 1:
                one = ov.detail(parts[0])
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

            if path.startswith("/api/metric/"):
                code = path.rsplit("/", 1)[-1].upper()
                detail = wb.metric_detail(code)
                if detail is None:
                    return self._json({"ok": False, "error": "unknown_metric", "code": code}, 404)
                return self._json({"ok": True, **detail})

            if path == "/api/ask/examples":
                return self._json({"ok": True, "items": EXAMPLES})

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
                return self._json({"ok": True, "items": chat.list_threads(), "stats": chat.stats()})

            tid = _tail(path, "/api/threads/")
            if tid:
                thread = chat.get(tid)
                if thread is None:
                    return self._json({"ok": False, "error": "thread_not_found"}, 404)
                return self._json({"ok": True, "thread": thread})

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
        body = self._body()

        try:
            if path == "/api/ask":
                return self._ask(body)

            if path == "/api/ask/stream":
                return self._ask_stream(body)

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

            if path == "/api/threads":
                thread = chat.create(
                    title=body.get("title"),
                    model_ref=body.get("model_ref") or modelcfg.active_ref(),
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
                res = ov.create_item(
                    title=str(body.get("title") or ""),
                    assignee=str(body.get("assignee") or ""),
                    deadline=str(body.get("deadline") or ""),
                    standard=str(body.get("standard") or ""),
                    source=str(body.get("source") or "市政府办交办"),
                    source_ref=str(body.get("source_ref") or ""),
                    co_assignees=[str(x) for x in (body.get("co_assignees") or [])],
                    priority=str(body.get("priority") or "一般"),
                    actor=str(body.get("actor") or "市政府办"),
                )
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

            # ---- 会议助手 ----
            if path == "/api/meetings":
                meeting = meetings.create(
                    title=str(body.get("title") or ""),
                    text=str(body.get("text") or ""),
                    held_at=str(body.get("held_at") or ""),
                    host=str(body.get("host") or ""),
                    attendees=body.get("attendees") or [],
                    source=str(body.get("source") or "manual"),
                )
                return self._json({"ok": True, "meeting": meetings.brief(meeting)})

            parts = _parts(path, "/api/meetings/")
            if len(parts) == 2 and parts[1] == "parse":
                return self._parse_meeting(parts[0], body)
            if len(parts) == 2 and parts[1] == "commit":
                result = meetings.commit(parts[0], body.get("item_ids") or [], actor=str(body.get("actor") or "市政府办"))
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
                removed = chat.delete(tid)
                # 会话没了，指向它的收藏也得清 —— 否则收藏夹里留点点开是空白的条目
                if removed:
                    saved_mod.purge_thread(tid)
                return self._delete_ok(removed)

            parts = _parts(path, "/api/meetings/")
            if len(parts) == 1:
                return self._delete_ok(meetings.delete(parts[0]))

            self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    # ---------------- PATCH ----------------
    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        try:
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
        """
        if ov.get(iid) is None:
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
            actor=str(body.get("actor") or "市政府办"),
            text=text,
            reason=reason,
            evidence=evidence,
            percent=body.get("percent"),
        )
        if not result.get("ok"):
            return self._json(result, 409)
        return self._json({**result, "item": ov.detail(iid), "stats": ov.stats()})

    def _urge(self, body: dict) -> None:
        """批量催办。

        和会议 commit 相反，这里**不整体回滚**：催办只是往事件流里记一条提醒，
        部分成功没有副作用；硬要原子性反而会让能催的也催不出去。逐条报结果，
        失败的写清原因，由调用方决定要不要重试。
        """
        ids = [str(i) for i in (body.get("ids") or []) if str(i).strip()]
        if not ids:
            return self._json({"ok": False, "error": "没有选择要催办的事项。"}, 400)
        if len(ids) > 200:
            return self._json({"ok": False, "error": f"一次最多催办 200 条，收到 {len(ids)} 条。"}, 400)

        actor = str(body.get("actor") or "市政府办")
        custom = str(body.get("text") or "").strip()
        done: list[dict] = []
        failed: list[dict] = []

        for iid in ids:
            one = ov.detail(iid)
            if one is None:
                failed.append({"item_id": iid, "error": "没有这件事项。"})
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
    def _prepare(self, body: dict) -> tuple[dict, list, str]:
        """建/取会话、取历史、落用户消息。返回 (thread, history, question)。"""
        question = str(body.get("question") or "").strip()
        if not question:
            raise ValueError("empty_question")

        thread = chat.ensure(body.get("thread_id"), model_ref=body.get("model_ref"))

        # 会话粘性：新会话先钉住当前默认模型，后续轮次沿用（对应 octop 的
        # threads.model_ref）。这样用户之后改了全局默认，老会话不会被悄悄换掉。
        pinned = body.get("model_ref") or thread.get("model_ref")
        if not pinned:
            pinned = modelcfg.active_ref()
        if pinned and pinned != thread.get("model_ref"):
            chat.set_model_ref(thread["id"], pinned)
            thread = chat.get(thread["id"]) or thread

        # 先取历史再落本条，否则当前问题会在 history 和 messages 里各出现一次
        history = chat.context(thread["id"])
        chat.append(thread["id"], "user", question)
        return thread, history, question

    def _persist_answer(self, thread_id: str, result: dict) -> dict | None:
        """落一轮回答。

        meta 里存 sources/notes/facts 是为了**历史消息能重新渲染成同样的样式**；
        不存 trace 是因为回看历史不需要调用链路，省一半体积。给模型用的上下文
        只取 metric_names/intent（见 core/chat.py），这些字段不会进模型。
        """
        info = result.get("planner_info") or {}
        # 专家解读要跟着落库，否则回看历史时那段凭空消失 —— 现在专家是「对话中」
        # 的常驻角色，缺失比以前的临时勾选更刺眼。裁剪规则见 experts.to_meta：
        # 只留回放字段，且这些字段绝不会进模型上下文（core/chat.py 的 _summary 只取三项）。
        views = expert_mod.to_meta(result.get("expert_views"))
        return chat.append(
            thread_id,
            "assistant",
            str(result.get("headline") or ""),
            meta={
                "refused": bool(result.get("refused")),
                "planner": result.get("planner"),
                "intent": result.get("intent"),
                "model": info.get("model"),
                "metric_codes": result.get("metric_codes") or [],
                "metric_names": result.get("metric_names") or [],
                "sources": result.get("sources") or [],
                "notes": result.get("notes") or [],
                "facts": result.get("facts") or [],
                "knowledge": knowledge_mod.to_meta(result.get("knowledge")),
                "expert_views": views,
            },
        )

    def _ask(self, body: dict) -> None:
        try:
            thread, history, question = self._prepare(body)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

        result = ask_mod.ask(
            question,
            history=history,
            model_ref=thread.get("model_ref"),
            expert_ids=_expert_ids(body),
            person_id=self._person(body),
        )
        result["planner_info"] = llm.llm_info()
        msg = self._persist_answer(thread["id"], result)
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
            thread, history, question = self._prepare(body)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def push(payload: dict) -> None:
            # 客户端中途关页面时 wfile 会抛 BrokenPipe。这一轮仍然跑完并落库
            # （对应 octop 的「断开不杀 turn」语义），只是没人接了，不该因此报错。
            try:
                self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        push(
            {
                "type": "start",
                "thread_id": thread["id"],
                "model_ref": thread.get("model_ref"),
                "turns": len(history) // 2,
            }
        )

        try:
            result = ask_mod.ask(
                question,
                history=history,
                model_ref=thread.get("model_ref"),
                on_step=lambda name, data: push({"type": name, **data}),
                expert_ids=_expert_ids(body),
                person_id=self._person(body),
            )
            result["planner_info"] = llm.llm_info()
            msg = self._persist_answer(thread["id"], result)
            push(
                {
                    "type": "done",
                    "thread_id": thread["id"],
                    "message_id": (msg or {}).get("id"),
                    "seq": (msg or {}).get("seq"),
                    "result": result,
                }
            )
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            push({"type": "error", "error": f"{type(exc).__name__}: {exc}"})


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    info = modelcfg.llm_info()
    print(f"AI 市长工作台已启动： http://127.0.0.1:{port}", flush=True)
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
