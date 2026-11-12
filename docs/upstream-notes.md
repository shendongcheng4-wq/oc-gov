# 与上游原型的关系（设计溯源）

> 这份文档回答「每个决策从哪来、为什么不照搬」。只想跑起来用这个项目的话，不需要读它。
> README 只留结论，溯源过程在这里。

页面的业务代码来自 `octop/octop-dashan/prototype/`，那个目录**原样保留、没有被改动**，两边可以对照。

## 迁移时做的改动

| 文件 | 改动 |
| --- | --- |
| `app/workbench.py` | `PROTO/metrics-qa`、`PROTO/oversight` → `core/metrics`、`core/oversight`；模块名 `data` → `workbench` |
| `app/main.py` | 同上，另加 `config/local.env` 路径；路由从 5 个扩到 15 个 |
| `core/oversight/oversight.py` | 状态文件从模块同级 `state.json` 改为 `data/oversight-state.json`，支持 `OCGOV_DATA` 覆盖；`_save()` 补建目录 |
| `core/metrics/ask.py` | `ask()` 增加 `history` / `model_ref` / `on_step` 三个可选参数（SSE 需要阶段回调）；返回里补 `metric_codes` / `metric_names` 供会话摘要用 |
| `core/metrics/llm.py` | 从「读环境变量单模型」改为「经 modelcfg 解析多模型」，`plan()` 支持历史前缀 |
| `app/web/index.html` | 左栏改多轮对话、加模型配置面板、去掉 8088 平台入口 |

**业务逻辑（取数、判定、口径）一行没改。** 新增的 `store` / `modelcfg` / `chat` 三个模块是这次加的，原型里没有对应物。第三方依赖仍为零。

## 从 octop 借鉴了什么

| 设计点 | octop 的位置 | 这里怎么用 |
| --- | --- | --- |
| provider 一条记录 + `models` 数组，不建 models 表 | `infra/db/migrations/001_initial.sql:52` | `data/models.json` 同一结构 |
| 模型标识统一为 `"provider/model"` 字符串 | `infra/db/repos/settings.py:31` | 全链路复用这一种标识 |
| 改模型列表后清理悬空引用 | `infra/agents/providers/store.py:138` | `_clear_stale()` + `prune_thread_refs()` |
| 连通性测试真发 ping，只回 `{ok, latency_ms}` | `infra/agents/providers/probe.py:228` | `modelcfg.probe()` |
| 不传 key 的更新不能清空已有 key（UNSET 哨兵） | `infra/db/repos/providers.py:106` | `_clean_provider()` 里 `if "api_key" in payload` |
| 会话 = thread + 单调 seq，一轮结束批量落库 | `infra/db/migrations/010_thread_message_projection.sql:7` | `core/chat.py` |
| 会话粘性 `threads.model_ref` | `infra/db/migrations/004_thread_composer_preferences.sql:2` | `chat.set_model_ref()` |
| 多轮靠全量历史回灌，不写 query 改写 | `octop_harness/agent.py:571` | 按窗口比例回灌原文（自动压缩尚未移植） |
| 授权单一收口 `user_has_permission(user, key)` | `infra/users/permissions.py` | 改成 `authz.can(scope, action, resource)`：粒度从「能不能进这个功能页」推到「能不能对这个**单位的数据**做这个**动作**」 |
| 审计五字段 `(ts, actor, action, target, payload)` | `infra/db/repos/audit.py` | `core/audit.py`，落成 append-only JSONL；动作词表与授权共用一套 |
| 角色带权限集合 | `infra/users/identity.py` | 三档角色 + 显式继承（`leader ⊃ dept-leader ⊃ bureau`），默认拒绝；Octop 的 `admin/user` 两档在政务场景不够用 |
| 底线工具不可关闭（`CRITICAL_TOOLS`） | `octop_harness` 工具白名单 | 换成 `BASELINE_TOOLS`：三个只读业务查询，按工具名锁，停了就只剩「查不到」 |
| 插件约定 `plugin.yaml` + `setup(ctx)` + `ctx.tool` | `octop/octop-dashan/plugins/demo-*` | `plugins/` 下照这个约定写；没装 octop_harness 时由 `core/toolhost.py` 补加载器 |

## 刻意不照搬的

| octop 的做法 | 这里的选择 | 理由 |
| --- | --- | --- |
| LangGraph 的 function calling 循环（模型可连续调工具、自主拆解） | 不引 LangGraph，但补了一层自己的：`core/agent.py` 手写的 4 轮工具循环，工具面限 4 个只读查询，正文每个数过 `numgate` | 要的是「能调工具、能反问」，不是「能自主拆解」；而模型一旦拿到正文自由，就必须同时有数闸兜住 —— 这两件配套，只搬循环不搬闸，就是多一条编数字的路 |
| WebSocket + hub/subscribe/active_turns 断线重连 | SSE | 标准库手写 WS 握手代价高、收益为零；单机演示不需要「断开不杀任务」 |
| `thread_history_projection` 双来源（投影 + checkpoint） | 只有一份存储 | 那是兼容老数据的迁移产物 |
| `trajectory_events` 观测表 | 不建 | 与对话正确性无关，需要时再加 |
| 按员工分配模型（`user_policies` + revision CAS） | 不做 | 单机单用户；做多租户时再补 |

## 技能与 MCP：哪些能复用，哪些不能

| 东西 | 位置 | 能不能用 | 为什么 |
| --- | --- | --- | --- |
| octop 插件（`kind: tool/skill/hook`） | `octop/octop-dashan/plugins/demo-*` | 不能 | `main.py` 里 `from octop_harness.plugins import PluginContext`，是 octop 运行时插件。oc-gov 没有这个运行时，塞进来等于引第三方依赖 |
| octop 的 MCP 原型 | `octop/octop-dashan/prototype/{metrics-qa,oversight}/mcp_server.py` | 只搬设计 | 用的是 `from mcp.server.fastmcp import FastMCP`（官方 SDK）。工具签名和描述文案可以直接搬，代码要按标准库 JSON-RPC 重写 |
| octop 的通道 | `docs/discord-channel.md` | 不能 | 走 `octop-gateway` 适配器，是外部包。可借鉴的只是形态：通道 = 独立配置 + 连通性检查 + 单实例 |
| 本机 MCP（github / lighthouse-ops / annolab / graphify） | `~/.workbuddy/mcp.json` | 与业务无关 | 部署、代码托管、图像标注，跟政务工作台不是一件事 |

所以这一版**没有引任何 MCP 客户端**，也没抄一行插件代码。要用 MCP 得先回答「谁调谁」：

- **oc-gov 当 client 调外部工具**（查政策库、查企业信用）：标准库下手写 stdio JSON-RPC，收益有，但当前没这个需求；
- **oc-gov 把能力暴露成 server 给 agent 调**：`core/*` 已经是纯函数，包一层就够。但 `commit_items` / `advance_oversight` 是写操作，必须单独鉴权，不能和只读工具混在一张表里放出去 —— 让模型自己判断要不要遵守状态机守卫，等于没有守卫。

真正值得复用的是**方法论**，不是代码：本机 `~/.workbuddy/skills` 里的 `verify-tests-by-mutation`（证明新加的断言真能抓到 bug）和 `local-llm-behavior-eval`（给「模型输出闸门」做可复现评测）。这个项目的闸门特别多（数字闸、长度闸、引文闸、知识库命中闸），值得照那套做法补一层评测。
