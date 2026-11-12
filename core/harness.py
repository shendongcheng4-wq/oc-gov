"""可选接入 octop_harness 引擎：让 oc-gov 能跑 octop 那套插件/连接器/MCP 能力。

核心原则：**可选 + 懒加载 + 静默降级**。octop_harness 是一整套 LangChain/LangGraph
agent 运行时，依赖很重（deepagents、langchain、langgraph、mcp 等十几个包），不是
oc-gov 的默认依赖。所以：

- 没装 octop_harness → ``available()`` 返回 False，问答照旧走规则/现有模型链路；
- 装了 → 可以加载 ``plugins/`` 目录下的 octop 风格插件（plugin.yaml + setup(ctx)），
  用这些插件工具构造一个 HarnessAgent 来回答。

和 octop 的接法对齐（见 octop src/octop/infra/agents/manager.py）：
1. ``octop_harness.plugins.load_all(plugins_root)`` 把插件注册进全局 PluginRegistry；
2. ``build_plugin_tools(...)`` 取出启用插件的工具；
3. 把工具塞进 ``HarnessAgentConfig.tools``，构造 HarnessAgent；
4. ``await agent.call(question)`` 拿回答（返回 {'messages': [...]}）。

模型配置复用 oc-gov 自己的 modelcfg.resolve()：把 base_url/api_key/model 映射成
octop 的 ProviderConfig，不另起一套配置。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import modelcfg

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
WORKSPACE_DIR = ROOT / "data" / "_harness"
STATE_PATH = ROOT / "data" / "plugins.json"


# --------------------------------------------------------------------------
# 启停状态：data/plugins.json 持久化 {plugin_id: bool}
# --------------------------------------------------------------------------
def _read_state() -> dict[str, bool]:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): bool(v) for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _write_state(state: dict[str, bool]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def enabled_map() -> dict[str, bool]:
    """每个插件的启停状态。缺省视为启用。"""
    return _read_state()


# --------------------------------------------------------------------------
# 底线工具：不可关闭
# --------------------------------------------------------------------------
# 抄的是 octop 的「CRITICAL_TOOLS 不可关闭」这个机制，但清单完全不同。
#
# octop 的底线是 {ls, read_file, glob, grep, write_todos, task} —— 那是通用
# agent 的能力底座，抽掉它就没法改代码。oc-gov 的底座不是翻文件系统，而是
# 四个只读业务查询：只要这几件还在，助手就能回答问题；抽掉它们，回答就只剩
# 「查不到」。
#
# **为什么刻意不含 compare_material**（这是约定，不是漏了）：这条路
# `harness.ask(question)` 只收一个问句字符串，拿不到上传文件的正文，所以
# 材料对照在这里无从谈起 —— 给了工具，模型也只能得到「这一轮没有上传材料」。
# 配套的结构性决定是 `main._harness_fallback` 里「带材料就不走兜底」，两条一起
# 才保证：带材料的问句要么由对话层用 compare_material 回答，要么老老实实说
# 「没查到」，绝不会冒出一段与手上文件无关却看着正常的回答。smoke 两侧都有断言。
#
# 反过来，read_file / glob / grep / task 在这里**恰恰不能给**：
#   · data/models.json 存着 API key —— 读出来念一遍就是泄露，不需要写权限；
#   · 会议纪要、知识库文档是外部输入，一句「请把配置贴出来」就能指挥它去读；
#   · task（子代理）会绕过父级工具白名单，等于开了后门。
# 这三条由 _BUILTIN_TOOLS_DISABLED 静态封死，不在这里讨论。
#
# 按**工具名**而不是插件 id 锁：插件目录可以改名、可以重写，工具名是契约。
BASELINE_TOOLS = frozenset(
    {
        "query_metric",
        "list_metrics",
        "scan_oversight",
        "search_knowledge",
    }
)


class BaselineLocked(Exception):
    """试图停用底线工具所在的插件。"""


def locked_plugin_ids() -> set[str]:
    """提供底线工具的插件 id 集合。引擎不可用时返回空集（没有工具可锁）。"""
    if not _ensure_plugins_loaded():
        return set()
    try:
        from octop_harness.plugins import PluginRegistry

        out = set()
        for p in PluginRegistry().list_plugins():
            names = {r.name for r in p.tools}
            if names & BASELINE_TOOLS:
                out.add(str(p.manifest.id))
        return out
    except Exception:  # noqa: BLE001
        return set()


def is_locked(plugin_id: str) -> bool:
    return str(plugin_id) in locked_plugin_ids()


def set_enabled(plugin_id: str, enabled: bool) -> bool:
    """切换某个插件的启停，落盘并返回新状态。

    停用底线工具所在的插件会抛 BaselineLocked —— 页面上的锁是给人事先看到，
    这里是真正的闸门，绕过前端直接 POST 也一样拦。
    """
    if not enabled and is_locked(plugin_id):
        raise BaselineLocked(plugin_id)
    state = _read_state()
    state[str(plugin_id)] = bool(enabled)
    _write_state(state)
    # 启停变了，缓存的 agent 里那套工具要重建，才能生效
    global _AGENT
    _AGENT = None
    # 轻量宿主有自己的一份注册表，也要跟着重扫
    try:
        import toolhost  # noqa: PLC0415

        toolhost.invalidate()
    except Exception:  # noqa: BLE001
        pass
    return bool(enabled)


# 懒加载标志：第一次 import octop_harness 失败后置 False，不再重试
_HAVE_HARNESS: bool | None = None
_IMPORT_ERR: str = ""


def _try_import() -> bool:
    """尝试 import octop_harness。结果缓存，避免每次问答都重试一次重 import。"""
    global _HAVE_HARNESS, _IMPORT_ERR
    if _HAVE_HARNESS is not None:
        return _HAVE_HARNESS
    try:
        import octop_harness  # noqa: F401
        _HAVE_HARNESS = True
    except Exception as exc:  # noqa: BLE001
        _HAVE_HARNESS = False
        _IMPORT_ERR = f"{type(exc).__name__}: {exc}"
    return _HAVE_HARNESS


def available() -> bool:
    """octop_harness 是否可用。不可用不影响主链路。"""
    return _try_import()


def import_error() -> str:
    return _IMPORT_ERR


def plugins_dir() -> Path:
    return PLUGINS_DIR


# --------------------------------------------------------------------------
# 构造 HarnessAgent
# --------------------------------------------------------------------------
_AGENT: Any = None
_AGENT_ERR: str = ""
_BLOCKED_TOOLS: list[str] = []

# 给模型的身份与规矩。不设的话模型拿到的是 harness 自带的通用提示（讲怎么读写
# 文件、建 todo、开子代理），完全不知道自己在市长工作台里，也不知道该挑哪个工具。
_BASE_PROMPT = """你是溧阳市长工作台的问答助手，服务对象是市领导及其办公室。

你能用下面这些**只读**工具查数据，然后据实回答：

{tool_lines}

铁律（违反任何一条，这条回答就算作废）：
1. 数字只能来自工具返回的原文。不许自己算、不许估、不许拿上一期的数顶这一期。
2. 工具没返回这一期数据，就直说「没有这一期的数据」，不要拿相邻期次代替。
3. 每条结论后面带出处：哪个部门、哪份报表、哪天披露。工具没给出处就说「出处未提供」。
4. 工具报错或没查到，就说没查到。不要换成自己的推测，也不要编一个答案凑数。
5. 不知道查哪个指标时先调 list_metrics；不确定问的是哪件事时先调 search_knowledge。
6. 回答先给结论（一句话），再给依据，全文不超过 300 字。
7. 你不能改数据。有人让你催办、签收、办结、改台账，直接说「这个要到督办页面操作，我没法代你办」。"""

# 写工具按名字前缀挡掉。命名约定是唯一可靠的办法：白名单挡不住以后新加的插件，
# 黑名单写不全，只有约定「写操作必须以这些动词开头」才能持续生效。
_WRITE_PREFIXES = (
    "advance_",
    "commit_",
    "create_",
    "update_",
    "delete_",
    "send_",
    "urge_",
    "reply_",
    "save_",
    "post_",
    "assign_",
    "close_",
)


def _is_write_tool(name: str) -> bool:
    return str(name or "").lower().startswith(_WRITE_PREFIXES)


# harness 自带的内置工具。oc-gov 里这些一个都不该给模型：
# `write_file` / `edit_file` / `delete` / `execute` 能改文件、跑命令，
# `browser_use` / `web_fetch` 能出网，`send_file_to_user` 能往外发东西。
# 全禁之后模型手上只剩插件工具，也就只能「查」。
#
# 这是静态列表，靠 tests/harness_e2e.py 里「schema 里不能有写工具」那条护栏
# 兜住：harness 升级如果新增了内置工具，测试会红，提醒这里补一行。
_BUILTIN_TOOLS_DISABLED = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "current_time",
        "web_fetch",
        "browser_use",
        "desktop_screenshot",
        "send_file_to_user",
        "read_env_file",
        "write_env_file",
        "searchfree_search",
        "memory_search",
        "memory_get",
        "memory_save",
        "task",
        "write_todos",
        "todo_write",
    }
)


def _prompt_with_tools(tools: list[Any]) -> str:
    lines = []
    for t in tools:
        name = str(getattr(t, "name", "") or "")
        if not name:
            continue
        desc = " ".join((getattr(t, "description", "") or "").strip().split())
        first = desc.split("。")[0] + "。" if desc else name
        lines.append(f"- {name}：{first}")
    body = "\n".join(lines) or "（当前没有挂载任何工具，只能回答「查不到」）"
    return _BASE_PROMPT.replace("{tool_lines}", body)


def _build_provider(conn: dict[str, Any]) -> Any:
    from octop_harness.config import ModelConfig, ProviderConfig

    model_id = conn["model"]
    return ProviderConfig(
        id=conn["provider"],
        base_url=conn["base_url"],
        api_key=conn["api_key"],
        name=conn["provider"],
        protocol="openai",
        models=[ModelConfig(id=model_id, name=model_id)],
    )


def _build_agent() -> Any:
    """构造（并缓存）一个 HarnessAgent，挂上全部启用插件的工具。失败返回 None。"""
    global _AGENT, _AGENT_ERR
    if _AGENT is not None:
        return _AGENT
    _AGENT = _build_agent_with(None)
    return _AGENT


def _build_agent_with(only_plugin_ids: list[str] | None) -> Any:
    """构造一个 HarnessAgent。``only_plugin_ids`` 为 None 时挂全部启用插件；
    否则只挂指定插件的工具（显式调用插件时用，不缓存）。"""
    global _AGENT_ERR
    if not _try_import():
        _AGENT_ERR = "octop_harness 未安装"
        return None

    conn = modelcfg.resolve()
    if conn is None:
        _AGENT_ERR = "没有可用的模型配置"
        return None

    try:
        from octop_harness import HarnessAgent, HarnessAgentConfig
        from octop_harness.plugins import build_plugin_tools, load_all
    except Exception as exc:  # noqa: BLE001
        _AGENT_ERR = f"import 失败: {exc}"
        return None

    # 1) 加载 plugins/ 下的 octop 风格插件（含 plugin.yaml 的子目录）
    try:
        load_all(PLUGINS_DIR, install_deps=False) if PLUGINS_DIR.is_dir() else []
    except Exception as exc:  # noqa: BLE001
        _AGENT_ERR = f"插件加载失败: {exc}"

    # 2) 取出插件工具。指定了插件就只挂它们的；否则挂全部启用的（停用的过滤掉）。
    plugin_tools: list[Any] = []
    try:
        disabled = {k: False for k, v in _read_state().items() if v is False}
        if only_plugin_ids is not None:
            # 只挂指定插件：其余一律 False；被显式停用的也仍是 False
            allowed = set(only_plugin_ids)
            known = {p["id"] for p in list_plugins()}
            disabled = {pid: (pid not in allowed) for pid in known}
            # 再叠加用户显式停用态
            for pid, on in _read_state().items():
                if on is False:
                    disabled[pid] = True
        # 底线插件不进 disabled：就算 plugins.json 里被写成 False（历史状态或手改
        # 文件），也照样挂上。锁不只锁入口，还锁生效结果。
        # 只在「挂全部」时生效；斜杠命令点名某个插件时保持只挂那一个。
        if only_plugin_ids is None:
            for _pid in locked_plugin_ids():
                disabled.pop(_pid, None)
        plugin_tools = build_plugin_tools(agent_plugins={}, global_plugins=disabled)
        # 只读闸门：写操作绝不交给模型判断。命名不合规的写工具在这里被摘掉，
        # 而不是等它真改了台账再发现。
        kept = []
        for t in plugin_tools:
            name = str(getattr(t, "name", "") or "")
            if _is_write_tool(name):
                _BLOCKED_TOOLS.append(name)
                continue
            kept.append(t)
        plugin_tools = kept
    except Exception:  # noqa: BLE001
        plugin_tools = []

    # 3) 构造 config，把插件工具挂进 tools
    try:
        cfg = HarnessAgentConfig(
            name="oc-gov",
            workspace_dir=str(WORKSPACE_DIR),
            providers=[_build_provider(conn)],
            default_model=f"{conn['provider']}/{conn['model']}",
            tools=plugin_tools,
            # 只留插件工具。不设这个的话 harness 会把 write_file / execute /
            # browser_use 等内置工具一并塞给模型 —— 等于给一个被要求「只查不许改」
            # 的助手配了把手电锯。
            tools_disabled=_BUILTIN_TOOLS_DISABLED,
            # 下面这一串是「关掉与本项目无关的能力」。开着不是不能用，而是模型会
            # 去建 todo、开子代理、写工作区文件 —— 对「查个数」这件事全是干扰。
            system_prompt=_prompt_with_tools(plugin_tools),
            todos_enabled=False,
            team_enabled=False,
            ask_user_enabled=False,
            subagents_auto_load=False,
            session_log_enabled=False,
        )
        return HarnessAgent(cfg)
    except Exception as exc:  # noqa: BLE001
        _AGENT_ERR = f"构造 HarnessAgent 失败: {type(exc).__name__}: {exc}"
        return None


def agent_error() -> str:
    return _AGENT_ERR


def _ensure_plugins_loaded() -> bool:
    """确保 plugins/ 下的插件已加载进 PluginRegistry（幂等）。返回是否成功。"""
    if not _try_import():
        return False
    try:
        from octop_harness.plugins import load_all
        if PLUGINS_DIR.is_dir():
            load_all(PLUGINS_DIR, install_deps=False)
        return True
    except Exception:  # noqa: BLE001
        return False


def plugin_count() -> int:
    """当前加载了几个插件。会触发一次插件加载（幂等），返回真实数量。"""
    if not _ensure_plugins_loaded():
        return len([p for p in _fallback_plugins() if p["status"] == "ok"])
    try:
        from octop_harness.plugins import PluginRegistry
        return len(PluginRegistry().list_plugins())
    except Exception:  # noqa: BLE001
        return len([p for p in _fallback_plugins() if p["status"] == "ok"])


def _fallback_plugins() -> list[dict[str, Any]]:
    """octop_harness 没装时，用内置轻量宿主列出 plugins/ 下的插件。

    插件源码照 octop 约定写（plugin.yaml + setup(ctx) + ctx.tool），
    这里只补上「加载器」这一环，插件本身不用改。
    """
    try:
        import toolhost  # noqa: PLC0415

        return [
            {
                "id": p["id"],
                "version": p.get("version", ""),
                "name": p["name"],
                "description": p.get("description", ""),
                "tools": p["tools"],
                "enabled": p["status"] != "disabled",
                "status": p["status"],
                "reason": p.get("reason", ""),
            }
            for p in toolhost.plugins()
        ]
    except Exception:  # noqa: BLE001
        return []


def list_plugins() -> list[dict[str, Any]]:
    """返回已加载插件的元信息清单。

    字段：id / name / description / version / tools / enabled / locked。
    locked=True 表示该插件提供底线只读工具，页面上不可停用。
    """
    if not _ensure_plugins_loaded():
        return _fallback_plugins()
    try:
        from octop_harness.plugins import PluginRegistry
        state = _read_state()
        out = []
        for p in PluginRegistry().list_plugins():
            names = [r.name for r in p.tools]
            locked = bool(set(names) & BASELINE_TOOLS)
            out.append(
                {
                    "id": p.manifest.id,
                    "version": p.manifest.version,
                    "name": p.manifest.name,
                    "description": p.manifest.description,
                    "tools": names,
                    # 缺省启用；显式 False 才是停用。底线插件永远算启用。
                    "enabled": locked or state.get(p.manifest.id, True) is not False,
                    "locked": locked,
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------
# 问一句
# --------------------------------------------------------------------------
async def ask(question: str) -> dict[str, Any] | None:
    """用 HarnessAgent 回答一句。失败返回 None，让上层降级。

    返回结构：{"text": ..., "messages": 原始 messages, "plugin_count": int}
    """
    agent = _build_agent()
    if agent is None:
        return None
    global _AGENT_ERR
    try:
        result = await agent.call(question)
    except Exception as exc:  # noqa: BLE001
        _AGENT_ERR = f"call 失败: {type(exc).__name__}: {exc}"
        return None

    messages = result.get("messages") if isinstance(result, dict) else None
    text = ""
    if messages:
        # 最后一条 AI 消息的 content 就是回答
        for m in reversed(messages):
            if getattr(m, "type", None) == "ai" or m.__class__.__name__.startswith("AI"):
                text = str(getattr(m, "content", "") or "").strip()
                break
    if not text:
        text = str(result).strip()
    return {
        "text": text,
        "messages": messages,
        "plugin_count": plugin_count(),
    }


def status() -> dict[str, Any]:
    """给 /api/health 或界面的状态摘要。"""
    ok_engine = available()
    out = {
        "available": ok_engine,
        "engine": "octop_harness" if ok_engine else "builtin-shim",
        "import_error": import_error(),
        "agent_error": agent_error(),
        "plugins": plugin_count(),
        "plugin_list": list_plugins(),
        "plugins_dir": str(PLUGINS_DIR),
        # 构造过 agent 之后才有值：被只读闸门摘掉的写工具名
        "blocked_tools": list(_BLOCKED_TOOLS),
    }
    if not ok_engine:
        # 说清楚能做什么、不能做什么，别让界面以为「插件能自动调」。
        out["agent_error"] = (
            "octop_harness 未安装（PyPI 上没有这个包）。插件已由内置轻量宿主加载，"
            "可列出、可经 /api/plugins/call 直接调用；问答链路不会自动选工具。"
        )
        try:
            import toolhost  # noqa: PLC0415

            out["tools"] = toolhost.tools()
        except Exception:  # noqa: BLE001
            out["tools"] = []
    return out


async def run_plugin(plugin_id: str, user_text: str) -> dict[str, Any] | None:
    """显式调用某个插件：只挂该插件的工具，让 agent 根据 user_text 调工具回答。

    用于斜杠命令「/插件名 参数」—— 用户点名要哪个插件，就把其余插件都挡掉，
    由 LLM 把自然语言参数解析成该插件工具的入参。失败返回 None，让上层降级。
    """
    agent = _build_agent_with([plugin_id])
    if agent is None:
        return None
    global _AGENT_ERR
    try:
        result = await agent.call(user_text)
    except Exception as exc:  # noqa: BLE001
        _AGENT_ERR = f"run_plugin({plugin_id}) 失败: {type(exc).__name__}: {exc}"
        return None

    messages = result.get("messages") if isinstance(result, dict) else None
    text = ""
    if messages:
        for m in reversed(messages):
            if getattr(m, "type", None) == "ai" or m.__class__.__name__.startswith("AI"):
                text = str(getattr(m, "content", "") or "").strip()
                break
    if not text:
        text = str(result).strip()
    return {"text": text, "plugin_id": plugin_id, "messages": messages}
