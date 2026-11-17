"""政策时效：知识库里那份文件现在还算不算数。

跑法::

    .venv\\Scripts\\python.exe tests\\policy_validity.py

政务知识库和通用知识库的分水岭就在这条上：通用库管「有没有」，政务库还得管
「还算不算数」。文档进库之后全文检索永远命中，于是模型可以理直气壮地引用一份
2019 年就废止的文件 —— 这不是模型在编，是知识库过期了，而界面上完全看不出来。

断言九件事：

1. 日期写法归一（2023年5月1日 / 2023/5/1 / 20230501 → ISO），写错的报错；
2. 四种状态：现行有效 / 已废止 / 尚未施行 / 未标时效；**废止当天起算失效**；
3. 上传时就能带时效；废止早于施行要当场拒绝，且**不留下半份文档**；
4. 改时效只动传进来的字段（``""`` 清空 ≠ ``None`` 不动）；
5. 检索把废止文件降权而不是剔除 —— 用户问的可能就是那份旧文件；
6. 命中结果自带时效，依据条目把它写进备注；
7. 阅读视图（doc_view）带时效与文号，详情页才能就地改；
8. 清单条目带 validity，前端据此画「已废止」红标；
9. 老数据（没有时效字段）一律报「未标时效」，**不默认有效**。

失败即非零退出。数据写在 data/_test_validity，不碰真实知识库。
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "metrics"))
sys.path.insert(0, str(ROOT / "core" / "oversight"))
sys.path.insert(0, str(ROOT / "core" / "meeting"))

TEST_DATA = ROOT / "data" / "_test_validity"
shutil.rmtree(TEST_DATA, ignore_errors=True)
os.environ["OCGOV_DATA"] = str(TEST_DATA)

import rag  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok  {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


DOC_A = "第一条 本办法适用于全市范围内的行政审批事项。\n第二条 办理时限为五个工作日。\n"
DOC_B = DOC_A  # 同样的正文，只有时效不同 —— 比较才干净


def t_dates() -> None:
    print("[1] 日期归一")
    cases = {
        "2023年5月1日": "2023-05-01",
        "2023/5/1": "2023-05-01",
        "2023.5.1": "2023-05-01",
        "20230501": "2023-05-01",
        "2023-05-01": "2023-05-01",
        "  ": "",
        None: "",
    }
    bad = []
    for raw, want in cases.items():
        try:
            got = rag.norm_date(raw)
        except ValueError:
            got = "<raise>"
        if got != want:
            bad.append(f"{raw!r}→{got!r}(期望 {want!r})")
    check("各种写法都归一", not bad, "；".join(bad))
    for junk in ("2023年13月1日", "去年", "2023-02-30"):
        try:
            rag.norm_date(junk)
            check(f"拒绝非法日期 {junk}", False)
        except ValueError:
            check(f"拒绝非法日期 {junk}", True)


def t_states() -> None:
    print("[2] 四种状态")
    today = date(2026, 11, 17)
    v = rag.validity({"effective_from": "2020-01-01"}, today)
    check("有施行日无废止日 → 现行有效", v["state"] == "valid" and v["label"] == "现行有效", str(v))
    v = rag.validity({"effective_to": "2023-05-01", "superseded_by": "某政发〔2023〕7 号"}, today)
    check("过了废止日 → 已废止", v["state"] == "expired" and v["stale"], str(v))
    check("废止说明带替代文件", "某政发〔2023〕7 号" in v["detail"], v["detail"])
    # 边界：废止日期当天起视为失效。差一天错在这里最难发现 —— 界面上只是
    # 「现行有效」和「已废止」的差别，而人不会拿日历去核
    v = rag.validity({"effective_to": "2026-11-17"}, today)
    check("废止当天即失效（边界）", v["state"] == "expired", str(v))
    v = rag.validity({"effective_to": "2026-11-18"}, today)
    check("废止前一天仍有效", v["state"] == "valid", str(v))
    v = rag.validity({"effective_from": "2027-01-01"}, today)
    check("还没到施行日 → 尚未施行", v["state"] == "upcoming" and not v["stale"], str(v))
    v = rag.validity({}, today)
    check("什么都没标 → 未标时效", v["state"] == "unknown" and not v["stale"], str(v))
    check("未标时效不等于有效", v["label"] != "现行有效", v["label"])


def make_base(name: str) -> str:
    base = rag.create_base(name, "测试库")
    return base["id"]


def upload(kb: str, name: str, **kw) -> dict:
    return rag.upload_document(kb, name, DOC_A.encode("utf-8"), **kw)


def t_upload() -> None:
    print("[3] 上传时带时效")
    kb = make_base("上传测试")
    doc = upload(kb, "旧办法.txt", effective_from="2019-03-01",
                 effective_to="2023-05-01", superseded_by="某政发〔2023〕7 号",
                 doc_no="某政发〔2019〕2 号")
    check("施行日入库", doc.get("effective_from") == "2019-03-01", str(doc))
    check("废止日入库", doc.get("effective_to") == "2023-05-01")
    check("文号入库", doc.get("doc_no") == "某政发〔2019〕2 号")
    check("替代文件入库", doc.get("superseded_by") == "某政发〔2023〕7 号")
    v = rag.validity(doc)
    check("这份文件判成已废止", v["state"] == "expired")

    before = len(rag.list_documents(kb))
    try:
        upload(kb, "填反了.txt", effective_from="2024-01-01", effective_to="2023-01-01")
        check("废止早于施行要拒绝", False)
    except ValueError:
        check("废止早于施行要拒绝", True)
    check("被拒绝的没有留下半份文档", len(rag.list_documents(kb)) == before,
          f"{before}→{len(rag.list_documents(kb))}")

    plain = upload(kb, "没标时效.txt")
    check("不传时效就不写字段", "effective_to" not in plain and "effective_from" not in plain,
          str(plain))
    check("不传时效判成未标", rag.validity(plain)["state"] == "unknown")


def t_set() -> None:
    print("[4] 改时效")
    kb = make_base("改期测试")
    doc = upload(kb, "待定.txt")
    got = rag.set_validity(kb, doc["id"], effective_from="2024年1月1日")
    check("只改施行日", (got or {}).get("effective_from") == "2024-01-01", str(got))
    check("没传的字段不动（没有被抹成空）", "effective_to" not in (got or {}), str(got))
    got = rag.set_validity(kb, doc["id"], effective_to="2025-06-30")
    check("补上废止日", (got or {}).get("effective_to") == "2025-06-30")
    check("施行日还在", (got or {}).get("effective_from") == "2024-01-01")
    got = rag.set_validity(kb, doc["id"], effective_to="")
    check("空串是清空", "effective_to" not in (got or {}), str(got))
    got = rag.set_validity(kb, doc["id"], superseded_by="新办法")
    check("只改替代文件不影响日期", (got or {}).get("effective_from") == "2024-01-01")
    try:
        rag.set_validity(kb, doc["id"], effective_to="2020-01-01")
        check("改期也会校验先后", False)
    except ValueError:
        check("改期也会校验先后", True)
    check("改不到的文件返回 None",
          rag.set_validity(kb, "doc_不存在", effective_from="2024-01-01") is None)


def t_search() -> None:
    print("[5] 检索：降权不剔除")
    kb = make_base("检索测试")
    old = upload(kb, "旧办法.txt", effective_from="2019-03-01", effective_to="2023-05-01")
    new = upload(kb, "新办法.txt", effective_from="2023-05-02")
    hits = rag.search([kb], "行政审批事项办理时限", k=6)
    by_doc: dict[str, dict] = {}
    for h in hits:
        by_doc.setdefault(h["doc_id"], h)
    check("两份文件都召回（没把旧文件剔掉）", {old["id"], new["id"]} <= set(by_doc),
          str([h["doc_name"] for h in hits]))
    if old["id"] in by_doc and new["id"] in by_doc:
        check("现行文件排在废止文件之前",
              hits.index(by_doc[new["id"]]) < hits.index(by_doc[old["id"]]),
              f'{by_doc[new["id"]]["score"]:.3f} vs {by_doc[old["id"]]["score"]:.3f}')
        check("废止的那条分数被压低",
              by_doc[old["id"]]["score"] < by_doc[new["id"]]["score"])
    check("命中自带时效", (by_doc[old["id"]].get("validity") or {}).get("state") == "expired",
          str(by_doc[old["id"]].get("validity")))
    check("原始分也留着（排错时要能看出是降权还是本来就不相关）",
          "raw_score" in by_doc[old["id"]])


def t_refs() -> None:
    print("[6] 依据条目")
    import ask  # core/metrics/ask.py
    hit = {
        "kb_id": "kb_x", "kb_name": "政策库", "doc_id": "doc_x",
        "doc_name": "旧办法", "ordinal": 2, "text": "原文",
        "score": 0.5, "method": "bigram",
        "validity": rag.validity({"effective_to": "2023-05-01", "superseded_by": "新办法"}),
    }
    ref = ask._doc_ref(hit)
    check("条目带 validity", (ref.get("validity") or {}).get("state") == "expired", str(ref.get("validity")))
    check("备注里写明已废止", "已废止" in str(ref["source"]["note"]), ref["source"]["note"])
    check("段号仍在备注里", "第 3 段" in str(ref["source"]["note"]), ref["source"]["note"])

    ok_hit = dict(hit, validity=rag.validity({"effective_from": "2024-01-01"}))
    ref2 = ask._doc_ref(ok_hit)
    check("现行文件的备注不加噪音", str(ref2["source"]["note"]) == "第 3 段", ref2["source"]["note"])


def t_view() -> None:
    print("[7] 阅读视图 / [8] 清单条目")
    kb = make_base("视图测试")
    doc = upload(kb, "旧办法.txt", effective_from="2019-03-01", effective_to="2023-05-01",
                 doc_no="某政发〔2019〕2 号")
    view = rag.doc_view(kb, doc["id"])
    check("doc_view 带时效", (view["doc"].get("validity") or {}).get("state") == "expired")
    check("doc_view 带文号", view["doc"].get("doc_no") == "某政发〔2019〕2 号")
    check("doc_view 带原始日期（详情页要能就地改）",
          view["doc"].get("effective_to") == "2023-05-01")

    import main
    docs = rag.browse_documents()
    row = next(d for d in docs if d["id"] == doc["id"])
    entry = main._kb_doc_entry(row, "正文")
    check("清单条目带 validity", (entry.get("validity") or {}).get("state") == "expired")
    check("清单备注里能看见已废止", "已废止" in str(entry["source"]["note"]),
          entry["source"]["note"])
    check("清单备注里带文号", "某政发〔2019〕2 号" in str(entry["source"]["note"]))
    import knowledge
    kept = knowledge.to_meta([entry])[0]
    check("落库裁剪保留 validity", (kept.get("validity") or {}).get("state") == "expired")


def t_answer() -> None:
    """资料库兜底那条链路：命中废止文件时必须自己说出来。

    这里直接调 `ask.ask`（不经对话层），因为要验的是**装配**：检索结果里的
    时效有没有跟着带进 rag 条目、答复有没有写"不作现行依据"。对话层会先让
    模型挑工具，那一步与本次改动无关。
    """
    print("[9] 问答兜底：命中废止文件要说出来")
    import ask
    kb = make_base("问答测试")
    rag.upload_document(kb, "溧阳市行政审批事项办理办法.txt", DOC_A.encode("utf-8"),
                        effective_from="2019-03-01", effective_to="2023-04-30",
                        superseded_by="溧政发〔2023〕7 号")
    got = ask.ask("溧阳市行政审批事项办理办法第二条", person_id="mayor")
    check("走到资料库兜底分支", got.get("intent") == "rag", str(got.get("intent")))
    rows = got.get("rag") or []
    # 检索跨全部库，别的测试库里有同样正文的文件也会被召回 —— 所以看的是
    # 「每条都带时效」+「其中确有废止的」，不是只看第一条
    check("每条命中都带 validity", bool(rows) and all("validity" in r for r in rows),
          str(rows[:1]))
    check("命中里确有已废止的那条",
          any((r.get("validity") or {}).get("state") == "expired" for r in rows),
          str([(r["doc_name"], (r.get("validity") or {}).get("state")) for r in rows]))
    notes = " ".join(str(n) for n in (got.get("notes") or []))
    check("答复里写明已废止", "已废止" in notes, notes[:60])
    check("没有说成检索失败", "没有" not in notes or "不作" in notes, notes[:60])


def main_() -> int:
    for fn in (t_dates, t_states, t_upload, t_set, t_search, t_refs, t_view, t_answer):
        fn()
    print()
    if FAILS:
        print(f"有失败：{len(FAILS)} 项 → " + "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
