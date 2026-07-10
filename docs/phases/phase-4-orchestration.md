# Phase 4 — 依赖编排引擎（核心价值）

> 状态：**设计**。这是整个平台的核心目标：**理解服务间依赖，一键让一整套服务按正确顺序起来/停下**。建立在 Phase 2（Agent 动作）与 Phase 3（多机纳管）之上。

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
| 探针调度（tcp/http/process/cmd × startup/readiness/liveness） | 跨编排的复杂工作流 DSL（可后续） |
| 依赖分级 CRITICAL/DEGRADED/OPTIONAL | |
| 编排组定义/管理 + 即席选择 | |
| 编排运行状态机 + 拓扑图前端 | |

## 3. 数据模型（在 Phase 3 基础上新增）

- **dependencies**: `id, from_service_id, to_service_id, level(CRITICAL/DEGRADED/OPTIONAL)`
  - 语义：`from` 依赖 `to`。**所有级别的依赖均为拓扑先决边**——`to` 须先到达终态（`READY`/`FAILED`），`from` 才启动；**级别仅决定 `to` 终态为 `FAILED` 时 `from` 的去留**（见 §4.1 步 5）。
    - ⚠️ `OPTIONAL` **不是**"不等待、并行启动"的旁路——它仍参与分层等待上游到达终态，只是上游 `FAILED` 时不阻塞下游。若日后需要真正的 fire-and-forget 旁路（下游根本不等、与上游同时启动），需另设语义，不在当前模型内。
  - `from_service_id`/`to_service_id` 均引用 Phase 3 `services` 表的**代理主键**（已隐含机器身份，见 architecture §7.2），因此依赖可**跨机器**；不要用机器内的 `local_service_id`，否则无法唯一定位跨机上游。
- **probes**: `id, service_id, kind(tcp/http/process/cmd), target(host:port/url/进程名/命令), role(startup/readiness/liveness), interval, timeout, retries, expect(如 HTTP 200 / 输出含 PONG)`
- **orchestration_groups**: `id, name, description` —— 一个**命名的服务集合**（如"订单系统""基础中间件"），是"一键启停"的**作用单元**。
- **group_members**: `group_id, service_id(引用 services 代理主键，可跨机)`
  - **作用域语义**：编排在**组成员导出的子图**上做拓扑排序与执行。
  - 成员依赖了**组外**上游时：按依赖级别处理——`CRITICAL` 要求该外部上游**已 READY**，否则该分支阻塞并提示；**不自动拉起组外服务**（避免副作用外溢到用户未选择的范围）。
    - **"已 READY" 如何判定**：组外 CRITICAL 上游**不进 `run_steps`**（它不在本次执行序列里，只作为门前**只读门控**）。编排器对其做就绪校验：优先用其 readiness 探针（CP 从 `probes` 表取 spec，内联调目标机 Agent `POST /probe`）；该服务**无探针定义**时回退到其缓存状态（Phase 3 轮询镜像的 `ServiceState`，`ACTIVE` 视作就绪、其它视作未就绪）。校验未过 → 该成员步 `SKIPPED`（CRITICAL）并提示是哪个组外上游未就绪。
  - 除命名组外，也支持**即席选择**（拓扑图圈选一组节点触发一次性 run），复用同一子图排序逻辑；`orchestration_runs` 记录本次作用域（组 id 或即席节点集）。
- **orchestration_runs**: `id, name, group_id(可空，即席为空), scope(即席时的节点集快照), action(start/stop), status, started_at, finished_at, trigger(operator)`
- **run_steps**: `id, run_id, service_id(引用 services 代理主键), state, started_at, finished_at, message`
  - **编排步骤状态机**：`PENDING → STARTING → WAITING_READY → READY | FAILED | SKIPPED`。`FAILED` 后若该服务**未耗尽重试次数**（§4.3），回退 `STARTING` 重跑（start + 探针门控重新开始）；耗尽则定格 `FAILED`。
  - ⚠️ 此状态**不等于** architecture §7.4 的 `ServiceState`（服务生命周期枚举 ACTIVE/INACTIVE/…）。二者维度不同：`ServiceState` 描述服务当前实际态，run-step state 描述该服务在**本次编排运行中**的推进态；虽同含 `STARTING`/`FAILED` 字样，勿混用。

## 4. 编排算法

### 4.1 启动流程（拓扑分层 + 就绪门控）
1. 由 `dependencies` 构建**先决图**：存储语义是 `from 依赖 to`（`to` 必须先就绪）。为得到**启动顺序**，以**先决关系**建边 `to → from`（"`to` 是 `from` 的先决"，等价于把依赖边**反向**）。在此先决图上用 **Kahn 算法**做拓扑排序，同时**检测环**（有环则中止并高亮环上节点）。
   - ⚠️ **方向易错点**：切勿按存储边 `from→to` 直接跑 Kahn——那样"入度为 0"命中的是没有服务依赖它的**下游叶子**（如末端 Java 应用），会把它当第 0 层先启动，上游尚未就绪，正是本系统要避免的反向失败。务必在**先决图**（反向边）上排序。
2. 计算层级：先决图中**入度为 0**（= `depends_on` 为空）的服务为第 0 层，逐层剥离。**同层无先决关系 → 并行启动**。等价判据：某服务归入第 *k* 层，当且仅当其全部 `depends_on` 都已出现在第 0…k-1 层。
3. 对每个待启动服务：先查其即时状态（`GET /services/{id}/status`）。
   - **`NOT_INSTALLED`** → 该步直接置 `FAILED`，原因 `NOT_INSTALLED`（**编排只负责运行期启停，不替用户安装**——安装属部署动作，应在编排前由用户单独完成；见 §4.3）。
   - 否则调用其所在机器 Agent 的 `start` → 进入 `WAITING_READY` → 由探针调度器执行 **readiness 探针**（含 startup 宽限期）。
4. 就绪判定：readiness 通过 → `READY`，**放行其下游**；超时/失败 → `FAILED`。
5. **依赖分级决定下游行为**：
   - `CRITICAL` 上游 FAILED → 下游 `SKIPPED`（阻塞）。
   - `DEGRADED` 上游 FAILED → 下游**降级放行**（照常尝试启动，记录告警）。
   - `OPTIONAL` 上游 FAILED → 下游正常放行。
6. 全部终态后汇总运行结果。

### 4.2 停止流程
- 按**逆拓扑序**逐层停止（先停下游，再停被依赖的上游），每层可并行。可配置是否等待优雅停止（`stoptimeout`）。

### 4.3 其它
- **Dry-run（预演）**：只输出计划的启动/停止顺序与分层，不真正执行——上线前必备。**一致性保证**：dry-run 与真实执行**复用同一套 planner（拓扑排序/分层/环检测）**，仅在"调用 Agent 动作 + 探针门控"这一步短路；不得另写一份预演逻辑，否则无法满足 §8"顺序与真实执行一致"的判据。
- **部分失败与重试**：单服务可配置重试次数；运行级可选"遇 CRITICAL 失败即停"或"尽力而为"。
- **基础设施级失败（Agent 掉线 / 网络分区）**：run 期间某机 Agent 不可达时，其上待处理步骤置 `FAILED` 并附**原因区分**（`AGENT_UNREACHABLE` vs `NOT_INSTALLED` vs 服务自身启动/探针失败），按依赖级别向下游传播（CRITICAL→SKIPPED 等）；run 整体标记为部分失败。不做自动跨机迁移（非目标）。Agent 恢复后由运维**手动重跑**受影响子图（不自动续跑，避免与期间外部变更冲突）。
- **手动干预**：运行中允许跳过/重试某步。

## 5. 探针调度器

- 四类探针：
  - `tcp`：端口可连。
  - `http`：请求 URL 期望状态码/响应体（如 `/actuator/health` 200）。
  - `process`：进程存在。
  - `cmd`：执行命令并校验输出（**真实校验**，如 Redis `PING`→`PONG`、MySQL `SELECT 1`）。
- 三种角色：`startup`（慢启动宽限，期间不判失败）/`readiness`（门控下游）/`liveness`（运行期健康，失败触发恢复策略）。
  - **liveness 恢复策略（本阶段默认）**：默认仅**上报状态 + 触发告警**（告警通道见 Phase 5），**不**自动重启——自动自愈易与外部运维/编排产生竞争。可选的"失败达 N 次自动 restart"作为**每探针可配置项**，且**由目标机 Agent 本地执行**（贴近、避免 CP 跨机误判抖动）；CP 只记录并在拓扑图标红。自动重启是否联动下游（重启后重跑 readiness 门控）留作后续增强。
- **执行位置与归属**（详见 architecture §7.3）：探针**定义归 CP**（存 `probes` 表），**执行在 Agent**，分两种模型：
  - `readiness/startup`（编排门控）：**数据流**——编排器从 `probes` 表按 `service_id + role` 取出探针定义 → 将 spec **内联**进 `POST /probe` 请求体调用目标机 Agent → Agent 执行一次并返回结果（通过/失败/超时），**Agent 侧不驻留该探针定义**。即定义在 CP、执行在 Agent、调用时才内联，CP 与 Agent 都不需要为 readiness 探针做额外下发/存储。
  - `liveness`（运行期自愈）：其定义 + 恢复策略作为服务"编排档案"**下发并驻留 Agent**，由 Agent 自主周期执行——CP 宕机时仍能自愈，避免 CP 持续轮询全 fleet liveness 的扩展性问题。此下发是 §7.3 明确允许的编排范围内下发，非"配置主本下发"。
  - 本地类探针（process/cmd/本机端口）一律 Agent 侧执行（贴近、绕开防火墙）；纯 HTTP 探针默认也走 Agent 以统一网络视角。

## 6. 前端（拓扑图）

- **依赖拓扑图**：节点=服务（含机器归属），边=依赖（区分分级用不同线型/颜色）；节点按实时状态着色（就绪/启动中/失败/未安装）。
- **一键启动/停止整条链路**：选择一个**编排组**（定义见 §3）或即席圈选节点 → 触发 run → 实时展示各步状态推进。
- **环检测可视化**：存在环时高亮环上节点并阻止执行。
- 技术：React Flow 或 vis-network（Phase 3 已选 React 打底）。

## 7. 风险与权衡

- **就绪定义难统一**：不同中间件就绪信号差异大——用可配置探针 + 内置模板默认值（见 `architecture.md` 模板表）缓解。
- **跨机时钟/网络**：跨机探针受网络与防火墙影响；优先 Agent 本地探针。
- **编排幂等与并发**：同一编排组不允许并发 run（加运行锁）；重复触发返回进行中的 run。
- **状态漂移**：编排完成后服务可能被外部改动——liveness 探针 + Phase 3 轮询共同维持真实状态。

## 8. 验收判据

- 定义一条真实依赖链（如 mysql/redis → nacos → java 应用，跨 ≥2 台机器），**一键启动**能按 `mysql,redis → nacos → java` 顺序推进，每级就绪后才起下游，最终整链 READY。
- 制造上游 CRITICAL 失败，下游正确 `SKIPPED`；改为 DEGRADED 时下游降级放行。
- 构造环依赖，编排前被检测并阻止。
- 编排组内含一个**仅建配置、未 install** 的服务：一键启动时该步 `FAILED`（原因 `NOT_INSTALLED`），其 CRITICAL 下游 `SKIPPED`、DEGRADED 下游降级放行（编排不替用户安装）。
- Dry-run 输出的顺序与真实执行一致。

## 9. 验证方式

- 用本机多 Agent + 若干轻量假服务（如简单 HTTP server 模拟就绪延迟）搭测试链路。
- 单元测试覆盖拓扑排序、环检测、分级放行逻辑（纯算法，可脱离真实服务）。
- `/run` + `playwright` 走查拓扑图一键启动的端到端体验。
