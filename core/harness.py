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

from pathlib import Path
from typing import Any

import modelcfg

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"
WORKSPACE_DIR = ROOT / "data" / "_harness"

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
    """构造（并缓存）一个 HarnessAgent。失败返回 None 并记录原因。"""
    global _AGENT, _AGENT_ERR
    if _AGENT is not None:
        return _AGENT
    if not _try_import():
        _AGENT_ERR = "octop_harness 未安装"
        return None

    conn = modelcfg.resolve()
    if conn is None:
        _AGENT_ERR = "没有可用的模型配置"
        return None

    try:
        from octop_harness import HarnessAgent, HarnessAgentConfig
        from octop_harness.plugins import PluginRegistry, build_plugin_tools, load_all
    except Exception as exc:  # noqa: BLE001
        _AGENT_ERR = f"import 失败: {exc}"
        return None

    # 1) 加载 plugins/ 下的 octop 风格插件（含 plugin.yaml 的子目录）
    try:
        loaded = load_all(PLUGINS_DIR, install_deps=False) if PLUGINS_DIR.is_dir() else []
    except Exception as exc:  # noqa: BLE001
        loaded = []
        _AGENT_ERR = f"插件加载失败: {exc}"

    # 2) 取出全部插件工具（默认全部启用；不传 agent_plugins 表示「装了即启用」）
    plugin_tools: list[Any] = []
    try:
        plugin_tools = build_plugin_tools(agent_plugins={}, global_plugins={})
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
        )
        _AGENT = HarnessAgent(cfg)
    except Exception as exc:  # noqa: BLE001
        _AGENT_ERR = f"构造 HarnessAgent 失败: {type(exc).__name__}: {exc}"
        _AGENT = None
    return _AGENT


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
        return 0
    try:
        from octop_harness.plugins import PluginRegistry
        return len(PluginRegistry().list_plugins())
    except Exception:  # noqa: BLE001
        return 0


def list_plugins() -> list[dict[str, Any]]:
    """返回已加载插件的元信息清单（id/name/description/version/tools）。"""
    if not _ensure_plugins_loaded():
        return []
    try:
        from octop_harness.plugins import PluginRegistry
        out = []
        for p in PluginRegistry().list_plugins():
            out.append(
                {
                    "id": p.manifest.id,
                    "version": p.manifest.version,
                    "name": p.manifest.name,
                    "description": p.manifest.description,
                    "tools": [r.name for r in p.tools],
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
    return {
        "available": available(),
        "import_error": import_error(),
        "agent_error": agent_error(),
        "plugins": plugin_count(),
        "plugin_list": list_plugins(),
        "plugins_dir": str(PLUGINS_DIR),
    }
