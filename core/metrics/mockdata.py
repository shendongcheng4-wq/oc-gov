"""城市指标问数原型 —— 模拟指标数据层。

这里是"假接口"，但模拟的是真接口该有的形状：每个数值都必须能回答三件事
——口径是什么、谁给的、什么时候披露的。少任何一件，这个数字就不该被写进材料。

真实接入时只替换 _VALUES / _BREAKDOWNS 的来源（改成调各部门接口），
下面的取值、口径、出处、时效判断逻辑都不用动。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

# --------------------------------------------------------------------------
# 时间轴：2024-11 ~ 2026-10 共 24 个月
# --------------------------------------------------------------------------

TOTAL_MONTHS = 24
FIRST_YEAR, FIRST_MONTH = 2024, 11


def _build_periods() -> list[str]:
    out: list[str] = []
    y, m = FIRST_YEAR, FIRST_MONTH
    for _ in range(TOTAL_MONTHS):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


PERIODS: list[str] = _build_periods()

# 月度指标统一在次月 10 日披露。今天是 2026-11-01，所以最新可得期是 2026-09，
# 10 月数据尚未披露 —— 原型里保留这个"缺口"，因为真实世界就是这样的。
DISCLOSURE_DAY = 10


def _disclosed_on(period: str) -> date:
    y, m = int(period[:4]), int(period[5:7])
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(y2, m2, DISCLOSURE_DAY)


def available_periods(today: date | None = None) -> list[str]:
    today = today or date.today()
    return [p for p in PERIODS if _disclosed_on(p) <= today]


def latest_available(today: date | None = None) -> str:
    return available_periods(today)[-1]


def pending_periods(today: date | None = None) -> list[str]:
    today = today or date.today()
    return [p for p in PERIODS if _disclosed_on(p) > today]


# --------------------------------------------------------------------------
# 指标定义（口径、责任部门、报表、别名）
# 别名用于自然语言匹配；真实系统里这张表应该来自指标中台的指标字典。
# --------------------------------------------------------------------------

METRICS: dict[str, dict[str, Any]] = {
    "GY_VA_GROWTH": {
        "code": "GY_VA_GROWTH",
        "name": "规模以上工业增加值增速",
        "aliases": ["规上工业增加值", "规上工业", "工业增加值", "工业增速", "工业生产", "工业"],
        "unit": "%",
        "direction": "positive",
        "department": "市统计局",
        "report": "《本市统计月报》",
        "frequency": "月",
        "cumulative": False,
        "definition": (
            "规模以上工业企业（年主营业务收入 2000 万元及以上）当月工业增加值同比增速，"
            "按可比价格计算，不含价格因素。"
        ),
        "caveats": [
            "口径限定为年主营业务收入 2000 万元及以上的规上企业，不含规下企业与个体户。",
            "增速为可比价计算，与按现价自行加总的结果会有差异。",
        ],
        "target": 6.5,
        "target_note": "年度目标 6.5%",
        "api": "GET /api/metrics/GY_VA_GROWTH",
    },
    "FAI_PROGRESS": {
        "code": "FAI_PROGRESS",
        "name": "市重大项目年度投资完成率",
        "aliases": [
            "重大项目投资完成率", "重大项目", "重点项目", "投资完成率",
            "项目投资", "投资进度", "项目建设",
        ],
        "unit": "%",
        "direction": "positive",
        "department": "市发展和改革委员会",
        "report": "《市重大项目月度调度表》",
        "frequency": "月",
        "cumulative": True,
        "definition": (
            "列入市重大项目清单的项目，年度累计完成投资额占年度计划投资额的比重。"
            "全市数为分区县完成额加权汇总，不等于各区县完成率的简单平均。"
        ),
        "caveats": [
            "累计口径：年初从 0 起算，年内逐月递增，不做环比。",
            "全市完成率为加权值，与各区县完成率直接平均的结果不一致属正常。",
        ],
        "api": "GET /api/metrics/FAI_PROGRESS",
    },
    "TZ_12345_CLOSURE": {
        "code": "TZ_12345_CLOSURE",
        "name": "12345 群众诉求按期办结率",
        "aliases": [
            "12345", "诉求办结率", "按期办结率", "办结率", "群众诉求", "热线",
            "工单办结", "民生诉求",
        ],
        "unit": "%",
        "direction": "positive",
        "department": "市政务服务和数据管理局（12345 热线中心）",
        "report": "《12345 热线运行月报》",
        "frequency": "月",
        "cumulative": False,
        "definition": (
            "当月受理工单中，在承诺时限内办结的工单数占应办结工单数的比重。"
            "超期后补办的工单不计入按期办结。"
        ),
        "caveats": [
            "分母为应办结工单数，不是全部受理量；跨月工单在办结月计入。",
            "全市数为按工单量加权，不等于各承办单位办结率的简单平均。",
        ],
        "api": "GET /api/metrics/TZ_12345_CLOSURE",
    },
    "AQI_GOOD_DAYS": {
        "code": "AQI_GOOD_DAYS",
        "name": "空气质量优良天数比例",
        "aliases": [
            "空气质量", "优良天数", "优良率", "AQI", "蓝天", "大气质量", "环境空气质量",
        ],
        "unit": "%",
        "direction": "positive",
        "department": "市生态环境局",
        "report": "《环境空气质量月报》",
        "frequency": "月",
        "cumulative": False,
        "definition": "当月 AQI 指数≤100 的天数占当月有效监测天数的比重，按《环境空气质量标准》GB 3095-2012 评价。",
        "caveats": [
            "分母为有效监测天数，因设备校准等原因缺测的天数不计入。",
            "受季节影响显著，冬季取暖期偏低属常态，跨月比较需注意季节性。",
        ],
        "api": "GET /api/metrics/AQI_GOOD_DAYS",
    },
    "UR_RATE": {
        "code": "UR_RATE",
        "name": "城镇调查失业率",
        "aliases": ["调查失业率", "失业率", "城镇失业", "就业形势"],
        "unit": "%",
        "direction": "negative",
        "department": "国家统计局本市调查队",
        "report": "《劳动力调查月报》",
        "frequency": "月",
        "cumulative": False,
        "definition": "城镇常住人口中，失业人口占城镇就业人口与失业人口之和的比重，按劳动力调查制度推算。",
        "caveats": [
            "为抽样调查推算数，非登记失业数，与城镇登记失业率不是同一指标。",
            "该指标越低越好，与增速类指标方向相反。",
        ],
        "api": "GET /api/metrics/UR_RATE",
    },
    "SS_ENTERPRISE": {
        "code": "SS_ENTERPRISE",
        "name": "新增市场主体户数",
        "aliases": [
            "新增市场主体", "市场主体", "新登记企业", "新增企业", "注册企业", "营商",
        ],
        "unit": "户",
        "direction": "positive",
        "department": "市市场监督管理局",
        "report": "《市场主体登记月报》",
        "frequency": "月",
        "cumulative": False,
        "definition": "当月新登记的市场主体户数，含企业、个体工商户和农民专业合作社。",
        "caveats": [
            "含个体工商户，不能等同于新设企业数。",
            "受登记便利化政策和集中登记影响，单月波动不代表趋势。",
        ],
        "api": "GET /api/metrics/SS_ENTERPRISE",
    },
    "GDP_TOTAL": {
        "code": "GDP_TOTAL",
        "name": "地区生产总值",
        "aliases": ["地区生产总值", "生产总值", "GDP", "经济总量", "全市经济"],
        "unit": "亿元",
        "direction": "positive",
        "department": "市统计局",
        "report": "《本市地区生产总值核算月报》",
        "frequency": "季",
        "cumulative": True,
        "kind": "total",
        "definition": (
            "按季度核算的地区生产总值，年内累计数（1 月至当季末）。"
            "总量为现价，同比增速按不变价计算，两者不能相互推算。"
        ),
        "caveats": [
            "累计口径：年初从 0 起算，12 月为全年数，不做环比。",
            "总量为现价、增速为不变价，用总量相除算出的增速与公布增速不一致属正常。",
            "季度核算数据在后续季度可能被修订，以最新一期为准。",
        ],
        "api": "GET /api/metrics/GDP_TOTAL",
    },
    "BUDGET_REV": {
        "code": "BUDGET_REV",
        "name": "一般公共预算收入",
        "aliases": ["一般公共预算收入", "公共预算收入", "一般公共预算", "财政收入", "预算收入"],
        "unit": "亿元",
        "direction": "positive",
        "department": "市财政局",
        "report": "《财政收支月报》",
        "frequency": "月",
        "cumulative": True,
        "kind": "total",
        "definition": (
            "年内累计实现的一般公共预算收入，含税收收入和非税收入，"
            "为地方留成部分，不含上级转移支付和上解支出。"
        ),
        "caveats": [
            "为地方留成口径，不等于全市组织的财政收入总量。",
            "非税收入受一次性因素影响较大，单月波动不代表趋势。",
        ],
        "api": "GET /api/metrics/BUDGET_REV",
    },
    "FAI_GROWTH": {
        "code": "FAI_GROWTH",
        "name": "固定资产投资增速",
        "aliases": ["固定资产投资增速", "固定资产投资", "固投", "投资增速"],
        "unit": "%",
        "direction": "positive",
        "department": "市统计局",
        "report": "《固定资产投资统计月报》",
        "frequency": "月",
        "cumulative": True,
        "definition": (
            "年内累计固定资产投资额同比增速，按可比口径计算，"
            "不含农户投资，含房地产开发投资。"
        ),
        "caveats": [
            "累计口径：1 月至当月末累计同比，不做环比。",
            "统计起点为计划总投资 500 万元以上项目，不含 500 万元以下项目。",
        ],
        "api": "GET /api/metrics/FAI_GROWTH",
    },
    "INVEST_SIGNED": {
        "code": "INVEST_SIGNED",
        "name": "招商引资新签约亿元以上项目数",
        "aliases": ["招商引资", "新签约项目", "签约项目", "亿元以上项目", "招商项目", "招商"],
        "unit": "个",
        "direction": "positive",
        "department": "市投资促进局",
        "report": "《招商引资月报》",
        "frequency": "月",
        "cumulative": True,
        "kind": "total",
        "target": 121,
        "target_note": "年度目标 121 个",
        "definition": (
            "年内累计新签约的计划总投资 1 亿元以上产业项目个数，"
            "以正式签订投资协议为准，不含意向协议和框架协议。"
        ),
        "caveats": [
            "只计已签正式协议的项目，签约到落地开工仍有距离。",
            "项目数不等于投资额，单个项目体量差异大。",
        ],
        "api": "GET /api/metrics/INVEST_SIGNED",
    },
    "LIVELIHOOD_DONE": {
        "code": "LIVELIHOOD_DONE",
        "name": "民生实事完成项数",
        "aliases": ["民生实事", "民生保障", "民生项目", "为民办实事", "民生工程"],
        "unit": "项",
        "direction": "positive",
        "department": "市政府办公室（民生实事专班）",
        "report": "《民生实事项目进度月报》",
        "frequency": "月",
        "cumulative": True,
        "kind": "total",
        "target": 45,
        "target_note": "年度 45 项",
        "definition": (
            "市政府年度民生实事清单中，已完成建设并通过验收的项目项数。"
            "在建、未验收的不计入完成数。"
        ),
        "caveats": [
            "只计已通过验收的项目，临近完工但未验收的不算完成。",
            "项数不代表工作量，单个项目规模差异大。",
        ],
        "api": "GET /api/metrics/LIVELIHOOD_DONE",
    },
}

# --------------------------------------------------------------------------
# 数值：与 PERIODS 一一对应（2024-11 ~ 2026-10）
# --------------------------------------------------------------------------

_VALUES: dict[str, list[float]] = {
    "GY_VA_GROWTH": [
        6.2, 6.5, 5.8, 6.9, 6.1, 5.6, 6.3, 6.8, 6.4, 5.9, 6.7, 7.0,
        6.8, 7.1, 6.3, 7.4, 7.8, 8.2, 7.6, 8.5, 8.0, 7.2, 8.9, 8.6,
    ],
    "FAI_PROGRESS": [
        89.4, 97.2, 6.2, 12.8, 21.5, 30.2, 39.1, 47.8, 56.2, 64.1, 72.4, 79.8,
        88.5, 96.4, 6.8, 13.5, 22.4, 31.8, 40.6, 49.3, 57.1, 65.4, 73.9, 81.7,
    ],
    "TZ_12345_CLOSURE": [
        94.1, 94.6, 93.8, 92.9, 94.5, 95.1, 94.8, 95.6, 95.2, 94.4, 95.9, 95.3,
        95.2, 94.8, 93.9, 94.2, 95.8, 96.4, 95.1, 94.6, 96.9, 95.4, 96.2, 95.7,
    ],
    "AQI_GOOD_DAYS": [
        71.0, 58.5, 51.2, 60.4, 74.1, 84.6, 89.2, 77.5, 64.0, 71.8, 87.2, 88.6,
        68.0, 55.0, 48.5, 58.0, 72.5, 83.0, 88.5, 76.0, 62.5, 70.0, 86.5, 90.0,
    ],
    "UR_RATE": [
        5.3, 5.2, 5.4, 5.3, 5.2, 5.1, 5.2, 5.3, 5.2, 5.1, 5.2, 5.3,
        5.2, 5.1, 5.3, 5.2, 5.1, 5.0, 5.1, 5.2, 5.3, 5.2, 5.1, 5.2,
    ],
    "SS_ENTERPRISE": [
        4510, 4380, 3720, 4050, 4610, 4780, 4520, 4890, 4760, 4980, 5120, 4880,
        4820, 4610, 3980, 4320, 5105, 5240, 4980, 5310, 5180, 5420, 5610, 5230,
    ],
    "GDP_TOTAL": [
        1450.0, 1582.0, 140.2, 282.4, 418.6, 552.1, 685.3, 820.5,
        955.2, 1090.4, 1223.0, 1358.2, 1490.5, 1625.4, 156.3, 308.7,
        452.9, 595.4, 738.1, 882.6, 1024.8, 1148.3, 1286.5, 1428.7,
    ],
    "BUDGET_REV": [
        352.0, 384.5, 34.1, 68.4, 102.2, 136.0, 170.3, 204.1,
        238.2, 270.0, 300.5, 332.4, 364.1, 396.8, 35.2, 71.3,
        107.4, 143.2, 178.5, 213.6, 248.9, 281.5, 312.8, 345.6,
    ],
    "FAI_GROWTH": [
        4.8, 5.1, 5.2, 4.9, 5.4, 5.6, 5.3, 5.5, 5.8, 5.2, 5.1, 5.3,
        5.0, 5.4, 5.0, 5.5, 5.8, 6.0, 5.5, 5.9, 6.2, 5.8, 5.6, 5.9,
    ],
    "INVEST_SIGNED": [
        96, 108, 8, 16, 25, 33, 41, 50, 58, 66, 74, 82,
        90, 99, 7, 15, 24, 34, 43, 52, 61, 71, 86, 94,
    ],
    "LIVELIHOOD_DONE": [
        38, 42, 3, 6, 10, 14, 18, 22, 26, 30, 33, 36,
        39, 43, 2, 5, 9, 14, 18, 23, 27, 31, 38, 40,
    ],
}

# 分解数据：code -> dim -> period -> [{"name","value"}]
_BREAKDOWNS: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
    "FAI_PROGRESS": {
        "区县": {
            "2026-09": [
                {"name": "高新区", "value": 80.2},
                {"name": "临港区", "value": 76.8},
                {"name": "城东区", "value": 71.5},
                {"name": "城西区", "value": 68.4},
                {"name": "云山县", "value": 64.1},
            ],
        },
    },
    "SS_ENTERPRISE": {
        "区县": {
            "2026-09": [
                {"name": "高新区", "value": 1520},
                {"name": "临港区", "value": 1240},
                {"name": "城东区", "value": 1010},
                {"name": "城西区", "value": 960},
                {"name": "云山县", "value": 880},
            ],
        },
    },
    "TZ_12345_CLOSURE": {
        "责任单位": {
            "2026-09": [
                {"name": "市教育局", "value": 98.6},
                {"name": "市卫健委", "value": 97.9},
                {"name": "市交通局", "value": 96.4},
                {"name": "市城管局", "value": 94.1},
                {"name": "市住建局", "value": 91.7},
            ],
        },
    },
    "GY_VA_GROWTH": {
        "区县": {
            "2026-09": [
                {"name": "高新区", "value": 11.4},
                {"name": "临港区", "value": 9.6},
                {"name": "城东区", "value": 7.8},
                {"name": "城西区", "value": 6.9},
                {"name": "云山县", "value": 5.2},
            ],
        },
    },
}


# --------------------------------------------------------------------------
# 查询函数
# --------------------------------------------------------------------------


def _period_label(period: str) -> str:
    return f"{int(period[:4])}年{int(period[5:7])}月"


def _index(period: str) -> int:
    if period not in PERIODS:
        raise KeyError(f"period out of range: {period}")
    return PERIODS.index(period)


def _round(value: float, code: str) -> float:
    unit = METRICS[code]["unit"]
    if unit == "%":
        return round(value, 2)
    if unit == "亿元":
        return round(value, 1)
    return round(value, 0)


def resolve_period(period: str | None, today: date | None = None) -> dict[str, Any]:
    """把请求期次解析成"可得/未披露"两种状态，未披露时给出最近可得期。"""
    avail = available_periods(today)
    latest = avail[-1]
    if not period:
        return {"ok": True, "period": latest, "latest": latest, "pending": False}

    if period in PERIODS and period not in avail:
        return {
            "ok": False,
            "pending": True,
            "period": period,
            "latest": latest,
            "disclosed_on": _disclosed_on(period).isoformat(),
            "message": (
                f"{_period_label(period)}数据尚未披露，按披露规则将于 "
                f"{_disclosed_on(period).isoformat()} 发布；最近可得期为 {_period_label(latest)}。"
            ),
        }
    if period not in PERIODS:
        if period > PERIODS[-1]:
            return {
                "ok": False,
                "pending": True,
                "period": period,
                "latest": latest,
                "message": f"{_period_label(period)}还没到披露期，最近可得期为 {_period_label(latest)}。",
            }
        return {
            "ok": False,
            "pending": False,
            "period": period,
            "latest": latest,
            "message": f"接口只覆盖 {_period_label(PERIODS[0])} 至 {_period_label(PERIODS[-1])}，{period} 超出范围。",
        }
    return {"ok": True, "period": period, "latest": latest, "pending": False}


def source_of(code: str, period: str) -> dict[str, Any]:
    """出处元数据 —— 每个数值必须带着它一起返回。"""
    meta = METRICS[code]
    return {
        "department": meta["department"],
        "report": f"{meta['report']}{_period_label(period)}",
        "period": _period_label(period),
        "published_at": _disclosed_on(period).isoformat(),
        "definition": meta["definition"],
        "api": meta["api"],
        "retrieved_at": datetime.now().replace(microsecond=0).isoformat(),
        "mock": True,
    }


def list_metrics() -> list[dict[str, Any]]:
    latest = latest_available()
    out = []
    for code, meta in METRICS.items():
        out.append(
            {
                "code": code,
                "name": meta["name"],
                "unit": meta["unit"],
                "department": meta["department"],
                "frequency": meta["frequency"],
                "latest_period": _period_label(latest),
                "latest_value": _round(_VALUES[code][_index(latest)], code),
                "definition": meta["definition"],
            }
        )
    return out


def get_metric(code: str) -> dict[str, Any]:
    meta = METRICS[code]
    return {
        "code": code,
        "name": meta["name"],
        "unit": meta["unit"],
        "department": meta["department"],
        "report": meta["report"],
        "frequency": meta["frequency"],
        "definition": meta["definition"],
        "caveats": meta["caveats"],
        "dimensions": list(_BREAKDOWNS.get(code, {}).keys()),
        "api": meta["api"],
    }


def query_metric(
    code: str,
    period: str | None = None,
    months: int = 1,
    today: date | None = None,
) -> dict[str, Any]:
    """取单指标时序。months 为回溯月数（1 = 只要最新一期）。"""
    if code not in METRICS:
        return {"ok": False, "error": "unknown_metric", "code": code}

    resolved = resolve_period(period, today)
    if not resolved["ok"]:
        prior = query_metric(code, None, months, today)
        return {
            "ok": False,
            "error": "not_disclosed",
            "code": code,
            "requested_period": period,
            "message": resolved["message"],
            "latest": prior.get("latest"),
        }

    end = resolved["period"]
    end_idx = _index(end)
    start_idx = max(0, end_idx - max(1, months) + 1)
    points = [
        {
            "period": PERIODS[i],
            "label": _period_label(PERIODS[i]),
            "value": _round(_VALUES[code][i], code),
        }
        for i in range(start_idx, end_idx + 1)
    ]

    latest_value = _VALUES[code][end_idx]
    prev = _VALUES[code][end_idx - 1] if end_idx > 0 else None
    year_ago = _VALUES[code][end_idx - 12] if end_idx >= 12 else None

    def _delta(cur: float, base: float | None) -> float | None:
        if base is None:
            return None
        unit = METRICS[code]["unit"]
        if unit == "%":
            return round(cur - base, 2)
        if unit == "亿元":
            return round(cur - base, 1)
        return round(cur - base, 0)

    return {
        "ok": True,
        "code": code,
        "name": METRICS[code]["name"],
        "unit": METRICS[code]["unit"],
        "period": end,
        "period_label": _period_label(end),
        "value": _round(latest_value, code),
        "mom": None if METRICS[code]["cumulative"] else _delta(latest_value, prev),
        "mom_note": "累计口径不做环比" if METRICS[code]["cumulative"] else None,
        "yoy": _delta(latest_value, year_ago),
        # 同比增速（%）：总量类指标（亿元/户/项）用它；增速类指标本身已是百分比，看 yoy 的百分点差。
        "yoy_pct": None if not year_ago else round((latest_value / year_ago - 1) * 100, 2),
        "kind": METRICS[code].get("kind", "rate"),
        "target": METRICS[code].get("target"),
        "series": points,
        "source": source_of(code, end),
        "caveats": METRICS[code]["caveats"],
    }


def breakdown_metric(
    code: str,
    dim: str | None = None,
    period: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    if code not in METRICS:
        return {"ok": False, "error": "unknown_metric", "code": code}
    dims = _BREAKDOWNS.get(code, {})
    if not dims:
        return {
            "ok": False,
            "error": "no_breakdown",
            "code": code,
            "message": f"{METRICS[code]['name']}目前只提供全市汇总值，没有维度分解接口。",
        }
    if dim is None:
        return {"ok": False, "error": "need_dim", "code": code, "dimensions": list(dims.keys())}
    if dim not in dims:
        return {"ok": False, "error": "unknown_dim", "code": code, "dimensions": list(dims.keys())}

    resolved = resolve_period(period, today)
    target = resolved.get("period") if resolved.get("ok") else latest_available(today)
    bucket = dims.get(dim, {}).get(target)
    if bucket is None:
        return {
            "ok": False,
            "error": "no_data",
            "code": code,
            "period": target,
            "message": f"{_period_label(target)}的{dim}分解数据未提供。",
        }
    return {
        "ok": True,
        "code": code,
        "name": METRICS[code]["name"],
        "unit": METRICS[code]["unit"],
        "period": target,
        "period_label": _period_label(target),
        "dimension": dim,
        "rows": sorted(bucket, key=lambda r: r["value"], reverse=True),
        "source": source_of(code, target),
        "caveats": METRICS[code]["caveats"],
    }


def compare_metrics(codes: list[str], period: str | None = None, today: date | None = None) -> dict[str, Any]:
    rows = []
    for code in codes:
        result = query_metric(code, period, 1, today)
        if result.get("ok"):
            rows.append(
                {
                    "code": code,
                    "name": result["name"],
                    "value": result["value"],
                    "unit": result["unit"],
                    "yoy": result["yoy"],
                    "period_label": result["period_label"],
                    "source": result["source"],
                }
            )
        else:
            rows.append({"code": code, "error": result.get("error"), "message": result.get("message")})
    return {
        "ok": any("value" in r for r in rows),
        "period_label": rows[0].get("period_label") if rows else None,
        "rows": rows,
    }
