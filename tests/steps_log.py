"""阶段流水（meta.steps）的结构约束。

跑法::

    .venv\\Scripts\\python.exe tests\\steps_log.py

断言七件事：

1. start / end 按 (kind, name) 配对，耗时非负；
2. 没有 start 的 end 也要留下 —— 事件丢了不等于这步没发生；
3. 同一个工具先后查两次是两条，后一条不覆盖前一条；
4. plan 这类一次性事件不配对，字段走白名单（names 截断、降级只留 kind）；
5. 条数封顶，防止某个循环把会话文件撑大；
6. 落库之后能读回来，而 chat.context() 里没有 —— 这份流水只给界面看，
   混进模型上下文等于每轮花几百 token 讲自己刚才干了什么；
7. 流式与非流式两条入口发出的事件形状能被同一个收集器吃下（_step_pusher）。

失败即非零退出。数据写在 data/_test_steps，不碰真实会话。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "metrics"))
sys.path.insert(0, str(ROOT / "core" / "oversight"))
sys.path.insert(0, str(ROOT / "core" / "meeting"))

# 必须先定数据目录再 import：store 在模块加载时就把 DATA_DIR 定了，
# 晚一步就会往真实 chats.json 里写测试会话。
TEST_DATA = ROOT / "data" / "_test_steps"
if not os.environ.get("OCGOV_TEST_KEEP"):
    shutil.rmtree(TEST_DATA, ignore_errors=True)
os.environ["OCGOV_DATA"] = str(TEST_DATA)

import chat  # noqa: E402
import main  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok  {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def feed_all(log, events: list[dict]) -> list[dict]:
    for ev in events:
        log.feed(ev)
    return log.items


# ── 1/2/3: 配对、丢 start、同名两次 ─────────────────────────────────────


def t_pairing() -> None:
    print("[1] start/end 配对")
    log = main.StepLog()
    items = feed_all(
        log,
        [
            {"type": "tool", "name": "query_metric", "phase": "start"},
            {"type": "tool", "name": "query_metric", "phase": "end", "ok": True},
        ],
    )
    check("一条 start+end 只产出一步", len(items) == 1, f"实际 {len(items)} 条")
    if not items:
        return
    it = items[0]
    check("kind 保留", it.get("k") == "tool", str(it))
    check("ms 被 end 补齐且非负", isinstance(it.get("ms"), int) and it["ms"] >= 0, str(it))
    check("ok 记在条目顶层", it.get("ok") is True, str(it))
    check("x 里带工具名", (it.get("x") or {}).get("name") == "query_metric", str(it))

    print("[2] 只有 end 没有 start")
    log = main.StepLog()
    items = feed_all(log, [{"type": "experts", "name": "发改委", "phase": "end", "ok": False}])
    check("仍然留下一条", len(items) == 1, f"实际 {len(items)} 条")
    if items:
        check("耗时记 0 而不是缺失", items[0].get("ms") == 0, str(items[0]))
        check("失败态能回放", items[0].get("ok") is False, str(items[0]))

    print("[3] 同一个工具两次")
    log = main.StepLog()
    items = feed_all(
        log,
        [
            {"type": "tool", "name": "query_metric", "phase": "start"},
            {"type": "tool", "name": "query_metric", "phase": "end", "ok": True},
            {"type": "tool", "name": "query_metric", "phase": "start"},
            {"type": "tool", "name": "query_metric", "phase": "end", "ok": False},
        ],
    )
    check("两次是两条", len(items) == 2, f"实际 {len(items)} 条")
    if len(items) == 2:
        check("后一条不覆盖前一条的 ok", items[0].get("ok") is True and items[1].get("ok") is False,
              f"{items[0].get('ok')} / {items[1].get('ok')}")
        check("t 单调不减", items[0]["t"] <= items[1]["t"], f"{items[0]['t']} > {items[1]['t']}")


# ── 4/5: 白名单与上限 ─────────────────────────────────────────────────


def t_whitelist() -> None:
    print("[4] 一次性事件与字段白名单")
    log = main.StepLog()
    items = feed_all(
        log,
        [
            {
                "type": "plan",
                "planner": "llm_retry",
                "intent": "value",
                "names": ["规上工业增加值", "固投", "社零", "财政", "外贸", "用电"],
                "period": "2026-09",
                "codes": ["A", "B", "C", "D", "E", "F"],
                # 完整降级对象不该整包落库：界面上只显示「规则解析 / 备用模型」
                "degraded": {"kind": "fallback", "model": "qwen38-27b", "why": "connect refused"},
            }
        ],
    )
    check("plan 产出一条", len(items) == 1, f"实际 {len(items)} 条")
    if not items:
        return
    x = items[0].get("x") or {}
    check("names 截断到 4 个", len(x.get("names") or []) == 4, str(x.get("names")))
    check("降级只留 kind", x.get("dg") == "fallback", str(x.get("dg")))
    blob = json.dumps(items[0], ensure_ascii=False)
    check("不落整包 degraded", "qwen38-27b" not in blob, blob)
    check("不落 codes（界面用不上）", "codes" not in x, str(x))
    check("一次性事件没有 ms（前端据此判「进行中」）", "ms" not in items[0], str(items[0]))

    print("[5] 条数上限")
    log = main.StepLog()
    for _ in range(main._STEP_MAX + 20):
        log.feed({"type": "tool", "name": "list_metrics"})
    check(f"封顶 {main._STEP_MAX} 条", len(log.items) == main._STEP_MAX, f"实际 {len(log.items)} 条")

    print("[5b] 开着的那一步怎么收")
    # 改写成功那条路后端不发 end，紧接着来的是 plan —— 由 plan 收掉它
    log = main.StepLog()
    items = feed_all(
        log,
        [
            {"type": "rewrite", "phase": "start"},
            {"type": "plan", "planner": "llm", "intent": "value", "names": ["固投"]},
        ],
    )
    check("rewrite 被 plan 收掉", items and items[0].get("ms") is not None, str(items))
    check("改写成功记为 ok", bool(items and items[0].get("ok") is True), str(items))
    check("plan 仍是一条", sum(1 for s in items if s.get("k") == "plan") == 1, str(items))

    # 用户点停止：收成「已中断」，不是「进行中」
    log = main.StepLog()
    feed_all(log, [{"type": "tool", "name": "query_metric", "phase": "start"}])
    log.close_open(cut=True)
    check("停止时收尾并标记 cut", log.items and log.items[0].get("cut") is True, str(log.items))
    check("收尾后不再挂着进行中", log.items[0].get("ms") is not None, str(log.items))


# ── 6/7: 落库、不进上下文、两种事件形状 ────────────────────────────────


def t_persist() -> None:
    print("[6] 落库与上下文隔离")
    thread = chat.create(title="steps", owner="tester")
    chat.append(thread["id"], "user", "9 月规上工业增加值多少")

    log = main.StepLog()
    feed_all(
        log,
        [
            {"type": "plan", "planner": "llm", "intent": "value", "names": ["规上工业增加值"]},
            {"type": "experts", "name": "发改委", "index": 1, "total": 3, "phase": "start"},
            {"type": "experts", "name": "发改委", "index": 1, "total": 3, "phase": "end", "ok": True},
        ],
    )
    chat.append(thread["id"], "assistant", "9 月同比增长 7.2%。", meta={"steps": log.items})

    got = chat.get(thread["id"])
    steps = ((got or {}).get("messages") or [{}])[-1].get("meta", {}).get("steps") or []
    check("meta.steps 读得回来", len(steps) == 2, f"实际 {len(steps)} 条")
    check("顺序按发生先后", [s.get("k") for s in steps] == ["plan", "experts"], str([s.get("k") for s in steps]))

    # 关键：这份 meta 只给界面，绝不能出现在回灌给模型的消息里
    ctx = chat.context(thread["id"])
    blob = json.dumps(ctx, ensure_ascii=False)
    check("上下文里没有 steps", "steps" not in blob, blob[:200])
    check("上下文里没有步骤字段名", "planner" not in blob and "发改委" not in blob, blob[:200])
    check("上下文仍然带正文", "7.2%" in blob, blob[:200])


def t_pusher() -> None:
    print("[7] on_step(name, data) → 收集器")
    log = main.StepLog()
    push = main._step_pusher(log)
    push("plan", {"planner": "rules", "intent": "trend", "names": ["固投"]})
    push("tool", {"name": "scan_oversight", "phase": "start"})
    push("tool", {"name": "scan_oversight", "phase": "end", "ok": True})
    check("三个事件 → 两步", len(log.items) == 2, f"实际 {len(log.items)} 条")
    if len(log.items) == 2:
        check("一次性事件先到", log.items[0].get("k") == "plan", str(log.items[0]))
        check("配对成功", log.items[1].get("ms") is not None, str(log.items[1]))
    # 非 step 类型不该进来（start/done/error 是传输层的事）
    push("done", {"result": {}})
    check("done 不入流水", len(log.items) == 2, f"实际 {len(log.items)} 条")


def main_() -> int:
    t_pairing()
    t_whitelist()
    t_persist()
    t_pusher()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
