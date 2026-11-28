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

关于日期：会议与议定事项全部对回溧阳市政府门户网站的公开报道，日期是真实发布
日期，不再按「今天」的相对偏移生成 —— 真实数据不该为了迁就脚本运行时间而改期。
代价是这些会放久了会变成历史数据，重跑时 `deadline_past` 会如实地报出来。

演示数据带 `source="demo"` 标记。项目里 `people.py` 早就有 `demo: True` 这个
约定（演示市长），这里沿用同一套语义：一眼能看出哪些是预置的，哪些是自己造的。
"""

from __future__ import annotations

import argparse
import sys
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


def demo_meetings() -> list[dict]:
    """三场会：两场已开（有纪要、可解析出议定事项），一场待开（进日程）。

    内容全部取自溧阳市政府门户网站公开报道，标题、主持人、参会单位、议定事项
    的措辞都照原文；只有「议定事项的具体时限」是按演示需要落到日期上的
    （原文只写「全力以赴」「逐个破解」这类要求，不写日子）。

    - 市政府召开经济运行分析会（2026-11-25）
      https://www.liyang.gov.cn/html/czly/2026/IQKPQDDP_1125/242985.html
    - 市政府召开党组会议和常务会议（2026-01-28，审定两个分解方案、禁放规定）
      https://www.liyang.gov.cn/html/czly/2026/IQKPQDDP_0128/235319.html
    - 重点民生实事票决项目扫尾调度会（待开，依据 11-25 市人大常委会视察要求
      「紧盯剩余项目时序节点，确保年度目标任务顺利完成」）
      https://www.liyang.gov.cn/html/czly/2026/IQKPQDDP_1125/242986.html
    """

    return [
        {
            "title": "市政府经济运行分析会",
            "held_at": "2026-11-25 09:00",
            "host": "市长 巢彬",
            "attendees": ["市发改委", "市工信局", "市商务局", "市税务局"],
            "kind": "record",
            "text": (
                "11月25日上午，市政府召开经济运行分析会，分析当前全市经济运行情况，"
                "部署年末收官攻坚任务，谋划明年开局工作，为2027年起步蓄势奠定基础。\n"
                "会议指出，今年是“十五五”开局之年，完成全年目标既是经济工作任务，"
                "更是必须扛起的政治责任。各板块、各部门要全力以赴完成年度目标，"
                "强化攻坚意识，短板领域加快追赶，优势领域多作贡献。\n"
                "要全力以赴稳定工业增长。聚焦重点薄弱环节，逐项开展专项调度；"
                "逐户摸排产值波动企业，及时协调要素保障；保障重点企业满产稳产。\n"
                "会议明确，由市工信局负责，12月10日前完成产值波动企业逐户摸排，"
                "报送要素保障需求清单。\n"
                "会议明确，由市发改委负责，12月15日前逐个破解重大项目开工竣工滞后问题，"
                "加快形成实物工作量。\n"
                "会议明确，由市发改委、市商务局负责，12月10日前分析社零流失原因，"
                "提出促消费活动方案。\n"
                "会上，市发改委、市工信局、市商务局、市税务局分别汇报1—10月全市经济运行、"
                "工业经济、批零住餐和消费外资外贸、税收情况。"
                "市领导李韬、丁红、张国良、谢振平参加会议。"
            ),
            "items": [
                {
                    "title": "逐户摸排产值波动企业并报送要素保障清单",
                    "assignee": "市工信局",
                    "deadline": "2026-12-10",
                    "standard": "完成规上产值波动企业逐户摸排，报送要素保障需求清单",
                    "quote": (
                        "由市工信局负责，12月10日前完成产值波动企业逐户摸排，"
                        "报送要素保障需求清单。"
                    ),
                    "confidence": "high",
                },
                {
                    "title": "破解重大项目开工竣工滞后问题",
                    "assignee": "市发改委",
                    "deadline": "2026-12-15",
                    "standard": "逐个破解滞后问题，加快形成实物工作量",
                    "quote": (
                        "由市发改委负责，12月15日前逐个破解重大项目开工竣工滞后问题，"
                        "加快形成实物工作量。"
                    ),
                    "confidence": "high",
                },
                {
                    "title": "分析社零流失原因并提出促消费活动方案",
                    "assignee": "市发改委",
                    "deadline": "2026-12-10",
                    "standard": "完成社零流失原因分析，提出促消费活动方案",
                    "quote": (
                        "由市发改委、市商务局负责，12月10日前分析社零流失原因，"
                        "提出促消费活动方案。"
                    ),
                    "confidence": "high",
                },
            ],
        },
        {
            "title": "市人大常委会视察2026年重点民生实事票决项目",
            "held_at": "2026-11-25 14:30",
            "host": "市人大常委会",
            "attendees": ["市教育局", "市住建局", "市公安局", "市卫健局", "市民政局"],
            "kind": "record",
            "text": (
                "11月25日，市人大常委会领导张爱文、曹俊、朱志军、夏国中、张雅萍、葛军伟一行"
                "视察2026年重点民生实事票决项目建设情况，副市长潘叶锋、市政府党组成员周文光参加。\n"
                "视察组先后察看了市道路交通事故一体化处理服务中心、三和王家村停车场、市公安局、"
                "燕湖小学、马垫老年公寓等项目现场，听取全溧畅达、全溧安居、全溧平安、全溧优学、"
                "全溧颐养等重点民生实事票决项目实施情况介绍。\n"
                "市道路交通事故一体化处理服务中心11月2日投入试运行，集事故受理、责任认定、调解、"
                "审判、保险理赔于一体，试运行以来累计受理案件412起，调解成功率达97.6%。"
                "三和王家村改造建设机动车停车位110个、新增车位55个。全市共3所学校完成心理辅导室"
                "标准化改造，燕湖小学“湖心屿”心理辅导室已建成投用。\n"
                "视察组要求，要全力做好今年民生实事票决项目的扫尾攻坚，紧盯剩余项目时序节点，"
                "确保年度目标任务顺利完成。\n"
                "会议明确，由市住建局负责，12月10日前完成三和王家村公厕和蓝盾驿站收尾；"
                "由市民政局负责，12月20日前完成228名银发顾问技能提升培训。"
            ),
            "items": [
                {
                    "title": "完成三和王家村公厕和蓝盾驿站收尾",
                    "assignee": "市住建局",
                    "deadline": "2026-12-10",
                    "standard": "公厕和蓝盾驿站建成投用，补齐社区便民服务短板",
                    "quote": (
                        "由市住建局负责，12月10日前完成三和王家村公厕和蓝盾驿站收尾；"
                        "由市民政局负责，12月20日前完成228名银发顾问技能提升培训。"
                    ),
                    "confidence": "high",
                },
                {
                    "title": "完成228名银发顾问技能提升培训",
                    "assignee": "市民政局",
                    "deadline": "2026-12-20",
                    "standard": "228名银发顾问全部完成技能提升培训并考核合格",
                    "quote": (
                        "由市住建局负责，12月10日前完成三和王家村公厕和蓝盾驿站收尾；"
                        "由市民政局负责，12月20日前完成228名银发顾问技能提升培训。"
                    ),
                    "confidence": "high",
                },
            ],
        },
        {
            "title": "2026年重点民生实事票决项目扫尾调度会",
            "held_at": "2026-12-08 09:00",
            "host": "市长 巢彬",
            "attendees": ["市教育局", "市住建局", "市公安局", "市卫健局", "市民政局", "团市委"],
            "kind": "plan",
            "text": "",
            "topics": ["重点民生实事票决项目扫尾攻坚", "2027年度民生实事项目谋划"],
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
