# Phase 2 — Agent 化（架构关键第一步）

> 状态：**设计**。目标是把现有单机逻辑封装为一个带 HTTP API 的独立 Agent 进程，为跨机器管理奠基。本阶段验收标准：本机 Agent 通过 HTTP 完成全部现有操作（自己管自己）。

## 1. 目标与价值

- 把 `core/winsw_manager.py` + `core/config_manager.py` 从 GUI 中解耦，抽成**独立进程**，通过 HTTP API 对外提供服务管理能力。
- 定义 **AgentBackend 抽象接口**，Windows/WinSW 为首个实现，为 Phase 5 的 Linux/systemd 留出扩展点。
- Agent 自身可被 WinSW 注册为开机自启服务，长期常驻。
- 这是从"本机工具"迈向"控制平面 + Agent"分布式架构的**第一块基石**。

## 2. 范围

| In | Out |
|----|-----|
| 独立 Agent 进程 + REST API + Token 认证 | 中心控制端 / 多机聚合（Phase 3） |
| AgentBackend 抽象 + WindowsWinSWBackend | Linux/systemd 后端（Phase 5） |
| 服务 CRUD、7 个生命周期动作、增量日志、健康 | 依赖编排 / 探针调度（Phase 4） |
| 单机自管（Agent 管理本机服务） | 前端界面（Phase 3 起） |

## 3. 架构与模块

```
agent/
├── main.py                  # 启动 FastAPI (uvicorn)，加载配置
├── config.py                # Agent 配置：监听地址、token、services 目录、winsw 路径
├── api/
│   ├── routes.py            # REST 路由
│   ├── auth.py              # Bearer token 依赖注入校验
│   └── schemas.py           # Pydantic 请求/响应模型
├── backends/
│   ├── base.py              # AgentBackend 抽象基类（ABC）
│   └── windows_winsw.py     # 复用 WinSWManager + ConfigManager
└── core/                    # 从现有 core/ 迁移或引用
    ├── winsw_manager.py
    └── config_manager.py
```

**复用策略**：`WinSWManager` 的 7 个方法（`install/uninstall/start/stop/restart/status/refresh`）已以 `config` dict 为输入、自行定位 XML，几乎零改造即可被 `WindowsWinSWBackend` 包装。日志增量读取复用 `gui/tabs/log_viewer_tab.py` 的 `seek` 思路。

## 4. AgentBackend 抽象接口

```python
class AgentBackend(ABC):
    def list_services(self) -> list[ServiceRecord]: ...
    def describe(self, service_id: str) -> ServiceRecord: ...
    def upsert(self, service_id: str, config: dict) -> None: ...   # 生成/更新配置
    def delete(self, service_id: str) -> None: ...
    def action(self, service_id: str, action: str) -> ActionResult: ...  # 5 个有副作用动作：install/uninstall/start/stop/restart
    def status(self, service_id: str) -> ServiceState: ...   # 只读；GUI 的 status/refresh 均归此查询
    def read_logs(self, service_id: str, offset: int) -> LogChunk: ...   # 增量
    def health(self) -> AgentHealth: ...
```

`ServiceState` 枚举：`ACTIVE / INACTIVE / STARTING / STOPPING / FAILED / NOT_INSTALLED / UNKNOWN`。

## 5. REST API 设计

统一前缀 `/api/v1`，全部需 `Authorization: Bearer <token>`（`/health` 除外）。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | Agent 存活 + 版本 + OS（免鉴权，供心跳） |
| GET | `/services` | 列出本机所有受管服务及状态 |
| GET | `/services/{id}` | 服务详情（配置 + 状态） |
| PUT | `/services/{id}` | 创建/更新服务配置（body: config JSON 或 XML） |
| DELETE | `/services/{id}` | 删除服务配置（可选先 uninstall） |
| POST | `/services/{id}/actions/{action}` | 执行**有副作用**的动作：install/uninstall/start/stop/restart |
| GET | `/services/{id}/status` | 查询状态（解析后的枚举） |
| GET | `/services/{id}/logs?offset=N&stream=out\|err\|wrapper` | 增量日志（`wrapper` 为 WinSW 包装器自身日志流） |
| POST | `/probe` | 执行一次探针（探针 spec **随请求体内联传入**，Agent 不驻留）——**Phase 4 新增**，本阶段不实现 |

> 上表为本阶段交付面。`POST /probe` 在 Phase 4 编排引擎引入就绪门控时新增（见 [phase-4 §5](./phase-4-orchestration.md)），届时再补实现；本阶段保留接口位与协议前缀一致即可。

> **动作/查询语义**：GUI 的 7 个操作里，`status` 与 `refresh` 是只读、无副作用的，统一走 `GET /services/{id}/status`（`refresh` = 重新查询即时状态），**不**作为 `POST /actions`；`POST /actions` 只承载 install/uninstall/start/stop/restart 这 5 个有副作用动作，符合 HTTP 语义并利于幂等/重试策略区分（见 §6）。

**错误模型**：统一 `{code, message, detail}`，HTTP 状态码语义化（404 服务不存在、409 动作冲突、401 鉴权失败、500 WinSW 执行错误）。

## 6. 关键横切设计

- **并发**：WinSW 调用是阻塞子进程，用 `run_in_executor` 丢到线程池，避免阻塞事件循环。每个服务加**互斥锁**，串行化对同一服务的动作（避免 install 与 start 竞争）。
- **状态解析**：WinSW v3 `status` 输出为文本（如 `Active (running)` / `Inactive (stopped)` / `NonExistent`），需在 backend 内解析成 `ServiceState` 枚举，屏蔽版本差异。
- **认证**：静态 Bearer Token，来自 Agent 配置文件或环境变量（`WINSW_AGENT_TOKEN`）。为 Phase 5 的 mTLS/轮转预留接口。
- **监听绑定**：默认绑定可配置网卡与端口（如 `0.0.0.0:9800`），文档提示在防火墙放行、生产限制来源 IP。
- **幂等**：`PUT /services/{id}` 幂等；`start` 对已运行服务应返回幂等成功而非报错。
- **主动心跳（可选，Phase 3 用）**：Agent 可周期性向已知 Control Plane 上报心跳，缓解纯轮询的状态延迟；本阶段先做被动 `/health`。

## 7. 部署形态

- 用 PyInstaller 打包 Agent 为单文件 exe（沿用现有 `build.sh` 思路）。
- Agent 自身用一份 WinSW XML 注册为 Windows 服务，开机自启、失败重启。
- 配置文件示例 `agent.yaml`：监听地址、token、services 目录、winsw 路径（复用现有 auto/custom 两种模式）。

## 8. 风险与权衡

- **权限**：install/uninstall 服务需管理员权限——Agent 服务账户需 LocalSystem 或具备相应权限。
- **安全**：开放 HTTP 管理端口风险高；本阶段用 token + IP 限制，明确 mTLS 为 Phase 5 必做项。
- **协议选择**：本阶段选 **HTTP/JSON**（简单、易调试、易被任意前端消费）；高频状态流场景（Phase 4 编排进度）再评估 gRPC/WebSocket/SSE。

## 9. 验收判据

- 本机 Agent 启动后，通过 `curl`/HTTP 客户端完成：创建服务 → install → start → status(ACTIVE) → 读增量日志 → stop → uninstall → delete，全流程成功。
- 未带 token 的请求被 401 拒绝。
- 对同一服务并发发起 start/stop，动作被串行化、无竞态错误。

## 10. 验证方式

- 编写 API 冒烟脚本（httpx/pytest）覆盖上表全部端点与错误码。
- 用 `fastapi-templates` skill 搭 FastAPI 骨架；`/run` 启动 Agent 后端到端跑通。
- 与现有 GUI 对照：同一 XML 经 GUI 与经 Agent API 操作，行为一致。
