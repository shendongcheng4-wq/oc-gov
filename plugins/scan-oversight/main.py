"""扫督办台账，看哪些事项该催了（只读）。

只读的是「查」，不是「催」。真正的催办 / 签收 / 办结走
``POST /api/oversight/{id}/action``，由状态机守卫和身份校验把关，
**不在这里给模型调用** —— 让模型自己判断要不要遵守状态机，等于没有守卫。
"""

from __future__ import annotations

import json
from typing import Any

from octop_harness.plugins import PluginContext

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _ensure_paths() -> None:
    """把 core 及其子模块放进 sys.path。

    自包含：不依赖 app/main.py 事先铺好路径，任何加载器（octop_harness 或内置
    轻量宿主）直接 import 本插件都能跑。
    """
    for sub in ("core", "core/metrics", "core/oversight", "core/meeting"):
        p = ROOT / sub
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))



def _payload(data: Any, text: str) -> str:
    return json.dumps(
        {"octop_ui": {"renderer": "oversight_list", "version": 1}, "data": data, "text": text},
        ensure_ascii=False,
    )


async def scan_oversight(days: int = 3) -> str:
    """扫出逾期 / 今日到期 / 临期 / 签收超时 / 停滞的事项。"""
    import oversight  # 由 toolhost 放进 sys.path

    days_n = max(1, min(int(days or 3), 30))
    r = oversight.scan(days_n)
    buckets: dict[str, list[dict[str, Any]]] = r.get("buckets") or {}

    lines: list[str] = [r.get("headline") or ""]
    for label in ("逾期", "今日到期", "临期", "签收超时", "停滞"):
        rows = buckets.get(label) or []
        if not rows:
            continue
        lines.append(f"\n【{label}】{len(rows)} 件")
        for row in rows:
            left = row.get("days_left")
            when = f"剩 {left} 天" if isinstance(left, int) and left >= 0 else f"已逾期 {abs(left or 0)} 天"
            # 逾期天数已经在 when 里说过一遍，risk_reasons 里的同义项不再重复
            reasons = "；".join(
                r for r in (row.get("risk_reasons") or []) if not str(r).startswith("已逾期")
            )
            lines.append(
                f"· {row.get('title')}｜{row.get('assignee')}｜{row.get('status')}｜{when}"
                + (f"｜{reasons}" if reasons else "")
            )

    if r.get("hit_count", 0) == 0:
        lines.append("没有需要催办的事项。")
    return _payload(r, "\n".join(lines))


def setup(ctx: PluginContext) -> None:
    _ensure_paths()
    ctx.tool(
        "scan_oversight",
        scan_oversight,
        description=(
            "扫描督办台账，返回逾期、今日到期、临期、签收超时、停滞的事项清单，"
            "每条带责任单位与风险原因。只读。days 是临期窗口天数（默认 3，1–30）。"
            "这个工具不能催办、不能签收、不能办结。"
        ),
    )
