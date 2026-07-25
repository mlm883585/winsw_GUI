# 分布式服务管理平台 — 实施基线

> 状态：**Phase 2 合同冻结候选**。本文定义 Phase 2–5 共同遵守的产品边界，以及 Phase 2 开工前必须满足的约束。完成本文 §9 的全部检查后，才可改为“Phase 2 设计冻结，待实现”。Phase 3–5 仍需在各自开工前复审，不能因 Phase 2 冻结而视为已冻结。

## 1. 规范文档与权威范围

| 领域 | 权威文档 | 约束 |
|------|----------|------|
| 产品边界、跨阶段最低门槛 | 本文 | 阶段文档不得降低本文要求 |
| 架构职责、数据归属 | [architecture.md](./architecture.md) | 只描述稳定边界，不复制线上 Schema |
| `ServiceConfig v1` 字段 | [service-config-v1.md](./contracts/service-config-v1.md) | 字段、默认值、校验和秘密语义以此为准 |
| Agent 行为、状态机、持久化 | [agent-protocol-v1.md](./contracts/agent-protocol-v1.md) | 动作、幂等、锁和恢复以此为准 |
| HTTP 线上格式 | [agent-openapi.yaml](./api/agent-openapi.yaml) | 路径、请求、响应和错误结构以此为准 |
| Phase 2 验收追踪 | [phase-2-traceability.md](./contracts/phase-2-traceability.md) | 每项 MUST 必须落到接口和测试场景 |

不同文档按领域分别权威，不采用“高层文档覆盖低层文档”的方式掩盖冲突。发现同一语义在两处不一致时，Phase 2 立即退出可实现状态，先修正文档再编码。

## 2. 产品边界与首个交付场景

### 2.1 核心用户与场景

首个生产场景面向**在受控内网或 VPN 中管理少量 Windows/Linux 服务器的开发运维或交付团队**：使用一个自托管 Control Plane 管理各机器上的 Agent，对已有程序创建系统服务、查看状态和日志，并按显式依赖关系执行启停编排。

Phase 2 只交付 Windows Agent：通过本机或远程 HTTPS API 管理本机 WinSW 服务。Control Plane、Web 前端、依赖编排和 Linux backend 均不属于 Phase 2 交付面，但 Phase 2 合同必须为其保留兼容边界。

### 2.2 设计规模

| 维度 | 首版设计目标 | 超出后的处理 |
|------|--------------|--------------|
| Control Plane | 单实例、自托管 | 高可用留 Phase 5 |
| 机器数 | 典型 2–20，设计上限 100 | 超限前压测并迁 PostgreSQL |
| 服务数 | 典型 20–200，设计上限 2,000 | 调整轮询分片与状态推送 |
| 网络 | 受控内网/VPN，不直接暴露公网 | 公网场景必须先启用 mTLS 与额外边界防护 |
| 操作者 | Phase 3 单管理员；Phase 5 多用户 RBAC | Phase 3 上线即记录基础操作日志 |

以上是容量假设，不是性能承诺。每阶段验收必须记录实际测试规模和结果，未测试前不得宣称达到设计上限。

### 2.3 明确非目标

- 不做应用安装包、业务二进制分发、配置中心、密钥库或制品仓库。
- 不做进程调度、资源装箱、自动扩缩容或容器编排。
- 不自动发现任意系统服务；只管理 Agent 明确登记的服务。
- 不保证网络分区下的跨机强一致；优先避免重复执行高风险动作并暴露不确定结果。
- CP 宕机不得影响已运行的业务服务；未完成编排只能恢复或人工处置，不能静默继续。
- Agent 不接受任意 shell 探针、任意文件读取、远程上传可执行文件或远程下载“最新 WinSW”。

## 3. 数据权威与兼容边界

### 3.1 Phase 2 权威存储

- Agent 的 SQLite `agent.db` 是 `ServiceConfig`、revision、秘密元数据、Operation 和写入 journal 的唯一权威主本。
- WinSW XML 是由 Agent 渲染的派生产物，不是公共 API，也不是可并行编辑的配置主本。
- SQLite 使用 WAL、`synchronous=FULL`、显式 Schema 版本和顺序迁移；秘密值在 Windows 上使用机器绑定 DPAPI 加密，文件同时应用最小 ACL。
- WinSW 最终 XML 若因平台要求必须含明文账户密码，UI、部署文档和 API 必须明确告警，并以最小 ACL 限制读取；不得宣称该文件已加密。

### 3.2 旧 GUI 与 XML 迁移

- 现有 Tkinter GUI 可继续作为独立的旧版本机工具，但不得直接编辑 Agent 已接管的服务目录。
- 旧 XML 只能通过“预检报告 → 报告哈希确认 → 提交”的显式导入流程进入 Agent 主本。
- 未知标签、损坏 XML、非法或重复 ID、无法无损拆分的参数以及秘密字段歧义均为阻断项；Agent 不做有损导入。
- 接管后发现 XML 被外部修改，配置状态进入 `DRIFTED`，阻止 install/start；Agent不自动吸收外部修改，也不静默覆盖。操作者必须用当前 revision 重新 PUT 权威配置，显式恢复派生文件。

## 4. `ServiceConfig v1` 公共契约

Agent API 只接受和返回 `application/json`。WinSW XML 与 systemd unit 均是 backend 内部格式。

### 4.1 模型分离

| 模型 | 用途 | 关键约束 |
|------|------|----------|
| `ServiceConfigWrite` | 创建或替换配置 | 可包含秘密写操作，不含服务端 revision |
| `ServiceConfigRead` | 获取完整配置 | 秘密只返回 `secret_set`，绝不回显原值 |
| `ServiceSummary` | 列表与聚合 | 不包含秘密、完整参数或可还原秘密的摘要 |
| `PutServiceRequest` | PUT 请求封装 | 固定为 `{expected_revision, config}` |

- `expected_revision: null` 仅表示创建；目标已存在时返回 `409 SERVICE_ALREADY_EXISTS`。
- `expected_revision` 为正整数时表示条件更新；目标不存在返回 404，不匹配返回 `409 REVISION_CONFLICT`。
- 语义相同的 PUT 返回 `changed:false` 且不提升 revision；若派生 XML 漂移，可在 revision 不变的前提下返回 `render_repaired:true`。
- URL `{id}` 必须等于 body `service_id`；创建后不允许原地改名。
- 所有层级拒绝未知字段；`platform_overrides` 不进入 v1。
- 参数只能是字符串数组，backend 负责确定性平台转义；前端不得拼接原始命令行。
- 完整字段、默认值、长度、路径、账户模式、敏感值三态和跨字段校验见 [ServiceConfig v1](./contracts/service-config-v1.md)。

## 5. 四维状态与生命周期

服务状态必须同时返回以下四个互相独立的维度：

| 维度 | 固定枚举 | 含义 |
|------|----------|------|
| `ConfigState` | `CURRENT / RESTART_REQUIRED / INVALID / DRIFTED / UNKNOWN` | 权威配置、派生文件与当前进程是否一致 |
| `InstallationState` | `INSTALLED / NOT_INSTALLED / UNKNOWN` | 是否已注册为系统服务 |
| `RuntimeState` | `ACTIVE / INACTIVE / STARTING / STOPPING / FAILED / UNKNOWN` | 当前进程生命周期状态 |
| `StartupState` | `AUTOSTART_ENABLED / AUTOSTART_DISABLED / START_BLOCKED / NOT_APPLICABLE / UNKNOWN` | 是否随系统启动及是否被平台阻止启动 |

明显矛盾的组合（例如 `NOT_INSTALLED + ACTIVE`）必须返回 `INCONSISTENT_SERVICE_STATE`，不得自行选择某一维为真。

### 5.1 配置、注册、运行和开机策略分离

| 操作 | 固定语义 | 明确禁止 |
|------|----------|----------|
| PUT | 只保存配置和渲染派生文件 | 不 install、不 restart |
| install | 注册系统服务，默认手动启动 | 不 start、不启用开机自启 |
| uninstall | 在服务已停止时注销系统服务，保留 Agent 配置 | 不隐式 stop、不 delete |
| start | 启动已安装服务 | 不隐式 install |
| stop | 停止已安装服务 | 不 uninstall |
| restart | 重启已安装且处于 `ACTIVE` 或 `FAILED` 的服务 | 不接受未运行、未安装或过渡态 |
| enable-autostart | 仅启用开机启动 | 不 start；Linux 不使用 `--now` |
| disable-autostart | 仅关闭开机启动，仍允许手动启动 | 不 stop；Linux 不 mask |
| DELETE | 仅删除 `NOT_INSTALLED` 的 Agent 配置及其受管派生物 | 不删除应用、工作目录、业务日志，不隐式 uninstall |

重复达到目标状态的动作返回幂等成功。处于 `STARTING/STOPPING/UNKNOWN`，或存在未确认 `UNKNOWN` Operation 时，不得盲发冲突动作。完整前置条件矩阵见 [Agent Protocol v1](./contracts/agent-protocol-v1.md)。

## 6. HTTP、Operation 与并发基线

- 所有写请求必须携带 `Idempotency-Key` Header，值为 UUIDv4；该值同时是全 Agent 唯一的 `operation_id`。
- PUT/DELETE 在原子配置事务完成后同步返回，同时持久化 Operation 记录。
- 七个生命周期动作返回 `202 Accepted`、Operation 表示和 `Location` 查询地址。
- Operation 固定状态为 `PENDING / RUNNING / SUCCEEDED / FAILED / REJECTED / UNKNOWN`。
- Agent 必须先持久化 operation_id、请求指纹和 `PENDING`，再执行任何副作用。
- 同一 service_id 的 PUT、DELETE 与全部动作共用一把写锁；不同服务可并发。锁冲突立即返回 `409 ACTION_IN_PROGRESS`，不在服务端排队。
- 同 ID 同指纹返回既有结果；同 ID 不同指纹返回 `409 OPERATION_ID_REUSED`。
- Operation 默认保留 30 天，配置下限为 7 天，且不得短于最长编排恢复窗口。
- `UNKNOWN` 不得自动重放。人工确认只解除该服务的操作隔离并记录证据，不得把未知结果伪造为成功。

## 7. 写入、恢复与对账

### 7.1 配置写入事务

1. 校验 Schema、revision、路径、秘密操作和 backend capability。
2. 在临时位置渲染并验证目标 XML。
3. 在 SQLite 写入 `PREPARED` journal 和 Operation。
4. 原子替换派生 XML并保留至少一份已接受的上一版本。
5. 提交新主本、revision、hash 和配置状态。
6. 完成 Operation；任一步失败都必须保留可证明的旧状态。

Agent 启动时扫描未完成 journal 和 Operation：能证明完成则提交，能证明未执行则回滚或确定失败，无法证明副作用结果则进入 `UNKNOWN` 并隔离该服务的冲突动作。

### 7.2 Phase 3 起的 CP 对账

| 场景 | CP 行为 |
|------|---------|
| Agent 出现新服务 | 创建镜像，记录 `config_hash/revision/last_seen_at` |
| Agent 配置变化 | 更新镜像摘要；旧 revision 提交时拒绝覆盖 |
| 一次轮询未见服务 | 标记 `MISSING`，不立即物理删除 |
| 连续超过阈值仍未见 | 标记 `DELETED_EXTERNALLY`，保留引用并要求人工处置 |
| Agent 离线 | 四维状态转 `UNKNOWN`；禁止配置写入，不缓存待下发主本 |
| service_id 改名 | 视为旧服务消失和新服务出现，不自动迁移依赖 |
| 删除机器 | 存在服务、依赖或运行历史时禁止硬删除，只允许停用或归档 |

## 8. 最低安全基线

- Agent 默认绑定 `127.0.0.1:9800`。非环回监听必须同时配置 TLS 证书/私钥、非空 CIDR allowlist 和高熵 Token；缺一项即拒绝启动。
- 仅本机开发模式可显式关闭 TLS，并必须输出醒目警告。生产客户端不得使用 `verify=false`。
- Token 至少包含 32 字节随机熵；Token、TLS 私钥、DPAPI 主本、备份和派生 XML应用最小 ACL。
- `/healthz` 免鉴权时只返回最小存活信息；版本、OS、backend 和 capabilities 只在鉴权后的 `/api/v1/agent` 返回。
- Agent 不直接服务浏览器，默认禁用 CORS。
- Authorization、秘密值、完整探针输出、未经处理的 stderr 和 traceback 不得写入普通日志或错误响应。
- JSON 请求和日志读取必须设置大小上限；详细值由 OpenAPI 固定。
- 生产环境禁止 Agent 自动下载 latest WinSW；必须使用固定版本及 SHA-256 校验。
- `cmd_template` 探针只允许 Agent 预注册模板和结构化参数，并限制超时、输出长度、工作目录和执行账户。
- LocalSystem 必须显式选择并在部署文档标记为高风险；默认部署指南必须给出最小权限方案。

## 9. 进入实现的冻结门槛

### 9.1 Phase 2 必须全部通过

| 检查 | 通过标准 |
|------|----------|
| 字段合同 | ServiceConfig 字段字典无待定项，秘密、默认值、长度和跨字段校验完整 |
| OpenAPI | OpenAPI 3.1 语法/语义通过，所有示例符合 Schema |
| 行为合同 | 动作矩阵、Operation、锁、崩溃恢复、日志 cursor 和漂移处理无二义性 |
| 安全部署 | TLS、Token、CIDR、DPAPI、ACL、WinSW 供应链和日志脱敏均有可执行要求 |
| 迁移 | 合法、未知字段、损坏、参数歧义、重复 ID 和接管后漂移均有验收场景 |
| 一致性 | API 路径、枚举、错误码、revision、operation_id 在全部文档中零漂移 |
| 追踪矩阵 | 每个 MUST 至少映射到一个接口/字段和一个验收场景 |
| Reader Test | 独立读者仅凭文档能正确回答关键实现问题，且无新的阻塞歧义 |

规范章节中不得残留未决的“待定”“二选一”“后续再评估”。尚未交付的 Phase 4 接口可以标注 delivery phase 与 capability=false，但其线上合同必须固定。

### 9.2 后续阶段开工前必须冻结

| 阶段 | 开工前必须冻结 |
|------|----------------|
| Phase 3 | CP 对账阈值、软删除/引用保护、模板版本模型、登录会话、操作日志字段 |
| Phase 4 | 编排状态机、租约、探针执行、失败传播、取消/恢复/重跑/回滚语义 |
| Phase 5 | Linux 能力矩阵、mTLS 证书生命周期、RBAC 权限矩阵、升级兼容矩阵 |
