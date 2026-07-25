# Phase 4 — 依赖编排引擎（核心价值）

> 状态：**设计**。这是整个平台的核心目标：**理解服务间依赖，一键让一整套服务按正确顺序起来/停下**。建立在 Phase 2（Agent 动作）与 Phase 3（多机纳管）之上。
> 开工前必须冻结 [实施基线](../implementation-baseline.md) §6 所列编排语义；本文件是该语义的权威细化。

## 1. 目标与价值

- 服务定义可声明 `depends_on[]`（**可跨机器**），构成有向依赖图。
- **一键启动整条链路**：按拓扑序分层并行启动，每个服务**就绪探针通过后才放行下游**。
- **一键停止**：按逆拓扑序停止。
- 依赖分级避免级联失败；循环依赖可检测并高亮。
- 前端提供**依赖拓扑图**（实时状态着色）与编排运行进度。

## 2. 范围

| In | Out |
|----|-----|
| 依赖图建模 + 拓扑排序 + 环检测 | 自动扩缩容 / 调度装箱（非目标） |
| 分层并行启动 + 就绪门控 + 逆序停止 | 容器化编排（K8s，非目标） |
| 探针调度（tcp/http/process/cmd_template × startup/readiness/liveness） | 跨编排的复杂工作流 DSL（可后续） |
| 依赖分级 CRITICAL/DEGRADED/OPTIONAL | |
| 编排组定义/管理 + 即席选择 | |
| 编排运行状态机 + 拓扑图前端 | |

## 3. 数据模型（在 Phase 3 基础上新增）

- **dependencies**: `id, from_service_id, to_service_id, level(CRITICAL/DEGRADED/OPTIONAL)`
  - 语义：`from` 依赖 `to`。**所有级别的依赖均为拓扑先决边**——`to` 须先到达终态（`READY`/`FAILED`），`from` 才启动；**级别仅决定 `to` 终态为 `FAILED` 时 `from` 的去留**（见 §4.1 步 5）。
    - ⚠️ `OPTIONAL` **不是**"不等待、并行启动"的旁路——它仍参与分层等待上游到达终态，只是上游 `FAILED` 时不阻塞下游。若日后需要真正的 fire-and-forget 旁路（下游根本不等、与上游同时启动），需另设语义，不在当前模型内。
  - `from_service_id`/`to_service_id` 均引用 Phase 3 `services` 表的**代理主键**（已隐含机器身份，见 architecture §7.2），因此依赖可**跨机器**；不要用机器内的 `local_service_id`，否则无法唯一定位跨机上游。
- **probes**: `id, service_id, kind(tcp/http/process/cmd_template), spec(JSON), role(startup/readiness/liveness), interval, timeout, retries, expect(JSON), recovery_policy`
  - `cmd_template` 只引用 Agent 预注册的探针模板并传结构化参数，禁止 CP 下发任意 shell 字符串。
  - v1 对 `(service_id, role)` 建唯一约束，即每个服务每种角色最多一个探针；多探针 `ALL/ANY` 组合不在首版范围。
- **orchestration_groups**: `id, name, description` —— 一个**命名的服务集合**（如"订单系统""基础中间件"），是"一键启停"的**作用单元**。
- **group_members**: `group_id, service_id(引用 services 代理主键，可跨机)`
  - **作用域语义**：编排在**组成员导出的子图**上做拓扑排序与执行。
  - 组外上游永不进入 `run_steps`、不获取租约、也不被自动启停；Planner 必须把边界边写入 scope 快照，并在放行下游前做只读即时判定。
  - **"已 READY" 如何判定**：优先调用目标机 Agent 的 readiness 探针；无探针时即时查询 Agent 四维状态，只有 `InstallationState=INSTALLED + RuntimeState=ACTIVE` 可回退为 READY，不能使用无新鲜度约束的 CP 缓存。
  - 组外上游不可用时：`CRITICAL` 令组内下游 `SKIPPED`；`DEGRADED` 放行并产生 warning；`OPTIONAL` 放行并记录 info。三种级别都不自动拉起组外服务。
  - 除命名组外，也支持**即席选择**（拓扑图圈选一组节点触发一次性 run），复用同一子图排序逻辑；`orchestration_runs` 记录本次作用域（组 id 或即席节点集）。
- **orchestration_runs**: `id, name, group_id(可空，即席为空), scope(节点与依赖快照), action(start/stop), status, policy_snapshot, started_at, finished_at, trigger(operator), heartbeat_at`
  - 运行状态：`PLANNING / RUNNING / CANCELLING / SUCCEEDED / SUCCEEDED_WITH_WARNINGS / PARTIALLY_SUCCEEDED / FAILED / CANCELLED / INTERRUPTED`。
- **run_steps**: `id, run_id, service_id(引用 services 代理主键), state, attempt, operation_id, reason_code, started_at, finished_at, message`
  - **启动步骤状态机**：`PENDING → STARTING → WAITING_STARTUP → WAITING_READY → READY | FAILED | SKIPPED | CANCELLED | OUTCOME_UNKNOWN`。
  - **停止步骤状态机**：`PENDING → STOPPING → WAITING_STOPPED → STOPPED | FAILED | SKIPPED | CANCELLED | OUTCOME_UNKNOWN`。
  - `run_steps` 保存当前汇总；每次实际尝试必须另写 **run_step_attempts**：`id, step_id, attempt_no, operation_id, state, error_code, started_at, finished_at, result_snapshot`。
  - 重试时先进入 `RETRYING` 并新增 attempt；只有上一 attempt 已确定失败时才可创建新 operation_id。终态 run/step 不反向跳转，人工继续或重试创建关联的新 run。
  - ⚠️ 此状态**不等于** architecture §7.4 的 `RuntimeState`。二者维度不同：`RuntimeState` 描述服务当前实际态，run-step state 描述该服务在**本次编排运行中**的推进态；虽同含 `STARTING`/`FAILED` 字样，勿混用。

### 3.1 执行租约

- 锁定对象是 **service_id**，不是编排组。不同组或即席 run 只要作用域重叠，就不能并发执行冲突动作。
- Planner 成功后按稳定顺序一次性申请本次作用域的服务租约；获取失败则不启动任何步骤，并返回占用它的 run。
- Planner 为每个 attempt 生成并先持久化一个 UUIDv4 `operation_id`；同一 attempt 的网络重试沿用该 UUID，只有确定失败后创建的新 attempt 才生成新 UUID。超时后先查询既有结果，不盲目重复动作。
- 租约有心跳和过期时间；CP 重启后先执行恢复扫描，不能直接把过期租约视为动作未执行。

## 4. 编排算法

### 4.1 启动流程（拓扑分层 + 就绪门控）
1. 由 `dependencies` 构建**先决图**：存储语义是 `from 依赖 to`（`to` 必须先就绪）。为得到**启动顺序**，以**先决关系**建边 `to → from`（"`to` 是 `from` 的先决"，等价于把依赖边**反向**）。在此先决图上用 **Kahn 算法**做拓扑排序，同时**检测环**（有环则中止并高亮环上节点）。
   - ⚠️ **方向易错点**：切勿按存储边 `from→to` 直接跑 Kahn——那样"入度为 0"命中的是没有服务依赖它的**下游叶子**（如末端 Java 应用），会把它当第 0 层先启动，上游尚未就绪，正是本系统要避免的反向失败。务必在**先决图**（反向边）上排序。
2. 计算层级：先决图中**入度为 0**（= `depends_on` 为空）的服务为第 0 层，逐层剥离。**同层无先决关系 → 并行启动**。等价判据：某服务归入第 *k* 层，当且仅当其全部 `depends_on` 都已出现在第 0…k-1 层。
3. 对每个待启动服务：先查其即时状态（`GET /api/v1/services/{id}/status`）。
   - **`InstallationState == NOT_INSTALLED`** → 该步直接置 `FAILED`，原因 `NOT_INSTALLED`（**编排只负责运行期启停，不替用户安装**——安装属部署动作，应在编排前由用户单独完成；见 §4.3）。
   - **`InstallationState == UNKNOWN` 或 `RuntimeState == UNKNOWN`** → `FAILED/STATE_UNKNOWN`，不得盲发 start。
   - **已经 `ACTIVE`** → 不重复调用 start，但仍执行 readiness；通过后计为 `READY`，并标记 `already_active=true`。
   - `STARTING` → 接管观察，不重复发 start；`STOPPING` → 有界等待稳定，转 `INACTIVE` 后才可 start；等待超时均失败。
   - `INACTIVE/FAILED` 仅在 `ConfigState=CURRENT/RESTART_REQUIRED` 时调用 Agent `start`；`INVALID/DRIFTED/UNKNOWN` 直接失败。
4. 探针顺序固定为：start Operation 确定成功 → startup 通过 → readiness 通过 → `READY` 并放行下游。已 `ACTIVE` 的服务跳过 startup，但仍执行 readiness。
   - 服务无 readiness 探针时，首版统一回退为：即时 `RuntimeState == ACTIVE` 即 READY，并在 run 中记录 `READINESS_FALLBACK_ACTIVE` 警告。生产关键服务应在 UI 中持续提示补充真实探针。
5. **依赖分级决定下游行为**：
   - `CRITICAL` 上游 FAILED → 下游 `SKIPPED`（阻塞）。
   - `DEGRADED` 上游 FAILED → 下游**降级放行**（照常尝试启动，记录告警）。
   - `OPTIONAL` 上游 FAILED → 下游正常放行。
6. 全部终态后按以下优先级汇总，命中后不再继续判断：
   1. 存在 `OUTCOME_UNKNOWN` → `INTERRUPTED`。
   2. 取消已生效且全部在途动作已对账 → `CANCELLED`。
   3. 所有组内步骤到达目标态且无 warning → `SUCCEEDED`。
   4. 所有组内步骤到达目标态，但存在 readiness fallback 或组外 `DEGRADED` 不可用 → `SUCCEEDED_WITH_WARNINGS`。
   5. 至少一个组内步骤成功且至少一个 `FAILED/SKIPPED` → `PARTIALLY_SUCCEEDED`。
   6. 没有组内步骤成功且存在 `FAILED/SKIPPED` → `FAILED`。
   - `SUCCEEDED_WITH_WARNINGS` 不得包含组内 `FAILED/SKIPPED`；组内 OPTIONAL 上游自身失败仍属于部分失败，不能被依赖级别“洗成成功”。
   - 组外 OPTIONAL 不可用只记录 info，不把 run 升为 warning。
   - `SKIPPED` 必须保存首个根因及完整依赖链，不能只写“上游失败”。

### 4.2 停止流程
- 按**逆拓扑序**逐层停止（先停下游，再停被依赖的上游），每层可并行。可配置是否等待优雅停止（`stoptimeout`）。
- 停止某个组前，检查组外服务是否依赖组内目标。组外 CRITICAL 消费者处于 `ACTIVE/STARTING/STOPPING/UNKNOWN` 均阻止停止其组内上游；DEGRADED/OPTIONAL 允许停止，但 dry-run 必须展示影响。
- 某个下游停止失败时，默认不继续停止其 CRITICAL 上游，避免破坏仍在运行的下游；“尽力停止”必须作为显式策略并在 dry-run 中展示风险。
- 停止前即时状态为 `UNKNOWN` 时失败且不盲发；`NOT_INSTALLED/INACTIVE` 视为幂等 `STOPPED`；`STARTING/STOPPING` 有界等待稳定后重新判定。
- 每个停止动作推进 `STOPPING → WAITING_STOPPED`，以即时 `RuntimeState == INACTIVE` 作为 `STOPPED` 判据；取消和崩溃恢复同样通过 `operation_id` 与即时状态对账。

### 4.3 其它
- **Dry-run（预演）**：只输出计划的启动/停止顺序与分层，不真正执行——上线前必备。**一致性保证**：dry-run 与真实执行**复用同一套 planner（拓扑排序/分层/环检测）**，仅在"调用 Agent 动作 + 探针门控"这一步短路；不得另写一份预演逻辑，否则无法满足 §8"顺序与真实执行一致"的判据。
- **部分失败与重试**：默认每个动作只尝试一次；仅错误体 `retryable=true` 的确定失败可按策略创建新 attempt。网络重试沿用原 operation_id，不算新 attempt。
- **基础设施级失败（Agent 掉线 / 网络分区）**：run 期间某机 Agent 不可达时，其上待处理步骤置 `FAILED` 并附**原因区分**（`AGENT_UNREACHABLE` vs `NOT_INSTALLED` vs 服务自身启动/探针失败），按依赖级别向下游传播（CRITICAL→SKIPPED 等）；run 整体标记为部分失败。不做自动跨机迁移（非目标）。Agent 恢复后由运维**手动重跑**受影响子图（不自动续跑，避免与期间外部变更冲突）。
- **取消**：用户取消后 run 进入 `CANCELLING`，不再启动新步骤；未开始步骤转 `CANCELLED`，已发动作必须用原 operation_id 对账。超时仍无法确定时 step=`OUTCOME_UNKNOWN`、run=`INTERRUPTED`，不能伪装为 `CANCELLED`。首版不自动回滚。
- **崩溃恢复**：CP 启动时扫描非终态 run。对每个步骤结合租约、`operation_id` 查询结果和服务即时状态进行对账：可证明完成则继续；可证明未执行则重新调度；无法证明则将 run 标记 `INTERRUPTED` 并要求人工选择继续或结束。禁止静默从头重跑。
- **手动干预**：终态 run 保持不可变。对 `FAILED` 重试或从 `INTERRUPTED` 继续时创建新 run，并记录 `retry_of_run_id/resumes_run_id`；`SKIPPED` 需先解决根因再重新规划。不得强制标记成功。
- **回滚**：首版不自动回滚，因为停止本次启动的服务可能影响编排外消费者。结果页提供“本次改变了哪些服务”的清单；自动补偿作为后续独立设计。
- **软删除节点**：外部删除的服务保留代理主键、依赖边和组成员，并显示为 ghost/tombstone。新 run 的组内 scope 含 tombstone 时，Planner 在取租约前失败；只作为组外依赖时按三级“不可用”语义处理。已开始 run 始终使用 scope 快照，执行中消失记 `SERVICE_MISSING`。

## 5. 探针调度器

- 四类探针：
  - `tcp`：端口可连。
  - `http`：请求 URL 期望状态码/响应体（如 `/actuator/health` 200）。
  - `process`：进程存在。
  - `cmd_template`：执行 Agent 预注册的受限命令模板并校验输出（如 Redis `PING`→`PONG`、MySQL `SELECT 1`）；不接受任意命令文本。
- 三种角色：`startup`（新启动时先通过）/`readiness`（startup 后门控下游）/`liveness`（运行期健康）。
  - `/probe` 每次只执行一次；CP 负责 `period_seconds/timeout_seconds/max_attempts/overall_deadline_seconds` 调度，Agent 不驻留 readiness/startup 定义。
  - **liveness 恢复策略**：默认 `REPORT_ONLY`，只产生健康事件并标红。显式 `RESTART` 必须配置冷却时间、滚动窗口、窗口内最大重启次数和 intentional-stop 抑制，并与手工/编排动作共用 Agent 服务写锁。
- **执行位置与归属**（详见 architecture §7.3）：探针**定义归 CP**（存 `probes` 表），**执行在 Agent**，分两种模型：
  - `readiness/startup`（编排门控）：**数据流**——编排器从 `probes` 表按 `service_id + role` 取出探针定义 → 将 spec **内联**进 `POST /api/v1/probe` 请求体调用目标机 Agent → Agent 执行一次并返回结果（通过/失败/超时），**Agent 侧不驻留该探针定义**。
  - `liveness`（运行期检测）：其定义 + 恢复策略作为独立版本化档案下发并驻留 Agent，由 Agent 自主周期执行；它不进入 `ServiceConfig` 或 XML/unit。线上格式使用 Phase 2 已冻结的 liveness-profile 合同。
    - CP 保存 desired revision/hash；Agent 回报 applied revision/hash。更新、禁用和删除均通过带 revision 的档案同步（删除使用 tombstone），重连时以 CP desired 为准重新对账；未确认 applied 前 UI 标记 `LIVENESS_PROFILE_PENDING`，不得宣称策略已生效。
  - 本地类探针（process/cmd_template/本机端口）一律 Agent 侧执行（贴近、绕开防火墙）；纯 HTTP 探针默认也走 Agent 以统一网络视角。

## 6. 前端（拓扑图）

- **依赖拓扑图**：节点=服务（含机器归属），边=依赖（区分分级用不同线型/颜色）；节点按实时状态着色（就绪/启动中/失败/未安装）。
- **一键启动/停止整条链路**：选择一个**编排组**（定义见 §3）或即席圈选节点 → 触发 run → 实时展示各步状态推进。
- **环检测可视化**：存在环时高亮环上节点并阻止执行。
- 技术：React Flow 或 vis-network（Phase 3 已选 React 打底）。

## 7. 风险与权衡

- **就绪定义难统一**：不同中间件就绪信号差异大——用可配置探针 + 内置模板默认值（见 `architecture.md` 模板表）缓解。
- **跨机时钟/网络**：跨机探针受网络与防火墙影响；优先 Agent 本地探针。
- **编排幂等与并发**：以服务租约阻止任何重叠作用域的冲突 run；重复请求通过客户端请求 ID 返回已有 run。
- **状态漂移**：编排完成后服务可能被外部改动——liveness 探针 + Phase 3 轮询共同维持真实状态。

## 8. 验收判据

- 定义一条真实依赖链（如 mysql/redis → nacos → java 应用，跨 ≥2 台机器），**一键启动**能按 `mysql,redis → nacos → java` 顺序推进，每级就绪后才起下游，最终整链 READY。
- 制造上游 CRITICAL 失败，下游正确 `SKIPPED`；改为 DEGRADED 时下游降级放行。
- 构造环依赖，编排前被检测并阻止。
- 编排组内含一个**仅建配置、未 install** 的服务：一键启动时该步 `FAILED`（原因 `NOT_INSTALLED`），其 CRITICAL 下游 `SKIPPED`、DEGRADED 下游降级放行（编排不替用户安装）。
- Dry-run 输出的顺序与真实执行一致。
- 两个不同组包含同一服务时，冲突 run 在执行前被拒绝；CP 在 `WAITING_READY` 期间重启后能正确恢复或明确标记 `INTERRUPTED`。
- 取消运行后不再启动新步骤，结果页准确列出已改变和未改变的服务；停止时不会误停仍被组外 CRITICAL 依赖使用的服务。

## 9. 验证方式

- 用本机多 Agent + 若干轻量假服务（如简单 HTTP server 模拟就绪延迟）搭测试链路。
- 单元测试覆盖拓扑排序、环检测、分级放行逻辑（纯算法，可脱离真实服务）。
- `/run` + `playwright` 走查拓扑图一键启动的端到端体验。
