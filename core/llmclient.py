"""通用「让模型返回一段 JSON」的调用层。

为什么单独抽一个模块：oc-gov 里有两种模型用法 ——
1. 问数的意图解析（`core/metrics/llm.py`）：只输出查询计划，不碰数字；
2. 会议纪要的事项抽取（`core/meeting/extract.py`）：只输出结构化字段 + 原文引文。

两者对模型的约束完全不同（prompt 不同、校验不同），但**连接方式和请求体是同一套**。
所以这里只管「按 provider/model 标识取连接参数 → 发一条 chat/completions → 把 JSON
解出来」，业务约束留在各自的调用方。之前这段代码在 llm.py 里，会议助手要用就得复制
一份，那是明确的重复，所以提到这里。

不做的事（有意）：
- 这一层**不自己重试**。失败原样抛给调用方 —— 「要不要再试、要不要换模型」是
  业务决定（问数可以降级到规则，会议纪要不行），写死在这里只能做错。
  调用方用 `retryable(exc)` 判断同一个模型再试有没有意义。
- 不做流式。这两处要的都是完整 JSON，没有可读正文流可推。
- 不缓存。同一句话两次解析结果可能不同，缓存会把「模型飘了」这件事藏起来。

## 用量采集（track_usage）

`usage` 以前只写进 trace 的一句摘要里，而 trace 不落库 —— 花掉的 token 就永久丢了。
现在每次请求都把 usage 记进一个 contextvar 里的 sink，由上层用 `with track_usage()`
圈住一轮对话，结束时 `summarize()` 汇成一份可落库的结构。

用 contextvar 而不是模块级 list：服务是多线程的（ThreadingHTTPServer 每请求一线程），
模块级累加器会把两个人的对话算到一个人头上。新线程的 context 是空的，正好互不干扰。
"""

from __future__ import annotations

import contextvars
import json
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any

import modelcfg


class ModelUnavailable(RuntimeError):
    """没有可用的模型连接。调用方据此决定是退回规则还是直接拒绝。"""


class ModelError(RuntimeError):
    """调用到了，但失败了（网络/额度/返回不是合法 JSON）。

    `kind` / `status` 是给重试逻辑看的：同样是失败，「鉴权错了」再试一百次也没用，
    「429 限流」等半秒就好，「连接超时」说明网断了、换一个 provider 才可能活。
    以前上层只能靠字符串里有没有 "401" 来猜，那是不可靠的。
    """

    def __init__(self, message: str, *, kind: str = "other", status: int | None = None):
        super().__init__(message)
        self.kind = kind          # http | network | json | other
        self.status = status


def retryable(exc: BaseException) -> bool:
    """同一个模型再试一次有没有意义。

    不重试的情形：鉴权失败（401/403）、请求不合法（400/404/422）、模型不存在。
    这些换个模型可能好，但同一个模型再试只是白等。
    """
    if not isinstance(exc, ModelError):
        return False
    if exc.kind in ("network", "json"):
        return True
    if exc.kind == "http":
        code = int(exc.status or 0)
        return code in (408, 425, 429) or code >= 500
    return False


# ── 用量采集 ────────────────────────────────────────────────────────────

_sink: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "ocgov_llm_usage", default=None
)


@contextmanager
def track_usage():
    """圈住一轮对话，收集期间所有模型调用的用量。

    用法：`with track_usage() as sink: ...`，结束后 `summarize(sink)`。
    嵌套时内层会覆盖外层 —— 本项目没有嵌套场景，真出现了也是内层那圈更准。
    """
    bucket: list[dict[str, Any]] = []
    token = _sink.set(bucket)
    try:
        yield bucket
    finally:
        _sink.reset(token)


def _record(entry: dict[str, Any]) -> None:
    bucket = _sink.get()
    if bucket is not None:
        bucket.append(entry)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def summarize(bucket: list[dict[str, Any]] | None) -> dict[str, Any]:
    """把逐次调用压成一份可落库的用量。

    失败调用也计入 `calls`，但 token 按 0 算（服务端可能根本没计费）。
    `reported` 表示"至少有一次调用真的返回了 usage"—— 否则 total 为 0 只代表
    对方没报，不代表没花钱，界面不能把它显示成「0 tokens」。
    """
    bucket = bucket or []
    prompt = sum(int((c.get("usage") or {}).get("prompt_tokens") or 0) for c in bucket)
    completion = sum(int((c.get("usage") or {}).get("completion_tokens") or 0) for c in bucket)
    total = sum(
        int((c.get("usage") or {}).get("total_tokens") or 0) for c in bucket
    ) or (prompt + completion)
    return {
        "calls": len(bucket),
        "failed": sum(1 for c in bucket if not c.get("ok")),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "ms": sum(int(c.get("ms") or 0) for c in bucket),
        "reported": any(bool(c.get("usage")) for c in bucket),
        "models": sorted({str(c.get("model") or "") for c in bucket if c.get("model")}),
    }


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
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        _record({"ok": False, "model": conn["model"], "ms": _ms(started)})
        raise ModelError(
            f"HTTP {exc.code}: {detail}", kind="http", status=int(exc.code)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _record({"ok": False, "model": conn["model"], "ms": _ms(started)})
        raise ModelError(f"连接失败: {exc}", kind="network") from exc

    # 到这一步 HTTP 已经成功、token 也已经计费，所以先记用量再解析内容：
    # 后面即使 JSON 不合法，这通电话的钱是真花了，不该在账上消失。
    _record(
        {
            "ok": True,
            "model": body.get("model") or conn["model"],
            "model_ref": conn["ref"],
            "usage": body.get("usage") or {},
            "ms": _ms(started),
        }
    )

    try:
        text = body["choices"][0]["message"]["content"]
        data = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ModelError(f"返回不是合法 JSON: {str(exc)[:200]}", kind="json") from exc
    if not isinstance(data, dict):
        raise ModelError(
            f"返回的 JSON 顶层不是对象，而是 {type(data).__name__}", kind="json"
        )

    return {
        "data": data,
        "model": body.get("model") or conn["model"],
        "model_ref": conn["ref"],
        "usage": body.get("usage") or {},
        "fell_back": bool(conn.get("fell_back")),
    }


def chat_text(
    system_prompt: str,
    user_content: str,
    *,
    history: list[dict[str, str]] | None = None,
    model_ref: str | None = None,
    temperature: float = 0.0,
    timeout: float = 60.0,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """发一条**纯文本**请求，返回 {"text": str, "model": ..., "usage": ...}。

    与 chat_json 的唯一区别：不设 `response_format`，也不解析 JSON，直接把模型的
    文本吐回来。给那些「要模型说人话、不要结构化输出」的场景用（比如专家解读）。
    避开 JSON 模式这个最脆弱的环节 —— 模型偶发输出前导语、或字符串被 max_tokens
    截断，都不会再让整段报废。

    抛 ModelUnavailable / ModelError，语义与 chat_json 一致。
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
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        _record({"ok": False, "model": conn["model"], "ms": _ms(started)})
        raise ModelError(
            f"HTTP {exc.code}: {detail}", kind="http", status=int(exc.code)
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _record({"ok": False, "model": conn["model"], "ms": _ms(started)})
        raise ModelError(f"连接失败: {exc}", kind="network") from exc

    _record(
        {
            "ok": True,
            "model": body.get("model") or conn["model"],
            "model_ref": conn["ref"],
            "usage": body.get("usage") or {},
            "ms": _ms(started),
        }
    )

    try:
        msg = body["choices"][0]["message"]
        text = msg.get("content")
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"返回没有内容: {str(exc)[:200]}", kind="json") from exc

    # 推理模型（deepseek-v4-pro 等）偶发把 max_tokens 全花在 thinking 上，
    # 导致 content 为空、finish_reason=length。此时 reasoning_content 里往往有
    # 已经想好的结论，取它尾部兜底，别让一次正常的调用变成「空回答」。
    text = str(text or "").strip()
    if not text:
        reasoning = str((body["choices"][0]["message"].get("reasoning_content")) or "").strip()
        if reasoning:
            # thinking 是流水账，取最后一句作结论（去掉可能残留的思考痕迹）
            tail = reasoning.split("\n")[-1].strip().lstrip("。-–—").strip()
            text = tail[:500]

    return {
        "text": text,
        "model": body.get("model") or conn["model"],
        "model_ref": conn["ref"],
        "usage": body.get("usage") or {},
        "fell_back": bool(conn.get("fell_back")),
    }
