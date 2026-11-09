"""会议通知成文与导出的自检。

断言六件事：

1. 标题按公文补全，已经是「关于……的通知」就不再套一层；
2. 日期带星期，且星期算对（2026-11-09 是星期一）；
3. 缺地点、缺参会单位时不编，写「另行通知」「各有关单位」，并给出 warnings；
4. 时间只有日期、日期已过期，都要有提示；
5. 导出真的落到指定目录（测试用临时目录，不碰桌面），UTF-8 带 BOM；
6. 重名不覆盖（加 -2），以及页面上改过的正文优先于系统原稿。

跑法::

    python tests\\notice_check.py

不联网、不写 data/、不写桌面。失败退出码非 0。
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core" / "meeting"))

import notice  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def meet(**kw) -> dict:
    base = {
        "id": "MT2026-0099",
        "title": "市政府第14次常务会议",
        "kind": "plan",
        "held_at": "2026-11-09 14:30",
        "place": "市政府三楼会议室",
        "host": "常务副市长",
        "attendees": ["市城管局", "市生态环境局"],
        "topics": ["研究入河排污口整治"],
    }
    base.update(kw)
    return base


def main() -> int:
    print("[1] 标题")
    n = notice.build(meet(), issued_on=date(2026, 11, 6), today=date(2026, 11, 6))
    check("会名补成公文标题", n["title"] == "关于召开市政府第14次常务会议的通知", n["title"])
    dup = notice.build(meet(title="关于召开专题会的通知"), today=date(2026, 11, 6))
    check("已是通知标题就不重复套", dup["title"] == "关于召开专题会的通知", dup["title"])
    check("正文不出现「召开召开」", "召开召开" not in dup["text"])

    print("[2] 时间")
    check("日期带正确星期", "2026年11月9日（星期一）14:30" in n["text"])
    check("时间节保留钟点", "一、会议时间\n　　2026年11月9日（星期一）14:30" in n["text"])

    print("[3] 缺字段不编")
    bare = notice.build(meet(place="", attendees=[]), today=date(2026, 11, 6))
    check("缺地点写另行通知", "另行通知" in bare["text"])
    check("缺参会单位写各有关单位", "各有关单位" in bare["text"])
    check("缺地点有提示", any("地点" in w for w in bare["warnings"]), str(bare["warnings"]))
    check("缺单位有提示", any("参会单位" in w for w in bare["warnings"]))

    print("[4] 时间异常提示")
    only_day = notice.build(meet(held_at="2026-11-09"), today=date(2026, 11, 6))
    check("只有日期要提示钟点", any("钟点" in w for w in only_day["warnings"]), str(only_day["warnings"]))
    past = notice.build(meet(held_at="2026-10-01 09:00"), today=date(2026, 11, 6))
    check("过期会议要提示", any("过期" in w for w in past["warnings"]), str(past["warnings"]))

    print("[5] 导出到指定目录")
    with tempfile.TemporaryDirectory() as tmp:
        res = notice.export(meet(), dest_dir=tmp, issued_on=date(2026, 11, 6))
        p = Path(res["path"])
        check("文件真的落盘", p.is_file(), str(p))
        check("落在指定目录", p.parent == Path(tmp), str(p.parent))
        check("文件名是通知标题", p.stem == "关于召开市政府第14次常务会议的通知", p.name)
        raw = p.read_bytes()
        check("UTF-8 带 BOM（记事本不乱码）", raw.startswith(b"\xef\xbb\xbf"))
        check("正文可读回", "参会单位" in raw.decode("utf-8-sig"))

        print("[6] 重名与改稿")
        again = notice.export(meet(), dest_dir=tmp, issued_on=date(2026, 11, 6))
        check("重名不覆盖，加 -2", Path(again["path"]).stem.endswith("-2"), Path(again["path"]).name)
        edited = notice.export(
            meet(), dest_dir=tmp, issued_on=date(2026, 11, 6),
            text="改过的通知正文\n",
        )
        body = Path(edited["path"]).read_text(encoding="utf-8-sig")
        check("导出用改过的正文", body.strip() == "改过的通知正文", body[:20])

    print()
    if FAILS:
        print(f"FAILED  {len(FAILS)} 项：" + "、".join(FAILS))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
