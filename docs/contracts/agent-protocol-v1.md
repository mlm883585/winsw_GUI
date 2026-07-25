# Agent Protocol v1 — 行为、状态与持久化合同

> 状态：**Phase 2 冻结合同**。本文定义 Agent 的行为语义、状态机、持久化、恢复、安全和后端边界。线上字段与 HTTP 响应以 [`agent-openapi.yaml`](../api/agent-openapi.yaml) 为权威，配置字段以 [`service-config-v1.md`](./service-config-v1.md) 为权威；两者与本文出现冲突时必须先修正文档，禁止由实现自行选择语义。
>
> 本文中的“必须 / 禁止 / 应当 / 可以”分别对应 RFC 2119 的 MUST / MUST NOT / SHOULD / MAY。

## 1. 范围、权威与不变量

### 1.1 Phase 2 交付边界

Phase 2 Agent 是单机服务管理的唯一写入口，交付以下能力：

- 持久化并读写 `ServiceConfig v1`；
- Windows/WinSW 服务的安装、卸载、启动、停止、重启和开机启动策略管理；
- 四维服务状态和增量日志查询；
- 同步配置写入、同步删除、异步生命周期 Operation；
- Bearer Token、TLS、来源 CIDR 限制、秘密加密和最小 ACL；
- 旧 WinSW XML 的一次性预检与导入。

Control Plane、跨机拓扑、编排组、依赖和探针定义不属于 Phase 2。Phase 4 使用的单次探针与 liveness profile 在本文冻结合同，但 Phase 2 必须报告对应 capability 为 `false`，不得宣称已经执行。

### 1.2 数据权威

| 数据 | 唯一权威 | 派生或镜像 | 强制约束 |
|---|---|---|---|
| 单服务运行配置 | Agent SQLite | WinSW XML；未来 systemd unit | XML/unit 不能反向覆盖 SQLite |
| 配置 revision 与秘密 | Agent SQLite | API Read 模型 | revision 不写入配置主体；秘密不回显 |
| 安装、运行、开机策略 | 操作系统即时状态 | SQLite 最近观测值、CP 镜像 | 返回状态前应查询 backend，不得用缓存伪造成功 |
| Operation 与幂等结果 | Agent SQLite | CP 编排步骤引用 | 必须跨 Agent 重启可查询 |
| 跨机依赖、编排组、探针 desired 定义 | Control Plane | Phase 4 liveness profile 的 Agent applied 副本 | 不进入 `ServiceConfig` 或 WinSW XML |

由此产生以下不可破坏的不变量：

1. Agent 接管后，任何 GUI、脚本或 Control Plane 都只能经 Agent API 修改受管配置。
2. WinSW XML 是可重建的派生产物；Agent 不把外部 XML 改动解析回 SQLite。
3. 配置写入不隐式执行 install、start、stop 或 restart。
4. install、运行状态和开机自启是三个独立维度，任何动作不得暗含另一个动作。
5. 任何可能触碰系统服务或派生文件的请求，必须先持久化 Operation，再发生外部副作用。
6. 无法证明副作用结果时必须暴露 `UNKNOWN`，禁止猜测成功、盲目重放或仅凭超时判定失败。

### 1.3 时间、标识与编码

- API 时间统一为带 `Z` 的 UTC RFC 3339 字符串，精度固定到毫秒；SQLite 内统一保存 Unix epoch 毫秒整数。
- `service_id`、路径和配置规范见 `ServiceConfig v1`；比较 `service_id` 时区分大小写，但合法 ID 本身仅允许小写 slug。
- `operation_id` 由 Agent 生成，使用 UUIDv4 的小写连字符格式。
- HTTP JSON 使用 UTF-8；请求和响应 `Content-Type` 必须为 `application/json`，日志端点除外的写请求不接受其他媒体类型。
- 哈希均使用 SHA-256，小写十六进制表示；密码、Token、秘密明文以及包含秘密的完整请求体禁止作为哈希旁的可读字段保存。

## 2. HTTP 资源与执行模型

### 2.1 端点分组

| 交付阶段 | 方法与路径 | 行为 |
|---|---|---|
| Phase 2 | `GET /healthz` | 免鉴权，仅返回最小存活信息 |
| Phase 2 | `GET /api/v1/agent` | 鉴权后返回版本、平台、Schema 与 capabilities |
| Phase 2 | `GET /api/v1/services` | 返回受管服务摘要列表 |
| Phase 2 | `GET /api/v1/services/{service_id}` | 返回 ServiceConfig Read envelope 与组合状态 |
| Phase 2 | `PUT /api/v1/services/{service_id}` | 同步创建、更新、no-op 或修复漂移 |
| Phase 2 | `DELETE /api/v1/services/{service_id}` | 同步删除未安装服务的 Agent 配置 |
| Phase 2 | `GET /api/v1/services/{service_id}/status` | 查询四维即时状态 |
| Phase 2 | `POST /api/v1/services/{service_id}/actions/{action}` | 异步执行七个生命周期动作 |
| Phase 2 | `GET /api/v1/services/{service_id}/logs` | 使用不透明 cursor 增量读取日志 |
| Phase 2 | `GET /api/v1/operations/{operation_id}` | 查询 Operation 的确定结果或不确定状态 |
| Phase 2 | `POST /api/v1/operations/{operation_id}/acknowledge-unknown` | 人工确认已知风险并解除服务隔离 |
| Phase 4 | `POST /api/v1/probe` | 执行一次、不驻留的 startup/readiness 探针 |
| Phase 4 | `GET /api/v1/services/{service_id}/liveness-profile` | 查询 Agent applied liveness profile |
| Phase 4 | `PUT /api/v1/services/{service_id}/liveness-profile` | 按 revision 下发、禁用或 tombstone profile |

除 `/healthz` 外全部端点必须鉴权。`/healthz` 只能返回 `status` 和当前时间，不得返回版本、主机名、OS、监听地址、服务数量、路径或 capabilities。

### 2.2 同步 PUT

请求正文固定为 `{expected_revision, config}`：创建时 `expected_revision` 为 `null`，更新时为当前正整数。Agent 必须在 HTTP 响应前完成校验、SQLite 提交、派生产物替换和 Operation 终态持久化。

| 场景 | HTTP | revision | `changed` | 其他结果 |
|---|---:|---:|---:|---|
| 新建 | `201` | `1` | `true` | `config_state=CURRENT` |
| 语义配置发生变化 | `200` | 原值 `+1` | `true` | 运行中服务返回 `RESTART_REQUIRED` |
| 规范化后配置完全相同 | `200` | 不变 | `false` | 不重写秘密、不制造新 revision |
| `DRIFTED` 且提交同一权威配置 | `200` | 不变 | `false` | 重建 XML，`artifact_repaired=true` |
| revision 不匹配 | `409` | 不变 | — | `REVISION_CONFLICT`，无副作用 |

创建时已有同名服务必须返回 `409 SERVICE_ALREADY_EXISTS`。更新时 URL ID 必须等于配置中的 `service_id`；服务 ID 不允许原地修改。

当 `ConfigState=DRIFTED` 时，只有“当前 revision + 与 SQLite 权威配置语义相同”的 PUT 被解释为显式修复。提交不同配置必须返回 `409 CONFIG_DRIFTED`，调用方先修复漂移、重新读取，再做正常更新，防止无意覆盖外部改动。

WinSW 注册期字段与运行期字段采用不同更新门控：

| 字段类别 | 字段 | 已安装时的 PUT 语义 |
|---|---|---|
| 注册期 | `name`、`description`、`account`、`recovery`、`process.interactive` | 只要规范化值发生变化即返回 `409 CONFIG_CHANGE_REQUIRES_UNINSTALL`；必须显式 stop → uninstall → PUT → install |
| 运行期 | `runtime`、`environment`、`logging`、`process.priority`、`process.stop_timeout_seconds` | 允许更新；ACTIVE 时为 `RESTART_REQUIRED`，INACTIVE 时为 `CURRENT` |

注册期字段相同的 no-op PUT 不触发卸载要求。Agent 不得为了让 PUT 成功而隐式 stop、uninstall、install 或 restart。

### 2.3 同步 DELETE

DELETE 只删除 Agent 主本、加密秘密、派生产物和该服务可安全清理的本地元数据；不删除程序、工作目录、业务日志和历史 Operation。

删除前必须同时满足：

- `InstallationState=NOT_INSTALLED`；
- 状态组合一致且任何维度均非 `UNKNOWN`；
- 服务没有进行中的 Operation 或未确认的 `UNKNOWN` Operation；
- `Idempotency-Key` 合法且未与其他请求冲突。

成功固定返回 `200 DeleteResult`。不存在的服务返回 `404 SERVICE_NOT_FOUND`；不得把普通 DELETE 实现为“存在则删、不存在也成功”。已安装返回 `409 SERVICE_STILL_INSTALLED`。DELETE 不隐式 uninstall 或 stop。

### 2.4 异步 action

动作集合固定为：

`install / uninstall / start / stop / restart / enable-autostart / disable-autostart`

Agent 在接受动作前完成认证、请求格式、幂等键、服务隔离、写锁和可即时判定的前置条件检查。接受后返回 `202 Operation`，并设置：

```http
Location: /api/v1/operations/{operation_id}
```

`202` 只表示已持久化并获准执行，不代表动作成功。调用方必须查询 Operation 至终态。若服务写锁已占用或前置条件不满足，Agent 持久化一个 `REJECTED` Operation，并返回 `409` 结构化错误，`detail.operation_id` 指向该记录。

对已在目标态的幂等动作，Agent 仍创建 Operation，并以 `SUCCEEDED`、`result.changed=false` 结束；不得再次调用 WinSW。

### 2.5 错误体

错误体固定为：

```json
{
  "code": "REVISION_CONFLICT",
  "message": "服务配置已被其他请求更新",
  "detail": {
    "service_id": "orders-api",
    "expected_revision": 3,
    "actual_revision": 4,
    "operation_id": "2d6be112-8d7f-4a06-b94f-8f39262560db",
    "trace_id": "0dc7f1bfb1854fa89aa10a62b278a4fe"
  }
}
```

- `code` 是供客户端分支处理的稳定枚举；`message` 是面向人的说明，客户端不得解析它；`detail` 必须是对象。
- `detail` 只能包含白名单字段，禁止放入请求原文、环境变量值、账户密码、Token、子进程完整命令行或未脱敏输出。
- 写请求一旦创建 Operation，任何错误响应都必须在 `detail.operation_id` 返回其 ID。
- 后端命令在已返回 `202` 后失败时，HTTP 响应不再变化；错误写入 Operation 的结构化 `error`。

最低稳定错误码如下；OpenAPI 可以增加更细错误码，但不得复用下列语义：

| HTTP | code | 语义 |
|---:|---|---|
| 400 | `IDEMPOTENCY_KEY_REQUIRED` / `INVALID_IDEMPOTENCY_KEY` | 缺失或不是规范 UUIDv4 |
| 401 | `AUTHENTICATION_REQUIRED` / `INVALID_TOKEN` | 未通过 Bearer Token 校验 |
| 403 | `SOURCE_NOT_ALLOWED` | 来源 IP 不在 CIDR allowlist |
| 404 | `SERVICE_NOT_FOUND` / `OPERATION_NOT_FOUND` | 资源不存在 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同一键对应不同请求指纹 |
| 409 | `IDEMPOTENT_REQUEST_IN_PROGRESS` | 同步请求的既有执行尚未结束 |
| 409 | `REVISION_CONFLICT` / `SERVICE_ALREADY_EXISTS` | 乐观锁或身份冲突 |
| 409 | `CONFIG_CHANGE_REQUIRES_UNINSTALL` | 已安装服务的注册期字段发生变化 |
| 409 | `ACTION_IN_PROGRESS` | 同一服务写锁被占用 |
| 409 | `ACTION_NOT_ALLOWED` | 当前确定状态不允许该动作 |
| 409 | `STATE_UNKNOWN` / `STATE_INCONSISTENT` | 状态未知或组合矛盾，禁止盲发写动作 |
| 409 | `CONFIG_DRIFTED` | 派生产物被外部修改且请求不是显式修复 |
| 409 | `SERVICE_STILL_RUNNING` / `SERVICE_STILL_INSTALLED` | stop/uninstall/delete 前置条件不满足 |
| 409 | `OPERATION_RESULT_UNKNOWN` | 服务仍被不确定 Operation 隔离 |
| 410 | `LOG_CURSOR_EXPIRED` | cursor 对应内容已轮转或淘汰 |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 写请求不是 JSON |
| 422 | `UNSUPPORTED_SCHEMA_VERSION` | Agent 不支持配置 Schema |
| 422 | `BACKEND_VALIDATION_FAILED` | 配置无法安全映射到当前 backend |
| 500 | `BACKEND_EXECUTION_FAILED` | 同步后端执行确定失败 |
| 500 | `OPERATION_RESULT_UNKNOWN` | 同步请求发生副作用后无法证明最终结果 |
| 501 | `CAPABILITY_NOT_AVAILABLE` | 调用了当前 Agent 尚未交付的 Phase 4 能力 |

## 3. 幂等、Operation 与并发

### 3.1 Idempotency-Key 和请求指纹

所有 `PUT`、`POST`、`DELETE` 请求必须携带：

```http
Idempotency-Key: 52a42f95-224e-4baf-a339-2f25f42844cf
```

约束如下：

1. 值必须是 RFC 4122 variant、version 4、小写连字符规范格式；作用域是单个 Agent 全局，而非单服务或单端点。
2. 客户端每个逻辑写意图生成一个新键；网络重试复用原键，业务重试必须使用新键。
3. Agent 使用 RFC 8785 JSON Canonicalization Scheme 规范化 JSON；空正文规范化为空字节串。
4. 请求指纹固定为以下 UTF-8 字节的 SHA-256：

   `UPPER_METHOD + "\n" + normalized_path + "\n" + canonical_query + "\n" + media_type + "\n" + canonical_body`

   其中 path 使用路由解析后的规范 ID，query 按百分号解码后的键和值做 Unicode code point 排序再以 RFC 3986 重新编码；`media_type` 去除参数并转小写。`Authorization`、`Idempotency-Key`、连接信息、客户端 IP 和非语义 Header 不参与指纹。
5. 同一键、同一指纹且原请求已结束时，返回原 HTTP 状态、原响应正文以及原 `Location`；不得再次执行。
6. 同一键、不同指纹返回 `409 IDEMPOTENCY_KEY_REUSED`，不得泄露原请求正文，只返回原指纹前 12 位供排障。
7. 同步 PUT/DELETE 的重复请求仍在运行时返回 `409 IDEMPOTENT_REQUEST_IN_PROGRESS` 和既有 `operation_id`；异步 action 返回既有 Operation 和 `202`。

幂等记录与对应 Operation 使用同一 SQLite 事务首次落库。若在这一步失败，不得发生任何外部副作用。

### 3.2 Operation 数据合同

每个服务配置 PUT、DELETE、生命周期 action、导入 commit 批次、Phase 4 liveness profile 写入以及自动恢复动作，都必须有 Operation。一次本机导入 commit 使用一个 batch Operation及一个 Agent 内部生成的幂等键，不为每个 item 创建互不关联的顶层 Operation。Operation 至少持久化：

| 字段 | 约束 |
|---|---|
| `operation_id` | Agent 生成的 UUIDv4，主键 |
| `idempotency_key` | 外部写请求的键；内部恢复动作使用 Agent 生成的 UUIDv4 |
| `request_fingerprint` | 64 位 SHA-256 十六进制串 |
| `kind` | `CONFIG_UPSERT / CONFIG_DELETE / ACTION / IMPORT / LIVENESS_PROFILE / LIVENESS_RECOVERY` |
| `service_id` | 单服务写操作必填；IMPORT batch 为 `null`，受影响 ID 写入脱敏 result items |
| `action` | `kind=ACTION` 或自动恢复时填写七动作之一 |
| `initiator` | `API / LEGACY_IMPORT / LIVENESS`；不保存 Token |
| `status` | 本节固定状态枚举 |
| `result` | 成功时的结构化、脱敏结果；大小受限 |
| `error` | 失败或拒绝时的 `{code,message,detail}`；必须脱敏 |
| `created_at/started_at/finished_at` | UTC 时间；尚未发生的时间为 `null` |
| `acknowledgement` | 仅 UNKNOWN 可有的人工确认元数据 |

Operation 状态固定为：

`PENDING / RUNNING / SUCCEEDED / FAILED / REJECTED / UNKNOWN`

| 当前状态 | 允许后继 | 语义 |
|---|---|---|
| `PENDING` | `RUNNING / REJECTED` | 已持久化，尚未开始外部副作用 |
| `RUNNING` | `SUCCEEDED / FAILED / UNKNOWN` | 已进入可能产生副作用的区域 |
| `SUCCEEDED` | 无 | 结果已证明成功，包括目标态 no-op |
| `FAILED` | 无 | 已证明动作未达到目标或后端确定失败 |
| `REJECTED` | 无 | 前置条件不满足，且已证明没有外部副作用 |
| `UNKNOWN` | 无 | 发生过或可能发生过副作用，但最终结果不可证明 |

终态不可被重写；尤其禁止将 `UNKNOWN` 改成 `SUCCEEDED` 或 `FAILED`。`result` 和 `error` 的 stdout/stderr 摘要各最多 64 KiB，超出时设置 `truncated=true`。

### 3.3 服务写锁与隔离

- 同一 `service_id` 的 PUT、DELETE、七个 action、导入变更和 Phase 4 自动恢复共用一把互斥写锁；不同服务可以并行。
- 锁必须由 SQLite 中的持久化 reservation 与进程内互斥量共同实现，不能只依赖进程内对象。Agent 重启时必须重建 reservation。
- API 不在服务端排队等待锁。新请求不能立即获得锁时，创建 `REJECTED` Operation 并返回 `409 ACTION_IN_PROGRESS`，其中包含占用锁的 `operation_id`。
- 只读请求允许并发；状态响应在有写锁时设置 `transitional=true` 并返回 `active_operation_id`。
- Operation 进入 `UNKNOWN` 后释放执行锁，但为服务建立持久化 quarantine。quarantine 期间所有服务写请求返回 `409 OPERATION_RESULT_UNKNOWN`；只读查询继续可用并显示隔离原因。

### 3.4 UNKNOWN 人工确认

`POST /api/v1/operations/{operation_id}/acknowledge-unknown` 只接受 `UNKNOWN` Operation。确认记录必须包含操作者提供的 disposition、非空说明、确认时间和请求追踪信息；disposition 固定为：

- `OBSERVED_SUCCEEDED`：操作者在系统外确认目标状态已达到；
- `OBSERVED_FAILED`：操作者确认目标状态未达到；
- `ACCEPT_RISK`：无法判定，但接受风险并继续人工处置。

确认行为只做两件事：追加不可变 acknowledgement，解除该 Operation 建立的 service quarantine。原 Operation 的 `status` 永久保持 `UNKNOWN`，其 `result/error` 不被伪造。解除后调用方必须重新读取四维状态；确认接口本身不重放原动作。

### 3.5 保留与清理

- `SUCCEEDED/FAILED/REJECTED`、已确认的 `UNKNOWN` 及其幂等结果默认保留 30 天，从 `finished_at` 或 `acknowledged_at` 起算。
- 保留期可配置，但硬下限为 7 天；Operation 与幂等记录必须同批删除，不能留下会导致重复执行的半份记录。
- `PENDING/RUNNING` 不按时间清理；启动恢复必须先处理。
- 未确认的 `UNKNOWN`、其 quarantine 和 acknowledgement 要求不得自动删除。
- 清理任务只能删除终态且无 journal、锁、quarantine 或导入批次引用的记录，并以小事务分批执行。

## 4. 四维状态合同

### 4.1 固定枚举

| 维度 | 枚举 | 含义 |
|---|---|---|
| `ConfigState` | `CURRENT` | SQLite 主本与派生产物一致；若进程运行，已知使用当前 revision |
|  | `RESTART_REQUIRED` | 派生产物已更新，但当前 revision 尚未由后续成功的 start/restart 证明已应用 |
|  | `INVALID` | 已存配置因 backend、版本或外部依赖变化而不再可渲染/执行 |
|  | `DRIFTED` | 派生产物缺失或其 SHA-256 与 SQLite 记录不一致 |
|  | `UNKNOWN` | 无法读取主本、派生产物或已应用 revision |
| `InstallationState` | `INSTALLED / NOT_INSTALLED / UNKNOWN` | 是否注册为系统服务 |
| `RuntimeState` | `ACTIVE / INACTIVE / STARTING / STOPPING / FAILED / UNKNOWN` | 当前系统服务运行态，不含 NOT_INSTALLED |
| `StartupState` | `AUTOSTART_ENABLED` | 已安装且会随系统启动 |
|  | `AUTOSTART_DISABLED` | 已安装、手动启动，仍允许显式 start |
|  | `START_BLOCKED` | 已安装但平台策略禁止显式 start，例如 Windows Disabled/systemd masked |
|  | `NOT_APPLICABLE` | 未安装，无法设置启动策略 |
|  | `UNKNOWN` | 无法读取启动策略 |

`restart_required` 是 `ConfigState=RESTART_REQUIRED` 的兼容布尔投影，二者必须一致。

### 4.2 组合规则

1. `NOT_INSTALLED` 的确定组合必须是 `RuntimeState=INACTIVE`、`StartupState=NOT_APPLICABLE`。
2. `INSTALLED` 时 `StartupState` 不得为 `NOT_APPLICABLE`。
3. `RESTART_REQUIRED` 可与已安装服务的 `ACTIVE/INACTIVE/FAILED/STARTING/STOPPING` 组合；只有后续 start/restart 成功并确认新实例使用当前 revision 后才能转为 `CURRENT`，不能仅因 stop 或进程退出而提前清除。
4. `DRIFTED` 由当前派生产物哈希与 SQLite `artifact_hash` 比较得出；不得根据 XML mtime 单独判定。
5. backend 返回互相矛盾的事实时，Agent保留每个维度的最佳观测值，同时设置 `consistent=false` 和结构化 diagnostics；禁止悄悄把矛盾值改成看似正常的组合。
6. 任一动作前必须执行一次即时状态查询。任一维度为 `UNKNOWN` 或 `consistent=false` 时，所有七个动作均被拒绝；只允许读取、UNKNOWN acknowledgement，以及符合条件的漂移修复 PUT。

状态响应必须包含 `observed_at`、`transitional`、`consistent`、四个枚举和 diagnostics；不得只返回 WinSW 原始字符串。原始输出只可作为经过限长、脱敏的诊断信息。

### 4.3 派生产物漂移

Agent 每次 status、install、start、restart 和 PUT 前必须验证 XML 存在且哈希匹配。外部删除或修改 XML 后：

- 立即报告 `ConfigState=DRIFTED`；
- install、start、restart 返回 `409 CONFIG_DRIFTED`；
- 不把 XML 内容合并回 SQLite；
- stop、uninstall、enable/disable-autostart 仅在其他状态确定且一致时仍可用于安全处置；
- 使用当前 `expected_revision` 提交与 SQLite Read 模型语义相同的 PUT，Agent 从权威主本重新渲染，revision 不变；
- 修复行为必须产生 Operation 和审计友好的 `artifact_repaired=true` 结果。

## 5. 七动作决策矩阵

### 5.1 通用门控

动作执行顺序固定为：认证与来源检查 → 幂等去重 → 读取服务 → quarantine 检查 → 获取服务写锁 → 即时状态与一致性检查 → 动作专属前置条件 → Operation `RUNNING` → backend 调用 → 重新查询目标状态 → Operation 终态。

除 stop、uninstall 和启动策略处置外，`INVALID/DRIFTED` 不允许会加载派生产物的动作。任何 `UNKNOWN` 或过渡态都不得盲目下发另一个写动作。

### 5.2 动作矩阵

| 动作 | 允许前态 | 已在目标态 | 确定拒绝条件 | 成功后态与禁止的隐式行为 |
|---|---|---|---|---|
| `install` | config=`CURRENT`；install=`NOT_INSTALLED`；runtime=`INACTIVE`；startup=`NOT_APPLICABLE` | install=`INSTALLED` 且状态一致时 `SUCCEEDED/changed=false` | config 为 `INVALID/DRIFTED`；任何 UNKNOWN/过渡或矛盾 | 注册为系统服务；startup=`AUTOSTART_DISABLED`；runtime=`INACTIVE`；**不 start、不启用自启** |
| `uninstall` | install=`INSTALLED`；runtime=`INACTIVE`；startup 已知 | install=`NOT_INSTALLED` 时 no-op 成功 | runtime=`ACTIVE/STARTING/STOPPING/FAILED` 或未知；状态矛盾 | 只注销系统服务；保留 SQLite 配置和主本；startup=`NOT_APPLICABLE`；**不 stop、不 delete** |
| `start` | config=`CURRENT/RESTART_REQUIRED`；install=`INSTALLED`；runtime=`INACTIVE/FAILED`；startup=`AUTOSTART_ENABLED/AUTOSTART_DISABLED` | runtime=`ACTIVE` 时 no-op 成功；若 config=`RESTART_REQUIRED`，不得把 no-op 伪装为已应用新 revision | 未安装；`START_BLOCKED`；runtime=`STARTING/STOPPING`；config=`INVALID/DRIFTED/UNKNOWN` | runtime 最终为 `ACTIVE`，成功启动新实例后 config=`CURRENT`；**不 install、不改 startup** |
| `stop` | install=`INSTALLED`；runtime=`ACTIVE` 或 `FAILED`；其他维度确定一致 | runtime=`INACTIVE` 时 no-op 成功 | 未安装；runtime=`STARTING/STOPPING/UNKNOWN`；状态矛盾 | runtime 最终为 `INACTIVE`；**不 uninstall、不改 startup** |
| `restart` | config=`CURRENT/RESTART_REQUIRED`；install=`INSTALLED`；runtime=`ACTIVE/FAILED`；startup 非 `START_BLOCKED` | 无 | 未安装；runtime=`INACTIVE/STARTING/STOPPING`；config=`INVALID/DRIFTED/UNKNOWN` | ACTIVE 时完成 stop+start，FAILED 时执行受控重新启动；成功后 config=`CURRENT`；**失败不谎报回滚** |
| `enable-autostart` | install=`INSTALLED`；startup=`AUTOSTART_DISABLED`；runtime 非过渡且全状态确定 | startup=`AUTOSTART_ENABLED` 时 no-op 成功 | 未安装、startup=`START_BLOCKED/NOT_APPLICABLE/UNKNOWN`、任一过渡/矛盾 | startup=`AUTOSTART_ENABLED`；**不 start、不 stop、不解除 START_BLOCKED** |
| `disable-autostart` | install=`INSTALLED`；startup=`AUTOSTART_ENABLED`；runtime 非过渡且全状态确定 | startup=`AUTOSTART_DISABLED` 时 no-op 成功 | 未安装、startup=`START_BLOCKED/NOT_APPLICABLE/UNKNOWN`、任一过渡/矛盾 | startup=`AUTOSTART_DISABLED`（可手动启动）；**不使用 Disabled/mask，不 start/stop、不解除 START_BLOCKED** |

`FAILED` 是否能被 stop 必须由 backend capability 明确支持；Windows backend 必须先确认 SCM 仍存在可停止实例。无法确认时返回 `STATE_UNKNOWN`，不能尝试 restart 代替 stop。

### 5.3 目标态确认

backend 子进程退出码为 0 不是成功的充分条件。每个动作必须在命令完成后轮询即时状态，直到达到以下目标或动作超时：

| 动作 | 目标判据 |
|---|---|
| install | `INSTALLED + INACTIVE + AUTOSTART_DISABLED` |
| uninstall | `NOT_INSTALLED + INACTIVE + NOT_APPLICABLE` |
| start | `INSTALLED + ACTIVE`；原 config 为 RESTART_REQUIRED 时同时确认 `CURRENT` |
| stop | `INSTALLED + INACTIVE` |
| restart | 观测到本次动作后的新 active 实例，且 `ACTIVE + CURRENT` |
| enable-autostart | `AUTOSTART_ENABLED`，运行态保持不变 |
| disable-autostart | `AUTOSTART_DISABLED`，运行态保持不变 |

达到超时后若可证明未达到目标，Operation 为 `FAILED`；若命令可能已生效但查询失败或观测互相矛盾，Operation 为 `UNKNOWN`。

## 6. SQLite、journal 与崩溃恢复

### 6.1 SQLite 运行参数

Agent 启动时必须独占数据目录实例锁，禁止两个 Agent 进程打开同一主本。每个连接至少执行：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA trusted_schema=OFF;
PRAGMA temp_store=MEMORY;
```

数据库必须有显式整数 `schema_version`。迁移在 `BEGIN IMMEDIATE` 中串行执行；迁移前创建受 ACL 保护的一致性备份。数据库版本高于当前二进制支持范围时拒绝启动，禁止自动降级或忽略未知列。

### 6.2 最低逻辑表

实现可以拆表优化，但必须保留以下逻辑实体及唯一/外键约束：

| 实体 | 最低持久化内容 |
|---|---|
| `schema_meta` | 数据库版本、实例 ID、legacy import 完成标记 |
| `services` | service_id、schema_version、revision、无明文秘密的 canonical config、config hash、artifact hash、最近应用 revision、时间戳 |
| `service_secrets` | service_id、JSON Pointer secret path、cipher version、DPAPI ciphertext、时间戳；`(service_id,path)` 唯一 |
| `operations` | §3.2 全部字段 |
| `idempotency_records` | key、fingerprint、operation_id、执行状态、原 HTTP status/body/Location、过期时间；key 全局唯一 |
| `service_write_reservations` | service_id、operation_id、获得时间；service_id 唯一 |
| `service_quarantines` | service_id、unknown operation_id、创建时间、解除时间 |
| `write_journal` | journal_id、operation_id、service_id、kind、phase、新旧快照引用、临时/正式/备份路径与哈希、时间戳 |
| `operation_acknowledgements` | operation_id、disposition、note、confirmed_at、trace metadata；operation_id 唯一 |
| `legacy_import_reports/items` | report_id/hash、来源快照、映射结果、issues、过期与消费状态 |
| `liveness_profiles`（Phase 4） | service_id、applied revision/hash、profile/tombstone、状态、时间戳；desired 值属于 CP，不在 Agent 伪造 |

`services` 的 canonical config 不保存 `secret` 写入值；秘密位置使用内部引用，API Read 时合成为 `secret_set`。SQLite WAL/SHM、备份和临时文件适用与主数据库相同的 ACL。

### 6.3 配置写 journal

SQLite 与文件系统不能形成单一原子事务，因此 PUT、DELETE 和导入必须使用持久化 write-ahead journal。PUT 固定流程如下：

1. 在同一 SQLite 事务中写入 idempotency record、`PENDING` Operation 和服务 reservation；提交失败则结束且无外部副作用。
2. 校验 ServiceConfig、revision、状态、backend capability 和路径；规范化 config，使用 DPAPI 加密新秘密。
3. 在正式文件的**同一卷、同一受管目录**创建随机临时文件；先设置目标 ACL，再写 XML，flush 并调用平台等价的 `fsync/FlushFileBuffers`。
4. 持久化 `write_journal.phase=PREPARED`，内容包含旧/新 revision、无明文秘密的快照、旧/新 artifact hash、临时/正式/上一版路径；随后把 Operation 置为 `RUNNING`。
5. 以原子 replace 将现有正式文件移为 `.previous`，再把临时文件替换为正式文件；每步后 flush 文件和目录元数据。必须始终保留至少一个可恢复的上一版本。
6. 持久化 `phase=ARTIFACT_REPLACED`。
7. 在一个 SQLite 事务中提交 service revision、canonical config、加密秘密与新 artifact hash，并置 `phase=DB_COMMITTED`。
8. 重新读取并核验正式文件哈希，清理不再需要的临时文件，置 `phase=DONE`、Operation=`SUCCEEDED`，保存同步 HTTP 结果并释放 reservation。

DELETE 使用相同阶段：先把派生产物原子移入隔离备份，再删除 SQLite 主本；只有 DB 提交并确认未安装后才清理隔离文件。禁止先物理删除主本再创建 journal。

配置校验或渲染在 `PREPARED` 前失败时，Operation 为 `REJECTED` 或 `FAILED`，正式文件与 revision 不变。进入 `RUNNING` 后发生任何无法判定的 I/O 错误时不得简单返回失败，必须按恢复规则判定。

### 6.4 启动恢复

Agent 必须在开放监听端口前完成恢复扫描；恢复期间 `/healthz` 即使已监听也只能返回 `status=starting`，其他端点不可用。

| 发现状态 | 恢复规则 |
|---|---|
| `PENDING` 且无 journal/执行启动标记 | 已证明未产生副作用；重建 reservation 后重新调度，或因前置条件已变化置 `REJECTED` |
| `PREPARED` 且 DB 仍为旧 revision、正式文件为旧 hash | 删除临时文件，回滚 journal，Operation=`FAILED` |
| `PREPARED` 且 DB 仍旧、正式文件为新 hash | SQLite 主本仍权威；从 `.previous` 恢复旧文件并核验，Operation=`FAILED` |
| `ARTIFACT_REPLACED` 且 DB 仍旧 | 恢复旧文件；不能验证旧文件时 config=`UNKNOWN`、Operation=`UNKNOWN` |
| `DB_COMMITTED` 且正式文件为新 hash | 完成清理，Operation=`SUCCEEDED` |
| `DB_COMMITTED` 但文件缺失/旧 hash | 从 SQLite 新主本重新渲染；成功则完成，不能证明则 `DRIFTED`、Operation=`UNKNOWN` |
| action `RUNNING` | 查询 OS 即时状态和命令启动记录；能证明目标态则 `SUCCEEDED`，能证明未达到且命令确定结束则 `FAILED`，其余 `UNKNOWN` |
| 文件为新旧 hash 之外的第三种内容 | 视为外部并发修改，标 `DRIFTED`，Operation=`UNKNOWN`，建立 quarantine |

恢复绝不自动重放 `UNKNOWN` Operation。所有恢复决定写入结构化恢复原因和时间，不得覆盖原错误证据。

### 6.5 配置 revision 与已应用 revision

- `revision` 只在 canonical 配置语义变化时递增；no-op、漂移修复、日志读取和生命周期动作不递增。
- Agent 记录进程已知使用的 `applied_revision`。运行中更新只替换下一次启动使用的 XML，`applied_revision` 保持旧值并报告 `RESTART_REQUIRED`。
- start/restart 成功并确认新实例后把 `applied_revision` 更新为当前 revision；stop 或进程退出不改变 `applied_revision`，原为 `RESTART_REQUIRED` 时继续保持。
- 无法证明运行进程对应哪个 revision 时 `ConfigState=UNKNOWN`，不得通过比较进程启动时间猜测。

## 7. 日志游标合同

### 7.1 请求与响应

`GET /api/v1/services/{service_id}/logs` 接受：

- `stream=stdout|stderr|wrapper`；
- 可选 `cursor`，省略表示从当前仍保留内容的最早位置开始；
- `limit_bytes` 默认 65,536，最小 1,024，最大 1,048,576。

响应包含 `data`、`next_cursor`、`eof` 和 `truncated`。`data` 是按该流配置解码后的 UTF-8 文本；非法字节使用 U+FFFD，`next_cursor` 仍按原始字节偏移计算，不能按字符数计算。单次读取不得跨越日志轮转边界。

### 7.2 cursor 不透明性

cursor 必须是 Agent 生成并用实例密钥认证的不透明 base64url token，至少绑定：协议版本、Agent instance ID、service_id、stream、文件 identity、原始 byte offset。客户端禁止构造、修改、解析或跨服务/流复用 cursor。

- 正常 EOF 返回可继续使用的 `next_cursor`；后续追加内容可从该位置读取。
- 文件轮转后，旧 cursor 指向的 identity 仍在保留集时继续读取该文件；已淘汰时返回 `410 LOG_CURSOR_EXPIRED`，`detail.reset_cursor` 给出当前最早可读位置。
- MAC 无效、服务/stream 不匹配返回 `400 INVALID_LOG_CURSOR`，不得返回内部路径或 offset。
- cursor 中不得包含可直接解码的绝对路径、Token 或秘密。

Agent 普通运行日志必须脱敏；日志 API 返回的是受管程序业务日志，可能由业务程序自行写入敏感内容，Agent 不承诺语义级清洗，但必须受认证、CIDR 和 TLS 保护，且不得把返回内容复制到 Agent 自身日志或 Operation。

## 8. 安全与部署合同

### 8.1 启动安全门控

默认监听 `127.0.0.1:9800`。解析后的任一监听地址不是 IPv4/IPv6 loopback（包括 `0.0.0.0`、`::` 和解析到非 loopback 的主机名）时，以下三项缺一必须拒绝启动：

1. 可加载且未过期的 TLS 证书/私钥；仅允许 TLS 1.2 及以上；
2. 非默认、至少 32 随机字节等价熵的 Bearer Token；
3. 至少一个显式来源 CIDR，且不得使用 `0.0.0.0/0` 或 `::/0`。

Agent 直接终止 TLS。Phase 2 不信任 `X-Forwarded-For`、`Forwarded` 等代理头，CIDR 校验使用 TCP peer IP；需要反向代理时必须在后续合同中显式冻结可信代理链。

Bearer Token 使用恒定时间比较。Token 只从环境变量或受限 ACL 文件读取，不接受命令行参数，不写入异常、访问日志或 `/api/v1/agent`。认证失败响应不说明 Token 是否存在。

### 8.2 CORS 与 HTTP 防护

- 默认不发送任何 CORS Header，不允许通配 origin。
- 若部署明确启用浏览器直连，只能配置精确 `https://host[:port]` allowlist；禁止 `*`、`null`、通配子域和明文 HTTP origin，预检不得绕过来源 CIDR。
- 请求体、Header 数量和 URL 长度必须设置上限；超限在解析业务正文前拒绝。
- 访问日志只记录 method、路由模板、状态码、耗时、trace_id 和脱敏后的来源，不记录 Authorization、请求/响应正文、query 中的 cursor 或秘密。

### 8.3 DPAPI 与文件 ACL

Windows Phase 2 使用 DPAPI `CRYPTPROTECT_LOCAL_MACHINE` 逐秘密加密，并带版本化 application entropy。DPAPI 机器作用域不替代 ACL；安全边界是“加密 + 受限目录 ACL”共同成立。

以下对象必须关闭宽泛继承，移除 `Users`、`Authenticated Users`、`Everyone` 的读取权限，仅授予 Agent 服务 SID 所需修改权限以及 `SYSTEM`、本机 Administrators 管理权限：

- SQLite、WAL/SHM、备份、journal 和 import report；
- Token、TLS 私钥、cursor/应用 entropy；
- 受管 XML、`.previous`、临时渲染目录；
- Agent 配置及包含操作诊断的本地日志。

临时文件必须先设 ACL 再写内容。秘密在内存中只保留完成渲染所需时间，错误、普通日志、Operation、配置摘要和请求指纹旁路字段不得包含明文。

WinSW XML 可能必须以明文包含服务账户密码或敏感环境变量。Agent 必须：

- 在能力和写响应 warning 中报告 `plaintext_secret_materialized=true`；
- 对 XML 和上一版使用上述最小 ACL；
- 禁止创建未加密、宽权限的调试副本；
- 明确说明普通文件删除无法保证介质级安全擦除。

### 8.4 最小权限与制品固定

Agent 应使用专用服务账户并只授予管理目标服务所需权限；若部署为 LocalSystem，部署文档必须标为高风险。生产运行时禁止访问 GitHub `latest` 或自动下载 WinSW。

WinSW 可执行文件必须由部署清单固定版本和 SHA-256；Agent 启动时、每次执行前或文件身份变化时校验哈希。不匹配返回 `BACKEND_INTEGRITY_FAILED` 并拒绝所有 backend 写动作。

## 9. WindowsWinSWBackend 重构边界

现有 `core/winsw_manager.py` 和 `core/config_manager.py` 仅作为迁移参考，不是可直接暴露给远程 API 的可靠性边界。Phase 2 必须建立结构化 backend，至少包含：

| 能力 | 强制实现 |
|---|---|
| 命令执行 | argv 数组、`shell=false`、绝对 executable/cwd、无字符串拼接 |
| 结果 | return code、分离 stdout/stderr、started/finished/duration、truncated 标志 |
| 超时 | 每动作固定上限；超时终止整个进程树并再次查询系统状态 |
| 输出限制 | stdout/stderr 各最多 64 KiB，保留头尾并标截断；先脱敏再持久化 |
| 错误 | 类型化为 validation、not-found、permission、timeout、non-zero、parse、integrity、unknown-result |
| 状态解析 | 按已支持 WinSW 版本解析精确输出；未知文本映射 `UNKNOWN`，不得 substring 猜测 |
| 路径 | 只能使用 Agent 配置的受管根目录和规范 `service_id` 计算，不接受用户提供 XML 路径 |
| 并发 | 阻塞子进程放入有界 worker pool；服务写锁仍在 application service 层统一持有 |
| 启动策略 | Windows Automatic ↔ `AUTOSTART_ENABLED`，Manual ↔ `AUTOSTART_DISABLED`，Disabled ↔ `START_BLOCKED` |

backend 接口必须分别提供配置 render/validate、四维 status、七动作和日志解析；`refresh` 不是动作，只是重新执行 status 查询。API 层不得解析 WinSW 文本，也不得根据字符串消息选择 HTTP 状态码。

## 10. 旧 WinSW XML 一次性导入

### 10.1 支持的操作界面

Phase 2 提供 CLI，并复用与 API 相同的解析、校验和提交 application service：

```text
winsw-agent legacy-import preflight --source <absolute-directory> --output <report.json>
winsw-agent legacy-import commit --report-id <uuid> --confirm-hash <sha256>
```

导入仅允许在目标机器本机执行，不暴露 REST import 端点。preflight 与 commit 均要求 Agent 已停止，并成功取得数据目录实例锁；CLI 直接调用与 Agent 相同的 application service、journal 和校验代码，禁止另写一套解析器或绕过服务层任意修改 SQLite。

一个 Agent 数据库只允许一次成功 commit；成功后持久化 `legacy_import_completed=true`。之后 preflight/commit 均返回 `409 IMPORT_ALREADY_COMPLETED`。失败或过期的 preflight 不消耗该机会。

### 10.2 preflight

`legacy-import preflight` 接收本机绝对来源目录，只读扫描 `.xml`，不得跟随离开来源根的 reparse point/symlink。每个 item 记录：规范绝对路径、文件 identity、size、mtime、源 SHA-256、提取 service_id、映射后的脱敏 ServiceConfig、安装/运行观测和 issues。

以下任一项是阻塞错误，报告存在阻塞错误时禁止 commit：

- XML 损坏、DTD/外部实体、未知元素或未知属性；
- `<id>` 非法、与文件名 stem 不一致或报告内/SQLite 中重复；
- executable、working directory、日志路径不满足 ServiceConfig 绝对本机路径规则；
- arguments 存在未闭合引号、不可逆转义，或按 Windows `CommandLineToArgvW` 解析后不能通过“argv → backend 序列化 → argv”往返保持一致；
- 已注册服务不指向该受管 WinSW 实例/目标 XML，无法证明接管安全；
- 账户、恢复、日志或环境字段无法无损映射到 v1；
- 文件读取期间 identity、size、mtime 或 hash 变化。

旧 XML 中账户密码按秘密导入并立即 DPAPI 加密。环境变量没有可靠敏感标记时不得猜测，按非敏感值映射并产生 `IMPORT_ENV_SENSITIVITY_REVIEW_REQUIRED` warning；报告和 CLI 输出仍不得显示账户密码。

`report_hash` 是对排序后的来源文件快照、映射配置和 issues 做 RFC 8785 canonical JSON 后的 SHA-256；排除 report_id、生成时间和展示文案。报告默认 24 小时过期，可配置但最长 7 天。

### 10.3 commit 与接管

`legacy-import commit` 必须同时提交 `report_id` 和操作者逐字确认的 `report_hash`。commit 前重新验证：报告未过期/未消费、无阻塞错误、每个文件 identity 与 SHA-256 未变化、服务状态未进入过渡/UNKNOWN、目标 ID 仍无冲突。任一失败则整个批次不开始。

commit 是逻辑全有或全无：

1. 先生成一个 `IMPORT` batch Operation，为所有 item 加密秘密和生成目标 XML，写入 import batch、每服务 reservation 与 per-service journal；
2. 未发布的 service 行使用 batch 可见性门控，普通列表不得看到半批数据；
3. 全部派生产物替换并校验后，一个 SQLite 事务发布所有 service、消费报告并设置永久完成标记；
4. 崩溃恢复要么完成整个已确认批次，要么恢复所有旧文件并保持未发布；不能恢复时，batch Operation=`UNKNOWN` 并隔离每个受影响服务，API 明确暴露导入未决状态。

导入不执行 install、start、stop 或 restart。已安装但 inactive 且注册路径一致的服务可接管；active 服务保守标记 `RESTART_REQUIRED`，直到一次显式 restart 证明使用当前 revision。commit 成功后，来源目录中的受管 XML 即成为 Agent 派生产物，任何后续外部写入按 `DRIFTED` 处理。

## 11. Phase 4 探针与 liveness profile 冻结合同

### 11.1 Phase 2 capability

Phase 2 的 `GET /api/v1/agent` 必须至少返回：

```json
{
  "capabilities": {
    "probe_once": false,
    "liveness_profile": false,
    "liveness_auto_restart": false
  }
}
```

Phase 2 可以注册 Phase 4 路由桩，但调用时固定返回 `501 CAPABILITY_NOT_AVAILABLE`；不得返回模拟成功、保存后不执行的 profile，或用 `/healthz` 冒充探针能力。

### 11.2 单次 probe

Phase 4 的 `POST /api/v1/probe` 每次只执行一个内联 spec，支持 `tcp/http/process/cmd_template`，角色只允许 `startup/readiness`。请求结束后不驻留 spec、不创建周期任务，也不修改 liveness profile。

- `cmd_template` 只能引用 Agent 预注册模板和结构化参数；禁止 shell 字符串、任意 executable 或调用方指定账户。
- 每次执行限制 timeout、输出大小、工作目录和并发；结果为 `PASSED/FAILED/TIMED_OUT/ERROR`，并返回脱敏、限长诊断。
- 单次 probe 不取得服务写锁，因为它必须只读；需要修改服务的模板不允许注册为 probe。

### 11.3 liveness profile

每个服务最多一个 `liveness` profile，与 ServiceConfig revision 独立。PUT envelope 固定为 `{expected_revision, profile}`；创建时 expected revision 为 `null`。`ProfileWrite` 不包含 revision，Agent 在 `ProfileRead` 返回 `profile_revision` 及 applied hash/status。profile 至少包含 enabled、tombstone、probe、schedule 和 recovery：

| 字段 | v1 约束 |
|---|---|
| `probe.kind` | `tcp/http/process/cmd_template` |
| `schedule.interval_seconds` | 5–3,600，默认 30 |
| `schedule.timeout_seconds` | 1–60 且小于 interval，默认 5 |
| `schedule.failure_threshold` | 1–20，默认 3 |
| `schedule.success_threshold` | 1–20，默认 1 |
| `recovery.mode` | `REPORT_ONLY`（默认）或 `RESTART` |
| `recovery.cooldown_seconds` | RESTART 必填，30–86,400 |
| `recovery.window_seconds` | RESTART 必填，60–86,400 |
| `recovery.max_restarts` | RESTART 必填，1–100，按 window 限流 |

普通 profile 必须 `tombstone=false` 并携带 probe/recovery；禁用使用新 revision 的 `enabled=false`。删除使用 `tombstone=true` 的新 revision，此时禁止携带 probe、schedule 或 recovery，且禁止直接物理删除 applied 记录。CP 保存 desired revision/hash，Agent只回报 applied revision/hash；不一致时 UI 必须显示 pending，不能宣称策略已生效。tombstone applied 后停止调度并清理运行态，仍保留 revision/hash 用于重连对账。

### 11.4 默认 report-only 与自动恢复

`REPORT_ONLY` 只产生本地健康事件和状态，不触发生命周期动作。`RESTART` 必须由操作者显式配置全部限流字段，且遵循：

1. failure threshold 达到后先检查 cooldown 和滚动 window 次数；超过上限只报告，不 restart。
2. 自动 restart 创建正常 `LIVENESS_RECOVERY` Operation，使用同一服务写锁、相同动作矩阵和 quarantine 规则。
3. 锁忙时不抢占人工/编排动作，记录 `RECOVERY_SKIPPED_ACTION_IN_PROGRESS`，下一调度周期重新评估。
4. config 为 `INVALID/DRIFTED/UNKNOWN`、服务未安装、runtime 非 `ACTIVE` 或任一状态不一致时禁止自动 restart。
5. `UNKNOWN` 自动恢复 Operation 立即隔离服务，禁止下一次周期重放。
6. 自动 restart 只处理本服务，不自动停止或重启下游；跨服务影响由 Control Plane 编排合同处理。
7. Control Plane 离线不停止已经 applied 的本地调度；重连后以 CP desired revision/tombstone 对账。

## 12. 实现验收场景

| 类别 | 必须通过的场景 |
|---|---|
| 配置 | 创建 revision=1；变化更新 +1；no-op 不增；运行中更新为 RESTART_REQUIRED；同 revision 修复 DRIFTED |
| 秘密 | 保留/替换/清除三态；API、错误、Operation、Agent 日志和 SQLite canonical config 均不出现明文 |
| 幂等 | 同 key 同指纹复用结果；同 key 异指纹 409；断线重试不重复副作用；保留期内跨重启一致 |
| 并发 | 同服务所有写互斥，不同服务并行；锁冲突产生 REJECTED Operation；只读返回 transitional |
| 生命周期 | 七动作逐项覆盖允许态、目标态 no-op、过渡态、UNKNOWN、矛盾状态和禁止的隐式动作 |
| 崩溃 | 分别在 PREPARED、文件替换后、DB commit 后、WinSW 命令运行中强制退出，恢复结果符合 §6.4 |
| UNKNOWN | 不自动重放；隔离后写请求被拒；ack 解除隔离但原状态保持 UNKNOWN |
| 日志 | EOF 后增量、UTF-8 非法字节、轮转保留、淘汰 410、篡改 cursor、跨服务复用 |
| 安全 | 非 loopback 缺 TLS/Token/CIDR 任一项均启动失败；Token/CIDR/CORS/ACL/DPAPI/WinSW hash 检查生效 |
| 导入 | 合法批次；未知 XML；损坏 XML；参数歧义；重复/非法 ID；源文件变更；active 服务；崩溃恢复；二次导入拒绝 |
| Phase 4 边界 | Phase 2 capabilities 全 false 且路由返回 501；profile 默认 REPORT_ONLY；显式 RESTART 受锁、冷却和次数上限约束 |

只有 OpenAPI 示例、ServiceConfig 字段、本文枚举/路径/错误码与追踪矩阵完全一致，并通过以上自动化验收后，Phase 2 才满足“设计冻结，待实现”的编码门槛。
