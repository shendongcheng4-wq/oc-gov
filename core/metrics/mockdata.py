"""城市指标问数原型 —— 模拟指标数据层。

这里是"假接口"，但模拟的是真接口该有的形状：每个数值都必须能回答三件事
——口径是什么、谁给的、什么时候披露的。少任何一件，这个数字就不该被写进材料。

真实接入时只替换 _VALUES / _BREAKDOWNS 的来源（改成调各部门接口），
下面的取值、口径、出处、时效判断逻辑都不用动。
"""

from __future__ import annotations

import copy
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


# 材料装配层要算「下一期什么时候披露」，公开出去而不是各写一份日历
disclosed_on = _disclosed_on


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
    # 下面两个是 2026-11 补的绝对额指标：真数表里有产值、用电，指标字典里却只有
    # 增速，于是「规上工业总产值多少」永远答「库里没有」，模型拿增加值增速顶上。
    # 它们没有演示序列 —— 只有 realdata 里那两期真数，问别的期次直接说没有。
    "GY_OUTPUT": {
        "code": "GY_OUTPUT",
        "name": "规模以上工业总产值",
        "aliases": ["规上工业总产值", "工业总产值", "规上产值", "工业产值", "总产值"],
        "unit": "亿元",
        "direction": "positive",
        "department": "市统计局",
        "report": "《规模以上工业生产月报》",
        "frequency": "月",
        "cumulative": True,
        "kind": "total",
        "real_only": True,
        "definition": (
            "规模以上工业企业（年主营业务收入 2000 万元及以上）当期完成的工业总产值，"
            "按现价计算，年内累计。"
        ),
        "caveats": [
            "现价口径，增速含价格因素，与按可比价计算的增加值增速不是一回事，不能相互印证。",
            "只含规上企业，不含规下企业与个体户。",
        ],
        "api": "GET /api/metrics/GY_OUTPUT",
    },
    "ELEC_GY": {
        "code": "ELEC_GY",
        "name": "工业用电量",
        "aliases": ["工业用电量", "工业用电", "用电量", "用电"],
        "unit": "亿千瓦时",
        "direction": "positive",
        "department": "市统计局",
        "report": "《能源统计月报》",
        "frequency": "月",
        "cumulative": True,
        "kind": "total",
        "real_only": True,
        "definition": "全社会用电量中第二产业（工业）部分，年内累计，实物量指标。",
        "caveats": [
            "实物量指标，可用于交叉验证产值增速：产值增、用电增得更慢，说明单位产值电耗在降。",
            "含企业自备电厂发电量，与供电公司售电量不是同一口径。",
        ],
        "api": "GET /api/metrics/ELEC_GY",
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
            "全市数为分镇街完成额加权汇总，不等于各镇街完成率的简单平均。"
        ),
        "caveats": [
            "累计口径：年初从 0 起算，年内逐月递增，不做环比。",
            "全市完成率为加权值，与各镇街完成率直接平均的结果不一致属正常。",
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
    # 绝对额口径。增速（上面那条）和总额是两张表、两个数，谁也不能替谁答：
    # 「固投下降 9.4%」和「固投 290.86 亿」要同时拿得到，才说得清「降了多少、还剩多少」。
    "FAI_TOTAL": {
        "code": "FAI_TOTAL",
        "name": "固定资产投资额",
        "aliases": [
            "固定资产投资额", "固定资产投资总额", "固投总额", "固投额",
            "投资总额", "固投总量",
        ],
        "unit": "亿元",
        "direction": "positive",
        "department": "市统计局",
        "report": "《固定资产投资统计月报》",
        "frequency": "月",
        "cumulative": True,
        "kind": "total",
        "real_only": True,
        "definition": (
            "年内累计完成的固定资产投资额，按可比口径计算，"
            "不含农户投资，含房地产开发投资，起点为计划总投资 500 万元以上项目。"
        ),
        "caveats": [
            "累计口径：1 月至当月末累计，不做环比。",
            "与市重大项目的「完成投资额」不是同一范围：后者只含列入清单的项目。",
        ],
        "api": "GET /api/metrics/FAI_TOTAL",
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

# 只有真数、没有演示序列的指标（产值、用电、固投额）。`_VALUES` 里没有它们，
# 取数一律走 realdata.REAL_PERIOD —— 编不出来的东西就不要编。
REAL_ONLY: frozenset[str] = frozenset(
    code for code, m in METRICS.items() if m.get("real_only")
)

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
#
# 镇街口径对齐溧阳市实际区划：9 镇 + 3 街道（市政府「行政区划」页）——
# 天目湖镇、埭头镇、上黄镇、戴埠镇、别桥镇、竹箦镇、上兴镇、南渡镇、社渚镇；
# 溧城街道、古县街道、昆仑街道。溧阳是县级市，没有「区/县」，所以维度叫「镇街」。
# 演示值可以编，单位名不该编（887b558 立的规矩）：这里写的是真镇名，数值是假的。
#
# 三个指标的行必须能对回全市值，否则同一屏里「全市 5610 户」和分镇街相加对不上：
#   - SS_ENTERPRISE 是户数，12 行相加恰好 = 5610；
#   - FAI_PROGRESS / GY_VA_GROWTH 是比率，全市数是按投资额 / 增加值加权，
#     不等于 12 行简单平均（见 METRICS 里的 caveats）。
_BREAKDOWNS: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
    "FAI_PROGRESS": {
        "镇街": {
            "2026-09": [
                {"name": "溧城街道", "value": 82.6},
                {"name": "昆仑街道", "value": 80.4},
                {"name": "古县街道", "value": 75.8},
                {"name": "戴埠镇", "value": 73.6},
                {"name": "天目湖镇", "value": 72.1},
                {"name": "上兴镇", "value": 70.3},
                {"name": "竹箦镇", "value": 68.5},
                {"name": "社渚镇", "value": 66.8},
                {"name": "南渡镇", "value": 65.2},
                {"name": "别桥镇", "value": 63.5},
                {"name": "埭头镇", "value": 61.8},
                {"name": "上黄镇", "value": 59.4},
            ],
        },
    },
    "SS_ENTERPRISE": {
        "镇街": {
            "2026-09": [
                {"name": "溧城街道", "value": 1120},
                {"name": "昆仑街道", "value": 820},
                {"name": "古县街道", "value": 540},
                {"name": "戴埠镇", "value": 500},
                {"name": "天目湖镇", "value": 480},
                {"name": "上兴镇", "value": 450},
                {"name": "竹箦镇", "value": 380},
                {"name": "南渡镇", "value": 340},
                {"name": "社渚镇", "value": 300},
                {"name": "别桥镇", "value": 250},
                {"name": "埭头镇", "value": 220},
                {"name": "上黄镇", "value": 210},
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
        "镇街": {
            "2026-09": [
                {"name": "昆仑街道", "value": 11.6},
                {"name": "上兴镇", "value": 10.8},
                {"name": "戴埠镇", "value": 9.4},
                {"name": "溧城街道", "value": 8.6},
                {"name": "竹箦镇", "value": 8.2},
                {"name": "古县街道", "value": 7.9},
                {"name": "天目湖镇", "value": 7.4},
                {"name": "社渚镇", "value": 7.0},
                {"name": "南渡镇", "value": 6.5},
                {"name": "别桥镇", "value": 6.0},
                {"name": "埭头镇", "value": 5.6},
                {"name": "上黄镇", "value": 5.1},
            ],
        },
    },
}

# 维度别名：模型（或旧调用方、旧会话）会说「区县」「板块」，口径统一叫「镇街」。
# 不做这层归位，`breakdown_metric` 会返回 unknown_dim，上层只能退回全市汇总 ——
# 而其实数据是有的，只是叫法不同。
_DIM_ALIAS = {
    "区县": "镇街",
    "县区": "镇街",
    "区镇": "镇街",
    "镇区": "镇街",
    "板块": "镇街",
    "各板块": "镇街",
    "各镇": "镇街",
    "各镇街": "镇街",
    "镇（街道）": "镇街",
    "镇街": "镇街",
    "部门": "责任单位",
    "单位": "责任单位",
}


def normalize_dim(dim: str | None) -> str | None:
    if dim is None:
        return None
    key = str(dim).strip()
    return _DIM_ALIAS.get(key, key)


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
        # 保留两位：统计表给到分的量级（1300.07、290.86），四舍五入到一位
        # 会变成 1300.1，对着材料核数时对不上账。
        return round(value, 2)
    if unit == "亿千瓦时":
        return round(value, 2)
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
        # code 跟着出处一起走：界面上「点来源看口径」要靠它定位到哪个指标，
        # 从 api 字符串反解一次能用但脆（接口路径一改就断），干脆在这里给全
        "code": code,
        "department": meta["department"],
        "report": f"{meta['report']}{_period_label(period)}",
        "period": _period_label(period),
        "published_at": _disclosed_on(period).isoformat(),
        "definition": meta["definition"],
        "api": meta["api"],
        "retrieved_at": datetime.now().replace(microsecond=0).isoformat(),
        "mock": True,
    }


def target_note(result: dict[str, Any]) -> str | None:
    """「离年度目标还差多少」的口径，只在这里算一次。

    工作台卡片和消息提醒都要说这句话，两边各写一份早晚会不一致 ——
    数字一样、措辞不同也是不一致。所以放在数据层，谁要谁调。
    """
    target = result.get("target")
    if target is None:
        return None
    value = result["value"]
    diff = round(value - target, 2)
    unit = result["unit"]
    if unit == "%":
        if diff > 0:
            return f"高于年度目标 {diff:g} pct"
        if diff < 0:
            return f"低于年度目标 {abs(diff):g} pct"
        return "与年度目标持平"
    if diff > 0:
        return f"超出年度目标 {diff:g}{unit}"
    if diff < 0:
        return f"距离年度目标还差 {abs(diff):g}{unit}"
    return "已完成年度目标"


def _realdata():
    """懒加载真数覆盖层。拿不到就当没有 —— 指标查询不该因为一个可选模块挂掉。"""
    try:
        import realdata  # noqa: PLC0415

        return realdata
    except Exception:  # noqa: BLE001
        return None


def period_real(code: str, period: str) -> dict[str, Any] | None:
    """某指标某一期是否已接入统计月报真数。

    给「附件表格 × 指标库逐期对照」用（core/metrics/extdata.py）。`query_metric`
    的 `series` 永远是演示序列，真数只覆盖 `value`（见那边的 `real_override` 注释），
    照 series 做对照会把真数期判成「不一致」—— 实测传一份 9 月月报进去，GDP 报
    「差 13.57 亿」、财政收入报「量级差十倍」，其实两边是同一张表。
    """
    rd = _realdata()
    return rd.real_period_value(code, period) if rd else None


def list_metrics() -> list[dict[str, Any]]:
    latest = latest_available()
    rd = _realdata()
    out = []
    for code, meta in METRICS.items():
        if code in REAL_ONLY:
            periods = rd.periods_of(code) if rd else []
            latest_period = _period_label(periods[-1]) if periods else None
            latest_value = (
                _round(rd.real_period_value(code, periods[-1])["value"], code)
                if periods
                else None
            )
        else:
            latest_period = _period_label(latest)
            latest_value = _round(_VALUES[code][_index(latest)], code)
        out.append(
            {
                "code": code,
                "name": meta["name"],
                "unit": meta["unit"],
                "department": meta["department"],
                "frequency": meta["frequency"],
                "latest_period": latest_period,
                "latest_value": latest_value,
                "definition": meta["definition"],
                # 只有真数、没有演示序列的指标：调用方据此知道「没有更早的期次」
                "real_only": code in REAL_ONLY,
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


def _real_only_query(code: str, period: str | None, today: date | None = None) -> dict[str, Any]:
    """只有真数的指标（产值、用电、固投额）：只答已接入的期次。

    这几条没有演示序列，问没接入的期次就直说没有 —— 回落 mock 会答出一个
    「10 月的数」，那比不答更糟。
    """
    meta = METRICS[code]
    rd = _realdata()
    periods = rd.periods_of(code) if rd else []
    if not periods:
        return {
            "ok": False, "error": "no_data", "code": code, "name": meta["name"],
            "message": f"{meta['name']}还没有接入统计部门的真数。",
        }
    if period is None:
        end = periods[-1]
    elif period in periods:
        end = period
    else:
        shown = "、".join(_period_label(p) for p in periods)
        want = _period_label(period) if period in PERIODS else str(period)
        return {
            "ok": False,
            "error": "no_data",
            "code": code,
            "name": meta["name"],
            "requested_period": period,
            "available_periods": periods,
            "latest": periods[-1],
            "message": (
                f"{meta['name']}目前只接入 {shown} 的真数，{want}还没有数据；"
                f"最近可得为 {_period_label(periods[-1])}。"
            ),
        }

    rec = rd.real_period_value(code, end) or {}
    points = [
        {
            "period": p,
            "label": _period_label(p),
            "value": _round(rd.real_period_value(code, p)["value"], code),
        }
        for p in periods
    ]
    return {
        "ok": True,
        "code": code,
        "name": meta["name"],
        "unit": meta["unit"],
        "period": end,
        "period_label": _period_label(end),
        "value": _round(rec["value"], code),
        "mom": None,
        "mom_note": "累计口径不做环比" if meta.get("cumulative") else None,
        # 真数表只给了同比，没给上年同期绝对量 —— 绝对增量算不出来，置空
        "yoy": None,
        "yoy_pct": rec.get("yoy"),
        "yoy_note": "统计局口径同比",
        "kind": meta.get("kind", "rate"),
        "target": meta.get("target"),
        "series": points,
        "source": rd.real_period_source(code, end),
        "caveats": meta["caveats"],
        "real_override": False,
        "demo": False,
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

    meta = METRICS[code]
    rd = _realdata()
    real_periods = rd.periods_of(code) if rd else []

    if code in REAL_ONLY:
        return _real_only_query(code, period, today)

    # 有真数的指标：不指定期次时按「最近一期真数」答。以前这里落到 PERIODS 的
    # 末位（2026-10），答出来的是演示序列 —— 材料上 94.9 亿、系统答 345.6 亿，
    # 就是这么来的。
    if period is None and real_periods:
        period = real_periods[-1]

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

    # 真数覆盖之后的期次（例如 10 月）不再回落演示序列：宁可说「没接入」，
    # 也不答一个编出来的当期数。
    if period is not None and real_periods and end > real_periods[-1]:
        return {
            "ok": False,
            "error": "no_data",
            "code": code,
            "name": meta["name"],
            "requested_period": end,
            "available_periods": real_periods,
            "latest": real_periods[-1],
            "message": (
                f"{meta['name']}的 {_period_label(end)}真数尚未接入，"
                f"最近可得为 {_period_label(real_periods[-1])}。"
            ),
        }

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
            return round(cur - base, 2)
        if unit == "亿千瓦时":
            return round(cur - base, 2)
        return round(cur - base, 0)

    result = {
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
        # 没被真数覆盖的一律标演示：界面上可以据此打「演示值」，模型据此别把它
        # 说成统计局的数。
        "demo": True,
    }

    # 真实值覆盖：先逐期（统计月报有的期次），再年度（公报数）。
    #
    # `real_override` 必须带出去：覆盖之后 value 是真数，series 仍是演示序列，
    # 两者不同口径。调用方（指标抽屉、答案气泡）拿到这个标记才知道「这条曲线
    # 不能挨着这个数画」，否则会被读成「年内掉了 300 亿」。
    real = rd.real_period_value(code, end) if rd else None
    if real is not None:
        result["value"] = _round(real["value"], code)
        # yoy 是「绝对增量」，真数表只给了同比、没给上年同期绝对量，算不出来；
        # 增速类指标（%）的 value 本身就是增速，再写一遍会变成「同比上升 -9.4%」。
        result["yoy"] = None
        result["yoy_pct"] = None if meta["unit"] == "%" else real.get("yoy")
        result["yoy_note"] = "统计局口径同比"
        result["mom"] = None
        result["mom_note"] = "累计口径不做环比"
        src = rd.real_period_source(code, end)
        if src:
            result["source"] = src
        result["real_override"] = True
        result["demo"] = False
        return result

    annual = rd.real_annual_value(code) if rd else None
    if annual is not None and end == f"{annual['year']}-12":
        result["value"] = _round(annual["value"], code)
        result["period_label"] = f"{annual['year']}年（全年）"
        # yoy 是「绝对增量」（亿元/户/项），公报只给了可比价增速，没给上年同期
        # 绝对量，算不出来 —— 只能置空，增速放 yoy_pct。
        # 曾经把百分比直接写进 yoy，问数按绝对增量读，GDP 就被写成
        # 「同比增加 5 亿元」。也不能拿总量和增速反推上年数：
        # 总量是现价、增速是可比价，两者不能相互推算（见指标的 caveats）。
        result["yoy"] = None
        result["yoy_pct"] = annual["yoy"]
        result["yoy_note"] = "可比价增速"
        rs = rd.real_source(code)
        if rs:
            result["source"] = rs
        # 值来自公报、序列仍是演示：这条标记给展示层判定用，不参与任何计算。
        result["real_override"] = True
        result["demo"] = False
    return result


def breakdown_metric(
    code: str,
    dim: str | None = None,
    period: str | None = None,
    today: date | None = None,
    backfill: int = 0,
) -> dict[str, Any]:
    if code not in METRICS:
        return {"ok": False, "error": "unknown_metric", "code": code}
    dim = normalize_dim(dim)
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
    used = target
    # 目标期没有分解、而历史上有（现实中很常见：分县数据要迟一个月才汇总上来），
    # 就沿时间轴往回找最近一期有的。找到也要把实际期次带回去 —— 静默换成别的
    # 期次比没有更糟，上层必须能说清「分解只到 9 月」。
    if bucket is None and backfill > 0 and target in PERIODS:
        start = PERIODS.index(target) - 1
        for i in range(start, max(-1, start - backfill), -1):
            if PERIODS[i] in dims.get(dim, {}):
                used = PERIODS[i]
                bucket = dims[dim][used]
                break
    if bucket is None:
        # 没给「最近的分解停在哪一期」，上层就只能说「没有数据」—— 而事实往往是
        # 「有，但停在三个月前」，或者「你问的那期没有、最新一期其实是有的」。
        # 这两个说法对读者的意义完全不同，所以把最新有的那一期一起带回去。
        have = [p for p in PERIODS if p in dims.get(dim, {})]
        return {
            "ok": False,
            "error": "no_data",
            "code": code,
            "period": target,
            "latest_period": have[-1] if have else None,
            "message": f"{_period_label(target)}的{dim}分解数据未提供。",
        }
    return {
        "ok": True,
        "code": code,
        "name": METRICS[code]["name"],
        "unit": METRICS[code]["unit"],
        "period": used,
        "period_label": _period_label(used),
        # 要的是哪一期、实际给的是哪一期：两个都在，前端和 trace 才说得清差异
        "requested_period": target,
        "backfilled": used != target,
        "dimension": dim,
        "rows": sorted(bucket, key=lambda r: r["value"], reverse=True),
        "source": source_of(code, used),
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
                    # 并排比的时候也要能看出哪一行是演示值
                    "demo": bool(result.get("demo")),
                }
            )
        else:
            rows.append({"code": code, "error": result.get("error"), "message": result.get("message")})
    labels = [r.get("period_label") for r in rows if r.get("period_label")]
    # 期次可能对不上：GDP 取到的是公报年度值（2025 全年），另一个是当月值。
    # 这时不能把其中任意一个当「本期」，只能把差异交给调用方说出来。
    mixed = len(set(labels)) > 1
    return {
        "ok": any("value" in r for r in rows),
        "period_label": None if mixed else (labels[0] if labels else None),
        "periods": labels,
        "mixed_period": mixed,
        "rows": rows,
    }
# --------------------------------------------------------------------------
# 民情焦点（多渠诉求聚类 + 可核查的来信原文）
#
# 罗盘卡片只显示 title / meta / tag；点开焦点弹窗要的是下面这套明细：
# 诉求详情、来信选登原文、关联工单、渠道分布、反馈人数、焦点词、14 日走势。
#
# 三条焦点的**主题与诉求原文**取自溧阳市人民政府门户「政民互动 · 来信选登」
# 的公开信件，字段 texts 是信件原文/答复原文的摘引，letters 里的编号就是
# 信件详情页的 id，点开能查到同一句话：
#   - 18842《东方花园学区划分》2025-12-11 留言 / 12-15 答复
#   - 18818《戴埠镇村燃气通气时间咨询》2025-11-15 留言 / 11-18 答复
#   - 18786《免费人才公寓的定义》2025-10-09 留言 / 10-10 答复
#   - 18902《青年人才购房补贴咨询》2026-02-02 留言 / 当日答复
#   入口：https://www.liyang.gov.cn/class/KPAAQDDK（栏目「来信选登」）
#   规模参照《2026年10月信件办理情况》：本站点当月收信 25 件、办结 25 件、
#   公开 1 件（https://www.liyang.gov.cn/html/czly/2026/AMKPAQEN_1101/242222.html）
#
# 演示数据：焦点**件数/人数/走势**是按演示需要编排的，来信原文与答复口径是
# 真实的；不含任何真实个人信息（来信人姓名已不出现）。真实接入时换掉
# FOCUS_TOPICS 的来源（改成 12345 平台聚类结果），字段形状和聚合逻辑都不用动。
#
# 口径提醒：channels 各项相加 = 诉求**件数**；people 是**按联系人去重**后
# 的人数。两者不是一个口径（一人多次来电只计 1 人、一件工单/一封来信可能
# 代表多人），所以 F3 会出现「1 件 / 37 人」这种看着不搭的组合 —— 那是对的。
# --------------------------------------------------------------------------

_LETTER_URL = "https://www.liyang.gov.cn/index.php?m=letter&c=index&a=show&id="

FOCUS_TOPICS: list[dict[str, Any]] = [
    {
        "id": "F1",
        "title": "城区初中学区归属咨询集中",
        "meta": "受理 128 件 · 环比 ↑42%",
        "tag": "集中",
        "tone": "warn",
        "type": "教育 / 招生入学",
        "state": "集中答复中",
        "state_tone": "warn",
        "first_seen": "2026-11-16",
        "detail": (
            "东方花园、燕山美林等小区业主反复来电：孩子上哪个初中、施教区明年会不会调。"
            "业主援引市教育局对《东方花园学区划分》的答复原文——“根据 2025 年初中施教区"
            "划分情况，东方花园初中施教区为溧阳市实验初级中学”，希望明确新一年度是否延续、"
            "二手房落户是否影响入学排序。市教育局现行口径为“以当年公布的施教区划分为准”，"
            "并留咨询电话 87220727；业主诉求是在新一轮划分方案公布前给出预告，"
            "避免下手买房时踩空。"
        ),
        "tickets": [
            ["TZ20261116032", "东方花园初中施教区归属", "已转办"],
            ["TZ20261118088", "二手房落户是否影响入学排序", "办理中"],
            ["TZ20261120015", "施教区调整何时公布", "已办结"],
        ],
        "channels": [
            ["12345 热线", 98],
            ["市长信箱", 14],
            ["「我的溧阳」APP", 11],
            ["网格员上报", 5],
        ],
        "source": "12345 热线 · 市教育局",
        "letters": [
            ["18842", "东方花园学区划分", "已答复",
             _LETTER_URL + "18842"],
        ],
        "letters_note": "2025-12-11 留言、12-15 答复，答复单位：溧阳市人民政府；答复时限 4 天。",
        "people": 96,
        "people_delta": "较昨日 ↑12 人",
        "keywords": [["施教区", 78], ["学区", 61], ["实验初中", 43], ["落户", 23]],
        "trend": [3, 4, 3, 5, 6, 4, 7, 9, 8, 11, 13, 12, 15, 18],
    },
    {
        "id": "F2",
        "title": "农村燃气通气进度入冬前反弹",
        "meta": "涉及 5 个镇 · 连续 2 周",
        "tag": "反弹",
        "tone": "info",
        "type": "公用事业 / 燃气",
        "state": "专项核查中",
        "state_tone": "info",
        "first_seen": "2026-11-12",
        "detail": (
            "戴埠镇高桥村委村民来信：村里 8—9 月预埋的燃气管道已完工，想知道什么时候通气。"
            "主管部门答复原文——“目前正在进行整改与打压，预计下月通气”，该件答复满意度为"
            "“非常满意”。同类来电集中在天目湖、社渚等镇，问的都是同一件事：管道埋好了、"
            "路面回填了，气什么时候进户。入冬前两周集中出现，属于每年采暖季前的规律性反弹。"
        ),
        "tickets": [
            ["TZ20261112061", "戴埠镇高桥村燃气通气时间", "已交办"],
            ["TZ20261114009", "管道已埋未通气", "核查中"],
        ],
        "channels": [
            ["12345 热线", 41],
            ["网格员上报", 15],
            ["「我的溧阳」APP", 7],
        ],
        "source": "12345 热线 · 市住建局",
        "letters": [
            ["18818", "戴埠镇村燃气通气时间咨询", "已答复",
             _LETTER_URL + "18818"],
        ],
        "letters_note": "2025-11-15 留言、11-18 答复，答复单位：溧阳市人民政府；满意度“非常满意”。",
        "people": 54,
        "people_delta": "较昨日 ↑6 人",
        "keywords": [["通气", 38], ["燃气管道", 26], ["打压", 12], ["入户", 9]],
        "trend": [4, 3, 5, 4, 6, 5, 4, 6, 7, 6, 8, 9, 8, 10],
    },
    {
        "id": "F3",
        "title": "某企业 37 人租房补贴不予受理",
        "meta": "11月10日受理 · 已转人社局核查",
        "tag": "转办",
        "tone": "muted",
        "type": "人才服务 / 补贴",
        "state": "转办核查",
        "state_tone": "muted",
        "first_seen": "2026-11-10",
        "detail": (
            "某企业职工来信：申报英才榜租房补贴被拒，理由为“单位已提供免费人才公寓的"
            "不再享受该政策”。来信称单位提供的是集体宿舍、且每月收取房租水电（已提供扣费"
            "明细），“一没有享受到人才公寓、二还是收费的集体宿舍”，不应被拒。"
            "答复原文——“一是单位已提供宿舍予以居住；二是通过了解，企业收费内容为宿舍水电费"
            "以及宿舍管理相关成本支出费用，不含房租”。该企业同批 37 名职工情况相同，"
            "由 1 人代表来信，故件数与人数不同口径。另有研究生同日询问自考学历能否享受购房补贴券。"
        ),
        "tickets": [["TZ20261110147", "某企业 37 人租房补贴不予受理", "已转人社局核查"]],
        "channels": [["市长信箱", 1]],
        "source": "市长信箱 · 来信选登 18786、18902",
        "letters": [
            ["18786", "免费人才公寓的定义", "已答复",
             _LETTER_URL + "18786"],
            ["18902", "青年人才购房补贴咨询", "已答复",
             _LETTER_URL + "18902"],
        ],
        "letters_note": "18786：2025-10-09 留言、10-10 答复；18902：2026-02-02 留言、当日答复。",
        "people": 37,
        "people_delta": "较昨日 —（无新增反映人）",
        "keywords": [["租房补贴", 37], ["人才公寓", 31], ["集体宿舍", 14]],
        "trend": [1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0],
    },
]


def focus_items() -> list[dict[str, Any]]:
    """焦点明细。返回深拷贝 —— 调用方改字段不会污染模块级常量。"""
    return copy.deepcopy(FOCUS_TOPICS)
