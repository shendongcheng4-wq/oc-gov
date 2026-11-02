"""通用「让模型返回一段 JSON」的调用层。

为什么单独抽一个模块：oc-gov 里有两种模型用法 ——
1. 问数的意图解析（`core/metrics/llm.py`）：只输出查询计划，不碰数字；
2. 会议纪要的事项抽取（`core/meeting/extract.py`）：只输出结构化字段 + 原文引文。

两者对模型的约束完全不同（prompt 不同、校验不同），但**连接方式和请求体是同一套**。
所以这里只管「按 provider/model 标识取连接参数 → 发一条 chat/completions → 把 JSON
解出来」，业务约束留在各自的调用方。之前这段代码在 llm.py 里，会议助手要用就得复制
一份，那是明确的重复，所以提到这里。

不做的事（有意）：
- 不做重试。模型调用失败时上层要么退回规则、要么直接报错，静默重试只会让用户
  等更久还不知道发生了什么。
- 不做流式。这两处要的都是完整 JSON，没有可读正文流可推。
- 不缓存。同一句话两次解析结果可能不同，缓存会把「模型飘了」这件事藏起来。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import modelcfg


class ModelUnavailable(RuntimeError):
    """没有可用的模型连接。调用方据此决定是退回规则还是直接拒绝。"""


class ModelError(RuntimeError):
    """调用到了，但失败了（网络/额度/返回不是合法 JSON）。"""


def available() -> bool:
    return modelcfg.available()


def info() -> dict[str, Any]:
    return modelcfg.llm_info()


def chat_json(
    system_prompt: str,
    user_content: str,
    *,
    history: list[dict[str, str]] | None = None,
    model_ref: str | None = None,
    temperature: float = 0.0,
    timeout: float = 60.0,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """发一条 JSON 模式请求，返回 {"data": 解析后的 dict, "model": ..., "usage": ...}。

    `history` 里的 user/assistant 消息会插在 system 之后、当前 user 之前，保持
    模型看到的是真正的多轮结构（而不是被压平成一坨文本 —— 压平会让指代解析变差）。

    抛 ModelUnavailable / ModelError，不返回半成品 —— 调用方不需要判断 None。
    """
    conn = modelcfg.resolve(model_ref)
    if conn is None:
        raise ModelUnavailable("no_model_configured")

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for item in history or []:
        if item.get("role") in ("user", "assistant") and item.get("content"):
            messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": user_content})

    payload: dict[str, Any] = {
        "model": conn["model"],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens

    req = urllib.request.Request(
        f"{conn['base_url']}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {conn['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ModelError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelError(f"连接失败: {exc}") from exc

    try:
        text = body["choices"][0]["message"]["content"]
        data = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ModelError(f"返回不是合法 JSON: {str(exc)[:200]}") from exc
    if not isinstance(data, dict):
        raise ModelError(f"返回的 JSON 顶层不是对象，而是 {type(data).__name__}")

    return {
        "data": data,
        "model": body.get("model") or conn["model"],
        "model_ref": conn["ref"],
        "usage": body.get("usage") or {},
        "fell_back": bool(conn.get("fell_back")),
    }
