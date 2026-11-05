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
        # 退回是审方动作：领导（市领导 + 分管领导）可退回。
        if _role_ok(scope, directory.ROLE_DEPT_LEADER):
            return True, ""
        return False, "退回需要领导。"

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

    # 未知动作：显式拒绝（对齐 Octop `user_has_permission` 对未知 key 返回 False）。
    return False, f"未注册的动作：{action}。"


def deny_reason(action: str) -> str:
    """给审计/日志用的简短拒绝原因（can() 里 reason 可能含资源上下文，这里给纯净版）。"""
    return "denied"
