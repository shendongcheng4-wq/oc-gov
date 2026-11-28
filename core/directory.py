"""单位目录与角色：权限层的单一事实来源。

oc-gov 之前有两处手写「单位 → 人」的映射，各维护各的、改了不联动：
- `core/people.py` 的 `PEOPLE[].units`（身份的分管单位）
- `core/experts.py` 的 `domain.units`（参谋读哪块知识库）

行级授权要靠「责任单位」这个锚点，锚点必须是单一份，否则会出现
「people 说分管、experts 说不分管」的死角。这个模块把三件事收敛到一处：

1. **单位全称**（`UNITS`）：台账 `assignee` / `co_assignees` 用的正式名，
   授权判定、知识库 domain、督办统计都以它为准。简称派生在
   `knowledge.unit_variants`，这里不重复做。
2. **角色**（`ROLES`）：四档 —— leader（市领导）/ secretary（秘书）/
   dept-leader（分管领导）/ bureau（委办局），继承方向
   leader ⊃ secretary ⊃ dept-leader ⊃ bureau。
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
# 收的口径是「**能承接督办事项、能收通知的单一主体**」。镇区（街道）不在里面：
# 「各镇（街道）」「各镇（区、街道）」「各镇（街道）卫生院」是汇总口径，
# 不是一个人、也不是一个能签收的账号，混进来之后「市住建局」和「溧城街道」
# 会在同一张下拉里，语义就乱了。镇区另有一套，暂不并入。
#
# 2026-11-28 补齐：早先只登记了 15 家委办局，而台账的 assignee/co_assignees 和
# 会议名单里还写着 14 家没登记的单位 —— 工信局、商务局、统计局、税务局、卫健局、
# 人社局、公安局、司法局、法院、融媒体中心、总工会、团市委、妇联、残联。
# 后果不是少个下拉项：投递侧按账号找人，认不出的单位永远收不到催办与会议通知，
# 只在回执里留一句「没有系统账号」，而界面上看不出为什么没人签收。
UNITS: tuple[str, ...] = (
    # —— 市政府工作部门 ——
    "市政府办",
    "市发改委",
    "市工信局",
    "市商务局",
    "市教育局",
    "市民政局",
    "市财政局",
    "市统计局",
    "市人社局",
    "市自然资源局",
    "市生态环境局",
    "市住建局",
    "市城管局",
    "市交通运输局",
    "市水务局",
    "市卫健局",
    "市应急管理局",
    "市市场监管局",
    "市大数据局",
    # —— 政法与垂管 ——
    # 交警支队与公安局是两个主体：事故处理、路面秩序的事挂在支队名下，
    # 补「市公安局」不是重复登记，两者的文书不能互相代收。
    "市公安局",
    "市公安局交警支队",
    "市司法局",
    "市法院",
    "市税务局",
    # —— 群团与事业单位 ——
    # 民生实事里「爱心暖托班」「心理辅导室」这类事项的责任单位就是团市委、
    # 妇联、总工会、残联、融媒体中心，不登记它们，这些事项从立项起就没人能签收。
    "市总工会",
    "团市委",
    "市妇联",
    "市残联",
    "市融媒体中心",
)

# 汇总口径的责任单位：台账里确实这么写，但**不是单一主体**，没有账号、不投递。
# 单列出来是为了让「台账里的单位都能找到人」这条检查能区分「漏登记」和
# 「本来就不是一家」——不然只能靠字符串前缀去猜。
AGGREGATE_UNITS: tuple[str, ...] = (
    "各镇（街道）",
    "各镇（区、街道）",
    "各镇（街道）卫生院",
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
    # 台账、纪要、会议名单里的口语写法。这些单位一共十来家，简称几乎每次都说，
    # 不登记就得让人按全称说话 —— 而纪要是照原文抄的，抄进来的就是简称。
    "工信局": "市工信局",
    "工业和信息化局": "市工信局",
    "商务局": "市商务局",
    "统计局": "市统计局",
    "税务局": "市税务局",
    "卫健局": "市卫健局",
    "卫生健康局": "市卫健局",
    "卫健委": "市卫健局",
    "人社局": "市人社局",
    "人力资源和社会保障局": "市人社局",
    "公安局": "市公安局",
    "司法局": "市司法局",
    "法院": "市法院",
    "人民法院": "市法院",
    "融媒体中心": "市融媒体中心",
    "总工会": "市总工会",
    "工会": "市总工会",
    "共青团": "团市委",
    "妇联": "市妇联",
    "妇女联合会": "市妇联",
    "残联": "市残联",
    "残疾人联合会": "市残联",
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

    **包在更长单位名里的不算。** 原文写「市公安局交警支队」时，字面上确实
    含有「市公安局」四个字，但那句话点的是支队，不是市局 —— 把这一处算成
    点了市局，「模型自己填市局」就查不出来了。所以先按长度把更长的单位名
    从原文里抹掉，再判断本名还剩没剩。
    """
    s = text or ""
    name = (unit or "").strip()
    if not s or not name:
        return False
    rest = s
    for other in sorted(UNITS, key=len, reverse=True):
        if other != name and name in other and other in rest:
            rest = rest.replace(other, "\u3000" * len(other))
    if name in rest:
        return True
    return any(alias in rest for alias, full in UNIT_ALIAS.items() if full == name and alias)


# 单位 → 单位账号（`people.PEOPLE` 里的 id）。一个单位可以有多个账号（局长 + 经办），
# 现在各留一个。投递和授权都从这里查「这个单位谁能接」：
#   - 催办要知道发给谁（`accounts_of_unit`）；
#   - 反向要知道某个登录身份代表哪个单位（`unit_of_account`）。
#
# 市生态环境局沿用既有身份 `bureau` —— 它本来就是该局的局长身份，再开一个
# `u-env` 会出现同一单位两个账号，事件流里分不清是谁签的。
#
# **这张表必须覆盖 UNITS 里的每一个单位**：漏一个，那个单位的事项就永远没人
# 收得到（催办只能写进台账），而界面上看不出来 —— 所以 tests/smoke.py 有一条
# 「每个单位都有账号」的断言守着。
UNIT_ACCOUNT: dict[str, tuple[str, ...]] = {
    "市政府办": ("u-gov",),
    "市发改委": ("u-drc",),
    "市教育局": ("u-edu",),
    "市公安局交警支队": ("u-tpa",),
    "市民政局": ("u-civil",),
    "市财政局": ("u-fin",),
    "市自然资源局": ("u-natres",),
    "市生态环境局": ("bureau",),
    "市住建局": ("u-zhujian",),
    "市城管局": ("u-chengguan",),
    "市交通运输局": ("u-transport",),
    "市水务局": ("u-water",),
    "市应急管理局": ("u-emergency",),
    "市市场监管局": ("u-market",),
    "市大数据局": ("u-data",),
    # 2026-11-28 补齐的 14 家。此前它们只作为字符串出现在台账和会议名单里，
    # 没有账号 = 催办只写进事件流、通知只在回执里记一句「发不到」。
    "市工信局": ("u-gongxin",),
    "市人社局": ("u-rsj",),
    "市卫健局": ("u-wsj",),
    "市公安局": ("u-police",),
    "市司法局": ("u-sifa",),
    "市法院": ("u-court",),
    "市税务局": ("u-tax",),
    "市商务局": ("u-swj",),
    "市统计局": ("u-stat",),
    "市总工会": ("u-union",),
    "团市委": ("u-youth",),
    "市妇联": ("u-women",),
    "市残联": ("u-canlian",),
    "市融媒体中心": ("u-media",),
}


def accounts_of_unit(unit: str) -> tuple[str, ...]:
    """这个单位的收件人是谁。没登记就返回空 —— 投递侧据此如实说「该单位没有
    账号，消息只留在台账」，而不是假装发出去了一条。
    """
    return UNIT_ACCOUNT.get((unit or "").strip(), ())


def is_aggregate(unit: str) -> bool:
    """台账里的汇总口径（「各镇（街道）」这类）。不是单一主体，本来就不该有账号。"""
    return (unit or "").strip() in AGGREGATE_UNITS


def unit_of_account(pid: str) -> str | None:
    """这个身份代表哪个单位。全市身份（市长 / 秘书）返回 None。"""
    for unit, pids in UNIT_ACCOUNT.items():
        if pid in pids:
            return unit
    return None


# 四档角色。顺序即继承方向：leader 蕴含 secretary，secretary 蕴含 dept-leader，
# dept-leader 蕴含 bureau。
ROLE_LEADER = "leader"
ROLE_SECRETARY = "secretary"
ROLE_DEPT_LEADER = "dept-leader"
ROLE_BUREAU = "bureau"

ROLES: tuple[str, ...] = (ROLE_LEADER, ROLE_SECRETARY, ROLE_DEPT_LEADER, ROLE_BUREAU)

# 角色中文名，界面与审计里显示用。
ROLE_LABELS: dict[str, str] = {
    ROLE_LEADER: "市领导",
    ROLE_SECRETARY: "秘书",
    ROLE_DEPT_LEADER: "分管领导",
    ROLE_BUREAU: "委办局",
}

# 角色继承：一个角色拥有的权限，包含它自己 + 所有「更窄」的角色。
# leader 是顶点（管全市），bureau 是叶子（只管本单位）。
# 用「角色是否在某个集合里」判继承，见 authz._role_at_least。
#
# 秘书排在分管领导之上、市领导之下：秘书跟的是市长这条线，读口要覆盖全市
# （备会、盯台账时看不全等于没法干活），但「确认办结 / 改全局配置 / 删会议改
# 议定事项」仍是市领导的动作，由 authz 的 leader-only 分支挡住。
ROLE_RANK: dict[str, int] = {
    ROLE_LEADER: 3,
    ROLE_SECRETARY: 2,
    ROLE_DEPT_LEADER: 1,
    ROLE_BUREAU: 0,
}


def role_at_least(role: str, minimum: str) -> bool:
    """role 的权限等级 >= minimum。leader(3) 满足一切，bureau(0) 只满足自己。"""
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(minimum, -1)


# 预设身份 → 角色。这是「演示身份」向「授权角色」的唯一映射点。
# 将来接真实账号，只需把登录用户映射到这四档之一，授权判定不用动。
# id 与 people.PEOPLE 保持一致。
PERSON_ROLE: dict[str, str] = {
    "mayor": ROLE_LEADER,
    "secretary": ROLE_SECRETARY,
    "vmayor": ROLE_DEPT_LEADER,
    "bureau": ROLE_BUREAU,
}
# 单位账号一律 bureau：只管本单位、且只有本单位。写在这里而不是靠 role_of 的
# 「未知身份回落 bureau」兜底 —— 兜底是防越权的保险丝，不该被当成正式映射用；
# 将来给某个单位配「分管副局长」时，只改这一行的值，判定侧不用动。
PERSON_ROLE.update({pid: ROLE_BUREAU for pids in UNIT_ACCOUNT.values() for pid in pids})

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
        # leader / secretary 的空 units 表达的是「不按单位过滤」，和「这个人
        # 没有分管单位」是两件事，必须分开判。
        "is_citywide": role in (ROLE_LEADER, ROLE_SECRETARY),
    }
