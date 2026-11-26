"""录制前拍快照、录废了一键还原。

为什么需要这个：4-5 点「落督办」是一次性写入——MI-01 一旦落了库，那一行就变成
DB 编号、勾选框消失，**没法再录第二遍**。现场 NG 一次，这段就废了。

    python tools/demo_snapshot.py snap      录制前跑一次
    python tools/demo_snapshot.py status    看现在数据是什么状态
    python tools/demo_snapshot.py back      还原（服务不用重启）

只动两个文件：`meetings.json`（会议台账）和 `oversight-state.json`（督办台账）。
`audit.jsonl` 是只追加的日志，**故意不还原**——审计记录就该留着，还原审计等于
篡改留痕，那是这套系统最不该做的事。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BAK = DATA / "_demo_snapshot"
FILES = ("meetings.json", "oversight-state.json")


def snap() -> None:
    BAK.mkdir(exist_ok=True)
    for name in FILES:
        src = DATA / name
        if not src.exists():
            print(f"跳过 {name}（不存在）")
            continue
        shutil.copy2(src, BAK / name)
        print(f"已存 {name}  {src.stat().st_size} 字节")
    print(f"\n快照在 {BAK}")
    print("录完确认没问题之前，别删这个目录。")


def back() -> None:
    if not BAK.exists():
        print("没有快照。先跑 snap。")
        return
    for name in FILES:
        src = BAK / name
        if not src.exists():
            print(f"跳过 {name}（快照里没有）")
            continue
        shutil.copy2(src, DATA / name)
        print(f"已还原 {name}")
    print("\n服务不用重启，存储层每次都是现读文件。刷新页面即可。")
    print("（audit.jsonl 不还原——审计留痕不该被抹掉）")


def status() -> None:
    mp = DATA / "meetings.json"
    if not mp.exists():
        print("没有 meetings.json")
        return
    doc = json.loads(mp.read_text(encoding="utf-8"))
    for m in doc.get("meetings") or []:
        items = m.get("items") or []
        done = [i for i in items if i.get("oversight_id")]
        flag = "  ← 已落库" if done else ""
        print(f"{m.get('id')}  {m.get('title')[:22]:<24} 事项 {len(items)}  已落 {len(done)}{flag}")
        for i in items:
            tag = i.get("oversight_id") or (i.get("verify") or {}).get("status")
            print(f"    {i.get('id')}  {tag}")
    if BAK.exists():
        print(f"\n有快照：{BAK}")
    else:
        print("\n没有快照——录制前先跑 snap。")


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    {"snap": snap, "back": back, "status": status}.get(cmd, lambda: print(__doc__))()
