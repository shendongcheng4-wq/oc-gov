"""冒烟测试：数据层的结构性约束 + （服务在跑时）HTTP 接口。

    python tests/smoke.py

不依赖 pytest，失败即非零退出。有一条硬断言值得单说：
罗盘卡片和问数必须读同一套指标字典 —— 这里用「同一个指标代码取两次，
数值必须相等」来兜住，防止有人给页面单独备一份数字。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "metrics"))
sys.path.insert(0, str(ROOT / "core" / "oversight"))
sys.path.insert(0, str(ROOT / "core" / "meeting"))

# 数据层测试必须跑在独立目录里：modelcfg / chat 会写文件，不能把真实配置和会话
# 搅坏。HTTP 测试打的是已启动的服务（另一个进程），读的是真实数据，所以那里的
# 写操作要自己收尾（用完就删会话）。
TEST_DATA = ROOT / "data" / "_test"
if not os.environ.get("OCGOV_TEST_KEEP"):
    shutil.rmtree(TEST_DATA, ignore_errors=True)
os.environ["OCGOV_DATA"] = str(TEST_DATA)

import chat  # noqa: E402
import extract as mt_extract  # noqa: E402
import ledger as mt_ledger  # noqa: E402
import modelcfg  # noqa: E402
import oversight as ov  # noqa: E402
import workbench as wb  # noqa: E402
from mockdata import DISCLOSURE_DAY, METRICS, query_metric  # noqa: E402

from datetime import date, timedelta  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(f"{name} {detail}")


def test_data_layer() -> None:
    print("[1] 数据层")

    kpis = wb.kpis()
    check("四个 KPI 卡", len(kpis) == 4, f"实际 {len(kpis)}")
    for k in kpis:
        check(
            f"KPI {k['code']} 带出处",
            bool(k.get("source", {}).get("department") and k["source"].get("report") and k["source"].get("api")),
            str(k.get("source")),
        )
        check(f"KPI {k['code']} 有走势", len(k.get("series") or []) >= 2, f"{len(k.get('series') or [])} 期")
        check(f"KPI {k['code']} 有口径提示", bool(k.get("caveats")), "")

    boards = wb.boards()
    check("五个板块", len(boards) == 5, f"实际 {len(boards)}")
    for b in boards:
        check(f"板块 {b['key']} 有判定依据", bool(b.get("status_why")), str(b.get("status")))
        check(f"板块 {b['key']} 状态合法", b.get("status") in ("正常", "关注", "预警"), str(b.get("status")))

    overview = wb.overview()
    check("总览带城市名", bool(overview.get("city")), "")
    check("总览带披露日期", bool(overview.get("disclosed_on")), "")
    check("总览指标数与字典一致", overview["metric_count"] == len(METRICS), f"{overview['metric_count']} vs {len(METRICS)}")


def test_workspace() -> None:
    """个人工作台：关注卡片与罗盘同源，动态每条可追溯到台账或接口；
    关注列表按人存、互不串。"""
    print("[6] 个人工作台")

    import people

    base = people.watch_of("mayor")
    ws = wb.workspace("mayor")
    check("默认身份关注数 = 默认表", [c["code"] for c in ws["watch"]] == base, f"{[c['code'] for c in ws['watch']]} vs {base}")
    check("关注指标全部在字典里", all(c in METRICS for c in base), str(base))
    check("关注数与声明一致", ws["watch_count"] == len(ws["watch"]), str(ws.get("watch_count")))

    for card in ws["watch"]:
        raw = query_metric(card["code"], None, 7)
        check(
            f"{card['code']} 卡片值 == 指标接口值",
            abs(float(card["value"]) - float(raw["value"])) < 1e-9,
            f"卡片 {card['value']} / 接口 {raw['value']}",
        )
        check(f"{card['code']} 有口径提示", bool(card.get("caveats")), "")

    check("动态有内容", len(ws["feed"]) > 0, "空列表")
    check("动态不超过上限", len(ws["feed"]) <= 6, f"实际 {len(ws['feed'])}")
    titles = [f["text"] for f in ws["feed"]]
    check("动态不重复", len(titles) == len(set(titles)), str(titles))
    for item in ws["feed"]:
        check(f"动态「{item['text'][:12]}」有状态标签", bool(item.get("tag")), "")
        check(f"动态「{item['text'][:12]}」有来源", bool(item.get("source")), "")
        check(f"动态「{item['text'][:12]}」色标合法", item.get("tone") in ("danger", "warn", "info", "ok", "muted"), str(item.get("tone")))
        check(f"动态「{item['text'][:12]}」有分类标签", item.get("label") in ("关注指标", "临近时限", "督办动态", "待披露", "正常"), str(item.get("label")))
        check(f"动态「{item['text'][:12]}」有已读标记", isinstance(item.get("unread"), bool), str(item.get("unread")))

    # 未读数不是数出来的一个假数字，必须等于 feed 里 unread 的条数
    check("未读数 = 未读条数", ws["unread"] == sum(1 for f in ws["feed"] if f["unread"]), f"{ws['unread']}")

    # 「我跟进的」：按分管单位从台账派生，台账里没有的不会凭空出现
    for pid in ("mayor", "vmayor", "bureau"):
        w = wb.workspace(pid)
        fu = w["follow"]
        units = set(w["person"].get("units") or [])
        check(f"{pid} 跟进数 = 列表长度", fu["count"] == len(fu["items"]), f"{fu['count']}")
        check(f"{pid} 跟进不含已办结", all(i["status"] != "已办结" for i in fu["items"]), str([i["status"] for i in fu["items"]]))
        if units:
            check(
                f"{pid} 跟进都落在分管单位内",
                all(units & ({i["assignee"]} | set(i["co_assignees"])) for i in fu["items"]),
                str([(i["assignee"], i["co_assignees"]) for i in fu["items"]]),
            )
        ranks = [wb._RISK_ORDER.get(i["risk"], 9) for i in fu["items"]]
        check(f"{pid} 跟进按紧急度排序", ranks == sorted(ranks), str(ranks))
        for i in fu["items"]:
            raw = ov.detail(i["id"])
            check(f"{pid}/{i['id']} 进度与台账一致", raw["progress"] == i["progress"], f"{raw['progress']} vs {i['progress']}")
            check(f"{pid}/{i['id']} 剩余天数与台账一致", raw["days_left"] == i["days_left"], f"{raw['days_left']} vs {i['days_left']}")

    # 局长的跟进里不该出现别人独办的事 —— 这是「按分管派生」最容易写错的地方
    bu = wb.workspace("bureau")["follow"]["items"]
    check(
        "局长跟进不含他部门独办事项",
        all(i["assignee"] == "市生态环境局" or "市生态环境局" in i["co_assignees"] for i in bu),
        str([(i["id"], i["assignee"]) for i in bu]),
    )

    # 标题是「重点注意事项」，就不能混进 risk=正常 的事项：那样这一块会退化成
    # 「我的待办全表」，真正逾期的反被稀释。total 与 count 要能区分开。
    for pid in ("mayor", "vmayor", "bureau"):
        fu = wb.workspace(pid)["follow"]
        check(f"{pid} 注意事项不含正常项", all(i["risk"] != "正常" for i in fu["items"]), str([i["risk"] for i in fu["items"]]))
        check(f"{pid} total ≥ count", fu["total"] >= fu["count"], f"{fu['total']} vs {fu['count']}")
        check(f"{pid} total = count + normal", fu["total"] == fu["count"] + fu["normal"], f"{fu['total']} {fu['count']} {fu['normal']}")

    # 已读：写进去之后未读归零，且不影响关注列表
    import people
    before_watch = people.watch_of("mayor")
    keys = [f["key"] for f in wb.workspace("mayor")["feed"]]
    people.mark_seen("mayor", keys)
    w2 = wb.workspace("mayor")
    check("标为已读后未读归零", w2["unread"] == 0, f"{w2['unread']}")
    check("已读不影响关注列表", people.watch_of("mayor") == before_watch, str(people.watch_of("mayor")))
    check("已读不影响动态条数", len(w2["feed"]) == len(keys), f"{len(w2['feed'])} vs {len(keys)}")

    # 内容变了要重新算未读：key 里带指纹，否则「数变了还显示已读」
    k1 = wb._feed_key("metric", "GDP_TOTAL", "地区生产总值低于目标 0.8 个点")
    k2 = wb._feed_key("metric", "GDP_TOTAL", "地区生产总值低于目标 0.3 个点")
    check("内容变则 key 变", k1 != k2, f"{k1} / {k2}")
    check("内容同则 key 同", k1 == wb._feed_key("metric", "GDP_TOTAL", "地区生产总值低于目标 0.8 个点"), "")


def test_people() -> None:
    """「每个人」这层：三个身份、各自一份关注、互不影响。

    这里刻意不用 HTTP：身份是数据层的事，接个新入口（MCP/CLI）也得走同一套规则。
    """
    print("[7] 演示身份与个人关注")

    import people

    check("三个预设身份", len(people.all_people()) == 3, str([p["id"] for p in people.all_people()]))
    check("未知身份回落默认", people.resolve("ghost") == "mayor", people.resolve("ghost"))
    check("未知身份读到默认关注", people.watch_of("ghost") == people.DEFAULT_WATCH["mayor"], str(people.watch_of("ghost")))

    # 三个身份的关注范围设计上就不同，否则"换人"看不出来
    mayor = [c["code"] for c in wb.workspace("mayor")["watch"]]
    vm = [c["code"] for c in wb.workspace("vmayor")["watch"]]
    bu = [c["code"] for c in wb.workspace("bureau")["watch"]]
    check("市长与常务看的不一样", mayor != vm, f"{mayor} / {vm}")
    check("常务与局长看的不一样", vm != bu, f"{vm} / {bu}")
    check("返回里带身份信息", wb.workspace("vmayor")["person"]["id"] == "vmayor", "")

    # 存一份、读回来
    r = people.set_watch("bureau", ["AQI_GOOD_DAYS", "UR_RATE"])
    check("写入关注", r.get("ok") is True and people.watch_of("bureau") == ["AQI_GOOD_DAYS", "UR_RATE"], str(r))
    check("只影响本人", people.watch_of("mayor") == people.DEFAULT_WATCH["mayor"], str(people.watch_of("mayor")))

    r = people.set_watch("bureau", ["AQI_GOOD_DAYS", "AQI_GOOD_DAYS", "UR_RATE"])
    check("重复项自动去重", r.get("watch") == ["AQI_GOOD_DAYS", "UR_RATE"], str(r.get("watch")))

    # 已读状态和关注列表存在同一个人身上，两次写入不能互相抹掉
    people.mark_seen("bureau", ["metric:AQI_GOOD_DAYS:seed"])
    check("已读写进本人", people.seen_of("bureau") == ["metric:AQI_GOOD_DAYS:seed"], str(people.seen_of("bureau")))
    check("已读不串到别人", "metric:AQI_GOOD_DAYS:seed" not in people.seen_of("mayor"), str(people.seen_of("mayor")))
    people.set_watch("bureau", ["AQI_GOOD_DAYS", "UR_RATE"])
    check("改关注不清空已读", "metric:AQI_GOOD_DAYS:seed" in people.seen_of("bureau"), str(people.seen_of("bureau")))
    check("改关注照常生效", people.watch_of("bureau") == ["AQI_GOOD_DAYS", "UR_RATE"], str(people.watch_of("bureau")))
    people.mark_seen("bureau", ["metric:AQI_GOOD_DAYS:seed"])
    check("重复标已读不重复计", people.seen_of("bureau").count("metric:AQI_GOOD_DAYS:seed") == 1, str(people.seen_of("bureau")))
    r = people.mark_seen("bureau", "not-a-list")
    check("已读参数类型被拒", r.get("ok") is False, str(r))

    r = people.set_watch("bureau", ["NO_SUCH_METRIC"])
    check("未注册指标被拒", r.get("ok") is False and "没有注册" in (r.get("error") or ""), str(r))

    nine = ["GDP_TOTAL", "GY_VA_GROWTH", "BUDGET_REV", "TZ_12345_CLOSURE", "FAI_GROWTH",
            "AQI_GOOD_DAYS", "UR_RATE", "SS_ENTERPRISE", "INVEST_SIGNED"]
    r = people.set_watch("bureau", nine)
    check("超上限被拒", r.get("ok") is False and "最多关注" in (r.get("error") or ""), str(r))
    check("被拒后原列表不变", people.watch_of("bureau") == ["AQI_GOOD_DAYS", "UR_RATE"], str(people.watch_of("bureau")))

    people.set_watch("bureau", [])
    check("清空后返回空卡片", wb.workspace("bureau")["watch"] == [], "还有卡片")
    check("清空不影响罗盘", len(wb.kpis()) == 4, "罗盘被带走了")

    people.set_watch("bureau", list(people.DEFAULT_WATCH["bureau"]))

    # codes=None = 取消覆盖、回到默认表。留一份写死的默认值会盖住以后对默认表的调整：
    # 改默认的人会发现「改了却对这个人不生效」，所以要看落库里 watch 键是否真的消失。
    import store

    r = people.set_watch("bureau", None)
    check("reset 回到默认表", r.get("ok") is True and r.get("reset") is True and people.watch_of("bureau") == list(people.DEFAULT_WATCH["bureau"]), str(r))
    raw = (store.read("workspaces", {}) or {}).get("people", {}).get("bureau") or {}
    check("reset 删掉 watch 键而非写入默认值", "watch" not in raw, str(raw)[:90])
    check("reset 保留已读", "metric:AQI_GOOD_DAYS:seed" in people.seen_of("bureau"), str(people.seen_of("bureau")))

    # 手动跟进：分管范围是**组织关系**，不是**注意力**。市长要盯一件不归他分管的事，
    # 必须能加进来，而且不能被"risk=正常就过滤掉"那条规则顺手删掉。
    bu_units = {"市生态环境局"}
    opened = [i for i in ov.list_items() if i["status"] != ov.CLOSED]
    outside = [
        i["id"]
        for i in opened
        if i["assignee"] not in bu_units and not (bu_units & set(i["co_assignees"]))
    ]
    if outside:
        r = people.set_follow("bureau", [outside[0]])
        check("手动跟进写入", r.get("ok") is True and people.follow_of("bureau") == [outside[0]], str(r))
        items = wb.workspace("bureau")["follow"]["items"]
        row = next((x for x in items if x["id"] == outside[0]), None)
        check("不归分管的事项也能进跟进", row is not None, str([x["id"] for x in items]))
        check("手动项标为 manual", row["source"] == "manual" if row else False, str(row and row["source"]))
        check("手动跟进不串到别人", people.follow_of("mayor") == [], str(people.follow_of("mayor")))
        normal = [i["id"] for i in opened if i["risk"] == "正常"]
        if normal:
            people.set_follow("bureau", [normal[0]])
            got = wb.workspace("bureau")["follow"]["items"]
            check("手动项不因「正常」被过滤", any(x["id"] == normal[0] for x in got), str([x["id"] for x in got]))
    r = people.set_follow("bureau", ["NO-SUCH-ITEM"])
    check("不存在的事项被拒", r.get("ok") is False and "台账里没有" in (r.get("error") or ""), str(r))
    people.set_follow("bureau", [])

    # 工作台布局：顺序与显隐按人存，但至少留一块（空工作台等于把自己锁在门外）
    r = people.set_layout("bureau", ["follow", "watch"])
    check("布局写入", r.get("ok") is True and people.layout_of("bureau") == ["follow", "watch"], str(r))
    check("布局回显到工作台", wb.workspace("bureau")["layout"] == ["follow", "watch"], str(wb.workspace("bureau")["layout"]))
    check("布局只影响本人", people.layout_of("mayor") == list(people.DEFAULT_LAYOUT), str(people.layout_of("mayor")))
    r = people.set_layout("bureau", [])
    check("空布局被拒", r.get("ok") is False and "至少" in (r.get("error") or ""), str(r))
    check("被拒后布局不变", people.layout_of("bureau") == ["follow", "watch"], str(people.layout_of("bureau")))
    r = people.set_layout("bureau", ["weather"])
    check("未知板块被拒", r.get("ok") is False and "没有这个板块" in (r.get("error") or ""), str(r))
    people.set_layout("bureau", list(people.DEFAULT_LAYOUT))


def test_alerts() -> None:
    print("[2] 督办预警")

    alerts = wb.alerts()
    check("预警条有内容", len(alerts) > 0, "台账为空")
    check("预警条不超过 3 条", len(alerts) <= 3, f"实际 {len(alerts)}")
    for a in alerts:
        check(f"{a['id']} 有风险等级", a.get("risk") in ("逾期", "今日到期", "临期", "签收超时", "停滞"), str(a.get("risk")))
        check(f"{a['id']} 有判定理由", bool(a.get("why")), "")
        check(f"{a['id']} 有责任单位", bool(a.get("assignee")), "")


def test_same_source() -> None:
    """罗盘和问数必须读同一套字典。

    boards() 对外只给格式化好的 value_display（「8.9%」「38 / 45 项」），所以从
    展示串里取第一个数字，与指标接口返回的 value 对齐 —— 对不上就说明卡片藏了
    一份自己的数字。
    """
    print("[3] 罗盘与问数同源")

    for board in wb.boards():
        code = board["code"]
        raw = query_metric(code, None, 7)
        m = re.search(r"-?\d+(?:\.\d+)?", board.get("value_display") or "")
        check(f"{code} 卡片值可解析", m is not None, str(board.get("value_display")))
        if not m:
            continue
        check(
            f"{code} 卡片值 == 指标接口值",
            abs(float(m.group()) - float(raw["value"])) < 1e-9,
            f"卡片 {m.group()} / 接口 {raw['value']}",
        )

    check("KPI 全部在指标字典里", all(c in METRICS for c in wb.KPI_CODES), str(wb.KPI_CODES))


def test_models() -> None:
    print("[4] 模型配置层")

    p = modelcfg.upsert_provider(
        {
            "name": "testprov",
            "base_url": "https://example.invalid/v1",
            "api_key": "sk-unit-test-0001",
            "models": [
                {"id": "m-chat"},
                {"id": "m-embed", "chat": False},
                {"id": "m-off", "enabled": False},
            ],
        }
    )
    check("新建 provider", p.get("name") == "testprov", str(p.get("name")))

    listed = next((x for x in modelcfg.list_providers() if x["id"] == p["id"]), {})
    check("列表默认不返回明文密钥", "api_key" not in listed, str(list(listed.keys())))
    check("列表给出掩码", str(listed.get("api_key_masked", "")).endswith("0001"), listed.get("api_key_masked"))

    refs = [r["ref"] for r in modelcfg.resolved()]
    check("chat=true 进选择器", "testprov/m-chat" in refs, str(refs))
    check("chat=false 不进选择器", "testprov/m-embed" not in refs, str(refs))
    check("enabled=false 不进选择器", "testprov/m-off" not in refs, str(refs))

    modelcfg.set_active("testprov/m-chat")
    check("默认模型可设置", modelcfg.active_ref() == "testprov/m-chat", str(modelcfg.active_ref()))

    conn = modelcfg.resolve("testprov/m-chat")
    check(
        "resolve 返回可调用参数",
        bool(conn) and conn["model"] == "m-chat" and conn["api_key"] == "sk-unit-test-0001",
        str(conn and conn.get("ref")),
    )
    check("显式 ref 不标记回退", bool(conn) and conn["fell_back"] is False, str(conn and conn.get("fell_back")))

    fallback = modelcfg.resolve("ghost/none")
    check(
        "无效 ref 显式标记已回退（不静默）",
        bool(fallback) and fallback["fell_back"] is True,
        str(fallback and fallback.get("ref")),
    )

    modelcfg.delete_provider(p["id"])
    refs_after = [r["ref"] for r in modelcfg.resolved()]
    check("删除后不在选择器", "testprov/m-chat" not in refs_after, str(refs_after))
    check("删除后默认模型不悬空", modelcfg.active_ref() != "testprov/m-chat", str(modelcfg.active_ref()))

    # 只改备注不该把密钥清掉（对应 octop 的 UNSET 哨兵）
    p2 = modelcfg.upsert_provider(
        {"name": "keepkey", "base_url": "https://example.invalid/v1", "api_key": "sk-keep-9999", "models": [{"id": "k1"}]}
    )
    modelcfg.upsert_provider(
        {"id": p2["id"], "name": "keepkey", "base_url": "https://example.invalid/v1", "note": "只改备注", "models": [{"id": "k1"}]}
    )
    kept = next((x for x in modelcfg.list_providers(True) if x["id"] == p2["id"]), {})
    check("改备注不清空密钥", kept.get("api_key") == "sk-keep-9999", str(kept.get("api_key")))
    modelcfg.delete_provider(p2["id"])


def test_chat() -> None:
    print("[5] 会话层")

    t = chat.create()
    check("新建会话", bool(t["id"]), str(t.get("id")))

    chat.append(t["id"], "user", "9月规上工业增加值增速是多少")
    chat.append(
        t["id"],
        "assistant",
        "2026年9月全市规模以上工业增加值增速为 8.9%",
        meta={"intent": "value", "metric_names": ["规模以上工业增加值增速"], "refused": False},
    )
    chat.append(t["id"], "user", "那上个月呢")

    thread = chat.get(t["id"])
    seqs = [m["seq"] for m in thread["messages"]]
    check("seq 从 1 单调递增", seqs == [1, 2, 3], str(seqs))
    check("首条用户消息成为标题", thread["title"].startswith("9月规上"), thread["title"])

    ctx = chat.context(t["id"])
    check("上下文条数一致", len(ctx) == 3, str(len(ctx)))
    check("上下文只有 user/assistant", all(c["role"] in ("user", "assistant") for c in ctx), str(ctx))

    # 出参统计：界面要据此告诉用户「哪几轮已经不在上下文里」。
    # 少这两行，用户唯一能感知到的就是「它怎么把刚才说的忘了」。
    stats: dict = {}
    chat.context(t["id"], out=stats)
    check(
        "上下文统计回传总轮数与使用轮数",
        stats.get("turns_total") == 2 and stats.get("turns_used") == 2 and stats.get("dropped") == 0,
        str(stats),
    )

    # 预算压到极小：至少保最近一轮，并把裁掉的轮数如实报出来
    tight: dict = {}
    chat.context(t["id"], token_budget=1, out=tight)
    check(
        "预算不够时只保最近一轮且报出裁掉数",
        tight.get("turns_used") == 1 and tight.get("dropped") == tight.get("turns_total") - 1,
        str(tight),
    )
    # 不传 out 时不能炸（老调用方还在用两参数形式）
    check("不传出参时不报错", isinstance(chat.context(t["id"], token_budget=1), list), "")

    # 安全边界：回灌给模型的历史不能带数值，否则模型下一轮更可能复述数字
    assistant_ctx = ctx[1]["content"]
    check("摘要不含百分比/小数", not re.search(r"\d+\.\d+|\d+\s*%", assistant_ctx), assistant_ctx)
    check("摘要不含原始答案", "8.9" not in assistant_ctx, assistant_ctx)

    check("清理不匹配前缀返回 0", chat.clear_model_refs(prefix="nonexistent/") == 0, "")
    check("删除会话", chat.delete(t["id"]) is True, "")
    check("删除后取不到", chat.get(t["id"]) is None, "")


def test_experts() -> None:
    """专家团：只解读、不报数。

    这里重点验「数字闸」—— 它是专家团唯一能伤到数值可信度的地方，
    所以规则必须是纯函数、可单独断言，而不是藏在提示词里靠模型自觉。
    """
    print("[8] 可召唤的专家团")

    import experts as ex

    roster = ex.all_experts()
    # 不写死 ==3：名单是可扩的。这里验「至少三位、且每条都完整」，
    # 扩名单不会误红，删到不足三位会红。
    check("专家名单至少三位", len(roster) >= 3, str([e["id"] for e in roster]))
    check("每位都有头像与关注面", all(e.get("avatar") and e.get("focus") for e in roster), str(roster)[:90])
    check("未知专家被忽略", ex.interpret({"question": "x", "metric_names": []}, ["ghost"]) == [], "居然产生了数据")

    allowed = ex._allowed_numeric_tokens(["12345 群众诉求按期办结率"])
    check("阿拉伯数字被判违规", ex._has_forbidden_number("增速上升 2 个百分点", allowed) is True, "")
    check("百分号被判违规", ex._has_forbidden_number("增长约 5%", allowed) is True, "")
    check("纯定性文字放行", ex._has_forbidden_number("增速偏高，投资偏弱", allowed) is False, "")
    check("指标名里的 12345 不误判", ex._has_forbidden_number("12345 热线办理质量偏弱", allowed) is False, "")

    # compare 型回答的数值落在 compare_rows、facts 是空列表。只读 facts 的话，
    # 多指标一问里专家看不到任何方向，只会回「信息不足」—— 召唤了等于没召唤。
    cmp_sig = ex._signals(
        {
            "metric_names": ["规模以上工业增加值增速", "空气质量优良天数比例"],
            "facts": [],
            "compare_rows": [
                {"name": "规模以上工业增加值增速", "yoy": 2.2},
                {"name": "空气质量优良天数比例", "yoy": -0.7},
            ],
        }
    )
    check("compare 型也能拿到方向信号", len(cmp_sig) == 2, str(cmp_sig))
    # 防回退：信号必须按行归属。统一用 metric_names[0] 套的话，会把空气质量的方向
    # 安到工业增速头上 —— 错位的方向比没有方向更危险。
    check("compare 型信号按指标逐行归属", "空气质量" in cmp_sig[1] and "下降" in cmp_sig[1], str(cmp_sig))
    # value 型仍走 facts 分支，两条路径不能互相挤掉
    val_sig = ex._signals(
        {"metric_names": ["规模以上工业增加值增速"], "facts": [{"label": "环比", "raw": 1.7}, {"label": "同比", "raw": -0.2}]}
    )
    check("value 型仍从 facts 取信号", len(val_sig) == 2 and "上升" in val_sig[0] and "下降" in val_sig[1], str(val_sig))

    # 数字闸要端到端验：模型真吐了数字，那一段必须被整段丢掉，而不是「修正」后放行
    old_chat = ex.llmclient.chat_json
    old_avail = ex.llmclient.available
    ex.llmclient.available = lambda: True
    ex.llmclient.chat_json = lambda *a, **k: {"data": {"analysis": "增速上升 2 个百分点，压力仍在"}, "model": "stub"}
    try:
        dirty = ex.interpret({"question": "x", "metric_names": ["规模以上工业增加值增速"], "facts": []}, ["economy"])[0]
    finally:
        ex.llmclient.chat_json = old_chat
        ex.llmclient.available = old_avail
    check("模型回了数字则整段丢弃", dirty["ok"] is False and not dirty["text"], str(dirty)[:90])
    check("丢弃时写明原因", "数字" in (dirty["why"] or ""), str(dirty.get("why")))

    # 没模型时必须给出原因，而不是抛异常或编一段
    old = ex.llmclient.available
    ex.llmclient.available = lambda: False
    try:
        views = ex.interpret(
            {"question": "规上工业多少", "metric_names": ["规模以上工业增加值增速"], "facts": []},
            ["economy", "livelihood", "eco", "economy"],
        )
    finally:
        ex.llmclient.available = old
    check("一次最多三段", len(views) == 3, f"{len(views)} 段")
    check("无模型时标注原因", all(v["ok"] is False and v["why"] for v in views), str(views)[:90])
    check("无模型时不编内容", all(not v["text"] for v in views), str(views)[:90])

    # 进度事件：每位专家都要有 start 和 done。失败路径漏掉 done 的话，
    # 流式界面会永远停在「正在看材料」，比不报进度更糟。
    steps: list[dict] = []
    ex.llmclient.available = lambda: False
    try:
        ex.interpret(
            {"question": "x", "metric_names": [], "facts": []},
            ["economy", "livelihood"],
            on_step=lambda name, data: steps.append(data),
        )
    finally:
        ex.llmclient.available = old
    check("每位专家各报 start/done", [s["phase"] for s in steps] == ["start", "done", "start", "done"], str([s["phase"] for s in steps]))
    check("失败时 done 带 ok=False", all(s["ok"] is False for s in steps if s["phase"] == "done"), str(steps)[:90])

    # 落库裁剪：专家解读要能回放，但不能把 model/model_ref 一起塞进 chats.json
    kept = ex.to_meta(
        [
            {
                "id": "economy",
                "name": "经济参谋",
                "avatar": "经",
                "focus": "工业",
                "ok": True,
                "text": "增速偏高，结构偏弱",
                "why": "",
                "model": "deepseek-flash",
                "model_ref": "deepseek/deepseek-chat",
            }
        ]
    )
    check("落库保留全部回放字段", set(kept[0]) == set(ex.META_KEYS), str(kept[0]))
    check("落库丢掉模型细节", "model" not in kept[0] and "model_ref" not in kept[0], str(kept[0]))
    check("无解读时裁成空列表", ex.to_meta(None) == [] and ex.to_meta([]) == [], "")

    # 端到端：落一轮回答后重读历史，专家解读还在（回看不该凭空少一段）
    th = chat.create("smoke-expert-meta")
    chat.append(th["id"], "assistant", "标题", meta={"expert_views": kept})
    again = chat.get(th["id"]) or {}
    back = ((again.get("messages") or [{}])[0].get("meta") or {}).get("expert_views") or []
    check("重读历史仍带专家解读", len(back) == 1 and back[0].get("name") == "经济参谋", str(back)[:90])
    # 且这份东西不能污染模型上下文（_summary 只取 指标名/意图/拒答）
    ctx = chat.context(th["id"])
    check("专家解读不进模型上下文", all("增速偏高" not in m.get("content", "") for m in ctx), str(ctx)[:90])
    chat.delete(th["id"])

    # 前端静态约定：没选人就是空白，不给「不召唤」一个名字；全组在 V3 里叫「AI秘书团」
    # （下拉菜单最后一项与该身份的标签必须同名，不然同一群人两个叫法）。
    # 这两条是产品口径，改坏了页面照样能跑，只能在这里卡住。
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("默认态不留「不召唤」占位", '>不召唤</b>' not in html and '"不召唤"' not in html, "")
    check("空态不占位（:empty 隐藏）", ".es-name:empty" in html and ".es-state:empty" in html, "")
    check("全组叫 AI秘书团", 'return "AI秘书团"' in html and 'item("__all", "团", "AI秘书团"' in html, "")
    check("再点一次可取消", "EXP.cur = !eid || EXP.cur === eid ? null : eid" in html, "")

    # 斜杠唤起（输入框里打 / 选参谋）。这几条都是「写错了页面照跑、但用起来是坏的」
    # 的地方，只能靠静态约定卡住：
    #   - 认「含 /」而不认「行首 /」→ 问句里写「投资/工业」就弹菜单，问题打不完
    #   - 只认半角 → 中文输入法上屏的 ／（U+FF0F）触发不了
    #   - 抢组词中的 Enter → 打中文必误选人
    #   - 选中另走一条设置逻辑 → 下拉和斜杠能选出两种状态
    check("斜杠唤起层存在", 'id="slashMenu"' in html and 'class="slash-menu"' in html, "")
    check("斜杠只在行首认", 'if (v[0] !== "/" && v[0] !== "／") return null;' in html, "")
    check("全角斜杠也认", 'v[0] !== "／"' in html, "")
    check("组词中不抢 Enter", "e.isComposing || e.keyCode === 229" in html, "")
    pick = html.split("function pickSlash(id)")[1][:220]
    check("斜杠选中与下拉同一出口", "selectExpert(id);" in pick, pick[:80])
    check("菜单项与下拉名单同源", "/api/experts" in html and "EXP.list.map((e) => ({" in html, "")
    check("没匹配不许当问题发出去", 'return toast("没有这个参谋' in html, "")


def test_meeting() -> None:
    print("[9] 会议助手：引文校验 / 人工认领 / 落督办")

    from datetime import timedelta

    # 时限必须用相对今天算出来的日子：写死 2026-11-20 的话，过几天这套测试就全红了。
    # 同时「20 日」必须真的出现在原文里，否则会被 deadline_not_grounded 拦下 —— 这正是
    # 我们要测的行为，所以原文句子由同一个 dl 拼出来。
    dl = date.today() + timedelta(days=18)
    text = (
        f"一、会议议定：由市住建局牵头，{dl.month}月{dl.day}日前完成管线迁改和路面恢复，"
        "验收标准为管线全部入地并通过竣工验收。\n"
        "二、会议要求市城管局会同云山区政府，年底前完成停车泊位施划，具体数量另行研究。"
    )
    units = mt_extract.known_units()

    def codes(item: dict) -> set[str]:
        return {i["code"] for i in mt_extract.verify_item(item, text, units)["issues"]}

    good = {
        "title": "完成管线迁改和路面恢复",
        "assignee": "市住建局",
        "deadline": dl.isoformat(),
        "standard": "管线全部入地并通过竣工验收",
        "quote": f"由市住建局牵头，{dl.month}月{dl.day}日前完成管线迁改和路面恢复",
        "confidence": "high",
    }
    r = mt_extract.verify_item(good, text, units)
    check("合规条目 -> ok", r["status"] == "ok", str(r["issues"]))
    check("合规条目引文被确认存在", r["quote_found"] is True, "")

    # 闸 1：引文改写
    fake_quote = dict(good, quote="由市住建局牵头，抓紧完成管线迁改")
    check("引文被改写 -> quote_not_found", "quote_not_found" in codes(fake_quote), str(codes(fake_quote)))

    # 闸 2：数字编造
    check(
        "标题编数字 -> number_not_in_source",
        "number_not_in_source" in codes(dict(good, title="完成 3 处管线迁改")),
        "",
    )
    check(
        "时限凭空定 -> deadline_not_grounded",
        "deadline_not_grounded"
        in codes(dict(good, deadline=(date.today() + timedelta(days=400)).replace(day=27).isoformat())),
        "",
    )

    # 闸 3：单位白名单
    check("单位不在名单 -> unknown_unit", "unknown_unit" in codes(dict(good, assignee="市航天局")), "")
    check("单位为空 -> assignee_missing", "assignee_missing" in codes(dict(good, assignee=None)), "")

    # 硬问题：时限已过期，谁改都要拦
    check("时限早于今天 -> deadline_past", "deadline_past" in codes(dict(good, deadline="2020-01-01")), "")

    # 软问题可被人工认领，硬问题不行
    soft = mt_extract.verify_item(
        dict(good, deadline=dl.isoformat()), text, units, manual={"deadline"}
    )
    check("人工认领时限后免检", soft["status"] == "ok" and "deadline" in soft["confirmed_by_human"], str(soft))
    hard = mt_extract.verify_item(
        dict(good, quote="原文里没有这句话"), text, units, manual={"deadline", "title"}
    )
    check("人工认领不能豁免引文校验", hard["status"] == "needs_review", str(hard["issues"]))

    # ---- 台账流转 ----
    m = mt_ledger.create("冒烟测试会议", text, held_at="2026-11-02 09:00", host="市长")
    check("新建会议拿到编号", bool(re.fullmatch(r"MT\d{4}-\d{4}", m["id"])), m["id"])

    parsed = {
        "model_ref": "test/fake",
        "warnings": [],
        "items": [
            {**good, "verify": mt_extract.verify_item(good, text, units)},
            {
                "title": "完成停车泊位施划",
                "assignee": "市城管局",
                "deadline": None,
                "standard": "完成主城区停车泊位施划",
                "quote": "年底前完成停车泊位施划",
                "confidence": "low",
                "verify": mt_extract.verify_item(
                    {
                        "title": "完成停车泊位施划",
                        "assignee": "市城管局",
                        "deadline": None,
                        "standard": "完成主城区停车泊位施划",
                        "quote": "年底前完成停车泊位施划",
                    },
                    text,
                    units,
                ),
            },
        ],
    }
    saved = mt_ledger.save_parse(m["id"], parsed)
    check("解析结果落库", saved.get("ok") is True, str(saved.get("error")))
    items = saved["meeting"]["items"]
    check("两条事项", len(items) == 2, str(len(items)))
    check("第一条可落库", items[0]["verify"]["status"] == "ok", "")
    check("第二条待核实（年底前不给日期）", items[1]["verify"]["status"] == "needs_review", "")

    # 混入未通过的条目 -> 整体拒绝，一条都不写
    before = len(ov.list_items())
    bad = mt_ledger.commit(m["id"], [i["id"] for i in items])
    check("混入待核实 -> 整体拒绝", bad.get("ok") is False and len(ov.list_items()) == before, str(bad.get("error")))

    # 只提通过的
    ok_ids = [items[0]["id"]]
    done = mt_ledger.commit(m["id"], ok_ids)
    check("只提通过条目 -> 成功", done.get("ok") is True and len(done.get("created") or []) == 1, str(done))
    check("台账条数 +1", len(ov.list_items()) == before + 1, str(len(ov.list_items())))
    new_id = (done.get("created") or [{}])[0].get("oversight_id", "")
    created = ov.detail(new_id) or {}
    check("台账条目来源指向会议", str(created.get("source", "")).startswith("会议："), str(created.get("source")))
    check("台账条目来源文号是会议编号", created.get("source_ref") == m["id"], str(created.get("source_ref")))

    again = mt_ledger.commit(m["id"], ok_ids)
    check("重复提交被跳过", again.get("ok") is True and not again.get("created"), str(again))

    check("已落库后不能重新解析", mt_ledger.save_parse(m["id"], parsed).get("ok") is False, "")

    # 人工改字段 -> 重新校验；quote 不允许改
    fixed = mt_ledger.update_item(m["id"], items[1]["id"], {"deadline": dl.isoformat(), "quote": "篡改引文"})
    check("人工补时限后通过", fixed.get("ok") is True and fixed["item"]["verify"]["status"] == "ok", str(fixed))
    check("引文不可篡改", "篡改" not in str(fixed["item"]["quote"]), str(fixed["item"]["quote"]))

    commit2 = mt_ledger.commit(m["id"], [items[1]["id"]])
    check("修正后可落库", commit2.get("ok") is True and len(commit2.get("created") or []) == 1, str(commit2))
    check("全部落库后状态为 committed", mt_ledger.get(m["id"])["status"] == "committed", "")


def test_knowledge() -> None:
    """知识库：口径 / 事项 / 会议原文。

    这里最值钱的一条是「引句必须逐字出现在会议原文中」——知识库一旦开始转述，
    「依据」就变成了第二手说法，和模型编造只差一个自以为是的程度。
    """
    print("\n[13] 知识库：口径 / 事项 / 会议原文可核")
    import ask
    import knowledge as kb
    import llm as llm_mod
    import mockdata as md_mod
    import people as people_mod

    mayor = people_mod.meta("mayor")
    bureau = people_mod.meta("bureau")

    # 自己造数据：不依赖前面几个测试留下了什么，单跑这一段也能过
    today = ov.today()
    a = ov.create_item(
        title="知识库测试·生态环境局事项",
        assignee="市生态环境局",
        deadline=(today + timedelta(days=6)).isoformat(),
        standard="有可核验结果",
    )
    b = ov.create_item(
        title="知识库测试·住建局事项",
        assignee="市住建局",
        deadline=(today + timedelta(days=4)).isoformat(),
        standard="有可核验结果",
    )

    text = (
        "一、会议议定：由市生态环境局负责，12月28日前完成冬季河道巡查并建立问题台账。\n"
        "二、会议要求市城管局加快雪天道路清扫预案报备。"
    )
    mk = mt_ledger.create(
        "知识库检索测试会",
        text,
        held_at="2026-11-01 09:00",
        host="市长",
        attendees=["市生态环境局", "市城管局"],
    )
    mt_ledger.save_parse(
        mk["id"],
        {
            "items": [
                {
                    "title": "完成冬季河道巡查并建立问题台账",
                    "assignee": "市生态环境局",
                    "deadline": "2026-12-28",
                    "quote": "由市生态环境局负责，12月28日前完成冬季河道巡查并建立问题台账",
                    "confidence": "high",
                }
            ]
        },
    )

    idx = kb.index(mayor)
    kinds = {e["kind"] for e in idx}
    check("索引覆盖四类来源", kinds == {"metric", "oversight", "meeting", "decision"}, str(sorted(kinds)))
    no_src = [e["id"] for e in idx if not (e["source"].get("department") and e["source"].get("report"))]
    check("索引每条都带得出出处", not no_src, str(no_src[:3]))

    # 口径必须是字典原样搬运。知识库一旦「顺一下句子」，纸面和口径说明就对不上，
    # 而这种不一致在人工核对时几乎查不出来。
    meta_gy = md_mod.get_metric("GY_VA_GROWTH")
    ent = next(e for e in idx if e["kind"] == "metric" and e["id"] == "GY_VA_GROWTH")
    check("口径原样搬运，不是改写", ent["quote"] == meta_gy["definition"], "")

    dec = [e for e in idx if e["kind"] == "decision" and e["id"].startswith(mk["id"])]
    check("议定事项单独成条", len(dec) == 1, f"实际 {len(dec)}")
    if dec:
        check("议定条目带原文引句", bool(dec[0]["quote"]), "")
        check("引句逐字出现在会议原文中", dec[0]["quote"] in text, dec[0]["quote"])

    # 检索：俗称、短词、无关问题
    check("俗称能命中（环保局 → 生态环境局）", bool(kb.search("环保局那件事", mayor)), "")
    check("全称也能命中", bool(kb.search("生态环境局", mayor)), "")
    check("短查询仍能命中（供暖 只有一个二字组）", bool(kb.search("供暖", mayor)), "")
    check("只对上一个通用词不算命中", kb.answer("火星基地建设情况", mayor)["refused"], "")
    check("无关问题不硬凑", kb.answer("今天晚上吃什么", mayor)["refused"], "")

    # 身份收窄：住建局那件只在市长眼里，局长看不到
    ids_bureau = {e["id"] for e in kb.index(bureau) if e["kind"] == "oversight"}
    check("知识库按身份收窄", b["item_id"] not in ids_bureau, f"局长索引里有 {b['item_id']}")
    check("本单位事项在自己索引里", a["item_id"] in ids_bureau, f"局长索引 {sorted(ids_bureau)[:4]}")

    # 回放裁剪
    hit = kb.search("入河排污口", mayor, 1) or kb.search("河道巡查", mayor, 1)
    check("构造的会可被检索到", bool(kb.search("河道巡查", mayor)), "")
    if hit:
        meta = kb.to_meta(hit)
        check("回放裁剪去掉 score", "score" not in meta[0], str(sorted(meta[0])))
        check("回放保留跳转与引句", bool(meta[0].get("ref")) and "quote" in meta[0], str(sorted(meta[0])))

    # 问数分支：什么话术算「问事项」
    check("事项类话术被识别", ask._wants_matter("环保局那件事办到哪了"), "")
    check("数值类话术不被误判为事项", not ask._wants_matter("规上工业增加值是多少"), "")
    check("口径类话术不被误判为事项", not ask._wants_matter("空气质量优良天数比例怎么算"), "")

    # 真跑一次，模型关掉走规则：问的是事项，绝不能答出一个数值
    orig = llm_mod.available
    llm_mod.available = lambda: False
    try:
        r = ask.ask("河道巡查那件事办到哪了", person_id="mayor")
    finally:
        llm_mod.available = orig
    check("事项类问题走知识库", r.get("intent") == "knowledge", str(r.get("intent")))
    check("事项类问题不返回数值", not (r.get("facts") or []), str(r.get("facts"))[:80])
    check("知识库答案带条目", bool(r.get("knowledge")), "")

    # 前端静态约定：类名统一 kb- 前缀（.brand 撞过顶栏 display:flex，代价是一次截图）
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("前端有知识库渲染函数", "function kbBlock(" in html, "")
    check("前端回放带上知识库条目", "knowledge: meta.knowledge" in html, "")
    check("知识库卡片样式存在", ".kb-item{" in html and ".kb-kind{" in html, "")
    check("知识库类名不与既有选择器同名", ".kb-item{" in html and not re.search(r"^\.item\{", html, re.M), "")
    check("知识库卡片可跳回出处", "data-kbref" in html, "")
    # showOvItem 只渲染不打开弹窗，用它会出现「点了没反应」
    check("跳转用 openOversight 而非 showOvItem", 'return openOversight(id)' in html, "")


def test_notify() -> None:
    """消息提醒：只搬运、按天去重、发送失败不影响站内信。

    最要紧的两条：
    - 每条提醒都能顺着 ref 回到台账 / 会议 / 指标。回不去就是第二份真相；
    - 外发闸门（静默时段 / 重复批次 / 无通道）真的拦得住，且只发紧急与重要。
      闸门失灵比不发更糟：群里一天被刷十条，第二周所有人都会把机器人屏蔽掉。
    """
    print("[16] 消息提醒")
    import notify

    now = ov._now()
    view = notify.build("mayor", now=now)
    check(
        "提醒带完整结构",
        all(k in view for k in ("notes", "groups", "rules", "channels", "unread", "next", "basis")),
        str(list(view))[:90],
    )
    check("提醒带规则表", len(view["rules"]) >= 6, str(len(view["rules"])))
    check("提醒有内容可测", view["total"] > 0, "种子台账里应至少有一条逾期或临期")

    # 提醒是派生视图：生成一百次也不能改台账。改了就成了第二个写入点。
    before = {i["id"]: (i["status"], i["progress"]) for i in ov.list_items()}
    for _ in range(3):
        notify.build("mayor", now=now)
    after = {i["id"]: (i["status"], i["progress"]) for i in ov.list_items()}
    check("生成提醒不改台账", before == after, "")

    orphan = []
    for n in view["notes"]:
        r = n["ref"]
        if r["type"] == "oversight" and ov.detail(r["id"]) is None:
            orphan.append(n["id"])
        elif r["type"] == "meeting" and mt_ledger.get(r["id"]) is None:
            orphan.append(n["id"])
        elif r["type"] == "metric" and r["id"] not in METRICS:
            orphan.append(n["id"])
    check("每条提醒都能追回出处", not orphan, str(orphan)[:90])

    # 一件事一天只能出现一次：逾期和停滞会同时命中，拆成两条用户得自己对编号
    ov_ids = [n["ref"]["id"] for n in view["notes"] if n["ref"]["type"] == "oversight"]
    check("同一事项只提醒一次", len(ov_ids) == len(set(ov_ids)), str(ov_ids)[:90])

    # id 里带日期桶：同一时刻稳定，跨天重来。这就是「每天提醒一次」的实现方式。
    again = notify.build("mayor", now=now)
    check("同一时刻 id 稳定", [n["id"] for n in again["notes"]] == [n["id"] for n in view["notes"]], "")
    tomorrow = notify.build("mayor", now=now + timedelta(days=1))
    check(
        "跨天重新提醒",
        bool({n["id"] for n in tomorrow["notes"]} - {n["id"] for n in view["notes"]}),
        "第二天没有新 id，说明去重桶没跟着日期走",
    )

    bureau = notify.build("bureau", now=now)
    wrong = [
        n["ref"]["id"]
        for n in bureau["notes"]
        if n["ref"]["type"] == "oversight" and (ov.detail(n["ref"]["id"]) or {}).get("assignee") != "市生态环境局"
    ]
    check("局长只收到本单位事项", not wrong, str(wrong)[:70])

    if view["notes"]:
        target = view["notes"][0]["id"]
        r = notify.mark_read("mayor", [target])
        check("标已读成功", r.get("ok") is True, str(r)[:70])
        v2 = notify.build("mayor", now=now)
        check("已读后未读数减一", v2["unread"] == view["unread"] - 1, f"{view['unread']} -> {v2['unread']}")
        check("已读不减少提醒条数", v2["total"] == view["total"], f"{view['total']} -> {v2['total']}")
    check("空 id 不等于全部已读", notify.mark_read("mayor", []).get("ok") is False, "")

    # ── 外发 ──────────────────────────────────────────────────────────
    # 静默时段（默认 22:00–07:00）会让所有外发用例变成 quiet_hours，
    # 用真实时钟跑这套测试，晚上 10 点后必然全红。外发用例固定到当天 10:00，
    # 静默时段另用 23:10 单独测 —— 两件事由两段代码各自负责。
    work = now.replace(hour=10, minute=0, second=0, microsecond=0)

    os.environ.pop("OCGOV_NOTIFY_WEBHOOK", None)
    no_chan = notify.dispatch(now=work)
    check("未配通道只记账不报错", no_chan.get("ok") is True and no_chan.get("skipped") == "no_channel", str(no_chan)[:80])

    os.environ["OCGOV_NOTIFY_WEBHOOK"] = "https://qyapi.example.com/cgi-bin/webhook/send?key=SECRET-KEY-1234"
    try:
        quiet = notify.dispatch(now=now.replace(hour=23, minute=10))
        check("静默时段不外发", quiet.get("skipped") == "quiet_hours", str(quiet)[:70])

        pool: list[dict] = []

        def fake_hook(cfg, text, items):
            pool.extend(items)
            return True, ""

        sent = notify.dispatch(now=work, send_hook=fake_hook)
        check("配通道后能发出", sent.get("sent", 0) > 0 and sent.get("delivered") == ["webhook"], str(sent)[:80])
        check("只外发紧急与重要", all(n["level"] in notify._EXTERNAL_LEVELS for n in pool), str([n["level"] for n in pool]))
        check("外发只含督办类", all(n["kind"] == "oversight" for n in pool), str({n["kind"] for n in pool}))
        check("外发正文带台账编号", "台账编号" in (sent.get("text") or ""), "")
        check("外发正文不冒充模型结论", "未经模型改写" in (sent.get("text") or ""), "")

        dup = notify.dispatch(now=work, send_hook=fake_hook)
        check("同批次不重发", dup.get("skipped") == "duplicate", str(dup)[:70])

        def bad_hook(cfg, text, items):
            return False, "HTTP 403 Forbidden"

        failed = notify.dispatch(now=work, send_hook=bad_hook, force=True)
        check("发送失败如实返回", failed.get("ok") is False and bool(failed.get("failed")), str(failed)[:80])
        st = notify.channels_status()
        check("失败原因记进通道状态", st["webhook"]["ok"] is False and "403" in st["webhook"]["error"], str(st["webhook"])[:90])
        check("webhook 地址打码", "SECRET-KEY-1234" not in st["webhook"]["target"], st["webhook"]["target"])
        check("群消息按企微格式", notify.config()["channels"]["webhook"]["format"] == "wecom", "")

        # 发送失败不能连带站内信：提醒还在面板里，这是两件事
        still = notify.build("mayor", now=now)
        check("外发失败不影响站内信", still["total"] == view["total"], f"{view['total']} -> {still['total']}")
        check("未配/失败时如实标注送达", still["channels"]["mode"] == "external", still["channels"]["mode"])
    finally:
        os.environ.pop("OCGOV_NOTIFY_WEBHOOK", None)

    # 规则表上写的节奏必须和代码里的分支对得上：文案和实现分家比没有文案更糟
    src = (ROOT / "core" / "notify.py").read_text(encoding="utf-8")
    missing = [r["id"] for r in notify.RULES if src.count(f'"{r["id"]}"') < 2]
    check("每条规则都有生成分支", not missing, str(missing))

    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("顶栏铃铛接上面板", 'tool.dataset.tool === "bell") return openNtModal' in html, "")
    check("提醒面板有规则表", "ntRuleBody" in html and "什么时候提醒什么" in html, "")
    check("未配通道如实标注", "未配通道" in html, "")
    check("提醒定时轮询", "setInterval" in html and "loadNotify()" in html, "")
    check("提醒类名统一 nt- 前缀", ".nt-item{" in html and not re.search(r"^\.item\{", html, re.M), "")


def test_saved() -> None:
    """收藏问答：收的是原文那条消息，按人存，删会话要连收藏一起清。

    最要紧的一条是「只认 message_id」：接口拒收前端传的正文，正文一律回 chats 取。
    否则收藏夹可以塞进任何一句话，"我查过这个数"就成了自说自话。
    """
    print("[17] 收藏问答")
    import saved as sv

    # 必须带 owner：收藏会按归属取会话，没主的老会话在真实服务里早被 own_orphans
    # 迁走了，测试若建一条无主会话，验的就不是线上那条路径。
    th = chat.create("smoke-saved", owner="mayor")
    chat.append(th["id"], "user", "全市 GDP 是多少？")
    msg = chat.append(
        th["id"],
        "assistant",
        "2026年1—9月累计，全市地区生产总值为 1,286.5 亿元。",
        meta={"metric_codes": ["GDP_TOTAL"]},
    )
    sv.remove("mayor", msg["id"])

    r = sv.add("mayor", th["id"], msg["id"])
    check("能收藏一条回答", r.get("ok") is True and r.get("count") == 1, str(r)[:90])
    item = r.get("item") or {}
    check("收藏带提问原文", str(item.get("question", "")).startswith("全市 GDP"), str(item.get("question")))
    check("收藏带回答原文", "1,286.5" in str(item.get("headline")), str(item.get("headline"))[:60])
    check("收藏记下指标代码", item.get("codes") == ["GDP_TOTAL"], str(item.get("codes")))
    check("重复收藏幂等", sv.add("mayor", th["id"], msg["id"]).get("duplicate") is True, "")
    check("收藏按人隔离", len(sv.list_of("bureau")) == 0, str(sv.list_of("bureau"))[:60])

    # 这三条是「不收前端内容」的另一面：只认会话里真有的东西
    check("拒绝不存在的消息", sv.add("mayor", th["id"], "m_fake")["ok"] is False, "")
    check("拒绝不存在的会话", sv.add("mayor", "th_fake", msg["id"])["ok"] is False, "")
    ubad = chat.append(th["id"], "user", "再问一句")
    check("不能收藏提问", sv.add("mayor", th["id"], ubad["id"])["ok"] is False, "")

    # 收藏写的是自己名下，但会话可能不是自己的。不核归属的话，拿到别人的
    # thread_id + message_id 就能把对方的回答收进自己收藏夹 —— 会话层已经一律
    # 404 了，这里敞着就是缺口。用返回文案区分：没核 owner 时命中的是「这条回答
    # 不在该会话里」，核了之后直接当会话不存在。不需要真造一条别人的回答。
    stolen = sv.add("vmayor", th["id"], msg["id"])
    check(
        "不能收藏别人会话里的回答",
        stolen.get("ok") is False and "会话不存在" in str(stolen.get("error")),
        str(stolen)[:80],
    )

    ws = wb.workspace("mayor")
    check(
        "工作台带出收藏",
        ws["saved"]["count"] == 1 and ws["saved"]["items"][0]["id"] == msg["id"],
        str(ws["saved"])[:90],
    )
    check("收藏上限随数据下发", ws["saved"]["max"] == sv.MAX_SAVED, str(ws["saved"].get("max")))

    rm = sv.remove("mayor", msg["id"])
    check("能取消收藏", rm.get("ok") is True and rm.get("count") == 0 and rm.get("removed") == 1, str(rm)[:70])
    check("重复取消不报错", sv.remove("mayor", msg["id"]).get("removed") == 0, "")

    # 删会话必须连收藏一起清：只靠 list_of 读时过滤，死条目会一直躺在存储里
    sv.add("mayor", th["id"], msg["id"])
    n = sv.purge_thread(th["id"])
    check("删会话清掉指向它的收藏", n == 1 and sv.list_of("mayor") == [], f"purged={n}")

    # 上限：造 MAX+1 条消息，最后一条必须被拦下
    th2 = chat.create("smoke-saved-max", owner="mayor")
    ids = [chat.append(th2["id"], "assistant", f"第 {i} 条答案")["id"] for i in range(sv.MAX_SAVED + 1)]
    th2 = chat.get(th2["id"]) or th2
    for mid in ids[: sv.MAX_SAVED]:
        sv.add("mayor", th2["id"], mid)
    over = sv.add("mayor", th2["id"], ids[-1])
    check("收藏有上限", over.get("ok") is False and str(sv.MAX_SAVED) in str(over.get("error")), str(over)[:70])
    sv.purge_thread(th2["id"])
    chat.delete(th["id"])
    chat.delete(th2["id"])
    check("清理后收藏为空", sv.list_of("mayor") == [], str(sv.list_of("mayor"))[:60])

    # 前端静态约定：星号挂在答案上，且只在拿到后端消息 id 之后才画
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("答案上有收藏星号", 'class="star-btn' in html and "data-star=" in html, "")
    check("星号用落库 id 定位", "messageId: f.message_id" in html and "data-mid=" in html, "")
    # 收藏并进「我的关注」网格：以前是卡片下面一个虚线小块（savedBox），看着像另一个
    # 板块，人不会认为那是自己关注的东西。现在和指标卡同在 #watchGrid 里，
    # 卡片自己带 data-svthread（点开回原答案）。
    check(
        "收藏卡并进我的关注网格",
        'class="kpi sv"' in html and "data-svthread=" in html and 'id="savedBox"' not in html,
        "",
    )
    check("收藏卡上有取消收藏", 'class="rm" data-unsave=' in html, "")
    check("收藏能跳回原回答", "function jumpToSaved" in html and "data-svmid=" in html, "")
    # 跳转必须先把问数面板切出来。问数长在「城市罗盘」那一屏，从工作台点收藏卡
    # 不切视图的话，答案确实渲染好了，但人还停在个人工作台上 —— 看着像点了没反应
    jump = html.split("async function jumpToSaved")[1].split("async function")[0]
    check("收藏跳转先切回问答视图", 'switchView("compass")' in jump, "")
    # 指标与收藏各自一个计数（`${total} / ${max} 项` 和 `${saved.length} 个`），
    # 不并成「7 / 8」—— 指标名额 8 个、收藏 24 条，混在一个分母里会让收藏占指标名额
    check("收藏单独计数", '`${saved.length} 个`' in html and 'id="watchQCount"' in html, "")
    check("空收藏给提示、不画卡", "还没有收藏的问题" in html and "innerHTML = saved.length" in html, "")


def test_meetplan() -> None:
    """对话里约会议：解析对、缺项说清、确认才落库、重复点只建一场。

    这里大部分断言是在守「不确定要说出来」这件事：时间怎么算的、主持为什么是
    这个人、哪个单位没对上清单，都得写在草稿上。悄悄按默认值走一遍，用户看到的
    就是一场他自己没同意过的会。
    """
    print("\n[18] 对话里约会议")
    import meetplan as mp
    import schedule as cal
    import notify as nt

    # 意图：这两类句子长得像，一个是要建会，一个是查记录。判错的方向不对称 ——
    # 把「查记录」判成建会，会凭空多出一场会；连卡都不用点，草稿就摆在那了。
    for q in ("下周三下午三点约个会", "安排一场会讨论供暖", "开个会，议题是停车难"):
        check(f"识别为约会议：{q}", mp.wants(q) is True, "")
    for q in ("上次开会定了什么", "下周有什么会", "会议助手怎么用", "会议时间冲突怎么办"):
        check(f"不误判：{q}", mp.wants(q) is False, "")

    # 固定基准日：2026-11-02 是周一。用真实今天会让「下周三」随运行日期漂。
    today = date(2026, 11, 2)
    d = mp.parse(
        "下周三下午三点约个会，议题是冬季供暖保障，市住建局和市城管局参加，地点在3号楼会议室",
        today=today, person={"post": "市长"},
    )
    check("解析出时间", d["held_at"] == "2026-11-11 15:00", d["held_at"])
    check("解析出议题", d["title"] == "冬季供暖保障", d["title"])
    check("解析出地点", d["place"] == "3号楼会议室", d["place"])
    check("单位别名对齐全称", d["attendees"] == ["市住建局", "市城管局"], str(d["attendees"]))
    check("必填齐全才允许建会", d["ready"] is True, str(d["missing"]))
    check("时间算法写出来", "2026-11-11" in (d["when_why"] or ""), d["when_why"])

    d2 = mp.parse("明天下午四点开个会", today=today, person={"post": "市长"})
    check("缺议题要标出来", "title" in d2["missing"], str(d2["missing"]))
    check("缺参会单位要标出来", "attendees" in d2["missing"], str(d2["missing"]))
    check("主持按当前身份并说明理由",
          d2["host"] == "市长" and any("按当前身份" in a for a in d2["assumptions"]),
          str(d2["assumptions"]))

    d3 = mp.parse("三点开个会，讨论测试", today=today, person={"post": "市长"})
    check("没说上下午按下午算且写明", d3["held_at"].endswith("15:00")
          and any("按下午" in a for a in d3["assumptions"]), d3["held_at"])

    # 单位不在台账里：原样留着并说明，不硬塞进清单 ——
    # 系统不该假装认识一个它从来没有的单位
    d4 = mp.parse("约个会，参会单位：市财政局", today=today, person={"post": "市长"})
    check("台账没有的单位不硬塞", d4["unknown_units"] == ["市财政局"]
          and "市财政局" not in d4["attendees"], str(d4["attendees"]) + str(d4["unknown_units"]))
    check("没对上的单位要提示", any("台账里没出现过" in n for n in d4["notes"]), str(d4["notes"])[:80])

    # ---- 落库 ----
    from datetime import datetime as _dt, timedelta as _td
    soon = (_dt.now() + _td(minutes=30)).strftime("%Y-%m-%d %H:%M")
    good = {"title": "供暖保障专题会", "held_at": soon, "place": "3号楼会议室",
            "host": "市长", "attendees": ["市住建局", "市城管局"], "topics": ["供暖保障"]}
    r1 = mp.commit(good, thread_id="th_smoke", message_id="m_smoke", actor="市长")
    mid = (r1.get("meeting") or {}).get("id") or ""
    check("确认后落库", r1.get("ok") is True and bool(mid), str(r1)[:90])
    one = mt_ledger.get(mid) or {}
    check("预约的会是 plan 类型", one.get("kind") == "plan", str(one.get("kind")))
    check("待召开的会状态是 planned", one.get("status") == "planned", str(one.get("status")))
    check("待召开的会没有纪要正文", one.get("text") == "", repr(one.get("text"))[:40])
    check("落库记下来源消息", one.get("from_message") == "th_smoke:m_smoke", str(one.get("from_message")))
    check("落库记下地点与主持", one.get("place") == "3号楼会议室" and one.get("host") == "市长",
          f"{one.get('place')} / {one.get('host')}")

    r2 = mp.commit(good, thread_id="th_smoke", message_id="m_smoke", actor="市长")
    check("同一条消息只建一场会",
          r2.get("duplicate") is True and (r2.get("meeting") or {}).get("id") == mid, str(r2)[:90])

    check("缺议题拒绝建会", mp.commit({"held_at": soon}, thread_id="a", message_id="b").get("ok") is False, "")
    check("缺时间拒绝建会", mp.commit({"title": "x"}, thread_id="c", message_id="d").get("ok") is False, "")

    # 没开过的会没有原文，这时候「解析」只能解析出模型编的东西
    refused = mt_ledger.save_parse(mid, {"items": []})
    check("待召开的会拒绝解析议定事项", refused.get("ok") is False, str(refused)[:90])

    # ---- 自动进日程和提醒 ----
    # 两场会：一场两天后（看日程文案），一场半小时后（看会前提醒）。
    # 用同一场会同时验两件事验不了 —— 半小时内的会，日程文案一定是「N 分钟后开始」，
    # 那不是 bug，是这件事本来的样子。
    later = ((_dt.now() + _td(days=2)).replace(hour=15, minute=0, second=0, microsecond=0)
             .strftime("%Y-%m-%d %H:%M"))
    r3 = mp.commit({"title": "两天后的专题会", "held_at": later, "host": "市长",
                    "attendees": ["市住建局"], "place": "常务会议室"},
                   thread_id="th_smoke", message_id="m_later", actor="市长")
    mid2 = (r3.get("meeting") or {}).get("id") or ""

    evs = [e for g in cal.build("mayor")["groups"] for e in g["items"]
           if e["kind"] == "meeting" and (e.get("ref") or {}).get("id") == mid2]
    check("预约的会进日程", len(evs) == 1, str(len(evs)))
    if evs:
        check("日程里写「待召开」", "待召开" in (evs[0].get("why") or ""), evs[0].get("why"))
        check("日程带上地点", "常务会议室" in (evs[0].get("sub") or ""), evs[0].get("sub"))

    notes = [n for n in nt.build("mayor")["notes"] if n["rule"] == "meet_soon" and mid in n["id"]]
    check("会前提醒自动挂上", len(notes) == 1, str([n["id"] for n in nt.build("mayor")["notes"]]))

    # ---- 回放与会话 ----
    meta = mp.to_meta(d) or {}
    check("回放裁剪掉单位清单", "unit_options" not in meta, str(sorted(meta))[:80])
    check("回放保留推定依据", bool(meta.get("assumptions")), str(meta.get("assumptions"))[:60])

    msgs = [{"id": "m_smoke", "meta": {"meet_draft": dict(meta)}}]
    mp.annotate("th_smoke", msgs)
    check("回放补上「已建会」",
          msgs[0]["meta"]["meet_draft"].get("meeting_id") == mid,
          str(msgs[0]["meta"]["meet_draft"].get("meeting_id")))

    import ask as ask_mod
    res = ask_mod.ask("下周三下午三点约个会，议题是接口测试，住建局参加", person_id="mayor")
    check("对话命中约会议意图", res.get("intent") == "meet" and bool(res.get("meet_draft")),
          str(res.get("intent")))
    check("约会议不调模型",
          all(t["tool"] != "llm.plan" for t in (res.get("trace") or [])),
          str([t["tool"] for t in (res.get("trace") or [])]))

    mt_ledger.delete(mid)
    if mid2:
        mt_ledger.delete(mid2)


def test_attach_entry() -> None:
    """附件：从「选文件」到「真进 prompt」的整条链路。

    这里要卡住两种回退：
    1. 又变回断头路（能选能显示、发送被拦住）—— 用户以为传了材料，其实没有；
    2. 越界变成数据源（材料里的数字被当成指标值）—— 那会把「每个数都能
       追到出处」这条底线拆掉。所以断言里既有「正文进了 payload」，
       也有「服务端只收清单、不收正文」。
    """
    print("\n[17.5] 附件")
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("输入区有附件按钮与文件选择", 'id="attachBtn"' in html and 'id="attachInput"' in html, "")
    check("附件只收纯文本白名单", "const TEXT_EXT" in html and '".pdf": "PDF 要解析库"' in html, "")
    check("不再有「附件没接进问数」的拦门", "附件还没接进问数" not in html, "")
    check("前端没有偷偷上传", "FormData" not in html, "")
    check("读文件走 FileReader", "readAsArrayBuffer" in html, "")
    # GBK：政务 csv 十有八九是 GBK，按 utf-8 硬读会得到满屏乱码
    check("按字节探测编码并支持 GBK", "TextDecoder" in html and '"gbk"' in html, "")
    check("附件随提问发出", "attachments: atts," in html, "")
    check("发送后清空附件芯片", "ATT.files = []; renderAttach();" in html, "")
    check("附件正文不进历史回放", "attachments: meta.attachments || []" in html, "")

    # 服务端校验：不信前端，自己再截一遍
    import main as web_main  # noqa: E402  （app/main.py 里的模块级函数）

    got = web_main._attachments({
        "attachments": [
            {"name": "a.csv", "text": "x" * 50},
            {"name": "b.csv", "text": ""},              # 空正文：扔掉
            {"name": "c.csv"},                          # 没有 text：扔掉
            "not-a-dict",                               # 垃圾：扔掉
        ]
    })
    check("服务端丢掉空/畸形附件", len(got) == 1 and got[0]["name"] == "a.csv", str(got)[:60])

    long_one = web_main._attachments({"attachments": [{"name": "big.txt", "text": "y" * 99999}]})
    check("单份材料按上限截断并标记", long_one[0]["chars"] == web_main._MAX_ATT_EACH and long_one[0]["truncated"], str(long_one[0]["chars"]))

    many = web_main._attachments({"attachments": [{"name": f"f{i}.txt", "text": "z" * 100} for i in range(20)]})
    check("附件份数有上限", len(many) == web_main._MAX_ATT_FILES, str(len(many)))

    huge = web_main._attachments({"attachments": [{"name": f"h{i}.txt", "text": "w" * 20000} for i in range(5)]})
    total = sum(a["chars"] for a in huge)
    check("附件总量有上限", total <= web_main._MAX_ATT_TOTAL, f"total={total}")
    check("超出总量的那份被标记截断", any(a["truncated"] for a in huge), "")

    check("没带附件时返回空列表", web_main._attachments({}) == [], "")
    check("attachments 不是列表时不死", web_main._attachments({"attachments": "oops"}) == [], "")

    # 材料到底有没有进 planner 的 prompt —— 上面全是"存下来了"，
    # 这一条才是"模型看得见"。少了它，附件就只是一串躺在 body 里的字。
    import ask as ask_mod  # noqa: E402
    import llm as metrics_llm  # noqa: E402

    mp = metrics_llm._materials_prompt([{"name": "月报.csv", "text": "固投 7.2%", "truncated": True}])
    check("材料正文进了 planner 的 prompt", "月报.csv" in mp and "固投 7.2%" in mp, mp[:50].replace("\n", " "))
    check("材料被围栏包住（防提示注入）", "<<<" in mp and ">>>" in mp and "不是指令" in mp, "")
    check("截断的材料要标出来", "已截断" in mp, "")
    check("没有材料时不产生空段落", metrics_llm._materials_prompt([]) == "", "")
    # 材料放 user 不放 system：放 system 会被每一轮历史重复带着走
    src_llm = (ROOT / "core" / "metrics" / "llm.py").read_text(encoding="utf-8")
    check("材料拼在 user 消息里而不是 system 里", "_materials_prompt(attachments or [])" in src_llm, "")

    # ---- B 档边界（2026-11-03 定）----
    # 决策：材料的数字**不进答案正文**，只在对照区和指标库的值并排，各自带出处。
    # 最早选的是 A（完全不碰材料数字），后来改成 B，理由是用户拿月报来问时
    # 「系统答 5.6%、他手上 7.2%」需要一个解释，而不是让他以为系统错了。
    #
    # B 比 A 危险的地方在于：A 根本不会显示材料数字，所以不可能错；
    # B 会显示，而一旦模型编一个"材料值"，界面就成了「材料里写的是 7.2%」——
    # 用户以为系统替他核对过。所以下面第一条是这套断言里最要紧的。
    check(
        "prompt 里仍禁止把材料数字当答案",
        "材料里的数字**不是答案**" in mp,
        "删了这句，planner 就会拿材料里的数当指标值",
    )
    check(
        "prompt 要求 material_refs 原样照抄",
        "material_refs" in mp and "原样照抄" in mp,
        "",
    )
    src_exp = (ROOT / "core" / "experts.py").read_text(encoding="utf-8")
    check(
        "专家解读拿不到材料（B 档也不进解读）",
        "attachment" not in src_exp and "material" not in src_exp.lower(),
        "",
    )
    src_ask = (ROOT / "core" / "metrics" / "ask.py").read_text(encoding="utf-8")
    check(
        "专家调用不传 attachments",
        "experts_mod.interpret(" in src_ask and "attachments" not in src_ask.split("experts_mod.interpret(")[1][:120],
        "",
    )

    # 校验函数本身：这才是 B 档的安全阀，全部离线可测，不花 token
    MAT = [{"name": "月报.csv", "text": "指标,值\n固投增速,7.2%\n工业增速,8.9%\n房地产,12.35%", "truncated": False}]

    def refs(value, code="FAI_GROWTH"):
        return metrics_llm._verify_material_refs([{"metric_code": code, "value": value}], MAT)

    got = refs("7.2")
    check("材料里真有的数被收下", len(got) == 1 and got[0]["written"] == "7.2%", str(got)[:70])
    check("收下时带出处文件与原文摘录", got and got[0]["file"] == "月报.csv" and "固投增速" in got[0]["excerpt"], "")
    check("★材料里没有的数被丢弃（防模型编）", refs("9.9") == [], "编一个 9.9 就能伪造『材料里写的』")
    check("★7.25 不能命中 7.2（不做子串匹配）", refs("7.25") == [], "子串匹配会让 7.2 命中 7.25")
    check("数值相等就算命中（7.20 vs 7.2）", len(refs("7.2")) == 1 and len(refs("7.20")) == 1, "")
    check("目录外的 code 丢弃", refs("7.2", code="NOT_A_METRIC") == [], "")
    check("单数字（易误命中）丢弃", refs("9") == [], "")
    check("written 用材料原文写法", (refs("7.20") or [{}])[0].get("written") == "7.2%", "模型抄成 7.20，展示的应是材料的 7.2%")

    # 对照组装：配不上宁可不配
    base = {
        "refused": False, "intent": "value", "metric_codes": ["FAI_GROWTH"],
        "facts": [{"label": "2026年1—9月累计", "value": "5.6%"}],
        "period_label": "2026年1—9月累计",
        "sources": [{"department": "市统计局", "report": "《固定资产投资统计月报》2026年9月"}],
    }
    rows = ask_mod._material_compare(got, base)
    check("对照配出两行所需字段", len(rows) == 1 and rows[0]["library_value"] == "5.6%" and rows[0]["same"] is False, str(rows)[:80])
    check("对照带库值口径与部门", rows and rows[0]["library_label"] and rows[0]["library_department"] == "市统计局", "")
    check("同值判 same=True", ask_mod._material_compare(got, dict(base, facts=[{"label": "x", "value": "7.2%"}]))[0]["same"] is True, "")
    check(
        "★另一个指标的数值不并入本行",
        ask_mod._material_compare([dict(got[0], code="OTHER")], base) == [],
        "错配比不显示更糟",
    )
    for it in ("compare", "breakdown", "definition"):
        check(f"{it} 场景不做对照（口径对不齐）", ask_mod._material_compare(got, dict(base, intent=it)) == [], "")
    check("拒答不做对照", ask_mod._material_compare(got, dict(base, refused=True)) == [], "")

    # 结构保证：材料数字没有路径进入结论正文
    check(
        "对照只在 ask() 里赋值，_ask_impl 的 headline 不碰它",
        'result["material_compare"] = cmp_rows' in src_ask
        and "material_compare" not in src_ask.split("def _ask_impl(")[1].split("def ask(")[0],
        "一旦在 _ask_impl 里拼进 headline，材料数字就进了答案正文",
    )

    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("气泡渲染对照块", 'class="mat-cmp"' in html and "mc-row" in html, "")
    check("导出带材料对照段", '"**材料对照**"' in html, "")
    check(
        "★历史回放与导出共用同一份字段映射（historyView）",
        "function historyView(" in html and "b.__m = aiMsgs[i] ? historyView(" in html,
        "挂原始消息会让刷新后的导出变成『完整但空』",
    )


def test_frontend_theme() -> None:
    """前端主题的静态约定。

    前端没有浏览器测试，但"深色主题漏定义变量"这类错误是纯文本可查的，
    而它的后果只在深色下露出一块白底 —— 最容易漏测，必须卡在提交前。
    """
    print("\n[14] 前端主题")
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r"<style>(.*?)</style>", html, re.S)
    check("页面含样式块", m is not None, "")
    body = m.group(1)

    def vars_of(sel: str) -> set[str]:
        block = re.search(re.escape(sel) + r"\s*\{(.*?)\}", body, re.S)
        return set(re.findall(r"(--[a-z0-9-]+)\s*:", block.group(1))) if block else set()

    root = vars_of(":root")
    dark = vars_of(':root[data-theme="dark"]')
    check(":root 定义了颜色变量", len(root) >= 20, str(len(root)))
    # --r 是圆角，和主题无关
    missing = sorted(root - dark - {"--r"})
    check("深色主题覆盖全部变量", not missing, "未覆盖: " + ", ".join(missing))

    # 只查背景类变量：文字色在深色下本来就该是亮的，一起查会误报
    dark_block = re.search(r':root\[data-theme="dark"\]\s*\{(.*?)\}', body, re.S)
    pairs = re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", dark_block.group(1) if dark_block else "")
    BGISH = ("bg", "card", "surface", "hover", "line", "thumb", "chip")
    bright = [
        f"{n}={v}"
        for n, v in pairs
        if any(k in n for k in BGISH) and sum(int(v[i : i + 2], 16) for i in (1, 3, 5)) > 480
    ]
    check("深色背景变量都是暗色", not bright, str(bright))

    # 防闪脚本要排在样式之前，否则深色用户每次刷新都会先白一下
    i_script = html.find('localStorage.getItem("ocgov.theme")')
    i_style = html.find("<style>")
    check("主题脚本在样式之前", 0 <= i_script < i_style, f"script={i_script} style={i_style}")

    # 强调色三套都要有深色版，否则深色下换色会失真
    for accent in ("teal", "red"):
        check(
            f"强调色 {accent} 有深色版",
            f':root[data-theme="dark"][data-accent="{accent}"]' in body,
            "",
        )

    # AI 日程：tone 必须带 tk- 前缀。`brand` 是顶栏 logo 的全局类（display:flex），
    # 写成 .cal-card.brand 会把卡片变成横向排列 —— 踩过一次，静态查得出来。
    check("日程弹窗存在", 'id="calModal"' in html, "")
    check("日程 tone 有防撞前缀", ".cal-card.tk-brand" in body and ".cal-card.brand{" not in body, "")
    check("日程按钮已接到弹窗", 'tool.dataset.tool === "cal"' in html, "")


def test_http(port: int | None = None) -> None:
    port = port or int(os.environ.get("OCGOV_PORT") or 8090)
    print(f"[15] HTTP 接口（127.0.0.1:{port}，未启动则跳过）")
    base = f"http://127.0.0.1:{port}"

    def get(path: str):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        health = get("/api/health")
    except (urllib.error.URLError, ConnectionError, OSError):
        print("  SKIP  服务未启动")
        return

    check("/api/health", health.get("ok") is True, str(health))
    check("健康检查报告模型状态", "planner" in health, str(health.keys()))

    ov = get("/api/overview")
    check("/api/overview", ov.get("ok") is True and len(ov.get("kpis") or []) == 4, "")

    # 民情焦点明细：罗盘卡只给标题/标签，焦点弹窗还要详情、工单、渠道、焦点词、走势。
    # 两个接口必须同源 —— 卡片上写的和点开看到的不是一件事，比没有这个弹窗更糟。
    fx = get("/api/focus")
    fitems = fx.get("items") or []
    card_items = (ov.get("focus") or {}).get("items") or []
    check("/api/focus", fx.get("ok") is True and len(fitems) == (ov.get("focus") or {}).get("count"), str(len(fitems)))
    check("焦点与罗盘同源", [i["title"] for i in fitems] == [i["title"] for i in card_items], "")
    need = ("id", "title", "tag", "tone", "detail", "tickets", "channels", "source", "people", "keywords", "trend")
    check("焦点带弹窗所需字段", all(all(k in i for k in need) for i in fitems), "")
    check("焦点走势是 14 天", all(len(i["trend"]) == 14 for i in fitems), "")
    check("焦点渠道有件数", all(i["channels"] and sum(n for _, n in i["channels"]) > 0 for i in fitems), "")

    ws = get("/api/workspace")
    check(
        "/api/workspace",
        ws.get("ok") is True and (ws.get("person") or {}).get("id") == "mayor" and len(ws.get("watch") or []) > 0,
        f"person={(ws.get('person') or {}).get('id')} 卡片={len(ws.get('watch') or [])}",
    )
    check("/api/workspace 带关注动态", len(ws.get("feed") or []) > 0, str(len(ws.get("feed") or [])))
    check("/api/workspace 带选择器目录", len(ws.get("catalog") or []) == len(METRICS), str(len(ws.get("catalog") or [])))

    wsv = get("/api/workspace?person=vmayor")
    check(
        "换身份换一组关注",
        [c["code"] for c in wsv["watch"]] != [c["code"] for c in ws["watch"]],
        f"{[c['code'] for c in wsv['watch']]}",
    )
    try:
        ppl = get("/api/people")
        check("/api/people 三个身份", len(ppl.get("people") or []) == 3 and ppl.get("demo") is True, str(ppl)[:80])
    except urllib.error.HTTPError as e:
        check("/api/people 三个身份", False, f"code={e.code}（服务跑的是旧代码？）")

    try:
        exs = get("/api/experts")
        import experts as ex_api

        roster = exs.get("experts") or []
        # 不把「恰好三个」写成契约：名单是可扩的，接口只要完整反映定义即可。
        check("/api/experts 返回全部专家", [e.get("id") for e in roster] == [e.get("id") for e in ex_api.EXPERTS], str(exs)[:80])
        # max 是「一次最多叫几位」，必须和 core 里的截断同源；两处各写一个数字就会
        # 出现「能选 3 个但后端只收 3 个以外」或反过来的鬼故事。
        check("max 与截断常量同源", exs.get("max") == ex_api.MAX_PER_CALL, f"max={exs.get('max')} const={ex_api.MAX_PER_CALL}")
        check("/api/experts 说明不报数", "不报数字" in (exs.get("rule") or ""), str(exs.get("rule")))
    except urllib.error.HTTPError as e:
        check("/api/experts 可读", False, f"code={e.code}")

    # 收藏接口只收 id：拿一个不存在的消息去收必须被拒（这条不落盘，可以天天跑；
    # 造真实收藏会写进真实 workspaces.json，清理不干净就留下"市长收藏了一条假回答"）
    try:
        req = urllib.request.Request(
            base + "/api/saved",
            data=json.dumps({"thread_id": "th_nope", "message_id": "m_nope"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            bad_saved = json.loads(r.read().decode("utf-8"))
        check("/api/saved 拒绝不存在的消息", bad_saved.get("ok") is False, str(bad_saved)[:80])
    except urllib.error.HTTPError as e:
        check("/api/saved 拒绝不存在的消息", e.code == 400, f"code={e.code}")

    detail = get("/api/metric/GDP_TOTAL")
    check("/api/metric/GDP_TOTAL", detail.get("ok") is True and detail.get("code") == "GDP_TOTAL", "")

    try:
        get("/api/metric/NOT_A_METRIC")
        check("未注册指标返回 404", False, "居然返回了 200")
    except urllib.error.HTTPError as e:
        check("未注册指标返回 404", e.code == 404, f"code={e.code}")

    # ---- 消息提醒 ----
    try:
        nt = get("/api/notify")
        check("/api/notify 返回提醒", nt.get("ok") is True and isinstance(nt.get("notes"), list), str(list(nt))[:80])
        check("/api/notify 带规则表", len(nt.get("rules") or []) >= 6, str(len(nt.get("rules") or [])))
        ch = nt.get("channels") or {}
        check("/api/notify 带通道状态", ch.get("mode") in ("local", "external"), str(ch.get("mode")))
        # 打码检查只看真实配置字段：template 里那个 key=… 是给人填的占位符
        check("不返回完整推送地址", "key=" not in ((ch.get("webhook") or {}).get("target") or ""), str((ch.get("webhook") or {}).get("target")))

        ntb = get("/api/notify?person=bureau")
        check("换身份换提醒范围", (ntb.get("basis") or "") != (nt.get("basis") or ""), f"{nt.get('basis')} / {ntb.get('basis')}")

        # 用「全部标已读」而不是「标一条、断言减一」：后者第二次跑就不成立
        # （那条早就是已读了），会变成一个只能在新环境通过的假测试。
        # 但这是打真实服务，写的是真实已读状态 —— 跑完必须还原，否则每跑一次
        # 提醒就多一层「读过」的假象，用户打开面板发现该看的都灰了。
        # HTTP 这半段打的是**另一个进程**，备份和还原必须落在那个进程的数据目录上。
        # 默认 ROOT/data（服务从仓库根起时就是这里）；服务是用 OCGOV_DATA 起的
        # 别的目录，就用 OCGOV_SERVER_DATA 指过来 —— 否则这两个操作打在另一个文件
        # 上：既还原不了被测服务的状态（断言必挂），又会误改本机正在用的数据。
        SERVER_DATA = Path(os.environ.get("OCGOV_SERVER_DATA") or (ROOT / "data"))
        ws_file = SERVER_DATA / "workspaces.json"
        ws_before = ws_file.read_bytes() if ws_file.exists() else None
        if ntb.get("notes"):
            req = urllib.request.Request(
                base + "/api/notify/read",
                data=json.dumps({"all": True}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Person-Id": "bureau"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                rd = json.loads(r.read().decode("utf-8"))
            check("提醒可全部标已读", rd.get("ok") is True and rd.get("unread") == 0, str(rd)[:90])
            check("全部已读不改提醒条数", rd.get("total") == ntb.get("total"), f"{ntb.get('total')} -> {rd.get('total')}")
        if ws_before is not None:
            ws_file.write_bytes(ws_before)
            again = get("/api/notify?person=bureau")
            check("已读状态跑完还原", again.get("unread") == ntb.get("unread"),
                  f"{ntb.get('unread')} -> {again.get('unread')}")

        req = urllib.request.Request(
            base + "/api/notify/dispatch",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            dp = json.loads(r.read().decode("utf-8"))
        check("巡检接口可用", dp.get("ok") is True and ("skipped" in dp or dp.get("sent", 0) >= 0), str(dp)[:90])
        # 这条走的是真实服务，用的是服务进程的时钟，改不了它的 now：
        # 夜里跑会命中默认静默时段（22:00–07:00），skipped 是 quiet_hours。
        # 两者都表示「一条都没发出去」，断言就问这件事，别绑死其中一个原因。
        check(
            "未配通道时巡检不假装发送",
            dp.get("sent", 0) == 0 and dp.get("skipped") in ("no_channel", "quiet_hours"),
            str(dp)[:90],
        )
    except urllib.error.HTTPError as e:
        check("/api/notify 可读", False, f"code={e.code}（服务跑的是旧代码？）")

    # ---- 对话里约会议 ----
    # 这是唯一一条会往真实台账里写东西的 HTTP 测试，所以建完必须删干净，
    # 并把「删掉了」也断言一次 —— 否则跑几次测试，会议列表里就多几场不存在的会。
    mtid, mttid = "", ""
    before = len(get("/api/meetings").get("items") or [])
    try:
        req = urllib.request.Request(
            base + "/api/ask",
            data=json.dumps({"question": "下周三下午三点约个会，议题是接口冒烟，住建局参加"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Person-Id": "mayor"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            mk = json.loads(r.read().decode("utf-8"))
        mttid = mk.get("thread_id") or ""
        check("POST /api/ask 命中约会议", mk.get("intent") == "meet", str(mk.get("intent")))
        check("约会议只出草稿不建会",
              len(get("/api/meetings").get("items") or []) == before,
              f"{before} -> {len(get('/api/meetings').get('items') or [])}")

        body = {
            "thread_id": mttid,
            "message_id": mk.get("message_id") or "",
            "draft": {"title": "接口冒烟用会", "held_at": "2030-01-08 15:00",
                      "place": "3号楼会议室", "host": "市长", "attendees": ["市住建局"]},
        }
        req = urllib.request.Request(
            base + "/api/meetplan", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Person-Id": "mayor"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = json.loads(r.read().decode("utf-8"))
        mtid = (ok.get("meeting") or {}).get("id") or ""
        check("POST /api/meetplan 建会", ok.get("ok") is True and bool(mtid), str(ok)[:90])
        check("建会后会议列表 +1", len(ok.get("items") or []) == before + 1, str(len(ok.get("items") or [])))

        with urllib.request.urlopen(urllib.request.Request(
            base + "/api/meetplan", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Person-Id": "mayor"}, method="POST",
        ), timeout=15) as r:
            dup = json.loads(r.read().decode("utf-8"))
        check("重复确认不会建第二场",
              dup.get("duplicate") is True and (dup.get("meeting") or {}).get("id") == mtid,
              str(dup)[:90])

        try:
            req = urllib.request.Request(
                base + "/api/meetplan",
                data=json.dumps({"draft": {"held_at": "2030-01-09 09:00"}}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            check("缺议题建会返回 400", False, "居然成功了")
        except urllib.error.HTTPError as e:
            check("缺议题建会返回 400", e.code == 400, f"code={e.code}")

        th = get(f"/api/threads/{mttid}")
        drafts = [(m.get("meta") or {}).get("meet_draft") for m in (th.get("thread") or {}).get("messages") or []]
        drafts = [x for x in drafts if x]
        check("回放带上已建会编号", bool(drafts) and drafts[0].get("meeting_id") == mtid,
              str(drafts[:1])[:90])
    except urllib.error.HTTPError as e:
        check("/api/meetplan 可用", False, f"code={e.code}（服务跑的是旧代码？）")
    finally:
        if mtid:
            try:
                with urllib.request.urlopen(urllib.request.Request(
                    f"{base}/api/meetings/{mtid}", method="DELETE"), timeout=10) as r:
                    r.read()
            except urllib.error.HTTPError:
                pass
        if mttid:
            try:
                with urllib.request.urlopen(urllib.request.Request(
                    f"{base}/api/threads/{mttid}", method="DELETE"), timeout=10) as r:
                    r.read()
            except urllib.error.HTTPError:
                pass
        check("约会议测试自清（会 + 会话）",
              len(get("/api/meetings").get("items") or []) == before
              and all(t.get("id") != mttid for t in get("/api/threads").get("items") or []),
              f"会议 {len(get('/api/meetings').get('items') or [])} vs {before}")

    models = get("/api/models")
    check("/api/models 有 resolved 列表", isinstance(models.get("resolved"), list), str(type(models.get("resolved"))))
    check(
        "provider 列表不含明文密钥",
        all("api_key" not in p for p in (models.get("providers") or [])),
        str([list(p.keys()) for p in (models.get("providers") or [])][:1]),
    )
    check("/api/models 有 ready 标志", "ready" in models, str(list(models.keys())))
    check("/api/threads 可读", isinstance(get("/api/threads").get("items"), list), "")

    # 建一个空会话再删掉，不触发模型调用
    req = urllib.request.Request(
        base + "/api/threads",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        created = json.loads(r.read().decode("utf-8"))
    tid = (created.get("thread") or {}).get("id")
    check("POST /api/threads 建会话", bool(tid), str(created)[:120])
    if tid:
        check("新建会话可读回", (get(f"/api/threads/{tid}").get("thread") or {}).get("id") == tid, "")
        dreq = urllib.request.Request(f"{base}/api/threads/{tid}", method="DELETE")
        with urllib.request.urlopen(dreq, timeout=10) as r:
            deleted = json.loads(r.read().decode("utf-8"))
        check("DELETE /api/threads 删除", deleted.get("ok") is True, str(deleted))

    # ---- 会话管理：改名 / 搜索 / 身份隔离 / 停止与续流 ----
    #
    # 这一组的重点不是"功能能用"，而是**串台不许发生**。演示身份是谁都能声称的
    # （见 main._person 的注释），所以会话列表一旦不按 owner 过滤，切到副市长就能
    # 看到市长的原话。这里把「看不到 / 改不了 / 删不了」都钉成断言：这三条任何一条
    # 回退，都是数据串台，不是体验问题。
    #
    # 全程零模型调用：只建空会话、改名、删除。跑测试不该产生 token 账单。
    def call(path: str, method: str = "GET", payload: dict | None = None, person: str | None = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        hdrs = {"Content-Type": "application/json"}
        if person:
            hdrs["X-Person-Id"] = person
        r = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    code, made = call("/api/threads", "POST", {}, person="mayor")
    own_tid = (made.get("thread") or {}).get("id")
    check("会话管理测试建会话", code == 200 and bool(own_tid), f"{code} {str(made)[:80]}")

    if own_tid:
        code, d = call(f"/api/threads/{own_tid}", "PATCH", {"title": ""}, person="mayor")
        check("改名空标题返回 400", code == 400 and d.get("error") == "empty_title", f"{code} {str(d)[:60]}")

        code, d = call("/api/threads/no-such-thread-9999", "PATCH", {"title": "x"}, person="mayor")
        check("改名不存在的会话返回 404", code == 404, f"{code}")

        # 回显必须是截断后的值。前端拿它直接改 DOM —— 回显全文会让标题先长后短，
        # 用户看到的是"刚改完，刷新一下又变了"。
        long_title = "一二三四五六七八九十" * 4
        code, d = call(f"/api/threads/{own_tid}", "PATCH", {"title": long_title}, person="mayor")
        check(
            "改名回显的是截断后的标题",
            code == 200 and d.get("title") == long_title[:chat.MAX_TITLE],
            f"{code} {str(d.get('title'))[:40]}",
        )

        # 搜索按标题命中。带 snippet 键（标题命中时值为 None），前端靠"有没有这个键"
        # 区分标题命中与正文命中两种展示。
        keyword = "冒烟搜索锚点"
        call(f"/api/threads/{own_tid}", "PATCH", {"title": keyword}, person="mayor")
        _, hits = call(f"/api/threads?q={urllib.parse.quote(keyword)}", person="mayor")
        hit_ids = [i.get("id") for i in hits.get("items") or []]
        check("搜索能按标题命中", own_tid in hit_ids, f"命中 {len(hit_ids)} 条")
        check(
            "标题命中带 snippet 键但不含正文片段",
            all("snippet" in i for i in (hits.get("items") or [])),
            str((hits.get("items") or [{}])[0].keys())[:60],
        )
        # 探针也必须编码：中文直接拼进请求行会让 http.client 在 encode('ascii') 上炸，
        # 报出来的是 UnicodeEncodeError，跟"搜索没结果"完全无关，极难认。
        _, blank = call(f"/api/threads?q={urllib.parse.quote('zzz绝不存在zzz')}", person="mayor")
        check("搜索无命中返回空列表", blank.get("items") == [], str(blank.get("items"))[:60])

        # ---- 跨身份 ----
        other = "vmayor"
        _, other_list = call("/api/threads", person=other)
        check(
            "别人的会话不出现在自己列表里",
            all(i.get("id") != own_tid for i in other_list.get("items") or []),
            f"{len(other_list.get('items') or [])} 条",
        )
        # 越权一律 404 而不是 403：403 等于承认"这个 id 存在"，
        # 换个 id 挨个试就能数出别人有几个会话。
        code, _ = call(f"/api/threads/{own_tid}", person=other)
        check("读别人的会话返回 404", code == 404, f"code={code}")
        code, _ = call(f"/api/threads/{own_tid}", "PATCH", {"title": "劫持"}, person=other)
        check("改不了别人的会话标题", code == 404, f"code={code}")
        code, _ = call(f"/api/threads/{own_tid}", "DELETE", person=other)
        check("删不了别人的会话", code == 404, f"code={code}")

        code, d = call(f"/api/threads/{own_tid}", "DELETE", person="mayor")
        check("会话管理测试自清", code == 200 and d.get("ok") is True, str(d)[:60])

    # ---- 停止与续流 ----
    # 只有错误分支卡得住契约，成功分支要真跑一轮模型（留给人点测，见 README）。
    code, d = call("/api/ask/cancel", "POST", {}, person="mayor")
    check("停止缺 turn_id 返回 400", code == 400 and d.get("error") == "turn_id_required", f"{code} {str(d)[:60]}")
    code, d = call("/api/ask/cancel", "POST", {"turn_id": "deadbeefdeadbeef"}, person="mayor")
    check("停止不存在的轮次返回 404", code == 404 and d.get("error") == "turn_not_found", f"{code} {str(d)[:60]}")
    code, d = call("/api/ask/turn/deadbeefdeadbeef", person="mayor")
    check("续流不存在的轮次返回 404", code == 404 and d.get("error") == "turn_not_found", f"{code} {str(d)[:60]}")
    # 带 Last-Event-ID 走标准头这条路：过期轮次也应干净 404，而不是解析炸成 500。
    req2 = urllib.request.Request(
        base + "/api/ask/turn/deadbeef?after=abc",
        headers={"Last-Event-ID": "deadbeef-7", "X-Person-Id": "mayor"},
    )
    try:
        with urllib.request.urlopen(req2, timeout=10) as r:
            ev = json.loads(r.read().decode("utf-8"))
        check("续流的错误参数不炸成 500", ev.get("ok") is not True, str(ev)[:60])
    except urllib.error.HTTPError as e:
        check("续流的错误参数不炸成 500", e.code == 404, f"code={e.code}")

    # ---- AI 日程 ----
    # 只测读 + 降级简报：brief 传 use_model=false，跑测试不该消耗一次模型调用。
    sched = get("/api/schedule?days=14")
    check("/api/schedule 返回日程", sched.get("ok") is True and isinstance(sched.get("groups"), list), str(list(sched.keys()))[:80])
    check(
        "/api/schedule 带窗口与口径",
        (sched.get("window") or {}).get("days") == 14 and bool(sched.get("basis")),
        str(sched.get("window")),
    )
    rows = [i for g in sched.get("groups") or [] for i in g.get("items") or []]
    stray = [i for i in rows if i.get("ref") and i["ref"].get("type") not in ("oversight", "meeting")]
    check("日程的跳转只有台账和会议两种", not stray, str(stray[:1]))
    check(
        "日程条数等于分组内条数",
        (sched.get("summary") or {}).get("total") == len(rows),
        f"{sched.get('summary')} vs {len(rows)}",
    )

    sched_b = get("/api/schedule?days=14&person=bureau")
    check(
        "日程按身份收窄",
        (sched_b.get("summary") or {}).get("total", 999) <= (sched.get("summary") or {}).get("total", 0),
        f"{sched_b.get('summary')} / {sched.get('summary')}",
    )
    check("日程写明分管口径", "分管单位" in (sched_b.get("basis") or ""), str(sched_b.get("basis")))

    req = urllib.request.Request(
        base + "/api/schedule/brief",
        data=json.dumps({"days": 14, "use_model": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        brief_res = json.loads(r.read().decode("utf-8"))
    check(
        "日程简报可降级",
        brief_res.get("ok") is True and brief_res.get("degraded") is True and brief_res.get("source") == "rule",
        str(brief_res)[:120],
    )

    # ---- 材料助手 ----
    bopt = get("/api/briefdoc/options")
    check(
        "/api/briefdoc/options 返回骨架与期次",
        bopt.get("ok") is True and len(bopt.get("presets") or []) == 3 and bool(bopt.get("periods")),
        str(list(bopt.keys()))[:60],
    )

    req = urllib.request.Request(
        base + "/api/briefdoc",
        data=json.dumps({"kind": "bulletin", "use_model": False, "person": "bureau"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        bdoc = json.loads(r.read().decode("utf-8"))
    check(
        "POST /api/briefdoc 按身份收窄",
        bdoc.get("ok") is True and "含协办" in (bdoc.get("meta", {}).get("scope_text") or ""),
        str(bdoc.get("meta", {}).get("scope_text")),
    )
    check("材料的导出 Markdown 可用", str(bdoc.get("markdown") or "").startswith("# "), "")
    check(
        "HTTP 入口同样不给模型写数",
        (bdoc.get("model") or {}).get("source") == "rule",
        str(bdoc.get("model")),
    )

    # ---- 知识库 ----
    kres = get("/api/knowledge?q=%E5%85%A5%E6%B2%B3%E6%8E%92%E6%B1%A1%E5%8F%A3")
    check(
        "/api/knowledge 返回条目",
        kres.get("ok") is True and bool(kres.get("hits")),
        str(kres.get("total")),
    )
    check(
        "知识库条目带出处与跳转",
        all((h.get("source") or {}).get("report") and h.get("ref") for h in (kres.get("hits") or [])),
        "",
    )
    check("知识库报告索引规模", int(kres.get("index_size") or 0) >= len(METRICS), str(kres.get("index_size")))
    empty = get("/api/knowledge?q=%E7%81%AB%E6%98%9F%E5%9F%BA%E5%9C%B0%E5%BB%BA%E8%AE%BE")
    check("/api/knowledge 无命中返回空而不是硬凑", empty.get("ok") is True and not empty.get("hits"), str(empty.get("total")))

    # 下面这条断言依赖「库里至少有一条会议记录」。但 data/ 不入库，CI 和新人
    # clone 下来都是空的 —— 以前它靠本机残留的演示数据才通过，一旦换到干净环境
    # 就红。测试要什么数据就自己造，测完自己清，别指望种子。
    fixture_mid = ""
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                base + "/api/meetings",
                data=json.dumps(
                    {
                        "title": "冒烟测试临时会议",
                        "text": "会议决定：建一条临时议题用于验证知识库分支，测试结束即删除。",
                        "held_at": "2026-11-02",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=30,
        ) as r:
            fixture_mid = ((json.loads(r.read().decode("utf-8")).get("meeting") or {}).get("id")) or ""
    except (urllib.error.URLError, OSError):
        fixture_mid = ""

    # 问数接口：事项类问题由知识库接住，且不掺数值
    req = urllib.request.Request(
        base + "/api/ask",
        data=json.dumps({"question": "上次开会定了什么", "person": "mayor"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        kask = json.loads(r.read().decode("utf-8"))
    check("POST /api/ask 事项问题走知识库", kask.get("intent") == "knowledge", str(kask.get("intent")))
    check("知识库答案不带数值", not (kask.get("facts") or []), str(kask.get("facts"))[:60])
    # 这一问会在真实 data/chats.json 里建一个会话。以前没删，跑 N 次测试就攒下 N 条
    # 同名会话（「上次开会定了什么」）—— 脏数据是测试自己造的，测试就该自己清掉。
    ktid = kask.get("thread_id")
    if ktid:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{base}/api/threads/{ktid}", method="DELETE"), timeout=10
            ) as r:
                r.read()
            left = [t for t in (get("/api/threads").get("items") or []) if t.get("id") == ktid]
            check("问数测试自清会话", not left, f"仍在：{ktid}")
        except urllib.error.HTTPError as e:
            check("问数测试自清会话", False, f"code={e.code}")

    # 清掉为这条断言造的临时会议：会议有删除接口，能干净收场
    if fixture_mid:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{base}/api/meetings/{fixture_mid}", method="DELETE"), timeout=10
            ) as r:
                r.read()
            still = [m for m in (get("/api/meetings").get("items") or []) if m.get("id") == fixture_mid]
            check("问数测试自清临时会议", not still, f"仍在：{fixture_mid}")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            check("问数测试自清临时会议", False, f"{e}")

    # ---- 督办台账 ----
    # 这里只测「读」和「被拒绝的写」：真正的流转会往真实台账里追加事件，而事项
    # 没有删除接口，跑一次测试就留一条脏数据。正常写入路径在 [8] 用临时目录测。
    book = get("/api/oversight")
    check("/api/oversight 返回列表", isinstance(book.get("items"), list) and bool(book["items"]), str(list(book.keys())))
    buckets = (book.get("scan") or {}).get("buckets") or {}
    check(
        "/api/oversight 带风险分桶",
        set(buckets) == {"逾期", "今日到期", "临期", "签收超时", "停滞"},
        str(list(buckets)),
    )
    check(
        "/api/oversight 带状态机选项",
        book.get("statuses") == ["待签收", "办理中", "待审核", "已办结"],
        str(book.get("statuses")),
    )

    first_id = book["items"][0]["id"]
    one = get(f"/api/oversight/{first_id}")
    check("/api/oversight/<id> 带事件流", isinstance((one.get("item") or {}).get("events"), list), "")
    check("/api/oversight/<id> 带可执行操作", isinstance((one.get("item") or {}).get("allowed_actions"), list), "")

    try:
        get("/api/oversight/DB9999-9999")
        check("不存在的事项返回 404", False, "居然返回 200")
    except urllib.error.HTTPError as e:
        check("不存在的事项返回 404", e.code == 404, f"code={e.code}")

    def post(path: str, payload: dict):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    code, d = post(f"/api/oversight/{first_id}/action", {})
    check("缺 action 返回 400", code == 400 and d.get("error") == "action_required", f"{code} {str(d)[:70]}")

    code, d = post("/api/oversight/nope-9999/action", {"action": "签收"})
    check("不存在的事项流转返回 404", code == 404, f"{code} {str(d)[:70]}")

    finished = [i for i in book["items"] if i["status"] == "已办结"]
    if finished:
        code, d = post(f"/api/oversight/{finished[0]['id']}/action", {"action": "催办"})
        check("已办结催办返回 409", code == 409 and d.get("ok") is False, f"{code} {str(d)[:70]}")

    code, d = post("/api/oversight/urge", {"ids": []})
    check("空催办列表返回 400", code == 400, f"{code} {str(d)[:70]}")

    # 关注列表走 HTTP 写一遍。这一步打的是**服务端的真实数据**，所以还原必须
    # 精确到"和测试前完全一样"：原来没有自定义覆盖的话，写回一份等于默认值的
    # 列表会留下一个覆盖，把以后对默认表的调整盖掉。此时改用 codes=null 删掉覆盖。
    wsb = get("/api/workspace?person=bureau")
    orig = [c["code"] for c in wsb["watch"]]
    import people  # 默认表在 core/people，用来判断"测试前有没有自定义覆盖"

    code, d = post("/api/workspace/watch", {"person": "bureau", "codes": orig[:2]})
    check("POST 关注列表", code == 200 and d.get("count") == min(2, len(orig)), f"{code} {str(d)[:70]}")
    now = [c["code"] for c in get("/api/workspace?person=bureau")["watch"]]
    check("写入后按人取到新列表", now == orig[:2], f"{now} vs {orig[:2]}")

    code, d = post("/api/workspace/watch", {"person": "bureau", "codes": ["NO_SUCH_METRIC"]})
    check("未注册指标写入返回 400", code == 400 and "没有注册" in (d.get("error") or ""), f"{code} {str(d)[:70]}")

    at_default = orig == list(people.DEFAULT_WATCH["bureau"])
    code, d = post("/api/workspace/watch", {"person": "bureau", "codes": None if at_default else orig})
    back = [c["code"] for c in get("/api/workspace?person=bureau")["watch"]]
    check("还原关注列表", code == 200 and back == orig, f"{code} {back} vs {orig}")
    if at_default:
        # 取消覆盖后必须落回默认表，而不是留一份写死的默认值
        check("codes=null 回到默认表", back == list(people.DEFAULT_WATCH["bureau"]), str(back))
        check("codes=null 标记为 reset", d.get("reset") is True, str(d)[:70])


def test_oversight_flow() -> None:
    """督办写闭环：状态机守卫、必填约束、催办语义。

    刻意新建一条自己的事项来跑完整生命周期，不去动种子数据 —— 种子数据是被
    test_alerts 按"逾期/临期/停滞"验过风险判定的，改一条就会连带改变断言前提。
    """
    print("[10] 督办流转")

    r = ov.create_item(
        title="冒烟测试事项",
        assignee="市测试局",
        deadline=(ov.today() + timedelta(days=5)).isoformat(),
        standard="测试期内完成一次签收与办结",
        priority="一般",
    )
    check("新建督办事项", r.get("ok") is True, str(r))
    iid = r.get("item_id")
    if not iid:
        return

    check("新建后为待签收", (ov.detail(iid) or {}).get("status") == ov.PENDING, str((ov.detail(iid) or {}).get("status")))
    check("待签收可签收", "签收" in (ov.detail(iid) or {}).get("allowed_actions", []), "")

    r = ov.advance(iid, "签收", "市测试局")
    check("签收后进入办理中", bool(r.get("ok")) and r.get("to_status") == ov.WORKING, str(r)[:90])

    r = ov.advance(iid, "签收", "市测试局")
    check("重复签收被守卫拒绝", r.get("ok") is False and "不能执行" in str(r.get("error")), str(r)[:70])

    r = ov.advance(iid, "确认办结", "市政府办")
    check("跳过环节直接办结被拒", r.get("ok") is False, str(r)[:70])

    r = ov.advance(iid, "提交办结", "市测试局", text="已办完")
    check("无进展记录不能提交办结", r.get("ok") is False and "进展" in str(r.get("error")), str(r)[:70])

    r = ov.advance(iid, "反馈进展", "市测试局", text="完成一半", percent=50)
    check("反馈进展保持办理中", bool(r.get("ok")) and r.get("to_status") == ov.WORKING, str(r)[:70])
    check("进展百分比可读回", (ov.detail(iid) or {}).get("progress") == 50, str((ov.detail(iid) or {}).get("progress")))
    check("进度越界被夹到 0-100", ov.advance(iid, "反馈进展", "市测试局", text="超额", percent=300).get("ok") is True
          and (ov.detail(iid) or {}).get("progress") == 100, str((ov.detail(iid) or {}).get("progress")))

    r = ov.advance(iid, "提交办结", "市测试局", text="已完成")
    check("缺佐证不能提交办结", r.get("ok") is False and "佐证" in str(r.get("error")), str(r)[:70])

    r = ov.advance(iid, "提交办结", "市测试局", text="已完成", evidence=["回执.pdf"])
    check("提交办结进入待审核", bool(r.get("ok")) and r.get("to_status") == ov.REVIEW, str(r)[:70])

    r = ov.advance(iid, "退回", "市政府办", text="")
    check("退回必须写理由", r.get("ok") is False, str(r)[:70])

    r = ov.advance(iid, "退回", "市政府办", text="佐证只有回执，缺现场照片")
    check("退回回到办理中", bool(r.get("ok")) and r.get("to_status") == ov.WORKING, str(r)[:70])

    before = ov.get(iid).status
    r = ov.advance(iid, "催办", "市政府办")
    check("催办不改状态", bool(r.get("ok")) and ov.get(iid).status == before, str(r)[:70])
    check("催办计入次数", ov.get(iid).remind_count == 1, str(ov.get(iid).remind_count))
    check("催办不算进展", ov.get(iid).events[-1].type == "催办", ov.get(iid).events[-1].type)

    ov.advance(iid, "提交办结", "市测试局", text="已补齐", evidence=["回执.pdf", "现场照片.zip"])
    r = ov.advance(iid, "确认办结", "市政府办")
    check("确认办结进入已办结", bool(r.get("ok")) and r.get("to_status") == ov.CLOSED, str(r)[:70])
    check("已办结没有可执行操作", ov.allowed_actions(ov.get(iid)) == [], str(ov.allowed_actions(ov.get(iid))))
    check("已办结不能再催办", ov.advance(iid, "催办", "市政府办").get("ok") is False, "")
    check("已办结风险等级为正常", ov.assess(ov.get(iid)).level == ov.RISK_NONE, ov.assess(ov.get(iid)).level)

    # 派生量不能被外部写进来：没有哪个字段能直接把风险设成"逾期"
    check("风险是派生量不是字段", "risk" not in ov.Item.__dataclass_fields__, "")

    missing = ov.advance("DB9999-0001", "签收", "市政府办")
    check("不存在的事项返回失败而不是抛异常", missing.get("ok") is False and "没有编号" in str(missing.get("error")), "")

    bad = ov.create_item(title="越期事项", assignee="市测试局", deadline="2000-01-01", standard="x")
    check("新建事项时限不能早于今天", bad.get("ok") is False and "早于今天" in str(bad.get("error")), str(bad)[:70])


def test_briefdoc() -> None:
    """材料助手：装配出来的每个数字都要能追回出处，模型只能写「下一步」。

    这里最要紧的一条是「材料里的指标值 == 问数接口的值」：材料是给外面看的，
    如果它和问数回答对不上，两份东西摆在一张桌上就没人能解释差在哪。
    """
    print("\n[12] 材料助手")
    import ask
    import briefdoc as doc
    import llmclient

    md_mod = doc.md
    today = ov.today()
    ov.create_item(title="材料测试·生态环境局事项", assignee="市生态环境局", deadline=(today + timedelta(days=5)).isoformat(), standard="有可核验结果")
    ov.create_item(title="材料测试·住建局事项", assignee="市住建局", deadline=(today + timedelta(days=2)).isoformat(), standard="有可核验结果")

    opt = doc.options("mayor")
    check(
        "配置项给出三种骨架",
        [p["id"] for p in opt["presets"]] == ["report", "minutes", "bulletin"],
        str([p["id"] for p in opt["presets"]]),
    )
    check(
        "配置项的期次都是已披露的",
        all(p["value"] in md_mod.available_periods(today) for p in opt["periods"]),
        str([p["value"] for p in opt["periods"]][:3]),
    )
    check("配置项带指标清单", bool(opt["metrics"]), "")

    d = doc.compose("mayor", use_model=False)
    ids = [b["id"] for b in d["blocks"]]
    check("工作汇报默认四节", ids == ["metrics", "oversight", "meeting", "next"], str(ids))
    check("章节用中文序号", [b["no"] for b in d["blocks"]] == ["一", "二", "三", "四"], str([b["no"] for b in d["blocks"]]))

    mb = next(b for b in d["blocks"] if b["id"] == "metrics")
    mismatch = []
    for row in mb["rows"]:
        ref = query_metric(row["code"], d["meta"]["period"], 1, today)
        if row["value"] != ask.fmt_value(ref["value"], ref["unit"]):
            mismatch.append((row["code"], row["value"], ask.fmt_value(ref["value"], ref["unit"])))
    check("材料里的指标值与问数接口一致", not mismatch, str(mismatch[:2]))
    check("每个指标都写了数据来源", all(r["source"] for r in mb["rows"]), "")
    check("资料来源带部门和披露日", all(s["published_at"] and s["department"] for s in d["meta"]["sources"]), "")
    check("指标口径写在备注里", "口径" in (mb.get("note") or ""), str(mb.get("note"))[:40])

    def nums(text: str) -> set[str]:
        return set(re.findall(r"\d+(?:\.\d+)?", text or ""))

    allowed = nums(d["meta"]["period_label"]) | nums(d["meta"]["generated_at"])
    for b in d["blocks"]:
        allowed |= nums(json.dumps(b, ensure_ascii=False))
    stray = nums(d["markdown"]) - allowed
    check("Markdown 里的数字都能在结构里找到", not stray, str(sorted(stray))[:80])
    check("Markdown 带标题与资料来源", d["markdown"].startswith("# ") and "资料来源" in d["markdown"], "")
    check("演示数据要标明", "未接入真实数据源" in d["markdown"], "")

    d2 = doc.compose("mayor", codes=[], use_model=False)
    check("指标全不选就不出指标块", not any(b["id"] == "metrics" for b in d2["blocks"]), str([b["id"] for b in d2["blocks"]]))
    check("指标全不选会给提示", any("没有选择指标" in w for w in d2["warnings"]), str(d2["warnings"]))
    d3 = doc.compose("mayor", sections=[], use_model=False)
    check("章节全不选就不出块（不能回落成默认）", d3["blocks"] == [], str([b["id"] for b in d3["blocks"]]))

    pend = md_mod.pending_periods(today)
    if pend:
        d4 = doc.compose("mayor", period=pend[0], use_model=False)
        check("未披露期次退回最新一期", d4["meta"]["period"] == md_mod.latest_available(today), d4["meta"]["period"])
        check("退回时写明原因", any("披露日" in w for w in d4["warnings"]), str(d4["warnings"]))

    def total_of(pid: str) -> tuple[int, str]:
        r = doc.compose(pid, kind="bulletin", use_model=False)
        blk = next(x for x in r["blocks"] if x["id"] == "oversight")
        return int(blk["stats"][0]["value"].split()[0]), r["meta"]["scope_text"]

    m_total, m_scope = total_of("mayor")
    b_total, b_scope = total_of("bureau")
    check("局长看到的台账少于市长", b_total < m_total, f"{b_total} / {m_total}")
    check("分管口径写明含协办", "含协办" in b_scope, b_scope)
    check("全市口径不写含协办", "含协办" not in m_scope, m_scope)

    mt_ledger.create(
        title="材料测试会",
        text="一、议定：由市生态环境局负责，月底前完成整改。",
        held_at=f"{today} 09:00",
        attendees=["市生态环境局"],
    )
    latest_mt = mt_ledger.list_meetings()[0]
    d5 = doc.compose("mayor", kind="minutes", use_model=False)
    mb2 = next(b for b in d5["blocks"] if b["id"] == "meeting")
    check("会议材料指向最近一场会", mb2["meeting"]["id"] == latest_mt["id"], f"{mb2['meeting']['id']} vs {latest_mt['id']}")
    check("会议材料第一块就是会议", [b["id"] for b in d5["blocks"]][0] == "meeting", str([b["id"] for b in d5["blocks"]]))
    # 参会单位是市生态环境局，它发布的指标要被挑进来；挑不满 4 项才用本人关注补
    check("会议材料按参会单位挑指标", "AQI_GOOD_DAYS" in d5["meta"]["metric_codes"], str(d5["meta"]["metric_codes"]))

    # 数字闸：模型写的段落里出现材料外的数字就整段丢
    orig_chat, orig_avail = llmclient.chat_json, llmclient.available
    llmclient.available = lambda: True
    try:
        llmclient.chat_json = lambda *a, **k: {"data": {"next": "明年要把增速做到 12.3% 才算过关。"}}
        dm = doc.compose("mayor", use_model=True)
        nb = next(b for b in dm["blocks"] if b["id"] == "next")
        check("模型编的数字被整段丢弃", dm["model"]["source"] == "rule" and "12.3" not in nb["text"], dm["model"]["note"])

        llmclient.chat_json = lambda *a, **k: {"data": {"next": "字" * 300}}
        dl = doc.compose("mayor", use_model=True)
        check("模型写过长的段落被丢弃", dl["model"]["source"] == "rule", dl["model"]["note"])

        st = ov.stats()
        say = f"台账共 {st['total']} 项，先办逾期和临界的。"
        llmclient.chat_json = lambda *a, **k: {"data": {"next": say}}
        dok = doc.compose("mayor", use_model=True)
        nok = next(b for b in dok["blocks"] if b["id"] == "next")
        check("模型复述材料里的数字可以通过", dok["model"]["source"] == "model" and nok["text"] == say, dok["model"]["note"])
        check("模型写过之后注释跟着换", "模型" in (nok.get("note") or ""), str(nok.get("note"))[:40])
    finally:
        llmclient.chat_json, llmclient.available = orig_chat, orig_avail

    body = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("材料弹窗在页面上", 'id="docModal"' in body and "function openDocModal" in body, "")
    check("顶栏按钮已接上分发", 'tool.dataset.tool === "doc"' in body, "")
    m = re.search(r"\.doc-paper\{([^}]*)\}", body)
    paper_css = m.group(1) if m else ""
    check("纸张底色写死白纸、不跟随主题", "#fff" in paper_css and "var(--" not in paper_css, paper_css[:60])
    check("打印样式只保留纸张", "@media print" in body and "visibility:hidden" in body, "")


def test_schedule() -> None:
    """AI 日程：只收已有时间的事、按身份过滤、同日不重复、模型只写话不排期。

    造数是必要的：数据层测试跑在空目录里，没有事项就没有日程。这里造的三条
    正好覆盖三种形态（待签收 / 今天到期 / 已逾期），会议补一场未来的、一场过去的。
    """
    print("\n[11] AI 日程")
    import schedule as cal

    today = ov.today()

    def mk(title: str, assignee: str, days: int) -> str:
        r = ov.create_item(title=title, assignee=assignee, deadline=(today + timedelta(days=days)).isoformat(), standard="有可核验结果")
        assert r.get("ok"), r
        return r["item_id"]

    mk("日程测试·待签收", "市测试局", 4)
    mk("日程测试·今天到期", "市测试局", 0)
    late = mk("日程测试·已逾期", "市测试二局", 1)
    # 逾期事项不能新建（create_item 会拒），只能建完再改时限——真实系统里就是时间走到了。
    # 先签收：待签收的动作优先级高于逾期，不签收测出来的会是 sign 而不是 overdue。
    ov.advance(late, "签收", "市测试二局")
    ov._ITEMS[late].deadline = (today - timedelta(days=2)).isoformat()

    mt_ledger.create(
        title="日程测试会",
        text="议定：由市测试局办理。",
        held_at=f"{today + timedelta(days=1)} 09:00",
        host="常务副市长",
        attendees=["市测试局"],
    )
    mt_ledger.create(
        title="日程测试·昨天的会",
        text="议定：已办。",
        held_at=f"{today - timedelta(days=1)} 09:00",
        attendees=["市测试局"],
    )

    data = cal.build("mayor", 14)
    groups = data["groups"]
    flat = [i for g in groups for i in g["items"]]

    check("日程按日期升序", [g["date"] for g in groups] == sorted(g["date"] for g in groups), "")
    check("今天永远是第一段", not groups or groups[0]["is_today"] or groups[0]["date"] > today.isoformat(), "")
    check("待签收进入今天", any(i["kind"] == "sign" and "待签收" in i["title"] for i in flat), "")

    sign_rows = [i for i in flat if "待签收" in i["title"]]
    check("签收动作与其后时限是两天两条", len(sign_rows) == 2, f"实际 {len(sign_rows)}")
    check("今天动作那行落在今天", any(i["date"] == today.isoformat() and i["kind"] == "sign" for i in sign_rows), "")

    due_rows = [i for i in flat if "今天到期" in i["title"]]
    check("当天到期的动作与时限不重复", len(due_rows) == 1, f"实际 {len(due_rows)}")

    over = [i for i in flat if i["kind"] == "overdue" and "已逾期" in i["title"]]
    check("逾期事项归到今天", bool(over) and over[0]["date"] == today.isoformat(), "")
    check(
        "逾期保留原时限",
        bool(over) and over[0]["extra"].get("origin_deadline") == (today - timedelta(days=2)).isoformat(),
        str(over[0]["extra"] if over else None),
    )

    check("已结束的会议不进日程", all("昨天的会" not in i["title"] for i in flat), "")
    check("未来的会议进日程", any("日程测试会" in i["title"] for i in flat), "")

    # 跳转必须落地：ref 指向的督办/会议真的存在，否则点了会 404
    bad_refs = [
        i["ref"]
        for i in flat
        if i["ref"]
        and not ((i["ref"]["type"] == "oversight" and ov.get(i["ref"]["id"])) or (i["ref"]["type"] == "meeting" and mt_ledger.get(i["ref"]["id"])))
    ]
    check("每条日程的跳转都指向存在的对象", not bad_refs, str(bad_refs[:2]))

    bureau = cal.build("bureau", 14)
    bureau_titles = [i["title"] for g in bureau["groups"] for i in g["items"]]
    check("局长看不到别的单位的事", all("市测试" not in t for t in bureau_titles), str(bureau_titles[:3]))
    check("局长的日程按分管单位标注口径", bureau["basis"] == "分管单位：市生态环境局", bureau["basis"])

    vm = cal.build("vmayor", 14)
    vm_titles = [i["title"] for g in vm["groups"] for i in g["items"]]
    check("常务副市长能看到自己主持的会", any("日程测试会" in t for t in vm_titles), str(vm_titles[:3]))
    check("无关的会不塞给别人", all("日程测试会" not in t for t in bureau_titles), "")

    disc = cal._next_disclosure(today)
    check("披露日固定在 10 号", disc.day == DISCLOSURE_DAY, str(disc))
    check("披露日在今天之后", disc > today, str(disc))

    rule = cal.brief("mayor", 14, use_model=False)
    check("没有模型时降级为规则排序", rule["degraded"] is True and rule["source"] == "rule", str(rule.get("note")))
    allowed = {str(data["summary"]["today"]), str(data["summary"]["total"])}
    for e in flat:
        allowed |= cal._nums(" ".join([e["title"], e["sub"], e["why"], e["date"], e["time"]]))
    check("规则简报不引入清单外的数字", not (cal._nums(rule["brief"]) - allowed), rule["brief"])

    # 数字闸：模型写了一个清单里没有的数字（99），整句必须被丢掉
    real_chat, real_avail = cal.llmclient.chat_json, cal.llmclient.available
    cal.llmclient.available = lambda: True
    try:
        cal.llmclient.chat_json = lambda *a, **k: {"data": {"brief": "今天有 99 件事要办", "why": {}}, "model": "stub"}
        bad = cal.brief("mayor", 14)
        check("模型编出来的数字被丢弃", bad["degraded"] is True and "数字" in (bad.get("note") or ""), str(bad.get("note")))

        cal.llmclient.chat_json = lambda *a, **k: {"data": {"brief": "先办逾期那件，会后补报进展。", "why": {}}, "model": "stub"}
        good = cal.brief("mayor", 14)
        check("合规简报被采纳", good["degraded"] is False and good["source"] == "model", str(good.get("note")))

        cal.llmclient.chat_json = lambda *a, **k: {"data": {"brief": "今天" + "。" * 90, "why": {}}, "model": "stub"}
        long = cal.brief("mayor", 14)
        check("超长简报被丢弃", long["degraded"] is True and "长度" in (long.get("note") or ""), str(long.get("note")))
    finally:
        cal.llmclient.chat_json, cal.llmclient.available = real_chat, real_avail


def test_frontend_sidebar() -> None:
    """左栏：会话列表 + 侧栏收起。

    这一段全部是"重构后有没有留死东西"的静态检查 —— 会话切换从
    `<select id="threadSel">` 换成列表之后，旧的 options.length 计数、
    旧的事件绑定如果没清干净，浏览器里不会报错，只会默默少一个入口。
    """
    print("\n[19] 左栏会话列表与收起")
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("会话列表屏在", 'id="askChatsPane"' in html and 'id="chatsList"' in html and 'id="chatsCount"' in html, "")
    check("列表项带会话 id", 'class="chat-item' in html and "data-tid=" in html, "")
    check("下拉框已彻底移除", 'id="threadSel"' not in html, "")
    check("会话计数只认 THREADS", "THREADS.length" in html and "options.length - 1" not in html, "")
    check("模型药丸按属性刷（两处共用）", "data-model-pill" in html, "")
    check("侧栏可收起", 'id="askFold"' in html and 'id="askRail"' in html and "html.ask-fold .ask{" in html, "")
    # 收起必须是「除窄条外全隐藏」。点名 .ask-head/.ask-body/.ask-chats 那版特异性比
    # .ask.show-chats .ask-chats 低，开着历史屏再收起会留下半截内容（截图上像"边上还有显示"）。
    check("收起时历史屏也一起藏（不留残影）", "html.ask-fold .ask > *:not(.ask-rail)" in html, "")
    # V3 把「＋新对话」从会话列表第一行提到头部（见 .new-chat-head 的注释），
    # 列表里不再有 class="chat-new" 的按钮 —— 两边同时存在会出现两个入口。
    check("新对话在头部、不在列表里",
          'class="new-chat-head"' in html and 'id="headNew"' in html and '"chat-new"' not in html, "")
    # 位置比的是 </head>，不是 "<body>"：head 里的注释本身就有 "<body> 之前"这句话，会先命中
    check("收起状态记在本机且首屏前生效",
          "ocgov.askfold" in html and html.index("ocgov.askfold") < html.index("</head>"), "")
    check("#chats 可直达历史对话", 'location.hash === "#chats"' in html, "")
    check("旧「新对话」按钮不再残留", 'id="newChat"' not in html, "")


def test_frontend_thread_switch() -> None:
    """跑着的时候切会话，这一轮不能被弄丢，也不能嫁接到别的会话上。

    这是「界面看着正常、数据是错的」那类 bug：切走会重建 #thread，正在写的
    那个 AI 气泡成了脱离文档的孤儿（往里写谁也看不见），随后 done 又把全局
    THREAD 改回旧会话 —— 用户在 B 接着打字，问题挂到 A 去了，草稿也跟着错位。
    修复靠三处同时在，少一处就会退回去，所以逐条卡住。
    """
    print("\n[19.5] 跑着的时候切会话")
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("写消息前先确认容器还在（live）", "const live = () =>" in html, "")
    seg = html[html.index("const stage = (text) =>"):][:300]
    check("进度提示走 live()，不直接写 holder", "const h = live();" in seg, "")
    # done 里不能再无条件覆盖当前会话 —— 这一行就是串台的直接成因
    done_seg = html[html.index('f.type === "done"'):][:600]
    check("done 不覆盖当前会话", "THREAD = f.thread_id;" not in done_seg, "")
    check("start 只接住新会话、不抢当前会话", "!RUN.detached" in html, "")
    check("切回会话时把「正在算」的占位挂回去", "!RUN.holder.isConnected" in html, "")
    check("按钮只属于它所在的那个会话",
          "setRunning(!!(RUN && RUN.threadId && RUN.threadId === id))" in html, "")
    # 刷新接回用的 threadId 必须是这一轮的，不是全局 THREAD
    check("待接记录用这一轮自己的会话", "threadId: RUN.threadId || THREAD" in html, "")
    check("在别的会话里按「停止」不会停掉那轮", "RUN.threadId !== THREAD" in html, "")


def test_frontend_live() -> None:
    """页面必须真的跟本地服务说话。

    V3 那份 index.html 是「静态快照」导出：文件头部自带一段 window.fetch 覆盖，
    把所有 /api/* 换成内联的 SNAP 数据，问答接口直接回一句
    「这是静态快照，问答需要启动本地服务」。放进服务里跑，页面看着一切正常，
    实际一个请求都没发出去 —— 问答、材料、督办操作全是死的。

    这类问题浏览器不报错，页面也照常渲染，只能静态卡住。
    """
    print("\n[20] 页面直连服务（不许有快照垫片）")
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")

    check("页面没有覆盖 fetch", "window.fetch =" not in html and "window.fetch=" not in html, "")
    check("没有静态快照文案", "这是静态快照" not in html and "静态快照模式" not in html, "")
    check(
        "问答走真实流式接口",
        'fetch("/api/ask/stream"' in html and "res.body.getReader()" in html,
        "",
    )

    # 页面里写死的接口，后端必须真的认。缺一个（比如当初的 /api/focus）不会白屏：
    # 页面静默降级或吃 404，只有点开那个弹窗的人才知道坏了。
    import re

    main_py = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    routes = set(re.findall(r'path == "(/api/[^"]*)"', main_py))
    prefixes = set(re.findall(r'path\.startswith\("(/api/[^"]*)"\)', main_py))

    def known(path: str) -> bool:
        if path in routes or any(path.startswith(x) for x in prefixes):
            return True
        # 页面里 "/api/meetings/" + id 这种拼接会留下裸前缀
        if path.endswith("/"):
            base = path.rstrip("/")
            return base in routes or any(r.startswith(path) for r in routes)
        return False

    calls = set()
    for m in re.finditer(r"fetch\(\s*[`'\"]([^`'\"]+)[`'\"]", html):
        url = m.group(1).split("?")[0]
        if url.startswith("/api/") and "${" not in url:
            calls.add(url)
    missing = sorted(c for c in calls if not known(c))
    check("页面调用的接口后端都实现了", bool(calls) and not missing, ",".join(missing)[:120])

    # 刷新接回：turn_id 必须真落进 sessionStorage。这条能力最容易在重构里被悄悄
    # 删掉 —— 删了不报错、不白屏，只是用户刷新后那一轮永久失联，后端算完也白算。
    check("进行中的轮次存进 sessionStorage", "sessionStorage.setItem(PENDING_TURN" in html, "")
    check("接回只认自己发起的轮次", "p.person !== PID.get()" in html, "")
    check("接回要等 done 才重渲染", "if (d && d.done)" in html, "")
    # 只有"还在后台跑"才留记录；正常结束、点停止都必须清，否则下次开页面会去接
    # 一轮几小时前的旧轮次
    check("正常结束才清掉待接记录", "if (!keepPending) pendingClear()" in html, "")
    check("启动时接回未完成的轮次", "if (pendingRead()) resumePending()" in html, "")

    # 回答导出 / 会话内查找 / 上下文透明。三样都是"界面上的入口一旦被重构删掉，
    # 没有任何报错"的东西 —— 恰恰最需要静态断言兜住。
    check("回答带导出按钮", 'data-exp="1"' in html, "")
    check("导出走结构化数据而不是扒 DOM", "function answerMd(" in html and "function mdFromHtml(" in html, "")
    check("导出里带出处与降级提示", "**出处**" in html and "答案质量可能受影响" in html, "")
    check("历史消息也挂了数据（走 meta 导出）", "b.__m = aiMsgs[i]" in html, "")
    check("会话内查找条存在", 'id="findBar"' in html and 'id="findInput"' in html, "")
    check("查找抢了 Ctrl+F", 'toLowerCase() === "f"' in html, "")
    # 只描边不改 DOM：改写文本节点会拆断复制/折叠/收藏的事件绑定。
    # 所以这里查"有没有真去造 <mark>"，而不是查字符串——注释里提到这个词不算。
    check(
        "查找只描边、不改写 DOM",
        "find-hit{outline" in html and 'createElement("mark")' not in html,
        "",
    )
    check("每条回答显示上下文轮数", 'class="ai-ctx' in html, "")
    check("上下文统计随历史回放", "context: meta.context || {}" in html, "")


def main() -> int:
    test_data_layer()
    test_alerts()
    test_same_source()
    test_models()
    test_chat()
    test_workspace()
    test_people()
    test_experts()
    test_meeting()
    test_oversight_flow()
    test_schedule()
    test_briefdoc()
    test_knowledge()
    test_notify()
    test_saved()
    test_meetplan()
    test_attach_entry()
    test_frontend_theme()
    test_frontend_sidebar()
    test_frontend_thread_switch()
    test_frontend_live()
    test_http()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
