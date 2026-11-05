"""授权判定：单一收口，三档角色 + 行级资源。

借鉴 Octop `users/permissions.py` 的 `user_has_permission(user, key)` 单一收口
思路，但粒度不同：Octop 判的是「能不能进这个功能页」，oc-gov 判的是
「能不能对这个**单位的数据**做这个**动作**」。

设计原则（都从 Octop 搬来、又按政务场景收紧）：

1. **显式授权，默认拒绝**。任何没被明确允许的 (action, resource) 一律 deny，
   不做「默认全开」。
2. **角色继承**。leader ⊃ dept-leader ⊃ bureau，用 `directory.role_at_least`
   判，父角色自动拥有子角色的全部权限。
3. **责任单位是行级锚点**。读/写督办、指标、知识库，都按 `assignee ∪
   co_assignees` 落在不在本人的分管单位集合里来判。
4. **写操作额外收紧**。签收/进展/办结只能由「本单位主办」的角色做，
   协办单位不能替主办签收。

这个模块**只做判定**，不做身份解析（那是 directory 的事）、不做审计
（那是 audit 的事）、不落数据。返回 (allowed, reason)，reason 在拒绝时
非空，直接给界面和审计用。
"""

from __future__ import annotations

from typing import Any

import directory

# ── 动作 ──────────────────────────────────────────────────────────────────
# 动作名用「域.动词」命名（对齐 Octop audit 的 `user.create` 点分层风格）。
# 授权和审计共用这套词表，避免两处各写一份、对不上。

# 读
READ_METRIC = "metric.view"
READ_OVERSIGHT = "oversight.view"
READ_MEETING = "meeting.view"
READ_KNOWLEDGE = "knowledge.view"

# 写（督办流转的每个动作都单独判，因为它们的责任方不同）
OVERSIGHT_CREATE = "oversight.create"     # 建档（交办）
OVERSIGHT_SIGN = "oversight.sign"         # 签收
OVERSIGHT_PROGRESS = "oversight.progress"  # 反馈进展
OVERSIGHT_SUBMIT = "oversight.submit"      # 提交办结
OVERSIGHT_CONFIRM = "oversight.confirm"    # 确认办结（市级收口）
OVERSIGHT_REJECT = "oversight.reject"      # 退回
OVERSIGHT_URGE = "oversight.urge"         # 催办
MEETING_COMMIT = "meeting.commit"          # 会议落库
MEETING_EDIT = "meeting.edit"              # 删会议、改议定事项（会进日程/提醒，市级动作）

# 配置类：改一次影响所有人，不是「某个单位的数据」。
# 判定的输入只有角色，不看 units —— 单位口径对这些动作没有意义。
CONFIG_MODEL = "config.model"              # 模型供应商 / 当前模型 / 连通性探测
CONFIG_EXPERT = "config.expert"            # 专家名单
CONFIG_PLUGIN = "config.plugin"            # 插件开关
NOTIFY_DISPATCH = "notify.dispatch"        # 群发巡检 / 通道测试（真的会往外发消息）
KNOWLEDGE_WRITE = "knowledge.write"        # 共享资料库：建库 / 上传 / 删

# 身份闸门：只回答「有没有登录」，不回答「能不能动这一条」。
# 用于那些行级判定必须看请求体（资源单位）的动作 —— 闸门拿不到 units，
# 业务层 _require 拿到后还要再判一次。
GATE_SESSION = "gate.session"

# 拒绝原因里的中文动作名。写在这里而不是拼在 message 里，是为了让
# 「改模型配置需要市领导」这句话只有一份。
_ACTION_LABEL: dict[str, str] = {
    CONFIG_MODEL: "改模型配置",
    CONFIG_EXPERT: "改专家名单",
    CONFIG_PLUGIN: "改插件开关",
    NOTIFY_DISPATCH: "群发提醒",
    MEETING_EDIT: "改会议记录",
}

# 只认角色的动作：市领导独占。
_LEADER_ONLY: tuple[str, ...] = (
    CONFIG_MODEL,
    CONFIG_EXPERT,
    CONFIG_PLUGIN,
    NOTIFY_DISPATCH,
    MEETING_EDIT,
)

# 动作 → 它对应的督办流转 action（oversight.advance 的 action 名）。
# 用于把 authz 的授权结果和 oversight 的动作名对上。
ADVANCE_ACTION_MAP: dict[str, str] = {
    OVERSIGHT_SIGN: "签收",
    OVERSIGHT_PROGRESS: "反馈进展",
    OVERSIGHT_SUBMIT: "提交办结",
    OVERSIGHT_CONFIRM: "确认办结",
    OVERSIGHT_REJECT: "退回",
    OVERSIGHT_URGE: "催办",
}

# 反向：oversight.advance 的中文动作名 → authz 动作常量。
# _advance 拿到的是「签收/反馈进展/...」这种中文名，用这个映射找到授权动作。
ACTION_FROM_ZH: dict[str, str] = {v: k for k, v in ADVANCE_ACTION_MAP.items()}


def _own_units(scope: dict[str, Any]) -> tuple[str, ...]:
    return tuple(scope.get("units") or ())


def _unit_in_scope(scope: dict[str, Any], units: tuple[str, ...]) -> bool:
    """资源涉及的单位，是否落在本人的分管范围内。leader（全市）恒真。"""
    if scope.get("is_citywide"):
        return True
    own = set(_own_units(scope))
    if not own:
        return False  # 非全市、又没有分管单位 = 什么都不该看到
    return any(u in own for u in units)


def _role_ok(scope: dict[str, Any], minimum: str) -> bool:
    return directory.role_at_least(scope.get("role", ""), minimum)


# ── 判定 ──────────────────────────────────────────────────────────────────
def can(
    scope: dict[str, Any],
    action: str,
    resource: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """是否允许 scope 对应的人对 resource 执行 action。

    `scope` 来自 `directory.person_scope(pid)`。
    `resource` 是行级对象，不同动作需要不同的字段：
      - oversight.* / metric / knowledge：`{"units": (责任单位, ...)}`
      - meeting.*：无需 units（会议是市级动作）

    返回 (allowed, reason)。allowed=False 时 reason 是一句可直接给界面/审计
    的中文。
    """
    resource = resource or {}
    units = tuple(str(u) for u in (resource.get("units") or ()) if str(u))

    # ── 读：按责任单位行级过滤 ──
    if action == READ_METRIC or action == READ_KNOWLEDGE:
        # 指标/知识库：只要涉及的单位有一个落在分管范围即可见。
        if not units or _unit_in_scope(scope, units):
            return True, ""
        return False, f"不是分管范围内的{'指标' if action == READ_METRIC else '记录'}。"

    if action == READ_OVERSIGHT:
        if _unit_in_scope(scope, units):
            return True, ""
        return False, "不是分管范围内的督办事项。"

    if action == READ_MEETING:
        # 会议是全市可见的（参会/主持另按职务过滤，见 schedule.py），这里不拦。
        return True, ""

    # ── 写：督办建档/流转 ──
    if action == OVERSIGHT_CREATE:
        # 建档（交办）是市级动作：leader 可建，dept-leader 只能给自己分管单位建。
        if _role_ok(scope, directory.ROLE_LEADER):
            return True, ""
        if _role_ok(scope, directory.ROLE_DEPT_LEADER) and (
            not units or _unit_in_scope(scope, units)
        ):
            return True, ""
        return False, "只有市领导或分管领导能给分管单位建档交办。"

    if action in (OVERSIGHT_SIGN, OVERSIGHT_PROGRESS, OVERSIGHT_SUBMIT):
        # 签收/进展/办结：责任单位自己的动作。只有「本单位主办」的角色能做，
        # 协办单位不能替主办签收。bureau 就是单位本身，dept-leader/leader 分管则能做。
        if not units:
            return False, "事项缺少责任单位，无法判定签收权限。"
        if _unit_in_scope(scope, units):
            return True, ""
        return False, "不能替非分管单位签收或反馈进展。"

    if action == OVERSIGHT_CONFIRM:
        # 确认办结是市级收口动作，只有市领导能做。
        if _role_ok(scope, directory.ROLE_LEADER):
            return True, ""
        return False, "确认办结需要市领导。"

    if action == OVERSIGHT_REJECT:
        # 退回是审方动作：领导（市领导 + 分管领导）可退回，但**只能退自己
        # 分管单位的事项** —— 和签收/催办同口径。早先这里只判角色不判单位，
        # 结果是常务副市长能退回环保局的办结单（他连看都看不到那一条），
        # 权限比"看得到才退得回"还宽，属于逻辑倒挂。
        if not _role_ok(scope, directory.ROLE_DEPT_LEADER):
            return False, "退回需要领导。"
        if not units:
            return False, "事项缺少责任单位，无法判定退回权限。"
        if _unit_in_scope(scope, units):
            return True, ""
        return False, "不能退回非分管单位的事项。"

    if action == OVERSIGHT_URGE:
        # 催办：领导可催（全市/分管），局长只能催自己单位。
        if not units or _unit_in_scope(scope, units):
            return True, ""
        return False, "不能催办非分管单位的事项。"

    if action == MEETING_COMMIT:
        # 会议落库是市级动作（会进日程、进提醒），只有市领导能建。
        if _role_ok(scope, directory.ROLE_LEADER):
            return True, ""
        return False, "会议落库需要市领导。"

    # ── 配置类：不分单位，只看角色 ──
    if action in _LEADER_ONLY:
        # 这些动作改的是全局共享状态（模型与 api_key、专家名单、插件开关、
        # 群发通道、会议主档）。给分管领导或局长开这个口子没有正当场景：
        # 谁能改 api_key，谁就能把所有人的提问导到自己的服务器上。
        if _role_ok(scope, directory.ROLE_LEADER):
            return True, ""
        return False, f"{_ACTION_LABEL.get(action, action)}需要市领导。"

    if action == KNOWLEDGE_WRITE:
        # 共享资料库：登录即可写，匿名不行。这里不按单位判 —— 库是所有人共用的，
        # 没有「这个单位的库」这种切法，硬切只会让人传完看不到自己传的东西。
        if _role_ok(scope, directory.ROLE_BUREAU):
            return True, ""
        return False, "资料库写入需要登录。"

    if action == GATE_SESSION:
        # 身份闸门：有角色就放行。行级判定不在这里做（闸门看不到 units）。
        if _role_ok(scope, directory.ROLE_BUREAU):
            return True, ""
        return False, "未登录：请先在右上角登录身份。"

    # 未知动作：显式拒绝（对齐 Octop `user_has_permission` 对未知 key 返回 False）。
    return False, f"未注册的动作：{action}。"


def deny_reason(action: str) -> str:
    """给审计/日志用的简短拒绝原因（can() 里 reason 可能含资源上下文，这里给纯净版）。"""
    return "denied"
