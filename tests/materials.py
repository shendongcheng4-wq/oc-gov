"""会话材料库（core/materials.py）的结构约束。

跑法::

    .venv\\Scripts\\python.exe tests\\materials.py

断言九件事：

1. 存进去能按 id 取回原文，id 形如 `mat_xxxxxxxxxxxx`；
2. **同文去重**：同一身份同一段正文存两次是同一份；不同身份、不同正文都是两份；
3. **按身份隔离**：拿别人的 id 取，返回 None（不是抛异常、也不是返回内容）；
4. `get_many` 保持传入顺序，取不到的跳过而不是占位；
5. 索引在、正文被删（手工清过目录）→ 当作不存在，不让调用方拿到空正文；
6. `forget` 同时清索引和正文，别的身份删不动；
7. **正文不进 chats.json**：会话只记 id —— 这条边界比功能本身要紧，
   一份一万多字的材料被每轮历史重复带走，几十轮就能把窗口吃干净；
8. `attach_materials` 的去重 / 摘除 / 封顶 / 挤掉最旧；
9. 前端契约：上传完拿 id、发送只发 id、切会话按服务端清单重建芯片。

失败即非零退出。数据写在 data/_test_materials，不碰真实材料与会话。
"""

from __future__ import annotations

import json
import os
import re
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
# 晚一步就会往真实的 materials.json 里写测试材料。
TEST_DATA = ROOT / "data" / "_test_materials"
if not os.environ.get("OCGOV_TEST_KEEP"):
    shutil.rmtree(TEST_DATA, ignore_errors=True)
os.environ["OCGOV_DATA"] = str(TEST_DATA)

import chat  # noqa: E402
import main  # noqa: E402
import materials  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok  {name}")
        return
    FAILS.append(name)
    print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


# ── 1: 存取往返 ──────────────────────────────────────────────────────────
print("[1] 存取往返")


def t_roundtrip() -> None:
    body = "期次\t指标\t数值\n2026-09\t规上工业增加值增速\t8.9\n"
    saved = materials.save("mayor", "9月月报.tsv", body, kind="tsv")
    check("返回 id 且形如 mat_xxxx", bool(re.fullmatch(r"mat_[0-9a-f]{12}", saved.get("id") or "")),
          str(saved.get("id")))
    got = materials.get(saved["id"], owner="mayor")
    check("取回正文原样", (got or {}).get("text") == body, str(got)[:60])
    check("字数记对", (got or {}).get("chars") == len(body), str((got or {}).get("chars")))
    check("文件名保留", (got or {}).get("name") == "9月月报.tsv", str((got or {}).get("name")))
    check("正文确实落成了文件", (materials.BLOB_DIR / f"{saved['id']}.txt").exists(), "")


# ── 2: 去重 ─────────────────────────────────────────────────────────────
print("[2] 同文去重")


def t_dedupe() -> None:
    body = "完全一样的一段材料：8.9%"
    a = materials.save("mayor", "a.txt", body)
    b = materials.save("mayor", "换了个名字.txt", body)
    check("同身份同正文 → 同一个 id", a.get("id") == b.get("id"), f"{a.get('id')} vs {b.get('id')}")
    check("后一次的名字覆盖旧的", (materials.get(a["id"], owner="mayor") or {}).get("name") == "换了个名字.txt", "")

    c = materials.save("vice", "a.txt", body)
    check("不同身份 → 两份", c.get("id") != a.get("id"), "")

    d = materials.save("mayor", "a.txt", body + "改了一个数")
    check("正文不同 → 两份（同名也不算同一份）", d.get("id") != a.get("id"), "")


# ── 3: 身份隔离 ──────────────────────────────────────────────────────────
print("[3] 按身份隔离")


def t_owner() -> None:
    saved = materials.save("mayor", "敏感材料.txt", "未公开的数是 8.9")
    check("本人取得到", materials.get(saved["id"], owner="mayor") is not None, "")
    check("别人取不到", materials.get(saved["id"], owner="vice") is None, "")
    check("传 owner=None 表示不过滤（内部用）", materials.get(saved["id"]) is not None, "")
    check("空 id 返回 None 而不是报错", materials.get("", owner="mayor") is None, "")
    check("乱写的 id 返回 None", materials.get("mat_ffffffffffff", owner="mayor") is None, "")


# ── 4: 批量取 ────────────────────────────────────────────────────────────
print("[4] get_many 顺序与缺失")


def t_many() -> None:
    one = materials.save("mayor", "1.txt", "第一份材料")
    two = materials.save("mayor", "2.txt", "第二份材料")
    three = materials.save("mayor", "3.txt", "第三份材料")
    got = materials.get_many([three["id"], "mat_ffffffffffff", one["id"], two["id"], one["id"]], owner="mayor")
    names = [x["name"] for x in got]
    check("按传入顺序返回", names == ["3.txt", "1.txt", "2.txt"], str(names))
    check("取不到的跳过、重复的只留一次", len(got) == 3, str(len(got)))
    check("空入参返回空", materials.get_many([], owner="mayor") == [] and materials.get_many(None) == [], "")


# ── 5: 正文丢了 ──────────────────────────────────────────────────────────
print("[5] 索引在、正文没了")


def t_missing_blob() -> None:
    saved = materials.save("mayor", "会被删掉正文.txt", "这份的正文会被手工删掉")
    (materials.BLOB_DIR / f"{saved['id']}.txt").unlink()
    check("get 返回 None（不返回空正文）", materials.get(saved["id"], owner="mayor") is None, "")
    check("get_many 跳过它而不是留个空壳",
          [x["name"] for x in materials.get_many([saved["id"]], owner="mayor")] == [], "")


# ── 6: 删除 ──────────────────────────────────────────────────────────────
print("[6] forget")


def t_forget() -> None:
    saved = materials.save("mayor", "要删的.txt", "这份要删掉")
    check("别人删不动", materials.forget(saved["id"], owner="vice") is False, "")
    check("本人删得动", materials.forget(saved["id"], owner="mayor") is True, "")
    check("删完取不到", materials.get(saved["id"], owner="mayor") is None, "")
    check("正文文件也没了", not (materials.BLOB_DIR / f"{saved['id']}.txt").exists(), "")


# ── 7: 正文不进会话 ──────────────────────────────────────────────────────
print("[7] 正文不进 chats.json")


def t_not_in_chat() -> None:
    body = "材料正文里的一个很特别的记号：QZ-7788-材料原文"
    saved = materials.save("mayor", "长材料.txt", body)
    thread = chat.create(owner="mayor")
    ids = chat.attach_materials(thread["id"], add=[saved["id"]])
    check("id 记到会话上", ids == [saved["id"]], str(ids))
    raw = (TEST_DATA / "chats.json").read_text(encoding="utf-8")
    check("chats.json 里没有材料正文", "QZ-7788-材料原文" not in raw, "")
    check("chats.json 里有材料 id", saved["id"] in raw, "")
    ctx = chat.context(thread["id"])
    check("上下文里没有材料正文", all("QZ-7788" not in (m.get("content") or "") for m in ctx), "")
    got = materials.describe(ids, owner="mayor")
    check("describe 不含 sha", all("sha" not in x for x in got), str(got)[:80])
    check("describe 不含正文", all("text" not in x for x in got), str(got)[:80])


# ── 8: 会话上的清单 ──────────────────────────────────────────────────────
print("[8] attach_materials")


def t_attach() -> None:
    thread = chat.create(owner="mayor")
    made = [materials.save("mayor", f"材料{i}.txt", f"第 {i} 份材料的内容")["id"] for i in range(3)]
    got = chat.attach_materials(thread["id"], add=[made[0], made[1], made[1]])
    check("加两份、重复的只算一次", got == [made[0], made[1]], str(got))
    got = chat.attach_materials(thread["id"], add=[made[2]])
    check("追加到末尾", got == [made[0], made[1], made[2]], str(got))
    got = chat.attach_materials(thread["id"], drop=[made[1]])
    check("按 id 摘得掉", got == [made[0], made[2]], str(got))
    got = chat.attach_materials(thread["id"], add=[made[1]])
    check("摘掉再挂回去，排到末尾", got == [made[0], made[2], made[1]], str(got))

    many = [materials.save("mayor", f"批量{i}.txt", f"批量材料 {i} 号")["id"] for i in range(chat.MAX_MATERIALS + 3)]
    got = chat.attach_materials(thread["id"], add=many)
    check(f"封顶 {chat.MAX_MATERIALS} 份", len(got) == chat.MAX_MATERIALS, str(len(got)))
    check("超了挤掉最旧的、留最新的", got[-1] == many[-1] and made[0] not in got, str(got)[:3])
    check("不存在的会话 id 不炸", chat.attach_materials("th_nope", add=[made[0]]) == [], "")


# ── 9: 前端契约 ──────────────────────────────────────────────────────────
print("[9] 前端契约")


def t_frontend() -> None:
    html = (ROOT / "app" / "web" / "index.html").read_text(encoding="utf-8")
    check("纯文本材料有入库接口", 'fetch("/api/materials"' in html, "")
    check("解析结果用上 id", "rec.id = it.id" in html, "")
    check("发送时只发 id（有 id 就不再带正文）", "f.id\n      ? { id: f.id" in html, "")
    check("切会话按服务端清单重建芯片", "(d.materials || []).map(" in html, "")
    check("摘掉的材料要回传后端", "drop_materials: ATT.drop.length" in html, "")
    check("上传按钮的说明不再写「仅本轮」", "仅本轮" not in html.split('id="attachBtn"')[1][:600], "")


# ── 10: 接口形状 ─────────────────────────────────────────────────────────
print("[10] _attachments 与新接口")


def t_api() -> None:
    got = main._attachments({"attachments": [{"name": "sample.txt", "text": "接口这条路也要落库"}]},
                            owner="mayor")
    check("只有 text 时也能给出 id", bool((got[0] if got else {}).get("id")), str(got)[:80])
    mid = got[0]["id"] if got else ""
    again = main._attachments({"attachments": [{"id": mid, "name": "sample.txt"}]}, owner="mayor")
    check("只给 id 时按库取回正文", bool(again and again[0].get("text")), str(again)[:80])
    check("取回的正文一致", bool(again and again[0]["text"] == "接口这条路也要落库"), "")
    check("id 还是那个 id", bool(again and again[0].get("id") == mid), "")
    stolen = main._attachments({"attachments": [{"id": mid, "name": "x.txt", "text": "冒充"}]},
                               owner="vice")
    check("别人的 id 取不到正文 → 退回用它自己的 text",
          bool(stolen and stolen[0]["text"] == "冒充"), str(stolen)[:80])
    check("空材料不入库", main._attachments({"attachments": [{"name": "e.txt", "text": "  "}]}) == [], "")


for fn in (t_roundtrip, t_dedupe, t_owner, t_many, t_missing_blob, t_forget,
           t_not_in_chat, t_attach, t_frontend, t_api):
    fn()

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: " + "、".join(FAILS))
    sys.exit(1)
print("全部通过")
