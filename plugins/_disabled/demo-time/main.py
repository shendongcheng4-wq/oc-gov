"""示例工具插件：当前时间。

照 octop 插件约定写：plugin.yaml 声明 + main.py 定义 setup(ctx)，用 ctx.tool(...)
注册可调用工具。工具是 async 函数，参数签名即调用协议。
"""

from __future__ import annotations

from datetime import UTC, datetime

from octop_harness.plugins import PluginContext


async def get_current_time(tz: str = "Asia/Shanghai") -> str:
    """Return the current date and time in the given timezone."""
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        zone = UTC
    return datetime.now(zone).strftime("%Y-%m-%d %H:%M:%S %Z")


def setup(ctx: PluginContext) -> None:
    ctx.tool(
        "get_current_time",
        get_current_time,
        description="获取当前日期和时间（默认东八区）。",
    )
