# Phase 2 — Windows Agent（合同冻结候选）

> 目标：把现有本机 WinSW 能力重构为一个可独立部署、可恢复、可审计的 HTTPS Agent。Phase 2 只交付 Windows/WinSW，不交付 Control Plane、Web 前端、编排器和探针执行器。
>
> 开工门槛：必须同时冻结 [实施基线](../implementation-baseline.md)、[ServiceConfig v1](../contracts/service-config-v1.md)、[Agent Protocol v1](../contracts/agent-protocol-v1.md)、[Agent OpenAPI](../api/agent-openapi.yaml) 和 [追踪矩阵](../contracts/phase-2-traceability.md)。

## 1. 交付目标

- Agent 以 Windows 服务长期运行，通过 FastAPI/HTTP JSON 管理本机受管服务。
- SQLite 是配置、revision、Operation、秘密元数据和写入 journal 的唯一权威主本。
- WinSW XML 是 Agent 渲染的派生文件；Agent 接管后禁止 GUI 或人工并行修改。
- 本机与远程调用使用同一 API；非环回访问必须启用 Agent 直终止 TLS、Bearer Token 和 CIDR allowlist。
- 配置、系统注册、运行状态和开机自启分别建模，不提供隐式组合动作。
- 所有写入可幂等去重；进程崩溃或网络中断后必须能确定恢复，不能确定时显式进入 `UNKNOWN`。

## 2. 范围

| In（Phase 2 交付） | Out（后续阶段） |
|--------------------|-----------------|
| 独立 Agent 进程与 Windows 服务部署 | Control Plane 与 Web 前端（Phase 3） |
| HTTPS、Token、来源限制、最小健康端点 | mTLS、Token 轮转、RBAC（Phase 5） |
| ServiceConfig CRUD、四维状态、七个生命周期动作 | 跨机依赖编排（Phase 4） |
| Operation、幂等、每服务写锁、崩溃恢复 | 编排租约与 run 状态机（Phase 4） |
| SQLite 主本、DPAPI、原子 XML 渲染与漂移检测 | Linux/systemd backend（Phase 5） |
| 旧 XML 预检与一次性导入 CLI | 长期双向同步旧 GUI/XML |
| 固定未来 probe/liveness 线上合同并报告 capability=false | 探针执行与 liveness 调度（Phase 4） |

## 3. 架构与职责

```text
agent/
├── main.py                    # 启动、配置校验、恢复扫描、FastAPI
├── api/                       # OpenAPI 路由、鉴权、请求限制、错误映射
├── application/               # 服务锁、Operation、配置事务、动作调度
├── domain/                    # ServiceConfig、四维状态、错误与动作枚举
├── persistence/               # SQLite repository、迁移、journal、DPAPI protector
├── backends/
│   ├── base.py                # PlatformBackend 协议
│   └── windows_winsw.py       # WinSW 渲染、注册、动作、状态、日志
├── execution/command_runner.py# 结构化子进程、超时、输出限长与脱敏
├── migration/winsw_import.py  # 旧 XML 预检/提交 CLI
└── security/                  # TLS、Token、CIDR、ACL
```

### 3.1 分层边界

| 层 | 负责 | 不负责 |
|----|------|--------|
| API | HTTP、鉴权、Schema、request_id、错误体 | WinSW 命令、SQLite 事务 |
| Application | 写锁、Operation、幂等、配置事务、恢复 | 平台命令细节 |
| Persistence | SQLite、revision、journal、DPAPI 密文 | HTTP、服务动作 |
| PlatformBackend | 验证/渲染平台配置、注册、状态、日志 | Token、Operation、CP revision |
| CommandRunner | 子进程退出码、stdout/stderr、超时 | 业务状态解释 |

Phase 4 的 `ProbeExecutor` 和 `LivenessSupervisor` 是独立扩展，不塞入 `PlatformBackend.upsert()` 或 WinSWManager。

### 3.2 PlatformBackend 最小能力

```python
class PlatformBackend(Protocol):
    def capabilities(self) -> BackendCapabilities: ...
    def validate(self, config: ServiceConfig) -> ValidationResult: ...
    def render(self, config: ServiceConfig, destination: Path) -> RenderResult: ...
    def inspect(self, service_id: str) -> ServiceStatus: ...
    def execute(self, service_id: str, action: ServiceAction) -> CommandResult: ...
    def read_logs(self, service_id: str, stream: LogStream, cursor: str | None,
                  limit_bytes: int) -> LogChunk: ...
```

配置 CRUD、revision、秘密更新、Operation 和锁均属于 application/persistence 层，不由 backend 自行实现。

## 4. 公共合同摘要

### 4.1 HTTP 端点

| 类别 | 端点 | Phase 2 行为 |
|------|------|--------------|
| 存活 | `GET /healthz` | 免鉴权，仅返回最小存活信息 |
| Agent 信息 | `GET /api/v1/agent` | 鉴权，返回版本、平台、Schema、capabilities |
| 服务 | `/api/v1/services...` | 列表、详情、PUT、DELETE、状态和增量日志 |
| 动作 | `POST /api/v1/services/{id}/actions/{action}` | 返回 `202 + Operation + Location` |
| Operation | `GET /api/v1/operations/{operation_id}` | 查询确定结果或 `UNKNOWN` |
| 未知确认 | `POST /api/v1/operations/{operation_id}/acknowledge-unknown` | 记录人工确认并解除隔离，不改写原结果 |
| 单次探针 | `POST /api/v1/probe` | Phase 4 合同；Phase 2 capability=false |
| liveness | `GET/PUT /api/v1/services/{id}/liveness-profile` | Phase 4 合同；Phase 2 capability=false |

精确 Header、query、Schema、状态码和示例只以 [OpenAPI](../api/agent-openapi.yaml) 为准。

### 4.2 同步与异步边界

- PUT/DELETE 在 SQLite 与派生文件事务确定完成后同步返回，并同时保存 Operation。
- install、uninstall、start、stop、restart、enable-autostart、disable-autostart 均异步返回 202。
- 所有写请求必须携带 `Idempotency-Key: <UUIDv4>`；没有该 Header 的请求在进入业务层前拒绝。
- API 不提供 `refresh` 动作；刷新等价于重新 GET 即时状态。

## 5. 配置持久化与原子性

### 5.1 SQLite

- 数据库固定为 Agent 数据目录下的 `agent.db`，启用 WAL、`synchronous=FULL` 和 foreign keys。
- Schema 必须包含：服务主本、秘密密文、Operation、配置 apply journal、Schema migration 版本。
- 迁移只能顺序前进；启动时发现数据库版本高于当前 Agent 支持范围必须拒绝启动，不能降级读取。
- Operation 默认保留 30 天；清理不得删除 `UNKNOWN`、未确认记录或未来 liveness tombstone。

### 5.2 配置事务

固定流程为：

1. 取得服务写锁并持久化 Operation。
2. 校验 `expected_revision`、Schema、路径、秘密操作和 backend capability。
3. 在同卷临时文件中渲染、解析回读并验证 XML。
4. SQLite 写入 `PREPARED` journal。
5. 原子替换正式 XML，保留上一已接受版本。
6. 提交规范配置、DPAPI 密文、revision、HMAC config_hash 和状态。
7. journal/Operation 完成后释放锁。

相同配置不提升 revision。配置语义相同但 XML 漂移时，PUT 只修复派生物并返回 `render_repaired:true`。

### 5.3 启动恢复

- 启动必须在开放写 API 前扫描 journal 和非终态 Operation。
- 能证明 XML 已替换且主本可提交时完成提交；能证明副作用未发生时回滚或确定失败。
- 生命周期命令已经发出但无法证明结果时，Operation 进入 `UNKNOWN`；该服务进入写隔离。
- 人工确认接口只保存确认人/时间/说明/即时状态快照并解除隔离，原 Operation 永久保留 `UNKNOWN`。

## 6. 生命周期与状态

四维枚举和完整动作矩阵以 [Agent Protocol v1](../contracts/agent-protocol-v1.md) 为准。Phase 2 必须遵守：

- install 只注册、默认手动启动；start 不自动 install。
- uninstall 只接受已停止服务，保留 SQLite 配置；delete 不自动 uninstall。
- disable-autostart 映射为 Windows Manual，不映射为 Disabled；`START_BLOCKED` 只表示外部阻止状态。
- 重复达到目标状态是幂等成功；过渡态和 `UNKNOWN` 不盲发冲突动作。
- PUT 更新运行中服务不自动 restart；需要重启时返回 `RESTART_REQUIRED`。
- `DRIFTED/INVALID` 阻止 install/start，但 stop/uninstall 仍可用于安全退出。

## 7. 旧 XML 导入与 Agent 接管

导入以本机 CLI 执行，不开放任意路径的远程导入 API：

```text
agent import-winsw preview --source <legacy-services-dir> --report <report.json>
agent import-winsw commit  --report <report.json> --report-hash <sha256>
```

- preview 只读扫描，不写数据库或目标目录；报告列出每个字段的映射、警告和阻断项。
- commit 只接受由同版本 Agent 生成且内容哈希匹配、没有阻断项的报告。
- 导入不能覆盖已存在 ID，不能自动改名，不能丢弃未知标签或猜测参数数组。
- commit 成功后 SQLite 成为主本；旧目录不再由 Agent 读取，旧 GUI 不得指向 Agent 受管目录。

## 8. WinSW 必要重构

现有 `core/winsw_manager.py` 与 `core/config_manager.py` 只能作为行为参考和迁移解析器，不能直接包装为生产 backend：

| 现状 | Phase 2 必须改为 |
|------|-----------------|
| 忽略 return code、异常转字符串 | 结构化 `exit_code/stdout/stderr/duration/timed_out` 与类型化错误 |
| stdout/stderr 合并且不限长 | 分流、限长、脱敏并保留诊断关联 ID |
| 相对 `services/` 与 `sys.argv[0]` | 启动时解析并注入绝对数据/服务/WinSW 路径 |
| 无命令超时 | 各动作固定超时；无法确定结果时进入 `UNKNOWN` |
| 启动时下载 GitHub latest | 生产仅接受固定版本、显式路径和 SHA-256 校验 |
| XML 解析失败返回默认配置 | 返回 `INVALID_CONFIG`，绝不伪造新服务 |
| 未知 XML 标签保存时丢失 | 导入预检直接阻断，不宣称无损 |
| GUI 动作前隐式保存 | Agent 动作只针对已提交 revision，绝不隐式 PUT |

## 9. 安全部署

### 9.1 启动模式

| 模式 | 允许条件 |
|------|----------|
| 本机开发 | 仅绑定 loopback；可显式关闭 TLS并输出警告 |
| 内网生产 | Agent 直终止 TLS + Token + 非空 CIDR allowlist；任一缺失即拒绝启动 |

- Token 至少 32 字节随机熵；TLS 私钥、Token 文件、数据库、备份和 XML 应用最小 ACL。
- SQLite 秘密字段使用机器绑定 DPAPI；API 只返回 `secret_set`。
- Agent 不启用 CORS，不返回 traceback，不记录 Authorization 或秘密值。
- JSON 请求上限和单次日志读取上限由 OpenAPI 固定。
- Agent Windows 服务账户默认按最小权限部署；选择 LocalSystem 必须显式确认风险。

## 10. Phase 4 兼容预留

- 鉴权后的 Agent 信息返回 `probe_execute:false`、`liveness_profiles:false`。
- `/probe` 是一次性执行接口，不保存 spec、不负责重试调度。
- liveness profile 使用与 ServiceConfig 独立的单调 revision/hash 和 tombstone；CP 保存 desired，Agent 保存 applied。
- Phase 2 只冻结 Schema 和错误语义，不实现路由行为、调度器或自动恢复。
- Phase 4 启用后默认 `REPORT_ONLY`；显式 `RESTART` 必须配置冷却时间、窗口和最大次数，并与全部手工动作共用服务锁。

## 11. 验收判据

| 类别 | Phase 2 必须证明 |
|------|------------------|
| 配置 | 创建、条件更新、no-op、revision 冲突、秘密三态、运行中更新、漂移修复 |
| 生命周期 | 注册/运行/开机策略分离；重复动作幂等；非法和过渡状态被拒绝 |
| Operation | 同 ID 同/异指纹、并发锁、202 查询、跨重启、UNKNOWN 确认 |
| 原子性 | XML 替换前后崩溃均能提交、回滚或明确 UNKNOWN，不产生静默半写入 |
| 日志 | cursor 支持追加、轮转、截断和读取上限，不暴露任意文件路径 |
| 迁移 | 合法导入成功；未知标签、损坏 XML、歧义参数、非法/重复 ID 阻断 |
| 安全 | 未授权、错误 Token、来源拒绝、非环回缺 TLS 拒绝启动、DPAPI/ACL、日志脱敏 |
| 供应链 | 固定 WinSW 版本和错误 hash 均可验证；生产路径不调用 latest 下载 |

验证场景与需求映射见 [Phase 2 追踪矩阵](../contracts/phase-2-traceability.md)。全部文档和 OpenAPI 通过一致性审查前，不得开始 Agent 代码实现。
