# 分布式服务编排管理平台 — 架构设计

> 本文档描述在现有 `winsw_GUI` 基础上，演进为**跨机器、带依赖编排、一键操作**的服务管理平台的目标架构。演进分阶段落地，见 [roadmap.md](./roadmap.md)。

## 1. 背景与目标

### 现状
当前 `winsw_GUI` 是一个**纯本机、单服务、Tkinter** 的 WinSW 图形前端：
- 图形化编辑 WinSW XML（9 个维度：基本信息/执行/环境变量/日志/恢复/账户/高级/XML 源码/日志查看）。
- 对**单个**服务做安装/卸载/启动/停止/重启/状态/刷新。
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
  - `liveness`：是否需要重启。
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

### 3.1 Agent（每台机器常驻，自身注册为 WinSW/systemd 服务）
- 复用现有 `core/winsw_manager.py` + `core/config_manager.py` 作为 Windows 后端。
- 暴露本地 HTTP API（token 认证）：`install / uninstall / start / stop / restart / status / logs(增量) / health / describe`。
- 平台适配层（策略模式）：`WindowsWinSWBackend`（现有逻辑）/ `LinuxSystemdBackend`（后期）。
- 状态上报：**起步以 CP 轮询** `/health` + `/services` 为准（见 [phase-3 §5](./phases/phase-3-control-plane.md)）；Agent **可选**主动上报心跳以降低状态延迟（[phase-2 §6](./phases/phase-2-agent.md) 标记为可选增强，非必需）。

### 3.2 Control Plane（Web 控制台）
- **后端 FastAPI**：机器清单、服务清单、依赖图、状态聚合、编排引擎、探针调度、认证。可复用 `ConfigManager` 生成/解析各机 XML。
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
> - `config` 归 **Agent**（本地权威），CP 只存镜像。
> - `service_id` 引用统一用 Phase 3 的 **CP 代理主键**（见 §7.2）。

- **Machine**: `id, name, host, os(win/linux), agent_endpoint, token, status, last_heartbeat`
- **Service**: `id, machine_id, name, type, config(XML/unit), depends_on[]（引用 service_id + 依赖级别）, probes[]`
- **Probe**: `service_id, kind(tcp/http/process/cmd), target, interval, timeout, retries, expect(如 HTTP 200 / 输出含 PONG), role(startup/readiness/liveness)`（权威字段以 [phase-4 §3](./phases/phase-4-orchestration.md) 为准）
- **ServiceTypeTemplate**: 内置各类型的默认启停方式 + 默认就绪探针（见下）

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
- **前端栈**：**React**（[phase-3 §8](./phases/phase-3-control-plane.md) 推荐，为 phase-4 拓扑图用 React Flow/vis-network 铺路）。
- **认证**：Agent 侧静态 Bearer Token 起步 → Phase 5 mTLS（[phase-5 §4.4](./phases/phase-5-linux-hardening.md)）；CP 侧起步单管理员 + JWT/会话 → Phase 5 RBAC（[phase-3 §7](./phases/phase-3-control-plane.md)、[phase-5 §4.3](./phases/phase-5-linux-hardening.md)）。OIDC 为更远期可选项。

## 7. 跨阶段通用约定（Cross-cutting）

以下约定贯穿 Phase 2–5，各阶段设计文档共同遵循，集中在此避免重复与漂移。

### 7.1 通信协议约定
- 传输：HTTP/JSON，统一前缀 `/api/v1`；错误体统一 `{code, message, detail}`，HTTP 状态码语义化。
- 幂等：配置写入（`PUT`）幂等；`start`/`stop` 对已处于目标态的服务返回幂等成功。
- 超时与重试：CP→Agent 调用带超时；只读/幂等请求可重试，动作类请求不自动重试（避免重复副作用）。

### 7.2 命名与身份
- `service_id`：机器内唯一，作为 XML/unit 文件名与命令定位键（沿用现有 `services/{id}.xml` 约定）。
- 全局唯一定位：逻辑上是 `machine_id + service_id`（机器内唯一的 `service_id`）。**落地时 CP 用一个代理主键承载这一复合身份**（Phase 3 `services.id`，附 `(machine_id, local_service_id)` 唯一约束）；编排的依赖引用一律用该代理主键，天然携带机器身份、支持跨机。

### 7.3 配置权威性与职责边界（authority split）

明确"谁拥有什么数据"是避免双写冲突、也是避免后期返工的根本。数据按归属一分为二：

- **Agent 拥有：单服务运行配置**（`executable/arguments/env/log/account/onfailure/...`）。这是**平台无关的 dict**，由各 backend 渲染为 WinSW XML（Windows）或 systemd unit（Linux）。**Agent 本地为唯一权威源**；CP 只存镜像/索引供聚合展示。
- **CP 拥有：跨机拓扑与编排元数据**（依赖 `dependencies`、探针定义、编排组、运行历史）。这些数据**本质跨机、无法归属任何单个 Agent**，故必然以 CP 为权威（Phase 3/4）。
  - ⚠️ 因此这些**不进服务的 XML/unit**：平台级 `depends_on`、`probes` 是 CP 表数据，不是 Agent 配置字段。§4/§5 中把它们画在 Service 上是**目标概念视图**，落地归属以本节为准。
  - 与 WinSW 原生 `<depend>` 区分：后者是**同机 OS 级**（SCM）依赖，若需要可由 backend 处理；平台的**跨机** `depends_on` 与之正交，两者不要混用。
- **探针是分工的中间态：定义归 CP，执行在 Agent 侧**，且有两种执行模型：
  - `readiness/startup`（编排门控）：CP 编排器按需调用 Agent `POST /probe`，**探针 spec 随调用内联传入、Agent 不驻留**——最简，无下发。
  - `liveness`（运行期自愈）：作为服务"编排档案"的一部分**下发给 Agent 自主本地执行**（可在 CP 宕机时仍自愈、可扩展）。这是一次**边界清晰、编排范围内的下发**，与下条禁止的"配置离线主本再下发"不同。
- **写路径（服务创建/编辑）**：
  - ✅ 允许 **CP 透传式创作**：CP → Agent `PUT /services/{id}`，Agent 仍是权威、CP **不保留可编辑主本**——Web 控制台据此提供新建/编辑能力（不构成双写）。
  - ⛔ **暂不做"CP 存可编辑主本、事后 push"**：易与 Agent 本地改动双写冲突，列为后续可选增强。
  - 新服务出现后，CP 经轮询 `/services`（Phase 3）发现并更新镜像；动作后触发即时刷新。

### 7.4 状态模型（统一枚举）
- `ServiceState`：`ACTIVE / INACTIVE / STARTING / STOPPING / FAILED / NOT_INSTALLED / UNKNOWN`。各 backend 负责把平台原生输出（WinSW status 文本 / systemctl is-active）解析为该枚举。

### 7.5 安全基线
- 起步：静态 Bearer Token（Agent 侧校验）+ 端口/来源 IP 限制；Token 在 CP 侧加密存储，不明文落库/入日志。
- 生产（Phase 5）：CP↔Agent mTLS、Token 轮转、敏感字段（服务账户密码）加密、CP 侧 RBAC。

### 7.6 依赖分级语义（Phase 4 起）
- 三级依赖均为**拓扑先决边**（上游到达终态 `READY`/`FAILED` 才放行下游）；级别**仅在上游终态为 `FAILED` 时**区分下游去留：
- `CRITICAL`：上游 `FAILED` → 下游阻塞（`SKIPPED`）。
- `DEGRADED`：上游 `FAILED` → 下游降级放行并告警。
- `OPTIONAL`：上游 `FAILED` → 下游照常放行。
- 详细算法见 [phase-4 §4.1](./phases/phase-4-orchestration.md)。

### 7.7 探针约定（Phase 4 起）
- 类型 `tcp/http/process/cmd`；角色 `startup/readiness/liveness`。
- **定义归 CP、执行在 Agent**（见 §7.3）：`readiness/startup` 由 CP 调用时内联传 spec；`liveness` 下发给 Agent 自主执行。
- 就绪判定做**真实校验**（Redis `PING`、MySQL `SELECT 1`、HTTP 200），非仅端口连通。
- 本地类探针优先由**目标机 Agent 执行**（贴近、绕开防火墙）。
