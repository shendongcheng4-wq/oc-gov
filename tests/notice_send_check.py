"""会议通知站内信：发给谁、发出去的是什么、谁读了、谁压根没收到。

断言八件事：

1. 参会单位 → 单位账号（市发改委 → u-drc），主持人按职务另外认（常务副市长 → vmayor）；
2. 没有账号的参会单位（市委宣传部）进 `uncovered`，并写进 warnings —— 不许假装都发了；
3. 正文与标题在发出那一刻冻结：之后改会议主档，已发的这一版不变；
4. 同一天、同一版正文、同一批收件人重复发送不堆第二条；
5. 换了正文就是新的一版（两条并存，旧的不会被覆盖）；
6. 只有收件人能标记已读；重复打开只记第一次；
7. 收件箱（notify.build）里能看到这条通知，已读状态跟着回执走，清本地 seen 也不倒退；
8. 别人（非收件人）的收件箱里没有这条。

跑法::

    python tests\\notice_send_check.py

用临时 OCGOV_DATA（仓库下的 .tmp_notice_send，跑完删掉），不碰 data/、不联网。
不用 tempfile.mkdtemp：本机 TMP 目录不可写时它会退回当前目录，把临时数据散在
仓库里 —— 这里显式指定，退出时自己清。失败退出码非 0。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp_notice_send"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ["OCGOV_DATA"] = str(TMP)

for _p in (ROOT / "core", ROOT / "core" / "metrics", ROOT / "core" / "meeting", ROOT / "core" / "oversight"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import ledger as meetings  # noqa: E402
import notice as meeting_notice  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    meet = meetings.create(
        "市政府第14次常务会议",
        "",
        held_at="2026-12-08 09:00",
        place="市政府三楼会议室",
        host="常务副市长",
        attendees=["市发改委", "市城管局", "市委宣传部"],
        topics=["研究入河排污口整治"],
        kind="plan",
    )
    mid = meet["id"]

    print("[1] 投递对象")
    rc = meetings.recipients(meet)
    ids = [t["person"] for t in rc["people"]]
    check("参会单位解析成单位账号", "u-drc" in ids and "u-chengguan" in ids, "、".join(ids))
    # 主持人这条要单独验：上面那场会的分管单位里本来就有常务副市长，
    # 用它来断言「按职务认人」是假通过。换一场他不管的单位的会。
    host_only = meetings.create(
        "专题会", "", held_at="2026-12-09 15:00", host="常务副市长",
        attendees=["市教育局"], kind="plan",
    )
    rc2 = meetings.recipients(host_only)
    whys = {t["person"]: t["why"] for t in rc2["people"]}
    check("主持人按职务认出来", whys.get("vmayor") == "主持会议", str(whys))
    check("没有账号的参会单位进 uncovered", rc["uncovered"] == ["市委宣传部"], str(rc["uncovered"]))
    check("未覆盖写进 warnings", any("市委宣传部" in w for w in rc["warnings"]), str(rc["warnings"]))
    check("同一人不会因多个单位重复出现", len(ids) == len(set(ids)), str(len(ids)))

    print("[2] 发送与冻结")
    built = meeting_notice.build(meet)
    sent = meetings.send_notice(
        mid,
        text=built["text"],
        title=built["title"],
        issuer="市政府办公室",
        held_at=meet["held_at"],
        place=meet["place"],
        by="secretary",
        by_name="市长秘书",
    )
    check("发送成功", bool(sent.get("ok")), str(sent.get("error") or ""))
    n1 = sent.get("notice") or {}
    check("收件人数 = 账号数", n1.get("total") == len(ids), f"{n1.get('total')} / {len(ids)}")
    check("初读数为 0", n1.get("read_count") == 0, str(n1.get("read_count")))
    check("未覆盖单位随记录落库", n1.get("uncovered") == ["市委宣传部"], str(n1.get("uncovered")))
    check("正文是快照", (n1.get("text") or "").startswith("关于召开市政府第14次常务会议的通知"), (n1.get("text") or "")[:24])

    def _rename(doc: dict) -> dict:
        for m in doc["meetings"]:
            if m["id"] == mid:
                m["title"] = "改过的会议名"
                m["place"] = "换了个会议室"
        return doc

    store.update("meetings", _rename, default={"version": 1, "seq": 0, "meetings": []})
    after = meetings.notices_for_meeting(mid)[0]
    check("改会议名不影响已发正文", "改过的会议名" not in (after.get("text") or ""), (after.get("text") or "")[:24])
    check("改会议室不影响已发正文", "市政府三楼会议室" in (after.get("text") or ""), "")

    print("[3] 重复发送")
    again = meetings.send_notice(mid, text=built["text"], title=built["title"], by="secretary", by_name="市长秘书")
    check("同一天同一版不重复发", bool(again.get("repeated")), str(again.get("repeated")))
    check("记录仍然只有一条", len(meetings.notices_for_meeting(mid)) == 1, str(len(meetings.notices_for_meeting(mid))))

    other = meetings.send_notice(mid, text=built["text"].replace("三楼", "五楼"), title=built["title"],
                                 by="secretary", by_name="市长秘书")
    check("换了正文算新的一版", bool(other.get("ok")) and not other.get("repeated"), str(other.get("repeated")))
    check("两版并存", len(meetings.notices_for_meeting(mid)) == 2, str(len(meetings.notices_for_meeting(mid))))

    print("[4] 回执")
    nid = n1["id"]
    got = meetings.mark_notice_read(nid, "u-drc")
    check("收件人可以标记已读", bool(got.get("ok")), str(got.get("error") or ""))
    check("已读计数 1", (got.get("notice") or {}).get("read_count") == 1, str((got.get("notice") or {}).get("read_count")))
    meetings.mark_notice_read(nid, "u-drc")
    check("重复打开不累加", meetings.notices_for_meeting(mid)[-1].get("read_count") == 1, "")
    bad = meetings.mark_notice_read(nid, "u-edu")
    check("非收件人标记无效", not bad.get("ok"), str(bad.get("error")))

    print("[5] 收件箱")
    view = notify.build("u-drc")
    mine = [n for n in view["notes"] if n["rule"] == "meet_notice"]
    check("收件箱里有这两版通知", len(mine) == 2, str(len(mine)))
    read_ids = {n["id"] for n in mine if n["read"]}
    check("已读的那版标成已读", f"meet_notice:{nid}:{nid}" in read_ids, str(sorted(read_ids)))
    check("未读的那版仍是未读", sum(1 for n in mine if not n["read"]) == 1, str(sum(1 for n in mine if not n["read"])))

    people_mod = sys.modules["people"]
    people_mod.mark_seen("u-drc", [])
    view2 = notify.build("u-drc")
    still = [n for n in view2["notes"] if n["rule"] == "meet_notice" and n["id"] == f"meet_notice:{nid}:{nid}"]
    check("清本地已读不回退（回执为准）", bool(still) and still[0]["read"], str(still[0]["read"] if still else "missing"))

    outsider = [n for n in notify.build("u-edu")["notes"] if n["rule"] == "meet_notice"]
    check("非收件人收不到", len(outsider) == 0, str(len(outsider)))

    print("[6] 空正文")
    empty = meetings.send_notice(mid, text="   ", title="x")
    check("空正文不发", not empty.get("ok"), str(empty.get("error")))

    print()
    if FAILS:
        print(f"{len(FAILS)} 项未通过：" + "、".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
