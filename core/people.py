"""演示身份层：让「个人工作台」真的是个人的。

现在没有登录，但「每个人有自己的关注」这件事必须按人存。做法是三个预设身份顶替
真实账号，配置一律按 `person_id` 落盘：

    data/workspaces.json
    {"people": {"mayor": {"watch": ["GDP_TOTAL", ...], "updated_at": "..."}}}

**这不是权限，是占位。** 谁都能切成任何身份，页面也照实写着（右上角标「演示身份」）。
将来接真实账号时，只需要把 `person_id` 换成登录用户 id：数据结构不用动，
`watch_of()` / `set_watch()` 的签名不用动，改的只有「pid 从哪来」这一层。

三个身份的默认关注是刻意分开的，这样切换时能看出「确实换人了」——
如果三个人默认一模一样，等于没做隔离。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "core" / "metrics"))

import mockdata as md  # noqa: E402
import store  # noqa: E402

STORE_NAME = "workspaces"
# 上限 8：现有指标一共 11 个，全关注等于把罗盘搬到工作台，"关注"就没意义了。
# 3 列布局下 8 张也正好收在两行半内，不会把关注动态挤到屏幕外。
MAX_WATCH = 8

# 工作台的板块。顺序 = 渲染顺序，缺省 = 三块都显示、按这个次序。
# 这里只登记"已经写好的块"，不是组件市场：加一块就要有对应的取数逻辑，
# 光在名单里加个 id 只会渲染出一片空白。
BLOCKS: tuple[tuple[str, str], ...] = (
    ("watch", "我的关注"),
    ("feed", "关注动态 · 今日提醒"),
    ("follow", "重点注意事项 · 我跟进的"),
)
BLOCK_IDS = tuple(b[0] for b in BLOCKS)
DEFAULT_LAYOUT = list(BLOCK_IDS)

# 手动跟进的条数上限：这是"我自己挑的几条重点"，不是第二份台账。
# 上限太松就会变成"全选一遍"，那和不挑没区别。
MAX_FOLLOW = 20

# 预设身份。scope 写清分管范围，界面直接显示，避免别人误以为这是权限。
PEOPLE: tuple[dict[str, str], ...] = (
    {
        "id": "mayor",
        "name": "市长",
        "avatar": "市",
        "post": "市长",
        "scope": "全市",
        "note": "总量、增速、重大项目、民情",
        # 分管单位 = 空：全市，不按单位过滤
        "units": [],
    },
    {
        "id": "vmayor",
        "name": "常务副市长",
        "avatar": "常",
        "post": "常务副市长",
        "scope": "分管经济与项目",
        "note": "盯投资、工业、财政收支",
        "units": ["市发改委", "市大数据局", "市住建局"],
    },
    {
        "id": "bureau",
        "name": "市生态环境局局长",
        "avatar": "环",
        "post": "局长",
        "scope": "生态环境",
        "note": "盯空气质量和群众投诉",
        "units": ["市生态环境局"],
    },
)

# 三个身份的关注范围刻意不同：市长看总量，常务看投资工业，局长看空气与投诉。
DEFAULT_WATCH: dict[str, list[str]] = {
    "mayor": ["GDP_TOTAL", "GY_VA_GROWTH", "BUDGET_REV", "TZ_12345_CLOSURE", "FAI_GROWTH", "AQI_GOOD_DAYS"],
    "vmayor": ["FAI_GROWTH", "FAI_PROGRESS", "INVEST_SIGNED", "GY_VA_GROWTH", "BUDGET_REV"],
    "bureau": ["AQI_GOOD_DAYS", "TZ_12345_CLOSURE", "GY_VA_GROWTH"],
}

DEFAULT_PERSON = "mayor"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def all_people() -> list[dict[str, Any]]:
    """身份清单（不含关注列表）。前端下拉直接用这个。"""
    return [dict(p) for p in PEOPLE]


def find(pid: str | None) -> dict[str, str] | None:
    for p in PEOPLE:
        if p["id"] == pid:
            return dict(p)
    return None


def resolve(pid: str | None) -> str:
    """未知身份一律回落到市长，不让一个过期的 localStorage 把接口打成 500。"""
    return pid if find(pid) else DEFAULT_PERSON


def _doc() -> dict[str, Any]:
    doc = store.read(STORE_NAME, {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    people = doc.get("people")
    if not isinstance(people, dict):
        people = {}
    return {"people": people}


def watch_of(pid: str | None) -> list[str]:
    """某人的关注列表：存过就用存的，没存过用默认。

    默认值只在**读**的时候兜底，不写盘 —— 否则一次只读请求也会生成文件，
    让人误以为"这人改过配置"。
    """
    key = resolve(pid)
    stored = _doc()["people"].get(key) or {}
    codes = stored.get("watch")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        return list(DEFAULT_WATCH.get(key, []))
    # 指标字典删过的话，旧配置里的死号不该继续显示
    return [c for c in codes if c in md.METRICS]


def set_watch(pid: str | None, codes: Any) -> dict[str, Any]:
    """整份替换某人的关注列表。

    用「整份替换」而不是 add/remove 两个动作：前端本来就有完整列表，
    整份写是幂等的，重复提交结果一致；add/remove 在并发点两次时会加两遍。

    `codes=None` 表示**取消覆盖、回到默认表**：删掉这个人的 watch 键而不是写一份
    默认值进去。两者当下表现一样，但留一份写死的默认值会盖住以后对默认表的调整——
    改默认表的人会发现「改了却对这个人不生效」，这种坑很难查。
    """
    key = resolve(pid)
    if codes is None:
        def _drop(doc: Any) -> dict[str, Any]:
            doc = doc if isinstance(doc, dict) else {}
            people = doc.get("people") if isinstance(doc.get("people"), dict) else {}
            prev = people.get(key)
            if isinstance(prev, dict):
                # seen 要留着：取消关注不该顺手把"看过哪些提醒"抹掉
                prev.pop("watch", None)
                if set(prev) <= {"updated_at"}:
                    # 什么都没剩（只有时间戳）就别留一个空壳条目，
                    # 否则读文件的人会以为"这个人配过东西"
                    people.pop(key, None)
                else:
                    prev["updated_at"] = _now()
                    people[key] = prev
            return {"people": people}

        store.update(STORE_NAME, _drop, {})
        return {"ok": True, "person": key, "watch": list(DEFAULT_WATCH.get(key, [])), "count": len(DEFAULT_WATCH.get(key, [])), "reset": True}

    if not isinstance(codes, list):
        return {"ok": False, "error": "codes 必须是数组。"}

    clean: list[str] = []
    for raw in codes:
        code = str(raw or "").strip()
        if not code:
            continue
        if code not in md.METRICS:
            return {"ok": False, "error": f"没有注册这个指标：{code}。只能关注已注册指标。"}
        if code not in clean:  # 去重但保序：同一张卡出现两次是前端 bug，不该落库
            clean.append(code)

    if len(clean) > MAX_WATCH:
        return {"ok": False, "error": f"最多关注 {MAX_WATCH} 个指标，收到 {len(clean)} 个。"}

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        people = doc.get("people") if isinstance(doc.get("people"), dict) else {}
        # 合并而不是覆盖：同一个人身上还挂着已读状态（seen），直接替换会把它们抹掉
        prev = people.get(key) if isinstance(people.get(key), dict) else {}
        people[key] = {**prev, "watch": clean, "updated_at": _now()}
        return {"people": people}

    store.update(STORE_NAME, _apply, {})
    return {"ok": True, "person": key, "watch": clean, "count": len(clean)}


def _ledger_ids() -> dict[str, str]:
    """台账里现存的事项：id → 状态。

    延迟导入（且只在写入时调）：people 是数据层，导入期不该把整本台账拉起来；
    台账模块也不依赖 people，所以不会成环。
    """
    sys.path.insert(0, str(ROOT / "core" / "oversight"))
    import oversight as ov  # noqa: PLC0415

    return {it["id"]: it["status"] for it in ov.list_items()}


def follow_of(pid: str | None) -> list[str]:
    """这个人手动挑进来跟进的事项 id（不含按分管单位自动派生的那些）。"""
    key = resolve(pid)
    stored = _doc()["people"].get(key) or {}
    ids = stored.get("follow")
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str) and i]


def set_follow(pid: str | None, ids: Any) -> dict[str, Any]:
    """整份替换手动跟进清单。

    只存 id，不存标题/状态/风险 —— 那些都由台账算，存进配置里就会过期。
    台账里已经不存在的 id 直接拒掉；**已办结的允许留着**（加进来时还没结，
    结了就自然不再显示，不该因为"它办完了"反过来报错）。
    """
    key = resolve(pid)
    if not isinstance(ids, list):
        return {"ok": False, "error": "ids 必须是数组。"}

    clean: list[str] = []
    for raw in ids:
        i = str(raw or "").strip()
        if not i or i in clean:
            continue
        clean.append(i)

    if len(clean) > MAX_FOLLOW:
        return {"ok": False, "error": f"最多手动跟进 {MAX_FOLLOW} 项，收到 {len(clean)} 项。"}

    known = _ledger_ids()
    missing = [i for i in clean if i not in known]
    if missing:
        return {"ok": False, "error": f"台账里没有这些事项：{'、'.join(missing[:3])}。"}

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        people = doc.get("people") if isinstance(doc.get("people"), dict) else {}
        prev = people.get(key) if isinstance(people.get(key), dict) else {}
        # 和 watch 一样合并写：不能把 seen / layout 抹掉
        people[key] = {**prev, "follow": clean, "updated_at": _now()}
        return {"people": people}

    store.update(STORE_NAME, _apply, {})
    return {"ok": True, "person": key, "follow": clean, "count": len(clean)}


def layout_of(pid: str | None) -> list[str]:
    """这个人工作台的板块顺序（只含显示出来的块）。没配过就是三块全显示。"""
    key = resolve(pid)
    stored = _doc()["people"].get(key) or {}
    blocks = stored.get("layout")
    if not isinstance(blocks, list):
        return list(DEFAULT_LAYOUT)
    # 只认登记过的块：配置文件里可能留着以后删掉的块 id
    seen: list[str] = []
    for b in blocks:
        if isinstance(b, str) and b in BLOCK_IDS and b not in seen:
            seen.append(b)
    return seen or list(DEFAULT_LAYOUT)


def set_layout(pid: str | None, blocks: Any) -> dict[str, Any]:
    """记住这个人的板块顺序与显隐。

    允许把某几块藏起来（不显示），但**不允许一块都不留**：空工作台不是"配置"，
    是把自己锁在门外 —— 那时连"自定义"按钮在哪都不知道。
    """
    key = resolve(pid)
    if not isinstance(blocks, list):
        return {"ok": False, "error": "blocks 必须是数组。"}

    clean: list[str] = []
    for b in blocks:
        code = str(b or "").strip()
        if not code or code in clean:
            continue
        if code not in BLOCK_IDS:
            return {"ok": False, "error": f"没有这个板块：{code}。"}
        clean.append(code)

    if not clean:
        return {"ok": False, "error": "至少要留一个板块，不然工作台会全空。"}

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        people = doc.get("people") if isinstance(doc.get("people"), dict) else {}
        prev = people.get(key) if isinstance(people.get(key), dict) else {}
        people[key] = {**prev, "layout": clean, "updated_at": _now()}
        return {"people": people}

    store.update(STORE_NAME, _apply, {})
    return {"ok": True, "person": key, "layout": clean, "hidden": [b for b in BLOCK_IDS if b not in clean]}


def seen_of(pid: str | None) -> list[str]:
    """这个人已经点开看过的提醒（key 列表）。没存过就是空，即全部未读。"""
    key = resolve(pid)
    stored = _doc()["people"].get(key) or {}
    keys = stored.get("seen")
    if not isinstance(keys, list):
        return []
    return [k for k in keys if isinstance(k, str) and k]


def mark_seen(pid: str | None, keys: Any) -> dict[str, Any]:
    """把几条提醒标为已读。

    只做并集、不做删除：提醒是派生出来的，今天读过的指标提醒明天可能因为数值
    变化重新出现——那时 key 相同但内容不同，用户仍然应该再看一眼，所以
    **key 里要带内容指纹**（由 workbench 拼），否则会出现「明明变了却显示已读」。
    """
    key = resolve(pid)
    if not isinstance(keys, list):
        return {"ok": False, "error": "keys 必须是数组。"}

    clean: list[str] = []
    for raw in keys:
        k = str(raw or "").strip()
        if k and k not in clean:
            clean.append(k)

    def _apply(doc: Any) -> dict[str, Any]:
        doc = doc if isinstance(doc, dict) else {}
        people = doc.get("people") if isinstance(doc.get("people"), dict) else {}
        prev = people.get(key) if isinstance(people.get(key), dict) else {}
        merged = [k for k in (prev.get("seen") or []) if isinstance(k, str)]
        for k in clean:
            if k not in merged:
                merged.append(k)
        # 只留最近 200 条：这个文件不该因为天天点已读无限变长
        prev["seen"] = merged[-200:]
        prev["updated_at"] = _now()
        people[key] = prev
        return {"people": people}

    store.update(STORE_NAME, _apply, {})
    return {"ok": True, "person": key, "marked": len(clean), "seen": len(seen_of(key))}


def meta(pid: str | None) -> dict[str, Any]:
    """界面要用的整块身份信息：是谁 + 关注什么 + 上限 + 分管哪些单位。"""
    key = resolve(pid)
    person = find(key) or dict(PEOPLE[0])
    watch = watch_of(key)
    return {
        **person,
        "watch": watch,
        "count": len(watch),
        "max_watch": MAX_WATCH,
        # 手动跟进清单 + 板块布局：和 watch 一样按人存，界面据此渲染与回显
        "follow": follow_of(key),
        "max_follow": MAX_FOLLOW,
        "layout": layout_of(key),
        "blocks": [{"id": i, "name": n} for i, n in BLOCKS],
        # 说清这不是登录态：页面右上角据此显示「演示身份」
        "demo": True,
    }
