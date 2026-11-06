"""轻量工具宿主 —— 让 plugins/ 下的插件在没有 octop_harness 时也能跑。

为什么需要这一层：plugins/ 里的插件写的是
``from octop_harness.plugins import PluginContext``，但 ``octop-harness``
不在 PyPI 上（``pip install octop-harness`` 会报 No matching distribution），
所以这些插件一直处于「写了但加载不了」的状态。

这里用两样东西补上缺口，**插件源码一行都不用改**：

1. 兼容 shim：没装 octop_harness 时，往 ``sys.modules`` 注入一个最小的
   ``octop_harness.plugins``，只实现 ``PluginContext.tool()``。装了真 harness
   就走真的（``setdefault``，不覆盖）。
2. 只读闸门：工具必须登记在 ``READONLY_TOOLS`` 里才允许被模型调用。
   写操作（催办 / 签收 / 办结 / 会议提交）永远不在这里 —— 它们要过
   ``app/main.py`` 的 ``WRITE_POLICY`` + ``_gate_write()`` + 审计，
   让模型自己判断要不要遵守状态机，等于没有守卫。

用法::

    from core import toolhost
    toolhost.load_all()
    toolhost.schemas()                 # 给模型看的 function schema
    await toolhost.call("query_metric", code="GY_VA_GROWTH")
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = ROOT / "plugins"

# 只允许模型调用登记在这里的只读工具。新增写操作一律不要往这里加。
READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "query_metric",
        "list_metrics",
        "scan_oversight",
        "search_knowledge",
    }
)

# 单次调用上限。工具里不许发网络请求，8 秒只防死循环。
DEFAULT_TIMEOUT = 8.0


class ToolError(RuntimeError):
    """工具不存在 / 未授权 / 执行失败。"""


# --------------------------------------------------------------------------
# 兼容 shim
# --------------------------------------------------------------------------
class PluginContext:
    """octop ``PluginContext`` 的最小子集：只实现 ``tool()``。

    参数形状与 octop_harness.plugins.context.PluginContext.tool 对齐，
    多出来的 ``middleware`` / ``skills`` 不在轻量宿主的能力范围内。
    """

    def __init__(self, manifest: dict[str, Any], source_path: Path) -> None:
        self.manifest = manifest
        self.source_path = source_path
        self.tools: list[dict[str, Any]] = []

    @property
    def plugin_id(self) -> str:
        return str(self.manifest.get("id") or "")

    def tool(
        self,
        name: str,
        fn: Any,
        *,
        description: str = "",
        config_fields: list[dict[str, Any]] | None = None,
    ) -> None:
        tool_name = str(name).strip()
        if not tool_name:
            raise ValueError("tool name must not be empty")
        self.tools.append(
            {
                "name": tool_name,
                "fn": fn,
                "description": description or tool_name,
                "config_fields": list(config_fields or []),
                "plugin_id": self.plugin_id,
            }
        )


def _install_shim() -> bool:
    """没装真 harness 时注入 shim。返回是否注入了。"""
    try:
        import octop_harness.plugins  # noqa: F401

        return False
    except Exception:  # noqa: BLE001
        pass

    pkg = types.ModuleType("octop_harness")
    pkg.__path__ = []  # 让 import 系统认它是包
    sub = types.ModuleType("octop_harness.plugins")
    sub.PluginContext = PluginContext  # type: ignore[attr-defined]
    sub.__all__ = ["PluginContext"]  # type: ignore[attr-defined]
    pkg.plugins = sub  # type: ignore[attr-defined]
    sys.modules.setdefault("octop_harness", pkg)
    sys.modules.setdefault("octop_harness.plugins", sub)
    return True


def shim_active() -> bool:
    """当前是不是靠 shim 在跑（即没装 octop_harness）。"""
    mod = sys.modules.get("octop_harness.plugins")
    if mod is None:
        return False
    # 真 harness 一定导出 PluginRegistry，shim 只有 PluginContext
    return not hasattr(mod, "PluginRegistry")


def _ensure_paths() -> None:
    """插件里要能 import core 下的业务模块（mockdata / oversight / knowledge）。"""
    for p in (
        ROOT / "core",
        ROOT / "core" / "metrics",
        ROOT / "core" / "oversight",
        ROOT / "core" / "meeting",
    ):
        s = str(p)
        if p.is_dir() and s not in sys.path:
            sys.path.insert(0, s)


# --------------------------------------------------------------------------
# manifest 解析
# --------------------------------------------------------------------------
def _parse_manifest(path: Path) -> dict[str, Any]:
    """读 plugin.yaml。有 pyyaml 就用，没有就退到极简解析（只为不引依赖）。"""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass

    out: dict[str, Any] = {}
    cur_list: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip()
        if stripped.startswith("- "):
            if cur_list:
                out.setdefault(cur_list, []).append(stripped[2:].strip())
            continue
        if line[0] in " \t":
            continue  # 嵌套块（ui: 之类）本宿主不关心
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v:
            out[k] = v
            cur_list = None
        else:
            out.setdefault(k, [])
            cur_list = k
    return out


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------
_REGISTRY: dict[str, dict[str, Any]] = {}
_LOADED: list[dict[str, Any]] = []
_LOADED_ONCE = False
_ERR: str = ""
_STATE_PATH = ROOT / "data" / "plugins.json"


def _enabled(plugin_id: str) -> bool:
    """启停状态与 octop 引擎共用 data/plugins.json。缺省视为启用。"""
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return True
    if not isinstance(raw, dict):
        return True
    return raw.get(plugin_id) is not False


def invalidate() -> None:
    """扔掉已加载的注册表，下次调用重新扫目录。插件开关变了要调这个。"""
    global _LOADED_ONCE
    _LOADED_ONCE = False
    _REGISTRY.clear()
    _LOADED.clear()


def _import_entry(plugin_dir: Path, entry: str) -> Any:
    mod_path = plugin_dir / entry
    if not mod_path.is_file():
        raise ToolError(f"入口文件不存在：{mod_path}")
    alias = "ocgov_plugin_" + "".join(c if c.isalnum() else "_" for c in plugin_dir.name)
    spec = importlib.util.spec_from_file_location(alias, mod_path)
    if spec is None or spec.loader is None:
        raise ToolError(f"无法加载：{mod_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def load_all(*, force: bool = False) -> list[dict[str, Any]]:
    """扫描 plugins/，import 每个 main.py 并执行 setup(ctx)。幂等。"""
    global _LOADED_ONCE, _ERR
    if _LOADED_ONCE and not force:
        return _LOADED

    _install_shim()
    _ensure_paths()
    _REGISTRY.clear()
    _LOADED.clear()
    _ERR = ""
    _LOADED_ONCE = True

    if not PLUGINS_DIR.is_dir():
        _ERR = f"插件目录不存在：{PLUGINS_DIR}"
        return _LOADED

    for d in sorted(PLUGINS_DIR.iterdir()):
        if not d.is_dir():
            continue
        yml = d / "plugin.yaml"
        if not yml.is_file():
            continue
        try:
            manifest = _parse_manifest(yml)
            plugin_id = str(manifest.get("id") or d.name)
            if not _enabled(plugin_id):
                _LOADED.append(
                    {
                        "id": plugin_id,
                        "name": str(manifest.get("name") or plugin_id),
                        "version": str(manifest.get("version") or ""),
                        "description": str(manifest.get("description") or ""),
                        "status": "disabled",
                        "reason": "在 data/plugins.json 里被停用",
                        "tools": [],
                    }
                )
                continue
            if str(manifest.get("kind") or "tool").lower() != "tool":
                # skill / hook 需要真 harness 的中间件与 skill 目录支持，这里跳过并记账
                _LOADED.append(
                    {
                        "id": str(manifest.get("id") or d.name),
                        "name": str(manifest.get("name") or d.name),
                        "version": str(manifest.get("version") or ""),
                        "description": str(manifest.get("description") or ""),
                        "status": "skipped",
                        "reason": f"kind={manifest.get('kind')} 需要 octop_harness，轻量宿主只跑 kind=tool",
                        "tools": [],
                    }
                )
                continue
            mod = _import_entry(d, str(manifest.get("entry") or "main.py"))
            setup = getattr(mod, "setup", None)
            if not callable(setup):
                raise ToolError("main.py 里没有 setup(ctx)")
            ctx = PluginContext(manifest, d)
            setup(ctx)
        except Exception as exc:  # noqa: BLE001
            _LOADED.append(
                {
                    "id": d.name,
                    "name": d.name,
                    "version": "",
                    "description": "",
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "tools": [],
                }
            )
            continue

        names: list[str] = []
        for t in ctx.tools:
            if t["name"] in _REGISTRY:
                t["conflict"] = _REGISTRY[t["name"]]["plugin_id"]
            _REGISTRY[t["name"]] = t
            names.append(t["name"])
        _LOADED.append(
            {
                "id": ctx.plugin_id,
                "name": str(manifest.get("name") or ctx.plugin_id),
                "version": str(manifest.get("version") or ""),
                "description": str(manifest.get("description") or ""),
                "status": "ok",
                "reason": "",
                "tools": names,
            }
        )
    return _LOADED


def plugins() -> list[dict[str, Any]]:
    load_all()
    return list(_LOADED)


def tools() -> list[dict[str, Any]]:
    load_all()
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "plugin_id": t["plugin_id"],
            "readonly": t["name"] in READONLY_TOOLS,
        }
        for t in _REGISTRY.values()
    ]


def status() -> dict[str, Any]:
    return {
        "engine": "octop_harness" if not shim_active() else "builtin-shim",
        "harness_installed": not shim_active(),
        "plugins_dir": str(PLUGINS_DIR),
        "error": _ERR,
        "plugins": plugins(),
        "tools": tools(),
    }


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
def _schema_for(fn: Any) -> dict[str, Any]:
    try:
        hints = inspect.get_type_hints(fn)
    except Exception:  # noqa: BLE001
        hints = {}
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {"type": "object", "properties": {}, "required": []}

    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        ann = hints.get(pname, p.annotation)
        typ = "string"
        if ann is int:
            typ = "integer"
        elif ann is float:
            typ = "number"
        elif ann is bool:
            typ = "boolean"
        item: dict[str, Any] = {"type": typ}
        if p.default is not inspect.Parameter.empty:
            if isinstance(p.default, (str, int, float, bool)) or p.default is None:
                item["default"] = p.default
        else:
            required.append(pname)
        props[pname] = item
    return {"type": "object", "properties": props, "required": required}


def schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    """OpenAI 风格的 function schema，只吐只读工具。"""
    load_all()
    out = []
    for t in _REGISTRY.values():
        if t["name"] not in READONLY_TOOLS:
            continue
        if names is not None and t["name"] not in names:
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": _schema_for(t["fn"]),
                },
            }
        )
    return out


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------
async def call(name: str, /, *, actor: str = "", timeout: float = DEFAULT_TIMEOUT, **kwargs: Any) -> dict[str, Any]:
    """执行一个只读工具。返回 ``{"ok": bool, "data"/"error": ...}``，从不抛异常。"""
    load_all()
    t = _REGISTRY.get(name)
    if t is None:
        return {"ok": False, "error": "unknown_tool", "tool": name}
    if name not in READONLY_TOOLS:
        _audit(actor, name, kwargs, ok=False, note="blocked_not_readonly")
        return {
            "ok": False,
            "error": "not_readonly",
            "tool": name,
            "hint": "写操作必须走 HTTP 写口（WRITE_POLICY + 状态机守卫），不给模型直接调",
        }

    fn = t["fn"]
    started = _now()
    try:
        if inspect.iscoroutinefunction(fn):
            out = await asyncio.wait_for(fn(**kwargs), timeout=timeout)
        else:
            out = await asyncio.wait_for(asyncio.to_thread(fn, **kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        _audit(actor, name, kwargs, ok=False, note="timeout", elapsed=_elapsed(started))
        return {"ok": False, "error": "timeout", "tool": name, "timeout_s": timeout}
    except TypeError as exc:
        _audit(actor, name, kwargs, ok=False, note=f"bad_args: {exc}")
        return {"ok": False, "error": "bad_arguments", "tool": name, "detail": str(exc),
                "schema": _schema_for(fn)}
    except Exception as exc:  # noqa: BLE001
        _audit(actor, name, kwargs, ok=False, note=f"{type(exc).__name__}: {exc}")
        return {"ok": False, "error": "tool_failed", "tool": name, "detail": str(exc)}

    _audit(actor, name, kwargs, ok=True, elapsed=_elapsed(started))
    return {"ok": True, "tool": name, "data": _decode(out)}


def call_sync(name: str, **kwargs: Any) -> dict[str, Any]:
    """同步封装，给脚本/测试用。"""
    return asyncio.run(call(name, **kwargs))


def _decode(out: Any) -> Any:
    """插件惯用「返回 JSON 字符串」的约定（octop 的 renderer payload）。

    这里把 ``{"octop_ui": ..., "data": ..., "text": ...}`` 拆成 data+text，
    其余字符串原样返回。
    """
    if not isinstance(out, str):
        return out
    s = out.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return s
    try:
        obj = json.loads(s)
    except ValueError:
        return s
    if isinstance(obj, dict) and "text" in obj:
        return {"text": obj.get("text", ""), "data": obj.get("data"), "renderer": (obj.get("octop_ui") or {}).get("renderer")}
    return obj


def _now() -> float:
    import time

    return time.perf_counter()


def _elapsed(started: float) -> float:
    return round((_now() - started) * 1000, 1)


def _audit(actor: str, name: str, args: dict[str, Any], *, ok: bool, note: str = "", elapsed: float | None = None) -> None:
    """工具调用也留痕。审计是旁路，写不进去不影响返回。"""
    try:
        import audit  # noqa: PLC0415

        audit.record(
            actor or "model",
            "tool.call",
            name,
            {"args": args, "ok": ok, "note": note, "ms": elapsed},
        )
    except Exception:  # noqa: BLE001
        pass
