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
- 主动向 Control Plane 上报心跳 + 服务状态（缓解远程模式的状态弱项）。

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

- **Machine**: `id, name, host, os(win/linux), agent_endpoint, token, status, last_heartbeat`
- **Service**: `id, machine_id, name, type, config(XML/unit), depends_on[]（引用 service_id + 依赖级别）, probes[]`
- **Probe**: `service_id, kind(tcp/http/process/cmd), target, interval, timeout, retries, role(startup/readiness/liveness)`
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

## 6. 待决技术选型（实现阶段再定）

- **Control Plane↔Agent 协议**：HTTP/JSON（起步，简单）vs gRPC（后期高频状态流）。
- **前端栈**：轻量（原生/Alpine）vs React（拓扑图用 React Flow / vis-network）。
- **认证**：静态 token（起步）→ mTLS / OIDC（生产）。
