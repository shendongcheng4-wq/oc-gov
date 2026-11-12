"""会议通知：把一场会写成可以直接下发的中文通知文本。

三条约束，都是有意的：

1. **不经模型**。通知里全是会务信息（时间、地点、参会单位、议题），错一个字
   就得重发，而重发的东西收件人手上会有两个版本。这里逐字段拼装，模型不参与。
2. **缺字段不编**。没有地点就写「另行通知」，没有参会单位就写「各有关单位」，
   同时把缺什么放进 `warnings` 说给人听 —— 宁可留白让人补，也不能替人拍一个
   会议室出来。
3. **不声称已送达**。这里只产出一份文本，发不发、发给谁、对方收没收到，系统
   一概不知道。所以不写「已通知」这种话，也不留发送记录。

落款默认「市政府办公室」：本工作台是市政府办的视角，会议通知的常规发文机关
就是它；但要能改，也能留空 —— 通知是盖了章才算数的东西，不能替用户认这个章。
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

DEFAULT_ISSUER = "市政府办公室"

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_CN_NUM = "一二三四五六七八九十"
_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
_UNSAFE = set('<>:"/\\|?*')


def export_dir() -> Path:
    """导出目录：默认桌面。

    找不到桌面就退回用户主目录 —— 宁可文件落在能看见的地方，也不要因为猜不到
    桌面路径就把导出做失败。`OCGOV_EXPORT_DIR` 可以强行指定。
    """
    env = os.environ.get("OCGOV_EXPORT_DIR")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop", home / "桌面"):
        if cand.is_dir():
            return cand
    return home


def _parse_when(held_at: str) -> tuple[date | None, str]:
    """`2026-11-02 14:30` → (date, "14:30")。认不出就返回 (None, "")。"""
    text = (held_at or "").strip()
    d = _DATE_RE.search(text)
    day = None
    if d:
        try:
            day = date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
        except ValueError:
            day = None
    t = _TIME_RE.search(text)
    clock = ""
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            clock = f"{hh:02d}:{mm:02d}"
    return day, clock


def _cn_date(day: date) -> str:
    return f"{day.year}年{day.month}月{day.day}日"


def _when_text(day: date | None, clock: str) -> str:
    if day is None:
        return clock or ""
    text = _cn_date(day) + f"（{_WEEKDAYS[day.weekday()]}）"
    return f"{text}{clock}" if clock else text


def _meeting_name(title: str) -> str:
    """会议名：从标题里剥掉「关于」「召开」「的通知」，只留会名。

    标题是人随手填的，三种写法都见过：「市政府第14次常务会议」「关于召开……的通知」
    「召开……会议」。正文那句「召开X」要能吃下这三种，不然会出现「召开召开……」。
    """
    name = (title or "").strip()
    if name.startswith("关于"):
        name = name[2:].strip()
    if name.endswith("的通知"):
        name = name[: -len("的通知")].strip()
    elif name.endswith("通知"):
        name = name[: -len("通知")].strip()
    if name.startswith("召开"):
        name = name[2:].strip()
    return name or (title or "").strip()


def notice_title(title: str) -> str:
    """通知标题：已经是「关于……的通知」就照用，否则补成公文标题。"""
    name = (title or "").strip()
    if name.startswith("关于") and name.endswith("通知"):
        return name
    return f"关于召开{_meeting_name(name)}的通知"


def _clean_list(values: Any) -> list[str]:
    out: list[str] = []
    for v in values or []:
        s = str(v).strip()
        if s and s not in out:  # 同一单位重复出现只发一份，别让收件人看到两遍
            out.append(s)
    return out


def build(
    m: dict[str, Any],
    *,
    issuer: str = DEFAULT_ISSUER,
    issued_on: date | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """把一条会议记录拼成通知。纯函数：不读写文件，不碰模型。"""
    issuer = (issuer or "").strip()
    today = today or date.today()
    issued_on = issued_on or today

    title = notice_title(m.get("title") or "")
    name = _meeting_name(m.get("title") or "")
    day, clock = _parse_when(m.get("held_at") or "")
    when = _when_text(day, clock)
    place = (m.get("place") or "").strip()
    host = (m.get("host") or "").strip()
    attendees = _clean_list(m.get("attendees"))
    topics = _clean_list(m.get("topics"))

    # 线上参会（腾讯会议）。会议号、入会链接、密码三样里有一项就成行 —— 只给
    # 链接不行：打印稿上看不见链接，电话里念不出链接，能念的只有会议号。
    online = m.get("online") or {}
    wm_code = str(online.get("meeting_code") or "").strip()
    wm_join = str(online.get("join_url") or "").strip()
    wm_pwd = str(online.get("password") or "").strip()
    online_line = ""
    if wm_code or wm_join:
        bits = []
        if wm_code:
            bits.append(f"腾讯会议 {wm_code}")
        if wm_join:
            bits.append(wm_join)
        online_line = "；".join(bits)
        if wm_pwd:
            online_line += f"（入会密码：{wm_pwd}）"

    warnings: list[str] = []
    if not attendees:
        warnings.append("没有参会单位：主送机关写成了「各有关单位」，发之前请补上具体单位。")
    if not place and not online_line:
        warnings.append("没有会议地点：地点一栏写成了「另行通知」。")
    if day and not clock:
        warnings.append("时间只有日期没有钟点，正文里看不出几点开始。")
    if day and day < today:
        warnings.append(f"会议日期 {day.isoformat()} 早于今天，这份通知发出去就是过期的。")

    head = "、".join(attendees) if attendees else "各有关单位"

    intro = f"　　经研究，定于{when}" if when else "　　经研究，决定"
    if place:
        intro += f"在{place}"
    elif online_line:
        intro += "以视频会议形式"
    intro += f"召开{name}。现将有关事项通知如下："

    sections: list[tuple[str, list[str]]] = []
    sections.append(("会议时间", [when or "另行通知"]))
    sections.append(("会议地点", [place or ("线上（腾讯会议）" if online_line else "另行通知")]))
    if online_line:
        # 线上会单独立一节：混进地点一栏，打印出来的通知上会议号会被折行拆断
        sections.append(("线上参会", [online_line]))
    if host:
        sections.append(("主持人", [host]))
    sections.append(("参会单位", ["、".join(attendees) if attendees else "各有关单位"]))
    if topics:
        sections.append(("会议议题", [f"（{_CN_NUM[i]}）{t}" if i < len(_CN_NUM) else f"（{i + 1}）{t}"
                                 for i, t in enumerate(topics)]))
    req = ["请参会单位安排负责同志参加，不得无故缺席。",
           "请参会人员提前10分钟入场。"]
    req.append(f"确需请假的，请提前向{issuer}报备。" if issuer else "确需请假的，请提前报备。")
    sections.append(("有关要求", [f"（{_CN_NUM[i]}）{r}" for i, r in enumerate(req)]))

    lines: list[str] = [title, "", f"{head}：", "", intro, ""]
    for i, (label, body) in enumerate(sections):
        lines.append(f"　　{_CN_NUM[i]}、{label}")
        for b in body:
            lines.append(f"　　{b}")
        lines.append("")

    if issuer:
        lines.append("　" * 14 + issuer)
    lines.append("　" * 14 + _cn_date(issued_on))

    text = "\n".join(lines).rstrip() + "\n"
    return {
        "title": title,
        "text": text,
        "filename": f"{_safe_name(title)}.txt",
        "warnings": warnings,
        "attendees": attendees,
        "held_at": m.get("held_at") or "",
        "place": place,
        "issuer": issuer,
    }


def _safe_name(title: str) -> str:
    name = "".join("_" if (ch in _UNSAFE or ord(ch) < 32) else ch for ch in title).strip(" .")
    return name[:80] or "会议通知"


def _free_path(folder: Path, stem: str) -> Path:
    """重名不覆盖：桌面上可能有你改过的上一版，覆盖等于把人的修改抹掉。"""
    p = folder / f"{stem}.txt"
    if not p.exists():
        return p
    n = 2
    while (folder / f"{stem}-{n}.txt").exists():
        n += 1
    return folder / f"{stem}-{n}.txt"


def export(
    m: dict[str, Any],
    *,
    issuer: str = DEFAULT_ISSUER,
    issued_on: date | None = None,
    dest_dir: str | Path | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """生成通知并写到磁盘（默认桌面）。返回的 dict 多一个 `path`。

    `text` 是从页面上改过的正文：人在弹窗里调过措辞，导出就该存他改过的那版，
    而不是悄悄用回系统拼的原稿 —— 那等于把人的修改吞掉。
    """
    notice = build(m, issuer=issuer, issued_on=issued_on)
    if text is not None and text.strip():
        notice["text"] = text if text.endswith("\n") else text + "\n"
    folder = Path(dest_dir).expanduser() if dest_dir else export_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = _free_path(folder, Path(notice["filename"]).stem)
    # 带 BOM：记事本、Word、WPS 打开都不乱码。不带 BOM 的 UTF-8 在旧版记事本里
    # 是乱码，而政务场景恰恰还有人用记事本。
    target.write_text(notice["text"], encoding="utf-8-sig")
    notice["path"] = str(target)
    notice["exported_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return notice
