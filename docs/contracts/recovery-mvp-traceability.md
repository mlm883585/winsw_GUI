# Recovery MVP v1 追踪矩阵

> 状态：**已冻结，允许实现**（2026-07-16）。本矩阵把产品约束追踪到公开 Schema/API、持久化数据、错误码或状态，以及可执行验收场景；它已与 `recovery-mvp-v1`、两份 OpenAPI 完成零漂移校验并通过独立 Reader Test。它不引入 `ServiceConfig`、revision、WinSW XML 接管或其他后续阶段合同。

## 1. 使用规则

| 项目 | 规则 |
|---|---|
| 权威来源 | 产品行为以 [`recovery-mvp-v1.md`](./recovery-mvp-v1.md) 为权威；线上格式分别以 `docs/api/recovery-agent-openapi.yaml` 与 `docs/api/recovery-control-plane-openapi.yaml` 为权威；本矩阵用于证明三者可实现、可验证。 |
| 冲突处理 | 任一约束在合同、OpenAPI、实现或测试间不一致即阻止 MVP 发布，不以“实现优先”或“文档优先”掩盖冲突。 |
| 场景格式 | 每条验收均采用 `Given / When / Then`；`Then` 必须能由 HTTP 响应、SQLite 记录、SCM 状态或 Web 页面确定判断。 |
| 测试层级 | `CT`：Schema/OpenAPI 合同测试；`UT`：单元测试；`IT`：Agent/CP/SQLite 集成测试；`E2E`：多进程端到端；`WIN`：真实 Windows Server 演练。 |
| 时间参数 | 自动化测试可通过依赖注入缩短间隔，但必须另有测试确认生产默认值为心跳 10 秒、离线 45 秒、汇聚 120 秒、探针间隔 3 秒、单次超时 2 秒、总期限 60 秒。 |

公开错误码固定使用下表；未列出的实现内部异常不得直接暴露堆栈或敏感数据。

| HTTP/Operation code | 典型结果 |
|---|---|
| `AUTH_REQUIRED` / `AUTH_INVALID` | `401` |
| `SOURCE_IP_DENIED` | `403` |
| `STALE_AGENT_INSTANCE` / `ENDPOINT_SOURCE_MISMATCH` | `409`，注册/心跳不更新现有实例与服务镜像 |
| `VALIDATION_ERROR` | `422` |
| `AGENT_NOT_FOUND` / `SERVICE_NOT_ALLOWLISTED` / `OPERATION_NOT_FOUND` / `RUN_NOT_FOUND` | `404` |
| `AGENT_OFFLINE` / `SERVICE_IN_ACTIVE_RUN` / `IDEMPOTENCY_KEY_REUSED` / `DEPENDENCY_CYCLE` / `GROUP_NOT_READY` | `409` |
| `IDEMPOTENCY_KEY_REQUIRED` / `IDEMPOTENCY_KEY_INVALID` / `PROBE_TARGET_DENIED` / `PROBE_UNSUPPORTED` | `422` |
| `SERVICE_ACTION_CONFLICT` | `202` 返回已持久化且为 `REJECTED` 的 Operation |
| `SERVICE_NOT_INSTALLED` / `SERVICE_STATE_UNKNOWN` | Agent 已定位服务后的业务拒绝：`202` 返回 `REJECTED` Operation；CP 组前置校验则归入 `409 GROUP_NOT_READY` |
| `SCM_ACTION_FAILED` / `SCM_ACTION_TIMEOUT` | `202` 受理后 Operation 最终为 `FAILED`，分别表示确定 SCM 失败和确定超时 |
| `PROBE_FAILED` | `200` 返回 `passed=false` 的 `ProbeResult`；传输或协议错误才使用 HTTP 错误 |

## 2. 身份、心跳与服务镜像

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| ID-01 | `agent_id` 首次启动生成 UUIDv4，并在 Agent 重启后稳定。 | `AgentIdentity.agent_id`；Agent `meta` 表；`GET /api/v1/agent` | 合法 UUIDv4；两次启动值相同 | Given 空 Agent 数据库，When 连续启动、停止、再启动 Agent，Then 第一次写入一个 UUIDv4，第二次 API 返回相同值且数据库只有一个身份值。 | UT/IT |
| ID-02 | `boot_marker` 只能来自 WMI `LastBootUpTime` 并规范化为 UTC FILETIME 十进制；查询失败不得以进程时间替代。 | Agent boot identity provider；Agent `meta.boot_marker` | 查询失败时 Agent 不注册/不加入自动恢复并报告确定错误 | Given WMI 返回同一启动时间、格式等价时间及查询异常，When构造启动身份，Then前两者规范化为同一十进制 marker；异常场景不生成伪 marker、不向 CP 注册。 | UT/IT/WIN |
| ID-03 | 首次见到新 marker 时原子生成并保存 UUIDv4 `boot_id`；marker 不变必须复用。 | `AgentIdentity.boot_id`；Agent `meta(boot_marker,boot_id)`；`BEGIN IMMEDIATE` | Agent 进程重启不变；新 marker 对应新 UUIDv4 | Given固定 marker 并并发启动身份初始化，When事务提交，Then只生成一个 boot_id；进程重启复用该值；注入新 marker 后 marker/id 在同一事务原子替换。 | UT/IT |
| ID-04 | `agent_instance_id` 每次进程启动生成 UUIDv4；`instance_generation` 必须先在 SQLite 内原子加一。 | `AgentIdentity.agent_instance_id/instance_generation`；Agent `meta` | generation 为正整数且严格增加；每次 instance id 不同 | Given同一数据库且 boot marker 不变，When连续/并发初始化两个进程，Then generation 分别取得 n+1、n+2，两个 instance id 不同，boot_id 相同。 | IT |
| ID-05 | `sequence` 在单个 instance 内从 1 单调递增，新 instance 可重置；注册重试与心跳遵守同一乱序规则。 | `AgentReport.sequence`；`agents.last_sequence`、`agents.agent_instance_id` | 重复/乱序返回 `200 accepted=false`；当前新实例 `sequence=1` 可接受 | Given已注册当前实例 A 且接受 sequence 2，When以 register 或 heartbeat 发送 A/2、A/1，Then都不修改镜像/freshness；When先合法注册 generation 更大的实例 B 再发送 B/1，Then接受。 | IT |
| ID-06 | CP 必须用 `(instance_generation, agent_instance_id)` fencing 旧 Agent 进程。 | Register/Heartbeat Schema；`agents.instance_generation/agent_instance_id` | `409 STALE_AGENT_INSTANCE` | Given当前为 generation 8/实例 B，When generation 7/实例 A 发送任意高 sequence，或 generation 8/不同实例注册，Then均为 409，received_at、endpoint、服务镜像不变；完全相同的 8/B 注册重试幂等成功。 | IT/E2E |
| HB-01 | Agent 启动立即注册，随后按默认 10 秒、±20% jitter 主动心跳，失败按 2–60 秒退避；服务观察或 ingress 抛出意外异常也不得杀死 worker。 | `POST /api/v1/agents/register`；`POST /api/v1/agents/{agent_id}/heartbeat`；Heartbeat worker 配置与稳定失败日志事件 | 注册/心跳 `200`；调度延迟分别落在 8–12 秒及 2–60 秒范围；日志无异常文本/秘密 | Given 可控时钟和依次发生 observe 异常、ingress 异常再成功的 CP，When 启动 Agent，Then worker 持续存活、异常后重新注册、sequence 单调，失败退避不低于2秒且不高于60秒，成功后恢复正常周期，日志只含稳定事件码且不含 canary。 | UT/IT |
| HB-02 | 心跳固定包含身份、版本、endpoint、hostname 和全部 allowlist 服务摘要；services 最多 1024 项，`display_name` 始终为非 null 的 1–256 字符。 | `AgentReport`；`ObservedService[]`；注册与心跳请求体 | 缺字段、null display name、超限或未知字段为 `422 VALIDATION_ERROR` | Given 有两个 allowlist 服务的 Agent，When 生成心跳，Then Schema 校验通过且 `services` 恰含两个服务；删除必填字段、把 display_name 改为 null、提交 1025 项或增加未知字段后请求被拒绝。 | CT/IT |
| HB-03 | CP 只使用服务端 `received_at` 判断在线，不信任客户端时间。 | `agents.received_at`；Schema 不提供决定租约的客户端时间字段 | `ONLINE/OFFLINE` | Given 客户端构造过去或未来时间，When CP 接收有效心跳，Then `received_at` 为 CP 时钟且节点为 `ONLINE`；客户端时间不改变租约。 | UT/IT |
| HB-04 | 45 秒无有效心跳转 `OFFLINE`，恢复心跳转 `ONLINE`；配置及 `AgentSummary.offline_after_seconds` 均固定为常量 45；在线租约只存在于当前 CP 进程的 monotonic 时间域，重启后必须 fail closed。 | Agent list/read API；`agents.received_at` 审计字段；进程内 monotonic lease | `ONLINE -> OFFLINE -> ONLINE`；CP重启后先 `OFFLINE` | Given t0 有效心跳，When monotonic推进到t0+44.999秒，Then仍在线；推进到至少45秒后离线；Given配置或公开模型尝试使用60，Then校验拒绝；Given仅有重启前持久 `received_at`，When CP重启且尚无新报告，Then Agent为OFFLINE且AUTO/代理动作不放行。 | CT/UT/IT |
| HB-05 | 重复或乱序心跳不刷新租约、不覆盖服务状态。 | `HeartbeatAck.accepted`；`agents.last_sequence`；`services` 镜像 | `200 accepted=false` | Given sequence 5 报告 MySQL ACTIVE，When sequence 4 报告 INACTIVE，Then HTTP 为 200 幂等确认，但 `received_at`、last sequence 和 MySQL 镜像保持 sequence 5 的值。 | IT |
| HB-06 | 心跳只表示节点在线，绝不能直接把服务标记 READY。 | `ObservedService.runtime_state`；`RecoveryStep.status` | 心跳后 Step 仍为 `PENDING/PROBING`，只有探针通过才 `READY` | Given 心跳报告服务 ACTIVE 但 HTTP readiness 失败，When 执行 Run，Then Agent 为 ONLINE，但 Step 最终 FAILED，绝不因心跳进入 READY。 | IT/E2E |
| HB-07 | CP 不信任上报 endpoint host，只按实际 socket peer 与已校验端口重建 Agent 地址。 | Register/Heartbeat `endpoint`；`agents.endpoint`；ASGI peer scope | `409 ENDPOINT_SOURCE_MISMATCH` 或保存 `http://<peer-ip>:<port>` | Given peer 为 10.0.0.8，When上报带 userinfo/path/query、DNS host、10.0.0.9 host、伪造 X-Forwarded-For，Then均不使 CP连接攻击者地址；只有无附加部分的 `http://10.0.0.8:port` 被接受且 XFF 被忽略。 | UT/IT |
| SVC-01 | ObservedService 只表达安装、运行与启动策略，不伪造 ConfigState。 | `ObservedService`；`GET /api/v1/services`；Heartbeat `services[]` | Schema 拒绝未知 `config_state`；三类状态枚举固定 | Given Agent 服务列表响应，When 用 OpenAPI Schema 校验，Then 仅有合同字段；When 添加 `config_state` 或未知枚举，Then 合同测试失败。 | CT |
| SVC-02 | `local_service_id` 为 Agent allowlist 内唯一小写 slug；CP 为 `(agent_id, local_service_id)` 分配稳定唯一的 `managed_service_id` UUID。 | `agent.json.services[]`；`ObservedService.local_service_id`；CP `services.managed_service_id` 唯一约束 | 重复/非法 slug 使 Agent 拒绝启动；重复心跳复用同一 managed UUID | Given重复/非法本地 ID，When Agent启动，Then监听和 SCM 副作用前失败；Given合法服务重复注册/心跳，Then CP服务 UUID 不变且无重复行。 | UT/IT |
| SVC-03 | CP 动作、依赖、探针和 Step 只引用 managed UUID，转发到 Agent 时才解析 local ID。 | CP action path `{managed_service_id}`；Dependency/Probe/Step Schema | 未知 managed UUID 为 `404 SERVICE_NOT_ALLOWLISTED` | Given两台 Agent 都有 local id `mysql`，When分别加入组并操作，Then CP UUID 不同且每次只转发到对应 Agent 的 local `mysql`，不存在跨节点歧义。 | CT/IT |
| SVC-04 | SCM 运行状态与启动类型必须按合同固定映射，访问拒绝/未知值不得猜测。 | SCM adapter；`ObservedService` 枚举 | 精确映射为 ACTIVE/INACTIVE/FAILED/STARTING/STOPPING/UNKNOWN 与 AUTOSTART_ENABLED/DISABLED/START_BLOCKED/UNKNOWN | Given逐一注入 RUNNING、STOPPED(0/非0)、START_PENDING、STOP_PENDING、PAUSED系列、访问拒绝及 AUTO/DELAYED/DEMAND/DISABLED，When观察服务，Then结果逐项与合同表一致。 | UT/WIN |
| SVC-05 | 同一 Agent 数据库中 local ID 对 Windows 服务的历史绑定不可改绑；服务名比较大小写不敏感。 | Agent `service_bindings`；启动配置校验；Operation 持久目标 | 启动失败或 `SERVICE_MAPPING_CHANGED`；零错误目标 SCM 调用 | Given `mysql -> MySQL80` 已持久化且存在 PREPARED/DISPATCHING Operation，When重启配置改为 `mysql -> Spooler`，Then Agent 在监听前拒绝启动；直接注入恢复器时 PREPARED 为 FAILED、DISPATCHING 为 UNKNOWN，且对 Spooler 查询/动作均为0。仅把名称大小写改为 `mysql80` 不视为改绑。 | UT/IT |
| SVC-06 | allowlist/local ID 必须稳定；最近报告中消失的服务保留为内部 tombstone、退出公开服务列表，同一 `(agent_id, local_service_id)` 回归时复用 managed UUID。MVP 不自动清理带审计关联的 tombstone。 | CP `services.seen_in_last_report`；active partial index；`GET /api/v1/services` | tombstone 不计公开 1024；异常轮换新 ID 作为配置/凭据事件处置 | Given服务 A 已上报并取得 UUID，When下一报告省略 A，Then公开列表不含 A但DB行仍在；When同一 local ID 恢复，Then UUID 不变；When持续轮换新 local ID，Then运维审计将其判为异常而非自动删历史。 | IT/WIN |

## 3. Agent 动作、幂等与崩溃恢复

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| ACT-01 | Agent 只能查询和操作 allowlist 服务，且不提供任意命令、文件或服务枚举入口。动作正文可省略，提供时只能是严格空对象。 | `GET /api/v1/services`；`POST /api/v1/services/{local_service_id}/actions/{action}`；`EmptyActionRequest`；OpenAPI 路径集合 | `404 SERVICE_NOT_ALLOWLISTED`；未定义路径 `404/405` | Given allowlist 仅含 `mysql`，When 请求操作 `spooler` 或提交 cmd/PowerShell/file 字段，Then 无 SCM 副作用；未知服务为 404且不创建 Operation，额外字段为 `422 VALIDATION_ERROR`，OpenAPI 中不存在任意执行接口。 | CT/IT |
| ACT-02 | 已 allowlist 但未安装/状态 UNKNOWN 的服务不得执行动作，且业务拒绝须可审计。 | `ObservedService`；Action API；Operation `status/code` | `202 REJECTED/SERVICE_NOT_INSTALLED` 或 `SERVICE_STATE_UNKNOWN`，无 SCM 调用 | Given allowlist 中 `redis` 为 NOT_INSTALLED 或 UNKNOWN，When start，Then返回已持久化 REJECTED Operation，SCM start 调用次数为0；路由/格式/allowlist失败则无 Operation。 | UT/IT |
| ACT-03 | 只允许 `start/stop/restart`。 | Action path enum；Operation `action` | 非法 action 为 `422 VALIDATION_ERROR` 或路由 `404` | Given 合法服务，When 请求 install、delete、shell，Then 无 Operation 副作用、无 SCM 调用且请求失败。 | CT/IT |
| ACT-04 | CP动作路径使用 managed UUID并代理到所属在线Agent；未知/离线Agent不得尝试连接。 | CP `POST /api/v1/services/{managed_service_id}/actions/{action}`；服务镜像 | `404 AGENT_NOT_FOUND`；`409 AGENT_OFFLINE` | Given服务所属Agent记录缺失、OFFLINE与ONLINE，When请求动作，Then前两者分别返回稳定错误且网络调用为0；ONLINE场景只向由已验证peer构造的endpoint转发local ID。 | CT/IT |
| ERR-01 | 所有错误体固定为 `{code,message,detail,request_id}`，message不得含敏感数据或底层命令行；未知路由、错误 Method 与未捕获异常也不得退回框架默认正文。 | 两份OpenAPI `ErrorResponse` Schema；全局异常处理器 | 四字段完整；`ROUTE_NOT_FOUND/METHOD_NOT_ALLOWED/INTERNAL_ERROR` 或其他稳定 code | Given逐一触发认证、校验、服务、探针、DAG、Run、404、405与注入的未捕获异常，When校验响应，Then全部符合Error Schema、request_id可关联日志且message/detail不含Token、密码、响应正文、异常文本或命令行。 | CT/IT |
| IDEM-01 | 每个动作都要求 UUIDv4 `Idempotency-Key`。 | Header `Idempotency-Key`；Operation `idempotency_key` | 缺失为 `422 IDEMPOTENCY_KEY_REQUIRED`；格式/版本错误为 `422 IDEMPOTENCY_KEY_INVALID` | Given 合法动作，When 不带 key、带普通字符串或 UUIDv1，Then 均不调用 SCM；When 带 UUIDv4，Then 请求可被接受。 | CT/IT |
| IDEM-02 | key 在单 Agent Operation 表全局唯一；相同 key 与相同规范请求必须返回既有 Operation。 | Operation `request_fingerprint`；全局唯一索引；指纹为 `SHA256(UPPER(method)+"\n"+canonical_path+"\n"+canonical_json_body)`，空正文 `{}` | 相同 `operation_id`、相同终态；SCM 调用一次 | Given首个 start 与多个并发相同请求使用同一 key，When同时提交且在完成后再重试，Then只有一条 Operation、所有响应 ID 相同、SCM调用一次；独立测试向量验证 canonical 指纹。 | UT/IT |
| IDEM-03 | 相同 key 与不同服务或动作必须冲突。 | Operation 请求指纹 | `409 IDEMPOTENCY_KEY_REUSED` | Given key K 已用于 mysql/start，When 用 K 请求 mysql/stop 或 redis/start，Then 返回 409、原 Operation 不变、无新增 SCM 调用。 | IT |
| OP-01 | key 查重、持久化服务活动锁和 `PENDING/PREPARED` 插入必须在一个 `BEGIN IMMEDIATE` 事务中完成；提交前无 SCM 副作用。 | `operations`/活动唯一索引；Action `202 + Location` | 事务提交后公开态 `PENDING`；内部 journal `PREPARED` | Given在事务提交前注入断点并并发相同/不同 key动作，When请求执行，Then提交前 SCM 计数为0；提交后同服务至多一个活动 Operation且 key 至多一行。 | IT |
| OP-02 | Action 返回 `202`、Operation 和可查询的 `Location`。 | Action response；`GET /api/v1/operations/{operation_id}` | `202`；Location 指向同一 operation_id；未知 ID 为 `404 OPERATION_NOT_FOUND` | Given 合法动作，When 请求成功受理，Then响应为 202，正文及 Location ID 一致；GET Location 返回相同 Operation；随机 ID 返回 404。 | CT/IT |
| LOCK-01 | 同一服务写动作由持久化活动记录串行，进程重启不能丢锁；锁冲突可审计。 | Operation 活动唯一索引/事务；Operation `status/code` | `202 REJECTED/SERVICE_ACTION_CONFLICT`，内部 journal `COMPLETED` | Given mysql/start 活动且 Agent重启，When并发请求 mysql/stop，Then第二个动作仍为 REJECTED、未调用 stop；首个动作完成/收敛后活动占用才释放。 | UT/IT |
| LOCK-02 | 不同服务允许并行，整体并发不会退化为全局串行。 | 服务级锁；Operation 时间戳 | 两个 Operation 可同时为 RUNNING | Given mysql 和 redis 的 SCM mock 均阻塞，When 同时 start，Then二者都进入 RUNNING 后才释放阻塞，证明不是全局锁。 | UT/IT |
| OP-03 | 所有动作必须严格遵守冻结矩阵；确定SCM失败或超时为 FAILED，只有副作用不确定才为 UNKNOWN。 | SCM state mapping；Operation `status/error_code` | no-op `SUCCEEDED`、业务 `REJECTED`、确定失败 `FAILED/SCM_ACTION_FAILED`、超时 `FAILED/SCM_ACTION_TIMEOUT` | Given逐一组合全部运行态与动作并注入SCM错误/超时，When执行，Then结果和SCM调用与合同动作矩阵完全一致，失败使用对应稳定码；restart 对 INACTIVE/FAILED 必须 REJECTED。 | UT/IT |
| OP-04 | `PENDING/PREPARED` 可在重启后安全重新调度；`RUNNING/DISPATCHING` 只能按目标状态收敛，restart 无持久完成证据必为 UNKNOWN。 | Startup reconciliation；Operation journal | `SUCCEEDED/FAILED/UNKNOWN`；DISPATCHING 不重放 | Given分别在 PREPARED、start DISPATCHING、stop DISPATCHING、restart DISPATCHING 强退，When重启，Then PREPARED仅调度一次；start/stop已达目标可成功，否则依证据失败/UNKNOWN；restart一律UNKNOWN且无第二次SCM调用。 | IT |
| OP-05 | Operation 跨 Agent 重启仍可按原 ID 查询，且保存目标、动作、指纹、时间、错误。 | `operations` 表；Operation Schema；GET operation | 字段完整；Token/密码不存在 | Given完成与 UNKNOWN Operation，When 重启 Agent 并查询，Then ID/状态/字段不变；扫描 DB 和日志不包含 Bearer Token 或管理员密码。 | CT/IT |
| OP-06 | Worker 必须在 SCM 前原子写 `RUNNING/DISPATCHING`，返回后写公开终态/`COMPLETED`；内部 journal 不得泄漏为公开状态。 | `operations.status/journal_state`；Operation Schema | 公开仅六态；内部 PREPARED/DISPATCHING/COMPLETED | Given对 SCM 调用前后设置屏障，When执行动作，Then调用前DB已为 DISPATCHING、返回后为COMPLETED；公开响应从不把 journal 值放进 status。 | CT/IT |

## 4. Readiness 探针

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| PRB-01 | 每个服务最多一个 readiness，类型仅 `scm/tcp/http`。 | `ProbeConfig`；CP `probes` 表唯一 service UUID；`POST /api/v1/probe` | 重复设置为替换而非新增；未知 kind 为 `422 PROBE_UNSUPPORTED` | Given 一个服务已有 tcp probe，When 保存 http probe，Then数据库仍一行且为新快照；When提交 exec/file kind，Then被拒绝。 | CT/IT |
| PRB-02 | scm probe 只能查询 allowlist 服务并仅以 ACTIVE 为通过。 | `ProbeRequest` scm 变体；`ProbeResult` | 非 allowlist 为 `404 SERVICE_NOT_ALLOWLISTED`；非 ACTIVE 为 `200 passed=false, code=PROBE_FAILED` | Given ACTIVE、STARTING 和未知服务，When分别执行 scm probe，Then仅 ACTIVE passed=true；STARTING 返回 passed=false；未知服务无 SCM 任意查询。 | UT/IT |
| PRB-03 | tcp/http host 只允许 `localhost` 或 Agent 本机实际绑定 IP literal；连接前重新枚举本机地址。 | Probe discriminated union；本机地址解析器 | `422 PROBE_TARGET_DENIED` | Given localhost、127.0.0.1、本机IP、DNS名、非本机IP及IPv4-mapped IPv6，When执行并在校验后改变网卡地址，Then只有连接时仍为本机的 literal/localhost可连接；DNS、mapped绕过和非本机目标连接次数为0。 | UT/IT |
| PRB-04 | http 固定 GET 且只允许 `http`；禁止 userinfo、请求体、自定义 Header、重定向和环境代理。 | `ProbeRequest.url`；HTTP client `trust_env=false/follow_redirects=false` | 非法输入 `422 PROBE_TARGET_DENIED/VALIDATION_ERROR`；302 为 `passed=false/PROBE_FAILED` | Given file/https/userinfo URL、本机302到远端、恶意 HTTP_PROXY 及额外 body/header，When probe，Then均不向远端/代理发送；只对合规本机URL发固定GET且不跟随302。 | CT/UT/IT |
| PRB-05 | ProbeResult 只返回通过、时间、延迟、code/message，不回显响应正文或秘密。 | `ProbeResult` Schema | `passed, observed_at, latency_ms, code, message`；无 body | Given健康端点正文含 canary secret，When body_contains 成功与失败，Then响应和普通日志均不含原始正文或 canary。 | CT/IT |
| PRB-06 | timeout 为可小数 number 0.1–10（默认2），interval 为 integer 1–30（默认3），deadline为 integer 1–300（默认60）且 deadline >= timeout；期限使用 monotonic clock。 | `ProbeConfig` 数值约束；Recovery Step执行器 | 越界或 interval/deadline 小数为 `422 VALIDATION_ERROR`；Step `PROBING -> READY/FAILED` | Given边界内/外组合、timeout=0.5、interval/deadline 小数与客户端时钟跳变，When校验并执行始终超时探针，Then只接受合法数值类型；wall clock跳变不延长 deadline，默认60秒停止。 | CT/UT/IT |
| PRB-07 | 无显式 probe 必须回退 scm，并把警告持久化且展示。 | `RecoveryStep.warnings[]`；Run Detail | 数组包含 `READINESS_FALLBACK_SCM` | Given组成员无 ProbeConfig 且服务 ACTIVE，When运行，Then执行 scm、Step 可 READY，同时数据库/API/Web 均显示 fallback 警告。 | IT/E2E |
| PRB-08 | HTTP 最多读取64 KiB，`body_contains`只能为1–256个Unicode字符；每次 attempt 均持久化脱敏结果。 | Probe Schema；`probe_attempts` 表；Run Detail | 越界 `422 VALIDATION_ERROR`；attempt含时间/耗时/code/message且无正文 | Given超长needle、空needle、超过64KiB响应及失败后成功两次attempt，When执行，Then非法needle拒绝；客户端不读超限正文；Run API/Web完整显示两次脱敏attempt而不含响应body。 | CT/IT/E2E |

## 5. 恢复组、依赖、arm 与自动触发

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| DAG-01 | 依赖语义固定为 dependent depends_on prerequisite，执行边为 prerequisite -> dependent。 | `DependencyInput`；`dependencies` 表；RecoveryGroup API | 拓扑顺序确定 | Given Nacos depends_on MySQL，When保存并运行，Then MySQL READY 前 Nacos 不产生 start Operation。 | UT/IT |
| DAG-02 | 保存依赖时立即用 Kahn 算法验环，失败不得部分写入。 | 依赖替换 API；数据库事务 | `409 DEPENDENCY_CYCLE` | Given数据库已有 A->B，When一次替换提交 B->A 及另一合法边，Then返回 409，读取依赖仍为提交前完整集合，不存在部分新增。 | UT/IT |
| GRP-01 | 恢复组中涉及的所有 Agent 均为 required，不支持 OPTIONAL/DEGRADED。 | `group_services` 联接 agents；RecoveryGroup Schema | 缺节点时 `WAITING_FOR_NODES`，并列出 missing agent IDs | Given组三个服务分属三个 Agent，When任一个 Agent 离线，Then缺失列表包含该 Agent、无服务 start Operation、无部分子图执行。 | CT/IT |
| GRP-02 | GroupState 与 RunStatus 必须分离；等待节点不是 Run。 | RecoveryGroup `state`；RecoveryRun `status`；数据库检查约束 | Group 六态：DISARMED/ARMED_IDLE/WAITING_FOR_NODES/SETTLING/BLOCKED_PRECONDITION/RUNNING；Run 五态：PENDING/RUNNING/SUCCEEDED/FAILED/UNKNOWN | Given armed组缺节点，When查询组与Runs，Then组为WAITING_FOR_NODES且尚无Run；Schema/DB拒绝把WAITING_FOR_NODES写入Run状态。 | CT/IT |
| GRP-03 | 只有 DISARMED 且没有活动 Run 的组可以修改成员、依赖或探针；修改后必须重新 arm。 | Group mutation APIs；服务占用/active runs | `409 GROUP_NOT_READY` 或 `SERVICE_IN_ACTIVE_RUN` | Given分别为 ARMED_IDLE、SETTLING、RUNNING、DISARMED但仍有活动Run，When替换成员/依赖/probe，Then都不修改；只有DISARMED且无活动Run时事务成功，之后仍为DISARMED且无自动基线。 | IT |
| GRP-04 | Group 文本与全部公开集合必须使用冻结上限：name 1–128、非 null description 0–1024；服务类数组1024、依赖16384、reason/warnings/Run页100；未知字段与显式 null Patch 值拒绝。CP 对 Agent、当前可见 managed service、Group 分别执行全局 1024 门禁，计数与写入必须同事务。 | Group/Run/Step/Agent collection Schema；Pydantic strict models；CP Store `BEGIN IMMEDIATE` | 超限/null/未知字段为 `422 VALIDATION_ERROR`；边界集合 GET 为200 | Given每项边界值和边界+1、并发争抢最后名额，以及 Patch 省略/显式null，When做Schema/API/Store校验，Then 1024完整可读、1025拒绝且 sequence/租约/镜像/Group 均不部分更新；所有OpenAPI数组均有一致maxItems。 | CT/IT |
| GRP-05 | `BLOCKED_PRECONDITION` 必须持久、公开至少一个严格 PreconditionIssue；其他 GroupState 必须公开空 `blocked_reasons`。 | `RecoveryGroup.blocked_reasons[]`；`PreconditionIssue`；CP SQLite v4（v3 引入字段，v4 冻结严格形状） | BLOCKED 为1..100项；其他状态为0项；文本禁止U+0000/孤立surrogate | Given自动前置失败、正常/Disarmed组、未知字段/重复或101个service IDs、NUL前后超长文本、UUID尾随NUL及孤立surrogate，When持久化并查询/升级，Then失败组原因完整可见，其他状态为空，非法shape被Schema/DB拒绝；合法surrogate pair规范化为字面Unicode；旧blocked记录使用显式legacy原因；脏 v3 升级原子拒绝且版本不前移。 | CT/IT |
| GRP-06 | 新增 Group 成员必须仍在所属 Agent 最近有效报告中；既有 stale member 可保留或移除，但移除后在恢复上报前不得重新加入。 | 成员替换 API；`group_services`；`services.seen_in_last_report` | 新加 stale 为 `404 SERVICE_NOT_ALLOWLISTED` 且事务不变；既有 stale 在 preflight 显示 `SERVICE_NOT_REPORTED` | Given A 已在组、随后离开报告且 B 仍活跃，When保留 A 并加入 B，Then成功；When移除 A 后立即重加，Then404且成员仍只有B；When A 以同local ID回归，Then复用原UUID并可重新加入。 | IT |
| ARM-01 | arm 前必须验证所有 required Agent 在线、服务已安装、业务服务手动启动且图无环。 | arm API；ObservedService 镜像；RecoveryGroup fields | `409 GROUP_NOT_READY`，details 列出所有阻塞原因；失败后 `state=DISARMED` | Given分别存在离线 Agent、NOT_INSTALLED、AUTOSTART_ENABLED、环，When arm，Then每种情况均失败且 Group仍为DISARMED；修复全部条件后 arm 成功并进入ARMED_IDLE。 | IT |
| ARM-02 | 首次 arm 只记录当前 epoch 基线，不得立即自动启动；已 arm 状态再次 arm 必须幂等且不重置任何计时。 | `RecoveryGroup.baseline_epoch/state/candidate_epoch/candidate_stable_since` | `ARMED_IDLE`；自动 Run 数为0 | Given所有节点在线且存在 INACTIVE 服务，When首次 arm，Then保存当前 baseline并进入ARMED_IDLE且无AUTO Run；Given组处于ARMED_IDLE/WAITING/SETTLING/BLOCKED/RUNNING，When再次arm，Then返回原组且baseline/candidate/计时/Run均不变。 | IT |
| EPOCH-01 | epoch 必须按带版本前缀、字段名、换行和小写 UUID 的规范 UTF-8 文本计算，agent行按小写UUID字典序排列。 | `RecoveryRun.epoch`；canonical epoch serializer | 64位小写SHA-256 hex | Given合同固定测试向量及 agent 不同输入顺序/UUID大小写，When序列化并哈希，Then规范文本逐字节一致且hash相同；删换行、冒号拼接或非规范大小写的实现测试失败。 | UT/CT |
| EPOCH-02 | Agent 进程重启但 Windows 未重启不得产生新 epoch 或 AUTO Run。 | `agent_instance_id` 与 `boot_id`；自动调度器 | baseline/epoch 不变；AUTO Run 数不增 | Given armed group，When仅 Agent instance_id 改变并 sequence 重置，Then等待超过 120 秒仍无新 AUTO Run。 | IT/E2E |
| EPOCH-03 | 任一 required Agent boot_id 改变产生候选 epoch；候选必须连续稳定满窗口，期间再次变化从零计时。 | `recovery_groups.candidate_epoch/candidate_stable_since` | Group `SETTLING`；候选与计时点原子替换 | Given t0出现候选E1，When t0+119秒另一节点二次重启形成E2，Then候选替换为E2、计时重置；t0+120不建Run，E2连续120秒后才可建。 | UT/IT/E2E |
| SETTLE-01 | 全部 required Agent 在线后才可开始/继续默认120秒汇聚；节点掉线立即清除连续计时，重新齐全从零计时。 | `node_settle_window_seconds=120`；Group state/timer | `SETTLING -> WAITING_FOR_NODES -> SETTLING` | Given t0全部在线开始settle，When t0+119掉线并于t0+130恢复，Then期间零Run/零动作且新计时从t0+130开始；t0+249仍无Run，t0+250才可创建。 | UT/IT/E2E |
| SETTLE-02 | 缺节点属于 Group `WAITING_FOR_NODES`，禁止预建 Run 或执行部分子图。 | RecoveryGroup `state/missing_agent_ids`；runs表 | `WAITING_FOR_NODES`；Run数与start Operation数均为0 | Given候选epoch存在但一节点离线到窗口之后，When反复扫描，Then缺失列表准确且无Run；节点恢复后必须重新完整settle，而非立即运行。 | IT/E2E |
| EPOCH-04 | AUTO Run 由 `(group_id, epoch)` 部分唯一索引保证一次；创建Run、写last_scheduled_epoch与占用全部成员服务必须同事务。 | `recovery_runs` AUTO部分唯一索引；`service_run_locks`；group字段 | 同epoch AUTO Run恰为1，事务全成或全不成 | Given两个调度worker并发且在事务各阶段注入崩溃，When提交，Then不存在“有Run无占用”或“更新epoch无Run”；成功时仅一Run、一组步骤和每服务一个占用。 | IT |
| EPOCH-05 | CP 重启本身不改变 epoch，也不能误触发自动恢复。 | 持久化 baseline/candidate；启动扫描 | AUTO Run 数不增 | Given armed group 且 boot IDs 均未变，When CP 重启一次或多次并等待 120 秒，Then不创建 AUTO Run。 | IT/E2E |
| ARM-03 | AUTO Run 创建前必须再次校验在线、安装、手动策略与无环；arm 后漂移不得产生动作。 | Scheduler preflight；RecoveryGroup state | `BLOCKED_PRECONDITION`，零Operation | Given成功arm后把服务改Automatic或卸载，Whenboot epoch变化且settle到期，Then不创建/执行Run，组进入BLOCKED_PRECONDITION并显示原因；即使外部条件恢复也保持隔离，只有disarm、修复、重新arm才建立新baseline。 | IT/WIN |

## 6. RecoveryRun 执行、失败传播与恢复

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| RUN-01 | Run 创建时必须持久化成员、依赖和 probe 快照；执行不回读可变组配置。 | Run/Step/Dependency/Probe快照表 | Run Detail 显示创建时快照 | Given创建Run后在测试事务中改变源组配置（不改快照），When恢复执行，Then仍按旧图/旧probe；公开API同时由GRP-03证明活动Run期间修改会被拒绝。 | IT |
| RUN-02 | 使用 Kahn 拓扑层，同层最多并行 4 个服务。 | Recovery executor；Step 时间戳 | 同时 `STARTING/PROBING` 数不超过 4 | Given一层含 6 个互不依赖服务且 Agent 调用被阻塞，When执行 Run，Then最多4个进入活动状态；释放一个后才调度第5个。 | UT/IT |
| RUN-03 | ACTIVE 服务不得重复 start，但必须执行 readiness。 | `ObservedService.runtime_state`；Step 状态 | 无 start Operation；`PENDING -> PROBING -> READY/FAILED` | Given服务 ACTIVE 且 probe 可计数，When执行，Then start 调用为0、probe至少1次，结果决定 Step 终态。 | IT |
| RUN-04 | POST Agent 动作前必须持久化 `dispatch_idempotency_key`；收到响应后保存 operation_id，后续只按原 ID 对账。 | `RecoveryStep.dispatch_idempotency_key/operation_id`；Agent action/operation API | `STARTING`；key为UUIDv4 | Given服务INACTIVE，When分别在保存key前、保存key后但收到operation_id前、保存operation_id后强杀CP，Then首种可正常生成一次key；第二种重启用同key重发并取回同一Operation；第三种只GET原ID；SCM副作用始终至多一次。 | IT/E2E |
| RUN-05 | 只有 readiness READY 才可放行严格下游。 | Step `PROBING/READY`；依赖快照 | 下游保持 `WAITING_DEPENDENCY` | Given A->B 且 A start 已成功、probe 前两次失败后成功，When运行，Then B 在 A READY 前无 action；A READY 后才可 STARTING。 | IT/E2E |
| RUN-06 | FAILED/UNKNOWN 上游只阻塞其全部可达下游；无依赖分支继续，所有步骤终态后才计算Run。 | `RecoveryStep.status/root_cause/dependency_chain` | 下游 `BLOCKED`；终态优先级 `UNKNOWN > FAILED > SUCCEEDED` | Given A->B->C 与独立D/E，When A FAILED、D UNKNOWN、E READY，ThenB/C BLOCKED且根因链指向A，E完成；全部终态后Run为UNKNOWN。去掉UNKNOWN分支后同图Run为FAILED。 | UT/IT |
| RUN-07 | 无法确认 Operation 结果时 Step/Run 为 UNKNOWN，不得用新key重发。 | Operation/Step/Run `UNKNOWN`；dispatch key | `UNKNOWN`；动作副作用至多一次 | Given Agent接受动作后断线且原Operation最终无法查询，When对账到确定性边界，Then Step UNKNOWN；其下游BLOCKED、独立分支继续，CP不构造第二个key或start。 | IT/E2E |
| RUN-08 | CP 重启后扫描未终态 Run；有 operation_id 的 Step 先对账，只有未产生副作用的 PENDING 可继续。 | 启动 reconciliation；Step journal | 原 run_id/operation_id 保持；无从头重放 | Given Run 中 A READY、B STARTING 且有 operation_id、C PENDING，When强杀并重启 CP，Then A不重跑，B查询原 Operation，确认后才调度 C；每个服务 start 最多一次。 | IT/E2E |
| RUN-09 | Run now 与 Retry 均创建新 MANUAL Run；Run now需全节点在线/前置通过，Retry复制父Run整图快照并重跑；可选 reason 非 null 时为1–512字符。 | runs/retry API；`ManualRunRequest.reason`；`RecoveryRun.trigger/retry_of_run_id` | 新run_id；非法reason `422 VALIDATION_ERROR`；前置失败 `409 GROUP_NOT_READY` | Given组当前配置已改且父AUTO Run失败，When Retry，Then新Run使用父图/probe快照并执行整图；When reason为空串、513字符或未知字段，Then不创建Run；省略/null/1及512字符可进入业务校验。 | CT/IT |
| RUN-10 | Run/Step 状态只能使用冻结枚举并遵守终态约束；WAITING_FOR_NODES只属于Group。 | RecoveryRun/RecoveryStep Schema；数据库检查约束 | Run五态；Step八态：PENDING/WAITING_DEPENDENCY/STARTING/PROBING/READY/FAILED/BLOCKED/UNKNOWN | Given公开响应与DB样本，When Schema/约束测试，Then未知值及Run.WAITING_FOR_NODES无法写入；READY/FAILED/BLOCKED/UNKNOWN Step不被调回活动态。 | CT/UT |
| RUN-11 | 所有非终态 Run 必须独占其 managed services；跨组/Run不得并发占用，CP重启后占用随原Run恢复。 | `service_run_locks(managed_service_id,run_id)`；Run创建事务 | `409 SERVICE_IN_ACTIVE_RUN` | Given组G1的Run占用服务S，When G2 Run now/Retry/AUTO也包含S，Then请求/调度不创建第二Run且返回/记录冲突；强杀重启CP后仍冲突；原Run终态事务提交后占用释放，新Run才可创建。 | IT/E2E |
| RUN-12 | 所有 AUTO/MANUAL Run 必须可公开发现；列表支持 group/trigger/status 过滤，并按 `(created_at, run_id)` 倒序做稳定、过滤绑定的 keyset 分页。 | `GET /api/v1/recovery-runs`；`RecoveryRunCollection.items/next_cursor`；Dashboard 最近 Runs | 管理员可读；非法/跨过滤游标 `422 VALIDATION_ERROR`；无重复或遗漏 | Given Scheduler 后台创建 AUTO Run 且调用方未知 run_id，When管理员查询列表或打开Dashboard，Then可进入其详情；Given多条同 created_at Run，When以有界limit遍历各页并组合过滤，Then每条恰出现一次且顺序稳定；未登录或cluster token调用被拒，非法游标只返回固定脱敏错误体。 | CT/IT |
| RUN-13 | CP 必须在保存首次 POST 的 operation_id 及每次 GET 对账后续处理前，先完成 Operation Schema 校验，再验证其与持久 start dispatch 的完整语义绑定；Windows 服务名仅按大小写不敏感等价。Schema 异常必须使用脱敏的类型化协议错误，不能当作通信失败重试。 | 真实 AgentClient Operation 反序列化；Agent Operation；`RecoveryStep.agent_id/local_service_id/dispatch_idempotency_key/operation_id`；成员快照 `windows_service_name`；canonical POST 空正文指纹 | 任一 Schema 非法或错绑使 Step/Run 立即 `UNKNOWN`；Run `failure_code=AGENT_PROTOCOL_MISMATCH`；零 probe、零重发；严格下游 `BLOCKED` | Given真实 AgentClient 通过 HTTP 分别收到缺字段、非法 UUID 的 `202 POST` 与 `200 GET` Operation，When执行或恢复 Run，Then仅发出一次对应请求、错误及 Step 消息不含原始正文/ValidationError、POST 不保存 operation_id、GET 保留原 operation_id 且立即隔离；Given分别篡改各语义绑定字段，Then同样在 probe/下游放行前隔离；Given仅改变 Windows 服务名大小写，Then可正常对账；Given另有独立分支，Then其继续至终态。 | UT/IT |

## 7. HTTP 实验安全、Web 与部署边界

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| SEC-01 | cluster token 只授权 Agent注册/心跳以及 CP调用Agent；不能访问CP管理API。`/healthz`免鉴权且最小化。 | 两份OpenAPI security；认证依赖分离 | 缺失/错误token `401 AUTH_REQUIRED/AUTH_INVALID`；Bearer token调用管理API仍未授权 | Given正确cluster token，When调用注册/心跳和Agent API，Then按业务处理；When调用组、Run、代理动作或Web管理API，Then被拒且无副作用；healthz不返回身份、服务、路径或配置。 | CT/IT |
| SEC-02 | Agent 必须按 socket peer 限制 CP 来源，CP register/heartbeat 也必须按 socket peer 限制 Agent 来源；两端都不读取 `X-Forwarded-For`。 | Agent trusted CP IP、CP `agent_source_cidrs`；两端 OpenAPI 403；ASGI peer来源校验 | 缺失/错误 token `401`；来源拒绝 `403 SOURCE_IP_DENIED` | Given正确Token但真实peer不在白名单且XFF伪造为允许IP，When分别调用Agent action/probe和CP register/heartbeat，Then仍403且无副作用；真实白名单peer即使XFF不同也按真实peer判定。 | CT/UT/IT |
| SEC-03 | Agent 默认不启用 CORS，浏览器不能绕过 CP 直接控制。 | FastAPI middleware 配置 | 无 `Access-Control-Allow-Origin` | Given任意 Origin 的预检与 action 请求，When访问 Agent，Then响应不授予跨域权限；实现不存在宽松 CORS 中间件。 | CT/IT |
| SEC-04 | Web 使用单管理员登录和签名、HttpOnly、SameSite 会话；所有写操作校验 CSRF。 | login/logout；session cookie；Web form endpoints | 未登录重定向/401；CSRF 失败为 403 | Given未登录、伪造签名 cookie、合法登录三种客户端，When访问受保护页，Then仅合法会话成功；Set-Cookie 含 HttpOnly/SameSite；缺失或错误 CSRF 的写请求无数据库/Agent副作用。 | IT/E2E |
| SEC-05 | Token、密码、探针响应秘密不得写入 Operation、SQLite 普通字段或日志。 | 日志过滤器；Operation/ProbeResult Schema | canary secret 搜索结果为 0 | Given Token、管理员密码和带秘密正文的探针，When覆盖成功与异常路径，Then扫描日志、API响应、Operation和Run错误信息均找不到 canary。 | IT |
| SEC-06 | Agent 配置、SQLite 与日志目录在 Windows 上仅服务账户和 Administrators 可读。 | 部署 PowerShell ACL 步骤 | ACL 审计通过；普通用户访问被拒 | Given按脚本安装的 Agent/CP，When用 `Get-Acl` 检查并以普通本地用户读取，Then继承被关闭或收敛，只有指定服务账户、SYSTEM/Administrators拥有所需权限。 | WIN |
| SEC-07 | 明文 HTTP 构建必须明确标识实验档位和非生产状态。 | `GET /api/v1/agent`、CP agent/version API 或 Dashboard build banner | `security_mode=LAB_HTTP`、`production_ready=false` | Given默认 MVP 配置，When读取 Agent/CP信息并打开 Dashboard，Then均能看到 LAB_HTTP/非生产警告；不存在把 HTTP 配置报告为生产就绪的路径。 | CT/E2E |
| SEC-08 | cluster token 必须来自至少32个随机字节，只从Authorization header读取并常量时间比较。 | 配置校验；Bearer认证实现 | 短token拒绝启动；query token无效 | Given31/32字节token、query中的token、header中的正确/错误token，When启动并鉴权，Then短token使启动失败；query不被读取；正确header成功；代码/测试证明比较路径使用常量时间函数。 | UT/IT |
| DEP-01 | Agent/CP 自身安装为 Automatic；受编排业务服务必须 Manual，arm 时再次验证。 | 固定 WinSW 部署脚本；SCM start type；arm API | 脚本审计 + `GROUP_NOT_READY` | Given安装包，When执行部署脚本并查询 SCM，Then Agent/CP 为 Automatic；业务服务为 Automatic 时 arm 返回 409，脚本不擅自改业务服务启动类型。 | WIN |
| DEP-02 | CP 部署在独立管理节点，其 SQLite 不依赖被管理 MySQL。 | CP 配置/依赖清单；SQLite DSN | CP 在全部业务服务停止时仍可启动 | Given MySQL/Redis/Nacos 全停，When重启 CP，Then CP、Dashboard、SQLite和调度恢复均可工作。 | E2E/WIN |
| DEP-03 | Agent/CP 使用 PyInstaller onedir；现场部署必须使用已评审 WinSW lock 固定版本、架构、URL、size、Authenticode 状态与 SHA-256，禁止 `latest`。源文件、复制后的 staged wrapper 与发布后的最终 wrapper 必须使用同一 lock 重验，最终重验先于 SCM install。 | build/install PowerShell；`winsw-x64-v2.12.0.lock.json`；`WinSWLockPath`；`STAGED_WINSW_VERIFY/PUBLISHED_WINSW_VERIFY` journal 阶段 | 任一 lock 属性/hash不符均在SCM变更前中止并进入可证明回滚；未签名事实可见 | Given合法 lock+离线资产、复制后立即篡改 staged wrapper、staged 校验后篡改待发布 wrapper和不同签名状态，When执行安装，Then两类复制后篡改均在SCM前失败，服务不存在，本次 package/service/staging 被正确回滚或明确报告残留；流程不查询GitHub latest，也不把本地 hash 宣称为上游签名保证。 | CT/IT/WIN |
| DEP-04 | Recovery 安装器必须以显式阶段 journal 实现失败回滚；既有受管路径或服务一律门禁，不覆盖、不猜测所有权。只有能够证明属于本次 wrapper 的 Recovery 服务才可先 stop 后 uninstall；仅删除本次创建的 package/service/staging，既有 ACL 恢复原 Owner+DACL，配置、SQLite、日志及其他数据保持不变。 | `install_recovery_service.ps1` journal、ACL SDDL 快照、服务 ImagePath 所有权检查、聚合错误 | `INSTALL_FAILED` 同时显示 primary、rollback issues 与 `retry_safe`；残留无法证明或回滚不完整时 fail closed | Given分别注入 WinSW install失败、start失败、rollback stop/uninstall失败，并预置同名服务、package/service/staging或普通数据文件，When安装，Then绝不删除非本次服务/文件；可完整回滚时服务与受管路径均消失、ACL恢复且第二次执行可成功；无法完整回滚时保留wrapper依赖并给出稳定人工处置指引。 | CT/IT/WIN |
| DEP-05 | `PostInstall` 必须证明 listen port 的全部 listener PID 均属于预期角色进程树；存在任一额外 PID 时不得通过。 | Host preflight `PORT_AGENT_LISTEN/PORT_CONTROL_PLANE_LISTEN`；每 PID executable、父进程链与 invalid PID 证据 | 全部 PID 合法才 `PASS`；任一 PID 路径/祖先不符为 exit code `7` | Given同一端口同时观察到一个合法角色 PID 和一个可执行路径相同但不属于 wrapper 进程树的冒充 PID，When运行 ownership assessment/PostInstall，Then端口检查 FAIL，并在 evidence 中列出该非法 PID；仅合法 PID 时 PASS。 | CT/WIN |
| DEP-06 | 同一主机的 Agent `database_path + listen_port` 只能由安装器创建的固定 WinSW 服务单进程拥有；不支持开发进程、复制服务或第二配置并行复用。 | Agent 配置/部署手册；固定 WinSW service id；PreInstall/PostInstall 端口归属门禁 | PreInstall 端口非空或 PostInstall 存在额外 listener 时 exit code `7`；不得进入恢复组 | Given固定 Agent 服务已安装运行，When再启动源码 Agent或复制服务复用同一数据库/端口，Then部署检查拒绝；停止固定服务后只允许一次受控维护启动，恢复服务前必须结束维护进程。 | CT/WIN |
| DEP-07 | 非秘密 Deployment Inventory 必须在配置生成前证明独立CP、至少3个Agent、服务/主机唯一性、本机readiness、五角色同组与严格验收DAG；输出配置固定含无效secret sentinel且`config_ready=false`。 | `recovery-deployment-inventory-v1.md`；CP `--prepare-deployment`；draft configs/blueprint/manifest | 合法输入exit0；非法输入固定脱敏exit2；目标机秘密注入前全部`--check-config`失败，注入后通过 | Given合法三Agent示例，When准备部署，Then原子生成3份Agent/1份CP草案、blueprint和可复算manifest且不建库/联网；Given未知字段、远端probe、环或缺验收边，Then目标目录不存在；Given已有输出目录，Then用户文件不变。 | CT/IT |
| DEP-08 | Host Facts 只读取当前主机且作用域由显式Windows Service Name/候选端口限定；不得扫描远程主机、输出秘密/服务账户/ImagePath或修改SCM/Firewall；地址必须可直接用于 Inventory。 | `get_recovery_host_facts.ps1`；`recovery-host-facts` JSON | `side_effects=NONE`、`remote_hosts_scanned=0`；非法作用域/非法端口类型/缺失事实为脱敏FAIL/exit2；采集器错误不得伪装为空闲 | GivenPS5.1与collector替身，When传合法服务/端口，Then只输出字段白名单、稳定数组和非loopback/link-local/unspecified/multicast/mapped地址；When无服务、重复/文本非法端口，Then任何collector调用为0且真实CLI返回JSON/exit2；When端口提供程序失败，Thenoccupied/listening为null且FAIL；静态审计不存在远程和写操作命令。 | CT/WIN |

## 8. SQLite、迁移与时间

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| DB-01 | Agent 与 CP SQLite 每个连接都必须启用 WAL、`synchronous=FULL`、`foreign_keys=ON` 和非零 `busy_timeout`。 | 两端 connection factory；PRAGMA | PRAGMA 查询值符合合同 | Given全新与既有数据库，When应用启动并打开读写连接，Then逐项PRAGMA断言通过；插入悬空外键失败；并发短锁在busy timeout内等待而非立即报错。 | UT/IT |
| DB-02 | 两端都有显式整数Schema version，只允许顺序、事务化向前迁移。 | `schema_version`；编号migration | vN只能经N+1…目标版本 | Given版本0、每个历史版本及缺少中间migration，When启动，Then合法版本按顺序升级且最终结构一致；缺迁移时拒绝启动，不跨级猜测。 | UT/IT |
| DB-03 | migration 必须原子；失败回滚结构和版本，数据库高于程序版本时拒绝启动，禁止降级/清空。 | migration runner transaction | 启动失败但原DB完整 | Given在migration中间注入异常和版本=程序支持+1的DB，When启动，Then前者schema/version均回到迁移前，后者拒绝启动；文件与业务行不被删除。 | IT |
| DB-04 | Operation、Run/Step、图/probe快照、服务占用及每次probe attempt必须持久化并可跨进程恢复。 | Agent/CP核心表与外键 | 重启后ID、状态、快照、占用和attempt历史一致 | Given每类记录各一条并含未终态样本，When分别强杀Agent/CP并重启，Then所有记录仍可查询且恢复器基于原记录继续，不从内存重建或丢历史。 | IT/E2E |
| DB-05 | 持久时间使用UTC RFC3339；同进程超时与在线租约使用monotonic clock，不信任客户端时间，且不得把重启前墙钟时间恢复成活租约。 | 所有`*_at`字段；Clock依赖；进程内 Agent lease | 时间带`Z`/UTC offset并可Schema校验；重启租约 fail closed | Given本地时区非UTC、wall clock前后跳、伪造客户端时间及带近期`received_at`的CP数据库，When写Operation/heartbeat/Run/probe attempt、执行超时并重启CP，Then持久值规范UTC；wall clock跳变不延长或提前租约/超时；重启后既有Agent先OFFLINE，只有新有效报告重建monotonic租约。 | CT/UT/IT |
| DB-06 | CP v5 migration 必须在同一升级事务验证 Agent、当前可见服务、Group 三个公开集合容量并创建 active service partial index；不得截断或自动清理历史。 | `schema_versions=5`；`idx_services_active_agent_local`；三类 COUNT | v4=1024 升级；v4=1025 以 `IntegrityError` fail closed，版本/数据保持v4 | Given三类旧v4库分别含1024与1025项，When启动新CP，Then前者升到v5且索引存在，后者启动拒绝、索引不存在、版本与全部行不变；备份/离线人工修复是唯一恢复路径。 | IT/WIN |

## 9. 范围防回归与最终验收

| ID | 产品约束（MUST） | Schema / API / 存储映射 | 错误码、状态或可观测结果 | 可执行验收场景 | 层级 |
|---|---|---|---|---|---|
| SCOPE-01 | MVP 不接管 WinSW XML，不实现 ServiceConfig CRUD、revision、安装/卸载或 Linux。 | OpenAPI 路径与 Schema 清单；依赖/源码扫描 | 未定义路径 `404/405`；不存在配置 revision 字段 | Given生成后的 OpenAPI，When对路径和 Schema 做快照测试，Then不存在 ServiceConfig、revision、install、uninstall、XML、systemd/Linux 管理接口；现有 Tkinter GUI 文件无行为改动。 | CT |
| SCOPE-02 | MVP 不实现 CP HA、RBAC/多用户、日志中心、拓扑拖拽、liveness restart、TLS/MSI/自动升级。 | Web/API 页面快照与依赖清单 | 不适用；实验警告持续存在 | Given MVP 构建产物，When审计路由、页面和依赖，Then只存在三页最小 Web 与单管理员模型，不宣称上述能力。 | CT/E2E |
| SCOPE-03 | CInfoCollect 仅作为主动心跳思路参考，不复制其源码；现有GUI继续负责注册WinSW业务服务。 | 源码/许可证清单；git diff | 无第三方复制文件；legacy GUI行为不变 | Given实现提交，When做来源审计与legacy回归，Then无CInfoCollect源码/资源片段或隐式依赖，且现有GUI仍可注册原服务，orchestrator不创建/安装业务服务。 | CT/IT |
| ACC-01 | 真实链路至少跨三台 Windows Server：`MySQL + Redis -> Nacos -> Java -> Nginx`。 | Recovery group、Dependency、Probe、Run Detail | 每一步最终 READY；时间线符合依赖 | Given三台以上服务器和冻结拓扑，When随机开机并触发恢复，Then MySQL/Redis 都 READY 后才启动 Nacos，随后 Java，再 Nginx；Run Detail 可解释每个时间点。 | WIN |
| ACC-02 | 必须覆盖随机开机顺序、CP 最后启动、单机重启和缺节点。 | Agent lease、epoch、Run/Step 记录 | 无乱序；缺节点零业务动作；单机仅补齐失效服务 | Given固定验收脚本轮换上述四类故障，When执行每轮，Then CP 最后启动仍恢复；缺节点无 start；单机重启时已 ACTIVE 上游不重复 start但仍 probe。 | WIN |
| ACC-03 | 必须覆盖 start失败、probe超时、Agent动作后断线、CP Run 中途强退。 | Operation/Run/Step journal；Run Detail | FAILED/BLOCKED/UNKNOWN 或恢复继续，均有根因 | Given逐项故障注入，When运行，Then失败传播和 UNKNOWN 与本矩阵 RUN-06～RUN-08 一致；CP重启不从头重放。 | E2E/WIN |
| ACC-04 | 连续 10 次全链随机冷启动必须零乱序、零同 epoch 重复 AUTO Run、零未知服务操作。 | 验收报告；`recovery_runs` 唯一约束；Agent审计记录 | 10/10 通过；三个违规计数均为 0 | Given清空业务运行态但保留系统与审计数据，When以记录随机种子的方式完成10次真实全链冷启动，Then每次成功且数据库查询证明乱序=0、重复AUTO=0、非allowlist操作=0；任一计数非0则 MVP 不可用。 | WIN |
| ACC-05 | Web 时间线必须准确说明 READY、FAILED、BLOCKED/UNKNOWN 原因。 | Dashboard、Recovery Groups、Run Detail；Run API | 页面与数据库/API一致 | Given成功、start失败、probe失败、依赖阻塞和未知结果样本 Run，When逐一打开 Run Detail，Then服务、Operation ID、探针结果、根因和依赖链与 API/SQLite一致且不泄密。 | E2E/WIN |
| ACC-06 | 十轮成功冷启动与故障注入必须分别留存结构化证据；不得靠场景标签或伪挂成功轮次来冒充故障覆盖。 | `recovery-mvp-evidence-v1.md`；Evidence JSON/Report；`scenario_exercises[]` 独立窗口；Action 的 `cold_round_number` XOR `scenario_exercise_id`；`manual_proof_records[]` | 离线报告 `PASS/FAIL`；场景 Run 漏导、Run 多重归属、Action 跨归属、CP/采集侧时间越窗、远端 Operation 时间不自洽或缺人工证据均为 `FAIL` | Given十个成功 AUTO Run 及独立 start/probe/断线等场景，When运行离线证据校验器，Then要求导出十轮和场景引用的全部 MANUAL/AUTO Run（含单节点重启）、复算唯一性/拓扑/放行顺序/失败传播，并验证 Run/Step/Action 包装层/未知服务负向请求属于正确窗口；Agent Operation 时间仅在自身时钟域检查 offset 和单调性；CP-last、进程/OS重启等缺 artifact ref、SHA-256 或复核人时不得 PASS。 | CT/WIN |

## 10. 发布门槛

MVP 只有同时满足以下条件才可标记“可用”：“实现完成”或本地测试通过不能替代真实断电验收。

| 门槛 | 证据 |
|---|---|
| 合同一致 | OpenAPI 语义校验及所有示例/Schema 测试通过；状态、路径、字段与本矩阵零漂移。 |
| Reader Test | 独立读者仅凭合同、两份OpenAPI与本矩阵能正确回答身份fencing、epoch/settle、dispatch崩溃窗、失败传播、安全边界和迁移问题，且无新的阻塞歧义。 |
| 自动化验证 | 所有 `CT/UT/IT/E2E` 场景通过，并保存测试报告。 |
| Windows 安全与部署 | 构建机只读烟雾通过；每台目标机依次取得 `PreInstall(Frozen)=PASS` 与 `PostInstall=PASS`，并复核 SCM/WinSW 进程树、启动类型、端口归属、完整数据树 ACL、来源 IP 限制及 LAB_HTTP 警告。 |
| 真实恢复 | 至少三台 Windows Server 完成 ACC-01～ACC-06；连续 10 次成功随机冷启动与九类独立故障演练全部完成，离线证据报告为 `PASS`，人工证据内容已逐项复核。 |
| 可解释性 | 每个失败或阻塞均有确定状态、稳定错误码和可见根因；任何无法证明的副作用都保持 UNKNOWN，不以重试伪造成功。 |
