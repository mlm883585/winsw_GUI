# Recovery MVP 三机断电验收证据合同 v1

状态：**已冻结，供真实环境验收使用（2026-07-16）**。

## 1. 目的和边界

本合同定义一个可离线复核的 JSON 证据包，用于检查三台以上 Windows Server 的 Recovery MVP 验收记录。证据被严格拆为两部分：`rounds[]` 是连续至少 10 轮、全部成功的随机冷启动 AUTO Run；`scenario_exercises[]` 是另外执行、各自拥有独立起止窗口的故障与恢复场景。场景不得复用十轮成功 Run 的 ID，也不能通过给成功轮次贴标签来代替。校验器只读取本地 JSON，不连接 Agent、Control Plane，不启动、停止或重启任何服务，也不承担日志中心职责。

证据包不是防篡改审计日志。唯一性、拓扑、时间顺序及绑定到 Run 的失败结果由程序从已导出的数据推导；“时间窗口内没有遗漏其他 Run/动作请求”只能由操作者声明。CP 最后启动、Agent 仅进程重启、单节点 OS 重启、缺节点期间零动作、Agent 断线和 CP 中途重启不能仅靠当前 API 导出独立证明，必须附加人工复核的证据文件引用、SHA-256 和复核人。校验器只校验这份引用元数据是否存在，不读取或判断证据文件内容。

## 2. 输入 Schema

权威 Schema 由与校验器相同的 Pydantic 模型生成，避免手写 Schema 与实现漂移：

```powershell
python .\scripts\validate_recovery_evidence.py --print-schema
# 发布包等价入口：
.\dist-recovery\winsw-recovery-evidence-validator\winsw-recovery-evidence-validator.exe --print-schema
```

证据文件必须是 UTF-8 JSON；允许由 Windows 工具产生 UTF-8 BOM。校验器不接受 ANSI、UTF-16 或其他本地代码页。

起始模板为 `examples/recovery-evidence.template.json`。模板故意保留空集合并把两项完整性声明设为 `false`，因此未经填充必须返回 `FAIL`，不能把模板本身当作验收结果。

| 顶层字段 | 来源 | 约束 |
|---|---|---|
| `schema_version` | 固定值 | `recovery-mvp-evidence-v1` |
| `campaign` | 验收计划 | 至少 10 轮；将 MySQL、Redis、Nacos、Java、Nginx role 映射到不同 `managed_service_id` |
| `completeness_attestation` | 操作者 | `all_runs_in_declared_windows` 与 `all_action_attempts_in_declared_windows` 必须明确声明十轮及所有场景窗口内的全部 Run/动作尝试均已导出；时间必须带 UTC offset |
| `inventory` | CP `GET /api/v1/services` 响应 | 直接粘贴完整 `ManagedServiceCollection`，至少覆盖 3 个 Agent |
| `rounds[]` | 十轮冷启动现场记录 | 连续 `round_number`、唯一 epoch、唯一 AUTO Run ID、开机次序和起止窗口；每轮必须 `SUCCEEDED` 且全部 Step `READY` |
| `scenario_exercises[]` | 独立故障演练记录 | 九种判别类型；每项必须有独立 `exercise_id/window_started_at/window_finished_at`，并引用对应 MANUAL/AUTO Run、服务、Operation 或人工证据 |
| `runs[]` | CP `GET /api/v1/recovery-runs/{run_id}` 响应 | 必须包含十轮 AUTO Run，以及所有场景引用的 MANUAL 或 AUTO Run（包括 `SINGLE_NODE_REBOOT`）；全部终态且每个 Run 恰好归属一个轮次或一个场景 |
| `actions[]` | Agent Operation 或错误响应 | 每项必须恰好设置 `cold_round_number` 或 `scenario_exercise_id`；每个非空 `step.operation_id` 对应一个终态 Operation，另记录未知服务负向请求 |

`actions[]` 有两种判别类型：

| `kind` | 必要字段 | 含义 |
|---|---|---|
| `operation` | 归属二选一、`managed_service_id`、`run_id`、`step_id`、采集侧 `observed_at`、Agent `Operation` 原文 | 已接受并持久化的恢复动作；必须引用同一归属下的 Run/Step，且是该 Step 的 `start` 和同一个幂等键 |
| `rejected_request` | 归属二选一、目标 Agent/local id、动作、HTTP 状态、时间、`ErrorResponse` 原文 | 未产生 Operation 的拒绝请求；未知服务必须归属 `UNKNOWN_SERVICE_REJECTION` 场景，并为 `404 + SERVICE_NOT_ALLOWLISTED` |

“归属二选一”指：冷启动动作只设置 `cold_round_number`；场景动作只设置 `scenario_exercise_id`。二者同时设置或同时缺失均为 Schema 错误。故障场景动作不得把 `cold_round_number` 指向某个成功轮次。

Action 包装层的 `observed_at` 来自采集侧，必须落在其归属窗口。`Operation.created_at/started_at/finished_at/updated_at` 来自远端 Agent 时钟；合同不信任它与 CP/操作者时钟同步，因此不拿这些时间与归属窗口或跨机 Step 排序，只要求全部带 offset、终态有 `finished_at`，并在 Agent 自身时钟域内满足 `created <= started <= finished <= updated`（`started_at` 可为空）。

证据文件不得保存 HTTP Header、Token、管理员 Cookie、密码或其他秘密。

## 3. 每轮采集步骤

1. 断电演练前记录 `window_started_at`，所有时间统一使用带 offset 的 RFC 3339 格式。
2. 记录随机的 Agent 开机次序。十个冷启动 AUTO Run 都必须最终 `SUCCEEDED` 且全部 Step `READY`；故障 Run 不得冒充其中一轮。
3. 每个场景另行记录自己的 `window_started_at/window_finished_at`。凡场景声明 `run_id`（或 CP 重启的 before/after Run ID），该 Run 必须单独导出，不得借用十轮成功 Run；`SINGLE_NODE_REBOOT` 的 AUTO Run 也不例外。
4. Run 到达 `SUCCEEDED / FAILED / UNKNOWN` 后，从 CP 导出完整 RecoveryRun JSON。`runs[]` 必须同时覆盖十轮 AUTO 和场景引用的全部 MANUAL/AUTO Run；未归属 Run、跨两个归属复用的 Run 都失败。
5. 对 Run 中每个非空 `operation_id`，根据 Step 的 `agent_id` 到对应 Agent 查询 `GET /api/v1/operations/{operation_id}`，保存原始响应并增加归属二选一及 `run_id/step_id/managed_service_id/observed_at` 包装字段。相同 Operation 的幂等重试响应只保留一份。
6. 在独立 `UNKNOWN_SERVICE_REJECTION` 场景窗口内，向 Agent 请求一个不在 allowlist 的 local id，把 `404 SERVICE_NOT_ALLOWLISTED` 记录为 `rejected_request`，并用该场景的 `scenario_exercise_id` 归属。不得把 Token/Header 写入证据。
7. 每个轮次或场景的 Run 和动作全部对账后记录对应 `window_finished_at`。确认所有声明窗口内没有遗漏 Run 或动作，再把两项完整性声明改为 `true`。
8. 十轮结束后重新导出 CP 服务集合到 `inventory`，确认 role 映射仍指向预期的 Agent/local id。

`scenario_exercises[]` 必须各自至少包含一次：

| `kind` | 结构化、可机检结果 | 仍需人工证据 |
|---|---|---|
| `CONTROL_PLANE_LAST` | 全部 Agent 的启动时间、CP 启动时间、全部节点登记时间和完整 120 秒后创建的独立 AUTO Run | 必需 |
| `AGENT_PROCESS_RESTART` | boot/epoch 不变、instance 改变、零新增 AUTO Run | 必需 |
| `SINGLE_NODE_REBOOT` | boot/epoch 改变；引用并导出该场景独立 AUTO Run；其他节点已 ACTIVE 服务无 Operation、仅 probe 后 READY | 必需 |
| `MISSING_NODE` | 用 `required_agent_ids` 固定必需节点集合；状态为 `WAITING_FOR_NODES`，缺失期间 Operation/AUTO Run ID 集合为空 | 必需 |
| `START_FAILURE` | 独立 MANUAL Run 的根 Step `FAILED`，start Operation `FAILED/REJECTED`，严格下游 `BLOCKED` | 不要求额外文件 |
| `PROBE_FAILURE` | 独立 MANUAL Run 的 start 成功或无需 start，probe 全失败，根 Step `FAILED`，下游 `BLOCKED` | 不要求额外文件 |
| `AGENT_DISCONNECT` | 独立 MANUAL Run 的根 Step/Run `UNKNOWN`，已知 Operation 也为 `UNKNOWN`，下游 `BLOCKED` | 必需，用于证明 UNKNOWN 原因确为断线 |
| `CONTROL_PLANE_RESTART` | 重启前后 `run_id/operation_id` 相同并最终终态 | 必需 |
| `UNKNOWN_SERVICE_REJECTION` | 引用唯一的 `404 SERVICE_NOT_ALLOWLISTED` ErrorResponse request ID | 不要求额外文件 |

十轮 `boot_order` 还必须至少出现三种不同排列。该检查只能证明记录具有变化，不能证明随机数来源。

## 4. 机器校验规则

| 规则 | 失败码 | 判定方式 |
|---|---|---|
| 至少十轮且编号连续 | `CAMPAIGN_MIN_ROUNDS`, `ROUND_NUMBER_SEQUENCE` | 统计 `rounds[]` |
| 十轮均为成功冷启动 | `COLD_ROUND_NOT_SUCCESSFUL` | 每轮唯一 AUTO Run 必须 `SUCCEEDED` 且全部 Step `READY` |
| 每轮 epoch 唯一 | `EPOCH_NOT_UNIQUE` | Campaign 内 epoch 不得重复 |
| `(group_id, epoch, AUTO)` 与 Run 唯一 | `AUTO_RUN_NOT_UNIQUE`, `AUTO_RUN_CARDINALITY` | 全部导出 Run 交叉比对 |
| Run 导出和唯一归属 | `SCENARIO_RUN_NOT_EXPORTED`, `UNASSIGNED_RUN`, `RUN_OWNERSHIP_AMBIGUOUS` | 十轮及每个场景引用的 MANUAL/AUTO Run 必须全部出现；每个 Run 只能归属一个轮次或场景 |
| Action 显式唯一归属 | Schema、`ACTION_OWNER_NOT_FOUND`, `ACTION_RUN_OWNERSHIP_MISMATCH`, `OPERATION_LINK_MISMATCH` | `cold_round_number` XOR `scenario_exercise_id`；Operation 必须引用同一归属下的 Run/Step |
| 归属窗口完整 | `RUN_OUTSIDE_OWNER_WINDOW`, `STEP_OUTSIDE_OWNER_WINDOW`, `ACTION_OUTSIDE_OWNER_WINDOW` | CP Run/Step/probe 与采集侧 `observed_at` 必须带 offset，并落在其冷启动轮或场景窗口内 |
| Agent Operation 时间自洽 | `OPERATION_TIME_INVALID` | 远端时间只在 Agent 自身时钟域检查 offset、终态完成时间与单调性；不得参与跨机或 owner-window 比较 |
| 三机和固定 role 链 | `MIN_AGENT_COUNT`, `REQUIRED_ROLE_CHAIN_MISSING` | inventory、member/dependency snapshot 交叉比对 |
| Kahn 拓扑层严格一致 | `RUN_GRAPH_INVALID`, `TOPOLOGY_LEVEL_INVALID` | 从依赖快照重新计算 |
| 上游 READY 前下游无活动 | `DEPENDENCY_NOT_READY`, `DEPENDENCY_ORDER_VIOLATION` | 只对比同属 CP 时钟域的上游 `finished_at` 与下游 Step/probe 时间；Agent Operation 时间不参与跨机排序 |
| FAILED/UNKNOWN 的全部可达下游均 BLOCKED | `FAILED_DESCENDANT_NOT_BLOCKED` | 对 DAG 做传递可达性检查 |
| READY 有成功 readiness | `READY_WITHOUT_PASSED_PROBE` | 最后一次 probe attempt 必须通过 |
| Operation 与 Step/allowlist 一致 | `OPERATION_EVIDENCE_MISSING`, `OPERATION_LINK_MISMATCH`, `OPERATION_TARGET_UNKNOWN` | 对账 ID、Agent、local id、动作和幂等键 |
| 未知服务不能产生 Operation | `UNKNOWN_SERVICE_NOT_REJECTED`, `UNKNOWN_SERVICE_NEGATIVE_TEST_MISSING`, `UNKNOWN_SERVICE_REJECTION_NOT_PROVEN` | 只接受归属同一个 `UNKNOWN_SERVICE_REJECTION` 场景的 `404 SERVICE_NOT_ALLOWLISTED` 负向证据 |
| 故障场景不能靠标签冒充 | `SCENARIO_COVERAGE_INCOMPLETE`, `SCENARIO_EXERCISE_MISSING` | 九类场景分别使用结构化判别模型并引用实际结果 |
| 无法仅从 API 推导的现场事实 | `MANUAL_PROOF_REQUIRED` | 必须带 artifact ref、SHA-256、复核人和复核时间；报告单列 `manual_proof_records[]` |
| 导出范围完整 | `RUN_EXPORT_INCOMPLETE`, `ACTION_EXPORT_INCOMPLETE` | `all_runs_in_declared_windows` 与 `all_action_attempts_in_declared_windows` 覆盖十轮和全部场景窗口；操作者明确声明，不是机器独立证明 |

Run 终态仍按冻结合同计算：任一 Step `UNKNOWN` 则 Run `UNKNOWN`；否则存在 `FAILED/BLOCKED` 则 Run `FAILED`；只有全部 Step `READY` 才是 `SUCCEEDED`。

## 5. 执行与结果

```powershell
.\dist-recovery\winsw-recovery-evidence-validator\winsw-recovery-evidence-validator.exe `
  .\acceptance\campaign-2026-07.json `
  --report .\acceptance\campaign-2026-07.report.json
```

源码仓库中的等价入口是 `python .\scripts\validate_recovery_evidence.py ...`；两种入口必须生成相同 Schema 和报告。

| 退出码 | 含义 |
|---:|---|
| `0` | Schema 和语义校验均通过，报告 `verdict=PASS` |
| `1` | JSON 可解析，但存在验收规则失败，报告 `verdict=FAIL` |
| `2` | 文件、JSON 或输入 Schema 无效，未执行语义判定 |

报告包含机器可读 `issues[]`、计数和 `manual_proof_records[]`。`PASS` 只说明 JSON 内部一致、可推导规则通过且所需人工证据元数据齐全；它不证明人工证据内容真实、不证明导出未被删改，也不替代故障演练评审。只有报告为 `PASS`、复核人逐项打开并核对人工证据、真实 WinSW/ACL 检查也通过，并由用户最终通读证据后，才允许把路线图状态改为“真实环境验收通过”。
