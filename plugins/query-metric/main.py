"""查本市经济指标（只读）。

数值全部来自 ``core/metrics/mockdata``，模型只决定「查哪个、哪一期」，
不参与取数，也没有机会编数字 —— 这是这个项目的安全边界。
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
        {"octop_ui": {"renderer": "metric_card", "version": 1}, "data": data, "text": text},
        ensure_ascii=False,
    )


async def query_metric(code: str, period: str | None = None) -> str:
    """查单个指标的最新值、同比、口径与出处。"""
    import mockdata  # 由 toolhost 放进 sys.path

    code = (code or "").strip().upper()
    if not code:
        return _payload({"error": "code_required"}, "请给出指标代码，例如 GY_VA_GROWTH。")

    r = mockdata.query_metric(code, (period or "").strip() or None)
    if not r.get("ok"):
        avail = "、".join(f"{k}（{v['name']}）" for k, v in mockdata.METRICS.items())
        return _payload(
            {"error": r.get("error"), "code": code, "available": list(mockdata.METRICS)},
            f"没有「{code}」这个指标。可用代码：{avail}",
        )

    src = r.get("source") or {}
    text = f"{r.get('name')}（{r.get('period_label')}）为 {r.get('value')}{r.get('unit')}"
    if r.get("yoy") is not None:
        text += f"，同比 {r['yoy']:+.1f}%"
    if r.get("target") is not None:
        text += f"，目标 {r['target']}{r.get('unit')}"
    report = str(src.get("report") or "")
    if report and not report.startswith("《"):
        report = f"《{report}》"
    text += f"。出处：{src.get('department', '')}{report}，{src.get('published_at', '')} 披露"
    return _payload(r, text)


async def list_metrics() -> str:
    """列出全部可用指标代码与名称。不知道代码时先调这个。"""
    import mockdata

    rows = mockdata.list_metrics()
    lines = [f"{m['code']}｜{m['name']}（{m['unit']}，{m['department']}）" for m in rows]
    return _payload({"metrics": rows}, f"共 {len(rows)} 个指标：\n" + "\n".join(lines))


def setup(ctx: PluginContext) -> None:
    _ensure_paths()
    ctx.tool(
        "query_metric",
        query_metric,
        description=(
            "查单个经济指标的最新值、同比、目标、口径与责任部门。"
            "code 为指标代码（如 GY_VA_GROWTH），period 形如 2026-09，省略则取最新一期。"
        ),
    )
    ctx.tool(
        "list_metrics",
        list_metrics,
        description="列出所有可查的指标代码与名称。不确定 code 时先调这个，再调 query_metric。",
    )
