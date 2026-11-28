"""演示身份层：让「个人工作台」真的是个人的。

现在没有登录，但「每个人有自己的关注」这件事必须按人存。做法是几个预设身份顶替
真实账号，配置一律按 `person_id` 落盘：

    data/workspaces.json
    {"people": {"mayor": {"watch": ["GDP_TOTAL", ...], "updated_at": "..."}}}

**这不是权限，是占位。** 谁都能切成任何身份，页面也照实写着（右上角标「演示身份」）。
将来接真实账号时，只需要把 `person_id` 换成登录用户 id：数据结构不用动，
`watch_of()` / `set_watch()` 的签名不用动，改的只有「pid 从哪来」这一层。

四个身份的默认关注是刻意分开的，这样切换时能看出「确实换人了」——
如果几个人默认一模一样，等于没做隔离。秘书是后来加的：他替市长跟线，
关注的是「今天该盯哪几件」，所以挑的是进度与办结类指标，而不是总量。
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

# 工作台的板块。顺序 = 渲染顺序，缺省 = 四块都显示、按这个次序。
# 这里只登记"已经写好的块"，不是组件市场：加一块就要有对应的取数逻辑，
# 光在名单里加个 id 只会渲染出一片空白。
#
# 「督办列表」排在「我的关注」前面：它是台账的全量清单（本单位主办/协办的事），
# 而「关注指标」是各人自己挑的几个数。单位账号进来第一眼要看的正是自己那几件账，
# 之前这些只混在「关注动态」里（按紧急度截前 5 条），看不见第八件。
#
# **市长例外**：这张清单按单位归口，市长看的是全市综合面（指标 + 动态 + 他跟进的
# 那几件），把八件分工摊在最上面反而把指标挤下去。所以 leader 的缺省块里没有它，
# 见 `default_block_ids()`；他真要看，仍可在「自定义」里勾回来（块本身没删）。
BLOCKS: tuple[tuple[str, str], ...] = (
    ("oversight", "督办列表"),
    ("watch", "我的关注"),
    ("feed", "关注动态 · 今日提醒"),
    ("follow", "重点注意事项 · 我跟进的"),
)
BLOCK_IDS = tuple(b[0] for b in BLOCKS)
# 「全部块」的缺省顺序。注意它不含角色判断：按人取缺省块请用 default_block_ids()。
DEFAULT_LAYOUT = list(BLOCK_IDS)

# 缺省不给「督办列表」的角色。只有一把手（看全市综合面）。
_NO_OVERSIGHT_ROLES = ("leader",)

# 手动跟进的条数上限：这是"我自己挑的几条重点"，不是第二份台账。
# 上限太松就会变成"全选一遍"，那和不挑没区别。
MAX_FOLLOW = 20

# 预设身份。scope 写清分管范围，界面直接显示，避免别人误以为这是权限。
# 这四个是「演示身份」：市长 / 常务 / 局长 / 秘书，用来展示换人之后可见范围不同。
# 单位账号在下面单独追加，不从这张表里派生 —— 两者是不同性质的东西。
DEMO_PEOPLE: tuple[dict[str, str], ...] = (
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
    {
        "id": "secretary",
        "name": "市长秘书",
        "avatar": "秘",
        "post": "秘书",
        "scope": "市长办公室",
        "note": "替市长盯台账、备会议、起文稿",
        # 秘书不挂分管单位：他不管哪个局，但要跟全市这条线（授权侧的
        # 「全市」由 directory 按角色给，不看这个空列表）。
        "units": [],
        # 一句话里怎么称呼这个人 —— 个人交办要靠它认人（「让秘书把这个材料
        # 整理好」）。只登记明确可被点名的称谓，不拿 name/post 去模糊匹配：
        # 「局长」能匹配到生态环境局局长，那样一句「让局长去办」就会错派。
        # 「秘书团」是 AI 参谋团，不是人，认人时按下面的 _NOT_PERSON 排除。
        "mentions": ("秘书", "市长秘书"),
    },
)

# ── 单位账号 ──────────────────────────────────────────────────────────────
# 一个责任单位一个可登录主体。为什么必须有：台账里的 assignee 只是「市住建局」
# 这样一个字符串 —— 能统计、能筛、能判权限，但**不是一个能收消息、能签收的主体**。
# 没有单位账号时，给住建局催办只会往事件流写一条记录：没人收到、没人签收，
# 逾期天数照涨。名单与 `directory.UNITS` 一一对应，授权侧不用改：
# 角色一律 bureau（只管本单位），units 只有自己那一格。
#
# 单位账号**不登记 mentions**：那是「个人交办」的认人表，一句「让住建局办」
# 不该被认成给某个自然人派活；`assignable()` 也因此不会把它们列进交办下拉。
#
# 关注列表（DEFAULT_WATCH）刻意不给默认值：单位看什么指标，是各局自己的事，
# 由使用者自己挑 —— 替财政局预设「看空气质量」比空着更糟。
UNIT_ACCOUNTS: tuple[tuple[str, str, str, str], ...] = (
    # (身份 id, 单位全称, 头像字, 简称)
    ("u-gov", "市政府办", "办", "政府办"),
    ("u-drc", "市发改委", "发", "发改"),
    ("u-edu", "市教育局", "教", "教育"),
    ("u-tpa", "市公安局交警支队", "警", "交通管理"),
    ("u-civil", "市民政局", "民", "民政"),
    ("u-fin", "市财政局", "财", "财政"),
    ("u-natres", "市自然资源局", "自", "自然资源"),
    ("u-zhujian", "市住建局", "住", "住建"),
    ("u-chengguan", "市城管局", "城", "城管"),
    ("u-transport", "市交通运输局", "交", "交通运输"),
    ("u-water", "市水务局", "水", "水务"),
    ("u-emergency", "市应急管理局", "应", "应急"),
    ("u-market", "市市场监管局", "监", "市场监管"),
    ("u-data", "市大数据局", "数", "大数据"),
    # 2026-11-28 补齐。此前这 14 家在台账里是责任单位、在会议名单里是参会单位，
    # 但没有账号 —— 催办写不进收件箱、会议通知的发文回执只能记「没有系统账号」。
    # 头像字取单位名里最有辨识度的那个字，简称给登录框和日程里用。
    ("u-gongxin", "市工信局", "工", "工信"),
    ("u-rsj", "市人社局", "人", "人社"),
    ("u-wsj", "市卫健局", "卫", "卫健"),
    ("u-police", "市公安局", "公", "公安"),
    ("u-sifa", "市司法局", "司", "司法"),
    ("u-court", "市法院", "院", "法院"),
    ("u-tax", "市税务局", "税", "税务"),
    ("u-swj", "市商务局", "商", "商务"),
    ("u-stat", "市统计局", "统", "统计"),
    ("u-union", "市总工会", "会", "工会"),
    ("u-youth", "团市委", "团", "共青团"),
    ("u-women", "市妇联", "妇", "妇联"),
    ("u-canlian", "市残联", "残", "残联"),
    ("u-media", "市融媒体中心", "媒", "融媒体"),
)
# 市生态环境局不在这里：它的账号就是既有的 `bureau`（生态环境局局长）。
# 再开一个 `u-env` 会出现同一单位两个账号，事件流里分不清是谁签的。


def _unit_account(pid: str, unit: str, avatar: str, short: str) -> dict[str, Any]:
    return {
        "id": pid,
        "name": unit,
        "avatar": avatar,
        "post": "单位账号",
        "scope": short,
        "note": "本单位事项的签收、报进展、提交办结",
        "units": [unit],
        # 和演示身份区分：前端/测试据此认出「这是单位，不是那位领导」
        "unit": True,
    }


PEOPLE: tuple[dict[str, Any], ...] = DEMO_PEOPLE + tuple(_unit_account(*row) for row in UNIT_ACCOUNTS)

# 四个身份的关注范围刻意不同：市长看总量，常务看投资工业，局长看空气与投诉，
# 秘书看「这几天要盯的进度与办结」。
DEFAULT_WATCH: dict[str, list[str]] = {
    "mayor": ["GDP_TOTAL", "GY_VA_GROWTH", "BUDGET_REV", "TZ_12345_CLOSURE", "FAI_GROWTH", "AQI_GOOD_DAYS"],
    "vmayor": ["FAI_GROWTH", "FAI_PROGRESS", "INVEST_SIGNED", "GY_VA_GROWTH", "BUDGET_REV"],
    "bureau": ["AQI_GOOD_DAYS", "TZ_12345_CLOSURE", "GY_VA_GROWTH"],
    "secretary": ["FAI_PROGRESS", "TZ_12345_CLOSURE", "LIVELIHOOD_DONE", "GY_VA_GROWTH"],
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


def _label_hits(text: str, label: str) -> bool:
    """这句话里点到这个词了吗 —— 排除「秘书团」这种被当成机构名用的情况。

    「秘书团」是 AI 参谋团，不是那位秘书；不排除的话，一句「让秘书团看看这个数」
    会被认成「给秘书派活」。
    """
    start = 0
    while True:
        idx = text.find(label, start)
        if idx < 0:
            return False
        after = text[idx + len(label): idx + len(label) + 1]
        if after != "团":
            return True
        start = idx + len(label)


def find_by_mention(text: str) -> dict[str, Any] | None:
    """一句话里点到的那位「个人」（能接交办的人）。认不出返回 None。

    只认显式登记的 `mentions`，不拿姓名/职务做模糊匹配 —— 「局长」会匹配到
    生态环境局局长，那样一句「让局长去办」就派错了人。长称谓优先：句子里同时
    出现「秘书」和「市长秘书」时，认更具体的那个。
    """
    s = (text or "").strip()
    if not s:
        return None
    best: dict[str, Any] | None = None
    best_len = 0
    for p in PEOPLE:
        for label in p.get("mentions") or ():
            if label and len(label) >= best_len and _label_hits(s, str(label)):
                best, best_len = dict(p), len(label)
    return best


def assignable() -> list[dict[str, str]]:
    """能接收个人交办的身份（前端下拉 / 模型提示词用）。"""
    return [
        {"id": str(p["id"]), "name": str(p.get("name") or ""), "post": str(p.get("post") or "")}
        for p in PEOPLE
        if p.get("mentions")
    ]


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


def default_block_ids(pid: str | None) -> tuple[str, ...]:
    """这个人台面上缺省显示哪些块（只算「有没有」，顺序仍按 BLOCK_IDS）。

    唯一的差别是「督办列表」：市长不看它。判角色走 `directory.role_of()` ——
    角色的唯一映射点在那边，这里不再抄一份「谁是市长」。

    **为什么延迟 import**：`directory` 反过来 import 本模块（它从 PEOPLE 派生
    角色与分管单位），模块级引它就是个循环导入。放在函数里，调用时 directory
    早已加载完，取到的是同一份映射。导不进来就让它报错，不吞 —— 静默回落到
    「市长也有督办列表」正是这次要修的那个 bug，藏起来更难查。
    """
    import directory

    key = resolve(pid)
    if directory.role_of(key) in _NO_OVERSIGHT_ROLES:
        return tuple(b for b in BLOCK_IDS if b != "oversight")
    return BLOCK_IDS


def default_layout(pid: str | None) -> list[str]:
    """没配过布局时的缺省块序。

    单位账号没有关注指标（`watch` 是空的：指标是市级口径，一个局看全市 GDP
    没有意义），把空的「我的关注」摆第一，登录进来第一眼就是一块白板。
    所以「没有关注项」的人缺省从督办列表开始 —— 他真正要看的是本单位的账。
    市长没有督办列表（见 default_block_ids），对他这条分支不成立。
    """
    ids = default_block_ids(pid)
    if not watch_of(pid):
        return [b for b in ("oversight", "follow", "feed") if b in ids]
    return list(ids)


def layout_of(pid: str | None) -> list[str]:
    """这个人工作台的板块顺序（只含显示出来的块）。没配过就按 default_layout。

    **新增的块要能自己出现**：老配置里只存了"当时显示了哪几块"，没有"藏了哪几块"。
    直接按老列表返回，后来加的块（这次的「督办列表」）对这些人是永远看不见的 ——
    他们不会想到去「自定义」里翻一个没见过的名字。所以这里补一层：既没显示、
    也没被显式藏起来的块，按缺省顺序插回去。

    `layout_hidden` 是 set_layout 新写的显隐记录。老记录没有这个键，一律当"没藏过"
    —— 代价是：在这次改动之前手动藏过某块的人，那一块会回来一次。现有数据里没有
    这种人（唯一存过布局的市长三块全在），而且"新功能没出现"比"旧块冒出来"更难查。

    **补的只是这个人缺省就该有的块**（`default_block_ids`）：市长存过
    `[watch, follow, feed]`，若按全量补，他会凭空长出一块「督办列表」——
    正是要他别看到的那个。
    """
    key = resolve(pid)
    stored = _doc()["people"].get(key) or {}
    blocks = stored.get("layout")
    if not isinstance(blocks, list):
        return default_layout(key)
    # 只认登记过的块：配置文件里可能留着以后删掉的块 id
    seen: list[str] = []
    for b in blocks:
        if isinstance(b, str) and b in BLOCK_IDS and b not in seen:
            seen.append(b)
    hidden = {b for b in (stored.get("layout_hidden") or []) if isinstance(b, str)}
    allow = default_block_ids(key)
    for b_id in allow:
        if b_id in seen or b_id in hidden:
            continue
        # 插到缺省顺序里"它后面那个已经在显示"的块前面，位置和默认布局一致
        pos = len(seen)
        for nxt in allow[allow.index(b_id) + 1:]:
            if nxt in seen:
                pos = seen.index(nxt)
                break
        seen.insert(pos, b_id)
    if not seen:
        return default_layout(key)
    return seen


def set_layout(pid: str | None, blocks: Any) -> dict[str, Any]:
    """记住这个人的板块顺序与显隐。

    允许把某几块藏起来（不显示），但**不允许一块都不留**：空工作台不是"配置"，
    是把自己锁在门外 —— 那时连"自定义"按钮在哪都不知道。

    藏起来的块要**显式记下来**（`layout_hidden`），否则 layout_of 会把它当成
    "后来才加的块"再补回来，用户会发现"勾掉了又自己出现"。
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
        people[key] = {
            **prev,
            "layout": clean,
            "layout_hidden": [b for b in BLOCK_IDS if b not in clean],
            "updated_at": _now(),
        }
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
