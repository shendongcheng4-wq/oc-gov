"""离线验证插件工具链路：假模型 + 真工具。

为什么要有这个：本机测试模型的 context 只有 8192，跑不通真 agent（harness 骨架
本身就超）。但「链路对不对」不该等到有算力才能验证。这里用一个假 OpenAI 服务
顶替模型，断言四件事：

1. 系统提示里带了工具清单和「不许编数字」的铁律；
2. 模型第一轮拿到的是 function schema（不是一段纯文本）；
3. 模型发出 tool_call 之后，工具**真的被执行**，结果回传给模型；
4. 写操作工具不在工具表里（按名字前缀挡掉）。

跑法::

    .venv\\Scripts\\python.exe tests\\harness_e2e.py

不依赖网络，不读写 data/ 之外的东西，失败退出码非 0。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "metrics"))

REQUESTS: list[dict] = []
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILS.append(name)


# --------------------------------------------------------------------------
# 假模型：第一轮发 tool_call，第二轮据工具结果作答
# --------------------------------------------------------------------------
def _completion(message: dict) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 1730000000,
        "model": "mock-1",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # langchain 探活会问 /models
        data = json.dumps({"object": "list", "data": [{"id": "mock-1", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        REQUESTS.append({"path": self.path, "body": body, "chars": len(raw)})

        msgs = body.get("messages") or []
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]

        if not tool_msgs:
            msg = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "query_metric",
                            "arguments": json.dumps({"code": "GY_VA_GROWTH"}),
                        },
                    }
                ],
            }
            payload = _completion(msg)
            payload["choices"][0]["finish_reason"] = "tool_calls"
        else:
            # 从工具返回里把数值抠出来，模拟「照实念」的模型
            blob = str(tool_msgs[-1].get("content") or "")
            m = re.search(r'"value":\s*([0-9.]+)', blob)
            value = m.group(1) if m else "?"
            msg = {
                "role": "assistant",
                "content": (
                    f"规模以上工业增加值增速 2026 年 9 月为 {value}%，同比 +2.2%。"
                    "出处：市统计局《本市统计月报》2026年9月，2026-10-10 披露。"
                ),
            }
            payload = _completion(msg)

        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音
        pass


def _serve() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


# --------------------------------------------------------------------------
def main() -> int:
    import harness
    from octop_harness import HarnessAgent, HarnessAgentConfig
    from octop_harness.config import ModelConfig, ProviderConfig
    from octop_harness.plugins import build_plugin_tools, load_all

    print("== 1. 插件加载 ==")
    load_all(ROOT / "plugins", install_deps=False)
    tools = build_plugin_tools(agent_plugins={}, global_plugins={})
    names = [t.name for t in tools]
    print("  工具：", ", ".join(names))
    check("业务工具已挂上", {"query_metric", "list_metrics", "scan_oversight", "search_knowledge"} <= set(names))

    print("\n== 2. 只读闸门 ==")
    blocked = [n for n in names if harness._is_write_tool(n)]
    check("没有写工具漏进工具表", not blocked, str(blocked))
    check("advance_oversight 会被判为写工具", harness._is_write_tool("advance_oversight"))
    check("commit_items 会被判为写工具", harness._is_write_tool("commit_items"))
    check("query_metric 不被误判", not harness._is_write_tool("query_metric"))

    print("\n== 3. 系统提示 ==")
    prompt = harness._prompt_with_tools(tools)
    check("提示里带工具清单", "query_metric" in prompt and "scan_oversight" in prompt)
    check("提示里带「不许编数字」", "不许自己算" in prompt)
    check("提示里带「不能改数据」", "你不能改数据" in prompt)

    print("\n== 4. 端到端：模型选工具 -> 工具执行 -> 回答 ==")
    srv, port = _serve()
    try:
        cfg = HarnessAgentConfig(
            name="oc-gov",
            workspace_dir=str(ROOT / "data" / "_harness"),
            providers=[
                ProviderConfig(
                    id="mock",
                    base_url=f"http://127.0.0.1:{port}/v1",
                    api_key="mock",
                    name="mock",
                    protocol="openai",
                    models=[ModelConfig(id="mock-1", name="mock-1")],
                )
            ],
            default_model="mock/mock-1",
            tools=tools,
            tools_disabled=harness._BUILTIN_TOOLS_DISABLED,
            system_prompt=prompt,
            todos_enabled=False,
            team_enabled=False,
            ask_user_enabled=False,
            subagents_auto_load=False,
            session_log_enabled=False,
        )
        agent = HarnessAgent(cfg)
        result = asyncio.run(agent.call("规上工业增加值增速现在多少？"))
    finally:
        srv.shutdown()

    check("调了模型两次（先 tool_call 再作答）", len(REQUESTS) >= 2, f"实际 {len(REQUESTS)} 次")

    first = (REQUESTS[0]["body"] if REQUESTS else {}) or {}
    sent_tools = first.get("tools") or []
    sent_names = [((t.get("function") or {}).get("name")) for t in sent_tools if isinstance(t, dict)]
    check("第一轮请求带了 tools schema", bool(sent_tools), f"{len(sent_tools)} 个")
    check("schema 里只有只读工具", not any(harness._is_write_tool(n) for n in sent_names), str(sent_names))

    sys_msg = next((m for m in (first.get("messages") or []) if m.get("role") == "system"), None)
    sys_text = str((sys_msg or {}).get("content") or "")
    check("system 里是我们的提示", "溧阳市长工作台" in sys_text)
    check("system 里列出了工具", "query_metric" in sys_text)

    second = REQUESTS[1]["body"] if len(REQUESTS) > 1 else {}
    tool_msgs = [m for m in (second.get("messages") or []) if m.get("role") == "tool"]
    check("工具结果回传给了模型", bool(tool_msgs), f"{len(tool_msgs)} 条")
    tool_blob = str((tool_msgs[-1] or {}).get("content") or "") if tool_msgs else ""
    check("工具结果里是真实数值（8.9）", "8.9" in tool_blob, tool_blob[:80])

    text = ""
    for m in reversed((result or {}).get("messages") or []):
        if getattr(m, "type", None) == "ai":
            text = str(getattr(m, "content", "") or "")
            break
    check("最终回答拿到了", bool(text.strip()))
    check("回答里带出处", "市统计局" in text, text[:80])

    if REQUESTS:
        biggest = max(r["chars"] for r in REQUESTS)
        print(f"\n  单次请求体最大 {biggest} 字符（约 {biggest // 2} token，估算）")

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：{FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
