# Recovery MVP v1 合同

> 状态：**已冻结，允许实现**（2026-07-16）。本文只约束“已有 Windows 服务在多机重启后按严格依赖自动恢复”的首个纵向 MVP。本文、[Agent OpenAPI](../api/recovery-agent-openapi.yaml)、[Control Plane OpenAPI](../api/recovery-control-plane-openapi.yaml) 与[追踪矩阵](./recovery-mvp-traceability.md)已通过 OpenAPI/示例校验和独立 Reader Test。原 Phase 2 `ServiceConfig`/WinSW XML 接管合同保留，但不阻塞本 MVP，也不得被本文暗中实现。

## 1. 范围与部署前提

| In | Out |
|---|---|
| 预注册 Windows Service 的发现、状态、start/stop/restart | 创建、安装、卸载或修改业务 Windows Service |
| Agent 主动注册/心跳，CP 服务镜像 | 完整资产盘点、软件清单 |
| 严格依赖 DAG、环检测、冷启动 RecoveryRun | OPTIONAL/DEGRADED、任意工作流 DSL |
| `scm/tcp/http` 单次 readiness | shell/cmd/PowerShell、持续 liveness |
| 单 CP、SQLite、最小 Web UI | CP 高可用、多用户/RBAC、Linux |

- MySQL、Redis、Nacos 首版均按单实例处理；集群选主、quorum 和数据修复不属于 MVP。
- Java/Nacos/Nginx/Redis 应先由现有 GUI 或运维流程注册为 WinSW 服务；MySQL 使用原生服务。
- Agent 与 CP 自身为 `Automatic`；进入恢复组的业务服务必须为手动启动，否则恢复组不能 arm。
- CP 必须部署在独立管理节点，不得依赖其管理的 MySQL/Redis/Nacos。
- 本合同的明文 HTTP 只允许实验/受控管理网。它不是生产安全档位。

## 2. 身份、心跳与在线租约

### 2.1 身份

- `agent_id`：Agent 首次启动生成 UUIDv4，持久化在 Agent SQLite；重装前稳定。
- `boot_marker`：通过 Windows WMI `Win32_OperatingSystem.LastBootUpTime` 取得并规范化为 UTC FILETIME 十进制字符串。查询失败时 Agent 拒绝加入自动恢复，不得以进程启动时间替代。
- `boot_id`：首次见到一个新 `boot_marker` 时生成 UUIDv4，并把 marker/id 原子持久化；marker 未变必须复用原 UUID。因此同一次 OS 启动内稳定，Agent 进程重启不变化。
- `agent_instance_id`：每次 Agent 进程启动生成 UUIDv4。
- `instance_generation`：Agent SQLite 内的正整数，每次进程启动在生成 instance id 前原子加一，用于隔离旧进程的延迟消息。
- `sequence`：在一个 `agent_instance_id` 内从 1 单调递增。
- `local_service_id`：Agent 配置 allowlist 内唯一的小写 slug；映射到既有 `windows_service_name`。
- `local_service_id -> windows_service_name` 绑定在 Agent SQLite 中持久化；Windows 服务名按大小写不敏感比较。同一 Agent 数据库中已经出现过的 local ID 不得改绑到另一 Windows 服务，改绑使 Agent 在监听端口和恢复 Operation 前拒绝启动。CLI 必须以退出码 2 和脱敏 JSON 中的稳定 `SERVICE_MAPPING_CHANGED` 报告，不得输出 traceback、服务名、配置路径或 Token。更换目标必须使用新的 local ID，并在 CP 中重新配置恢复组。
- `managed_service_id`：CP 分配的 UUID；`(agent_id, local_service_id)` 唯一。CP 的服务动作路径、依赖边、探针和步骤只引用该 UUID。
- allowlist 与 `local_service_id` 是稳定配置，不得为同一目标轮换临时 ID。服务离开一次有效上报后，CP 将原记录保留为内部 tombstone 且不再放入公开 managed-service 列表；同一 `(agent_id, local_service_id)` 恢复上报时必须复用原 `managed_service_id`。异常持续产生新 ID 视为配置错误或 Token/来源边界事件，MVP 不以自动删除审计关联来掩盖。

### 2.2 注册与心跳

Agent 启动后立即 `POST /api/v1/agents/register`，随后默认每 10 秒向
`POST /api/v1/agents/{agent_id}/heartbeat` 上报；正常间隔加入 ±20% jitter，失败采用 2–60 秒指数退避。

上报主体固定包含：

`agent_id, boot_id, agent_instance_id, instance_generation, sequence, version, endpoint, hostname, services[]`

- 注册只接受比现存 generation 更大的实例，或完全相同的 `(generation, instance_id)` 重试；更小 generation 或同 generation 不同 instance 返回 `409 STALE_AGENT_INSTANCE`。完全相同实例的注册重试也执行 sequence 规则：`sequence <= last_sequence` 时返回幂等忽略且不刷新 lease、endpoint 或服务镜像；只有更大 sequence 才更新。
- 心跳只接受当前已注册的 `(agent_instance_id, instance_generation)`；旧实例即使 sequence 更大也返回 `409 STALE_AGENT_INSTANCE`，不得覆盖服务镜像。
- CP 以服务端 `received_at` 计算 freshness，不接受客户端时间决定在线状态。
- 同一 instance 中 `sequence <= last_sequence` 的重复/乱序心跳幂等忽略并返回 200，但**不刷新** `received_at`。
- 新 `agent_instance_id` 允许 sequence 重新从 1 开始；相同 `boot_id` 不触发新冷启动 epoch。
- CP 不信任 Agent 上报的 endpoint host：只接受无 userinfo/path/query 的 `http://IP-literal:port`，并用实际 TCP peer IP 加上已校验的端口构造保存地址；不读取 `X-Forwarded-For`。host 与 peer 不一致时返回 `ENDPOINT_SOURCE_MISMATCH`。
- 默认 45 秒未收到有效心跳转为 `OFFLINE`；重新收到有效心跳转为 `ONLINE`。
- `received_at` 只作为持久审计时间，不能跨 CP 进程证明在线租约。CP 每次启动后必须先把数据库中既有 Agent 保守视为 `OFFLINE`；只有当前 instance 的新注册或 `sequence` 更大的有效心跳才能在本进程建立 monotonic 租约并转为 `ONLINE`，重复/乱序报告不得恢复在线。
- 单次服务观察或 CP ingress 的意外异常不得永久终止心跳后台任务；Agent 只记录不含异常文本或秘密的稳定失败事件，清除已注册假设，并按同一 2–60 秒有界退避继续注册。进程取消信号仍必须及时传播。
- 心跳仅证明节点可达，绝不表示任何业务服务 READY。
- MVP 心跳只上报受管服务摘要，不采集 CPU、磁盘、安装软件等非核心资产。
- CP 的公开服务镜像只包含当前 Agent 最近一次有效报告中的服务；历史 tombstone 不计入公开服务集合容量。容量判断、Agent/sequence 更新与服务镜像替换必须在同一 `BEGIN IMMEDIATE` 事务内完成，拒绝的报告不得推进 sequence、租约或任何服务行。

## 3. ObservedService

MVP 不拥有业务配置，不返回 `ConfigState`。公开模型固定为：

```json
{
  "local_service_id": "mysql",
  "windows_service_name": "MySQL80",
  "display_name": "MySQL 8",
  "installation_state": "INSTALLED",
  "runtime_state": "ACTIVE",
  "startup_state": "AUTOSTART_DISABLED",
  "last_observed_at": "2026-07-16T08:00:00Z"
}
```

枚举：

- `InstallationState`: `INSTALLED / NOT_INSTALLED / UNKNOWN`
- `RuntimeState`: `ACTIVE / INACTIVE / STARTING / STOPPING / FAILED / UNKNOWN`
- `StartupState`: `AUTOSTART_ENABLED / AUTOSTART_DISABLED / START_BLOCKED / UNKNOWN`

Agent 只能查询和操作 allowlist 中的服务。服务不存在返回 `NOT_INSTALLED`；不得退化为任意 SCM 浏览器或任意命令执行器。

SCM 映射固定为：`SERVICE_RUNNING -> ACTIVE`、`SERVICE_STOPPED` 且退出码为零 `-> INACTIVE`、`SERVICE_STOPPED` 且退出码非零 `-> FAILED`、`SERVICE_START_PENDING -> STARTING`、`SERVICE_STOP_PENDING -> STOPPING`；`PAUSED/PAUSE_PENDING/CONTINUE_PENDING`、访问拒绝和无法识别值均为 `UNKNOWN`。启动类型 `AUTO/DELAYED_AUTO -> AUTOSTART_ENABLED`、`DEMAND -> AUTOSTART_DISABLED`、`DISABLED -> START_BLOCKED`，查询失败为 `UNKNOWN`。

## 4. Agent 动作与 Operation

动作固定为 `start / stop / restart`。所有动作请求必须携带 `Idempotency-Key: UUIDv4`。
动作正文可以省略；若提供只能是拒绝未知字段的空 JSON object `{}`。任何 `cmd`、`PowerShell`、`file` 或其他字段均返回 `422 VALIDATION_ERROR`，不创建 Operation。请求指纹中的 canonical JSON 始终为 `{}`。

Operation 状态固定为：

`PENDING / RUNNING / SUCCEEDED / FAILED / REJECTED / UNKNOWN`

执行规则：

1. `idempotency_key` 在单 Agent 的 Operation 表全局唯一。请求接受规范连字符形式、大小写不限的 UUIDv4，并在 Operation 响应中统一输出小写；其他 UUID 表示法一律拒绝。请求指纹固定为 `UPPER(method) + "\n" + canonical_path + "\n" + canonical_json_body` 的 SHA-256；空正文的 canonical JSON 是 `{}`。
2. 校验 allowlist、路由动作和 UUID 后，在一个 SQLite `BEGIN IMMEDIATE` 事务内完成 key 查重、活动 Operation 判定和 `PENDING/PREPARED` Operation 插入；事务提交后才能产生 SCM 副作用。
3. 相同 key + 相同指纹返回既有 Operation；相同 key + 不同指纹返回 `409 IDEMPOTENCY_KEY_REUSED`。两个并发相同 key 请求只能生成一条记录。
4. 未通过路由/格式/allowlist 的请求直接返回 4xx，不创建 Operation；已定位服务后的业务前置条件失败创建 `REJECTED/COMPLETED` Operation 并以 202 返回。
5. 同一服务所有动作共用一把持久化活动锁；不同服务可并行。锁冲突创建 `REJECTED/COMPLETED` Operation，错误码 `SERVICE_ACTION_CONFLICT`。
6. Worker 在调用 SCM 前先把 Operation 原子改为 `RUNNING/DISPATCHING`，调用返回后改为终态及 `COMPLETED`。`PREPARED/DISPATCHING/COMPLETED` 是内部 journal 状态，不加入公开 Operation 枚举。
7. Agent 重启可安全重新调度仍为 `PENDING/PREPARED` 的动作；`RUNNING/DISPATCHING` 只能按下表的收敛规则对账。start/stop 当前已达目标可记成功；restart 没有持久化完成证据一律 `UNKNOWN`，不得自动重放。
8. Operation 必须保存请求指纹、目标服务/动作、journal 状态、时间、错误码和消息；禁止把 Token 写入记录或日志。
9. Worker 和启动恢复器在任何 SCM 查询/动作前，都必须核对当前 allowlist 中的 Windows 服务名与 Operation 持久目标按大小写不敏感相等。PREPARED 阶段不一致时零 SCM 调用并确定性结束为 `FAILED/SERVICE_MAPPING_CHANGED`；DISPATCHING 阶段不一致时直接 `UNKNOWN/SERVICE_MAPPING_CHANGED`，不得查询或操作新目标。

动作矩阵固定如下：

| 当前状态 | start | stop | restart |
|---|---|---|---|
| `ACTIVE` | no-op `SUCCEEDED` | 执行并等待 `INACTIVE` | 执行 stop→start 并等待 `ACTIVE` |
| `INACTIVE` / `FAILED` | 执行并等待 `ACTIVE` | no-op `SUCCEEDED` | `REJECTED` |
| `STARTING` | 只观察等待 `ACTIVE` | `REJECTED` | `REJECTED` |
| `STOPPING` | `REJECTED` | 只观察等待 `INACTIVE` | `REJECTED` |
| `UNKNOWN` / `NOT_INSTALLED` | `REJECTED` | `REJECTED` | `REJECTED` |

SCM 调用返回确定失败时 Operation 为 `FAILED/SCM_ACTION_FAILED`；观察或执行达到超时后为 `FAILED/SCM_ACTION_TIMEOUT`。只有崩溃/断线导致副作用是否发生无法证明时才为 `UNKNOWN`。

核心路径：

- `GET /healthz`
- `GET /api/v1/agent`
- `GET /api/v1/services`
- `POST /api/v1/services/{local_service_id}/actions/{action}`
- `GET /api/v1/operations/{operation_id}`
- `POST /api/v1/probe`

## 5. Readiness probe

每个服务在 CP 中最多一个 readiness；定义归 CP，单次执行在目标 Agent。

| kind | 必填 | 限制 |
|---|---|---|
| `scm` | `local_service_id` | 只判断 allowlist 服务是否 `ACTIVE` |
| `tcp` | `host, port` | host 只能是 `localhost` 或 Agent 本机实际绑定 IP 的 literal |
| `http` | `url, expected_status` | 固定 GET 且只允许 `http`；目标 host 规则同 TCP；可选 `body_contains` |

- TCP/HTTP 在连接前重新取得本机地址并校验；拒绝 DNS 主机名、非本机 IP、IPv4-mapped IPv6 绕过、URL userinfo、请求体、自定义 Header、所有重定向及环境代理。
- HTTP 客户端固定 `trust_env=false`、`follow_redirects=false`，最多读取 64 KiB；`body_contains` 为 1–256 个 Unicode 字符，响应正文不得写日志或回传。
- 单次 `timeout_seconds` 为可带小数的 JSON number，取值 `0.1–10`、默认 2；CP `interval_seconds` 和总 `deadline_seconds` 为整秒 JSON integer，分别取值 `1–30`（默认 3）和 `1–300`（默认 60），且 deadline 必须不小于 timeout。
- 禁止任意命令、文件和其他 URL scheme。
- 无显式探针时使用 `scm` fallback，Run Step 保存 `READINESS_FALLBACK_SCM` 警告。
- Agent 返回 `passed, observed_at, latency_ms, code, message`，不得回显敏感响应正文。

## 6. 恢复组、epoch 与触发

- MVP 只有严格依赖：存储语义为 `managed_service_id depends_on prerequisite_managed_service_id`。
- 写入依赖时使用 Kahn 算法验证无环；有环返回 409，不保存部分修改。
- 一个恢复组包含的所有 Agent 都是 required nodes，不支持可选节点。
- Arm 前必须验证：所有节点在线、服务已安装、业务服务 `AUTOSTART_DISABLED`、图无环。
- 首次 arm 只把当前 boot epoch 保存为基线，不自动启动；操作者可手工 `Run now` 验证。
- 只有 `DISARMED` 且没有活动 Run 的组可以修改成员、依赖或探针；修改后必须重新 arm 并保存新基线。
- 成员替换只允许把当前最近一次有效报告仍包含的服务新加入组。既有成员后来离开 allowlist 时可在 DISARMED 组中继续保留或显式移除，但移除后在其恢复上报前不得重新加入；违反时返回 `404 SERVICE_NOT_ALLOWLISTED` 且成员集合不变。既有 stale member 会在 arm/preflight 中产生可见的 `SERVICE_NOT_REPORTED`，不得静默忽略。
- `POST .../arm` 对 `DISARMED` 执行完整校验并保存基线；对其他任意组状态都是幂等读取，不重置 baseline、candidate、settle 计时或活动 Run。只有显式 disarm 后再次 arm 才能建立新基线。
- 恢复组 `name` 为 1–128 个 Unicode 字符；`description` 始终为非 null 字符串，取值 0–1024 个字符，空字符串表示清空。PATCH 中字段可省略，但显式 `null` 非法。

公开集合容量固定如下；所有对象继续拒绝未知字段，所有写入超过上限均返回 `422 VALIDATION_ERROR`：

| 集合 | 容量 |
|---|---:|
| Agent capabilities `service_actions/probe_kinds` | 各恰好 3 项且唯一 |
| Agent report/services、Agent/managed-service/group 列表 | 最多 1024 项 |
| group members、missing agents、probes、Run members/probes/steps、Step probe attempts/dependency chain | 最多 1024 项 |
| group/Run dependency edges | 最多 16384 项且唯一 |
| group `blocked_reasons`、Step warnings、单页 Run 列表 | 最多 100 项；适用处保持唯一 |

CP 对新 Agent、最近一次有效报告可见的 managed service 总量和恢复组总量执行全局 1024 门禁；计数与写入位于同一 SQLite 写事务，不允许并发超卖。达到边界的完整集合仍必须可读，边界后的写入返回 `422 VALIDATION_ERROR`，不得先提交后由响应校验变成 500。

恢复组状态固定为：

`DISARMED / ARMED_IDLE / WAITING_FOR_NODES / SETTLING / BLOCKED_PRECONDITION / RUNNING`

epoch 固定为：

```text
SHA256(UTF8(
  "recovery-mvp-v1\n" +
  "group_id=<lowercase-uuid>\n" +
  按 agent_id 小写 UUID 字典序排列的若干行：
  "agent_id=<lowercase-uuid>;boot_id=<lowercase-uuid>\n"
))
```

输出固定为 64 位小写十六进制字符串。

任一 required Agent 的 boot_id 改变即产生候选新 epoch。只有全部 required Agent 在线，且当前候选 epoch 连续稳定达到 `node_settle_window_seconds`（默认 120），才能创建 AUTO Run。settle 期间任一 boot id 再变化必须替换候选 epoch并从零计时；任一节点掉线立即进入 `WAITING_FOR_NODES`，节点重新齐全后也从零计时。

AUTO Run 创建前必须再次验证安装状态、手动启动策略、节点在线和无环；失败进入 `BLOCKED_PRECONDITION` 且零动作。该状态是显式隔离：即使外部条件自行恢复也不得自动建 Run，管理员必须先 disarm、修复并重新 arm。创建 AUTO Run、写入 `last_scheduled_epoch` 和占用涉及的服务必须在同一事务完成；数据库以 `(group_id, epoch)` 的 AUTO 部分唯一索引兜底。

`RecoveryGroup.blocked_reasons` 固定为最多 100 个严格 `PreconditionIssue`。每项只允许：`code`（1–64 个 Unicode scalar value）、`message`（1–512 个 Unicode scalar value）、可空 UUIDv4 `managed_service_id`、可空 UUIDv4 `agent_id`、以及最多 100 个且唯一的 UUIDv4 `managed_service_ids`；`code/message` 禁止 U+0000 和孤立 surrogate。`BLOCKED_PRECONDITION` 必须至少包含一项；其余所有 GroupState（包括 `DISARMED`）必须为空数组。SQLite v4 升级必须先以同一模型验证并规范化旧 JSON；非法 v3 数据原子拒绝升级，存储类型必须为 TEXT，合法 surrogate pair 规范化为字面 Unicode，普通字面文本 `\\ud800` 不得误判。v4 trigger 必须在每个数据库连接上调用同一严格校验函数，缺少函数的旧进程写入应 fail closed。历史数据无法恢复具体原因时必须使用显式稳定原因，不得伪造为空。

缺少节点时组保持 `WAITING_FOR_NODES` 并列出缺失节点，不执行部分子图。CP 自身重启但 Agent boot_id 未变化时不得触发新 AUTO Run。

## 7. RecoveryRun 状态机

等待节点属于恢复组、不是已创建 Run 的状态。Run 只在手工请求校验通过，或 AUTO settle 与前置校验全部完成后创建。

Run 状态：

`PENDING / RUNNING / SUCCEEDED / FAILED / UNKNOWN`

Step 状态：

`PENDING / WAITING_DEPENDENCY / STARTING / PROBING / READY / FAILED / BLOCKED / UNKNOWN`

启动算法：

1. 快照组成员、依赖和探针，按先决边 `prerequisite -> dependent` 做拓扑分层。
2. 同层最多并行 4 个服务。
3. 服务已 `ACTIVE` 时跳过 start，仍执行 readiness。
4. `INACTIVE/FAILED` 时创建 start Operation，并用原 operation_id 轮询确定结果。
5. start 成功后按 probe interval/deadline 调用单次 probe；只有 READY 放行下游。
6. Step `FAILED` 时其所有可达下游变为 `BLOCKED`，保存首个根因和依赖链；独立分支完成后再计算 Run 终态。
7. Operation 结果无法确认时 Step/Run 进入 `UNKNOWN`，不盲目重发。
8. CP 重启扫描未终态 Run：持有 operation_id 的步骤先对账；尚未产生副作用的 PENDING 才能继续调度。
9. CP 在 POST Agent 动作前，必须先持久化 Step 的 `dispatch_idempotency_key`；若在收到 operation_id 前崩溃，恢复后用相同 key 重发以取得同一 Operation，不生成第二个副作用。
10. 上游 `FAILED` 或 `UNKNOWN` 时，只把其全部可达下游标为 `BLOCKED`；无依赖的其他分支继续。所有步骤终态后按 `UNKNOWN > FAILED > SUCCEEDED` 计算 Run 终态。
11. 手工 `Run now` 创建新 MANUAL Run；不受 AUTO epoch 唯一约束，但所有 required nodes 必须在线且前置校验通过。
12. `Retry` 复制父 Run 的图与探针快照并重新执行整图；已 `ACTIVE` 的服务仍只做 readiness。Agent 动作使用新 Run/Step 确定生成的新幂等 key。
13. CP 必须把 Agent 返回的 Operation 当作不可信协议输入。真实 Agent 客户端必须先对 `202 POST` 与 `200 GET` 正文执行完整 Operation Schema 校验；JSON 损坏、字段缺失、未知字段或字段值非法必须抛出不携带原始正文及校验器文本的类型化协议异常，不得降级为普通通信失败或进入轮询重试。首次 POST 响应和此后的每次 GET 对账都必须核对：`agent_id`、`local_service_id`、按大小写不敏感比较的 `windows_service_name`、`action=start`、与 Step 已持久化值相同的 `idempotency_key`，以及 `canonical_request_fingerprint("POST", "/api/v1/services/{local_service_id}/actions/start", {})`；已有持久化 `operation_id` 时还必须核对响应中的同名字段。首次响应必须先通过 Schema 与语义核对，才允许保存 `operation_id`。任一 Schema 或语义不匹配时，Step 与 Run 立即进入 `UNKNOWN`，Run 使用稳定码 `AGENT_PROTOCOL_MISMATCH`；CP 不得重发动作、不得执行 readiness、不得放行严格下游，但无依赖分支继续执行。

手工 Run/Retry 的 `reason` 可省略或为 `null`；提供非 null 值时必须为 1–512 个 Unicode 字符，空字符串非法。

CP 对全部非终态 Run 建立 `managed_service_id` 独占记录；不同组或 Run 涉及同一服务时返回 `409 SERVICE_IN_ACTIVE_RUN`，终态提交时释放。CP 崩溃后独占记录随原 Run 一起恢复，不得另开 Run 绕过。

## 8. CP API 与最小 Web

CP API 固定提供：

- Agent 注册/心跳、节点/服务列表。
- `POST /api/v1/services/{managed_service_id}/actions/{action}`（代理到所属 Agent 的 local id）。
- Recovery group CRUD、成员替换、依赖替换、probe 设置、arm/disarm。
- `POST /api/v1/recovery-groups/{group_id}/runs` 与 `POST /api/v1/recovery-runs/{run_id}/retry`。
- `GET /api/v1/recovery-runs`：公开发现 AUTO/MANUAL Run，支持可选 `group_id`、`trigger`、`status` 过滤。
- `GET /api/v1/recovery-runs/{run_id}`。

Run 列表只允许管理员签名 Session 读取，响应固定为 `{items, next_cursor}`。列表按
`created_at DESC, run_id DESC` 做稳定 keyset 分页；`limit` 默认 50、最小 1、最大 100。
`cursor` 是不透明、带版本且绑定当前过滤条件的游标，调用方不得解析或修改；下一页必须严格
位于上一页最后一项之后，因此时间戳相同的 Run 仍不得重复或遗漏。格式错误、字段非法、版本
不支持、游标与当前过滤条件不一致均统一返回 `422 VALIDATION_ERROR`，响应不得包含解码器、SQL、
堆栈或游标内部字段。后台 Scheduler 创建的 AUTO Run 必须无需预知 `run_id` 即可由此接口及
Dashboard 最近 Run 区域发现并进入详情页。

Web 只有 Dashboard、Recovery Groups、Run Detail 三页；使用服务端模板和本地静态 JS，不引入 React、拓扑图、日志中心或模板市场。

## 9. 最低安全边界

- `GET /healthz` 免鉴权且只返回最小存活信息。
- `cluster_token` 只允许 Agent 注册/心跳以及 CP 调用 Agent；它不能访问恢复组、Run、代理动作或 Web 管理 API。
- CP 注册/心跳入口同时按 socket peer 校验 `agent_source_cidrs`，不读取 `X-Forwarded-For`；缺失/错误 Token 返回 401，来源拒绝返回 403。
- 管理 API 只接受管理员签名 session；所有写请求还必须通过 CSRF。Web 使用单管理员密码和签名、HttpOnly、SameSite session。
- Agent 同时限制 CP 来源 IP，只使用 socket peer，不接受 `X-Forwarded-For`。
- Agent 默认禁用 CORS，不接受浏览器直连。
- `agent.json`、SQLite 与日志目录应用仅服务账户/Administrators 可读的 ACL；日志脱敏 Token。
- cluster token 必须来自至少 32 个随机字节，使用常量时间比较，只能通过 Authorization header 传输，禁止 URL/query。
- Agent 的 `advertised_endpoint` 必须使用非 unspecified、非 multicast 的 IP literal，其端口必须等于 `listen_port`；具体 IP `listen_host` 必须与 advertised IP 相同。任一网络字段使用 loopback 时，`listen_host`、advertised IP、Control Plane URL 与 CP 来源 host CIDR 必须全部为 loopback。`control_plane_url` 使用 IP literal 时，该地址必须落入 `control_plane_source_cidrs`，避免配置校验通过后才在注册或回连阶段暴露错误边界。
- Control Plane 的 `listen_host` 必须是非 multicast IP literal；`0.0.0.0`、`::` 和明确的 loopback 地址允许作为监听绑定。`admin_password_hash` 只接受完整四段 `pbkdf2_sha256` 表示：100000–1000000 次迭代、16–64 字节 canonical URL-safe base64 salt，以及 32 字节 digest；格式错误必须在监听、建库和服务安装前脱敏拒绝。
- 明文 HTTP 下 Token、密码和控制流可被同网段攻击者窃听或篡改，因此构建必须显示 `LAB_HTTP / production_ready=false`；不得用于生产或真实秘密。

## 10. 存储与迁移

- Agent 与 CP SQLite 均固定 `journal_mode=WAL`、`synchronous=FULL`、`foreign_keys=ON` 和非零 `busy_timeout`。
- 两端都有显式整数 Schema version，只允许按顺序、事务化向前迁移；数据库版本高于程序支持版本时拒绝启动，禁止静默降级或清空。
- CP schema v5 在升级事务中验证 Agent、当前可见服务和恢复组均不超过 1024，并创建仅覆盖 `seen_in_last_report=1` 的服务索引。v4 数据恰好 1024 项可升级；任一集合为 1025 项时迁移原子拒绝、版本保持 v4、所有数据不变。程序不得截断列表或自动删除 tombstone；操作者必须先备份并按受审计的离线修复流程处理旧库。
- Agent Operation、CP RecoveryRun/Step、图与探针快照、服务独占、probe attempt 均须持久化。Run Detail 必须能显示每一次 probe attempt 的时间、耗时、结果码和脱敏消息。
- 时间持久化为 UTC RFC 3339；超时与在线租约计算在同一进程内使用 monotonic clock，不能信任客户端时间或把重启前的墙钟 `received_at` 当作当前在线证明。CP 重启后租约必须 fail closed，等待新有效报告重建。

## 11. 稳定错误码

| 领域 | 错误码 |
|---|---|
| 认证/来源 | `AUTH_REQUIRED`, `AUTH_INVALID`, `SOURCE_IP_DENIED`, `STALE_AGENT_INSTANCE`, `ENDPOINT_SOURCE_MISMATCH` |
| 请求/幂等 | `VALIDATION_ERROR`, `IDEMPOTENCY_KEY_REQUIRED`, `IDEMPOTENCY_KEY_INVALID`, `IDEMPOTENCY_KEY_REUSED` |
| 框架 | `ROUTE_NOT_FOUND`, `METHOD_NOT_ALLOWED`, `INTERNAL_ERROR` |
| 服务/动作 | `AGENT_NOT_FOUND`, `AGENT_OFFLINE`, `SERVICE_NOT_ALLOWLISTED`, `SERVICE_MAPPING_CHANGED`, `SERVICE_NOT_INSTALLED`, `SERVICE_STATE_UNKNOWN`, `SERVICE_ACTION_CONFLICT`, `SERVICE_IN_ACTIVE_RUN`, `SCM_ACTION_FAILED`, `SCM_ACTION_TIMEOUT`, `OPERATION_NOT_FOUND` |
| 探针 | `PROBE_TARGET_DENIED`, `PROBE_UNSUPPORTED`, `PROBE_FAILED` |
| 恢复 | `DEPENDENCY_CYCLE`, `GROUP_NOT_READY`, `RUN_NOT_FOUND`, `AGENT_PROTOCOL_MISMATCH` |

所有错误体固定为 `{code, message, detail, request_id}`；`message` 不含 Token、密码、响应正文或底层命令行。

## 12. 打包与部署合同

- Agent 与 CP 使用 PyInstaller **onedir** 打包，禁止 onefile 临时解压模式；配置、SQLite 与日志位于可审计的外部数据目录。
- 发布包必须包含 `recovery-deployment-inventory-v1.md`、Inventory example 和本机只读 Host Facts 脚本。Inventory 只接受非秘密事实，严格证明独立 CP、至少三 Agent、五个验收角色、本机 readiness 与 DAG；渲染草案固定 `config_ready=false` 且所有秘密为权威配置加载器必然拒绝的 sentinel。Host Facts 不接受远程参数且固定 `side_effects=NONE`、`remote_hosts_scanned=0`。详细线上字段、原子输出和验收以 Inventory 合同为准。
- 安装脚本只能使用已提供的 WinSW 可执行文件，或下载配置中明确写死的版本 URL；必须按已评审 lock 校验 SHA-256、size 与 Authenticode 状态。源文件校验通过后，复制到唯一 staging 的 wrapper 和发布到最终 `DataDirectory\service` 的 wrapper 必须分别重新执行同一组校验，最终校验位于 SCM install 之前；任一次不符均进入事务回滚且不得调用 SCM。禁止 GitHub `latest`、运行时选最新版或未校验下载。
- Agent 与 CP 自身注册为 `Automatic`；业务服务仍由既有 GUI/原生安装器管理并保持 `Manual`。脚本不得安装、卸载或修改业务服务。
- 安装是一次有所有权边界的本机事务。任何文件或 SCM 副作用前，目标 Recovery 服务、`DataDirectory\package`、`DataDirectory\service` 以及本角色的安装 staging 必须全部不存在；安装器不得覆盖、合并或自动清理无法证明属于本次执行的残留。
- 安装器必须用逐阶段 journal 标记 staging、发布、ACL、SCM install/start 与临时下载。失败时只回滚本次创建的路径；若本次注册了 Recovery 服务，先在确认 wrapper 路径属于本次执行后 best-effort stop，再 uninstall。配置、SQLite、日志、数据根目录及其中其他文件一律保留。
- ACL 修改前必须保存既有路径的 Owner+DACL；失败时恢复本次已经触及的既有路径。若无法证明服务所有权、服务仍存在或 ACL/文件回滚不完整，安装器必须保留服务运行所需的本次 package/service 文件，返回同时包含原始失败与稳定回滚问题码的聚合错误，并阻止直接重试，不能制造指向已删除 wrapper 的错误服务。
- 回滚后只有 Recovery 服务以及本角色 package/service/staging 均不存在时才声明 `retry_safe=true`；此时用同一配置再次执行必须可重试。成功路径仍固定为 `Automatic + LocalSystem`，且不触碰任何业务服务。
- 每台主机的一个 `database_path + listen_port` 组合只允许一个 Agent 进程使用。受支持部署只能由安装器创建的固定 WinSW Agent 服务拥有该配置、SQLite 与端口；禁止同时运行源码开发进程、复制第二个 Recovery Agent 服务或让另一份配置复用同一数据库/端口。MVP 不以第二套持久隔离协议支持多实例；启动前端口为空和安装后全部 listener 归属固定 WinSW 进程树是强制门禁。
- `PostInstall` 的 listen port 归属是全称约束：该端口观察到的每一个唯一 listener PID 都必须同时匹配预期角色 EXE，并在最多八级父进程链内回到 SCM 报告的本次 WinSW wrapper PID。只要存在一个额外、不可查询、路径不符或不在该进程树内的 PID，整项检查即失败；不得以“至少找到一个合法 PID”判定通过。

## 13. MVP 验收

至少三台 Windows Server 上跑通：`MySQL + Redis -> Nacos -> Java -> Nginx`。连续 10 次随机冷启动必须满足：零乱序、同 epoch 零重复 AUTO Run、未知服务零操作；并覆盖 CP 最后上线、缺节点、乱序心跳、单机重启、start/probe 失败、Agent 断线及 CP 中途退出恢复。

实现前的自动化验收还必须覆盖：旧 instance fencing；同 key 并发与异指纹；全部 SCM/启动类型映射；本机探针的 DNS/代理/重定向/userinfo 绕过；settle 第 119 秒二次重启与中途掉线；arm 后改 Automatic/卸载；CP 在 dispatch key 已保存但 operation_id 未保存时退出；Agent 在 Operation PREPARED 和 DISPATCHING 窗口退出；cluster token 调管理员 API被拒；伪造 endpoint 不得触发 CP SSRF。

行为状态机以本文为准，线上字段、路径与响应以 OpenAPI 为准；任何冲突均阻止冻结。每项 MUST 约束必须在追踪矩阵中映射到至少一个接口/字段和一个可执行验收场景。
