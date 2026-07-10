# 演进路线图（索引）

> 从现有 `winsw_GUI` 到分布式服务编排平台的分阶段落地计划。
> 总体架构见 [architecture.md](./architecture.md)；各阶段**详细设计文档**见下表链接。
> 每阶段都可独立交付、独立验证。前 2 阶段几乎纯复用现有代码，风险低。

## 阶段总览

| 阶段 | 主题 | 状态 | 核心交付 | 详细设计 |
|------|------|------|----------|----------|
| Phase 0 | 现状基线 | ✅ 已完成 | 本机单服务 WinSW 图形管理 | — |
| Phase 1 | 本机多选批量 + 服务模板 | ✅ 已完成 | 多选批量操作、从模板一键建服务、6 个内置模板 | [phase-1](./phases/phase-1-multiselect-templates.md) |
| Phase 2 | Agent 化 | 📝 设计 | 独立 Agent 进程 + REST API + Token；AgentBackend 抽象 | [phase-2](./phases/phase-2-agent.md) |
| Phase 3 | Control Plane Web 骨架 | 📝 设计 | 多机纳管、聚合查看、远程单服务操作、远程建服务（透传）、SQLite | [phase-3](./phases/phase-3-control-plane.md) |
| Phase 4 | 依赖编排引擎 | 📝 设计 | DAG 拓扑 + 就绪门控 + 探针 + 编排组 + 拓扑图一键启动（**核心价值**） | [phase-4](./phases/phase-4-orchestration.md) |
| Phase 5 | Linux 适配 + 生产化 | 📝 设计 | systemd 后端、告警、审计、RBAC、mTLS、体验完善 | [phase-5](./phases/phase-5-linux-hardening.md) |

## 依赖关系

```
Phase 1（本机能力，已落地）
   │
Phase 2（Agent 化：能力下沉为可远程调用的 API）
   │
Phase 3（Control Plane：多机纳管的载体）
   │
Phase 4（依赖编排：平台的最终目标，建立在多机 + Agent 动作之上）
   │
Phase 5（跨平台 + 生产化：在核心稳定后加固）
```

## 关键复用点（贯穿各阶段）
- **下沉为 Agent 后端**：`core/winsw_manager.py`（7 个操作，以 `config` dict 为输入自定位 XML）、`core/config_manager.py`（XML↔dict）。
- **平台无关配置**：Phase 2 起服务配置为与平台无关的 dict，由各 backend 渲染为 WinSW XML / systemd unit。
- **增量日志**：`gui/tabs/log_viewer_tab.py` 的 `seek` 增量读取 → Agent 的 `logs` API。
- **批量交互**：Phase 1 的多选/批量分发理念 → Phase 3 经 CP 的远程批量。
- **新增目录**：`agent/`、`control_plane/`（backend + frontend）、编排引擎（`control_plane/backend` 内）。

## 实施建议
- 后续每个 Phase 进入实现前，先按其详细设计文档再走一次 plan 流程细化。
- Phase 2 起，每阶段实现后端到端跑通对应 API/界面，再进入下一阶段。
- 跨阶段的协议、命名、安全、数据模型通用约定见 [architecture.md](./architecture.md) 的「跨阶段通用约定」。
