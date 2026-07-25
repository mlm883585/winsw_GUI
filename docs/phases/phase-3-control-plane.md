# Phase 3 — Control Plane Web 骨架

> 状态：**设计**。目标是搭起中心控制端（Web 控制台），集中纳管多台机器上的 Agent，实现聚合查看与远程单服务操作。本阶段**尚不含依赖编排**（留给 Phase 4）。
> 数据同步、软删除和安全前置要求遵循 [实施基线](../implementation-baseline.md)。

## 1. 目标与价值

- 提供一个浏览器访问的中心控制台，是"一键操作界面"的载体。
- 纳管多台机器：登记机器 + Agent 地址/Token，集中查看所有机器的服务与状态。
- 打通 Control Plane（CP）↔ Agent 的调用链与心跳，远程对单个服务执行生命周期动作。
- 引入持久化存储（机器、服务镜像、操作日志）。

## 2. 范围

| In | Out |
|----|-----|
| 机器注册/编辑/删除 | 依赖关系与拓扑编排（Phase 4） |
| 聚合多机服务列表 + 状态 + 集中日志 | 就绪探针调度（Phase 4） |
| 远程单服务/批量启停（无依赖顺序） | 告警/审计/RBAC（Phase 5） |
| 远程服务创建/编辑（**透传**至 Agent `PUT`，含从模板新建） | CP 存可编辑配置主本再下发（后续增强） |
| CP↔Agent 客户端 + 心跳 + SQLite | mTLS、多租户（Phase 5） |

## 3. 架构

```
control_plane/
├── backend/
│   ├── main.py              # FastAPI 入口
│   ├── db.py                # SQLite (SQLAlchemy)，可平滑迁 PostgreSQL
│   ├── models.py            # Machine / Service / OperationLog / TemplateVersion
│   ├── agent_client.py      # 封装对 Agent REST 的调用（带 token、超时、重试）
│   ├── poller.py            # 后台任务：心跳 + 状态轮询，更新缓存
│   └── api/                 # 面向前端的 REST
└── frontend/                # Web 前端（机器列表、服务表、日志）
```

调用方向：**前端 → CP 后端 → 各机 Agent**。前端不直连 Agent（统一鉴权、跨域、审计入口）。

## 4. 数据模型（SQLite 起步）

- **machines**: `id, name, host, os, agent_endpoint, token(加密存储), status(online/offline), last_heartbeat, created_at`
- **services**: `id(CP 代理主键), machine_id, local_service_id, name, type, config_state, installation_state, runtime_state, startup_state, sync_state, config_revision, config_hash, last_seen_at, last_synced_at, deleted_at`
  - `local_service_id` = 机器内唯一的 service_id（= Agent 侧 XML 文件名，见 architecture §7.2）；CP 侧另用代理主键 `id` 做全局唯一定位。
  - **唯一约束** `(machine_id, local_service_id)`；Phase 4 的依赖/编排一律引用代理主键 `id`，从而天然携带机器身份、支持跨机依赖。
  - Agent 为配置的权威源，CP 存镜像用于聚合展示与后续编排引用。
- **operation_logs**: `id, operation_id, machine_id, service_id, action, operator, request_summary, result_code, message, created_at`
- **service_templates**: `id, key, name, description, created_at, archived_at`
- **template_versions**: `id, template_id, version, config_template(JSON), platform_fields(JSON), form_schema(JSON), probe_seeds(JSON), content_hash, created_at`
  - `(template_id, version)` 唯一；版本发布后不可变，只能创建新版本。模板不得包含秘密、原始 XML/unit 或任意 shell。
- **template_applications**: `id, template_version_id, machine_id, service_id, rendered_input_hash, probe_seed_snapshot, applied_at`
  - 服务实例固定引用实际使用的模板版本；模板升级不传播到既有服务。
  - Phase 3 只保存 `probe_seed_snapshot`，不创建或执行探针；Phase 4 迁移时按 application id 幂等物化，默认禁用并等待确认。

> 权威性约定：**服务配置以 Agent 本地为准**，CP 存的是索引/镜像；避免双写不一致。编排所需的依赖关系（Phase 4）是 CP 独有数据。

### 4.1 对账与删除

- Agent 列表中新出现的服务创建镜像；配置变化按 `revision/config_hash` 更新摘要。
- 一次轮询未见不能立即删除，先标记 `MISSING`；超过配置阈值后标记 `DELETED_EXTERNALLY`，保留依赖与历史供人工处置。
- Agent 离线时服务四维状态均为 `UNKNOWN`，Web 禁止编辑，不缓存“等待上线后下发”的配置主本。
- 删除服务/机器采用引用保护和软删除；存在依赖、编排组、运行中任务时拒绝删除。
- Web 编辑提交期望 revision；冲突返回用户比较并重新载入，不做静默覆盖。

## 5. 后台轮询与心跳

- `poller` 周期性（可配置，如 10s）对每台机器 `GET /healthz` 判定存活；随后用鉴权的 `GET /api/v1/agent` 检查版本/capabilities，并拉取 `/api/v1/services` 刷新状态缓存。
- online/offline 状态变化写入日志；前端展示实时状态徽标。
- 轮询失败（超时/拒绝）标记 offline，不阻塞其他机器（并发轮询 + 单机隔离超时）。
- 本阶段状态获取以 **CP 拉取**（pull）为准；Agent 主动 push 心跳为可选增强（[architecture §3.1](../architecture.md)、[phase-2 §6](./phase-2-agent.md)），本阶段不依赖。

## 6. 前端

- **机器管理页**：增删改机器（name/host/agent_endpoint/token），显示在线状态、Agent 版本。
- **服务列表页**：跨机聚合表格，列出机器/服务/类型/配置/安装/运行/开机四维状态；支持**多选批量**远程启停（复用 Phase 1 的批量交互理念，改为经 CP 调 Agent）。
- **服务新建/编辑（透传）**：选目标机 → 从模板或空白填表单 → CP 透传 `PUT` 至该机 Agent（Agent 仍为权威，CP 不留可编辑主本，见 §9 与 architecture §7.3）；提交后即时刷新镜像。把 Phase 1 的"从模板新建"从本机提升为**跨机远程新建**。
- **模板归属**：Web 使用的跨机模板由 CP 以不可变版本管理，内容拆分为平台无关配置、结构化平台字段、默认探针 seed 和表单元数据。Agent 只接收实例化后的 `ServiceConfig`；Phase 3 保存 seed 快照但不执行，Phase 4 再幂等物化。模板升级不自动修改既有服务。
- **日志查看**：选中服务 → CP 代理 Agent 的增量日志（轮询或 SSE）。
- **状态刷新**：起步用前端轮询 CP 缓存；后续可升级 SSE 推送。

## 7. 认证与安全

- **CP 自身登录**：本阶段最小化——单管理员账号 + 服务端会话或短期 Token；需定义退出、过期、CSRF/重放防护。细粒度 RBAC 留 Phase 5。
- **CP→Agent**：携带该机器登记的 Bearer Token。
- **Token 存储**：DB 中加密存储（如对称加密，**主密钥**来自环境变量/密钥文件），不明文落库。主密钥不入库、不入日志、不随 DB 备份一同外泄（备份仅含密文）。
- 明确 mTLS、Token 轮转、**主密钥轮转/丢失恢复**为 Phase 5 项（见 Phase 5 §4.4）。
- 本阶段 `operation_logs` 是基础可追溯记录；Phase 5 的“安全审计”是在此基础上增加只追加、防篡改、独立保留和导出能力，两者不冲突。

## 8. 技术选型（推荐 + 备选）

| 项 | 推荐 | 理由 / 备选 |
|----|------|-------------|
| 后端 | FastAPI + SQLAlchemy + SQLite | 与 Agent 同 HTTP/Schema 生态；CP 不使用 `ConfigManager`，规模上来迁 PostgreSQL |
| 前端 | React + 组件库（为 Phase 4 拓扑图铺路） | Phase 4 需 React Flow/vis-network；备选：轻量 Alpine/原生（若想极简） |
| 实时 | 轮询起步 → SSE | WebSocket 备选（编排进度双向不必需） |

## 9. 风险与权衡

- **状态一致性**：轮询有延迟；生命周期动作收到 202 后必须轮询原 `operation_id`，只有到达确定终态后才主动拉取即时四维状态。超时不能把动作当失败后重发。
- **配置权威性**：Agent 为配置权威。**区分两种写法**（见 architecture §7.3）：CP **透传式**创作（`PUT` 直达 Agent、CP 不留可编辑主本）**本阶段做**，不构成双写；CP **存可编辑主本再下发**易与 Agent 本地改动冲突，**推迟**为后续增强。
- **前端栈引入成本**：从 Tkinter 转 React 是较大跨越；本阶段控制在"骨架"，避免过度设计。

## 10. 验收判据

- 在 CP 网页登记 ≥2 台机器，能看到各机在线状态与服务列表。
- 对某台机器的单个服务远程 start/stop，CP 能跟踪 202 Operation 到确定终态，四维状态在数秒内刷新正确。
- 在 Web 端**从模板远程新建**一个服务（透传至目标机 Agent），刷新后镜像出现该服务；随后可对其远程 start/stop。
- 机器离线时列表正确标记 offline，不影响其他机器展示与操作。
- Agent 中服务被本地删除时，CP 先标记 `MISSING/DELETED_EXTERNALLY` 而不破坏依赖引用；revision 冲突不会覆盖他人修改。

## 11. 验证方式

- 本机起 2 个 Agent（不同端口）模拟两台机器，CP 纳管两者端到端跑通。
- 透传建服务闭环：Web 端新建 → 断言 Agent `GET /api/v1/services/{id}` 可见且 revision 正确、CP 镜像同步；测试不得把派生 XML 当公共验收接口。
- `/run` 启动 CP 后端 + 前端；`playwright-best-practices` 可用于前端关键路径 E2E。
- 断开一个 Agent，验证心跳判定 offline 与恢复 online。
