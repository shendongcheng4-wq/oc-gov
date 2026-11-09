"""写入演示数据，让新拿到的仓库不是「空的」。

    python seed_demo.py            # 写入演示数据（已有则跳过）
    python seed_demo.py --force    # 清掉旧的演示数据后重建
    python seed_demo.py --dry-run  # 只打印将要做什么，不落盘

为什么需要这个脚本：

    `data/` 不进版本库 —— 那是运行时状态，里面会有会话、模型配置这些不该公开
    的东西。代价是别人 clone 下来之后，会议库是空的，问「上次开会定了什么」只能
    答「知识库 19 条里没有相关记录」。

    但知识库其实不空：指标口径 11 条、督办事项 8 条都是代码内置的，只有会议记录
    这一块依赖 `data/meetings.json`。所以本脚本只补会议，不动指标和督办。

为什么走 `ledger.create()` 而不是直接写 JSON：

    `meetings.json` 里有 `version`、`seq`、每场会的 `id` 编号，手写 JSON 迟早
    写错一处，而且写错的当时不报错，等以后某次 `create()` 撞了编号才发现。调用
    业务层函数，格式永远是对的。

关于日期：全部按「今天」的相对偏移生成，原文里的「X月X日」和 `deadline` 字段
由同一个日期算出。这样脚本放半年后再跑，时限也不会过期、校验也不会红 ——
写死日期的示例数据，过一段时间就变成一堆 `deadline_past`。

演示数据带 `source="demo"` 标记。项目里 `people.py` 早就有 `demo: True` 这个
约定（演示市长），这里沿用同一套语义：一眼能看出哪些是预置的，哪些是自己造的。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "metrics"))
sys.path.insert(0, str(ROOT / "core" / "oversight"))
sys.path.insert(0, str(ROOT / "core" / "meeting"))

import extract as mt_extract  # noqa: E402
import knowledge  # noqa: E402
import ledger as mt_ledger  # noqa: E402


def _day(offset: int, hhmm: str = "09:00") -> tuple[date, str]:
    """今天 + offset 天，返回 (date, "YYYY-MM-DD HH:MM")。"""
    d = date.today() + timedelta(days=offset)
    return d, f"{d.isoformat()} {hhmm}"


def _md(d: date) -> str:
    """日期写成会议纪要里的样子：11月25日。"""
    return f"{d.month}月{d.day}日"


def demo_meetings() -> list[dict]:
    """三场会：两场已开（有纪要、可解析出议定事项），一场待开（进日程）。"""
    d1, at1 = _day(-1, "09:00")          # 昨天开的常务会
    d2, at2 = _day(-6, "14:30")          # 一周前的专题会
    d3, at3 = _day(4, "09:00")           # 四天后要开的会

    dl1, dl2, dl3 = _day(20)[0], _day(45)[0], _day(30)[0]

    return [
        {
            "title": "市政府第 15 次常务会议",
            "held_at": at1,
            "host": "常务副市长",
            "attendees": ["市生态环境局", "市住建局", "市城管局"],
            "kind": "record",
            "text": (
                f"一、会议议定：由市生态环境局负责，{_md(dl1)}前完成入河排污口排查整治，"
                "形成问题台账并逐项销号。\n"
                f"二、会议要求市住建局加快高铁东站周边路网配套工程征迁，{_md(dl2)}前完成全部签约。\n"
                "三、会议强调，要举一反三，防止同类问题反弹。"
            ),
            "items": [
                {
                    "title": "完成入河排污口排查整治并逐项销号",
                    "assignee": "市生态环境局",
                    "deadline": dl1.isoformat(),
                    "standard": "形成问题台账，逐项销号",
                    "quote": (
                        f"由市生态环境局负责，{_md(dl1)}前完成入河排污口排查整治，"
                        "形成问题台账并逐项销号。"
                    ),
                    "confidence": "high",
                },
                {
                    "title": "完成高铁东站周边路网配套工程征迁签约",
                    "assignee": "市住建局",
                    "deadline": dl2.isoformat(),
                    "standard": "完成全部签约",
                    "quote": f"市住建局加快高铁东站周边路网配套工程征迁，{_md(dl2)}前完成全部签约。",
                    "confidence": "high",
                },
            ],
        },
        {
            "title": "城市更新专题会议",
            "held_at": at2,
            "host": "副市长",
            "attendees": ["市住建局", "市自然资源局"],
            "kind": "record",
            "text": (
                f"一、市自然资源局负责，{_md(dl3)}前完成本年度老旧小区改造项目规划许可核发，"
                "不得以材料不全为由拖延。\n"
                f"二、市住建局负责，{_md(dl3)}前完成三个片区的居民意愿征询，征询结果报市政府办公室。\n"
                "三、会议要求，改造方案要充分听取居民意见，避免反复施工。"
            ),
            "items": [
                {
                    "title": "完成本年度老旧小区改造项目规划许可核发",
                    "assignee": "市自然资源局",
                    "deadline": dl3.isoformat(),
                    "standard": "完成规划许可核发",
                    "quote": (
                        f"市自然资源局负责，{_md(dl3)}前完成本年度老旧小区改造项目规划许可核发，"
                        "不得以材料不全为由拖延。"
                    ),
                    "confidence": "high",
                },
                {
                    "title": "完成三个片区居民意愿征询并上报结果",
                    "assignee": "市住建局",
                    "deadline": dl3.isoformat(),
                    "standard": "征询结果报市政府办公室",
                    "quote": f"市住建局负责，{_md(dl3)}前完成三个片区的居民意愿征询，征询结果报市政府办公室。",
                    "confidence": "high",
                },
            ],
        },
        {
            "title": "市政府第 16 次常务会议",
            "held_at": at3,
            "host": "常务副市长",
            "attendees": ["市城管局", "市应急管理局"],
            "kind": "plan",
            "text": "",
            "topics": ["冬季供热保障", "重点项目建设进度"],
            "items": [],
        },
    ]


def _existing_demo() -> list[dict]:
    return [m for m in mt_ledger.list_meetings() if (m.get("source") == "demo")]


def main() -> int:
    ap = argparse.ArgumentParser(description="写入 oc-gov 演示数据（只补会议，不动指标与督办）")
    ap.add_argument("--force", action="store_true", help="先删除已有演示数据再重建")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写入")
    args = ap.parse_args()

    before = len(knowledge.index({}))
    existing = _existing_demo()

    print(f"[seed] 当前知识库索引：{before} 条")
    print(f"[seed] 已有演示会议：{len(existing)} 场")

    if existing and not args.force:
        print("[seed] 已有演示数据，跳过。要重建请加 --force。")
        for m in existing:
            print(f"       · {m.get('id')}  {m.get('title')}")
        return 0

    plans = demo_meetings()
    print(f"[seed] 将要写入 {len(plans)} 场会议：")
    for p in plans:
        kind = "待召开" if p["kind"] == "plan" else "已开"
        print(f"       · [{kind}] {p['title']}  @ {p['held_at']}  议定 {len(p['items'])} 条")

    if args.dry_run:
        print("[seed] --dry-run，未写入。")
        return 0

    if existing:
        for m in existing:
            mt_ledger.delete(m["id"])
        print(f"[seed] 已删除 {len(existing)} 场旧演示会议")
        # 删完重算基线：否则 --force 会打印「26 → 26」，看起来像什么都没发生
        before = len(knowledge.index({}))

    units = set(mt_extract.known_units())
    made = 0
    for p in plans:
        meeting = mt_ledger.create(
            title=p["title"],
            text=p["text"],
            held_at=p["held_at"],
            host=p["host"],
            attendees=p["attendees"],
            source="demo",
            kind=p["kind"],
            topics=p.get("topics") or [],
        )
        mid = meeting["id"]

        # 待召开的会没有原文，不解析；已开的会跑真实校验，不自己编 verified 标记
        if p["items"]:
            verified = []
            for it in p["items"]:
                # assignee 必须在已知单位名单里，否则校验会标 unit_unknown
                assert it["assignee"] in units, f"单位不在名单里：{it['assignee']}"
                v = mt_extract.verify_item(it, p["text"])
                verified.append({**it, "verify": v})
                if v["status"] != "ok":
                    print(f"[seed]   ! {it['title']} 校验未过：{v['issues']}")
            res = mt_ledger.save_parse(
                mid,
                {"items": verified, "model_ref": "demo/seed", "warnings": []},
            )
            if not res.get("ok"):
                print(f"[seed]   落库失败：{res.get('error')}")
                return 1
        print(f"[seed] + {mid}  {p['title']}")
        made += 1

    after = len(knowledge.index({}))
    print(f"\n[seed] 完成：新增 {made} 场会议")
    print(f"[seed] 知识库索引：{before} → {after} 条（会议记录与议定事项已可检索）")
    print("[seed] 接下来：python app/main.py 8090  →  问「上次开会定了什么」")
    print("[seed] 清空演示数据：python seed_demo.py --force 前先删，或直接删 data/meetings.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
