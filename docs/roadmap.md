# 演进路线图

> 从现有 `winsw_GUI` 到分布式服务编排平台的分阶段落地计划。架构设计见 [architecture.md](./architecture.md)。
> 每阶段都可独立交付、独立验证。前 2 阶段几乎纯复用现有代码，风险低。

## Phase 0 — 现状（已完成）
本机单服务 WinSW 图形管理。基线不动。

## Phase 1 — 本机多服务 + 服务模板（复用为主，低风险）
- `gui/service_list_view.py`：单选 → 多选；`gui/actions_panel.py`：支持批量启停。
- 接入 `templates/`：从模板一键创建服务（当前模板未被代码引用）。新增 java/nodejs/nacos/mysql/redis/nginx 模板文件。
- **验收判据**：单机可"选多个服务一键启停 + 从模板建服务"。

## Phase 2 — Agent 化（架构关键第一步）
- 把 `core/winsw_manager.py` + `core/config_manager.py` 抽成**独立 Agent 进程**，套一层 FastAPI，暴露 install/start/stop/status/logs/health API + token 认证。
- 定义 `AgentBackend` 抽象接口，Windows 实现落地。
- Agent 自身可被 WinSW 注册为开机自启服务。
- **验收判据**：本机 Agent 通过 HTTP 完成全部现有操作（自己管自己），协议跑通。

## Phase 3 — Control Plane Web 骨架
- FastAPI 后端 + 前端：机器清单（增删机器、填 Agent 地址+token）、聚合多机服务列表、集中查看状态/日志、远程单服务启停。
- 心跳/状态上报打通；SQLite 落地机器/服务表。
- **验收判据**：一个网页集中看/操作多台机器上的服务（尚无依赖编排）。

## Phase 4 — 依赖编排引擎（核心价值）
- 服务定义加 `depends_on[]`（跨机）+ 探针配置 + 依赖分级。
- 编排引擎：拓扑排序、分层并行、就绪门控、逆序停止、环检测。
- 前端依赖拓扑图（实时状态着色）+ **一键启动整条链路**。
- 探针调度器：tcp/http/process/cmd 四类，含 startup/readiness/liveness 角色。
- **验收判据**：一键让一整套有依赖关系的服务按正确顺序起来（本方案的最终目标）。

## Phase 5 — Linux 适配 + 生产化增强
- `LinuxSystemdBackend`（SSH 或同款 Agent 的 Linux 构建）。
- 告警通知、操作审计、RBAC 权限、深色主题/i18n、状态自动刷新。

---

## 关键文件与复用点
- **直接复用/下沉为 Agent**：`core/winsw_manager.py`（`WinSWManager._execute/_run_command`、7 个操作）、`core/config_manager.py`（`ConfigManager` XML↔dict、`get_default_config`）。
- **模板机制**：`templates/python.xml` 现为文档、未接代码——Phase 1 正式接入并扩充为多类型模板。
- **日志增量读取**：`gui/tabs/log_viewer_tab.py` 的 `seek` 增量读取思路可复用为 Agent 的 `logs` API。
- **新增目录**：`agent/`（FastAPI + backends）、`control_plane/`（后端+前端）、`orchestrator/`（DAG+探针）。

## 实施建议
- 后续每个 Phase 进入实现前，单独再走一次 plan 流程细化该阶段设计。
- Phase 2 起，每阶段实现后端到端跑通对应 API/界面再进入下一阶段。
