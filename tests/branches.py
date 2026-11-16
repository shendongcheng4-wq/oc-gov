"""答案分支：同一问可以留几版答案。

跑法::

    .venv\\Scripts\\python.exe tests\\branches.py

这件事的难点不在"能留两版"，而在**每一处读消息的地方都要按同一套规则挑一版**：
上下文、回放、侧栏条数、导出。少一处，用户就会看到"屏幕上写着 7.4%，
模型追问时却说 7.2%"，而且没有任何报错。

断言十二件事：

1. 老数据（没有 reply_to）折出来的轮次和逐条读的顺序一致 —— 升级不改上下文；
2. 同一问两版 → 只有选中的那版进上下文，另一版一个字都不进；
3. 换版之后上下文跟着换；
4. picks 指向一条已经不存在的答案 → 退回最后一版，不丢轮次（手改过文件也不炸）；
5. 只有最后一轮能换版 —— 中间轮换了，后面几轮就是照着另一版答案说的；
6. 别人的会话换不了（和"不存在"同样处理）；
7. 换版不删消息、不重复记问句：问句数不变，库里的版数增加；
8. 版数封顶，超了丢最旧的，且最新那版一定是选中的；
9. `drop_tail` 摘掉末尾一轮之后，轮数/用量统计跟着变（不是只把内容藏起来）；
10. 摘掉的那一轮带对照、上一轮也带 → 仍回灌上一轮那张表，而不是一起丢掉；
11. "已停止生成"占位也是一版：重答之后仍能切回去，且切回去时不会把另一版的
    数值带进上下文；
12. 没有问句的 assistant（导入、脚本写入）自成一轮，不当成"同一问的几版"。

失败即非零退出。数据写在 data/_test_branches，不碰真实会话。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "core"))

# 必须先定数据目录再 import store 系：晚一步就会往真实 chats.json 里写测试会话
TEST_DATA = ROOT / "data" / "_test_branches"
if not os.environ.get("OCGOV_TEST_KEEP"):
    shutil.rmtree(TEST_DATA, ignore_errors=True)
os.environ["OCGOV_DATA"] = str(TEST_DATA)

import chat  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok  {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


def new_thread(owner: str = "zhang") -> dict:
    return chat.create(model_ref="m/x", owner=owner)


def say(tid: str, text: str) -> str:
    return chat.append(tid, "user", text)["id"]


def answer(tid: str, text: str, meta: dict | None = None, reply_to: str | None = None) -> dict:
    return chat.append(tid, "assistant", text, meta=meta, reply_to=reply_to)


def ctx_of(tid: str, **kw) -> list[dict]:
    return chat.context(tid, **kw)


def texts(history: list[dict]) -> list[str]:
    return [h["content"] for h in history]


# ── 1: 老数据折轮次 ────────────────────────────────────────────────────


def t_legacy() -> None:
    print("[1] 没有 reply_to 的老数据")
    th = new_thread()
    tid = th["id"]
    # 手工造一份"升级前"的消息：只有 role/content/id/seq，没有 reply_to
    msgs = [
        {"id": "u1", "seq": 1, "role": "user", "content": "9月规上工业增加值增速是多少"},
        {"id": "a1", "seq": 2, "role": "assistant", "content": "7.2%", "meta": {"intent": "value"}},
        {"id": "u2", "seq": 3, "role": "user", "content": "那上个月呢"},
        {"id": "a2", "seq": 4, "role": "assistant", "content": "6.9%", "meta": {"intent": "value"}},
    ]

    def _do(cfg):
        cfg["threads"][0]["messages"] = msgs
        return cfg

    import store

    store.update(chat.FILE, _do, chat._empty())

    hist = texts(ctx_of(tid))
    check(
        "老数据按位置折成两轮",
        hist == ["9月规上工业增加值增速是多少", "7.2%", "那上个月呢", "6.9%"],
        str(hist),
    )
    vis = chat.visible(chat.get(tid))
    check("可见消息与库存一致", [m["id"] for m in vis] == ["u1", "a1", "u2", "a2"])
    check("一版的时候不塞 alts", all("alts" not in m for m in vis))


# ── 2/3/7: 两版、换版、条数 ────────────────────────────────────────────


def t_two_versions() -> dict:
    print("[2/3/7] 同一问两版")
    th = new_thread()
    tid = th["id"]
    uid = say(tid, "9月规上工业增加值增速是多少")
    a1 = answer(tid, "A版：7.2%", meta={"intent": "value", "metric_names": ["规上工业增加值增速"]})
    a2 = answer(tid, "B版：7.4%", meta={"intent": "value", "metric_names": ["规上工业增加值增速"]})

    check("新答案自动挂到最近那条问句", a1.get("reply_to") == uid and a2.get("reply_to") == uid)

    hist = texts(ctx_of(tid))
    check("上下文只有最新那一版", "B版：7.4%" in hist and "A版：7.2%" not in hist, str(hist))
    check("问句只回灌一次", hist.count("9月规上工业增加值增速是多少") == 1, str(hist))

    vis = chat.visible(chat.get(tid))
    check("可见消息两条（问句 + 一版答案）", [m["role"] for m in vis] == ["user", "assistant"], str(len(vis)))
    alts = vis[1].get("alts") or {}
    check("可见答案带 alts 2/2", (alts.get("n"), alts.get("i")) == (2, 2), str(alts))
    check("alts 里带各版 id（切换时不必再问一次）", len(alts.get("ids") or []) == 2, str(alts.get("ids")))

    got = chat.pick_answer(tid, a1["id"])
    check("换版成功", bool(got and got.get("ok")), str(got))
    hist = texts(ctx_of(tid))
    check("换版后上下文换成 A 版", "A版：7.2%" in hist and "B版：7.4%" not in hist, str(hist))
    vis = chat.visible(chat.get(tid))
    check("换版后 alts 变成 1/2", (vis[1].get("alts") or {}).get("i") == 1, str(vis[1].get("alts")))

    raw = chat.get(tid)
    check("两版都还在库里（没删）", len([m for m in raw["messages"] if m["role"] == "assistant"]) == 2)
    check("问句只有一条（没重复记）", len([m for m in raw["messages"] if m["role"] == "user"]) == 1)

    # 侧栏：条数按可见算，不按库存
    row = next(x for x in chat.list_threads(owner="zhang") if x["id"] == tid)
    check("侧栏条数按可见算", row["message_count"] == 2, str(row["message_count"]))
    check("侧栏预览是最新一版", row["last_preview"].startswith("A版"), row["last_preview"])
    return {"tid": tid, "uid": uid, "a1": a1["id"], "a2": a2["id"]}


# ── 4: picks 指向不存在的消息 ──────────────────────────────────────────


def t_bad_pick() -> None:
    print("[4] picks 指向一条已经不存在的答案")
    th = new_thread()
    tid = th["id"]
    say(tid, "固定资产投资增速")
    answer(tid, "第一版")
    answer(tid, "第二版")

    import store

    def _do(cfg):
        t = next(x for x in cfg["threads"] if x["id"] == tid)
        for m in t["messages"]:
            if m["role"] == "user":
                t["picks"] = {m["id"]: "m_不存在"}
        return cfg

    store.update(chat.FILE, _do, chat._empty())
    hist = texts(ctx_of(tid))
    check("退回最后一版而不是空", hist == ["固定资产投资增速", "第二版"], str(hist))
    check("轮次没丢", chat.visible(chat.get(tid))[1]["content"] == "第二版")


# ── 5: 只有最后一轮能换版 ──────────────────────────────────────────────


def t_only_last(env: dict) -> None:
    print("[5] 只有最后一轮能换版")
    tid = env["tid"]
    say(tid, "那上个月呢")
    answer(tid, "第二轮答案")
    got = chat.pick_answer(tid, env["a1"])
    check("中间轮换版被拒", got is None, str(got))
    vis = chat.visible(chat.get(tid))
    check("中间轮仍显示原来那版", vis[1]["content"].startswith("A版"), vis[1]["content"])
    check("最后一轮还能换", bool(chat.pick_answer(tid, vis[3]["id"])))


# ── 6: 别人的会话 ──────────────────────────────────────────────────────


def t_owner(env: dict) -> None:
    print("[6] 别人的会话换不了")
    got = chat.pick_answer(env["tid"], env["a2"], owner="li")
    check("换别人的会话返回 None（等同不存在）", got is None, str(got))
    # 换自己的：得拿**最后一轮**里的那一版（上一组已经又加了一轮）
    last = [m for m in chat.visible(chat.get(env["tid"])) if m["role"] == "assistant"][-1]
    check("自己换得到", bool(chat.pick_answer(env["tid"], last["id"], owner="zhang")))


# ── 8: 版数封顶 ────────────────────────────────────────────────────────


def t_cap() -> None:
    print("[8] 版数封顶")
    th = new_thread()
    tid = th["id"]
    say(tid, "12345 办结率")
    ids = [answer(tid, f"第 {i} 版")["id"] for i in range(chat.MAX_ANSWERS + 3)]
    raw = chat.get(tid)
    live = [m for m in raw["messages"] if m["role"] == "assistant"]
    check(f"最多留 {chat.MAX_ANSWERS} 版", len(live) == chat.MAX_ANSWERS, str(len(live)))
    check("丢的是最旧的", ids[0] not in [m["id"] for m in live] and ids[-1] in [m["id"] for m in live])
    vis = chat.visible(raw)
    check("最新的那版是选中的", vis[1]["content"] == f"第 {chat.MAX_ANSWERS + 2} 版", vis[1]["content"])
    check("alts 计数等于实际版数", (vis[1].get("alts") or {}).get("n") == chat.MAX_ANSWERS)


# ── 9/10: drop_tail 与对照摘要 ─────────────────────────────────────────


def _ext(n_same: int = 2) -> dict:
    return {
        "files": ["9月月报.xlsx"],
        "metrics": [{
            "name": "规上工业增加值增速", "unit": "%",
            "rows": [
                {"period": "2026-08", "label": "2026年8月", "external": 6.9, "internal": 6.9, "state": "same"},
                {"period": "2026-09", "label": "2026年9月", "external": 7.2, "internal": 7.3, "state": "diff"},
            ][: n_same + 1],
        }],
    }


def t_drop_tail() -> None:
    print("[9/10] drop_tail 与对照摘要")
    th = new_thread()
    tid = th["id"]
    say(tid, "对一下这份月报")
    answer(tid, "对照结果见下方。", meta={"external": _ext()})
    say(tid, "哪期差最多")
    answer(tid, "9月差 0.1 个点。")

    st = {}
    hist = ctx_of(tid, out=st)
    check("不摘时四轮两条问句", len([t for t in hist if t["role"] == "user"]) == 2)
    check("不摘时两条都没有对照摘要", not any("外部数据对照" in t["content"] for t in hist))

    st2 = {}
    hist2 = ctx_of(tid, out=st2, drop_tail=2)
    check("摘掉末尾一轮后只剩一问", len([t for t in hist2 if t["role"] == "user"]) == 1, str(len(hist2)))
    check("轮数统计跟着变", st2["turns_total"] == st["turns_total"] - 1 and st2["turns_used"] == st["turns_used"] - 1,
          f'{st["turns_total"]}/{st["turns_used"]} → {st2["turns_total"]}/{st2["turns_used"]}')
    # 报出来的用量要等于**实际发出去的那些字**，不能是"摘之前的账单" ——
    # 这类偏差在界面上完全看不出来，只会让"上下文占了多少"这笔账悄悄失真
    want = sum(chat._est_tokens(t["content"]) + chat._MESSAGE_FRAMING_TOKENS for t in hist2)
    check("token 数与实际发出的内容对得上", st2["tokens"] == want, f'{st2["tokens"]} vs {want}')
    check(
        "摘掉之后仍回灌上一轮那张表",
        any("外部数据对照" in t["content"] for t in hist2),
        str([t["content"][:20] for t in hist2]),
    )
    check("被摘掉那一轮的话一个字都不在", not any("0.1 个点" in t["content"] for t in hist2))

    # 反过来：最后一轮没有对照，就不该回灌旧表（老契约，别被这次改动带偏）
    th2 = new_thread()
    say(th2["id"], "对一下这份月报")
    answer(th2["id"], "对照结果见下方。", meta={"external": _ext()})
    say(th2["id"], "你好")
    answer(th2["id"], "您好，可以问我指标和督办事项。")
    hist3 = ctx_of(th2["id"])
    check(
        "最后一轮没对照就不回灌旧表",
        not any("外部数据对照" in t["content"] for t in hist3),
        str([t["content"][:20] for t in hist3]),
    )

    # 上一轮失败、只落了问句的情况：重试时只摘那一条。摘 2 条的话会把**再上
    # 一轮**的问句摘成孤儿，模型会看到一句没有回答的提问（以前 history[:-1] 就对）。
    th3 = new_thread()
    say(th3["id"], "第一问")
    answer(th3["id"], "第一答")
    say(th3["id"], "第二问（这一轮失败了，没有答案）")
    h1 = ctx_of(th3["id"], drop_tail=1)
    check(
        "只摘 1 条时上一轮仍成对",
        [x["role"] for x in h1] == ["user", "assistant"],
        str([x["role"] for x in h1]),
    )


# ── 11: 已停止占位也是一版 ─────────────────────────────────────────────


def t_cancelled() -> None:
    print("[11] 已停止占位也是一版")
    th = new_thread()
    tid = th["id"]
    say(tid, "全市 GDP 是多少")
    stop = answer(
        tid, "已停止生成，这一轮没有产出答案。",
        meta={"cancelled": True, "steps": [{"k": "plan", "t": 0, "ms": 120, "ok": True, "x": {}}]},
    )
    good = answer(tid, "2026年上半年 GDP 3200 亿元。")
    vis = chat.visible(chat.get(tid))
    check("停止占位与答案是一问的两版", (vis[1].get("alts") or {}).get("n") == 2, str(vis[1].get("alts")))
    check("默认显示后落的那版答案", vis[1]["id"] == good["id"])

    got = chat.pick_answer(tid, stop["id"])
    check("能切回「已停止」那一版", bool(got and got.get("ok")), str(got))
    hist = texts(ctx_of(tid))
    # 占位那条有正文（「已停止生成…」），所以原样回灌的是它，而不是 _summary 的
    # 模板；两种写法都不带数值，关键是**别把上一版答案的数带进去**
    check(
        "切回停止版时不把另一版的数值带进上下文",
        any("已停止" in t for t in hist) and not any("3200" in t for t in hist),
        str(hist),
    )
    check("占位的阶段流水没丢", len(chat.get(tid)["messages"][-2].get("meta", {}).get("steps") or []) == 1)


# ── 12: 没有问句的 assistant ───────────────────────────────────────────


def t_orphan() -> None:
    print("[12] 没有问句的 assistant 自成一轮")
    th = new_thread()
    tid = th["id"]
    answer(tid, "导入进来的一条答案，前面没有问句")
    answer(tid, "还有一条")
    vis = chat.visible(chat.get(tid))
    check("两条都在（不当成同一问的两版）", len(vis) == 2, str(len(vis)))
    check("也不带 alts", all("alts" not in m for m in vis))
    check("上下文两条都回灌", len(ctx_of(tid)) == 2)


def main() -> int:
    print(f"数据目录 {TEST_DATA}")
    t_legacy()
    env = t_two_versions()
    t_bad_pick()
    t_only_last(env)
    t_owner(env)
    t_cap()
    t_drop_tail()
    t_cancelled()
    t_orphan()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for name in FAILS:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
