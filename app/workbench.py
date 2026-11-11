"""工作台数据聚合层。

把两个原型的数据缝成一个页面需要的形状：

- metrics-qa/mockdata.py   —— 指标数值与口径（城市罗盘的每张卡片）
- oversight/oversight.py   —— 督办台账（追踪推进的每条预警）

这里只做"取数 + 拼装 + 判定"，不产生任何新数字。所有数值都能顺着
`source` 字段回到某个指标接口的某一次返回，页面上点开即可核对。
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# core/ 下的模块直接加进 sys.path：指标层与督办层各自内部互相 import，保持平铺改名成本最低。
# core/ 本身也要在路径里 —— store / modelcfg / chat 是平铺模块，不是包。
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "metrics"))
sys.path.insert(0, str(ROOT / "core" / "oversight"))

import mockdata as md  # noqa: E402
import oversight as ov  # noqa: E402
import people  # noqa: E402
import saved as saved_mod  # noqa: E402

CITY = "溧阳市"


# --------------------------------------------------------------------------
# 城市罗盘 · 四个总览指标
# --------------------------------------------------------------------------

KPI_CODES = ["GDP_TOTAL", "GY_VA_GROWTH", "BUDGET_REV", "FAI_GROWTH"]

# 「我的关注」不再是这里的一个写死列表 —— 它按人存在 core/people.py 的
# DEFAULT_WATCH（各身份默认值）和 data/workspaces.json（各人改过的值）里。
# 原来那个 WATCH_CODES 常量已删除：留着会变成第二个真相，改一边忘一边。


def _fmt_yoy(result: dict[str, Any]) -> tuple[str, str, str]:
    """返回 (展示文本, 方向, 说明)。方向用于上色：涨为红、跌为绿（国内习惯）。

    分岔看**单位**而不是 `kind`：比率类（%）的同比是百分点差，其余（亿元/户/项）
    是增长率。以前看 `kind`，而 `kind` 只有几个指标显式写了 "total"，`户` 这类
    就会落进「百分点差」分支，写成「同比 +490 pct」—— 490 户变成 490 个百分点。
    这条规则和 ask._yoy_text、briefdoc._yoy_text 是同一套。
    """
    if (result.get("unit") or "") != "%":
        pct = result.get("yoy_pct")
        if pct is None:
            return "同比 —", "flat", ""
        arrow = "↑" if pct >= 0 else "↓"
        tone = "up" if pct > 0 else ("down" if pct < 0 else "flat")
        return f"同比 {arrow} {abs(pct):.1f}%", tone, "总量为现价，增速按可比价计算"
    delta = result.get("yoy")
    if delta is None:
        return "同比 —", "flat", ""
    sign = "+" if delta >= 0 else ""
    tone = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return f"同比 {sign}{delta} pct", tone, "较上年同期的百分点变化"


def _target_note(result: dict[str, Any]) -> str | None:
    """口径实现在 `mockdata.target_note()` —— 消息提醒也要说同一句话，不能各写一份。"""
    return md.target_note(result)


def kpis(codes: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    latest = md.latest_available()
    # 注意用 is None 判：传空列表是"这个人一个都不关注"，
    # 写成 `codes or KPI_CODES` 会把空列表当成没传，清空后又冒出四张罗盘卡。
    for code in (KPI_CODES if codes is None else codes):
        r = md.query_metric(code, None, 7)
        yoy_text, yoy_tone, yoy_why = _fmt_yoy(r)
        note = _target_note(r)
        metric = md.METRICS[code]
        # 当月口径的指标要标出来，否则会被当成累计数看
        if note and not metric["cumulative"]:
            note = f"当月 · {note}"
        out.append(
            {
                "code": code,
                "name": r["name"],
                "value": r["value"],
                "unit": r["unit"],
                "display": f"{r['value']:,}" if r["unit"] == "亿元" else f"{r['value']:g}",
                "yoy_text": yoy_text,
                "yoy_tone": yoy_tone,
                "yoy_why": yoy_why,
                "note": note,
                "note_why": metric.get("target_note"),
                "period_label": r["period_label"],
                # 页头写的是「2026年1—10月」，但 GDP 卡的数是 2025 年全年公报值。
                # 卡片必须自己交代期次，否则唯一一条真实值会被当成当期数读。
                "period_is_current": r["period"] == latest,
                "real_override": bool(r.get("real_override")),
                "series": [p["value"] for p in r["series"]],
                "source": {
                    "department": r["source"]["department"],
                    "report": r["source"]["report"],
                    "period": r["period_label"],
                    "api": metric["api"],
                },
                "caveats": r["caveats"],
            }
        )
    return out


# --------------------------------------------------------------------------
# 城市罗盘 · 五大板块 + 第三条推送
# 每张板块卡的核心数字都来自某个已注册指标，判定规则同样可解释。
# --------------------------------------------------------------------------

def _board(
    key: str,
    title: str,
    code: str,
    value_display: str,
    sub: str,
    progress: float,
    status: str,
    status_tone: str,
    status_why: str,
    trend: list[float],
) -> dict[str, Any]:
    m = md.METRICS[code]
    r = md.query_metric(code, None, 6)
    return {
        "key": key,
        "title": title,
        "code": code,
        "metric_name": m["name"],
        "value_display": value_display,
        "sub": sub,
        "progress": max(0.0, min(100.0, round(progress, 1))),
        "status": status,
        "status_tone": status_tone,
        "status_why": status_why,
        "trend": [p["value"] for p in r["series"]],
        "trend_labels": [p["label"] for p in r["series"]],
        "source": {
            "department": r["source"]["department"],
            "report": r["source"]["report"],
            "period": r["period_label"],
            "api": m["api"],
        },
        "caveats": r["caveats"],
    }


def boards() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # 1) 经济运行 —— 规上工业增加值增速 对比 年度目标
    gy = md.query_metric("GY_VA_GROWTH", None, 6)
    target = gy["target"]
    over = round(gy["value"] - target, 2)
    out.append(
        _board(
            "economy", "经济运行", "GY_VA_GROWTH",
            f"{gy['value']:g}%",
            f"规上工业增加值增速 · {'高于' if over >= 0 else '低于'}年度目标 {abs(over):g} pct",
            progress=gy["value"] / target * 100,
            status="正常" if over >= 0 else "预警",
            status_tone="ok" if over >= 0 else "warn",
            status_why=f"{gy['period_label']}当月增速 {gy['value']:g}%，年度目标 {target:g}%",
            trend=[],
        )
    )

    # 2) 招商引资 —— 新签约项目数 对比 年度目标
    inv = md.query_metric("INVEST_SIGNED", None, 6)
    tgt = inv["target"]
    rate = inv["value"] / tgt * 100
    out.append(
        _board(
            "invest", "招商引资", "INVEST_SIGNED",
            f"{inv['value']:g} 个",
            f"新签约亿元以上项目 · 完成年度目标 {rate:.0f}%",
            progress=rate,
            status="正常" if rate >= 70 else "关注",
            status_tone="ok" if rate >= 70 else "warn",
            status_why=f"年度目标 {tgt} 个，1—9 月已完成 {inv['value']} 个（{rate:.1f}%），时序进度 75%",
            trend=[],
        )
    )

    # 3) 重大项目 —— 投资完成率 对比 时序进度
    fai = md.query_metric("FAI_PROGRESS", None, 6)
    elapsed = 9 / 12 * 100  # 1—9 月时序进度
    gap = round(fai["value"] - elapsed, 1)
    out.append(
        _board(
            "project", "重大项目", "FAI_PROGRESS",
            f"{fai['value']:g}%",
            f"年度投资完成率 · 时序进度 {elapsed:.0f}%",
            progress=fai["value"],
            status="预警" if gap < 0 else "正常",
            status_tone="warn" if gap < 0 else "ok",
            status_why=(
                f"累计完成率 {fai['value']:g}%，低于 1—9 月时序进度 {abs(gap):g} pct，"
                "需关注滞后项目"
            ),
            trend=[],
        )
    )

    # 4) 社会治理 —— 12345 按期办结率
    tz = md.query_metric("TZ_12345_CLOSURE", None, 6)
    mom = tz.get("mom")
    mom_txt = f"环比 {'+' if (mom or 0) >= 0 else ''}{mom} pct" if mom is not None else "累计口径"
    out.append(
        _board(
            "society", "社会治理", "TZ_12345_CLOSURE",
            f"{tz['value']:g}%",
            f"12345 诉求按期办结率 · {mom_txt}",
            progress=tz["value"],
            status="正常" if tz["value"] >= 95 else "关注",
            status_tone="ok" if tz["value"] >= 95 else "warn",
            status_why=f"按期办结率 {tz['value']:g}%，95% 为市级考核基准线",
            trend=[],
        )
    )

    # 5) 民生保障 —— 民生实事完成项数 / 年度总数
    lv = md.query_metric("LIVELIHOOD_DONE", None, 6)
    total = lv["target"]
    done_rate = lv["value"] / total * 100
    out.append(
        _board(
            "livelihood", "民生保障", "LIVELIHOOD_DONE",
            f"{lv['value']:g} / {total} 项",
            f"民生实事已完成 · 完成率 {done_rate:.0f}%",
            progress=done_rate,
            status="正常" if done_rate >= 80 else "关注",
            status_tone="ok" if done_rate >= 80 else "warn",
            status_why=f"年度 {total} 项，已验收 {lv['value']} 项，时序进度 75%",
            trend=[],
        )
    )

    # 趋势数据由 _board 内部统一取近 6 期，这里不需要再回填
    return out


# --------------------------------------------------------------------------
# 民情焦点（今日推送）
# 真实系统里来自 12345 / 信访 / 舆情的热点聚类，这里给固定条目，但每条都带
# 可核查的来源与工单号，避免"看起来像真的但查不到"。
# --------------------------------------------------------------------------

def focus() -> dict[str, Any]:
    """民情焦点。数据源在指标层（core/metrics/mockdata.py 的 FOCUS_TOPICS），
    罗盘卡片和焦点弹窗用的是同一份，页面上的标题、标签、详情不会两处对不上。

    这里只取不造：接真实的 12345 工单聚类时，改 mockdata 一处即可。"""
    items = md.focus_items()
    return {"count": len(items), "items": items}


# --------------------------------------------------------------------------
# 追踪推进 · 预警提醒（来自督办台账，全部是派生态）
# --------------------------------------------------------------------------

_BUCKET_TONE = {
    "逾期": "danger",
    "今日到期": "warn",
    "临期": "warn",
    "签收超时": "info",
    "停滞": "info",
}


def _alert_text(item: dict[str, Any]) -> str:
    risk = item["risk"]
    if risk == "逾期":
        return f"已逾期 {abs(item['days_left'])} 天"
    if risk == "今日到期":
        return "今日到期"
    if risk == "临期":
        return f"剩 {item['days_left']} 天"
    if risk == "签收超时":
        return "交办未签收"
    if risk == "停滞":
        return f"{item['idle_days']} 天无进展"
    return


def alerts(limit: int = 3) -> list[dict[str, Any]]:
    sc = ov.scan(5)
    out: list[dict[str, Any]] = []
    order = ["逾期", "今日到期", "临期", "签收超时", "停滞"]
    for bucket in order:
        for item in sc["buckets"].get(bucket, []):
            out.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "assignee": item["assignee"],
                    "risk": item["risk"],
                    "tag": _alert_text(item),
                    "tone": _BUCKET_TONE.get(bucket, "muted"),
                    "status": item["status"],
                    "deadline": item["deadline"],
                    "remind_count": item["remind_count"],
                    "why": "；".join(item["risk_reasons"]),
                }
            )
            if len(out) >= limit:
                return out
    return out


def oversight_summary() -> dict[str, Any]:
    st = ov.stats()
    return {
        "total": st["total"],
        "open": st["open"],
        "closed": st["closed"],
        "overdue": st["overdue"],
        "due_soon": st["due_soon"],
        "stalled": st["stalled"],
        "on_time_rate": st["on_time_rate"],
    }


# --------------------------------------------------------------------------
# 页面总览
# --------------------------------------------------------------------------

def overview() -> dict[str, Any]:
    latest = md.latest_available()
    kpi_list = kpis()
    year, month = int(latest[:4]), int(latest[5:7])
    span = f"{year}年1—{month}月" if month > 1 else f"{year}年1月"
    return {
        "city": CITY,
        "title": f"{CITY}AI市长工作台",
        "period": span,
        "period_label": "当期数据",
        "disclosed_on": md.source_of("GDP_TOTAL", latest)["published_at"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kpis": kpi_list,
        "boards": boards(),
        "focus": focus(),
        "alerts": alerts(3),
        "oversight": oversight_summary(),
        "pending_periods": md.pending_periods(),
        "metric_count": len(md.METRICS),
        "mock": True,
    }


# --------------------------------------------------------------------------
# 个人工作台 · 我的关注 + 关注动态
# 卡片数值走 kpis(WATCH_CODES)，与罗盘同一条取数路径；
# 动态只做归集排序，不新增判断 —— 每条都能追到某个接口或某张台账。
# --------------------------------------------------------------------------

_FEED_MAX = 6
_OVERSIGHT_MAX = 5
# 「重点注意事项」不设条数上限，只在前端滚动；这里给个保险上限，防止台账涨到几百条时
# 一次吐出满屏 JSON。排序按紧急度，所以被截掉的永远是最后面那些不着急的。
_FOLLOW_MAX = 30

# 动态右侧的分类胶囊。按**来源**分而不是按颜色分：warn/danger 这个颜色既可能是
# 指标落后，也可能是时限临近，只看颜色不知道"这条为什么冒出来"。
_LABEL_METRIC = "关注指标"
_LABEL_DUE = "临近时限"
_LABEL_OV = "督办动态"
_LABEL_PERIOD = "待披露"
_DUE_BUCKETS = ("逾期", "今日到期", "临期")


def _feed_key(kind: str, ident: str, text: str) -> str:
    """已读标记用的 key，带内容指纹。

    只用 id 当 key 会出问题：同一条指标明天数值变了、同一件事催得更紧了，文字已经
    不同，却因为 id 没变而仍显示"已读"。带上正文指纹，内容一变就重新算未读。
    """
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"{kind}:{ident}:{digest}"


def _watch_feed(
    cards: list[dict[str, Any]],
    seen: list[str] | None = None,
    units: list[str] | None = None,
) -> list[dict[str, Any]]:
    known = set(seen or [])
    # 分管单位非空时，督办类动态只收自己分管的 —— 否则局长的「我的动态」里会冒出
    # 别的部门逾期，和下面「我跟进的」自相矛盾。指标类动态跟着关注列表走，不用再筛。
    scope = {u for u in (units or []) if u}
    feed: list[dict[str, Any]] = []

    def add(
        kind: str,
        ident: str,
        text: str,
        label: str,
        tone: str,
        source: str,
        code: str | None = None,
        ov_id: str | None = None,
    ) -> None:
        key = _feed_key(kind, ident, text)
        feed.append(
            {
                "kind": kind,
                "key": key,
                "text": text,
                "label": label,
                "tag": label,  # 老字段名保留：预警条和测试都在用 tag
                "tone": tone,
                "source": source,
                "code": code,
                "ov_id": ov_id,
                "unread": key not in known,
            }
        )

    # 1) 指标层面：低于年度目标的最该被看见
    for c in cards:
        note = c.get("note") or ""
        if "低于" in note or "还差" in note:
            add(
                "metric",
                c["code"],
                f"{c['name']}{note}",
                _LABEL_METRIC,
                "warn",
                c["source"]["department"],
                code=c["code"],
            )

    # 2) 督办层面：与底部预警条同一套派生规则，只是这里按紧急度全量收
    sc = ov.scan(5)
    picked: set[str] = set()
    ov_count = 0
    for bucket in ("逾期", "今日到期", "临期", "签收超时", "停滞"):
        for item in sc["buckets"].get(bucket, []):
            if item["id"] in picked:  # 同一件事可能同时命中「逾期」和「停滞」
                continue
            if scope and item["assignee"] not in scope:
                continue
            picked.add(item["id"])
            why = _alert_text(item)
            add(
                "oversight",
                item["id"],
                f"{item['title']} · {why}" if why else item["title"],
                _LABEL_DUE if bucket in _DUE_BUCKETS else _LABEL_OV,
                _BUCKET_TONE.get(bucket, "muted"),
                item["assignee"],
                ov_id=item["id"],
            )
            ov_count += 1
            if ov_count >= _OVERSIGHT_MAX:
                break
        if ov_count >= _OVERSIGHT_MAX:
            break

    # 3) 期次层面：哪一期还没披露，避免把旧数当最新
    for p in md.pending_periods()[:2]:
        add(
            "period",
            p,
            f"{p} 期数据尚未披露，当前展示的是 {md.latest_available()} 期",
            _LABEL_PERIOD,
            "info",
            "指标接口",
        )

    if not feed:
        add(
            "none",
            "none",
            "所关注指标均在年度目标内，暂无逾期事项",
            "正常",
            "ok",
            "系统",
        )
    return feed[:_FEED_MAX]


_RISK_ORDER = {"逾期": 0, "今日到期": 1, "临期": 2, "签收超时": 3, "停滞": 4}


def follow_items(person: dict[str, Any]) -> list[dict[str, Any]]:
    """「我跟进的」：分管单位派生的 + 本人手动挑的。

    为什么派生优先、而不是另建一张"谁跟进"的映射表：台账里没有这个字段，凭空加一个
    就要有人维护，维护不动就会变成假数据。分管单位是身份自带的（见 core/people.py），
    台账里每条都有 assignee，两边对得上就是真关系，对不上就不出现——不会编。

    为什么还要手动挑：分管范围是**组织关系**，不是**注意力**。市长可能就盯着某一个
    开发区的路网工程，那不属于他分管的任何一个单位，但确实是他要跟的。所以两条路都留，
    `source` 标出这一条是怎么来的，界面上手动项带个小标记，免得看起来像系统派的。
    """
    units = [u for u in (person.get("units") or []) if u]
    unit_set = set(units)
    manual = {i for i in (person.get("follow") or []) if i}
    rows: list[dict[str, Any]] = []
    for it in ov.list_items():
        if it["status"] == ov.CLOSED:
            continue
        in_unit = (not unit_set) or it["assignee"] in unit_set or bool(unit_set & set(it["co_assignees"]))
        in_manual = it["id"] in manual
        if not (in_unit or in_manual):
            continue
        rows.append(
            {
                "id": it["id"],
                "title": it["title"],
                "assignee": it["assignee"],
                "co_assignees": it["co_assignees"],
                "status": it["status"],
                "risk": it["risk"],
                "risk_reasons": it["risk_reasons"],
                "tone": _BUCKET_TONE.get(it["risk"], "muted"),
                "days_left": it["days_left"],
                "progress": it["progress"],
                "deadline": it["deadline"],
                "priority": it["priority"],
                # both = 既在分管范围内、我又手动挑了；界面只在 manual/both 上打标
                "source": "both" if (in_unit and in_manual) else ("manual" if in_manual else "unit"),
            }
        )
    rows.sort(key=lambda r: (_RISK_ORDER.get(r["risk"], 9), r["days_left"]))
    return rows


def workspace(person_id: str = "mayor") -> dict[str, Any]:
    """个人工作台：谁看的，就给他谁的关注、动态和跟进事项。

    身份来自 core/people.py（演示身份，不是登录态）。关注列表按人存在
    data/workspaces.json，没存过就是该身份的默认值。
    """
    person = people.meta(person_id)
    codes = person["watch"]
    cards = kpis(codes)
    latest = md.latest_available()
    year, month = int(latest[:4]), int(latest[5:7])
    feed = _watch_feed(cards, people.seen_of(person_id), person.get("units") or [])
    follow_all = follow_items(person)
    # 派生项只留需要注意的（risk=正常 的挤进来会把逾期的稀释掉）；
    # **手动挑进来的照留** —— 那是本人明确要看的事，系统再替他筛一遍就等于没让他挑。
    attention = [i for i in follow_all if i["source"] != "unit" or i["risk"] != "正常"]
    follow = attention[:_FOLLOW_MAX]
    units = [u for u in (person.get("units") or []) if u]
    return {
        "city": CITY,
        "title": "个人工作台",
        "person": person,
        "period": f"{year}年1—{month}月" if month > 1 else f"{year}年1月",
        "period_label": f"{year}年{month}月",
        "watch": cards,
        "watch_count": len(cards),
        # 收藏的问答：和指标关注并列显示在同一板块，但两套数据、两套上限。
        # 这里只做投影，取数/写入全在 core/saved.py。
        "saved": saved_mod.to_meta(person_id),
        "catalog": metric_catalog(codes),
        "feed": feed,
        "unread": sum(1 for f in feed if f["unread"]),
        "follow": {
            "count": len(follow),
            "items": follow,
            # total 是分管范围内未办结总数，count 是其中需要注意的。两个数分开报，
            # 是为了让"没有注意事项"和"没有在办事项"能说成两句不同的话。
            "total": len(follow_all),
            "normal": len(follow_all) - len(attention),
            "manual": sum(1 for i in follow if i["source"] in ("manual", "both")),
            "units": units,
            # 说清这块是怎么来的：按分管单位派生，不是人工维护的"我要跟进"
            "basis": "分管单位：" + "、".join(units) if units else "全市",
        },
        # 板块顺序与显隐（按人配）；前端照着排，没有就退回默认三块
        "layout": person.get("layout") or list(people.DEFAULT_LAYOUT),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mock": True,
    }


def metric_catalog(watched: list[str]) -> list[dict[str, Any]]:
    """「添加关注」选择器的数据：全部已注册指标 + 当前值 + 是否已关注。

    带当前值是有意的：选指标时人想知道"这个数现在是多少"，只给名字等于让人瞎选。
    """
    seen = set(watched or [])
    out: list[dict[str, Any]] = []
    for code, meta in md.METRICS.items():
        r = md.query_metric(code, None, 2)
        unit = r["unit"]
        out.append(
            {
                "code": code,
                "name": r["name"],
                "unit": unit,
                "display": f"{r['value']:,}" if unit in ("亿元", "户", "个", "项") else f"{r['value']:g}",
                "note": _target_note(r),
                "watched": code in seen,
            }
        )
    return out


def _period_text(code: str, label: str) -> str:
    """累计口径要说清是 1—N 月累计，不能只说月份。"""
    meta = md.METRICS.get(code, {})
    if not meta.get("cumulative") or len(label) < 7:
        return label
    try:
        year, month = int(label[:4]), int(label[5:-1])
    except (ValueError, TypeError):
        return label
    return f"{year}年全年" if month == 12 else f"{year}年1—{month}月累计"


def metric_detail(code: str) -> dict[str, Any] | None:
    if code not in md.METRICS:
        return None
    r = md.query_metric(code, None, 12)
    m = md.METRICS[code]
    yoy_text, yoy_tone, yoy_why = _fmt_yoy(r)
    # 真实年度值不配演示走势：`query_metric` 覆盖后 value 是 2025 年公报值，
    # series 仍停在演示序列的 2026-10。两条一起画，就成了「1746.3，正在跌到
    # 1428.7」—— 一期真实的年度数和一条编出来的月度曲线，读者没法分辨哪半是真的。
    # 这里干脆不给序列，用一句话说明为什么没有，比画一条假线诚实。
    overridden = bool(r.get("real_override"))
    series_note = ""
    if overridden:
        # 「2025年（全年）」→「2025年全年」：括号在句子里读着别扭
        plabel = r["period_label"].replace("（", "").replace("）", "")
        series_note = (
            f"本指标目前只接入 {plabel}统计公报值，没有可比的月度序列。"
            "演示用的月度曲线与它不是同一期、也不是同一口径，已隐藏 —— "
            "把它和上面的年度数并排看，会被误当成年内下滑。"
        )
    return {
        "code": code,
        "name": r["name"],
        "value": r["value"],
        "unit": r["unit"],
        "period_label": r["period_label"],
        "period_text": _period_text(code, r["period_label"]),
        "yoy_text": yoy_text,
        "yoy_tone": yoy_tone,
        "yoy_why": yoy_why,
        "target": r.get("target"),
        "target_note": m.get("target_note"),
        "definition": m["definition"],
        "caveats": r["caveats"],
        "aliases": m["aliases"],
        "source": {
            "department": r["source"]["department"],
            "report": r["source"]["report"],
            "period": r["period_label"],
            "disclosed_on": r["source"].get("published_at"),
            "api": m["api"],
        },
        "real_override": overridden,
        "series": [] if overridden else r["series"],
        "series_note": series_note,
    }
