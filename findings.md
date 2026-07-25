# Findings & Decisions: Recovery MVP 现场部署与三机验收

## Requirements

- 使用 `planning-with-files` 维护持久化规划材料，不能只创建不更新。
- 阶段完成、关键发现和错误发生后及时维护计划、发现与进度。
- 重要决策前回读相关规划内容，避免上下文丢失。
- 延续既定 Recovery MVP：功能优先、严格依赖、普通受控局域网 HTTP + Token。
- 真实三机连续 10 轮随机冷启动通过前，不把 MVP 标为可用或生产就绪。

## Baseline Findings

- Recovery MVP 合同、Agent/CP OpenAPI、严格 DAG、最小 Web、部署脚本和证据合同已经完成。
- 最终 `dist-recovery` 包含 Agent、Control Plane、evidence validator 三个 onedir 包。
- 最近一次全量回归为 `129 passed`，另有 4 条第三方弃用警告。
- 发布清单覆盖除清单自身外的 330 个文件：零缺失、零额外、零哈希差异、零 reparse point。
- Windows PowerShell 5.1 下发布根烟雾通过；仅观察 `EventLog`，未调用 action 或 Run，结束后无残留进程/端口。
- 独立 Reader Test 已对构建、发布布局、清单和部署顺序给出 PASS，blocker 为无。
- 既有材料曾把主要缺口判断为现场主机信息，但活动目标要求“根据方案文档进行编码”；因此必须先做合同到实现的当前状态审计，不能预设代码零缺口。

## Current-Session Findings

- Phase 1R 公开集合容量门禁已落到同一 SQLite 写事务：新 Agent、当前上报可见服务总量和恢复组总量达到 1024 后均以 `422 VALIDATION_ERROR` 拒绝；服务超限回滚已证明不会推进 heartbeat sequence，也不会扩大公开镜像。当前仍需补齐 Agent 总量边界/API 错误序列化证据，并在合同中明确“历史未上报服务不计入公开集合”与拒绝写入的原子性。
- `Database.transaction()` 使用进程写锁加 `BEGIN IMMEDIATE`，因此容量计数与插入/镜像替换不会在同一 CP 进程的并发写入间超卖；默认服务列表只返回 `seen_in_last_report=1`，历史未上报行不属于 1024 项公开服务集合。尚存的升级风险是：旧 v4 数据库若已积累超过上限的 Agent/活动服务/Group，新版本单靠未来写门禁仍会在 GET 响应校验时失败；需要启动时 fail-closed 校验或明确迁移策略。
- CP 现有迁移器会在 `BEGIN IMMEDIATE` 内顺序执行并在异常时回滚版本，已有脏 v1/v3 fail-closed 测试先例；若决定兼容旧候选数据库，最小一致方案是新增仅做容量不变量验证的顺序 migration，而不是在 GET 静默截断对象或返回不完整编排状态。
- Control Plane App 支持注入 Store，适合用缩小后的容量常量做 HTTP 边界测试：验证第二次写返回稳定 `422 VALIDATION_ERROR` 且 GET 仍返回完整边界内集合；Store 另保留真实 1024/1025 测试，避免仅靠替换常量证明冻结数值。
- 容量 HTTP 回归应紧邻现有 Group 严格输入测试，并复用登录/CSRF 边界；这样同时证明超限由业务 Store 转译成公开错误体，而非被响应模型转成 500。
- 最小 Web readiness 子任务已实现按服务回显已保存的 TCP/HTTP/SCM 定义、无配置时明确显示 SCM fallback、DELETE 后刷新与绑定服务 ID 的防串写保护；定向 UI/API 11 项通过，仍需 root 复读差异并纳入稳定全量回归后才能关闭 Phase 1R 检查项。
- Root 联合运行容量三条 Store 边界、Group HTTP 超限和 Web readiness UI 合同共 5 项全部通过；真实 1024/1025 Group/Service 边界没有被仅使用小常量的 API 测试替代。
- 独立容量审计确认冻结阻塞项：旧 schema v4 若已含 1025 个 Agent、`seen_in_last_report=1` 服务或 Group，当前 Store 仍可启动，随后集合 GET 在响应模型处变成 500；活动服务超限还会让后续心跳全部被拒且无法自愈。决定新增 v5 容量不变量 migration：1024 原子升级通过，1025 原子 fail closed，不静默截断/删除历史；运维文档给出备份与人工修复边界。
- v5 不需要改表结构：在现有顺序迁移事务中分别计数 `agents`、`services WHERE seen_in_last_report=1`、`recovery_groups`，任一大于 1024 抛出稳定 `sqlite3.IntegrityError`，版本保持 v4、数据不变。历史 `seen_in_last_report=0` 服务不计入公开容量，符合默认 GET 语义。
- 容量复审另发现两个需冻结的内部语义：Group 当前可新加入历史未上报服务，直到 arm 才以 `SERVICE_NOT_REPORTED` 阻塞；以及历史 `services` 行没有 retention/硬上限与 active partial index，恶意或漂移 allowlist 可长期扩大 SQLite 并让心跳容量 COUNT 扫描全表。需要在 v5 前决定“stale member 新增”策略与历史服务保留/容量策略，不能仅修公开响应 500。
- 历史服务不能按“未上报即删除”简单清理：`group_services`、活动服务锁、CP proxy dispatch 与 proxy operation 都用 `ON DELETE RESTRICT` 保留关联，而 Run step 已保存服务快照。MVP 最小方案应拒绝把 stale 服务新加入 Group、允许既有 stale member 留待显式移除；历史容量另以索引与固定 allowlist 运维约束处理，避免引入不完整的审计数据 GC。
- 已冻结最小 MVP 语义：`replace_members` 对新增项要求 `seen_in_last_report=1`，既有 stale member 可保留或移除，服务用相同 `(agent_id, local_service_id)` 回归时复用 managed UUID；v5 新增 active partial index。MVP 不做 tombstone 自动 GC/硬上限，合同要求 allowlist/local ID 稳定且低频变更，异常轮换按配置错误或凭据事件告警处置。
- v5 迁移测试通过“先建空 v5、再回退为真实 v4 形状”构造旧库；回退必须同时删除 v5 新增的 partial index，仅改 `schema_versions` 会制造不真实的重复索引失败。该测试夹具已在运行前修正。
- Phase 1R 容量定向集现为 11 passed：覆盖 Agent/活动服务/Group 写容量、HTTP 422、三类 v4 的 1024 升级、1025 原子拒绝、partial index，以及 stale member 保留/移除/拒绝新加/同 ID 回归。
- 文档同步范围已确定：合同 §2/§6/§10、追踪 SVC/GRP/DB、CP OpenAPI members 描述，以及运维手册的固定 allowlist 与 v4→v5 fail-closed 故障处置；安装器仍是 install-only，不把这次数据库迁移扩张成自动升级器。
- 权威安装器固定 Control Plane Windows Service ID 为 `winsw-recovery-control-plane`；v4 超限恢复流程可以据此明确先停服务、带 WAL/SHM 一致备份、保留旧库，再以新 DB 重建 CP 配置，且必须明确 MVP 不支持原地删历史修库。
- 容量并发测试已正式固化：两个独立 Store 通过 SQLite `BEGIN IMMEDIATE` 三轮争抢最后一个名额，每轮均恰好一项成功、一项 `422`，最终 Agent/服务各一行。结合 OpenAPI/UI 共 15 项通过，运维手册也已给出停止 CP、带外一致备份、保留旧库、新库重建与安全停止回滚路径；Phase 1R 的容量与 Web 两个检查项可关闭。
- v5 后首次全量回归为 313 passed/1 failed；唯一失败是既有状态域测试仍硬编码 `schema_versions=4`，产品行为实际正确为5。该断言属于随显式新 migration 必须同步的测试基线，不应回退产品版本。
- 版本断言已精确分流：正常当前库必须为5，三类超限 v4 migration 失败后必须仍为4；两项合计4个用例通过，原子回滚合同未被测试修复掩盖。
- 稳定工作树完整回归现为 314 passed、4 个既有上游弃用 warning、54.16 秒；Phase 1R 代码/合同修改没有剩余测试失败，下一门槛是静态/依赖检查与独立终审。
- Phase 1R 静态与依赖门禁通过：`compileall`、`git diff --check`、`pip check` 均成功；仅有既存 Git LF→CRLF 提示，不是 whitespace error。等待独立终审后可回读计划并重建全新 Frozen 候选。
- 构建脚本只接受仓库直属、名称匹配 `dist-*` 的相对目录，并在同一发布树中生成三个 onedir、脚本/示例/文档与 SHA256SUMS；为避免把旧候选误当新证据，本轮将使用新的 `dist-recovery-phase1r-20260717`，不覆盖任何既有 `dist-recovery*`。
- Phase 1R 独立终审在重建前拦住 1 个 P1：真实 `AgentClient` 会先用 `Operation.model_validate()` 解析 POST/GET，缺字段或非法 UUID 的 ValidationError 被 RecoveryEngine 通用异常路径折叠为普通 UNKNOWN、`failure_code=None`；安全上零 probe/下游但违反 RUN-13 对“缺失/非法/错绑”统一 `AGENT_PROTOCOL_MISMATCH` 的稳定分类。Frozen 重建暂停，需引入类型化协议解析异常并用真实 HTTP client 集成测试闭环。
- 修复边界应只把“HTTP 202/200 已成功但 Operation JSON 无法通过冻结 Schema”转成类型化协议错误；网络失败、非成功 HTTP 的 `AgentClientError` 与暂时不可达仍走现有轮询/UNKNOWN语义。RecoveryEngine 在 POST/GET 捕获该协议错误后立即复用 mismatch 标记，消息必须稳定且不得包含响应正文或 Pydantic 细节。
- 现有测试仅用宽松 FakeAgentClient 覆盖“Schema可解析但语义错绑”，真实 AgentClient 测试只覆盖重试键；新集成用例必须把 FakeStore 的 endpoint 改为合法无路径 IP-literal URL，再以 `httpx.MockTransport` 分别返回 POST 缺字段与 GET 非法 UUID，才能同时证明真实解析层和 RecoveryEngine 稳定分类。
- 非冻结阻塞的后续加固已记录：手工代理 `GET /api/v1/operations/{id}` 当前按请求 ID 找路由，但未在保存前显式要求 Agent 返回 `operation_id` 等于路径 ID，合法但错 ID 的响应可能改写 dispatch 指向。它不影响 RecoveryRun（RUN-13 已另行绑定），但应在 Phase 2 前决定是否补成 `AGENT_PROTOCOL_MISMATCH`，避免审计查询身份漂移。
- P1 修复已落地：`AgentOperationProtocolError` 是脱敏的 `AgentClientError` 子类，只包装成功 HTTP 的 Operation JSON/Schema 失败；RecoveryEngine 在 POST/GET 明确捕获并立即使用 `response_schema` mismatch 隔离。真实 MockTransport 集成覆盖缺字段与非法 UUID、POST 不保存 operation_id、GET 保留原 ID、单次请求、零 probe、canary 不泄漏；子任务定向31项通过，待 root 稳定复跑。
- Root 联合复跑真实 AgentClient、RecoveryEngine 与 CP App 共 41 项全部通过；畸形 Operation P1 检查项关闭，下一步只读复审该修复后再重跑全量，不直接沿用修复前的314项证据。
- 畸形 Operation 修复后的稳定完整回归为 321 passed、4个既有上游弃用warning、48.02秒；此前314项基线已被这份更新证据取代，等待独立复审与静态门禁。
- 最终源码静态/依赖门禁再次通过：compileall、diff-check、pip check 全绿，仅既有LF→CRLF提示。若独立复审返回PASS，当前工作树具备重新构建资格。
- 发现原 P1 的独立审计者已复审修复并给出 PASS（P0=0、P1=0）：脱敏、POST/GET立即隔离、普通HTTP/网络语义、合同RUN-13均一致；定向12项通过。当前工作树满足重新构建前置门槛。
- 全新 `dist-recovery-phase1r-20260717` 已从321项全绿工作树成功构建，exit 0、69.9秒，包含三个 onedir；仅出现既知 `tzdata` hidden import warning。构建成功不等于冻结，仍须依次通过包内完整性、Frozen smoke、示例负向、evidence负向、故障harness、新鲜度与清理门禁。
- 新候选包内 distribution verifier 返回 PASS：331 expected/331 actual，missing/extra/hash mismatch 全0，side_effects=NONE；清单自身仍按合同为 `OUT_OF_BAND_REQUIRED`，需另算带外SHA-256。
- 包内 Frozen smoke 实际启动新 Agent/CP 二进制并通过：health均ok、LAB_HTTP、CP见1个Agent、EventLog映射INSTALLED/ACTIVE/AUTOSTART_ENABLED、Dashboard 200、Group 0；脚本明确未调用action或Run，SideEffects=NONE。
- 新包未编辑 Agent/CP example 均按权威 `--config PATH --check-config` 返回 exit2、`config_valid=false`、稳定脱敏错误，且输出不含usage；证明配置 sentinel fail closed，不是参数语法假阳性。
- 新包 evidence validator 对空白模板返回 exit1、verdict=FAIL、16项问题；未填写模板不能伪造成三机/十轮验收成功。

- 2026-07-17 continuation catch-up found no missing implementation result beyond the visible continuation messages; all prior decisions remain represented in the planning files. The authoritative next action is to inspect the partially completed model/OpenAPI work and run stable full regression after all subagent edits have landed.
- The model/OpenAPI capacity edits have landed far enough for the full suite to collect 197 tests. The three failures all originate from legacy test fixtures with `services=[]`; the new contract and AgentConfig require at least one managed service, so fixture repair—not schema relaxation—is the aligned fix.
- `HeartbeatReporter._report()` validates the callback result through `AgentReport`; an invalid empty list terminates its background task before ingress, explaining the tests' timeout. A shared valid `ObservedService` fixture will exercise reporter sequencing rather than schema failure.
- The repaired reporter/source-binding fixtures pass 3/3. This retains the stronger invariant that an Agent cannot register an empty allowlist while restoring the intended sequencing and endpoint peer tests.
- The converged worktree passes all 197 automated tests. This proves the current automated scope only; Phase 1 still requires a requirement-by-requirement traceability audit and a fresh frozen build/smoke, while real three-machine WIN evidence remains Phase 2+ work requiring target inventory.

- session catch-up 检测到 6 条未同步上下文，并明确要求读取规划文件、运行 `git diff --stat` 后继续。
- 当前规划原先直接从“候选基线”进入部署输入收集，遗漏了活动目标要求的逐项编码完成度审计。
- 已把“方案到实现完成度审计与缺口编码”设为新的当前 Phase 1；现场部署阶段整体后移。
- 129 项既有测试、发布清单和烟雾测试仍是有效证据，但只能证明其覆盖范围，不能外推到未逐项核对的合同要求。
- `git diff --stat` 只统计到 10 个已跟踪文档/依赖文件；Recovery MVP 的 `orchestrator/`、合同、API、脚本和测试当前全部是未跟踪内容，因此审计必须直接读取工作树，不能依赖 Git diff 判断实现规模。
- 当前实现文件覆盖 Agent、Control Plane、acceptance、common、4 个 PowerShell/2 个 Python 脚本以及 20 个测试模块；具备做并行领域审计的清晰边界。
- 冻结合同的高风险编码窗口集中在四处：Agent Operation 的 PREPARED/DISPATCHING 崩溃收敛、CP dispatch key 已持久化但 operation_id 未保存的恢复、epoch/120 秒连续稳定窗口、以及旧 Agent instance fencing/endpoint peer 绑定。
- 追踪矩阵明确要求 CT/UT/IT/E2E 自动化场景全部通过；仅有真实 Windows/断电类 `WIN` 场景可以保留到现场阶段，不能把合同中标成 IT/E2E 的缺口推迟到部署。
- 数据层必须持久化 Operation、Run/Step、图/probe 快照、服务占用和每次 probe attempt；恢复与证据链不能只依赖内存状态或页面展示。
- 源码未出现 TODO/FIXME/`NotImplementedError`；搜索到的裸 `pass` 位于异常类型或清理/停止容错分支，不能据此判定功能空缺。
- PREPARED/DISPATCHING、instance_generation、candidate settle、dispatch idempotency key、service run locks 和 probe attempts 在实现与测试中均有实体引用；下一步必须验证其语义与原子边界，而不是只检查名称存在。
- 当前工作树重新执行全量测试仍为 `129 passed`；4 条警告均来自 jsonschema/OpenAPI 校验器的弃用提示。
- 环境已安装 Coverage.py 7.15.2，可用于发现未被既有测试触达的核心分支；覆盖率只能作为审计线索，不能替代合同场景验证。
- 全量测试的 orchestrator 行覆盖率为 80%；主要盲区是 Agent SCM 50%、Agent probes 65%、Agent operations 69%、CP agent client 56%、CP app 62%、RecoveryEngine 78%、CP store 79%。
- `__main__.py` 显示 0% 是因为 CLI 测试通过子进程执行，不代表未测试；相反，RecoveryEngine/Store 的未覆盖行直接落在调度、对账、状态更新和错误分支，应优先与追踪矩阵逐项核对。
- Windows SCM 的低覆盖有一部分只能由 WIN 测试补足，但状态映射、超时、错误类型与动作矩阵仍应通过平台无关 fake/mock 测试证明。
- Agent 并行审计已动态复现 P0 探针 TOCTOU：本机 IP 校验发生在事件循环，实际 socket/http 连接在线程中稍后执行；若地址在两者之间被移除，仍会连接已不再属于本机的目标。
- Agent `local_addresses` 在 psutil 不可用时退化到 hostname DNS 解析，不符合“实际绑定 IP + fail closed”的安全边界。
- Agent 动作端点没有严格空正文模型，会忽略 `{"cmd":"whoami"}` 等额外字段并创建 Operation，而追踪矩阵 ACT-01 要求额外字段 422 且零 Operation。
- 404/405 等框架错误当前返回 FastAPI 默认 `{"detail":...}`，没有统一为稳定 ErrorResponse；合同错误码列表是否应补 `ROUTE_NOT_FOUND/METHOD_NOT_ALLOWED/INTERNAL_ERROR` 需先对照 OpenAPI 决策。
- CP 注册/心跳的 `ingress_peer()` 确实先执行 Bearer 常量时间校验和 socket peer CIDR 校验，认证实现存在；现有测试仍缺少无 Token、错误 Token 和 XFF 绕过的显式证明。
- 通用错误处理器只覆盖 `ApiError` 和 `RequestValidationError`；框架 `HTTPException`（含未知路由/方法）与未捕获异常没有固定 ErrorResponse，确认是跨 Agent/CP 的合同缺口。
- 冻结 ErrorCode 枚举未定义未知路由、方法不允许和内部错误代码；若只改异常处理器会导致 OpenAPI/实现漂移。修复顺序必须是先在合同、追踪矩阵和两份 OpenAPI 增加 `ROUTE_NOT_FOUND/METHOD_NOT_ALLOWED/INTERNAL_ERROR`，再实现全局处理器与契约测试。
- ACT-01 明确要求 cmd/PowerShell/file 等额外字段返回 `422 VALIDATION_ERROR`；因此动作 API 需要一个 `extra=forbid` 的可选空正文 Schema，不能继续依赖“路由没有 body 参数”。
- 两份 OpenAPI 的动作端点目前都没有 `requestBody`；Agent 描述还明确写“无正文”。这与 ACT-01 的额外字段 422 验收相冲突，必须先把线上合同改为“正文可省略；若提供只能是空对象”。
- 公共 `StrictModel(extra=forbid)` 可直接承载新的空动作正文模型，Agent 与 CP 共享同一 Schema/实现边界。
- `ServiceSlug` 线上权威要求首字符 `[a-z]`，公共模型/AgentConfig 当前允许 `[0-9]` 开头，已动态复现 `1mysql` 被接受；需收紧公共 regex 并补跨配置/OpenAPI测试。
- HTTP probe 对 URL 端口 `:0` 或显式空端口使用 `parsed.port or 80`，会静默改投本机 80；应把缺省端口与非法/空端口分开处理并拒绝后两者。
- CP SQLite v1 对 Group/Run/Step 状态列缺少 CHECK，store 更新方法接受任意字符串，也允许终态 Step 回退活动态；这是 RUN-10/GRP-02 的明确持久化合同违例，需要迁移或等效数据库约束加转换守卫。
- CP 模型/OpenAPI 出现多处边界漂移：group 名称/描述、manual reason 长度、probe interval 类型、集合 maxItems、display_name nullable、offline_after_seconds 是否固定 45；必须逐项选择线上权威并同步实现/配置/示例。
- CP 的 online lease 与 120 秒 settle 目前都用 UTC 墙钟比较；NTP/人工时钟前跳可能在真实不足120秒时创建 AUTO Run，违反合同 monotonic 要求。正确方向是进程内维护 monotonic 心跳/候选起点，候选变化或掉线清零；CP 重启保守要求新心跳并重置未完成 settle。
- Run Detail 的 probe attempts 只显示序号、结果码和耗时，未显示 started_at/finished_at，违反“每次 attempt 时间可见”。
- CP 在 AUTO preflight 漂移时只持久化 `BLOCKED_PRECONDITION` 状态，具体原因随 scheduler 返回值丢失；API/UI 无 blocked reasons，违反 ARM-03“显示原因”。
- Run Detail 同时缺 Step/attempt 的完整时间、dependency_chain、可读根因映射；root_cause_step_id 只是孤立 UUID，无法满足 ACC-05 可解释性。
- UI/API 没有 Run 列表或 group last-run 引用，后台 AUTO Run 只能在已知 run_id 时查询；这也使十轮验收无法从公开系统发现全部 Run 或证明窗口导出完整。
- Agent probe 的 `timeout_seconds` 当前只是每次 socket I/O inactivity timeout，不是单次 probe 总期限；slowloris 每80ms滴一字节可让 timeout=0.1 的请求在0.327秒后仍成功，存在无限拖延/线程堆积风险。
- Agent stop 语义与冻结动作矩阵相反：矩阵把 FAILED stop 视为 no-op SUCCEEDED，但实时等待和 DISPATCHING 恢复只把 INACTIVE 当目标，现有测试甚至锁死 FAILED→UNKNOWN。按当前权威，应把 stop 目标集合统一为 `{INACTIVE, FAILED}`；若产品要区分，必须先改合同而不能让测试覆盖合同。
- WinSW 安装器在复制包、写 XML、收敛 ACL、install/start 的多步变更失败后没有回滚；失败会留下已注册/运行服务，而 install-only existing-service 门禁又阻止直接重试，运维手册也缺回滚流程。
- 示例 Agent/CP 的 `CHANGE-ME` Token 和 session secret 都满足当前纯长度校验，固定管理员哈希也有效，因此 `--check-config` 会放行未替换示例秘密。
- CP `agent_source_cidrs` 默认是 IPv4/IPv6 全网 `/0`；Agent 来源列表也允许 `/0`，与受控单 CP 来源边界冲突。正确最小边界：CP 端 Agent 来源列表必填且拒绝 `/0`，Agent 端 CP 来源只接受显式 `/32` 或 `/128` host prefix。
- CP 正常配置加载没有像 Agent 一样脱敏包装 JSON/Pydantic 错误，可能把 session secret 的 `input_value` 写入 WinSW stderr 日志。
- 源码秘密生成器没有复制进发布包，而且依赖 Python；目标机合同是只接收 Frozen 包。最小可用方案是在冻结 CP CLI 增加交互式 `--generate-secrets`，源码脚本仅保留开发等价入口。
- CP CLI 当前把 `--config` 设为无条件 required；要支持冻结 `--generate-secrets`，必须把 config 改为条件必填，生成分支在任何配置加载/App/DB 前返回。
- 现有 config-check/BOM/打包测试直接把 example 当合法配置；示例改为故意无效 sentinel 后，测试应先在临时副本中注入测试秘密，另新增“原始示例必须失败”的安全回归。
- Agent Operation 还有一个更高风险的跨重启目标漂移：Operation 持久化 `windows_service_name=MySQL80` 后，若同一 local id 的 allowlist 改映射为 `Spooler`，PREPARED/DISPATCHING 恢复会使用新目标查询或执行，但 Operation 仍显示旧目标；已动态复现可能实际启动错误的 Windows Service。
- 对目标漂移的确定性策略：PREPARED 尚无副作用，可用新的稳定码 `SERVICE_MAPPING_CHANGED` 失败结束且零 SCM；DISPATCHING 已进入不确定窗口，映射变化必须直接 UNKNOWN，绝不能查询或操作新目标；仅大小写差异按 Windows 服务名不敏感比较视为同一目标。
- 仅在 Operation 恢复时核对目标仍不够：`(agent_id, local_service_id)` 是 CP 的 managed identity，若 Agent 允许同一 local id 静默改绑，后续新 Run 也会把旧 managed service 指向新 Windows Service。需要把 local id→Windows service 绑定持久化，并在 Agent 启动时拒绝历史 local id 改绑；运维若要换目标必须使用新的 local id。
- 追踪矩阵 SVC-02 只写了 managed UUID 稳定，没有明确 local id 对 Windows service 的历史不可改绑；这是冻结合同遗漏，需文档先补 SVC-05（持久绑定/大小写不敏感/改绑拒绝）及 `SERVICE_MAPPING_CHANGED`。
- 两份 OpenAPI 的 ServiceSlug pattern 一致为 `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`；实现应直接采用该权威表达式，不另造近似 regex。
- CP SessionMiddleware 已固定 `SameSite=strict` 且实验 HTTP 下 `https_only=false`；管理写路由普遍依赖 `require_admin_write`，但 logout 的 CSRF 使用普通字符串比较而不是常量时间比较，需判断是否统一加固。
- 代码库目前只有离线 evidence validator，没有从 CP/Agent SQLite 或 API 自动导出验收 bundle 的实现；运维手册要求操作者手工“汇总为本地 JSON”。是否属于冻结合同的编码阻塞需结合证据合同复核，但它至少是十轮验收的高风险可操作性缺口。
- 测试名称盘点显示已覆盖核心 happy/failure 路径，但追踪矩阵中明确命名的若干自动化场景没有对应的显式测试名称，例如真正并发的同 key 请求、迁移失败原子回滚、短锁 busy_timeout、cluster token 调全部管理 API、CORS 预检以及多个 CP 崩溃注入点；需检查是否被其他测试间接覆盖，间接证据不足时补测。
- `SQLiteDatabase` 的迁移实现按版本逐个 `BEGIN IMMEDIATE`、失败回滚并拒绝高版本，设计方向符合合同；现有测试只覆盖单次迁移和高版本拒绝，未证明多步顺序、迁移中途原子回滚、外键失败或 busy_timeout 等待。
- `SQLiteDatabase` 原先接受 `busy_timeout_ms=0/负数`；现已在公共边界强制正数，并用测试证明默认 PRAGMA=5000、顺序迁移、失败 DDL/version 原子回滚及外键约束。
- 严格空动作正文、字母开头 ServiceSlug、统一 404/405/500 ErrorResponse 已完成首轮实现；Agent 非空正文在路由校验阶段返回 422 且 operations 表仍为0，CP 非空正文不调用 Agent。
- 配置安全修复的稳定测试结果为 16 passed：原始 example 中 `REQUIRED` sentinel 必须失败，临时注入强秘密后通过；Agent CP 来源仅允许 `/32`/`/128`，CP 正常启动配置错误只输出脱敏消息，`--generate-secrets` 不加载数据库或应用。
- RecoveryRun 当前只有按已知 `run_id` 查询的 API，Dashboard/恢复组页也没有最近 Run 入口；因此后台 Scheduler 创建的 AUTO Run 无法通过公开接口发现，这是验收证据链的独立 P0 缺口。
- RecoveryRun 列表合同采用稳定 keyset 分页：`(created_at, run_id)` 倒序，游标绑定 `group_id/trigger/status` 过滤条件；格式损坏、版本/字段非法或跨过滤条件复用游标统一返回脱敏的 `422 VALIDATION_ERROR`。
- 当前 `get_run()` 在单个只读连接中装配 Run/Step/attempt；列表实现应复用同一连接的装配 helper，避免 N+1 连接和列表期间删除造成的短页。分页以 `limit + 1` 判定 `next_cursor`，游标边界必须严格使用 `< (created_at, run_id)`，确保时间戳相同仍无重复/遗漏。
- Dashboard 当前只并发加载 Agent 与 service；最小改动可并发加入最近 10 个 Run，并链接既有 `/runs/{run_id}`，不新增第四个页面，仍符合三页 UI 边界。
- 实现已把 Run 装配提取为单连接 helper，并新增 `limit + 1` 的过滤绑定 keyset 查询；没有修改 RecoveryEngine、AUTO candidate 或租约逻辑，可与并行 monotonic 修复保持边界分离。
- 并行 monotonic 修复后 Scheduler 只有在同一 candidate 的 wall-clock `READY` 且进程内 monotonic 也走满 settle window 时才创建 AUTO Run；RUN-12 集成测试必须推进两种时钟并调用真实 `scan_auto_groups()`，不能直接向数据库插入 AUTO 行来冒充 Scheduler 证据。
- 分页测试可以在同一固定 UTC 毫秒时间创建并终结多条 MANUAL Run：终态事务释放服务锁，下一条可复用同组；这能直接验证 `run_id DESC` 的确定性 tie-break，而不是依赖不同时间戳碰巧有序。
- Scheduler 发现测试可令服务已 ACTIVE，并由 fake Agent 返回成功 SCM readiness；真实 Engine 仍会创建并执行 AUTO Run，而列表查询本身不接收/依赖该 run_id，可证明公开发现链路。
- 新增验收已通过：固定同一 `created_at` 的 8 条 Run 以 limit=3 遍历时严格按 run_id 倒序且零重复/遗漏；过滤、跨过滤游标、损坏游标、管理员/cluster token 边界、Dashboard 链接和真实 Scheduler AUTO 发现均通过。
- 与并行 Store monotonic lease 修复对齐后，相关合同明确 `received_at` 仅为审计时间：CP 重启后 persisted Agent 一律先 OFFLINE，必须由当前 instance 的新有效 sequence 重建进程内 lease；这不会改变 Run 列表的持久可发现性。
- 收尾源码复核确认 API 静态集合路由位于动态 `{run_id}` 详情路由之前，Dashboard 复用既有表格/徽章与详情页，不新增静态资源、JS 写操作或第四个页面；AUTO/MANUAL 触发徽章使用默认中性色，Run 状态继续使用既有状态色。
- Run Detail 当前只显示 Run 状态/触发/epoch/失败摘要；缺少 Run 四类时间，Step 只显示孤立 root UUID，probe attempt 只有 code/latency/message，所有 Step/attempt 时间与 dependency_chain 均未呈现。
- 当前 `/runs/{run_id}` 已把完整持久化 Run 字典交给 Jinja，可在模板内基于 `members_snapshot` 和 `steps` 解析服务显示名、根因 Step 与依赖链；无需扩大 App、模型、Store 或 RecoveryEngine 修改范围。
- `dependency_chain` 的持久语义是按根因到当前 Step 直接先决节点的顺序保存 Step ID：直接阻塞为 `[根因]`，多级传播为 `[根因, ..., 直接上游]`；模板必须保持该数组顺序并逐项解析显示名/local id/status。
- Run Detail 模板现已为 Run/Step/Probe 三层真实时间统一使用 `<time>`；任一空值显示“未发生”。FAILED/UNKNOWN 无显式 root ID 时明确标识“本步骤”为根因，BLOCKED 则解析并链接根 Step，避免孤立 UUID。
- 专门 UI 夹具在同一 Run 中覆盖 READY/FAILED/BLOCKED/UNKNOWN，并验证 BLOCKED 的多项 dependency chain 保持原顺序；恶意 run reason、failure message、member display name、step message、probe message 均由 Jinja 自动转义。
- CP proxy action 已在 Agent 调用前事务化写 `proxy_dispatches`；同 key replay 优先读取该记录，已保存 operation_id 时直接解析持久结果。现有测试只覆盖普通重复请求，尚未故障注入 prepare/Agent创建但响应丢失/save后三个崩溃边界。
- `prepare_proxy_action` 在首次请求中持久化 managed_service_id、agent_id、local_service_id、endpoint、action 与 fingerprint；重试在检查可变 lease/镜像/锁之前返回原记录，为“镜像漂移不得换目标”提供了实现基础，需用 App 重建集成测试锁定。
- Evidence v1 明确把验收包定义为“操作者汇总 + 离线只读校验”，并承认部分现场事实无法从当前 API 独立证明；因此自动 evidence exporter 是高价值后续增强，但不是当前冻结编码的 P0。当前先用公开 Run 列表消除 AUTO Run 不可发现这一硬阻塞，再评估半自动采集器。
- CP 状态完整性不能只依赖 Python 枚举：当前数据库 v1 的 run/step/group 状态列无 CHECK，且 `update_run`/`update_step` 接受任意字符串；修复需要数据库迁移约束或等效触发器，加上应用层合法转换守卫，不能仅补输入类型注解。
- RecoveryEngine 虽已注入 `time.monotonic`，它只用于 Operation/探针 deadline；AUTO candidate 的生产 Store 仍完全按持久化 wall clock 决定 READY。最小安全修复是在 Engine 为每个 `(group_id, candidate_epoch)` 维护进程内 monotonic 连续窗口：SETTLING 首见开始，掉线/候选变化/非候选状态清零，Store 即使因墙钟前跳提前报 READY 也必须等 monotonic 满窗；CP 重启后 tracker 为空则保守重等完整窗口。
- 上述 monotonic guard 不能把正常窗口加倍：必须在 Store 返回 SETTLING 时就记录 epoch，而不是首次 READY 才起计时；Store 决策仍负责跨进程持久候选和原子 preflight，Engine guard 只提供同进程不可被墙钟跳变提前的第二道安全门。
- CP lease 的安全实现应把持久 `last_received_at` 与进程内 freshness 分离：Store 对每次“已接受且非乱序”的 report 记录 monotonic tick；所有列表、preflight、缺节点和代理动作只以 tick 的 `<45s` 判断在线。CP 重启后 tick 表为空，所有旧 Agent 保守离线，直到新的有效 heartbeat；重复/乱序请求不得续租。
- 当前 `_write_accepted_report` 用 wall clock 判断“之前租约是否过期”并决定是否清 settle。改为在接受报告前查询 monotonic lease 是否仍有效，可同时修复 NTP 跳变和 CP 重启后首次有效心跳必须重置 settle 的问题。
- AUTO settle guard 在 monotonic 满窗后应允许同一候选从 Store 的 `SETTLING` 或 `READY` 进入原子 Run 创建；否则 wall clock 后跳会无限延长窗口。Run 创建仍重新校验当前 epoch、在线/安装/Manual/无环和唯一约束，不能绕过 Store preflight。
- RecoveryEngine monotonic guard 已实现并通过 13 项测试，包括 wall clock 前跳一天仍零 Run、后跳两天不延长已完成窗口、CP 进程重启后必须重新连续观察完整 120 秒。剩余时钟缺口是 Store 在线租约仍需从 wall cutoff 切换到 accepted-heartbeat monotonic tick。
- `MonotonicLeaseRegistry` 已作为独立、线程安全、进程内 freshness 组件实现：启动无 tick 即离线、精确 45 秒边界失效、异常 tick 后退 fail closed、新 accepted report 可恢复；3 项单测通过，下一步接入 Store 全部 online-only 决策。
- Agent 修复分支自身 71 项回归全部通过；其全量 172 passed/1 failed 的唯一失败来自 Host Preflight 测试仍直接使用故意无效的 CP example。正确修复是测试/预检夹具在临时副本注入强秘密，不能恢复可投产的示例秘密。
- Host Preflight 夹具已按上述策略修正并 5/5 通过：原始 example 继续是不可启动 sentinel，成功场景只在 pytest 临时目录创建有效配置，且断言注入的 cluster/session secret 不出现在预检 JSON 或 stderr。
- Store lease 切换后，既有 CP 代理动作测试不能再通过篡改 SQLite `last_received_at` 模拟离线；这正是新边界要防止的行为。测试应注入可控 monotonic tick，先证明 wall timestamp 篡改不影响 freshness，再推进 tick 到 45 秒证明新请求被拒且已持久化幂等重试仍可解析。
- Store 的所有关键 liveness 消费点现已接入 `MonotonicLeaseRegistry`：Agent/Service 列表、required agents、missing nodes、arm/preflight、candidate settle、Run 创建和新 proxy action。持久 `last_received_at` 只做证据；CP 重启后旧记录离线，重复/乱序 report 不续租，新有效 sequence 才恢复在线。
- 相关 15 项 Store/Engine/三机集成测试和 1 项代理动作测试通过；测试还证明把数据库时间篡改到 2000 年不会伪造离线或影响同 key 重试，而 monotonic 精确到 45 秒后新 key 才稳定返回 `AGENT_OFFLINE`。
- `scheduler_loop` 当前对 `list_groups`/Store/意外实现错误零隔离，任何一次非 ApiError 都会让唯一后台 scheduler task 永久结束；App 既不监督重启也不公开任务健康。这是自动恢复静默失效风险，需让 loop 对可恢复异常做限速重试并记录日志，`CancelledError` 仍立即传播。
- `launch_run` 的 done callback 只从 `_tasks` 删除任务而不读取异常；如果 execute_run 有未收敛异常，会产生不可观测的 task failure，且同进程不再自动 resume。后续应至少消费并记录异常，同时决定是否把 Run 确定性标记 UNKNOWN 或由 supervisor 重新对账。
- Scheduler 已改为每轮先从持久化重新发现未完成 Run，再扫描 AUTO groups；意外异常会记录堆栈并以 1–60 秒指数退避继续，取消立即传播。Run task done callback 会消费并记录异常，使下一轮可按原 run_id/dispatch key 安全对账而非从头重放。
- Scheduler/Run task 新增 3 个回归后 RecoveryEngine 共 16 项通过：瞬时失败后第二轮恢复、失败任务被观测且同 run_id 可重新拉起、非正扫描间隔被拒绝。
- CP 数据库状态约束可用兼容 SQLite 的 v2 triggers 实现，避免高风险重建多张有关联的表：迁移先扫描现有非法 state/status/trigger 并原子失败，再为 INSERT/UPDATE 建白名单 trigger；另建 terminal immutability trigger，禁止 Run/Step 从终态变更为不同状态。应用层仍要显式 Enum 校验和终态转换守卫，提供比 SQLite Abort 更稳定的错误。
- 当前调用面只使用合法枚举字符串，现有直接 `finish_run` 测试不会阻碍迁移；需新增“v1 含非法值时升级回滚并保留 schema version=1”、直接 SQL 非法写、终态回退和同终态幂等测试。
- Run 列表子任务已把 cursor helpers 和 list/get Run 区域落入 Store，并明确停止修改 migration/状态更新段；v2 状态迁移现在可在不覆盖并行改动的前提下实施。
- CP Store v2 已加入服务/组/Run/Step/proxy action 域值 triggers，以及 Run/Step 终态不可变和 RUNNING→PENDING 禁止 trigger；`update_run`/`finish_run`/`update_step` 同步做枚举与转换守卫。还需用升级失败原子性和直接 SQL 绕过测试验证。
- CP Store v2 的升级/绕过测试已通过：schema version=2；应用非法枚举为 ValueError，直接 SQL 为 IntegrityError；Run/Step 终态回退被双层拒绝；含非法 group state 的 v1 升级失败后 version 仍为1且零残留 trigger。
- CP Store v3 已加入 `blocked_reasons_json`：迁移为旧 BLOCKED 记录写明确 `LEGACY_REASON_UNAVAILABLE`，trigger 强制 BLOCKED 至少一项、其他状态必须空；AUTO preflight 两条路径原子保存具体 issues，disarm/re-arm/正常调度清空，Group read 返回结构化 reasons。需要同步现有手工 BLOCKED 测试和 schema version 断言后验证。
- Store v3 定向 12 项已通过：Automatic 漂移会持久化包含 agent/service/code/message 的 `STARTUP_NOT_MANUAL`，重建 Store 与修复外部条件都不会解除隔离，只有 disarm 清空原因；直接把组改 BLOCKED 却不给原因被 trigger 拒绝。
- 旧 schema v2 中已经 BLOCKED 但历史原因不可恢复的组，v3 会显式写入 `LEGACY_REASON_UNAVAILABLE` 而非伪造业务根因；新增升级测试后 Store 共 13 passed。
- 管理 API 写入已用 `hmac.compare_digest` 校验 CSRF，但 HTML `/logout` 仍用普通字符串 `!=`；应复用同一个常量时间 helper，避免两个管理入口形成不同安全语义，并补未登录/错误/正确 token 的会话测试。
- Logout 与管理 API 现统一复用 `csrf_matches()` 常量时间校验；测试证明未登录/错误 token 为固定 403 且不会清会话，正确 token 清会话并 303 到 login。
- Group UI 现对 `BLOCKED_PRECONDITION` 显示持久 code/message/Agent/Service，并明确“disarm→修复→重新 arm、不会自动解除”；持久文本经过 Jinja autoescape。与 Run Detail 的全时间线/根因链专项联合 2 passed。
- Run Detail 已覆盖 Run/Step 四类时间、每次 probe 起止/observed/code/message/latency、BLOCKED 根因锚点与有序 dependency chain；缺失时间明确为“未发生”，FAILED/UNKNOWN 区分本步骤根因，不再只显示孤立 UUID。
- Python runtime 与开发依赖均使用精确版本，构建脚本强制 Python 3.13、PyInstaller 6.16.0 和 onedir；构建时在线 pip 安装只发生在发布构建机，不是服务器运行时下载，仍符合“目标机 Frozen 包离线部署”边界。
- Git 当前仍把整个 Recovery MVP 实现/合同/脚本/测试视为未跟踪文件；在最终交付前必须以文件级测试与发布清单为证据，不能依赖 `git diff --stat`（它只显示10个既有跟踪文件）。
- Run 列表、Store v2、monotonic scheduler/lease、CP API、OpenAPI 与三机集成联合回归为 42 passed；4 条警告均为 jsonschema/OpenAPI validator 上游弃用提示。
- Agent 并发审计复现并修复同服务 admission/观察未共用锁的真实竞态；采用短 SQLite admission + 服务锁内二次 admission，既保证运行中冲突快速 REJECTED，也保证同 key 单 Operation/单 SCM、同服务串行、不同服务并行。Agent 专项 76 passed，operations 并发连续5轮稳定。

## Planning Skill Findings

- 当前继续执行公开合同边界一致性审计：以 Recovery MVP 冻结合同为权威，逐项核对两份 OpenAPI、公共 Pydantic 模型、Agent/CP 配置及示例；未知字段、秘密、CIDR、monotonic 和 Run 列表边界不得放宽。
- 冻结合同已明确 probe 数值范围与默认值、离线阈值 45 秒、入口认证结果；但 group 文本长度、manual reason 长度及部分集合上限尚未全部写死，需采用最小 MVP 上限后先补合同和追踪矩阵再同步实现。
- `interval_seconds` 当前 Pydantic 为浮点数而 CP OpenAPI 为整数；合同只写秒数范围、未规定数值类型，需在重要决策前回读模型消费方式并选择不制造亚秒调度的最小整数合同。
- Readiness 消费路径最终以 float 传给 monotonic/sleep，但冻结值和所有测试均使用整秒 interval/deadline；只有单次 timeout 需要 0.1 秒精度。最小 MVP 冻结为 `timeout_seconds: number`、`interval_seconds: integer`、`deadline_seconds: integer`，并保持 deadline >= timeout。
- 新增要求：`RecoveryGroup.blocked_reasons` 必须是最多 100 项的严格 `PreconditionIssue[]`；`BLOCKED_PRECONDITION` 至少一项，DISARMED/正常状态为空。Store v3 由主任务并行实现，本专项只负责文档、OpenAPI、公共模型及其边界测试。
- 已确认当前主要线上漂移：group 模型为 name 120/description 1000，而 CP OpenAPI 为 128/1024；ManualRun reason 为 500 对 512；readiness interval 为 float 对 integer；ObservedService/RecoveryMember 的 `display_name` 模型必填非空字符串而 OpenAPI 允许 null 且缺 minLength。
- `display_name` 的运行时和存储权威均为非空：Agent 在 SCM 无 display name 或服务未安装时回退到配置 display name/Windows service name，CP SQLite 列为 `TEXT NOT NULL`。因此应修正两份 OpenAPI 为非 null、1..256，并给 RecoveryMember 同样边界，而不是放宽模型/数据库。
- 多个公共集合模型没有长度上限，虽 OpenAPI 部分已有 1024/16384/100 上限；需建立统一的最小 MVP 容量表并确保所有公开数组都有 `maxItems`/Pydantic `max_length`，同时保持唯一性约束。
- 递归盘点确认 Agent OpenAPI 只有 capabilities 的固定 3 项和 services 1024 已设上限；CP OpenAPI 中 Agent/ManagedService/Group/Step/Run 多数返回数组仍无上限。Pydantic 除 RecoveryRun 列表 100 外，几乎所有对应数组都无 `max_length`。
- 最小 MVP 容量应围绕单 Agent allowlist/单恢复组最多 1024 服务建立：服务/成员/步骤/探针/Agent服务摘要最多 1024；依赖边最多 16384；blocked reason、Run 列表最多 100。其余公开集合也需给出不低于单组可表达性的明确上限，避免无界响应和 schema 漂移。
- 对组/Run 结构采用同构上限：`missing_agent_ids/members/probes/steps/members_snapshot/probes_snapshot/dependency_chain` 最大 1024，`dependencies/dependencies_snapshot` 最大 16384；`probe_attempts` 由 deadline/最短 interval 理论上不超过 300，但为防持久历史兼容采用 1024；`warnings` 采用 100。Agent/Managed service/group 列表采用 1024 的 MVP 内存响应上限。
- CP register/heartbeat 实现会按真实 peer CIDR 返回 `403 SOURCE_IP_DENIED`，但当前两个 OpenAPI ingress 路径只声明 401/409/422；必须补 403 response 并加入 schema/端点回归。
- CP 已有可复用 `Forbidden` ErrorResponse，描述覆盖“来源策略拒绝”；register/heartbeat 只需显式声明 403，并把该响应示例改为/补充 `SOURCE_IP_DENIED`，不改变认证实现。
- `ControlPlaneConfig` 已把 `offline_after_seconds` 固定为 `Literal[45]`、cookie 固定为 `Literal["recovery_admin_session"]`，示例也一致；但公开 `AgentSummary.offline_after_seconds` 仍是无界 int，需要同步为固定 45。
- 重要边界决策已按“冻结文档优先、既有线上合同次之、单一规范表示”收敛：group `name` 采用 1..128、`description` 采用非 null 0..1024（空字符串表示清空），manual reason 采用可空 1..512；这些值沿用已发布 CP OpenAPI 的容量而修正模型中的 120/1000/500 漂移。
- Patch 字段“省略”与显式 JSON null 必须区分；显式 null 不应穿透到 NOT NULL 存储或形成第二种 description 表示。实现将保留省略语义但拒绝显式 null，未知字段继续 422。

- 本机技能路径：`C:\Users\maoliang\.codex\skills\planning-with-files\SKILL.md`。
- 技能版本为 3.5.1；核心文件为 `task_plan.md`、`findings.md` 和 `progress.md`。
- 项目中原先不存在根计划或 `.planning` 活动计划；session catch-up 未报告未同步上下文。
- 本任务采用根目录兼容模式；若未来并行开展另一复杂任务，应改用隔离计划目录。

## Deployment Inputs Still Required

| 类别 | 必需事实 |
|---|---|
| 管理节点 | 主机名、IP、Windows Server 版本、CP 监听端口 |
| 业务节点 | 至少三台主机名、IP、Windows Server 版本、Agent 监听端口 |
| 服务映射 | `service_id`、准确 SCM 服务名、所属主机、当前启动类型 |
| readiness | `scm`、本机 `tcp` 端口或本机 `http` URL |
| 依赖 | 下游依赖上游的明确边列表 |
| 网络 | Agent→CP 心跳、CP→Agent API 的防火墙规则及 CP 来源 IP |
| 执行 | WinRM、RDP，或现场人员按手册运行脚本 |
| 安全 | WinSW 未签名资产的 EDR 放行和受控分发方式 |

## Technical Decisions

| Decision | Rationale |
|---|---|
| 下一阶段只做目标清单和只读 PreInstall | 缺少真实主机信息时不应注册服务或扫描局域网 |
| 当前先做只读合同/代码审计并修复缺口 | 这是活动编码目标的一部分，不依赖目标机信息，也不会产生外部副作用 |
| 修复优先级为安全/错误目标与数据库不变量 → 公开合同一致性 → UI/覆盖证据 | 错误服务副作用、SSRF/探针绕过和非法持久状态的风险最高 |
| 现场部署必须使用 Frozen 模式 | Python 源码模式只能用于开发诊断，不能作为发布证据 |
| WinSW 必须按 lock 校验 | v2.12.0 二进制未签名，固定大小和 SHA-256 是 MVP 的最低可重复边界 |
| CP 必须位于独立管理节点 | 避免其 SQLite 或进程依赖被编排的 MySQL/业务服务 |
| 缺少任一必需节点时不执行部分子图 | 严格依赖恢复不能以局部启动换取表面可用 |

## Issues Encountered

| Issue | Resolution |
|---|---|
| `planning-with-files` 未出现在会话技能清单中 | 在本机技能目录找到并完整读取其 `SKILL.md` 后使用 |
| 技能定位命令返回 1，同时输出了有效路径 | 未把非零状态误判为“技能不存在”；改为直接读取明确路径 |

## Resources

- `D:\dev\py-dev\winsw_GUI\docs\recovery-mvp-operations.md`
- `D:\dev\py-dev\winsw_GUI\docs\contracts\recovery-mvp-v1.md`
- `D:\dev\py-dev\winsw_GUI\docs\contracts\recovery-mvp-evidence-v1.md`
- `D:\dev\py-dev\winsw_GUI\docs\contracts\recovery-mvp-traceability.md`
- `D:\dev\py-dev\winsw_GUI\dist-recovery\SHA256SUMS.txt`
- `D:\dev\py-dev\winsw_GUI\deployment\winsw-x64-v2.12.0.lock.json`

## Visual/Browser Findings

- 本轮尚未使用图像、PDF 或浏览器；无待转录的多模态信息。
- 2026-07-17 冻结包首次重建的 1 秒超时已终止原构建：进程检查仅命中检查命令自身，没有仍在运行的 `build_recovery_mvp.ps1`/PyInstaller 进程；`dist-recovery-20260717` 已生成半成品目录，后续由构建脚本在长超时下安全重建并以最终 manifest 校验，不把半成品视为交付物。
- 2026-07-17 CP 最终合同审计初步发现潜在 OpenAPI/公共模型漂移：`RecoveryGroup.description` 的 nullability、`blocked_reasons` 的 required 声明和若干 Group/Run 集合 `maxItems` 需要逐项核验；在精确审计与修复完成前，当前构建只能视为候选而非冻结包。
- 2026-07-17 `dist-recovery-20260717` 已从当前稳定工作树完整重建成功（exit 0，76.1 秒），包含 Agent、Control Plane、evidence validator 三个 PyInstaller onedir；PyInstaller 仅报告 `tzdata` hidden import 未找到，下一步必须先通过精确 manifest 校验与实际二进制烟雾，才能判断该 warning 是否无影响。
- 2026-07-17 CP 最终审计发现 P0 崩溃恢复错误：`RecoveryEngine._probe_until_ready` 在重启恢复时仅凭最后一个持久 probe attempt 的 `passed=true` 判 READY，未重新核对 attempt 完成时间与总 deadline；若 CP 在“迟到成功 attempt 已提交、Step 标 FAILED 前”崩溃，重启会错误放行下游。必须新增该精确崩溃窗测试并让恢复判定与正常路径使用同一 deadline 规则。
- 2026-07-17 CP 公开合同仍有冻结阻塞：Control Plane OpenAPI 缺 `RecoveryGroup.blocked_reasons`/`PreconditionIssue`，`description` 仍 nullable，12 个公开数组缺 `maxItems`；Groups UI 的 120/1000 限长也落后于冻结 128/1024。现有 OpenAPI 测试不足以捕获这些漂移。
- 2026-07-17 Store v3 的 `blocked_reasons_json` trigger 只检查 JSON 数组与状态空/非空，没有约束最多 100 项及 `PreconditionIssue` 项形状/UUID/唯一性；直接 Store 写入可制造公共模型无法读取的持久脏数据。追踪矩阵 GRP-05 要求 Schema/DB 均拒绝，必须补 DB 约束与迁移/回归。
- 2026-07-17 proxy dispatch 崩溃窗证据仍不完整：已有 RUN-04/08 只覆盖保存幂等键后、Agent 创建 Operation 前取消，尚需证明“Agent 已创建但响应丢失”和“operation_id 已持久化后 CP 崩溃”两个窗口不会盲重发。
- 2026-07-17 新构建尚不能通过实际 Frozen smoke：`scripts/smoke_recovery_binaries.ps1` 仍写 `session_cookie_name=recovery_smoke_session`，但 `ControlPlaneConfig` 已冻结为 Literal `recovery_admin_session`，CP 会在启动前拒绝配置。现有 smoke 测试仅做关键词静态断言，未校验脚本生成的配置能够被当前模型接受；必须修脚本并增强测试，然后重建。
- 2026-07-17 Store 约束修复必须新增 SQLite migration v4，不能直接改 v3：现有现场/候选数据库已经记录 schema version 3，修改旧 migration 不会为它们安装新 trigger。v4 应先验证全部既有 `blocked_reasons_json`，遇到脏数据原子拒绝升级，再替换 v3 trigger；测试需同时覆盖干净 v3→v4、脏 v3 原子失败及新 v4 直接写入拒绝。
- 2026-07-17 `PreconditionIssue` 的冻结 DB 形状可由 SQLite JSON1 trigger 完整约束：顶层 1..100（仅 BLOCKED）、每项 object 且键集合严格限于五个字段、code/message 类型和长度、两个可空 UUIDv4、`managed_service_ids` 数组 0..100 且元素为唯一 UUIDv4；其他状态仍强制 `[]`。公共模型已实现大部分同类约束，但 DB 是防止直接 Store 写入污染读路径的必要第二道边界。
- 2026-07-17 `SQLiteDatabase.initialize` 已逐版本使用 `BEGIN IMMEDIATE` 并在异常时 rollback，因此 v4 可以用“先全表校验、再 DROP/CREATE triggers”的方式保证升级原子性；`ControlPlaneStore` 当前只注册 v1..v3，必须显式加入 v4，schema version 断言同步改为 4。
- 2026-07-17 `PreconditionIssue.managed_service_ids` 在 API 模型中可省略并默认为空数组；若字段存在则必须是数组、最多 100、UUIDv4 唯一。两个单值 ID 可省略或显式 null。DB trigger 应保持同一语义，而不是强迫序列化所有可选字段。
- 2026-07-17 probe deadline 崩溃窗 P0 已修复：恢复路径使用持久化首个 attempt.started_at 与最后 passed attempt.finished_at 复核总 deadline；迟到、缺失或时间倒退证据 fail closed 为 FAILED。精确崩溃窗测试通过，Engine 17 passed，相关桥接/三机/UI 4 passed。
- 2026-07-17 Frozen smoke cookie 漂移已修复为 `recovery_admin_session`；测试现在提取脚本的实际赋值并通过公共 `ControlPlaneConfig.model_validate` 验证完整烟雾配置，相关 36 passed。此前构建已过期，所有 CP/Store/OpenAPI 修复完成后必须重新构建并实跑。
- 2026-07-17 Agent 最终审计发现六项冻结冲突：显式 action JSON `null` 被当省略并返回 202；probe 数字字段会接受字符串/bool/非严格数值；未知 probe kind 返回通用 VALIDATION_ERROR 而非 PROBE_UNSUPPORTED；stop 后观测 FAILED 被误判 SUCCEEDED；instance generation 与 instance_id 的提交顺序违反原子身份合同；原生 SCM query/start/stop 在线程中无硬截止，可在 action deadline 后无限 RUNNING。全部属于公开 API/动作正确性或可恢复性 P0，必须修复并新增负向/超时测试后才能再次冻结。
- 2026-07-17 Store 测试只有一处稳定库版本断言仍写 v3，另有 v2 原子迁移测试读取版本但不固定 v3；v4 回归应更新前者，并新增“脏 v3 升级 rollback 保持 version=3”以证明部署升级安全。
- 2026-07-17 CP Store v4 已实现并通过 23 项专项：完整严格验证持久 `PreconditionIssue[]`、替换 v3 trigger、用 no-op UPDATE 全表验证旧数据；脏 v3 升级会 rollback 并保持 schema version 3/原 triggers，干净库升级为 4。直接 DB 写入的未知键、空文本、非对象、非 UUIDv4、重复 ID、null 数组和 101 项均被拒绝。
- 2026-07-17 修复后的源码 smoke 脚本已实际驱动审计前 `dist-recovery-20260717` Agent/CP onedir 通过：health、LAB_HTTP、AgentCount=1、EventLog INSTALLED/ACTIVE、登录/Dashboard 200、RecoveryGroupCount=0、SideEffects=NONE；结束后端口 listener=0、临时目录残留=0。该结果证明当前二进制基本可运行，但包内 smoke 脚本仍旧，且之后还有代码修复，所以最终包必须重建后从包内脚本复跑。
- 2026-07-17 RUN-04 的关键实现集中在 `RecoveryEngine` 约 900 行：Step 先持久 `dispatch_idempotency_key`，Agent action 返回后再 `assign_step_operation`；Store 另有 proxy_dispatch journal。现有 FakeStore 测试覆盖 key 保存取消，但最终审计指出尚未精确覆盖“Agent 已创建 Operation、响应在 CP 侧丢失”与“operation_id 保存后崩溃只 GET”两个恢复窗口，后续需针对该路径补可计数客户端测试。
- 2026-07-17 RUN-04 可在现有 FakeEngine 层精确验证而无需改生产代码：共享一个按 idempotency key 保存 Operation/副作用计数的 Agent double，第一次在创建 Operation 后丢响应并模拟 CP 取消，第二次恢复用同 key POST 得到同 operation_id，副作用计数仍为 1；另在 FakeStore 的 `assign_step_operation` 持久写后注入取消，重启必须只调用 GET、action 调用为 0。两者共同闭合合同列出的第二、第三崩溃窗。
- 2026-07-17 CP 公开 Schema/UI 漂移已收口：OpenAPI 的 RecoveryGroup required 字段与公共模型字段全集一致并强制输出 blocked_reasons，description 只允许 0..1024 字符串；所有公开数组均有 1024/16384/100 冻结上限。Groups 同时提供新建和编辑元数据表单，二者均使用 name=128、description=1024，编辑通过既有 PATCH API 完成。
- 2026-07-17 RUN-04/08 两个缺失崩溃窗现已由 Engine 测试闭合：Agent 创建 Operation 后响应丢失时，CP 重启以持久 key 重发并取回同 ID，模拟副作用计数保持 1；operation_id 已持久后崩溃时，恢复只 GET 原 ID、action 调用为 0。`tests/test_recovery_engine.py` 当前 19 passed。
- 2026-07-17 当前 `git status --short` 仍显示 Recovery MVP 的 `orchestrator/`、`tests/`、`docs/api/`、`docs/contracts/` 等主体为未跟踪目录，且审计前 `dist-recovery-20260717/` 也未跟踪；因此 `git diff --check` 只能覆盖已跟踪文件，最终静态验证必须同时使用 `compileall`、测试执行、OpenAPI解析和显式全树文件检查，不能把空 diff 当作实现完整证据。
- 2026-07-17 Agent 六项 P0 已落地首版并完成 67 项专项：action 显式 null 拒绝且不建 Operation；probe 数值 strict、未知 kind 为 PROBE_UNSUPPORTED；stop 只以 INACTIVE 为达成目标；identity generation 与 instance UUID 同一 IMMEDIATE 事务且失败 rollback；所有 Operation/观察 query 有 deadline；慢 SCM 原生线程超时后 Operation 为 FAILED/SCM_ACTION_TIMEOUT，并对该服务隔离到晚返回，避免重叠副作用。仍需等待 Agent 全量/OpenAPI回归和最终剩余审计。
- 2026-07-17 发布 P1 首版已落地：PostInstall 逐 listener PID 验证并拒绝合法+冒充混合占用；WinSW source/staged/published 三处按 lock 重验 SHA/size/Authenticode，SCM install 前覆盖 final TOCTOU。故障 harness 已加入 staged/published 篡改后零 SCM、清残留、可重试断言，等待 PS5.1 与 pytest 结果。
- 2026-07-17 Store v4 对抗审查发现 SQLite/Pydantic Unicode 语义不等价：SQLite `length()` 在 U+0000 截断，可让超长 code 或 UUID 尾随 NUL 绕过，也会误拒模型当前接受的单 NUL；SQLite JSON1 还接受孤立 surrogate，而 Python/Pydantic 拒绝，导致脏 v3 可升级后在公开响应处 500。修复方向冻结为：产品合同/API模型显式禁止 NUL 与 surrogate；v4 迁移用 Python 级 `json.loads + PreconditionIssue` 验证历史行；trigger 显式拒绝 NUL，新增四类对抗测试。
- 2026-07-17 Unicode 安全实验确认：Python `json.loads` 会把合法 `\ud83d\ude00` 配对合并为单个 U+1F600，Pydantic 接受；孤立 `\ud800` 被拒绝；U+0000 原先被接受。v4 迁移现先用严格 duplicate-aware JSON+PreconditionIssue 验证并以 `ensure_ascii=false` 规范化，故合法配对会转成字面 Unicode，随后 DB trigger 可安全拒绝所有原始 surrogate escape 而不影响 API 正常 emoji 写入。
- 2026-07-17 Unicode/Store v4 对抗缺口已闭合并联合验证 36 passed：API模型和OpenAPI禁止 U+0000/孤立surrogate，v4 migration duplicate-aware 验证并规范化合法 surrogate pair 为字面emoji，DB trigger拒绝NUL与原始surrogate escape；脏v3保持版本3，合法emoji升级为4且可读。4条warning仍仅来自jsonschema/OpenAPI依赖弃用。
- 2026-07-17 发布P1两项已完成：PostInstall要求全部listener PID属于预期进程树；WinSW source/staged/published三次锁定复验且final检查位于SCM install前。PS5.1四脚本解析、listener混合冒充harness、8场景安装事务harness均PASS，相关pytest 31 passed。
- 2026-07-17 Store v4 二次对抗复核发现：扫描 raw JSON 的 surrogate GLOB 会误拒普通字面文本 `\\ud800`；BLOB/CAST 非UTF-8可利用SQLite JSON1宽松解码绕过纯SQL trigger；migration虽然调用模型却未使用返回值，非规范UUID未真正规范化。修复方案升级为每个SQLite连接注册确定性Python UDF，trigger以严格duplicate-aware JSON+PreconditionIssue验证实际值并要求storage class TEXT；移除raw GLOB；migration使用模型 `model_dump(mode=json, exclude_unset=True)`规范化。这样外部旧进程缺UDF时写入也会fail closed。
- 2026-07-17 Store v4 二次复核缺口已改为连接级确定性UDF闭环：`SQLiteDatabase.register_function` 让每个连接注册严格校验；trigger要求TEXT并调用duplicate-aware JSON+PreconditionIssue，移除raw surrogate扫描；migration使用模型输出规范化UUID且保留exclude_unset语义。字面 `\\ud800`、非UTF8 BLOB/CAST、非规范UUID、合法emoji与脏v3均新增回归，common+Store+OpenAPI 51 passed。
- 2026-07-17 Agent P1可靠性已收敛：服务映射冲突使用typed error，正常CLI只输出稳定脱敏JSON `SERVICE_MAPPING_CHANGED`/exit2且不进uvicorn；Heartbeat observe/ingress异常被监督、日志只含稳定事件码并继续2–60秒退避，observe失败不消耗sequence；OpenAPI拆分大小写兼容请求UUID与小写响应UUID；mysql/start和restart指纹固定向量已按实现重算并由CT验证；部署合同明确同一DB+端口单实例。Agent全量/公共安全/CLI/OpenAPI 114 passed，4条上游warning。
- 2026-07-17 Store v4 最终独立复审通过，无剩余P0/P1：字面反斜线文本、非UTF8 BLOB/CAST、非规范UUID升级、合法emoji、旧无UDF连接fail-closed和正常每连接注册均动态验证；相关42 passed。连接级UDF+TEXT门禁+模型规范化闭环成立。
- 2026-07-17 所有并行写入停止后的稳定全量回归为 `232 passed, 4 warnings in 38.96s`；无产品失败，4条仍仅是 jsonschema RefResolver 与 openapi-spec-validator shortcut 的第三方弃用警告。该结果替代此前197项基线，但仍需静态检查、最终重建和包内实跑。
- 2026-07-17 稳定工作树静态检查通过：`compileall` 覆盖 orchestrator/tests/scripts，`git diff --check` 无 whitespace error，`pip check` 无破损依赖；Git只提示10个既有已跟踪文件未来LF→CRLF转换。因主体仍未跟踪，静态结论与232项执行测试/OpenAPI语义验证共同使用。
- 2026-07-17 最终干净目录 `dist-recovery-final-20260717` 已从232项全绿工作树构建成功（exit0，150.8秒），包含三个PyInstaller onedir；仅重复报告 `tzdata` hidden import未找到。该目录尚需包内manifest、实际smoke和负向配置检查，不能仅凭构建成功发布。
- 2026-07-17 最终包内验证首两层通过：distribution verifier 对 `dist-recovery-final-20260717` 返回PASS，331/331、missing/extra/hash mismatch全0、side_effects=NONE（manifest仍需带外信任）；包内smoke实际启动新Agent/CP，health均ok、LAB_HTTP、AgentCount=1、EventLog映射INSTALLED/ACTIVE/AUTOSTART_ENABLED、Dashboard 200、GroupCount=0，且未调用action/Run。
- 2026-07-17 最终包的负向门禁已用权威结构验证：Agent/CP未编辑example均按正确 `--config PATH --check-config` 路径返回exit2、`config_valid=false`、稳定脱敏`configuration validation failed`且非usage错误；Frozen evidence validator读取模板返回exit1/`verdict=FAIL`/16项问题，证明空模板不能冒充验收通过。
- 2026-07-17 最终发布故障门禁按正确参数复跑通过：安装事务8场景覆盖staged/published篡改、install/start失败、rollback命令失败、uninstall失败保留依赖、非本次服务保护、受管残留fail-closed，结果PASS/TEST_TEMP_ONLY；listener harness覆盖唯一合法通过与合法+冒充混合失败，PASS/side_effects=NONE。
- 2026-07-17 最终本机构建/验证清理复核为零残留：Frozen进程0、smoke端口listener 0、近15分钟smoke临时目录0、Recovery Agent/CP Windows Service 0；本轮没有在开发机注册服务或留下后台进程。
- 2026-07-17 动作矩阵终审新增24组合与journal/SCM失败测试时发现一处待动态确认的真实漂移：Operation进入PREPARED后，首次SCM query异常被通用观察层折叠为UNKNOWN，现可能错误落 `REJECTED/SERVICE_STATE_UNKNOWN`；OP-03要求确定query失败为 `FAILED/SCM_ACTION_FAILED`。该问题若专项复现，将只在Operation查询边界最小修复并使列表观察仍可报告UNKNOWN。
- 2026-07-17 最终冻结审计以mtime确认 `dist-recovery-final-20260717` Agent EXE早于后续 `scm.py/operations.py` 修改，故331/331只证明包内自洽，不能代表最新源码；Phase1暂不可完成。所有动作矩阵写入停止后必须再次全量→新干净目录重建→manifest→Frozen smoke/负向/harness，当前final目录降级为过期候选。
- 2026-07-17 动作矩阵真实漂移已最小修复：新增严格观察边界，仅Worker执行/等待阶段保留确定SCM query异常并落FAILED/SCM_ACTION_FAILED；请求入账前软观察仍可业务REJECTED/SERVICE_STATE_UNKNOWN，DISPATCHING恢复仍保守UNKNOWN。新增24组合、5组SCM失败、3动作journal屏障与PREPARED/DISPATCHING恢复证据；operations 58 passed、Agent 115 passed、子任务全量266 passed。
- 2026-07-17 最终并发审计确认Agent quarantine排队竞态：请求B可在A设置quarantine前通过锁外检查并排队，A超时释放服务锁后B在锁内只重查Store active记录、不重查quarantine，可能与仍未返回的native副作用重叠。冻结前必须在取得per-service lock后做第二次quarantine检查并持久REJECTED/SERVICE_ACTION_CONFLICT，以精确屏障测试证明晚返回前零第二副作用。
- 2026-07-17 最终线上Schema审计确认CP `ReadinessWrite` 数值仍会被Pydantic强转：timeout接受string/bool，interval/deadline接受string/3.0，tcp port/http expected_status也需同步strict。PRB-06要求timeout为strict JSON number，interval/deadline及端口/status为strict integer；三kind必须补模型与PUT probe 422负向测试，修完后才能重建。
- 2026-07-17 Agent quarantine排队竞态已闭合：取得服务锁后、Store admission/SCM前二次检查，命中持久REJECTED/SERVICE_ACTION_CONFLICT；精确屏障证明B提前排队也不会产生第二start，晚native返回清隔离后新请求可no-op成功。operations59、Agent116、子任务全量267 passed。
- 2026-07-17 CP Readiness strict已闭合：Scm/Tcp/Http timeout为strict number，interval/deadline为strict integer，Tcp port/Http expected_status为strict integer；OpenAPI/Pydantic边界及真实PUT probe 422零持久化测试通过。专项17、子任务全量269 passed；最终只读审计确认除陈旧包重建和真实WIN外无剩余已确认P0/P1。
- 2026-07-17 所有最终P1写入停止后，root稳定全量复跑为269 passed/4上游warning（32.28秒）；compileall、git diff check、pip check再次通过。当前源码具备最终重建资格，先前两个20260717候选目录均不再作为部署来源。
- 2026-07-17 最新且唯一拟冻结目录 `dist-recovery-frozen-20260717` 已从269项全绿、最终P1修复后的稳定工作树构建成功（exit 0，67.3秒），包含Agent、Control Plane和evidence validator三个onedir包。PyInstaller仍仅报告既知的`tzdata` hidden import warning；该目录在manifest、Frozen smoke、配置负向、evidence负向、安装/listener harness、清理及源码时间新鲜度全部通过前仍只是冻结候选。
- 2026-07-17 最新冻结候选的包内精确完整性门禁通过：verifier报告331个预期文件与331个实际文件，missing/extra/hash mismatch均为0、无reparse副作用，`side_effects=NONE`。清单自身仍按合同标记`OUT_OF_BAND_REQUIRED`，最终必须另行记录`SHA256SUMS.txt`的带外SHA-256。
- 2026-07-17 最新冻结候选已用包内脚本和包内二进制完成实际只读烟雾：CP/Agent health均为ok，安全模式为LAB_HTTP，CP收到1个Agent，EventLog映射为INSTALLED/ACTIVE/AUTOSTART_ENABLED，Dashboard返回200、恢复组为0；脚本未调用任何action或Run，报告`SideEffects=NONE`。这也动态证明既知`tzdata`构建warning未阻断当前运行路径。
- 2026-07-17 最新冻结候选的Agent与Control Plane未编辑example均通过安全失败门禁：按正确`--config PATH --check-config`调用时，两者均exit 2、返回可解析JSON、`config_valid=false`、`error=configuration validation failed`，且输出不含usage；因此不是argparse语法错误造成的退出码假阳性。
- 2026-07-17 最新冻结候选的evidence validator负向门禁通过：对空白验收模板返回exit 1、`verdict=FAIL`和16项问题，证明未填写的模板不能伪造成三机/十轮验收成功。
- 2026-07-17 最新源码对应的安装事务故障注入再次通过全部8个场景：staged/published wrapper篡改、install/start失败、rollback命令失败聚合、uninstall失败保留依赖、非本次服务不删除、受管残留fail-closed；报告`outcome=PASS`、副作用仅为`TEST_TEMP_ONLY`。
- 2026-07-17 listener所有权故障注入再次通过：唯一合法listener被接受，合法进程与冒充进程混合占用同一端口时被拒绝；报告`outcome=PASS`、`side_effects=NONE`。
- 2026-07-17 最新冻结候选的最终本机收尾门禁通过：相关进程0、28785/28786监听0、近30分钟smoke临时目录0、Recovery Windows Service 0。Agent EXE时间晚于`scm.py`/`operations.py`/`common/models.py`的最新修改，CP EXE时间晚于`common/models.py`，二进制新鲜度均为true。
- 2026-07-17 `dist-recovery-frozen-20260717/SHA256SUMS.txt`的带外SHA-256为`760851a85e145c4bb0b572d2193c16b7c9e9629900258249f5b618b1dda00366`。发布树共有332个物理文件、122110872 bytes；其中331个文件由清单覆盖，另1个是清单自身，和verifier的331/331口径一致。
- 2026-07-17 回读总目标和成功标准后确认：Phase 1“合同到实现审计、缺口编码、稳定回归和最新冻结包验证”已完成；真实Windows安装、三机随机冷启动和10轮证据尚未发生，不能据此宣称MVP现场可用。当前阶段转为Phase 2，仅收集目标清单并取得部署授权，不推测主机或秘密。
- 2026-07-17 阶段转换一致性检查通过：Phase 1状态为complete且无未勾选项，Current Phase与Phase 2状态分别为目标清单/部署授权和in_progress，三份规划文件无尾随空白。
- 2026-07-17 Phase 2 首轮文件盘点确认：仓库已有Agent/CP示例配置、安装器、Host Preflight、发布验证器和现场运维文档，但文件清单中没有独立的deployment inventory合同、采集器或校验器。是否构成编码缺口需继续核对配置模型和运维流程；若确认，最小修复应只处理非秘密主机/服务事实，不扩展为资产管理系统。
- 2026-07-17 配置与运维流程核对确认该缺口成立：`AgentConfig`只验证单机allowlist/端点/CIDR，`ControlPlaneConfig`只验证单进程配置，二者都不能在生成配置前验证“1个独立CP+至少3个Agent、跨节点service_id/端口/IP冲突、readiness归属、严格DAG”；Host Preflight又要求最终JSON已存在。因此当前Phase 2只能靠人工从example逐机复制，缺少把非秘密现场事实转换成可审计配置草案的机器边界。
- 2026-07-17 最小实现边界确定为“部署清单合同 + 严格校验/渲染CLI + 本机只读事实采集脚本”，不做远程扫描、秘密生成/传输、SCM修改或资产数据库。清单仅保存主机/IP/端口、Windows版本、精确服务名、readiness与依赖；输出配置必须保留无效secret sentinel，确保未在目标机本地注入秘密前仍会fail closed。
- 2026-07-17 实现复用点已确认：readiness的严格数值/联合类型可直接复用`orchestrator.common.models.ReadinessWrite`，Agent草案可由`AgentConfig`最终复验；Control Plane现有Frozen可执行文件已有离线CLI分支，适合新增互斥的`--prepare-deployment`而无需引入第四个onedir。构建脚本当前只复制固定脚本/example/docs，新工件必须显式加入发布树和manifest测试。
- 2026-07-17 现有CLI/打包测试结构可直接扩展：`tests/test_config_check_cli.py`已用子进程验证脱敏JSON和无数据库副作用，`tests/test_packaging_contract.py`静态/动态验证Frozen帮助与发布文件。仓库无pyproject/setup打包元数据，PyInstaller直接以模块入口构建；新增inventory模块只需被CP入口导入即可进入Frozen包，依赖仍限现有Pydantic/标准库。
- 2026-07-17 inventory探针校验必须与真实Agent一致：TCP/HTTP只允许`localhost`或当前Agent本机绑定的IP，IPv4-mapped IPv6、zone id、userinfo、非HTTP和空/0端口均拒绝；HTTP可含路径/query且不允许fragment。清单无法实时证明网卡绑定，因此每个Agent节点需声明受审计的本机IP集合，探针目标只能是loopback或该集合，最终仍由PreInstall/运行时Agent再次验证。
- 2026-07-17 构建发布树在PyInstaller后显式复制固定脚本、examples和少量docs，再生成清单；因此inventory合同/example/只读采集脚本若新增但未同步`build_recovery_mvp.ps1`与packaging tests，会在源码可用、Frozen现场不可用。此项属于交付一致性门禁。
- 2026-07-17 Phase 2前的独立完成度审计动态复现新的P1：CP代理动作`POST /api/v1/services/{managed_service_id}/actions/{action}`对显式JSON `null`返回202并调用Agent；冻结合同只允许省略正文或严格`{}`。根因是CP路由只依赖`EmptyActionRequest`默认值，未像Agent路由读取raw body区分显式null。必须先补精确零dispatch回归并修复，再继续inventory。
- 2026-07-17 同一审计复现Agent Operation查询UUID格式漂移：`GET /api/v1/operations/{operation_id}`的FastAPI `UUID4`会接受compact/braced/URN表示并进入业务返回404，而OpenAPI `UuidV4Input`只允许规范连字符形式（大小写均可），应在路由边界返回422。需抽取不带Idempotency专用错误语义的canonical UUIDv4解析并审计所有公开UUID path参数。
- 2026-07-17 CP恢复组写模型又确认strict数值漂移：`RecoveryGroupCreate/Patch.node_settle_window_seconds`会接受字符串，`max_parallel_services`会接受bool并强转为1；OpenAPI只允许JSON integer，且二者直接控制汇聚时序/并发。应将所有对应创建/更新字段设为strict integer，并以真实API证明422且零持久化。
- 2026-07-17 UUID边界检索显示问题不限Agent Operation：CP的agent heartbeat、服务动作、Operation、Group、Run以及页面/过滤参数普遍直接标注`uuid.UUID`，Pydantic同样可能接受compact/braced/URN。需用一个可复用的canonical UUIDv4输入类型统一所有公开HTTP输入，同时保留响应模型的UUID4序列化；不能只点修单一路由。
- 2026-07-17 Phase 2工具审计又确认Agent配置跨字段缺口：`advertised_endpoint`端口可与`listen_port`不同，也可使用`0.0.0.0`等不可回连地址；`--check-config`仍通过，随后只会在CP回连/endpoint peer阶段失败。应在`AgentConfig`内拒绝不可单播/未指定endpoint并强制端口等于listen_port；若CP URL是IP literal，还应要求其落入唯一CP host-prefix allowlist。
- 2026-07-17 Run reason合同存在OpenAPI漂移：模型对非null reason已要求1..512，但OpenAPI的`ManualRunRequest.reason`及`RecoveryRun.reason`缺`minLength: 1`，且现有API测试未覆盖now/retry的省略、null、空串、1/512/513和未知字段零副作用。需同步Schema和行为证据。
- 2026-07-17 恢复引擎审计发现高风险协议错绑：Agent start响应目前只消费`operation_id/status`，未核对`agent_id/local_service_id/windows_service_name/action/idempotency_key/request_fingerprint`与当前Step/member/持久dispatch key一致。若返回另一个服务的SUCCEEDED Operation，当前Step可能进入PROBING/READY并放行严格下游。必须在保存operation_id和任何状态推进前做完整语义绑定验证；不匹配保守落Step/Run UNKNOWN并使用稳定协议错误，禁止重发或探针。
- 2026-07-17 错绑修复落点已确认：生产`AgentClient`会先将POST/GET响应解析为完整`Operation`，但Engine测试double只返回三字段，掩盖了语义绑定。Engine应按当前member与dispatch key重算`POST /api/v1/services/{local_service_id}/actions/start`空正文fingerprint，统一验证POST初始响应和后续GET响应；现有fakes必须升级为完整Operation，否则不能证明恢复窗口与协议同时成立。
- 2026-07-17 Phase 2工具审计完成：除inventory/facts外，HTTP+Token实验边界还缺一项独立、只读的双向网络/Firewall验收；现有local-only PreInstall明确把CP可达性标为SKIP。该门禁只能访问inventory明列端点并使用GET，禁止扫描、action、probe、SCM或修改Firewall；真实实现仍需目标机和授权，当前先冻结合同接口。
- 2026-07-17 配置安全另有两项可本机修复：CP `listen_host`当前连空字符串也能通过；`admin_password_hash`只检查前缀，`pbkdf2_sha256$garbage`可通过配置校验并导致管理员永久无法登录，超大iterations还可能造成登录DoS。应解析完整四段格式，限制迭代范围、salt/digest长度并验证base64，保持生成器输出兼容。
- 2026-07-17 本机facts采集的安全边界冻结为：无配置/Token依赖、显式服务名、只读输出hostname/Windows版本/架构/活动单播IP/服务Name+DisplayName+StartMode+State和候选端口；禁止远程WMI/WinRM、ImagePath/账户/环境输出及任何SCM/Firewall写操作，固定`side_effects=NONE`、`remote_hosts_scanned=0`。
- 2026-07-17 大组AUTO preflight确认容量失配：合法组可含1024成员，但`blocked_reasons`/DB仅允许100；`_preflight`会生成全部issue并直接持久化。101个违规成员已动态触发SQLite invariant，组卡在SETTLING且scheduler重复失败，既不fail open也无法解释。需确定性限制为100：保留至多99个排序后的代表问题，并用第100个稳定`PRECONDITION_ISSUES_TRUNCATED`汇总省略数量；1..100问题不应额外截断。
- 2026-07-17 公开集合容量又确认写侧不变量缺失：Agent/managed-service/group响应均声明最多1024，但Store没有全局封顶或分页；实测可创建1025组，`list_groups()`返回后被`RecoveryGroupCollection`拒绝，真实GET将变500。MVP不应在此时扩分页协议，最小修复是在注册Agent/镜像服务/创建组的同一写事务内执行全局1024上限，超限返回稳定业务错误且不部分写入；补1024/1025边界。
- 2026-07-17 最小Web交付项确认readiness编辑缺口：Groups页无论所选服务当前是TCP/HTTP都显示硬编码SCM JSON，切换服务不会加载`selected_group.probes`，也没有DELETE/回退SCM入口；保存后不刷新，操作者无法可靠核对或清除配置。需从页面已嵌入的成员/探针数据按service选择预填，提供显式删除，并以UI合同测试证明不会覆盖错服务。
- 2026-07-17 CP OpenAPI的所有路径/header/filter UUID目前复用只接受小写输出型`UuidV4`，而运行时`UUID`接受更多非规范表示；Agent已冻结为“规范连字符输入大小写均可、响应小写”。CP应新增同一`UuidV4Input`并只用于HTTP输入参数，响应继续使用小写`UuidV4`，从而同时拒绝compact/braced/URN并保留大小写兼容。
- 2026-07-17 RUN-09权威追踪明确：手工Run/Retry reason省略、`null`、1和512字符可进入业务校验；空串、513和未知字段必须422且零Run。OpenAPI只缺`minLength:1`，不应把nullable reason或可选请求体收紧掉。
- 2026-07-17 第一批复审修复已通过67项定向回归：CP显式null零dispatch；Agent/CP路径与过滤UUID只接受规范连字符UUIDv4且大小写均可；恢复组settle/parallel拒绝string/bool/float并零持久化；Run/Retry reason省略/null/1/512进入业务而空/513/未知字段422；Agent endpoint/CP配置/密码哈希安全边界也通过。4条warning仍仅为上游OpenAPI/jsonschema弃用。
- 2026-07-17 配置子审计的独立39项也全绿：Agent强制advertised端口等于listen端口、拒绝不可回连地址并校验具体listen/CP IP allowlist；CP listen_host必须IP literal且非multicast；PBKDF2严格解析四段、100k–1m iterations、canonical base64、16–64B salt和32B digest，登录验证复用同一解析。合同/operations已同步，错误路径不回显秘密。
- 2026-07-17 因独立完成度审计在原冻结后确认真实P1且源码已修改，`dist-recovery-frozen-20260717`立即降级为过期候选，之前的331/331与smoke仍是历史证据但不能代表当前源码。规划必须重新打开Phase 1，待Operation绑定、容量和UI缺口全部闭合后再全量、重建和包内复验。
- 2026-07-17 RecoveryEngine Operation语义绑定已闭合：POST初始与每次GET均核对operation_id、agent、local/Windows service、start动作、持久key和canonical fingerprint；错绑在保存/探针/放行前使Step/Run UNKNOWN并持久`AGENT_PROTOCOL_MISMATCH`，独立分支继续、严格下游BLOCKED。Engine/Store/三节点/OpenAPI/common 48 passed；当时稳定全量300 passed/4上游warning。
- 2026-07-17 blocked reason容量失配已闭合：`_preflight`对超过100项的问题做确定性排序，保留前99项并用第100项`PRECONDITION_ISSUES_TRUNCATED`汇总省略数量；101个STARTUP_NOT_MANUAL问题现稳定进入BLOCKED_PRECONDITION、持久100项且不再卡SETTLING。精确测试2 passed。
- 2026-07-17 集合容量修复方案冻结为写侧原子门禁而非扩大MVP分页：新Agent注册前检查agents总量；每次accepted report在同一事务中以“其他Agent当前seen服务数 + 本报告服务数”检查公开服务总量；新Group插入前检查group总量。按合同统一422/VALIDATION_ERROR，事务失败不得更新lease、sequence、镜像或部分插入。历史unreported服务不计公开集合，但仍由不可改绑身份边界约束。
- 2026-07-17 Phase 1R 冻结验证续作已回读三份规划材料；当前唯一剩余门槛是验证新候选 `dist-recovery-phase1r-20260717`，不进入真实部署。两个权威直接 harness 已定位为 `tests/test_install_recovery_service_transaction.ps1` 与 `tests/test_recovery_host_preflight_listener_ownership.ps1`，执行前必须分别读取参数块。
- 2026-07-17 Phase 1R 新候选对应的直接故障注入门禁通过：安装事务8场景 `outcome=PASS`、`side_effects=TEST_TEMP_ONLY`；listener所有权2场景 `outcome=PASS`、`side_effects=NONE`。两者均使用替身边界，未安装服务或操作真实监听器。
- 2026-07-17 新候选的冻结烟雾脚本默认端口为18765/18766，临时目录前缀为`winsw-recovery-smoke-`；最终清理检查同时覆盖默认端口和历史自定义端口28785/28786，避免仅依赖旧候选的清理口径。Agent/CP EXE时间分别为03:25:33Z与03:25:54Z，下一步将与当前权威源码最大mtime做机器比较。
- 2026-07-17 Phase 1R 新候选带外清单SHA-256为`ceccf51d5c76ff0b2afb824f64c5f9a6ef50c477896cab282752ba8331e90bb5`；331个受管文件+清单自身=332个物理文件，共122127072 bytes。当前37个运行时源码/模板/静态文件中最新为`agent_client.py`（03:22:35Z），Agent/CP EXE均更新（03:25:33Z/03:25:54Z），新鲜度通过。
- 2026-07-17 新候选收尾扫描为相关进程0、默认及历史自定义端口监听0、Recovery服务0。TEMP中另有2个`winsw-recovery-smoke-*`目录，但mtime均为2026-07-16，早于本次源码、构建和烟雾，不是新候选残留；不擅自删除历史用户临时内容，最终门禁将按“本次构建/近30分钟新增残留=0”核实。
- 2026-07-17 Phase 1R 精确收尾门禁通过：自本次Agent EXE构建时间以来烟雾目录0、近30分钟烟雾目录0、相关进程0、四个可能烟雾端口监听0、Recovery服务0；`git diff --check` exit 0，仅输出既知LF→CRLF提示。两个2026-07-16历史TEMP目录保留且明确不计入本次副作用。
- 2026-07-17 独立冻结门槛审计指出：直接harness目前只绑定仓库脚本，仍须把`RepositoryRoot`改为新候选发布树，验证其中复制的安装器/preflight；同时须纳入Evidence Validator EXE新鲜度，并对发布树脚本、示例、文档与WinSW lock做源→包哈希一致性证明。此前结果仍有效，但不足以单独完成Frozen标记。
- 2026-07-17 两个故障注入已重新绑定`dist-recovery-phase1r-20260717`发布树中的复制脚本并全部通过：安装事务8场景PASS/TEST_TEMP_ONLY，listener 2场景PASS/NONE。至此仓库脚本与实际发布复制件均有动态证据。
- 2026-07-17 构建脚本的发布复制集合已精确回读：4个PowerShell脚本、3个example、WinSW lock、运维文档和evidence合同共10个源→包文件；Evidence Validator唯一直接源码输入为`scripts/validate_recovery_evidence.py`。下一门禁按该权威清单逐项SHA-256，不做模糊目录比较。
- 2026-07-17 发布树源→包绑定门禁通过：构建脚本声明的10个复制文件逐项SHA-256全部一致。Evidence Validator按入口脚本、acceptance与common Python输入共11个文件比较，最新输入02:52:14Z，EXE为03:26:08Z，`evidence_exe_fresh=true`。
- 2026-07-17 两个历史TEMP目录已完成删除前只读审计：绝对路径均位于当前用户`%TEMP%`，名称严格匹配`winsw-recovery-smoke-{UUID}`，目录不是reparse point，内部仅含本项目smoke生成的agent/CP配置、日志与SQLite夹具（8项/5项）。可按脚本同等安全边界限定删除，以满足全局临时残留为0并清除测试Token夹具。
- 2026-07-17 已在命令内重新验证TEMP前缀、严格UUID目录名和非reparse后删除2个历史smoke夹具；复核为smoke目录0、相关进程0、四端口监听0、Recovery服务0。删除范围仅为已确认的本项目临时测试数据。
- 2026-07-17 所有候选绑定测试结束后再次执行发布完整性验证：331/331、missing/extra/hash mismatch均0、side_effects=NONE；带外清单SHA仍为`ceccf51d5c76ff0b2afb824f64c5f9a6ef50c477896cab282752ba8331e90bb5`，证明测试过程未改变发布树。
- 2026-07-17 最终独立只读复核确认Phase 1R无剩余技术冻结项：321全量、静态/依赖、P0/P1、构建、包内烟雾、负向门禁、发布树harness、三EXE新鲜度、10/10资源绑定和残留清理全部闭环。`dist-recovery-phase1r-20260717`现作为当前Frozen发布候选；真实PreInstall/PostInstall、三机与十轮断电仍未发生，当前阶段转入Phase 2目标清单与授权。
- 2026-07-17 Phase 2状态复核正确：Phase 1 complete、Phase 2 in_progress。仓库当前没有inventory/facts/deployment template文件；在不修改已冻结发布树的前提下，下一步必须由用户提供非秘密目标事实与部署授权，不能猜测主机、Windows Service Name、readiness或网络边界。
- 2026-07-17 planning-with-files session-catchup识别到5条未同步上下文；回读三份规划材料并执行`git diff --stat`后确认，Phase 1R证据已同步，尚未同步的是“继续自主推进Phase 2工具”的决策。现将Phase 2拆为合同/CLI、只读facts、发布重冻结、再收集现场事实；旧Frozen不原地修改。
- 2026-07-17 Inventory复用审计确认：`AgentConfig`已具备服务唯一性、endpoint/listen端口、IP/CIDR与loopback交叉校验；`ControlPlaneConfig`具备IP/CIDR与secret校验；`topological_levels()`可直接做严格DAG环/域外引用检测。CP CLI当前是手写分支，新增`--prepare-deployment INVENTORY --output-dir DIR`必须与`--config/--check-config/--generate-secrets`形成显式互斥，并在任何uvicorn/config加载前完成离线处理。
- 2026-07-17 配置草案不能伪装为可启动配置：现有Agent/CP模型会正确拒绝无效secret sentinel。Inventory渲染应先严格验证所有非秘密结构，再输出带明确`__GENERATE_ON_TARGET__`类sentinel及`config_ready=false`的草案；不得为“复用最终模型”而输出可预测的伪Token或放宽生产配置校验。
- 2026-07-17 `ReadinessWrite`可复用严格kind/数值/超时联合模型，但它本身不验证TCP host或HTTP URL的本机安全边界；Inventory必须额外按目标Agent的`local_addresses`执行与运行时探针相同的URL/IP规则，不能把Pydantic判定等同于可执行探针。`topological_levels`边语义已明确为`(dependent, prerequisite)`。
- 2026-07-17 现有CLI测试已建立脱敏、BOM、缺文件、配置零数据库副作用和未编辑example fail-closed模式。`--prepare-deployment`应沿用机器JSON输出：成功exit0且只输出摘要/工件路径与哈希，失败exit2且固定脱敏错误，不把inventory正文、服务名以外的敏感未来字段或异常repr写到stdout/stderr。
- 2026-07-17 Agent运行时探针安全规则及其对抗测试已完整定位：仅`localhost`或地址提供器中的IP，拒绝DNS、远端IP、IPv4-mapped IPv6、zone id、非HTTP、userinfo、fragment、0/空端口，并在连接边界二次解析/验证。抽取共享纯函数时必须保持请求前与连接前双重调用，不能因复用Inventory而削弱TOCTOU防护。
- 2026-07-17 Packaging合同当前以构建脚本文本断言、PS5.1安全删除边界、Frozen help和发布manifest为主；新增工件必须同时加入构建脚本复制清单、help断言、发布文件存在/哈希测试和Frozen `--prepare-deployment`无数据库/无网络烟雾，不能只测源码CLI。
- 2026-07-17 权威合同/运维术语核对：部署清单将连接现有“固定local_service_id映射、每服务最多一个readiness、依赖语义dependent→prerequisite、至少三机验收、PreInstall前配置必须严格通过”的流程；草案阶段必须醒目标记不可PreInstall，只有目标机本地注入secret并各自`--check-config`通过后才进入现有唯一发布顺序。
- 2026-07-17 Packaging只读审计确认最小新增发布件恰为3个：`scripts/collect_recovery_host_facts.ps1`、`examples/deployment-inventory.example.json`、`docs/contracts/recovery-deployment-inventory-v1.md`。manifest会自动覆盖，但构建脚本须显式Copy-Item；Inventory Python模块经CP `__main__`静态import即可进入既有第三方依赖图，不新增第四个onedir。
- 2026-07-17 Host Facts实现的实际文件名冻结为`scripts/get_recovery_host_facts.ps1`，参数为必填非空`-WindowsServiceName`（alias `ServiceName`）、可空`-CandidatePort`及`-PassThru`；对应测试为`tests/test_recovery_host_facts_contract.py`。发布与文档后续统一使用实际文件名，废弃先前建议的`collect_...`命名。
- 2026-07-17 Host Facts代码已落地并完成root只读审查：所有采集入口仅用本机`Win32_OperatingSystem`、精确`Win32_Service`、活动接口和`Get-NetTCPConnection`；Invoke层重建字段白名单，未知/缺失服务保守FAIL，非法作用域在任何collector前拒绝，顶层固定`side_effects=NONE`与`remote_hosts_scanned=0`。参数未用PowerShell Mandatory绑定而由机器JSON路径返回FAIL/exit2，这是为了无参数时仍保持非交互、可解析失败，不改变“调用者必须显式提供非空服务列表”的合同语义。
- 2026-07-17 Host Facts只输出OS数值version与architecture，不输出Caption/edition、ImagePath、账户、环境或进程ID；这足以作为Windows版本事实并由后续PreInstall判定Server/64-bit。活动地址允许原样报告link-local等真实接口事实，Inventory只会选择并接受明确的非loopback、非link-local主地址，二者职责分离。
- 2026-07-17 Host Facts定向合同复跑9/9通过（2.05秒），覆盖PS5.1解析、无参数机器FAIL、字段白名单、单元素数组、非法服务/端口零collector及静态禁止远程/写操作。唯一warning是sandbox下pytest cache WinError 5，与产品无关；后续禁用cacheprovider避免噪声。
- 2026-07-17 Deployment Inventory v1合同与三Agent/五角色示例已落地。合同写死独立CP、至少3 Agent、全局唯一service_id、64位/本地绝对数据目录、严格本机readiness、每服务显式探针、五角色同组及四条必需直接依赖，并定义原子输出、故意无效secret sentinel、manifest与脱敏CLI错误；尚待模型/测试反证后才能标记冻结。
- 2026-07-17 Inventory example已通过标准JSON解析；Host Facts在禁用pytest cacheprovider后9/9再次通过且无warning，确认先前WinError 5只来自测试缓存写入，不是产品或脚本问题。
- 2026-07-17 运维手册已把唯一发布顺序扩展为manifest校验→本机Host Facts→Inventory渲染→目标机本地秘密注入/权威check-config→PreInstall，明确`config_ready=false`草案不得安装。CP节点无业务服务时只可用明确的本机既有服务完成facts作用域，不得把它加入Agent allowlist。
- 2026-07-17 Recovery MVP主合同的打包/部署章节已加入Inventory/Host Facts发布与fail-closed边界，避免新合同只成为旁路文档；详细字段仍由专门Inventory合同权威管理。
- 2026-07-17 Control Plane CLI已接入`--prepare-deployment INVENTORY --output-dir DIR`离线分支：与config/generate模式互斥，先于配置加载和uvicorn，成功仅输出数量/hash与`config_ready=false`，任一异常固定脱敏exit2。当前暂依赖并行实现中的`orchestrator.deployment.inventory`，模型落地前不运行CLI测试。
- 2026-07-17 新增CLI端到端测试合同：合法示例输出3 Agent/5服务/1组、manifest逐项复算、草案全部被权威check-config拒绝、注入同一Token与本地CP秘密后全部通过；未知字段/既有输出目录/模式混用均exit2且不改用户文件。测试待Inventory模块完成后执行。
- 2026-07-17 共享探针验证器的43项定向测试通过；扩大Agent App集时6项setup被默认TEMP pytest ACL WinError 5阻断，未进入产品代码。后续所有较宽pytest统一`--basetemp .test-tmp/<唯一目录> -p no:cacheprovider`，不修改系统TEMP ACL。
- 2026-07-17 Phase 2 packaging已落地：构建脚本显式复制Host Facts、Inventory example与合同；新增轻量真实构建测试逐件证明源文件/包内文件/manifest三方SHA一致并清理临时dist。新增2项PASS，排除并行缺模块的CLI help后packaging 23项PASS；完整集待Inventory模块落盘后重跑。
- 2026-07-17 Root复核packaging修改：三个Copy-Item目标分别进入既有scripts/examples/docs/contracts目录，manifest生成之后自动纳入；轻量构建使用唯一`dist-packaging-contract-{uuid}`并在finally清理，逐项比较源、包与manifest hash。现有help断言尚只覆盖config/check/generate，模块收敛后需补prepare/output两个参数。
- 2026-07-17 Architecture与Roadmap已同步Phase 2部署准备职责：Inventory/Host Facts是Recovery MVP纵切的一部分，但当前状态明确为“Phase 1R旧冻结包完成、部署清单工具编码中”，避免把修改中的源码误称为新Frozen或现场可用。
- 2026-07-17 Recovery追踪矩阵新增DEP-07/DEP-08，把Inventory与Host Facts分别映射到合同/CLI/工件/JSON结果及可执行CT/IT/WIN场景；新MUST不再只存在于说明文档。
- 2026-07-17 Root复核共享`probe_targets`：纯函数零I/O、显式地址集合异常fail closed，保持localhost/loopback、URL端口/path/query quote与统一422码；Agent请求接收与`_tcp/_http`连接创建前仍分别重新获取实时地址并验证，TOCTOU二次边界未丢。新增测试覆盖string/ipaddress地址、IPv6规范化、畸形地址集和全部绕过向量。
- 2026-07-17 创建并验证工作区`.test-tmp`父目录后，shared probe + Agent probes + Agent App联合49/49通过（3.65秒）；此前6个setup错误确认仅因basetemp父目录不存在。共享验证器可进入Inventory收敛测试。
## 2026-07-17：Deployment Inventory 实现接管审计

- `orchestrator/deployment/inventory.py` 已落地严格模型、跨节点校验、DAG 环检测、探针本机目标校验及原子目录发布，但原实现代理未补交独立测试，后续由主任务接管验证。
- 发现 CP CLI 与模块公开 API 不一致：CLI 以错误参数调用 `render_deployment`；应直接调用 `prepare_deployment(inventory_path, output_directory)`。
- 部署清单字段存在命名漂移：实现输出 `input_sha256`，新增 CLI 验收测试期望 `inventory_sha256`。为使字段语义明确，统一冻结为 `inventory_sha256`。
- Windows 数据目录校验仍需拒绝非法文件名字符、尾随空格/点及保留设备名，避免生成无法在目标 Windows Server 使用的配置。
- 节点网卡地址目前只保证主地址唯一；同一 IP 仍可能被两个 Agent 的 `active_unicast_ips` 重复声明，可能造成探针目标归属歧义，需改为所有已声明 Agent 地址全局唯一。
- 原子发布、失败清理、未知字段、依赖链、探针目标、秘密哨兵、配置可用性和 CLI 模式隔离均需由专门的 Inventory 测试覆盖后才能视为合同实现完成。
- 对照完整 Inventory 合同后确认：CP data directory 与 Agent 不同、秘密 sentinel、三节点/五角色直接依赖链均是显式 MUST；路径可用性与多网卡归属仍需进一步写死。现已决定将 Windows 非法字符/尾随空格点/保留设备名纳入路径拒绝规则，并将所有 Agent 声明接口地址冻结为全局唯一。
- `deployment-manifest.json` 的输入哈希公开键统一为 `inventory_sha256`；CLI 必须调用包含“读取原始字节→校验→计算哈希→原子渲染”完整流程的 `prepare_deployment`，不得绕过该入口自行拼接参数。
- CLI 专项成功后复查确认：文档、实现和测试已无 `input_sha256` 旧键；仓库根也没有遗留 prepared/tmp 渲染目录。打包脚本及其合同测试已经明确包含 Host Facts 脚本、Inventory 示例和 Inventory v1 合同，待收敛树上重跑完整打包门禁。
- 对照最终配置模型后确认，Inventory 生成的 IP literal、host CIDR、HTTP base URL、端口、服务 slug/name 和数据库绝对路径均落在 `AgentConfig` / `ControlPlaneConfig` 的有效域；示例注入合法秘密后已经由真实 CLI 证明可启用，没有“Inventory 通过但草案结构永远无效”的兼容性缺口。
- 独立 Inventory 对抗测试已形成 94 项，覆盖未知字段/严格类型、节点与服务全局约束、Windows 路径、探针本机边界、DAG/五角色直接链、秘密 sentinel、manifest/逐文件哈希、确定性及故障注入原子清理。
- 独立只读审计又发现三个冻结阻塞：Pydantic `strict=True` 并未让 `Literal[1]` 拒绝 JSON `true`/`1.0`；CP CLI 顶层导入 `uvicorn/create_app` 使离线 prepare 模式仍加载 Web 栈；失败清理使用 `ignore_errors=True` 会静默遗留临时目录。三项都必须通过显式实现和回归证明后才能关闭 Inventory 阶段。
- 该审计已正向验证 IPv4 与全 IPv6 渲染：secret sentinel 会被最终配置模型拒绝，注入合法共享 Token、PBKDF2 哈希及 session secret 后 CP 与三个 Agent 均可通过；IPv6 URL 方括号和 `/128` 来源 CIDR 兼容。
- P2 调用面也已收紧：调用者不再能通过公开 `render_deployment(..., inventory_sha256=...)` 自报输入哈希；只有 `prepare_deployment` 可从原始 Inventory 字节内部计算 manifest 哈希。语义重排测试改为比较除 manifest 输入哈希外的稳定输出。
- 独立测试原先未直接覆盖 strict schema 的 bool/float、总服务 1025、非 64 位节点和 Web 栈惰性加载；前三项已补入 Inventory 测试，最后一项将使用隔离子进程 import guard 验证。
- 隔离子进程 import guard 已证明 `--prepare-deployment` 不加载 `uvicorn` 或 `orchestrator.control_plane.app`。Inventory 与全配置 CLI 的 135 项联合回归全绿，三个新增 P1 均有直接失败前/成功后证据。

## 2026-07-17：Host Facts 主线审计

- 非法作用域当前仍在最终组装报告时调用 hostname collector，与合同“非法输入零采集”冲突；非法输入应保留 `hostname=null`，所有事实采集只能进入校验成功分支。
- 候选端口使用 `Get-NetTCPConnection -LocalPort ... -ErrorAction SilentlyContinue`，会把“查询权限/提供程序失败”与“没有连接”混为一谈并错误报告端口空闲。应以 `-ErrorAction Stop` 获取连接集合后本地筛选，真正采集失败进入 `outcome=FAIL` 和 null 状态。
- 活动接口当前会输出 IPv4/IPv6 link-local，无法直接填入 Inventory v1（其明确拒绝 link-local）；Host Facts 应只输出与 Inventory 相同的规范非 loopback、非 link-local、非 unspecified/multicast、非 IPv4-mapped IPv6 地址。
- OS 字段、接口地址和服务关键字段即使为空仍可能保持 PASS；“可解析对象”不等于“已确认事实”，需要对版本/架构、至少一个有效接口以及服务 Name/DisplayName/StartMode/State 完整性做失败闭合。
- 独立审计已实证真实 CLI `-CandidatePort not-a-port` 会在 `[int[]]` 参数绑定前置失败：exit 1、非 JSON，并泄露原始输入和绝对脚本路径。候选端口顶层参数必须改为无损字符串接收，再在脚本内以 invariant 十进制整数规则验证，所有非法值统一返回脱敏 FAIL JSON + exit 2。
- Host Facts 修复后 12 项 PowerShell 5.1 合同测试全绿；`Get-NetTCPConnection` 现在以 Stop 语义获取事实，link-local/unspecified/multicast/mapped IPv6 被拒绝，非法输入报告 `hostname=null` 且不会触发任何 collector。
- 全仓交叉检索未发现其他地方依赖“非法输入仍有 hostname”或 link-local 输出；运维命令的 PowerShell 数组写法可由新的字符串入口正常接收。追踪矩阵 DEP-08 仍需补入“非法类型脱敏 JSON/exit2、地址兼容和端口采集失败 fail closed”的新证明。
- Host Facts 最终汇聚不再信任 collector 对象：hostname/OS/service/address/port 均需严格基础类型与内部一致性，任一对象型 canary 会被转换为固定 null/UNKNOWN 失败形态且不会进入 JSON。公开参数面现只剩合同规定的服务名与候选端口。
- 发布约束回读确认：构建器要求 Python 3.13、固定 PyInstaller 6.16.0，并只接受仓库根的直接 `dist-*` 子目录。`dist-recovery-phase2-20260717` 当前不存在且旧 `dist-recovery-phase1r-20260717` 存在，因此新候选使用前者，旧包保持只读回退。
- 1 秒超时后的残留审计确认没有 Python 构建进程；新输出目录只含构建器声明管理的 `.build`，没有程序包、manifest 或用户文件。可由同一构建脚本在原目标安全收敛，不需手工删除目录。
- 新 Phase 2 包的独立分发校验已返回 PASS：manifest 与实际文件均为 334，缺失/额外/哈希不一致均为 0；manifest 仍按合同标记 `OUT_OF_BAND_REQUIRED`，需另算清单自身 SHA-256。
- Frozen 烟雾失败后的清理审计确认 Agent/CP 进程和 18765/18766 listener 均已消失，但脚本按旧逻辑在失败时保留整个系统临时树（8 个文件）。该树包含烟雾配置中的随机 Token/session secret；即使仅供诊断也不应持久留存。日志 tail 已在异常路径输出，随后应无条件删除已验证位于系统 temp 下的受管烟雾目录。
- 本次残留已通过路径归属与受管前缀双重检查后删除并复核；现有测试只有烟雾脚本静态/配置合同，没有故障路径清理 harness，需补“失败仍调用清理且路径逃逸拒绝”的直接证据。
- 烟雾脚本可在主逻辑前加入 dot-source guard，将清理函数作为可独立验证的安全边界：只删除系统 temp 的直接子目录、要求受管前缀且拒绝 reparse point。finally 无条件调用该函数；合同测试同时执行真实删除与越界拒绝，并静态证明不再受 `$failed` 条件控制。
- 重建后的分发再次为 334/334 文件、零缺失/额外/哈希漂移；新的 manifest 自身 SHA-256 为 `7d8af82d5bf7c6c0695090b61bf8e0743a466c07923b2c8cc92b6658d9b4180e`。Frozen Inventory prepare 再次成功，输出哈希稳定。
- 同一重建包在非沙箱只读权限下完成真实 WMI/SCM + loopback 全栈烟雾，所有门禁通过且未调用 action/Run；先前 PermissionDenied 已被证实为执行环境权限差异。
- Phase 2 收敛树全量测试为 467/467 通过；仅剩第三方 OpenAPI/jsonschema 弃用警告，不影响当前语义验证。不能由此推断真实三机与断电验收通过，后者仍需现场 Inventory/授权。
- 路线图仍写“Inventory 工具编码中”，与当前 Phase 2 Frozen/467 项验证证据漂移；已改为“工具与候选已验证，待现场清单/授权和三机 10 轮”，README 同步但继续明确非生产可用。
- 最终发布树交叉核对证明 smoke 脚本、Host Facts、Inventory 示例与合同的“源码 = 包内 = SHA256SUMS”三方哈希完全一致；分发验证仍为 334/334 PASS。`git diff --check` exit 0，仅提示现有 Git CRLF 转换策略，无空白错误。
- 冻结 EXE 直接验证了 1 份 CP + 3 份 Agent 草案均因 sentinel 以固定 JSON/exit2 fail closed；包内 Host Facts 对非法端口也返回脱敏 FAIL/exit2。工作区最终只剩 3 个明确由本轮 pytest/代理创建的临时目录，可在校验绝对路径仍为仓库直接子树后删除。

## 2026-07-17：目标完成度续审

- 当前任务计划中尚未完成的 Phase 2–7 项全部以真实主机清单、现场只读预检、安装授权或三机断电证据为前置；不能仅凭本机 467 项测试勾选。
- `TODO/FIXME/NotImplemented/pass` 搜索命中均为抽象异常类型、协议占位或故意忽略的清理异常，未发现可执行路径中的显式未实现函数。
- 核心公开能力已存在直接实现与测试：MANUAL Run、AUTO Run 发现、retry、Run Detail、readiness attempt 时间线、证据 Schema/validator、单服务操作和登录/CSRF 均非文档空壳。
- 仍需重点独立审计“现场证据如何从 CP/Agent 数据形成验收包”；当前离线 evidence validator 与模板存在，但是否具备足够、稳定的产品导出面不能仅由模板单元测试证明。
- Web/证据独立审计确认 API 足以导出 evidence-v1 的机器可推导字段；CP/OS 重启等事实按冻结合同强制 manual proof，属于有意边界而非漏实现。
- 新发现四项本地 P1 候选，需复现后修复：evidence validator 可能接受伪造 `dependency_chain`、Step 非单调时间和早于活动窗口的 `exported_at`；Groups 编辑器要求 managed UUID 但页面不展示；Run Detail 不显示冻结 dependencies/probes 快照；单服务异步动作 UI 不轮询 Operation 终态。
- 核心独立审计确认 CP 手工单服务代理仍缺完整 Operation 绑定：POST 可接受同目标旧 dispatch key，GET 未强制响应 `operation_id` 等于路径 ID，且 Agent 非 2xx 的 message/detail 会原样公开。此缺口必须以 CP 自有稳定错误、完整预期绑定和 `AGENT_PROTOCOL_MISMATCH` fail-closed 修复。
- 部署链独立审计确认 PreInstall 端口查询仍可能 fail-open：`Get-NetTCPConnection -ErrorAction SilentlyContinue` 无法区分“无 listener”与提供程序/权限失败。端口事实不可确认时必须稳定 FAIL，不能报告 free。
- 部署链独立审计确认安装器没有跨进程互斥：两个提升权限安装可能同时越过 snapshot/gate 并在冲突回滚时恢复旧 ACL。互斥必须在任何 gate、快照和 ACL 前取得，按固定 role + data directory 唯一，持有至 commit/rollback 完成；失败必须零副作用。
- Evidence 对抗审计实证当前会错误 PASS：`exported_at` 早于演练、BLOCKED Step 的虚假 `dependency_chain`、`finished_at < started_at`。报告还遗漏 `ManualProof.reviewed_at/summary`。前三项按 P1 修复，报告字段同时补齐。
- Web P1 范围收敛为 Groups 能选择/识别上下游 managed service、Run Detail 显示冻结 dependencies/probes 快照；单服务 Operation 终态轮询暂列 P2，但若修改同一静态链路且风险可控则一并完成。
- 核心审计仅另发现 WMI boot identity 查询失败会冒泡 traceback 的 P2；其行为已经 fail-closed，但应评估用 exit 2 + 稳定脱敏 JSON 改善可运维性。
- 构建链新增 P1：`build_recovery_mvp.ps1` 复用既有 `dist-*` 根时只重建已知受管目录，未知顶层文件/目录会被一并写入 manifest，验证器仍会 PASS。当前 Phase 2 候选经独立检查为 clean；但任何重建前必须枚举允许的顶层项，未知项 fail-closed 且不删除用户数据，并在 Python/PyInstaller 调用前完成。
- Web P1 已用两项回归测试稳定复现：Groups 页面没有结构化 dependent/prerequisite 控件；Run Detail 没有冻结执行快照。失败均发生在新增断言，既有登录、Store 与页面渲染正常，修复范围可限定为模板、静态 JS/CSS 和对应 UI 测试。
