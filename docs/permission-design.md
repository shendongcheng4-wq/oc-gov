# oc-gov 权限层设计 · Octop 借鉴对照表

> 依据：Octop 主仓库（本机 `/d/Octop/Octop-main`）的权限实现，与 oc-gov 现状逐项对照。
> 结论先行：**Octop 做的是「多用户 SaaS 的功能级 RBAC + 对象归属 + 资源配额」，
> oc-gov 要的是「政务三档 + 部门行级授权 + 写操作鉴权 + 审计」。
> RBAC 骨架、身份/代理分离、审计表结构可以直接照搬设计；行级授权是 Octop 没覆盖的，要自己写。**

---

## 一、Octop 权限体系全景（已核实源码）

| 层 | 文件 | 核心内容 |
|---|---|---|
| 认证 | `infra/auth/tokens.py` | JWT（HS256），`sub`/`uname`/`role` + iat/exp，ttl 默认 86400s |
| 认证 | `infra/auth/{ldap,sso}/` | 飞书 / 企微 / 钉钉 / OIDC / LDAP 五种 SSO + 验证码 |
| 身份 | `infra/users/identity.py` | `Role` 枚举（`admin`/`user`）+ `User` dataclass（含 `permissions` 列表） |
| 身份 | `infra/users/acting.py` | 「替谁操作」的解析：`--user` → CLI 默认用户 → agent owner 三级优先级 |
| 授权 | `infra/users/permissions.py` | 模块级 permission key 目录（约 60 个 key）+ `user_has_permission()` 单一收口 |
| 授权 | `infra/users/resource_policy.py` | 每用户 `workspace_root_dir` / `token_quota` / `max_agents` 三类资源策略 |
| 行级 | `infra/knowledge/service.py` | 知识库 `owner_user_id` + `shared` + `default_open` 对象级归属 |
| 审计 | `infra/db/repos/audit.py` | `audit_log` 表（ts/actor/action/target/payload）+ 过滤查询 + 过期清理 |

---

## 二、逐项对照：照搬 / 改造 / 丢弃

### 1. 认证（登录态）

| Octop 做法 | oc-gov 现状 | 处置 |
|---|---|---|
| 密码 + 验证码 + 五种 SSO + JWT | 无登录，`X-Person-Id` 头前端可篡改 | **分两步**：先照搬「后端签发、前端不可篡改」的最小闭环（JWT 或签名 session），SSO 五种**留作接真实登录时的选项**，不在第一版做 |
| JWT 载荷 `sub/uname/role` + exp | — | **照搬**载荷结构：oc-gov 的 token 载荷应是 `{sub: person_id, role: 三档角色, units: [分管单位], exp}`，units 直接进 token，授权判定就不用每请求查库 |
| ttl 86400s | — | **改造**：政务场景默认更短（如 8h），且登出要能即时失效 |

**关键取舍**：oc-gov 是单机自托管、标准库 HTTP 服务，**不引入 `pyjwt` 依赖**——用 `hmac` + `secrets`（标准库）签一个等价 token，或干脆用 `secrets.token_urlsafe` 作 session id 存 `data/sessions.json`。前者无状态、后者可即时吊销；**政务偏「可即时吊销」，建议 session 方案**。

### 2. 身份与代理（`identity.py` + `acting.py`）

| Octop 做法 | oc-gov 现状 | 处置 |
|---|---|---|
| `User` 有 `role` + `permissions` 列表 | `people.py` 只有 `id/name/units`，无角色、无权限列表 | **照搬结构**：给 `people.py` 的预设身份加 `role` 字段（`leader`/`dept-leader`/`bureau`），`units` 从展示字段升级为授权锚点 |
| `acting.py` 把「谁操作」和「替谁操作」分开（`--user` / agent owner） | 督办 `actor` 是前端传的字符串，「谁」和「替谁」混在一起 | **照搬思想**：新增「当前登录用户（principal）」与「代理身份（actor）」两个概念。签收时 `principal` 是登录者，`actor` 是「替哪个单位签」，两者都进审计 |

**这是 oc-gov 最该学、也最省事的一处**：Octop 用 `acting.py` 一个函数把「代理身份从哪来」收口，oc-gov 现在 `actor` 散在前端和 `advance()` 里，正是要收口的地方。

### 3. 授权（RBAC）—— `permissions.py`

| Octop 做法 | 处置 |
|---|---|
| `PERMISSIONS` dict：`key → PermissionDef(category/label_zh/page/assignable)` | **照搬「permission key 目录」结构**，但 key 从「功能模块」换成「动作 + 资源」 |
| `admin` 全局旁路 | **照搬**：`mayor`（市领导）对应全局旁路 |
| `user_has_permission(user, key)` 单一收口 | **照搬**：oc-gov 的 `authz.can(principal, action, resource)` 就是它的等价物 |
| `BASELINE_PERMISSIONS` 显式预勾选、必须落库才生效 | **照搬「显式授权、默认拒绝」原则**：没配的角色默认 deny，不做「默认全开」 |
| `LEGACY_GRANTS` + `expand_permission_grants` 父 key 展开 | **改造**：三档角色天然有继承（`leader ⊃ dept-leader ⊃ bureau`），用同一套「父级包含子级」的展开逻辑 |
| `validate_permission_keys` 未知 key 拒绝 | **照搬**：授权配置里出现未知 action/resource 直接报错，不留静默降级 |

**差异（要自己写）**：Octop 的 key 是「能不能进这个页」，oc-gov 要的是「能不能对这个**部门的数据**做这个**动作**」。所以 oc-gov 的 `can()` 必须带 `resource`（行级对象），这是 Octop 没有的维度。

### 4. 行级授权 —— 这是核心差距

| Octop 做法 | oc-gov 需要 | 处置 |
|---|---|---|
| 只有知识库 `owner/shared/default_open` 对象级归属 | 督办 / 指标 / 会议 / 知识库，按「责任单位（assignee/co_assignees）」行级过滤 | **Octop 无可抄，自建** |
| `resource_policy.py` 每用户一个 `workspace_root_dir`（文件沙箱根） | 「局长 = 生态环境局」这种 scope | **借思想**：把「分管单位集合」建模成类似 workspace_root 的 `scope`，`authz.can()` 里做 `resource.assignee ∈ principal.units` 判定 |

**判定规则（oc-gov 自定）**：

```
read:  metric      → leader 全量；dept-leader 分管；bureau 本单位（协办也算）
read:  oversight   → 按 assignee ∪ co_assignees 过滤
write: oversight   → 签收/进展/办结：资源单位在分管范围内（bureau=本单位，dept-leader=分管内）
                     建档/催办/退回：leader、dept-leader(分管内)；确认办结：仅 leader
                     （催办与退回都按「涉及单位」判 —— 退回原本只判角色，会出现
                       「看不到这条却退得回」的倒挂，实现里已按同口径收口）
write: meeting     → 仅 leader（会议落库是市级动作）
read:  knowledge   → 按 domain.units 收窄（现有 experts 逻辑迁移过来）
```

### 5. 审计 —— `audit.py`

| Octop 做法 | 处置 |
|---|---|
| `audit_log(ts, actor, action, target, payload)` 五字段 | **照搬表结构**，但 oc-gov 无 DB，用 append-only JSONL（`data/audit.jsonl`） |
| `actor` 区分 `_system` / `_admin` / 用户名 | **照搬**：`_system`（定时任务）、`_admin`（超管操作）、登录用户名 |
| `action` 词表用「点分层」命名（`user.create` / `auth.login` / `auth.failed`） | **照搬命名法**，oc-gov 的词表如下（见下） |
| `query(since/actor/action)` + `delete_before(cutoff)` | **改造**：JSONL 无 SQL，按行扫描过滤 + 按时间截断清理 |

**oc-gov 审计动作词表（自定，点分层）**：

```
auth.login / auth.logout / auth.failed        # 登录
oversight.create / .sign / .progress / .submit / .confirm / .reject / .urge
meeting.commit                                 # 会议落库
metric.view                                    # 高敏读取：看指标口径/详情
access.denied                                  # 越权被拒（记录攻击尝试）
```

**记录原则**（和方案一致）：写操作全记（含拒绝）；读操作只记「越权被拒」和「高敏读取」，不记每次罗盘刷新。

### 6. 资源策略 —— `resource_policy.py`

| Octop 做法 | 处置 |
|---|---|
| `workspace_root_dir` / `token_quota` / `max_agents` | **只借 `workspace_root_dir` 的 scope 思想**（见行级授权），`token_quota`/`max_agents` 与 oc-gov 无关，**丢弃** |

---

## 三、最终落地路径（依赖顺序）

| 步 | 产出 | 借鉴来源 |
|---|---|---|
| 0 | `core/directory.py`：单位目录 + 人→单位→角色，单一事实来源 | 自建（Octop 无部门概念） |
| 1 | `people.py` 加 `role`；`experts.py` 的 `domain.units` 改从 directory 取 | `identity.py` 的 role 结构 |
| 2 | `core/authz.py`：`can(principal, action, resource)` 单一收口 + 三档继承 + 显式拒绝 | `permissions.py` 的 `user_has_permission` |
| 3 | `core/auth.py`：session 签发/校验（标准库，可即时吊销） | `tokens.py` 载荷 + `acting.py` 的 principal/actor 分离 |
| 4 | `core/audit.py`：append-only JSONL + 点分层词表 | `audit.py` 表结构 |
| 5 | `main.py` 写入口加鉴权闸门；`_person()` 改读 session | — |
| 6 | 前端身份切换改「后端返回的角色」，写按钮按授权结果禁用 | — |

**步 5 的最终形态（2026-11 补齐）**：不是逐个 handler 想起来加 `_require`，而是
`main.py` 顶部的 `WRITE_POLICY` 登记表 + `_gate_write()`，三个写方法在路由前各查一次，
**表外一律 403**。第一轮只覆盖了督办和会议，第二轮补成「配置类锁市领导、资料库写要登录、
个人数据保演示态」。`tests/smoke.py::test_write_policy` 扫源码里的路由字面量，漏登记即红。

**第一批（最小可用闭环，先堵最危险的口子）**：步 0 + 2 + 3 + 4 + 5 —— 交付「写台账真的被拦住了、且留了审计」的硬结果。
**第二批**：三档可见范围的读收窄全量铺开 + 前端改造（步 1 完整 + 6）。

---

## 四、三个待拍板的分歧点（做之前定）

1. **身份来源**：session 方案（可即时吊销，偏政务）还是无状态签名 token（无状态、简单）？我倾向 **session**。
2. **授权粒度**：先 RBAC 三档（够用、和演示身份一一对应），行级 `resource` 参数预留、暂不铺开？我倾向 **RBAC 起步**。
3. **审计范围**：正常读（谁看了哪条详情）要不要也留痕？写操作 + 越权读全记无争议，分歧在正常读；单机自托管下日志没人审，我倾向**不记正常读**，只记高敏读取。
