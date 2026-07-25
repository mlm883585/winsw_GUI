# Recovery Deployment Inventory v1 合同

## 1. 目的与边界

Deployment Inventory 把现场收集的**非秘密事实**转换为可审计的 Agent/Control Plane 配置草案和恢复组蓝图。它属于 Recovery MVP 的部署准备工具，不是资产管理系统，也不代表安装授权。

本合同固定以下边界：

- 输入只包含主机、IP、端口、Windows 版本、Windows Service Name、readiness 和严格依赖；禁止 Token、密码、密码哈希、session secret、账户、ImagePath 或环境变量。
- 工具不连接远程主机、不探测端口、不读取 SCM、不创建数据库、不启动 Web 服务，也不修改 SCM、注册表、ACL 或防火墙。
- 输出配置中的秘密字段固定为故意无效的 `REQUIRED-GENERATE-ON-TARGET`。草案必须由目标机本地注入秘密并通过对应 Frozen `--check-config` 后，才能执行 `PreInstall(Frozen)`。
- Inventory 中的事实不能替代 Host Facts、`PreInstall` 或 `PostInstall`；三者分别证明“声明”“安装前实时状态”和“安装后实时状态”。
- 当前通信仍是受控普通局域网 HTTP + Token，仅限 MVP/实验环境。

## 2. 权威关系

| 领域 | 权威来源 |
|---|---|
| Inventory 字段与渲染行为 | 本文档 |
| Agent/CP 最终配置有效性 | `AgentConfig`、`ControlPlaneConfig` 与各自 `--check-config` |
| readiness 数值及运行行为 | `ReadinessWrite`、Agent probe 合同 |
| 依赖方向与恢复行为 | `recovery-mvp-v1.md` |
| 安装与现场门禁 | `recovery-mvp-operations.md` |

发生冲突时必须修正文档或实现，不允许通过隐含优先级掩盖。

## 3. Inventory 根对象

所有对象拒绝未知字段；所有整数/浮点数使用严格 JSON 类型，不接受字符串或布尔值强转。

| 字段 | 类型 | 约束 |
|---|---|---|
| `schema_version` | integer | 固定 `1` |
| `deployment_name` | string | 1–64，小写 slug，全局输出目录标识 |
| `control_plane` | object | 唯一独立管理节点，见第 4 节 |
| `agents` | array | 3–1024 个，见第 5 节 |
| `recovery_groups` | array | 1–1024 个，见第 7 节 |
| `acceptance_roles` | object | 固定五个角色映射，见第 8 节 |

公开 Agent、服务和 Group 总量不得超过现有 MVP 的 1024 上限；依赖边每组最多 16384 条。

## 4. Control Plane 节点

| 字段 | 类型 | 约束 |
|---|---|---|
| `node_id` | string | 1–64，小写 slug；不得与 Agent `node_id` 重复 |
| `hostname` | string | 1–255，无首尾空白；按大小写不敏感唯一 |
| `windows_version` | string | 1–160，来自本机 Host Facts/现场记录 |
| `architecture` | string | 1–64，必须包含 `64`；最终由 PreInstall 再验证 |
| `address` | string | 规范 IP literal；非 unspecified/multicast/loopback/link-local/IPv4-mapped IPv6 |
| `listen_port` | integer | 1–65535，默认 `8766` |
| `data_directory` | string | 本机绝对 Windows 路径；禁止 UNC、设备路径、`.`/`..`、驱动器根目录、控制字符、Windows 非法字符、尾随空格/点和保留设备名 |

Control Plane 的 hostname、address 和 data directory 必须与全部 Agent 不同，确保数据库不依赖或共置于被管理业务节点。

## 5. Agent 节点

| 字段 | 类型 | 约束 |
|---|---|---|
| `node_id` | string | 1–64，小写 slug；全局唯一 |
| `hostname` | string | 1–255；按大小写不敏感全局唯一 |
| `windows_version` | string | 1–160 |
| `architecture` | string | 1–64，必须包含 `64` |
| `address` | string | Agent advertised/listen 主地址；规则同 CP address |
| `active_unicast_ips` | string array | 1–64 个规范 IP literal，单节点及 Agent 间全局唯一；必须包含 `address`；只允许非loopback、非link-local本机地址 |
| `listen_port` | integer | 1–65535，默认 `8765` |
| `data_directory` | string | 本机绝对 Windows 路径；规则同 CP data directory |
| `services` | array | 1–1024 个；全局合计不超过 1024 |

渲染规则固定为：

- `listen_host = address`；`advertised_endpoint = http://{address}:{listen_port}`。
- `control_plane_url = http://{cp.address}:{cp.listen_port}`。
- `control_plane_source_cidrs` 只包含 CP address 对应的 `/32` 或 `/128` host prefix。
- CP 的 `agent_source_cidrs` 只包含全部 Agent 主 address 的 `/32` 或 `/128` host prefix。
- 数据库路径固定为 `{data_directory}\agent.sqlite3` 或 `{data_directory}\control-plane.sqlite3`。

## 6. 服务与 readiness

每个服务对象字段如下：

| 字段 | 类型 | 约束 |
|---|---|---|
| `service_id` | string | 1–64，小写 slug；整个 Inventory 全局唯一；渲染为 Agent `local_service_id` |
| `windows_service_name` | string | 1–256，无首尾空白；同一 Agent 内按大小写不敏感唯一 |
| `display_name` | string | 1–256，允许 Unicode |
| `startup_mode` | string | 固定 `Manual`；最终由 PreInstall 实时确认 |
| `readiness` | object | 必填且每服务恰好一个 `scm/tcp/http` |

readiness 复用 `ReadinessWrite` 的严格数值边界：默认 timeout 2 秒、interval 3 秒、deadline 60 秒，且 deadline 不得小于 timeout。

额外目标规则：

- `scm` 不包含目标地址，只检查当前服务 SCM 状态。
- `tcp.host` 只能是 `localhost`、`127.0.0.1`、`::1` 或所属 Agent `active_unicast_ips` 中的地址。
- `http.url` 只允许 `http`，禁止 userinfo、fragment、空端口、端口 0、DNS 名称、zone id 和 IPv4-mapped IPv6；其主机必须满足同一 Agent 本机地址规则。
- HTTP path/query 允许存在；重定向仍由运行时 Agent 拒绝。

Inventory 校验与 Agent 运行时必须共享同一纯目标验证器；运行时在请求接收和连接创建前仍各验证一次，不能因复用而取消 TOCTOU 二次检查。

## 7. Recovery Group 蓝图

每个 Group：

| 字段 | 类型 | 约束 |
|---|---|---|
| `group_id` | string | 1–64，小写 slug；Inventory 内唯一 |
| `name` | string | 1–128 |
| `description` | string | 0–1024，默认空字符串 |
| `node_settle_window_seconds` | integer | 1–3600，默认且示例固定 120 |
| `max_parallel_services` | integer | 1–4，默认 4 |
| `service_ids` | string array | 1–1024，唯一，必须引用 Inventory 服务 |
| `dependencies` | array | 最多 16384，边唯一；字段为 `dependent_service_id` 与 `prerequisite_service_id` |

每条依赖的两个服务都必须属于当前 Group；禁止自依赖和环。方向固定为“下游 `dependent_service_id` 依赖上游 `prerequisite_service_id`”。每个 Inventory 服务至少属于一个 Group。

蓝图使用稳定 `service_id`，因为 `managed_service_id` 只有 Agent 首次心跳后才由 CP 分配。配置 Group 时必须先按 `(agent_id, local_service_id)` 对账取得 managed UUID，不得按显示名猜测。

## 8. 验收角色

`acceptance_roles` 只允许以下五个字段，值为 Inventory 中五个互不相同的 `service_id`：

`mysql / redis / nacos / java / nginx`

五个角色必须处于同一个 Recovery Group，且该组至少包含以下直接严格依赖：

- Nacos depends on MySQL
- Nacos depends on Redis
- Java depends on Nacos
- Nginx depends on Java

允许增加额外严格依赖，但不得删除上述验收链。

## 9. CLI 与原子输出

命令固定为：

```powershell
.\winsw-recovery-control-plane.exe `
  --prepare-deployment .\deployment-inventory.json `
  --output-dir .\prepared-deployment
```

源码等价入口为 `python -m orchestrator.control_plane ...`。

参数规则：

- `--prepare-deployment` 与正常 `--config` 模式、`--check-config`、`--generate-secrets` 互斥。
- `--output-dir` 仅能与 `--prepare-deployment` 一起使用且必填。
- 输出目录必须不存在；工具不得覆盖或合并已有目录。
- 先在同父目录的随机临时目录完整渲染和计算哈希，成功后原子重命名；任一可捕获失败不得留下目标目录，临时目录必须清理，清理失败必须显式报错而不得静默吞掉。
- 输入支持 UTF-8 与 UTF-8 BOM；损坏 JSON、非对象根、未知字段或任一交叉约束失败均 exit 2。

参数缺失或互斥冲突属于 CLI usage 错误，使用标准 argparse usage 并 exit 2；Inventory 读取、校验或渲染失败则使用下述固定脱敏 JSON，不得混用两类错误。

成功 stdout 为单个脱敏 JSON，至少包含：

`component=recovery-deployment-preparer`、`inventory_valid=true`、`config_ready=false`、Agent/服务/Group 数量和 manifest SHA-256。失败只向 stderr 输出固定：

```json
{"component":"recovery-deployment-preparer","inventory_valid":false,"error":"deployment inventory validation failed"}
```

不得输出原始 Inventory、Pydantic repr、路径内的输入内容或 traceback。

## 10. 输出文件

| 路径 | 内容 |
|---|---|
| `control-plane/control-plane.json` | CP 配置草案，三个秘密字段为无效 sentinel |
| `agents/{node_id}/agent.json` | 每个 Agent 配置草案，共享 Token 为无效 sentinel |
| `recovery-blueprint.json` | 规范化非秘密服务映射、readiness、Group 和验收角色 |
| `deployment-manifest.json` | `config_ready=false`、`inventory_sha256` 输入哈希及除自身外每个输出文件的相对路径/SHA-256 |

所有 JSON 使用 UTF-8、排序键、两空格缩进和尾换行。相对路径统一 `/`；manifest 文件列表按路径排序。任何输出都不得包含真实或可直接使用的秘密。

公开 Python 调用面只提供 `load_deployment_inventory` 与 `prepare_deployment`；实际 renderer 为内部实现，调用方不得自报输入哈希生成 manifest。

渲染完成后，所有 Agent/CP 草案必须因 sentinel 被权威 `--check-config` 拒绝。目标机本地生成并注入秘密后，每份最终配置必须通过 `--check-config`；这两个相反断言都属于验收门槛。

## 11. Host Facts v1

发布包提供：

```powershell
.\scripts\get_recovery_host_facts.ps1 `
  -WindowsServiceName MySQL80,redis `
  -CandidatePort 3306,6379,8765
```

公开参数只包含 `WindowsServiceName` 与可选 `CandidatePort`，不提供别名、远程参数或非 JSON 输出开关。调用者必须显式提供 1–1024 个非空、大小写不敏感唯一的 Windows Service Name；候选端口最多 1024 个，若提供必须是 invariant 十进制整数文本、按数值唯一且位于 1–65535。无参数、非法类型、超限或非法作用域均返回脱敏可解析 FAIL JSON 和 exit 2，且不得调用任何 collector；此时 `hostname=null`，不得为填充报告而额外读取本机事实。

输出只允许：schema/component/outcome/side effects、`remote_hosts_scanned=0`、hostname、Windows version/architecture、活动且与 Inventory 兼容的规范接口地址、精确服务的 Name/DisplayName/StartMode/State，以及候选端口的 occupied/listening/listen addresses。汇聚边界必须逐字段重建并验证基础类型，collector 返回的对象或额外属性不得透传。接口地址必须拒绝 loopback、link-local、unspecified、multicast 与 IPv4-mapped IPv6；活动 Tunnel 接口只按地址规则判断，不因接口类型单独排除。hostname、64 位 Windows 信息、至少一个有效接口、任一服务关键字段或端口查询无法确认时，`outcome=FAIL`；不得把采集器错误伪装成端口空闲。

脚本不得接受远程计算机/Session参数，不得输出 ImagePath、账户、环境、Token、密码或进程命令行，不得写 SCM/Firewall/Registry/ACL。

## 12. 验收矩阵

至少覆盖：

- 合法三 Agent、五角色、严格链成功渲染；输出可重复且 manifest 可复算。
- 2 个 Agent、CP 与 Agent 共机/IP、重复 hostname/address/node/service/windows name、总服务 1025 均拒绝。
- 非64位声明、UNC/设备/相对/根/穿越/Windows非法字符/尾随空格或点/保留设备名 data path 均拒绝。
- DNS/远端/映射IPv6/zone/userinfo/HTTPS/fragment/0端口 readiness 均拒绝。
- 域外依赖、自依赖、重复边、环、缺验收边、角色重复/跨组均拒绝。
- 未知字段、string/bool 数值、损坏 JSON、BOM、已存在输出目录和渲染中断原子清理。
- 成功路径不建SQLite、不监听网络、不加载uvicorn App；失败输出脱敏。
- 未注入 secret 的所有草案被权威配置检查拒绝；注入同一有效共享Token及本地CP秘密后全部通过。
- Host Facts 在 PowerShell 5.1解析通过，非法输入（含无法绑定为端口的文本）零采集并返回脱敏 JSON/exit 2，输出字段白名单稳定，采集失败 fail closed，且静态不存在远程与写操作命令。
