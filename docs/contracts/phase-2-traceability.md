# Phase 2 需求—接口—验收追踪矩阵

> 状态：**冻结候选**。本矩阵把 Phase 2 的规范性产品约束映射到公开合同和可执行验收场景。任何 MUST 无接口/字段或无验收场景，均阻止 Phase 2 进入编码。

## 1. 使用约定

- 字段合同：[ServiceConfig v1](./service-config-v1.md)
- 行为合同：[Agent Protocol v1](./agent-protocol-v1.md)
- 线上合同：[Agent OpenAPI](../api/agent-openapi.yaml)
- 场景编号用于未来测试命名；本阶段只冻结输入、预期输出和错误码，不创建代码测试。

## 2. 配置与持久化

| ID | MUST 约束 | 合同落点 | 失败码/状态 | 验收场景 |
|----|-----------|----------|-------------|----------|
| P2-CFG-001 | SQLite 是规范配置、revision、Operation 和 journal 的唯一主本；XML 只是派生物 | Protocol「权威存储」 | `CONFIG_DRIFT` | T-CFG-01：修改 XML 不改变 GET 主本，状态变 `DRIFTED` |
| P2-CFG-002 | 创建使用 `expected_revision=null`，成功 revision=1 | `PUT /api/v1/services/{id}` | `SERVICE_ALREADY_EXISTS` | T-CFG-02：首次创建成功；重复创建被拒绝 |
| P2-CFG-003 | 更新必须携带当前正整数 revision | PUT Schema | `REVISION_CONFLICT` | T-CFG-03：旧 revision 不能覆盖新配置 |
| P2-CFG-004 | 语义相同的 PUT 不提升 revision | PutServiceResponse | — | T-CFG-04：相同请求返回 `changed=false` |
| P2-CFG-005 | 同 revision PUT 可显式修复漂移派生文件 | ServiceConfig「漂移修复」 | `CONFIG_DRIFT` | T-CFG-05：修复后 `render_repaired=true`、revision 不变 |
| P2-CFG-006 | PUT 不 install、不 restart | Protocol「动作边界」 | — | T-CFG-06：运行中更新返回 `RESTART_REQUIRED`，进程 PID 不变 |
| P2-CFG-007 | DELETE 只允许未安装服务并校验 revision | DELETE 服务端点 | `SERVICE_STILL_INSTALLED` / `REVISION_CONFLICT` | T-CFG-07：已安装删除被拒；未安装只删除受管配置 |
| P2-CFG-008 | DELETE 不删除应用、工作目录或业务日志 | Protocol「删除边界」 | — | T-CFG-08：删除后业务文件保持不变 |
| P2-CFG-009 | 所有层级拒绝未知字段 | ServiceConfig 全部 Schema `additionalProperties=false` | `INVALID_REQUEST` | T-CFG-09：顶层/嵌套未知字段均 400/422，不静默丢弃 |
| P2-CFG-010 | 参数是字符串数组，backend 做确定性 Windows 转义 | ServiceConfig `runtime.arguments` | `BACKEND_VALIDATION_FAILED` | T-CFG-10：空格、引号、尾反斜杠往返一致 |
| P2-CFG-011 | 路径必须为允许的本机绝对路径 | ServiceConfig 路径规则 | `BACKEND_VALIDATION_FAILED` | T-CFG-11：相对、`..`、设备路径和默认 UNC 被拒绝 |
| P2-CFG-012 | config_hash 不泄露秘密且秘密变化时必须变化 | Protocol「摘要」 | — | T-CFG-12：相同主本 hash 稳定；替换秘密后 hash 改变 |
| P2-CFG-013 | 配置事务使用 PREPARED journal 和同卷原子替换 | Protocol「写入事务」 | `OPERATION_RESULT_UNKNOWN` | T-CFG-13：在 XML 替换前/后注入崩溃，恢复结果确定或 UNKNOWN |

## 3. 敏感字段

| ID | MUST 约束 | 合同落点 | 失败码/状态 | 验收场景 |
|----|-----------|----------|-------------|----------|
| P2-SEC-001 | 写模型支持秘密保留、替换、清除三态 | ServiceConfig `SecretWrite` | `INVALID_REQUEST` | T-SEC-01：三态分别得到预期结果，互斥字段被拒绝 |
| P2-SEC-002 | 读模型只返回 `secret_set` | ServiceConfig `SecretRead` | — | T-SEC-02：详情、列表和错误体均不含原秘密 |
| P2-SEC-003 | SQLite 秘密使用机器绑定 DPAPI，文件同时受 ACL 保护 | Protocol「本地秘密」 | `SECRET_PROTECTION_FAILED` | T-SEC-03：数据库不出现明文；错误 DPAPI 上下文不能解密 |
| P2-SEC-004 | 派生 XML 不可避免的明文必须最小 ACL并告警 | Protocol「派生文件」 | — | T-SEC-04：未授权用户不能读取，部署检查展示风险 |
| P2-SEC-005 | Token、秘密、完整 stderr/探针输出不得进入普通日志 | Protocol「日志脱敏」 | — | T-SEC-05：构造含秘密失败请求，访问日志与应用日志无泄露 |

## 4. 四维状态与生命周期

| ID | MUST 约束 | 合同落点 | 失败码/状态 | 验收场景 |
|----|-----------|----------|-------------|----------|
| P2-LIF-001 | 状态响应同时包含 Config/Installation/Runtime/Startup 四维枚举 | `ServiceStatus` Schema | `INCONSISTENT_SERVICE_STATE` | T-LIF-01：覆盖每一枚举并拒绝矛盾组合 |
| P2-LIF-002 | install 只注册，默认不开机自启且不启动 | install action | `ACTION_NOT_ALLOWED` | T-LIF-02：install 后 `INSTALLED/INACTIVE/AUTOSTART_DISABLED` |
| P2-LIF-003 | uninstall 要求服务已停止并保留配置 | uninstall action | `SERVICE_RUNNING` | T-LIF-03：ACTIVE 时拒绝；停止后注销且 GET 配置仍存在 |
| P2-LIF-004 | start 不自动 install；ACTIVE 时幂等成功 | start action | `SERVICE_NOT_INSTALLED` | T-LIF-04：未安装拒绝；重复 start 不产生第二次副作用 |
| P2-LIF-005 | stop 不 uninstall；INACTIVE 时幂等成功 | stop action | `ACTION_IN_PROGRESS` | T-LIF-05：停止后仍 INSTALLED；重复 stop 成功 |
| P2-LIF-006 | restart 只接受已安装且 ACTIVE/FAILED | restart action | `ACTION_NOT_ALLOWED` | T-LIF-06：INACTIVE、过渡态、未安装均拒绝 |
| P2-LIF-007 | enable/disable-autostart 不隐式 start/stop | autostart actions | `ACTION_NOT_ALLOWED` | T-LIF-07：切换前后 RuntimeState 不变 |
| P2-LIF-008 | disable-autostart 不阻止手动启动 | StartupState | — | T-LIF-08：AUTOSTART_DISABLED 状态仍能 start |
| P2-LIF-009 | `UNKNOWN`、过渡态或矛盾状态不得盲发冲突动作 | 动作矩阵 | `ACTION_IN_PROGRESS` / `INCONSISTENT_SERVICE_STATE` | T-LIF-09：所有状态组合均按矩阵得到唯一结果 |

## 5. 幂等、并发与恢复

| ID | MUST 约束 | 合同落点 | 失败码/状态 | 验收场景 |
|----|-----------|----------|-------------|----------|
| P2-OP-001 | 所有写请求要求 UUIDv4 `Idempotency-Key` | Header component | `INVALID_IDEMPOTENCY_KEY` | T-OP-01：缺失、格式错误和正确 Header |
| P2-OP-002 | 同 ID 同指纹返回原 Operation/结果 | Protocol「请求指纹」 | — | T-OP-02：断线后重发不产生第二次副作用 |
| P2-OP-003 | 同 ID 不同指纹必须拒绝 | Protocol「请求指纹」 | `OPERATION_ID_REUSED` | T-OP-03：改变 path/body 后复用 ID 返回 409 |
| P2-OP-004 | 必须先持久化 PENDING，再执行副作用 | Operation 状态机 | `OPERATION_RESULT_UNKNOWN` | T-OP-04：在命令发出点注入崩溃，Operation 可查询 |
| P2-OP-005 | PUT/DELETE 同步；七个动作返回 202 与 Location | OpenAPI 响应 | — | T-OP-05：同步响应为终态；动作可沿 Location 查询 |
| P2-OP-006 | 同服务写操作共用锁，不同服务可并发 | Protocol「并发」 | `ACTION_IN_PROGRESS` | T-OP-06：同服务冲突拒绝，不同服务同时推进 |
| P2-OP-007 | UNKNOWN 不得自动重放且隔离冲突写操作 | Operation 状态机 | `OPERATION_RESULT_UNKNOWN` | T-OP-07：重启后 UNKNOWN 未产生重复动作 |
| P2-OP-008 | 人工确认只解除隔离，不改写 UNKNOWN | acknowledge 端点 | — | T-OP-08：确认后原状态仍 UNKNOWN，审计信息完整 |
| P2-OP-009 | Operation 默认保留 30 天、硬下限 7 天 | Protocol「保留」 | `INVALID_AGENT_CONFIG` | T-OP-09：小于 7 天配置导致启动失败 |

## 6. 日志、安全与部署

| ID | MUST 约束 | 合同落点 | 失败码/状态 | 验收场景 |
|----|-----------|----------|-------------|----------|
| P2-LOG-001 | 日志 API 使用不透明 cursor，不接受任意文件路径 | logs 端点 | `INVALID_LOG_CURSOR` | T-LOG-01：伪造 cursor 无法读取其它文件 |
| P2-LOG-002 | cursor 能识别追加、轮转和截断 | `LogChunk` | — | T-LOG-02：三种文件变化返回确定 next_cursor/reset_reason |
| P2-LOG-003 | 单次读取有固定字节上限 | logs `limit_bytes` | `INVALID_REQUEST` | T-LOG-03：超限被拒，响应不超过合同上限 |
| P2-NET-001 | `/healthz` 免鉴权且只返回最小存活信息 | healthz 端点 | — | T-NET-01：响应不含版本、OS、capability |
| P2-NET-002 | 详细 Agent 信息必须鉴权 | `/api/v1/agent` | `AUTHENTICATION_FAILED` | T-NET-02：无/错误/正确 Token |
| P2-NET-003 | 非环回监听要求 TLS、Token、非空 CIDR allowlist | Agent 启动配置 | `INVALID_AGENT_CONFIG` | T-NET-03：缺任一项均拒绝启动 |
| P2-NET-004 | Agent 默认禁用 CORS | Protocol「HTTP 边界」 | — | T-NET-04：浏览器跨域预检不获授权 |
| P2-NET-005 | JSON 请求和日志响应受大小限制 | OpenAPI/Protocol | `REQUEST_TOO_LARGE` | T-NET-05：超限请求在业务层前拒绝 |
| P2-SUP-001 | 生产只允许固定 WinSW 版本与 SHA-256 | Protocol「供应链」 | `WINSW_INTEGRITY_FAILED` | T-SUP-01：错误 hash 拒绝启动，运行期不请求 GitHub latest |

## 7. 迁移与未来能力

| ID | MUST 约束 | 合同落点 | 失败码/状态 | 验收场景 |
|----|-----------|----------|-------------|----------|
| P2-MIG-001 | preview 只读并生成稳定报告和 SHA-256 | 导入 CLI 合同 | — | T-MIG-01：源目录和 Agent 数据库均不改变 |
| P2-MIG-002 | commit 只接受同版本、hash 匹配且无阻断项的报告 | 导入 CLI 合同 | `IMPORT_REPORT_INVALID` | T-MIG-02：篡改/旧版本/有 blocker 的报告全部拒绝 |
| P2-MIG-003 | 未知标签、损坏 XML、歧义参数、非法/重复 ID 阻断导入 | ServiceConfig「迁移」 | `IMPORT_BLOCKED` | T-MIG-03：每类阻断项都有字段级诊断 |
| P2-FUT-001 | Phase 2 固定 probe 与 liveness-profile Schema，但 capabilities=false | Future API paths | `UNSUPPORTED_CAPABILITY` | T-FUT-01：Agent 信息报告 false，Phase 2 不宣称能力已生效 |
| P2-FUT-002 | liveness profile 与 ServiceConfig revision 独立，并以 tombstone 删除 | liveness-profile Schema | `PROFILE_REVISION_CONFLICT` | T-FUT-02：旧 revision、同 revision 异 hash、tombstone 防复活 |
| P2-FUT-003 | liveness 默认 REPORT_ONLY；RESTART 必须有冷却/窗口/次数上限 | RecoveryPolicy Schema | `BACKEND_VALIDATION_FAILED` | T-FUT-03：缺少限制的 RESTART profile 校验失败 |

## 8. 冻结签核

| 检查 | 状态 | 证据 |
|------|------|------|
| Markdown 相对链接 | 待执行 | 本地链接检查结果 |
| OpenAPI YAML 解析 | 待执行 | YAML parser 输出 |
| OpenAPI 3.1 语义 | 待执行 | OpenAPI validator 输出 |
| Schema 示例 | 待执行 | 示例验证结果 |
| 枚举/路径/错误码一致性 | 待执行 | 跨文档扫描结果 |
| 独立 Reader Test | 待执行 | 读者问题与修订记录 |
| 用户最终通读 | 待确认 | 用户确认 |

只有前六项全部通过，Phase 2 才可标为“设计冻结，待实现”；用户最终通读用于确认产品意图，不代替机器校验。
