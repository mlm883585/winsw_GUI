# 演进路线图（索引）

> 从现有 `winsw_GUI` 到分布式服务编排平台的分阶段落地计划。
> 总体架构见 [architecture.md](./architecture.md)；各阶段**详细设计文档**见下表链接。
> 每阶段都可独立交付、独立验证。Phase 2 复用现有功能认知与迁移解析能力，但命令执行、配置权威、远程高权限 API 和并发恢复边界必须按冻结合同重构。
> 开发共同前置条件见 [implementation-baseline.md](./implementation-baseline.md)。
> **Recovery MVP v1 插入原 Phase 2 之前且不重编号后续阶段**：先交付已有 Windows Service 的多机断电恢复纵切；原 Phase 2–5 的 ServiceConfig/WinSW XML 接管路线保留为后续增强。

## 阶段总览

| 阶段 | 主题 | 状态 | 核心交付 | 详细设计 |
|------|------|------|----------|----------|
| Phase 0 | 现状基线 | ✅ 已完成 | 本机单服务 WinSW 图形管理 | — |
| Phase 1 | 本机多选批量 + 服务模板 | 🧪 代码完成，待人工验收 | 多选批量操作、从模板一键建服务；新增 6 个、合计 7 个模板 | [phase-1](./phases/phase-1-multiselect-templates.md) |
| Recovery MVP | Windows 多机冷启动恢复 | 🧪 核心代码、Inventory/Host Facts 与 Phase 2 Frozen 候选已验证；待现场清单/授权和真实三机 10 轮验收 | Agent 主动心跳 + ObservedService + 严格 DAG/readiness 门控 + 非秘密部署准备 + Jinja 最小 Web；HTTP+Token 仅限受控局域网实验 | [合同](./contracts/recovery-mvp-v1.md) / [Inventory 合同](./contracts/recovery-deployment-inventory-v1.md) / [部署验收](./recovery-mvp-operations.md) / [离线证据合同](./contracts/recovery-mvp-evidence-v1.md) / [Agent API](./api/recovery-agent-openapi.yaml) / [CP API](./api/recovery-control-plane-openapi.yaml) |
| Phase 2 | Agent 化 | 📐 合同冻结中 | HTTPS Agent + SQLite 权威主本 + Operation；PlatformBackend 抽象 | [phase-2](./phases/phase-2-agent.md) |
| Phase 3 | Control Plane Web 骨架 | 📝 设计 | 多机纳管、聚合查看、远程单服务操作、远程建服务（透传）、SQLite | [phase-3](./phases/phase-3-control-plane.md) |
| Phase 4 | 依赖编排引擎 | 📝 设计 | DAG 拓扑 + 就绪门控 + 探针 + 编排组 + 拓扑图一键启动（**核心价值**） | [phase-4](./phases/phase-4-orchestration.md) |
| Phase 5 | Linux 适配 + 生产化 | 📝 设计 | systemd 后端、告警、审计、RBAC、mTLS、体验完善 | [phase-5](./phases/phase-5-linux-hardening.md) |

## 依赖关系

```
Phase 1（本机能力，已落地）
   │
Recovery MVP（已有 Windows Service：主动心跳 + 严格 DAG 冷启动恢复）
   │
Phase 2（Agent 化：能力下沉为可远程调用的 API）
   │
Phase 3（Control Plane：多机纳管的载体）
   │
Phase 4（依赖编排：平台的最终目标，建立在多机 + Agent 动作之上）
   │
Phase 5（跨平台 + 生产化：在核心稳定后加固）
```

## 关键迁移与复用点（贯穿各阶段）
- **Recovery MVP 有界纵切**：新增独立 `orchestrator/` 包，只操作固定 allowlist 中的已注册 Windows Service；不改现有 Tkinter GUI，不接管 ServiceConfig 或 WinSW XML。
- **行为参考而非直接包装**：`core/winsw_manager.py` 的 7 个操作与 `core/config_manager.py` 的 XML 解析用于确认现状和迁移；Phase 2 重新实现结构化命令、原子持久化和错误边界。
- **平台无关配置**：Phase 2 起 `ServiceConfig` 存于 Agent SQLite，由 backend 单向渲染为 WinSW XML / systemd unit。
- **增量日志思路**：`gui/tabs/log_viewer_tab.py` 的 `seek` 逻辑演进为带文件身份、轮转处理和读取上限的不透明 cursor API。
- **批量交互**：Phase 1 的多选/批量分发理念 → Phase 3 经 CP 的远程批量。
- **新增目录**：`agent/`、`control_plane/`（backend + frontend）、编排引擎（`control_plane/backend` 内）。

## 实施建议
- Recovery MVP 编码以 [Recovery MVP 合同](./contracts/recovery-mvp-v1.md)、[Agent OpenAPI](./api/recovery-agent-openapi.yaml) 和 [Control Plane OpenAPI](./api/recovery-control-plane-openapi.yaml) 为准；完成 Schema/状态机/验收矩阵验证后，先跑通主动心跳、严格 DAG 与最小 Jinja Web，再进入真实三机连续 10 次随机冷启动验收。
- 真实部署前先按 [Deployment Inventory v1](./contracts/recovery-deployment-inventory-v1.md) 用本机 Host Facts 生成无秘密配置草案；目标机本地注入秘密并通过 Frozen `--check-config` 后，才允许执行 PreInstall。Inventory 不是安装授权，也不替代现场门禁。
- Recovery MVP 的 HTTP + 共享 Token 仅是普通受控局域网实验例外，并须限制 CP 来源 IP；不得作为生产部署或替代 Phase 2/5 的 TLS 安全基线。
- Phase 2 开工前必须同时冻结 [实施基线](./implementation-baseline.md)、[ServiceConfig v1](./contracts/service-config-v1.md)、[Agent Protocol v1](./contracts/agent-protocol-v1.md)、[OpenAPI](./api/agent-openapi.yaml) 与 [追踪矩阵](./contracts/phase-2-traceability.md)。
- 后续每个 Phase 进入实现前，先按其详细设计文档再走一次 plan 流程细化。
- Phase 2 起，每阶段实现后端到端跑通对应 API/界面，再进入下一阶段。
- 跨阶段的协议、命名、安全、数据模型通用约定见 [architecture.md](./architecture.md) 的「跨阶段通用约定」。
