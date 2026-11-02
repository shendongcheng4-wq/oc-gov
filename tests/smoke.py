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
from mockdata import METRICS, query_metric  # noqa: E402

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


def test_frontend_theme() -> None:
    """前端主题的静态约定。

    前端没有浏览器测试，但"深色主题漏定义变量"这类错误是纯文本可查的，
    而它的后果只在深色下露出一块白底 —— 最容易漏测，必须卡在提交前。
    """
    print("\n[10] 前端主题")
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


def test_http(port: int | None = None) -> None:
    port = port or int(os.environ.get("OCGOV_PORT") or 8090)
    print(f"[11] HTTP 接口（127.0.0.1:{port}，未启动则跳过）")
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

    detail = get("/api/metric/GDP_TOTAL")
    check("/api/metric/GDP_TOTAL", detail.get("ok") is True and detail.get("code") == "GDP_TOTAL", "")

    try:
        get("/api/metric/NOT_A_METRIC")
        check("未注册指标返回 404", False, "居然返回了 200")
    except urllib.error.HTTPError as e:
        check("未注册指标返回 404", e.code == 404, f"code={e.code}")

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


def main() -> int:
    print(f"oc-gov 冒烟测试  测试数据目录 {TEST_DATA}")
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
    test_frontend_theme()
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
