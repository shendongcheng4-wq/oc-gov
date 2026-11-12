"""问数/参谋 回归题库跑批。

两件事：
1. `--doc`：把 tests/qa_set.json 渲染成 docs/test-questions.md（人看的那份）；
2. 直接跑：逐条打 /api/ask，把答复、工具链、数字闸是否改写、耗时落成一份报告。

题库里只有 `re` / `avoid` 两类机械检查，命中的打 ✓，不命中的打 ✗ —— 它们只用来
快速定位可疑答案，最终还是要人读一遍。反驳类、建议类的「好不好」判不了。

用法：
    python tools/qa_run.py                     # 全量
    python tools/qa_run.py --only A,B          # 只跑 A、B 两组
    python tools/qa_run.py --ids A1,C1         # 只跑指定题号（按文件顺序）
    python tools/qa_run.py --doc               # 重新生成 docs/test-questions.md
    python tools/qa_run.py --model ""          # 用全局默认模型（不钉 deepseek）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SET_PATH = ROOT / "tests" / "qa_set.json"
DOC_PATH = ROOT / "docs" / "test-questions.md"


def load_set() -> dict:
    with SET_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def select(data: dict, only: str | None, ids: str | None) -> list[dict]:
    rows = data["questions"]
    if only:
        keys = [x.strip().upper() for x in only.split(",") if x.strip()]
        rows = [q for q in rows if q["group"][:1].upper() in keys or q["id"][0].upper() in keys]
    if ids:
        want = [x.strip().upper() for x in ids.split(",") if x.strip()]
        order = {qid: i for i, qid in enumerate(want)}
        rows = [q for q in rows if q["id"].upper() in order]
        rows.sort(key=lambda q: order[q["id"].upper()])
    return rows


def check(row: dict, text: str) -> list[dict]:
    """跑机械检查。返回 [{ok, hint, pattern, kind}]。"""
    out: list[dict] = []
    for item in row.get("checks") or []:
        if "re" in item:
            hit = re.search(item["re"], text) is not None
            out.append(
                {"ok": hit, "kind": "re", "pattern": item["re"], "hint": item.get("hint", "")}
            )
        elif "avoid" in item:
            hit = re.search(item["avoid"], text) is not None
            out.append(
                {"ok": not hit, "kind": "avoid", "pattern": item["avoid"], "hint": item.get("hint", "")}
            )
    return out


def ask(base: str, row: dict, thread_id: str | None, model: str, timeout: int) -> dict:
    body: dict = {"question": row["q"], "model_ref": model}
    if thread_id:
        body["thread_id"] = thread_id
    if row.get("attachments"):
        body["attachments"] = row["attachments"]

    req = urllib.request.Request(
        base.rstrip("/") + "/api/ask",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        elapsed = time.time() - started
        payload = json.loads(raw)
    except urllib.error.HTTPError as exc:
        return {
            "elapsed": round(time.time() - started, 1),
            "error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}",
            "text": "",
            "thread_id": thread_id,
            "tools": [],
            "planner": "",
            "note": "",
            "degraded": None,
        }
    except Exception as exc:  # noqa: BLE001 网络/超时/JSON 都算一条失败，不让整批断掉
        return {
            "elapsed": round(time.time() - started, 1),
            "error": f"{type(exc).__name__}: {exc}",
            "text": "",
            "thread_id": thread_id,
            "tools": [],
            "planner": "",
            "note": "",
            "degraded": None,
        }

    agent = payload.get("agent") or {}
    notes = payload.get("notes") or []
    return {
        "elapsed": round(elapsed, 1),
        "error": None,
        "thread_id": payload.get("thread_id") or thread_id,
        "text": payload.get("headline") or "",
        "planner": payload.get("planner") or "",
        "tools": agent.get("tools") or [],
        "rounds": agent.get("rounds"),
        "note": " / ".join(str(n) for n in notes[:2]),
        "degraded": payload.get("degraded"),
        "numbers": payload.get("numbers") or [],
        "sources": [s.get("code") for s in (payload.get("sources") or [])],
        "intent": payload.get("intent"),
    }


def render_doc(data: dict) -> str:
    meta = data.get("meta") or {}
    lines = [
        "# 测试问题（问数 / 参谋）",
        "",
        f"- 数据快照：{meta.get('data_snapshot', '')}，最新可得期：{meta.get('latest_period', '')}",
        f"- 期望值按快照写，指标数据变了要同步改 `tests/qa_set.json` 里的 numbers。",
        f"- 跑批：`{meta.get('howto', 'python tools/qa_run.py')}`；本文件由 `python tools/qa_run.py --doc` 生成，别手改。",
        "",
    ]
    current = None
    for row in data["questions"]:
        if row["group"] != current:
            current = row["group"]
            lines += [f"## {current}", ""]
        lines += [f"### {row['id']}", "", f"**问**：{row['q']}", "", f"**期望**：{row['expect']}", ""]
        checks = row.get("checks") or []
        if checks:
            lines.append("**机器判据**：")
            lines.append("")
            for c in checks:
                if "re" in c:
                    lines.append(f"- 应命中 `{c['re']}` —— {c.get('hint', '')}")
                else:
                    lines.append(f"- 不应命中 `{c['avoid']}` —— {c.get('hint', '')}")
            lines.append("")
        if row.get("thread"):
            lines.append(f"**会话**：`{row['thread']}`（与同组同 key 的题连着问，测指代）")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="deepseek/deepseek-chat", help='留空用全局默认')
    ap.add_argument("--only", default=None, help="A,B 只跑这些组")
    ap.add_argument("--ids", default=None, help="A1,C1 只跑这些题号")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", default=str(ROOT / "data" / "_qa"))
    ap.add_argument("--doc", action="store_true", help="只重新生成 docs/test-questions.md")
    args = ap.parse_args()

    data = load_set()

    if args.doc:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(render_doc(data), encoding="utf-8")
        print(f"已生成 {DOC_PATH}")
        return 0

    rows = select(data, args.only, args.ids)
    if not rows:
        print("没有匹配的题目，检查 --only / --ids")
        return 2

    threads: dict[str, str] = {}
    results: list[dict] = []
    print(f"共 {len(rows)} 题，目标 {args.base}，模型 {args.model or '(全局默认)'}\n")

    for i, row in enumerate(rows, 1):
        key = row.get("thread")
        thread_id = threads.get(key) if key else None
        res = ask(args.base, row, thread_id, args.model, args.timeout)
        if key and res.get("thread_id"):
            threads[key] = res["thread_id"]

        checks = check(row, res.get("text") or "")
        bad = [c for c in checks if not c["ok"]]
        mark = "ERR" if res["error"] else ("OK " if not bad else f"x{len(bad)} ")
        print(f"[{i:02d}/{len(rows)}] {row['id']:<3} {mark} {res['elapsed']:>6.1f}s  {row['q']}")
        if res["error"]:
            print(f"        ! {res['error']}")
        for c in bad:
            print(f"        x {c['hint']}（{c['kind']}: {c['pattern']}）")
        if res.get("note"):
            print(f"        note: {res['note']}")
        if res.get("degraded"):
            print(f"        degraded: {json.dumps(res['degraded'], ensure_ascii=False)[:160]}")

        results.append({"row": row, "res": res, "checks": checks})

    # ---- 落报告 ----
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    md_path = out_dir / f"qa-{stamp}.md"
    json_path = out_dir / f"qa-{stamp}.json"

    total = len(results)
    errs = sum(1 for r in results if r["res"]["error"])
    fails = sum(1 for r in results if any(not c["ok"] for c in r["checks"]))
    dur = sum(r["res"]["elapsed"] for r in results)

    lines = [
        f"# 问数回归报告 {stamp}",
        "",
        f"- 题数 {total}（HTTP 失败 {errs}，有检查未命中 {fails}）",
        f"- 模型 `{args.model or '全局默认'}`，耗时合计 {dur:.0f}s",
        "",
        "| 题 | 问句 | 耗时 | 工具 | 未命中 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in results:
        miss = "、".join(c["hint"] for c in r["checks"] if not c["ok"]) or "—"
        tools = ",".join(t.replace("tool.", "") for t in r["res"].get("tools") or []) or "—"
        q = r["row"]["q"].replace("|", "\\|")
        lines.append(f"| {r['row']['id']} | {q} | {r['res']['elapsed']}s | {tools} | {miss} |")
    lines.append("")

    for r in results:
        row, res = r["row"], r["res"]
        lines += [f"## {row['id']}　{row['q']}", ""]
        if res["error"]:
            lines += [f"**请求失败**：{res['error']}", ""]
            continue
        lines += [res["text"] or "（空答复）", ""]
        meta_bits = [f"planner={res['planner']}", f"tools={len(res.get('tools') or [])}"]
        if res.get("degraded"):
            meta_bits.append("**degraded**")
        lines += [f"`{' | '.join(meta_bits)}`", ""]
        if res.get("note"):
            lines += [f"备注：{res['note']}", ""]
        lines += [f"期望：{row['expect']}", ""]
        if r["checks"]:
            lines += [
                "检查：" + "；".join(("✓" if c["ok"] else "✗") + c["hint"] for c in r["checks"]),
                "",
            ]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            [
                {
                    "id": r["row"]["id"],
                    "group": r["row"]["group"],
                    "q": r["row"]["q"],
                    "expect": r["row"]["expect"],
                    "answer": r["res"]["text"],
                    "tools": r["res"].get("tools"),
                    "planner": r["res"].get("planner"),
                    "note": r["res"].get("note"),
                    "degraded": r["res"].get("degraded"),
                    "elapsed": r["res"]["elapsed"],
                    "error": r["res"]["error"],
                    "checks": r["checks"],
                }
                for r in results
            ],
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    print(f"\n报告：{md_path}")
    print(f"原始：{json_path}")
    print(f"合计 {total} 题，HTTP 失败 {errs}，检查未全中 {fails}，耗时 {dur:.0f}s")
    return 0 if errs == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
