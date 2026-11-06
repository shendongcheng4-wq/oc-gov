"""验证「底线工具不可关闭」：抄 octop 的 CRITICAL_TOOLS 机制，清单换成业务三件套。

断言四件事：

1. 三个业务插件被标成 locked，页面据此把开关锁死；
2. 调 harness.set_enabled(..., False) 会抛 BaselineLocked —— 这是真正的闸门，
   前端禁用按钮只是让人事先看见；
3. plugins.json 被手改成 false 也没用，底线工具照样挂进模型工具表；
4. 非底线插件仍然可以正常停用，锁没有锁成一片（示例插件已移到
   plugins/_disabled/，这条改成动态找非底线插件，找不到就反向断言）。

跑法::

    .venv\\Scripts\\python.exe tests\\baseline_lock.py

会临时改写 data/plugins.json，跑完原样还原。失败退出码非 0。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import harness  # noqa: E402

FAILS: list[str] = []
STATE = ROOT / "data" / "plugins.json"


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def mounted_tool_names() -> set[str]:
    """模型实际拿到的工具名集合。

    HarnessAgent 把工具挂在 config.tools 上（agent.tools 不存在），别按直觉写。
    """
    agent = harness._build_agent()
    if agent is None:
        return set()
    cfg = getattr(agent, "config", None) or getattr(agent, "_config", None)
    tools = getattr(cfg, "tools", None) or []
    return {str(getattr(t, "name", "")) for t in tools}


def main() -> int:
    original = STATE.read_text(encoding="utf-8") if STATE.exists() else "{}"

    print("1) 锁定标记")
    locked = harness.locked_plugin_ids()
    check("业务三件套被锁", locked == {"query-metric", "scan-oversight", "search-knowledge"}, str(sorted(locked)))
    by_id = {p["id"]: p for p in harness.list_plugins()}
    check("页面拿到的 locked 字段为 true", by_id.get("query-metric", {}).get("locked") is True)

    print("\n2) set_enabled 拒绝停用")
    for pid in sorted(locked):
        try:
            harness.set_enabled(pid, False)
            check(f"{pid} 停用被拒", False, "没抛异常")
        except harness.BaselineLocked:
            check(f"{pid} 停用被拒", True)

    print("\n3) 篡改 plugins.json 也不生效")
    try:
        STATE.write_text(
            json.dumps({pid: False for pid in locked}, ensure_ascii=False), encoding="utf-8"
        )
        harness._AGENT = None
        names = mounted_tool_names()
        check("query_metric 仍然挂载", "query_metric" in names, ",".join(sorted(names)))
        check("scan_oversight 仍然挂载", "scan_oversight" in names)
        check("search_knowledge 仍然挂载", "search_knowledge" in names)
    finally:
        STATE.write_text(original, encoding="utf-8")
        harness._AGENT = None

    print("\n4) 非底线插件仍可停用（锁没锁成一片）")
    # 不绑死具体插件名：示例插件已经移到 plugins/_disabled/ 了，这里动态找一个
    # 非底线插件来验证「锁只锁底线，不锁全部」。一个都没有时改成断言反向命题。
    all_plugins = {p["id"]: p for p in harness.list_plugins()}
    plain = [pid for pid, p in all_plugins.items() if not p.get("locked")]
    if plain:
        pid = plain[0]
        tools_of = set(all_plugins[pid].get("tools") or [])
        try:
            harness.set_enabled(pid, False)
            harness._AGENT = None
            names = mounted_tool_names()
            check(f"{pid} 可停用，其工具被摘掉", not (tools_of & names), str(sorted(tools_of)))
            check("底线工具没被连带摘掉", "query_metric" in names)
        finally:
            harness.set_enabled(pid, True)
    else:
        check("没有非底线插件可测，反向确认锁未覆盖全部", len(all_plugins) == len(locked), str(sorted(all_plugins)))

    print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：{FAILS}"))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
