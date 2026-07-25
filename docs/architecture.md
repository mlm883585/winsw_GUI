# 分布式服务编排管理平台 — 架构设计

> 本文档描述在现有 `winsw_GUI` 基础上，演进为**跨机器、带依赖编排、一键操作**的服务管理平台的目标架构。演进分阶段落地，见 [roadmap.md](./roadmap.md)。
> 产品边界、`ServiceConfig v1`、配置生命周期和最低安全门槛见 [implementation-baseline.md](./implementation-baseline.md)。
> 当前优先交付的是插入原 Phase 2 之前的 **Recovery MVP v1**：仅编排已注册的 Windows Service；其冻结合同见 [recovery-mvp-v1.md](./contracts/recovery-mvp-v1.md)，线上格式分别见 [Agent OpenAPI](./api/recovery-agent-openapi.yaml) 与 [Control Plane OpenAPI](./api/recovery-control-plane-openapi.yaml)。原 Phase 2–5 的 ServiceConfig/XML 接管路线完整保留，作为 MVP 之后的增强。

## 1. 背景与目标

### 现状
当前 `winsw_GUI` 是一个**纯本机、Tkinter** 的 WinSW 图形前端：
- 图形化编辑 WinSW XML（9 个维度：基本信息/执行/环境变量/日志/恢复/账户/高级/XML 源码/日志查看）。
- 对单个或多选服务做安装/卸载/启动/停止/重启/状态/刷新（Phase 1 代码已完成，待真实环境验收）。
- 核心资产：`core/winsw_manager.py`（调用 WinSW.exe）、`core/config_manager.py`（XML↔dict 双向转换）。

**缺失的目标能力**：多机器管理、服务间依赖编排、批量一键操作。

### 目标
演进为一个**跨机器**、能管理 java / nodejs / nacos / mysql / redis / nginx 等异构服务、**理解服务间依赖关系**、并提供**一键编排（按依赖顺序启停）** 的 Web 控制平台。

### 关键决策
| 维度 | 决策 |
|------|------|
| 目标平台 | 主要 Windows（WinSW），少量 Linux（systemd 适配，后期） |
| 架构模式 | **Agent 常驻**：每机器一个轻量 Agent，中心控制端下发指令 |
| 控制端形态 | **转 Web 控制台**（FastAPI 后端 + Web 前端）；Tkinter 逻辑下沉为 Agent 能力 |
| 演进策略 | 渐进式，基于现有代码分阶段落地 |

## 2. 业界参考

- **服务包装器**：WinSW（现有，v3.x）、[Servy](https://servy-win.github.io/)（可视化依赖树 + 生命周期钩子）、NSSM、Shawl。它们的单机依赖用的是 Windows SCM `depend=` 机制——**只解决同机依赖**。
- **跨机编排**：这些包装器都不原生解决跨机顺序。业内做法是 **控制平面 + Agent**（Kubernetes / Nomad / HashiCorp 模式），或 Agentless（Ansible / PowerShell DSC / WinRM）。本方案取 **Agent 模式**。
- **依赖门控（借鉴 K8s 探针三分法）**：
  - `startup`：慢启动保护。
  - `readiness`：能否对外服务——**用于门控下游是否放行**。
  - `liveness`：检测运行期健康；是否重启由独立恢复策略决定。
  - 依赖分级 `CRITICAL / DEGRADED / OPTIONAL`，避免"全部当强依赖"导致级联失败。
  - 就绪探针要做**真实校验**（Redis `PING`、MySQL `SELECT 1`、HTTP 200），而非仅"端口能连"。

## 3. 目标架构

```
                    ┌───────────────────────────────────────┐
                    │        Control Plane (Web 控制台)        │
                    │  FastAPI 后端 + Web 前端                  │
                    │  ┌─────────────┐  ┌──────────────────┐  │
                    │  │ 编排引擎     │  │ 拓扑图 / 一键操作 │  │
                    │  │ DAG+探针门控 │  │ 服务列表 / 日志   │  │
                    │  └─────────────┘  └──────────────────┘  │
                    │  存储: 机器/服务/依赖/状态 (SQLite→PG)   │
                    └───────┬──────────────┬──────────────┬───┘
                     HTTP/token(轮询+心跳)  │              │
              ┌─────────────┘        ┌─────┘        ┌─────┘
      ┌───────▼────────┐    ┌────────▼───────┐  ┌───▼────────────┐
      │ Agent @ 机器A   │    │ Agent @ 机器B  │  │ Agent @ 机器C   │
      │ (Windows/WinSW) │    │ (Windows)      │  │ (Linux/systemd) │
      │  install/start  │    │  nacos, redis  │  │  mysql, nginx   │
      │  /stop/status   │    │                │  │                 │
      │  /logs/health   │    │                │  │                 │
      │  java, nodejs   │    │                │  │                 │
      └────────────────┘    └────────────────┘  └────────────────┘
```

### 3.0 当前实施纵切：Recovery MVP v1

Recovery MVP 不试图提前实现完整目标架构，而是先闭环“Windows 多机断电后，已有服务按严格依赖顺序恢复”这一条纵切：

| 边界 | MVP 冻结取值 |
|------|-------------|
| 工作负载 | 由现有 GUI 或原生安装器预先注册为 Windows Service，且必须为手动启动；Agent/CP 自身为自动启动 |
| Agent 能力 | 固定 allowlist、SCM 状态与 `start/stop/restart`、持久化 Operation、`scm/tcp/http` 单次探针；不提供任意命令或文件执行 |
| 状态镜像 | `ObservedService` 只含服务身份、`InstallationState`、`RuntimeState`、`StartupState` 和观察时间；不伪造 `ConfigState=CURRENT` |
| 节点发现 | Agent 每 10 秒（±20% jitter）主动向 CP 心跳；CP 只以自己的 `received_at` 判定在线，45 秒无心跳即离线 |
| 恢复编排 | CP 等待组内全部必需 Agent 在线及 120 秒汇聚窗口，再按严格 DAG 分层启动；readiness 未通过绝不放行下游 |
| 部署准备 | 非秘密 Deployment Inventory 严格校验独立 CP、至少三 Agent、服务映射、本机 readiness 与验收 DAG；Host Facts 只读采集本机事实。渲染配置固定 `config_ready=false` 和无效 secret sentinel，目标机本地注入并通过权威检查前不得安装 |
| 控制台 | FastAPI + Jinja2 + 本地静态 JavaScript 的三页最小 Web 界面，不做拖拽拓扑和 React |
| 通信例外 | 普通受控局域网内使用 HTTP + 共享实验 Token + CP 来源 IP 限制；这是仅限 MVP/实验环境的显式例外，不满足生产安全基线 |
| 配置权威 | MVP 不实现 ServiceConfig CRUD、不读取或接管 WinSW XML；原 Phase 2 的 Agent SQLite 配置主本与派生 XML 方案延后实施 |

Control Plane 必须部署在独立管理节点，其 SQLite 不依赖任何被编排的 MySQL。恢复组涉及的所有 Agent 都是必需节点；缺少任一节点时保持等待，不执行部分子图。

### 3.1 Agent（每台机器常驻，自身注册为 WinSW/systemd 服务）
- 现有 `core/winsw_manager.py` + `core/config_manager.py` 仅作为行为参考和旧 XML 迁移解析器；Phase 2 必须重构结构化命令执行、绝对路径、超时、原子渲染和错误语义。
- 暴露版本化 HTTPS API：服务 CRUD、四维状态、增量日志和 `install / uninstall / start / stop / restart / enable-autostart / disable-autostart` 七个异步动作；精确合同见 [Agent OpenAPI](./api/agent-openapi.yaml)。
- 平台适配层：`WindowsWinSWBackend`（Phase 2）/ `LinuxSystemdBackend`（Phase 5）。配置、Operation、写锁和秘密不属于 backend 职责。
- Agent SQLite 是 `ServiceConfig`、revision、Operation 和 journal 的唯一权威主本；WinSW XML/systemd unit 是受管派生物。
- 状态上报：原 Phase 2/3 路线**以 CP 轮询** `/healthz`、鉴权的 `/api/v1/agent` 和 `/api/v1/services` 为准（见 [phase-3 §5](./phases/phase-3-control-plane.md)）；这不覆盖 §3.0 Recovery MVP 已冻结的 Agent 主动心跳合同。

### 3.2 Control Plane（Web 控制台）
- **后端 FastAPI**：机器清单、服务镜像、依赖图、状态聚合、编排引擎、探针调度、认证。CP 只透传版本化 `ServiceConfig`，不得生成或解析各机 XML/unit。
- **前端**：依赖拓扑图（可视化 `depends_on` + 实时状态着色）、服务列表（多选批量）、一键启动/停止整条链路、集中日志。
- **存储**：起步 SQLite（单文件、零运维），规模上来换 PostgreSQL。

### 3.3 编排引擎（核心增量）
- 服务定义 `depends_on[]`（**可跨机器**），构成有向图。
- 一键启动 = 拓扑排序 → 分批（同层并行）→ **每个服务就绪探针通过后才放行下游**。
- 一键停止 = 逆拓扑序。
- 依赖分级 `CRITICAL/DEGRADED/OPTIONAL` 决定"下游是否阻塞/降级放行"。
- 循环依赖检测（拓扑排序失败即报错并高亮环）。

## 4. 数据模型（核心表）

> 本节是**目标概念视图**，把一个服务相关的信息画在一起便于理解；**数据归属与落地表结构以 §7.3（职责边界）+ 对应 Phase 文档为准**：
> - `depends_on[]`、`probes[]` 归 **CP**（Phase 4 独立 `dependencies` / `probes` 表），**不进服务 XML/unit**——不要据本图给 `ConfigManager` 增依赖/探针标签。
> - `config` 归 **Agent SQLite**（本地权威），CP 只存不可编辑镜像；XML/unit 只是 Agent backend 的派生物。
> - `service_id` 引用统一用 Phase 3 的 **CP 代理主键**（见 §7.2）。

- **Machine**: `id, name, host, os(win/linux), agent_endpoint, token, status, last_heartbeat`
- **Service**: `id, machine_id, name, type, config_revision/config_hash（镜像）, depends_on[]（引用 service_id + 依赖级别）, probes[]`
- **Probe**: `service_id, kind(tcp/http/process/cmd_template), spec, interval, timeout, retries, expect, role(startup/readiness/liveness)`（权威字段以 [phase-4 §3](./phases/phase-4-orchestration.md) 为准）
- **ServiceTypeTemplate**: CP 管理的不可变版本化模板，包含平台无关配置、结构化平台覆盖、表单元数据与默认探针 seed；Agent 只接收展开后的 `ServiceConfig`。

## 5. 内置服务模板

把现有 `templates/python.xml` 模式推广到各服务类型：

| 类型 | 启动方式（Windows/WinSW） | 默认就绪探针 |
|------|---------------------------|--------------|
| Java | `java -jar app.jar` | HTTP `/actuator/health` 或 TCP 端口 |
| Node.js | `node app.js` / npm | HTTP 端口 200 |
| Nacos | `startup.cmd` | HTTP `:8848/nacos/actuator/health` |
| MySQL | `mysqld` | `SELECT 1`（或 TCP 3306） |
| Redis | `redis-server` | `PING`（或 TCP 6379） |
| Nginx | `nginx.exe` | HTTP 端口 / `nginx -t` |
| Python | 现有 `templates/python.xml` | 自定义 |

## 6. 技术选型（已定向，细节实现阶段定）

- **Control Plane↔Agent 协议**：**HTTP/JSON**（[phase-2 §5](./phases/phase-2-agent.md) 已据此设计，统一 `/api/v1`、统一错误体）。编排运行进度等高频/流式场景再评估 SSE/WebSocket/gRPC（[phase-3 §8](./phases/phase-3-control-plane.md) 起步轮询→SSE）。
- **前端栈**：Recovery MVP 使用 **Jinja2 + 本地静态 JavaScript**，只交付 Dashboard、恢复组和运行详情；原 Phase 3/4 的目标 Web 控制台仍推荐 **React**（[phase-3 §8](./phases/phase-3-control-plane.md)），为后续拓扑图用 React Flow/vis-network 铺路。
- **认证**：Agent 侧静态 Bearer Token 起步 → Phase 5 mTLS（[phase-5 §4.4](./phases/phase-5-linux-hardening.md)）；CP 侧起步单管理员 + JWT/会话 → Phase 5 RBAC（[phase-3 §7](./phases/phase-3-control-plane.md)、[phase-5 §4.3](./phases/phase-5-linux-hardening.md)）。OIDC 为更远期可选项。

## 7. 跨阶段通用约定（Cross-cutting）

以下约定贯穿 Phase 2–5，各阶段设计文档共同遵循，集中在此避免重复与漂移。

### 7.1 通信协议约定
- 传输：HTTP/JSON，统一前缀 `/api/v1`；错误体统一 `{code, message, detail}`，HTTP 状态码语义化。
- 配置：公共 API 只接受版本化 JSON `ServiceConfig`；字段以 [ServiceConfig v1](./contracts/service-config-v1.md) 为准，WinSW XML/systemd unit 不作为 API 输入。
- 幂等：所有写请求携带 `Idempotency-Key: UUIDv4`。PUT/DELETE 同步完成并记录 Operation；七个生命周期动作返回 `202 + Operation`。重复达到目标态返回幂等成功。
- 超时与重试：CP→Agent 调用带超时；只读请求可重试。写操作携带 `operation_id`，只有 Agent 能确认未执行或能返回既有结果时才可重试；不确定结果进入人工确认。
- 线上路径、Header、Schema 和状态码只以 [Agent OpenAPI](./api/agent-openapi.yaml) 为准；行为状态机以 [Agent Protocol v1](./contracts/agent-protocol-v1.md) 为准。

### 7.2 命名与身份
- `service_id`：机器内唯一，作为 XML/unit 文件名与命令定位键（沿用现有 `services/{id}.xml` 约定）。
- 全局唯一定位：逻辑上是 `machine_id + service_id`（机器内唯一的 `service_id`）。**落地时 CP 用一个代理主键承载这一复合身份**（Phase 3 `services.id`，附 `(machine_id, local_service_id)` 唯一约束）；编排的依赖引用一律用该代理主键，天然携带机器身份、支持跨机。

### 7.3 配置权威性与职责边界（authority split）

明确"谁拥有什么数据"是避免双写冲突、也是避免后期返工的根本。数据按归属一分为二：

- **Agent 拥有：单服务运行配置**（`executable/arguments/env/log/account/onfailure/...`）。这是版本化的 `ServiceConfig`，以 Agent SQLite 为唯一权威源，由 backend 渲染为 WinSW XML（Windows）或 systemd unit（Linux）；CP 只存镜像/索引供聚合展示。
- **CP 拥有：跨机拓扑与编排元数据**（依赖 `dependencies`、探针定义、编排组、运行历史）。这些数据**本质跨机、无法归属任何单个 Agent**，故必然以 CP 为权威（Phase 3/4）。
  - ⚠️ 因此这些**不进服务的 XML/unit**：平台级 `depends_on`、`probes` 是 CP 表数据，不是 Agent 配置字段。§4/§5 中把它们画在 Service 上是**目标概念视图**，落地归属以本节为准。
  - 与 WinSW 原生 `<depend>` 区分：后者是**同机 OS 级**（SCM）依赖，若需要可由 backend 处理；平台的**跨机** `depends_on` 与之正交，两者不要混用。
- **探针是分工的中间态：定义归 CP，执行在 Agent 侧**，且有两种执行模型：
  - `readiness/startup`（编排门控）：CP 编排器按需调用 Agent `POST /api/v1/probe`，**探针 spec 随调用内联传入、Agent 不驻留**。
  - `liveness`（运行期检测）：作为独立于 ServiceConfig 的版本化档案下发给 Agent 自主执行。Phase 2 冻结 GET/PUT 合同但报告 capability=false，Phase 4 才实现；默认 `REPORT_ONLY`，自动 restart 必须显式配置冷却、窗口和次数上限。
- **写路径（服务创建/编辑）**：
  - ✅ 允许 **CP 透传式创作**：CP → Agent `PUT /api/v1/services/{id}`，Agent 仍是权威、CP **不保留可编辑主本**——Web 控制台据此提供新建/编辑能力（不构成双写）。
  - ⛔ **暂不做"CP 存可编辑主本、事后 push"**：易与 Agent 本地改动双写冲突，列为后续可选增强。
  - 新服务出现后，CP 经轮询 `/services`（Phase 3）发现并更新镜像；动作后触发即时刷新。

### 7.4 状态模型（四维响应）
- `ConfigState`：`CURRENT / RESTART_REQUIRED / INVALID / DRIFTED / UNKNOWN`。
- `InstallationState`：`INSTALLED / NOT_INSTALLED / UNKNOWN`。
- `RuntimeState`：`ACTIVE / INACTIVE / STARTING / STOPPING / FAILED / UNKNOWN`。
- `StartupState`：`AUTOSTART_ENABLED / AUTOSTART_DISABLED / START_BLOCKED / NOT_APPLICABLE / UNKNOWN`。
- Agent 服务详情和 CP 镜像分别保存四维状态；各 backend 负责解析平台原生输出。旧称 `ServiceState` 时仅指 `RuntimeState`，不得再用 `NOT_INSTALLED` 表示运行态。
- 配置、注册、运行和开机自启严格分离：install 默认手动启动；enable/disable-autostart 不隐式 start/stop；uninstall 保留 Agent 配置；DELETE 仅删除未安装服务的受管配置。

### 7.5 安全基线
- **Recovery MVP 有界例外**：仅在普通、受控、可信局域网的实验部署中允许 HTTP + 共享 Token，并限制 Agent 只接受 CP 来源 IP；不得将该模式标记或外推为生产可用。进入原 Phase 2/5 安全路线前必须恢复下述 TLS 等门槛。
- 起步：静态 Bearer Token + Agent 直终止 TLS + CIDR allowlist；默认仅绑定本机，非环回监听缺少任一项即拒绝启动。Agent SQLite 中的秘密使用 Windows DPAPI + ACL，派生 XML 不可避免的明文使用最小 ACL并明确告警。Token 在 CP 侧加密存储，不明文落库/入日志。
- 生产（Phase 5）：CP↔Agent mTLS、Token 轮转、敏感字段（服务账户密码）加密、CP 侧 RBAC。
- `cmd_template` 探针只允许 Agent 预注册模板及结构化参数，不接受任意 shell 字符串；详细基线见实施基线 §8。

### 7.6 依赖分级语义（Phase 4 起）
- 三级依赖均为**拓扑先决边**（上游到达终态 `READY`/`FAILED` 才放行下游）；级别**仅在上游终态为 `FAILED` 时**区分下游去留：
- `CRITICAL`：上游 `FAILED` → 下游阻塞（`SKIPPED`）。
- `DEGRADED`：上游 `FAILED` → 下游降级放行并告警。
- `OPTIONAL`：上游 `FAILED` → 下游照常放行。
- 详细算法见 [phase-4 §4.1](./phases/phase-4-orchestration.md)。

### 7.7 探针约定（Phase 4 起）
- 类型 `tcp/http/process/cmd_template`；角色 `startup/readiness/liveness`。
- **定义归 CP、执行在 Agent**（见 §7.3）：`readiness/startup` 由 CP 调用时内联传 spec；`liveness` 下发给 Agent 自主执行。
- 就绪判定做**真实校验**（Redis `PING`、MySQL `SELECT 1`、HTTP 200），非仅端口连通。
- 本地类探针优先由**目标机 Agent 执行**（贴近、绕开防火墙）。
