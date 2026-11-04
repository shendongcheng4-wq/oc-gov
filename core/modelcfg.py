"""模型配置：多 provider × 多模型。

设计照搬 octop 的三条骨架，理由都写在下面：

1. **provider 一条记录 + models 数组**，不单独建 models 表。读路径只有「查 provider
   → 解析数组」两步，省掉外键和三套 id 映射。
2. **模型标识统一是 `"provider_name/model_id"` 字符串**。这个串同时充当：界面选项、
   会话粘性（thread.model_ref）、全局默认（active_model）、API 入参。全链路一种标识，
   不用在三种 id 之间转换。
3. **改模型列表后清理悬空引用**。删掉一个模型，`active_model` 和会话里的 `model_ref`
   可能还指着它 —— 不清理的话，界面会显示一个点开就报错的模型。

与 octop 的两处不同（都是刻意的）：

- **不静默回退**。octop 的 `resolve_explicit_default_model` 找不到就返回 None（等于
  AUTO）。这里显式指定了一个不存在的模型时，返回结果里带 `fell_back=True`，让上层
  能告诉用户「你要的模型没配好，已用默认」——政务场景里静默换模型比报错更危险。
- **api_key 不返给前端**。octop 的 `/api/providers` 原样返回 api_key，靠前端打码。
  这里 `list_providers(with_secrets=False)` 用默认值就把 key 换成了掩码，只有调用
  模型时才取真值，减少一次「前端漏打码就等于泄露」的机会。

落盘在 `data/models.json`，与 octop 一样是明文（单机自托管，没有 KMS 可用）。
该目录已在 .gitignore 里，不要提交。
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Any

import store

FILE = "models"
VERSION = 1


def _load_env_file() -> None:
    """自己读 config/local.env，不指望调用方先加载。

    这一步不能省：模型配置首次迁移要读环境变量，而 `python -c "import modelcfg"`
    （或者测试脚本）不会经过 app/main.py 的加载逻辑，不自己读就会迁移出一份空配置。
    `setdefault` 保证真实环境变量优先于文件。
    """
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "config" / "local.env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

# 模型条目的默认值。`chat` 决定它能否被对话选用（embedding/rerank 类模型置 false）。
MODEL_DEFAULTS: dict[str, Any] = {
    "name": "",
    "enabled": True,
    "chat": True,
    "context_window": None,
    "max_output_tokens": None,
}


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _empty() -> dict[str, Any]:
    return {"version": VERSION, "active_model": None, "providers": []}


def state() -> dict[str, Any]:
    """读配置，保证结构完整。首次调用时从环境变量迁移出第一条 provider。"""
    raw = store.read(FILE)
    if not isinstance(raw, dict) or "providers" not in raw:
        raw = _bootstrap_from_env()
        store.write(FILE, raw)
        return raw

    raw.setdefault("version", VERSION)
    raw.setdefault("active_model", None)
    if not isinstance(raw.get("providers"), list):
        raw["providers"] = []

    if not raw["providers"]:
        # 配置被清空了也再试一次环境变量，避免用户删光后页面彻底没法用
        seeded = _bootstrap_from_env()
        if seeded["providers"]:
            raw = seeded
            store.write(FILE, raw)
    return raw


def _bootstrap_from_env() -> dict[str, Any]:
    """把原有的 config/local.env 三件套迁移成一条 provider。

    这样老部署升级上来不用重新配一遍；没有 key 就返回空配置，页面会提示去配。
    """
    key = ""
    for name in ("OCTOP_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        key = (os.environ.get(name) or "").strip()
        if key:
            break
    base = (os.environ.get("OCTOP_LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
    model_id = (os.environ.get("OCTOP_LLM_MODEL") or "deepseek-chat").strip()

    cfg = _empty()
    if not key:
        return cfg

    provider = {
        "id": _new_id("prov"),
        "name": _provider_name_from_url(base),
        "kind": "openai",
        "base_url": base,
        "api_key": key,
        "enabled": True,
        "note": "从 config/local.env 自动导入",
        "models": [dict(MODEL_DEFAULTS, id=model_id, name=model_id)],
        "created_at": _now(),
        "updated_at": _now(),
    }
    cfg["providers"].append(provider)
    cfg["active_model"] = f"{provider['name']}/{model_id}"
    return cfg


def _provider_name_from_url(base_url: str) -> str:
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    host = host.split(":")[0]
    parts = [p for p in host.split(".") if p not in ("api", "www", "com", "cn", "net", "org")]
    return (parts[0] if parts else "custom").replace("-", "_")[:24] or "custom"


# ── 读取 ────────────────────────────────────────────────────────────────


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def list_providers(with_secrets: bool = False) -> list[dict[str, Any]]:
    out = []
    for p in state()["providers"]:
        item = {k: v for k, v in p.items() if k != "api_key"}
        item["has_key"] = bool(p.get("api_key"))
        if with_secrets:
            item["api_key"] = p.get("api_key", "")
        else:
            item["api_key_masked"] = mask_key(p.get("api_key", ""))
        item["models"] = [dict(MODEL_DEFAULTS, **m) for m in (p.get("models") or [])]
        out.append(item)
    return out


def _chat_models(provider: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for m in provider.get("models") or []:
        merged = dict(MODEL_DEFAULTS, **m)
        if merged.get("enabled") and merged.get("chat"):
            items.append(merged)
    return items


def usable_providers() -> list[dict[str, Any]]:
    """可用 = 启用 + 有 base_url + 有 key + 至少一个可对话模型。四个条件缺一不可。"""
    out = []
    for p in state()["providers"]:
        if not p.get("enabled"):
            continue
        if not (p.get("base_url") or "").strip() or not (p.get("api_key") or "").strip():
            continue
        if not _chat_models(p):
            continue
        out.append(p)
    return out


def resolved() -> list[dict[str, Any]]:
    """扁平模型列表，给界面的模型选择器用。**不含任何密钥**。"""
    out = []
    for p in usable_providers():
        for m in _chat_models(p):
            out.append(
                {
                    "ref": f"{p['name']}/{m['id']}",
                    "provider": p["name"],
                    "provider_id": p["id"],
                    "model": m["id"],
                    "label": m.get("name") or m["id"],
                    "context_window": m.get("context_window"),
                    "max_output_tokens": m.get("max_output_tokens"),
                }
            )
    return out


def active_ref() -> str | None:
    ref = state().get("active_model")
    if ref and any(x["ref"] == ref for x in resolved()):
        return ref
    # 配置里的默认模型失效了就落到第一个可用模型，不要留一个点开就报错的默认值
    items = resolved()
    return items[0]["ref"] if items else None


def set_active(ref: str) -> str:
    refs = {x["ref"] for x in resolved()}
    if ref not in refs:
        raise ValueError(f"模型 {ref} 不可用。可选项：{sorted(refs) or '无'}")

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["active_model"] = ref
        return cfg

    store.update(FILE, _do, _empty())
    return ref


# ── 解析成可调用的连接参数 ──────────────────────────────────────────────


def resolve(ref: str | None = None) -> dict[str, Any] | None:
    """把 `"provider/model"` 解析成真正能发请求的参数。

    回退链：显式 ref → active_model → 环境变量。显式 ref 无效时**不静默吞掉**，
    返回的 dict 里 `fell_back=True` 让上层能提示。
    """
    requested = ref
    fell_back = False

    cfg = state()
    target = ref or cfg.get("active_model")

    for p in cfg["providers"]:
        if not p.get("enabled") or not p.get("api_key"):
            continue
        for m in _chat_models(p):
            if f"{p['name']}/{m['id']}" == target:
                return {
                    "ref": target,
                    "requested": requested,
                    "fell_back": False,
                    "provider": p["name"],
                    "base_url": p["base_url"].rstrip("/"),
                    "api_key": p["api_key"],
                    "model": m["id"],
                    "context_window": m.get("context_window"),
                    "max_output_tokens": m.get("max_output_tokens"),
                    "llamacpp": bool(p.get("llamacpp")),
                }

    # 显式 ref 没解析出来，退到第一个可用模型
    if requested:
        fell_back = True
    for p in usable_providers():
        for m in _chat_models(p):
            return {
                "ref": f"{p['name']}/{m['id']}",
                "requested": requested,
                "fell_back": fell_back,
                "provider": p["name"],
                "base_url": p["base_url"].rstrip("/"),
                "api_key": p["api_key"],
                "model": m["id"],
                "context_window": m.get("context_window"),
                "max_output_tokens": m.get("max_output_tokens"),
                "llamacpp": bool(p.get("llamacpp")),
            }

    # 最后退到环境变量（locals.env 没被迁移时的兜底）
    key = ""
    for name in ("OCTOP_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        key = (os.environ.get(name) or "").strip()
        if key:
            break
    if key:
        base = (os.environ.get("OCTOP_LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
        mid = (os.environ.get("OCTOP_LLM_MODEL") or "deepseek-chat").strip()
        return {
            "ref": f"{_provider_name_from_url(base)}/{mid}",
            "requested": requested,
            "fell_back": True,
            "provider": _provider_name_from_url(base),
            "base_url": base,
            "api_key": key,
            "model": mid,
            "context_window": None,
            "max_output_tokens": None,
            "llamacpp": False,
        }
    return None


def llm_info() -> dict[str, Any]:
    """给页面顶部显示当前用的是哪个模型。"""
    conn = resolve()
    if conn is None:
        return {"available": False, "ref": None, "model": None, "provider": None}
    return {
        "available": True,
        "ref": conn["ref"],
        "model": conn["model"],
        "provider": conn["provider"],
        "base_url": conn["base_url"],
    }


def available() -> bool:
    return resolve() is not None


# ── 写入 ────────────────────────────────────────────────────────────────


def _clean_models(models: Any) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for m in models or []:
        if isinstance(m, str):
            m = {"id": m}
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        merged = dict(MODEL_DEFAULTS, **m)
        merged["id"] = mid
        merged["name"] = str(merged.get("name") or mid)
        merged["enabled"] = bool(merged.get("enabled", True))
        merged["chat"] = bool(merged.get("chat", True))
        for k in ("context_window", "max_output_tokens"):
            v = merged.get(k)
            merged[k] = int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        out.append(merged)
    return out


def _clean_provider(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    base = dict(existing or {})
    base["id"] = base.get("id") or _new_id("prov")

    name = str(payload.get("name") or base.get("name") or "").strip()
    if not name:
        raise ValueError("provider 名称不能为空")
    name = name.replace(" ", "_")

    others = {p["name"] for p in state()["providers"] if p["id"] != base["id"]}
    if name in others:
        raise ValueError(f"已存在同名 provider「{name}」")

    base["name"] = name
    base["kind"] = str(payload.get("kind") or base.get("kind") or "openai")
    base["base_url"] = str(payload.get("base_url") or base.get("base_url") or "").strip().rstrip("/")
    if not base["base_url"]:
        raise ValueError("base_url 不能为空")

    # 不传 api_key 就保持原值（对应 octop 那个 UNSET 哨兵：改备注不该把 key 清掉）
    if "api_key" in payload:
        base["api_key"] = str(payload.get("api_key") or "").strip()
    else:
        base.setdefault("api_key", "")

    base["enabled"] = bool(payload.get("enabled", base.get("enabled", True)))
    # llamacpp 标记：本地 llama.cpp/llama-server 服务（OpenAI 兼容但支持 chat_template_kwargs
    # 这类非标准扩展）。请求层据此给推理模型关掉思维链，避免本地小模型把时间花在 thinking 上。
    base["llamacpp"] = bool(payload.get("llamacpp", base.get("llamacpp", False)))
    base["note"] = str(payload.get("note") or base.get("note") or "")
    base["models"] = _clean_models(payload.get("models", base.get("models")))
    base.setdefault("created_at", _now())
    base["updated_at"] = _now()
    return base


def upsert_provider(payload: dict[str, Any]) -> dict[str, Any]:
    """有 id 就更新，没 id 就新增。"""
    pid = payload.get("id")

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        existing = next((p for p in cfg["providers"] if p["id"] == pid), None) if pid else None
        merged = _clean_provider(payload, existing)
        if existing is None:
            cfg["providers"].append(merged)
        else:
            idx = cfg["providers"].index(existing)
            cfg["providers"][idx] = merged
        cfg["active_model"] = _clear_stale(cfg.get("active_model"), cfg["providers"])
        return cfg

    cfg = store.update(FILE, _do, _empty())
    pid_now = payload.get("id") or next(
        (p["id"] for p in cfg["providers"] if p["name"] == payload.get("name", "").replace(" ", "_")), None
    )
    return next((p for p in list_providers(True) if p["id"] == pid_now), {})


def delete_provider(pid: str) -> bool:
    removed = {"name": None}

    def _do(cfg: dict[str, Any]) -> dict[str, Any]:
        keep = []
        for p in cfg["providers"]:
            if p["id"] == pid:
                removed["name"] = p["name"]
            else:
                keep.append(p)
        cfg["providers"] = keep
        cfg["active_model"] = _clear_stale(cfg.get("active_model"), keep)
        return cfg

    store.update(FILE, _do, _empty())
    return removed["name"] is not None


def _clear_stale(active: Any, providers: list[dict[str, Any]]) -> str | None:
    """默认模型指向的 provider/模型被删了，就把默认值换成第一个可用模型。"""
    refs = set()
    for p in providers:
        if not p.get("enabled") or not p.get("api_key"):
            continue
        for m in _chat_models(p):
            refs.add(f"{p['name']}/{m['id']}")
    if active in refs:
        return active
    return sorted(refs)[0] if refs else None


def prune_thread_refs(name_of_deleted_provider: str) -> int:
    """把会话里指向已删 provider 的 model_ref 置空，返回改动条数。

    单独抽出来是为了让 app 层在删除 provider 后调用，避免 core 互相 import。
    """
    import chat  # 延迟导入，core/chat.py 不反向依赖本模块

    return chat.clear_model_refs(prefix=f"{name_of_deleted_provider}/")


# ── 连通性测试 ──────────────────────────────────────────────────────────


def probe(payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """真发一条最小请求，回 {ok, latency_ms} / {ok: false, error}。

    为什么不只查 `GET /models`：那个只能证明鉴权通、路径对，证明不了能推理。
    政务场景里"地址填对了但模型下线"是最常见的一类故障，必须真跑一次。
    """
    base = str(payload.get("base_url") or "").strip().rstrip("/")
    key = str(payload.get("api_key") or "").strip()
    model = str(payload.get("model") or "").strip()

    # 草稿测试允许只传 provider_id，此时用已存的 key
    if not key and payload.get("id"):
        existing = next((p for p in state()["providers"] if p["id"] == payload["id"]), None)
        if existing:
            key = existing.get("api_key", "")
            base = base or existing.get("base_url", "").rstrip("/")
    if not key and payload.get("provider"):
        existing = next((p for p in state()["providers"] if p["name"] == payload["provider"]), None)
        if existing:
            key = existing.get("api_key", "")
            base = base or existing.get("base_url", "").rstrip("/")

    if not base or not key:
        return {"ok": False, "error": "base_url 或 api_key 为空"}
    if not model:
        return {"ok": False, "error": "未指定要测试的模型"}

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        latency = int((time.monotonic() - started) * 1000)
        data = json.loads(raw)
        if not data.get("choices"):
            return {"ok": False, "latency_ms": latency, "error": "响应里没有 choices，可能不是 OpenAI 兼容接口"}
        return {"ok": True, "latency_ms": latency, "model": model, "ref": model}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {_trim(detail or str(e.reason))}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"连不上：{_trim(str(e.reason))}"}
    except TimeoutError:
        return {"ok": False, "error": f"超时（>{timeout:.0f}s）"}
    except Exception as e:  # noqa: BLE001 网络层什么都可能抛，统一转成人话
        return {"ok": False, "error": _trim(f"{type(e).__name__}: {e}")}


def _trim(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def fetch_models(payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """拉远端 `GET /models`，方便用户从列表里挑，而不是手打模型名。"""
    base = str(payload.get("base_url") or "").strip().rstrip("/")
    key = str(payload.get("api_key") or "").strip()
    if not key and payload.get("id"):
        existing = next((p for p in state()["providers"] if p["id"] == payload["id"]), None)
        if existing:
            key = existing.get("api_key", "")
            base = base or existing.get("base_url", "").rstrip("/")
    if not base or not key:
        return {"ok": False, "error": "base_url 或 api_key 为空", "items": []}

    req = urllib.request.Request(
        f"{base}/models",
        headers={"Authorization": f"Bearer {key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        items = []
        for row in data.get("data") or []:
            mid = str(row.get("id") or "").strip()
            if mid:
                items.append({"id": mid, "name": mid})
        items.sort(key=lambda x: x["id"])
        return {"ok": True, "items": items, "count": len(items)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "items": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _trim(f"{type(e).__name__}: {e}"), "items": []}
