"""真实数据覆盖层 —— 已核实的溧阳市公开统计数据。

这是 mockdata 之上的一个「真实值优先」的薄层：罗盘查指标时，先到这里找
有没有已核实的真实值，有就用真实值，没有才回落到 mockdata 的演示序列。

设计意图：
- 不改 mockdata 的 24 个月演示序列（走势图仍用它），只在「当期大数」这一层
  用真实值覆盖，避免为了接一个年度数去硬掰月度结构。
- 每一条真实值都必须带三件套：口径、来源（谁发布）、披露时间 —— 和 mockdata
  的 source_of 同构，界面上「点来源看口径」照样能工作。
- 真实值只覆盖「年度/全年」这一个粒度。月度指标（工业增速、办结率等）因为
  公报只公布全年数，不硬造月度，等有真实月报再接。

真实接入的下一步（不是现在做）：
- 把这里改成「调各部门接口」即可，取值、口径、出处、时效逻辑都不动。
"""

from __future__ import annotations

from typing import Any

# 数据期次。真实 GDP 是年度数，披露在次年（统计公报通常在次年 3-4 月发布）。
# 2025 年公报已出（2026 年发布），所以「最新可得真实年度」是 2025。
REAL_LATEST_YEAR = 2025

# code -> 真实年度值。结构对齐 mockdata.source_of 的三件套，但多一个 value 与
# yoy（同比，可比价口径）。
# 数据来源（均已多源交叉核对）：
#   - 《2025年溧阳市国民经济和社会发展统计公报》
#   - 常州市统计局《常州统计年鉴2025》
#   - 新华社江苏频道（js.xinhuanet.com）、中吴网（cztv.tv）等多源一致
REAL_ANNUAL: dict[str, dict[str, Any]] = {
    "GDP_TOTAL": {
        "year": 2025,
        "value": 1746.29,          # 亿元，现价
        "yoy": 5.0,                # 可比价增长 %
        "department": "市统计局",
        "report": "《2025年溧阳市国民经济和社会发展统计公报》",
        "published_at": "2026-04",  # 公报通常次年发布，具体日期待核实
        "note": "现价总量；增速为可比价，两者不能相互推算。",
    },
    # 其他真实值待接入：规上工业总产值 3194.26 亿(-7.4%)、一般公共预算收入
    # 112.8 亿(五年从 73.8 亿升至 112.8 亿)、动力电池产业 1031.2 亿(+24.7%)等，
    # 均在同源公报里，需要时按同样结构补。
}


def real_annual_value(code: str) -> dict[str, Any] | None:
    """取某指标的已核实真实年度值。没查到返回 None，由调用方回落 mock。"""
    return REAL_ANNUAL.get(code)


def real_source(code: str) -> dict[str, Any] | None:
    """取真实值的出处元数据，形状对齐 mockdata.source_of 的返回。"""
    r = REAL_ANNUAL.get(code)
    if r is None:
        return None
    return {
        "code": code,
        "department": r["department"],
        "report": r["report"],
        "period": f"{r['year']}年（全年）",
        "published_at": r["published_at"],
        "definition": r.get("note", ""),
        "api": f"GET /api/metrics/{code}",
        "mock": False,
    }
