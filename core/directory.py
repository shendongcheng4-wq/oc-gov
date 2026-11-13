"""单位目录与角色：权限层的单一事实来源。

oc-gov 之前有两处手写「单位 → 人」的映射，各维护各的、改了不联动：
- `core/people.py` 的 `PEOPLE[].units`（身份的分管单位）
- `core/experts.py` 的 `domain.units`（参谋读哪块知识库）

行级授权要靠「责任单位」这个锚点，锚点必须是单一份，否则会出现
「people 说分管、experts 说不分管」的死角。这个模块把三件事收敛到一处：

1. **单位全称**（`UNITS`）：台账 `assignee` / `co_assignees` 用的正式名，
   授权判定、知识库 domain、督办统计都以它为准。简称派生在
   `knowledge.unit_variants`，这里不重复做。
2. **角色**（`ROLES`）：三档 —— leader（市领导）/ dept-leader（分管领导）
   / bureau（委办局），天然有继承关系 leader ⊃ dept-leader ⊃ bureau。
3. **人 → 角色 → 分管单位**（`PEOPLE_ROLE` + `PEOPLE_UNITS`）：
   从 people.py 的三个预设身份派生，是授权的输入。

这是**数据层**，不做判定。判定在 `core/authz.py`，这里只回答「谁、什么角色、
管哪些单位」。
"""

from __future__ import annotations

from typing import Any

# 单位全称表。这是**唯一的单位清单**：台账 assignee/co_assignees、指示拟办的
# 责任单位、授权行级判定都从这里取。早先这份清单只写在注释里，代码里各写各的，
# 结果是「模型拆出来的单位系统不认识，等下发到台账才发现」。
#
# 只收委办局一级；镇区（街道）另有一套，暂不混进来 —— 混进来之后「市住建局」
# 和「溧城街道」会在同一张下拉里，语义就乱了。
UNITS: tuple[str, ...] = (
    "市政府办",
    "市发改委",
    "市教育局",
    "市公安局交警支队",
    "市民政局",
    "市财政局",
    "市自然资源局",
    "市生态环境局",
    "市住建局",
    "市城管局",
    "市交通运输局",
    "市水务局",
    "市应急管理局",
    "市市场监管局",
    "市大数据局",
)

# 简称 → 全称。拟办环节必须认这些写法，否则「住建局」「生态环境局」会被判成
# 不存在的单位，等于逼人按字典说话。
UNIT_ALIAS: dict[str, str] = {
    "住建局": "市住建局",
    "住房和城乡建设局": "市住建局",
    "城管局": "市城管局",
    "城市管理局": "市城管局",
    "综合行政执法局": "市城管局",
    "环保局": "市生态环境局",
    "生态环境局": "市生态环境局",
    "数据局": "市大数据局",
    "大数据局": "市大数据局",
    "发改委": "市发改委",
    "发展和改革委员会": "市发改委",
    "教育局": "市教育局",
    "民政局": "市民政局",
    "财政局": "市财政局",
    "自然资源局": "市自然资源局",
    "交通运输局": "市交通运输局",
    "水务局": "市水务局",
    "应急管理局": "市应急管理局",
    "市场监管局": "市市场监管局",
    "市场监督管理局": "市市场监管局",
    "交警支队": "市公安局交警支队",
    "公安局交警支队": "市公安局交警支队",
    "政府办": "市政府办",
    "市政府办公室": "市政府办",
}


def normalize_unit(text: str) -> str | None:
    """把「住建局」「市住房和城乡建设局」这类写法收敛成全称。认不出返回 None。

    刻意**不做模糊匹配**：写错一个字就该被拦下来问一句，而不是猜一个最近的单位
    把事项交办过去 —— 交办错单位的代价比多问一句大得多。
    """
    name = (text or "").strip()
    if not name:
        return None
    if name in UNITS:
        return name
    return UNIT_ALIAS.get(name)


def mentioned_in(text: str, unit: str) -> bool:
    """原文里有没有真的点到这个单位（全称或简称都算）。

    用来抓「原文没提、模型自己填」的情况。只查包含，不做相似度：
    宁可多报一次「原文没点名」，也不要放过一个替领导定的责任单位。
    """
    s = text or ""
    name = (unit or "").strip()
    if not s or not name:
        return False
    if name in s:
        return True
    return any(alias in s and full == name for alias, full in UNIT_ALIAS.items())


# 三档角色。顺序即继承方向：leader 蕴含 dept-leader，dept-leader 蕴含 bureau。
ROLE_LEADER = "leader"
ROLE_DEPT_LEADER = "dept-leader"
ROLE_BUREAU = "bureau"

ROLES: tuple[str, ...] = (ROLE_LEADER, ROLE_DEPT_LEADER, ROLE_BUREAU)

# 角色中文名，界面与审计里显示用。
ROLE_LABELS: dict[str, str] = {
    ROLE_LEADER: "市领导",
    ROLE_DEPT_LEADER: "分管领导",
    ROLE_BUREAU: "委办局",
}

# 角色继承：一个角色拥有的权限，包含它自己 + 所有「更窄」的角色。
# leader 是顶点（管全市），bureau 是叶子（只管本单位）。
# 用「角色是否在某个集合里」判继承，见 authz._role_at_least。
ROLE_RANK: dict[str, int] = {ROLE_LEADER: 2, ROLE_DEPT_LEADER: 1, ROLE_BUREAU: 0}


def role_at_least(role: str, minimum: str) -> bool:
    """role 的权限等级 >= minimum。leader(2) 满足一切，bureau(0) 只满足自己。"""
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(minimum, -1)


# 预设身份 → 角色。这是「演示身份」向「授权角色」的唯一映射点。
# 将来接真实账号，只需把登录用户映射到这三档之一，授权判定不用动。
# id 与 people.PEOPLE 保持一致。
PERSON_ROLE: dict[str, str] = {
    "mayor": ROLE_LEADER,
    "vmayor": ROLE_DEPT_LEADER,
    "bureau": ROLE_BUREAU,
}

# 预设身份 → 分管单位（主办+协办都算「分管范围」里的单位）。
# 与 people.PEOPLE[].units 保持同源：这里是从 people 导入，而不是另写一份。
# leader 的空列表表示「全市，不按单位过滤」——这是授权判定的特殊分支，
# 不是「什么都不管」。


def person_units(pid: str) -> tuple[str, ...]:
    """某人的分管单位（空 = 全市）。数据源仍是 people.py，这里只做收敛。"""
    import people  # noqa: PLC0415  # 延迟导入，避免 people ↔ directory 成环

    p = people.find(pid)
    if p is None:
        return ()
    units = p.get("units") or []
    return tuple(str(u).strip() for u in units if str(u).strip())


def role_of(pid: str) -> str:
    """身份 → 角色。未知身份回落到委办局（最窄、默认拒绝），不是 leader。"""
    return PERSON_ROLE.get(pid, ROLE_BUREAU)


def person_scope(pid: str) -> dict[str, Any]:
    """一次算齐授权判定的输入：角色 + 分管单位集合。

    authz.can() 只吃这个结构，不直接碰 people / directory，这样判定的
    输入边界清晰、可测试。
    """
    units = person_units(pid)
    role = role_of(pid)
    return {
        "person": pid,
        "role": role,
        "units": units,
        # 全市与否**只看角色**，不看 units 是否为空。
        # 早先用「units 为空」推全市，是个隐式约定：给任何身份配上空 units
        # 就等于悄悄给了他全市数据（新建身份时忘填 units 就会踩，而且不报错）。
        # leader 的空 units 表达的是「不按单位过滤」，和「这个人没有分管单位」
        # 是两件事，必须分开判。
        "is_citywide": role == ROLE_LEADER,
    }
