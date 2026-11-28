"""真实数据覆盖层 —— 已核实的溧阳市公开统计数据。

这是 mockdata 之上的一个「真实值优先」的薄层：罗盘查指标时，先到这里找
有没有已核实的真实值，有就用真实值，没有才回落到 mockdata 的演示序列。

设计意图：
- 不改 mockdata 的 24 个月演示序列（走势图仍用它），只在「当期大数」这一层
  用真实值覆盖，避免为了接一个年度数去硬掰月度结构。
- 每一条真实值都必须带三件套：口径、来源（谁发布）、披露时间 —— 和 mockdata
  的 source_of 同构，界面上「点来源看口径」照样能工作。
- 两种粒度并存：`REAL_PERIOD` 是逐期真数（统计月报/主要指标完成情况表），
  `REAL_ANNUAL` 是年度公报数（GDP 这类按年发布的）。逐期优先于年度。

为什么以前只有年度：公报只公布全年数，硬拆月会编。2026-11 拿到统计局
《主要指标完成情况表》后，1—6 月、1—9 月两期有了官方口径的逐期真数，
逐期覆盖才有意义 —— 月报有什么期就只接什么期，不插值、不外推。

真实接入的下一步（不是现在做）：
- 把这里改成「调各部门接口」即可，取值、口径、出处、时效逻辑都不动。
"""

from __future__ import annotations

from typing import Any

# 数据期次。真实 GDP 是年度数，披露在次年（统计公报通常在次年 3-4 月发布）。
# 2025 年公报已出（2026 年发布），所以「最新可得真实年度」是 2025。
REAL_LATEST_YEAR = 2025

# --------------------------------------------------------------------------
# 逐期真数：code -> {期次: 记录}
# --------------------------------------------------------------------------
# 数据来源（同一张表，五个指标一次公布）：
#   《2026 年 1-9 月溧阳市国民经济主要指标完成情况表》
#   市统计局 2026-11-03 发布，索引号 014143475/2026-00013
#   https://www.liyang.gov.cn/html/czly/2026/AQCCAJNM_1103/35947.html
#   《2026 年 1-6 月溧阳市国民经济主要指标完成情况表》2026-07-30
#   https://www.liyang.gov.cn/html/czly/2026/AQCCAJNM_0730/35373.html
#
# 字段：value 为当期实绩（单位与 mockdata.METRICS 一致），yoy 为同比（%）；
#       growth 类指标（单位 %）的 value 本身就是增速，yoy 留空。
# 口径：全部为年内累计数，同比是与上年同期累计数相比，不是环比。
_REPORT_Q3 = "《2026年1-9月溧阳市国民经济主要指标完成情况表》"
_REPORT_Q2 = "《2026年1-6月溧阳市国民经济主要指标完成情况表》"
_PUB_Q3 = "2026-11-03"
_PUB_Q2 = "2026-07-30"

REAL_PERIOD: dict[str, dict[str, dict[str, Any]]] = {
    "GDP_TOTAL": {
        "2026-09": {
            "value": 1300.07, "yoy": 5.8,
            "report": _REPORT_Q3, "published_at": _PUB_Q3,
        },
    },
    "GY_OUTPUT": {
        "2026-06": {
            "value": 1647.37, "yoy": 11.7,
            "report": _REPORT_Q2, "published_at": _PUB_Q2,
        },
        "2026-09": {
            "value": 2585.23, "yoy": 18.5,
            "report": _REPORT_Q3, "published_at": _PUB_Q3,
        },
    },
    "ELEC_GY": {
        "2026-06": {
            "value": 63.88, "yoy": 12.5,
            "report": _REPORT_Q2, "published_at": _PUB_Q2,
        },
        "2026-09": {
            "value": 100.47, "yoy": 10.2,
            "report": _REPORT_Q3, "published_at": _PUB_Q3,
        },
    },
    "FAI_TOTAL": {
        "2026-06": {
            "value": 223.02, "yoy": -8.8,
            "report": _REPORT_Q2, "published_at": _PUB_Q2,
        },
        "2026-09": {
            "value": 290.86, "yoy": -9.4,
            "report": _REPORT_Q3, "published_at": _PUB_Q3,
        },
    },
    "FAI_GROWTH": {
        # 增速类：value 就是同比增速（%），没有「绝对增量」这一说
        "2026-06": {
            "value": -8.8,
            "report": _REPORT_Q2, "published_at": _PUB_Q2,
        },
        "2026-09": {
            "value": -9.4,
            "report": _REPORT_Q3, "published_at": _PUB_Q3,
        },
    },
    "BUDGET_REV": {
        "2026-06": {
            "value": 72.16, "yoy": 1.8,
            "report": _REPORT_Q2, "published_at": _PUB_Q2,
        },
        "2026-09": {
            "value": 94.9, "yoy": 4.7,
            "report": _REPORT_Q3, "published_at": _PUB_Q3,
        },
    },
}

_PERIOD_DEFINITION = (
    "年内累计数（1 月至当月末），同比为与上年同期累计数相比，不是环比。"
)

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
    # 其他年度值待接入：规上工业总产值 3194.26 亿(-7.4%)、一般公共预算收入
    # 112.8 亿(五年从 73.8 亿升至 112.8 亿)、动力电池产业 1031.2 亿(+24.7%)等，
    # 均在同源公报里，需要时按同样结构补。
}


def periods_of(code: str) -> list[str]:
    """某指标已接入真数的期次（升序）。没有真数返回空列表。"""
    return sorted(REAL_PERIOD.get(str(code or ""), {}))


def has_period_data(code: str) -> bool:
    return bool(REAL_PERIOD.get(str(code or "")))


def real_period_value(code: str, period: str) -> dict[str, Any] | None:
    """取某指标某一期的真数。没有这一期返回 None，由调用方决定回落还是拒答。"""
    return REAL_PERIOD.get(str(code or ""), {}).get(str(period or ""))


def real_period_source(code: str, period: str) -> dict[str, Any] | None:
    """逐期真数的出处。形状对齐 mockdata.source_of，`mock` 为 False。"""
    r = real_period_value(code, period)
    if r is None:
        return None
    y, m = period.split("-")
    return {
        "code": code,
        "department": "市统计局",
        "report": r["report"],
        "period": f"{int(y)}年{int(m)}月",
        "published_at": r["published_at"],
        "definition": _PERIOD_DEFINITION,
        "api": f"GET /api/metrics/{code}",
        "mock": False,
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
