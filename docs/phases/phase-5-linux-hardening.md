# Phase 5 — Linux 适配 + 生产化增强

> 状态：**设计**。在核心能力（Phase 1–4）稳定后，补齐跨平台与生产可用性：Linux 服务、告警、审计、权限、安全加固、体验完善。

## 1. 目标与价值

- 支持少量 Linux 上的服务（nacos/mysql/redis/nginx 常部署于 Linux）。
- 从"能用"迈向"生产可用"：告警、审计、权限、安全、可观测、体验。

## 2. 范围

| In | Out |
|----|-----|
| LinuxSystemdBackend（跨平台） | 容器/K8s 编排（非目标） |
| 告警通知、操作审计、RBAC | 完整 APM/链路追踪（可后续接入） |
| mTLS、Token 轮转、密钥管理 | 多租户 SaaS 化（非目标） |
| Agent 版本管理与升级、协议版本化 | |
| i18n、深色主题、状态自动刷新 | |

## 3. Linux 适配

- **方案：同一套 Agent，新增 `LinuxSystemdBackend`** 实现 Phase 2 定义的 `AgentBackend` 接口——保持控制面协议不变，仅后端差异化。
  - `upsert` → 生成 systemd unit 文件（`.service`）写入 `/etc/systemd/system/`，`daemon-reload`。
  - `action` → 映射到 `systemctl start/stop/restart/enable/disable/status`。
  - `install/uninstall` → `enable`（开机自启）/ 删除 unit + `daemon-reload`。
  - `read_logs` → `journalctl -u <unit>`（增量按游标）或指定日志文件。
  - `status` → 解析 `systemctl is-active/is-failed` 为统一 `ServiceState`。
- **配置抽象**：Phase 2 起服务配置已是与平台无关的 `config` dict；由各 backend 渲染为 WinSW XML 或 systemd unit。内置模板表（见 `architecture.md`）需补 Linux 侧启动命令。
- **备选**：Agentless（SSH + systemctl）——部署轻但状态/探针弱，仅作为过渡或对无法装 Agent 的机器的兜底。

## 4. 生产化增强

### 4.1 告警通知
- 触发源：服务 down（liveness 失败）、编排运行失败、机器 offline。
- 通道：Webhook / 邮件 / 钉钉 / 企业微信（可插拔通道抽象）。
- 去抖与聚合，避免告警风暴。

### 4.2 操作审计
- 记录 who / when / what / result（登录、机器变更、服务动作、编排触发）。
- 审计日志独立存储、只追加、可查询导出。

### 4.3 RBAC 权限
- 角色：`admin`（全权）/`operator`（可操作服务与编排）/`viewer`（只读）。
- 权限点覆盖机器管理、服务动作、编排触发、配置修改。
- 与 Phase 3 的最小登录平滑升级为多用户 + 角色。

### 4.4 安全加固
- **CP↔Agent mTLS**：双向证书校验，取代/叠加静态 token。
- **Token 轮转**：Agent token 可按机器轮转，DB 加密存储。
- **密钥管理**：服务账户密码等敏感字段加密存储，避免明文入库/日志。**加密主密钥生命周期**：主密钥支持轮转（旧密钥解密 → 新密钥重加密的双密钥过渡窗口）、丢失/泄露时的重置流程，以及与 DB 备份分离存放；可选接入外部 KMS/密钥库。
- 端口与来源 IP 白名单、最小权限运行。

### 4.5 体验完善（含 README 既有待办）
- i18n（中/英）、深色主题、服务状态自动刷新（无需手动"刷新"）。
- 编排历史、状态时间线、批量操作结果汇总视图。

### 4.6 Agent 版本管理与升级
- CP 采集并展示各机 Agent 版本（`/health` 已返回），标记与 CP 兼容基线不符的过旧 Agent。
- 升级路径：Agent 自更新（下载新二进制 → 校验签名 → 自重启）或运维分发；升级期需保证不误杀被托管服务（Agent 进程重启 ≠ 业务服务重启）。
- 协议兼容：`/api/v1` 版本化，CP 对旧 Agent 做能力降级或提示升级，避免 fleet 中新旧混跑时的接口断裂。

## 5. 可观测与可靠性

- 关键指标：服务状态分布、编排成功率、Agent 在线率、动作耗时。
- Control Plane 存储从 SQLite 迁 PostgreSQL；定期备份；考虑 CP 高可用（无状态化 + 外部 DB）。

## 6. 风险与权衡

- **跨平台一致性**：WinSW 与 systemd 语义并不完全对齐（如失败恢复策略、日志形态）——在 backend 层做能力探测与差异说明，UI 标注平台特性。
- **安全改造成本**：mTLS/RBAC 侵入面广，建议在协议稳定后统一引入，避免反复。

## 7. 验收判据

- 一台 Linux 机器上的 nginx/redis 通过同一 CP 完成创建→enable→start→status→logs→stop 全流程。
- 触发一次服务宕机与一次编排失败，对应告警按配置通道送达。
- 以 viewer 角色登录无法执行任何写操作；操作审计可追溯到具体用户与动作。
- 启用 mTLS 后，无有效客户端证书的请求被拒绝。

## 8. 验证方式

- 准备一台 Linux 测试机（或容器）装 Agent，纳入现有 CP。
- 针对告警/审计/RBAC/mTLS 分别编写端到端用例；`security-review` 审查安全改造。
