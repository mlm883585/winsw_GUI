# Recovery MVP 部署与三机验收手册

> 适用范围：普通、受控、可信局域网中的 MVP/实验环境。当前通信是明文 HTTP；同网段攻击者可窃取 Token 或篡改控制流，**不得作为生产安全方案**。

## 1. 部署拓扑

| 节点 | 部署 | Windows 启动类型 |
|---|---|---|
| 独立管理节点 | Control Plane + 本地 SQLite | `Automatic` |
| 每台业务服务器 | Recovery Agent | `Automatic` |
| 业务工作负载 | 既有 MySQL/WinSW Windows Service | 必须 `Manual` |

Control Plane 不能部署在依赖其编排的 MySQL/Redis/Nacos 上。MVP 不安装、不卸载、不改写业务服务或 WinSW XML。

## 2. 准备工作

1. 在受控构建/测试机安装 Python 3.13，并用 `scripts/build_recovery_mvp.ps1` 生成 onedir 发布包；目标服务器只接收该冻结包，不依赖本机 Python。
2. 通过现有 Tkinter GUI/原生安装器注册 Java、Redis、Nacos、Nginx、MySQL 服务。
3. 在 `services.msc` 确认所有业务服务是“手动”；Agent/CP 尚未安装，其 `Automatic` 状态由后续 `PostInstall` 复核。
4. 在管理 VLAN/Windows Firewall 中只放行：Agent→CP 心跳端口、CP→Agent API 端口；Agent 的来源 CIDR只写 CP 地址。
5. 在解压后的发布根目录运行 `.\winsw-recovery-control-plane\winsw-recovery-control-plane.exe --generate-secrets`，交互输入管理员密码并分别保存 cluster token、管理员哈希和 session secret。源码开发环境可运行等价的 `python scripts/generate_admin_secrets.py`。example 中的 `REQUIRED` 是故意无效的 sentinel，未替换时 `--check-config` 必须失败。

唯一发布顺序是：`构建 onedir → 构建机只读烟雾 → 向目标机分发并校验 → 本机 Host Facts → Inventory 校验/渲染 → 目标机本地注入秘密并通过 --check-config → PreInstall(Frozen) → Install → PostInstall → 配置恢复组 → 三机验收`。开发模式的 Python 进程和 `RuntimeMode=Python` 只用于源码诊断，不能替代这条发布链中的 Frozen 检查。

### 2.1 非秘密部署清单与本机事实

在生成任何目标机配置前，先把同一冻结发布包复制到各节点并复算发布清单。每台业务节点只对明确列出的 Windows Service Name 和候选端口执行本机事实采集；脚本不接受远程主机参数，不连接 CP/Agent，也不修改 SCM 或 Firewall：

```powershell
& .\scripts\get_recovery_host_facts.ps1 `
  -WindowsServiceName @("MySQL80", "redis") `
  -CandidatePort @(3306, 6379, 8765)
```

Control Plane 节点也必须采集 Windows version/architecture/address；若该节点没有业务服务，可用本机一个明确、无副作用的既有 Windows 服务完成脚本作用域要求，但不得把它加入 Agent allowlist。报告必须显示 `outcome=PASS`、`side_effects=NONE` 和 `remote_hosts_scanned=0`；服务缺失、状态无法确认或参数不唯一时先修正现场事实，不能手工把 FAIL 改成 PASS。

把非秘密事实填入 `examples/deployment-inventory.example.json` 的副本，然后在受控工作站或 CP 节点运行：

```powershell
& .\winsw-recovery-control-plane\winsw-recovery-control-plane.exe `
  --prepare-deployment .\deployment-inventory.json `
  --output-dir .\prepared-deployment
```

字段和失败规则以 `docs/contracts/recovery-deployment-inventory-v1.md` 为准。成功只表示主机、服务映射、readiness、严格 DAG 和网络地址声明内部一致；输出固定 `config_ready=false`，Agent/CP 配置中的秘密为故意无效的 `REQUIRED-GENERATE-ON-TARGET`。不得把草案直接交给 PreInstall。

在 CP 目标机本地运行 `--generate-secrets`，把同一 cluster token 通过受控离线方式写入 CP 和所有 Agent 的本地配置；管理员密码哈希与 session secret 只写 CP。不要把秘密写回 Inventory、聊天记录或 Host Facts。随后在每台目标机用对应 Frozen EXE 执行 `--check-config`，全部成功后才进入第 4 节。

## 3. 冻结程序只读烟雾验证（构建发布门槛）

一次完整构建必须整体保留以下目录；不要只复制单个 EXE：

| 构建产物 | 用途 |
|---|---|
| `dist-recovery/winsw-recovery-agent/` | Agent onedir，目标机 `PackageDirectory` 指向此目录 |
| `dist-recovery/winsw-recovery-control-plane/` | CP onedir |
| `dist-recovery/winsw-recovery-evidence-validator/` | 验收工作站使用的离线证据校验器；不连接或控制服务器 |
| `dist-recovery/scripts/`、`examples/`、`docs/` | 安装/预检/烟雾/Host Facts 脚本、Inventory/配置/证据示例与合同/验收手册；目录布局与本文命令一致 |
| `dist-recovery/deployment/` | 固定版本的 WinSW lock；不内置未签名的 WinSW 二进制 |
| `dist-recovery/SHA256SUMS.txt` | 除清单自身外所有发布文件的哈希；受控传输后必须逐项复算，且除该清单外不得出现缺失/额外文件 |

发布包到达目标机后，必须先执行本地完整性校验，再运行烟雾测试或安装。`SHA256SUMS.txt` 不包含自身，因此它不能自证可信；操作员必须通过独立受控渠道取得并核对该清单（或清单的签名/哈希），不能只信任与发布包同路传输的清单。

```powershell
& .\scripts\verify_recovery_distribution.ps1 -DistributionDirectory .
if ($LASTEXITCODE -ne 0) { throw "Recovery distribution integrity verification failed: exit=$LASTEXITCODE" }
```

校验器只读取本机发布目录，不访问网络，不调用 SCM，不修改注册表或文件。成功时返回 `outcome=PASS`、`exit_code=0`、`side_effects=NONE`，并明确 `manifest_self_verified=false`、`manifest_trust=OUT_OF_BAND_REQUIRED`。退出码 `2` 表示目录/清单结构不安全或存在任意 reparse point，退出码 `3` 表示文件缺失、额外文件或 SHA-256 不一致；任一非零结果都必须阻止后续烟雾测试、安装与服务启动。

向目标机分发前，必须先在一台受控 Windows 构建/测试机上用最新 onedir 产物验证真实 WMI/SCM 读取、Agent 主动心跳、管理员会话和 Dashboard；失败即阻止分发。烟雾实例使用独立 loopback 端口和临时状态，默认只观察 Windows `EventLog`，不调用任何 action 或 Run 接口，也不会注册/修改 Windows Service：

从源码仓库根目录验证刚构建的产物：

```powershell
.\scripts\smoke_recovery_binaries.ps1 -DistributionDirectory dist-recovery
```

若正在解压后的 `dist-recovery` 发布根目录中复核同一包，则使用：

```powershell
.\scripts\smoke_recovery_binaries.ps1 -DistributionDirectory .
```

输出必须显示两端 health 为 `ok`、`AgentCount=1`、被观察服务为 `INSTALLED`，并明确 `SideEffects=NONE`。这只能证明冻结程序的本机启动、真实 SCM 读取、心跳和最小 Web 链路，不能替代后文的目标机 WinSW/ACL 复核和三机断电验收。

## 4. 部署前只读主机预检

先在每台目标机创建专用数据目录，把已经本地注入秘密且通过对应 Frozen `--check-config` 的 JSON 配置安全地放入其中，再从**提升权限的本机 PowerShell**执行预检。`config_ready=false` 或仍含 `REQUIRED-GENERATE-ON-TARGET` 的 Inventory 草案禁止进入本步骤。预检只读取当前主机：不接受远程主机参数、不连接 Agent 配置中的 CP 地址、不修改 SCM、不注册服务，也不修复 ACL。默认阶段是 `PreInstall`，默认输出 JSON；`-PassThru` 可返回 PowerShell 对象。

Agent 目标机必须使用待安装的 Frozen onedir：

```powershell
& .\scripts\test_recovery_host_preflight.ps1 `
  -Role Agent `
  -ConfigPath C:\ProgramData\WinSW-Recovery-Agent\agent.json `
  -DataDirectory C:\ProgramData\WinSW-Recovery-Agent `
  -RuntimeMode Frozen `
  -PackageDirectory C:\Recovery\winsw-recovery-agent `
  -BusinessServiceName @("MySQL80", "redis") |
    Set-Content -Encoding utf8 C:\Recovery\agent-preflight.json
$preflightExit = $LASTEXITCODE
```

Control Plane 同样传入其角色 onedir 的**直接目录**：

```powershell
& .\scripts\test_recovery_host_preflight.ps1 `
  -Role ControlPlane `
  -ConfigPath C:\ProgramData\WinSW-Recovery-ControlPlane\control-plane.json `
  -DataDirectory C:\ProgramData\WinSW-Recovery-ControlPlane `
  -RuntimeMode Frozen `
  -PackageDirectory C:\Recovery\winsw-recovery-control-plane
$preflightExit = $LASTEXITCODE
```

Agent 的 `-BusinessServiceName` 只能包含本机 `agent.json` allowlist 中的服务；省略时精确检查 allowlist 的全部服务。服务检查通过注册表中的精确服务键和本机 SCM 读取完成，不枚举或操作未指定的业务服务。端口检查只读取当前本机的活动 TCP listener；Agent 的远端 CP 可达性会明确标为 `SKIP`，留给部署网络验收。

配置不是靠 PowerShell 重写校验：预检会调用所选源码/冻结 CLI 的只读 `--check-config`，由 Agent/CP 的权威 Pydantic loader 做完整严格校验；该分支不建库、不监听端口，失败输出经过脱敏。随后才读取通过校验的少数字段完成路径、端口和 allowlist 主机检查。

脚本仍保留 `-RuntimeMode Python -Python <python.exe>` 供源码开发诊断，但该结果不是发布证据；真实部署必须用将要安装的同一 onedir 再执行一次 `PreInstall(Frozen)` 并通过。

现场发布的 `PreInstall` 要求：64 位 Windows Server、提升权限、可运行的目标角色 onedir 包、配置和 SQLite 路径均位于专用数据目录、目标 listen port 空闲、全部业务服务存在且为 `Manual`。ACL 检查覆盖数据目录、配置文件及已存在的 SQLite 文件；若尚未达到安装后的固定边界，会报告 `WARN` 而不让合法的首次安装无法通过。安装脚本必须在服务启动前把数据根目录收敛为：关闭继承、所有者为本机 Administrators，并且只允许 LocalSystem (`S-1-5-18`) 和本机 Administrators (`S-1-5-32-544`) 完全控制，目录 ACE 还必须以 `ContainerInherit + ObjectInherit`、`Propagation=None` 向下继承。

安装后用同一脚本执行 `-Stage PostInstall`，此阶段只接受 `RuntimeMode=Frozen` 且 `PackageDirectory=DataDirectory\package`。脚本核对固定角色服务为 `LocalSystem + Automatic + Running`，SCM `ImagePath` 指向受管 WinSW wrapper，WinSW XML 绑定的 executable/config/working directory 均是本次受检路径；listen port 的**全部唯一 listener PID**都必须是预期角色 EXE，且各自最多八级父进程链都回到 SCM 报告的 WinSW wrapper PID。即使已有一个合法 PID，只要同时存在一个额外、路径不符、不可查询或不属于该 wrapper 进程树的 PID，端口检查仍失败并在 evidence 中列出非法 PID。

`PostInstall` 还要求 SQLite 已创建，并递归审计整个 `DataDirectory`（package、service/WinSW、logs、SQLite/WAL/SHM 均在内），拒绝 reparse point 和任意第三方 SID/非完全控制/错误传播规则。数据根自身必须是受保护 DACL 且 owner 为 Administrators；服务启动后由 LocalSystem 新建的后代允许 owner 为 SYSTEM，并允许从已逐层审计的安全父目录继承。这与 Windows 的实际继承/owner 行为一致，不要求新 SQLite 文件伪装成“独立受保护且 owner=Administrators”。两个阶段的相反端口期望与 ACL 严格度因此是显式可达的。

| 退出码 | 含义 |
|---:|---|
| `0` | 全部强制检查通过；远端 CP 和尚未创建的 SQLite ACL 可有声明式 `SKIP` |
| `2` | 参数或角色范围无效 |
| `3` | OS、位数或提升权限不满足 |
| `4` | Python 3.13 / onedir 运行时无效 |
| `5` | 配置 JSON、目录布局、数据库路径或 listen port 配置无效 |
| `6` | 业务服务无效，或安装后的 Recovery 角色服务身份/路径/状态无效 |
| `7` | listen port 不符合阶段期望：安装前应空闲；安装后必须有 listener，且全部 listener PID 均属于受检 Recovery 角色进程树 |
| `8` | `PostInstall` 的 SQLite 不存在，或数据树 ACL/reparse point 不符合固定服务账户边界 |
| `10` | 未分类的预检内部错误 |

报告的 `failures[]` 会保留全部失败及各自 `failure_code`，`warnings[]` 单列安装前可修复的 ACL 差异；进程退出码取依赖顺序中的首个失败。只有 `PreInstall` 报告为 `outcome=PASS`、`exit_code=0` 且 `side_effects=NONE` 才进入安装步骤。

## 5. 配置与启动

复制并修改：

- `examples/control-plane.example.json` → 管理节点 `C:\ProgramData\WinSW-Recovery-ControlPlane\control-plane.json`
- `examples/agent.example.json` → 每台业务节点 `C:\ProgramData\WinSW-Recovery-Agent\agent.json`

服务安装时，配置文件及其 `database_path` 必须位于对应的专用 `DataDirectory` 内，安装脚本才会继续。脚本会把 onedir 包复制到该目录，并将代码、Token、SQLite 和日志的 DACL **替换**为仅 LocalSystem 与本机 Administrators 可访问；首版固定用 LocalSystem 运行 Agent/CP，不提供名义上的“自定义账户”参数。

配置检查同时拒绝“语法正确但网络上不可能工作”的组合：Agent advertised 端口必须等于 listen 端口，advertised IP 不能是 unspecified/multicast，具体 listen IP 必须与其一致；loopback 只能用于两端 URL、监听和来源 host CIDR 均为 loopback 的本机烟雾环境。Agent 的 Control Plane URL 若写 IP literal，该地址必须包含在 Agent 的 CP 来源 host CIDR 中。Control Plane listen host 只能是 IP literal（允许 `0.0.0.0`/`::` 通配监听，但拒绝 multicast），管理员密码哈希必须是冻结参数范围内的完整 `pbkdf2_sha256` 表示。任一失败均由 `--check-config` 在建库、监听或安装前以脱敏 JSON 和退出码 2 拒绝。

Agent allowlist 与每个 `local_service_id` 必须长期稳定；配置变更只用于真实增删服务，不得按启动或发布轮换临时 ID。服务暂时从 allowlist 消失时，CP 会保留内部 tombstone 以维持 Group 根因、代理审计与同 ID 回归，不会在 Dashboard 服务列表显示。持续产生新 ID 属于配置错误或 Token/来源边界事件：停止相关 Agent、核对配置来源和 Token 使用者，不要直接删除 SQLite 行。

Agent 配置中的 `database_path + listen_port` 是单进程独占边界：同一主机只允许安装器创建的固定 WinSW Agent 服务使用它。禁止在该服务运行时再执行下方源码命令、复制第二个 Recovery Agent 服务，或让另一份 `agent.json` 复用同一数据库/端口。`PreInstall` 的端口为空和 `PostInstall` 的全部 listener 都归属固定 wrapper 进程树是强制门禁；`instance_generation` 只用于隔离延迟消息，不代表支持双进程。若必须源码诊断，先停止固定 Agent 服务，完成后结束诊断进程并通过 `PostInstall` 再恢复服务。

开发模式：

```powershell
python -m orchestrator.control_plane --config C:\ProgramData\WinSW-Recovery-ControlPlane\control-plane.json
python -m orchestrator.agent --config C:\ProgramData\WinSW-Recovery-Agent\agent.json
```

服务模式必须使用显式、固定版本的 WinSW 文件和仓库内已审核的 lock；安装脚本拒绝 `latest` 与 hash/size 不匹配：

```powershell
.\scripts\install_recovery_service.ps1 `
  -Role Agent `
  -PackageDirectory C:\Recovery\winsw-recovery-agent `
  -ConfigPath C:\ProgramData\WinSW-Recovery-Agent\agent.json `
  -DataDirectory C:\ProgramData\WinSW-Recovery-Agent `
  -WinSWPath C:\Approved\WinSW-x64.exe `
  -WinSWLockPath .\deployment\winsw-x64-v2.12.0.lock.json
```

安装器是 install-only 事务，不是升级器或残留清理器。产生任何安装副作用前，下列项目必须全部不存在：固定 Recovery 服务、`DataDirectory\package`、`DataDirectory\service`，以及名称以 `.install-<service-id>-` 开头的本角色 staging。任一残留都会在覆盖、ACL 修改或 SCM 操作前 fail closed；操作员必须先查明来源并人工处置，不能通过重新运行安装器删除旧版本或用户文件。配置、SQLite、日志和数据目录内其他文件允许存在并始终保留。

事务阶段固定为 `WINSW_ACQUIRE → WINSW_VERIFY → STAGING → STAGED_WINSW_VERIFY → ACL_APPLY → PUBLISH_PACKAGE → PUBLISH_SERVICE → PUBLISHED_WINSW_VERIFY → SCM_INSTALL → SCM_START → TEMP_CLEANUP → COMPLETE`。源 WinSW 通过 lock 后，复制到本次唯一 staging 的 wrapper 立即按同一 SHA-256、size 与 Authenticode 状态重验；目录移动发布后，最终 `DataDirectory\service` wrapper 在 SCM install 前再次按同一 lock 重验。任一次不符都不得调用 SCM，并按 journal 回滚本次 package/service/staging。每个可能产生副作用的阶段先写入内存 journal 所有权标志；package/service 以“不允许目标已存在”的目录移动发布。ACL 修改前记录所有既有路径的 Owner+DACL；失败时恢复本次已触及的既有路径。

任何阶段失败都会显示稳定的 `INSTALL_FAILED` 聚合错误，其中同时保留 `phase`、原始 `primary` 失败、`rollback_issues` 和 `retry_safe`。如果本次 SCM install 已产生固定 Recovery 服务，安装器只有在 ImagePath 仍指向本次 wrapper 时才会 best-effort stop 后 uninstall；无法证明所有权时绝不操作同名服务。仅当服务确认不存在时，才删除本次创建的 package/service/staging，避免留下指向已删除 wrapper 的错误服务。回滚命令失败也不会中断其余回滚步骤。

`retry_safe=true` 表示固定 Recovery 服务和本次受管路径均已清除，可修正原始问题后直接用同一命令重试；`retry_safe=false` 表示仍有服务、受管路径或 ACL 恢复问题，必须按聚合错误中的问题码核对服务 ImagePath、停止/卸载结果及精确路径后再重试。无论结果如何，安装器都不得删除配置、SQLite、日志、`DataDirectory` 或其中其他非本次文件，也不得修改任何业务服务。

内存 journal 处理的是脚本可捕获的失败，不宣称跨进程崩溃恢复。若安装进程被强杀或主机在事务中断电，下次执行必须由服务/package/service/staging 残留门禁拒绝，操作员依据固定服务 ImagePath 和精确路径人工确认所有权；安装器不得把不完整残留自动当成本次执行并删除。

Control Plane 使用同一脚本并将 `-Role` 改为 `ControlPlane`。Recovery MVP 的现场验收路径必须传 `-WinSWLockPath`；`-WinSWPath` 是可选的离线资产来源，省略时安装器才按 lock 内固定 URL 下载。不得同时传 `-WinSWLockPath` 与 `-WinSWSha256/-WinSWDownloadUrl`。安装器保留的 SHA-only 参数只用于未来更换资产前的开发兼容，不属于本锁定版本的现场部署合同；更换版本必须先生成并评审新 lock。

仓库内 lock 固定 WinSW `2.12.0` x64 的版本、URL、文件大小、Authenticode 状态与 SHA-256；该 digest 是从固定的官方 release 资产下载后在本地计算的，上游 release/API 并未提供可独立对照的 digest。该资产的 Authenticode 状态为 `NotSigned`，因此 lock 只证明待安装字节与已评审记录一致，不证明文件确实来自该 URL、发布者身份、无恶意内容或 commit 与二进制存在密码学绑定。部署必须使用受控分发、仓库评审和适用的 EDR 放行流程，且不得使用 `latest`。

安装完成后立即执行只读复核；冻结包目录应指向安装脚本复制后的 `DataDirectory\package`：

```powershell
& .\scripts\test_recovery_host_preflight.ps1 `
  -Role Agent `
  -Stage PostInstall `
  -ConfigPath C:\ProgramData\WinSW-Recovery-Agent\agent.json `
  -DataDirectory C:\ProgramData\WinSW-Recovery-Agent `
  -RuntimeMode Frozen `
  -PackageDirectory C:\ProgramData\WinSW-Recovery-Agent\package `
  -BusinessServiceName @("MySQL80", "redis")
```

只有 `PostInstall` 也返回 `exit_code=0`，目标机才继续恢复组配置。目标机安装顺序固定为：`PreInstall → Install → PostInstall → 恢复组`。

## 6. 首次配置恢复组

1. 登录 Web Dashboard，确认全部 Agent 为 `ONLINE`，boot_id 稳定。
2. 确认成员服务为 `INSTALLED + AUTOSTART_DISABLED`。
3. 创建恢复组并选择成员。
4. 保存严格依赖：`Nacos depends_on MySQL/Redis`、`Java depends_on Nacos`、`Nginx depends_on Java`。
5. 为 MySQL/Redis 配 TCP，为 Nacos/Java/Nginx 配本机 HTTP readiness；未配置时系统会回退 SCM 并显示警告。
6. 先点 `Run now` 验证整图；成功后再 arm。首次 arm 只记录当前 epoch，不会立即启动。

成员选择只展示最近有效报告仍包含的服务。API 也拒绝把 tombstone 新加入 Group；既有成员后来离开 allowlist 时会保留并在 arm 时显示 `SERVICE_NOT_REPORTED`，管理员可在 DISARMED 状态移除，或先让同一 local ID 恢复上报后继续使用。

## 7. 真实三机断电演练

推荐链路：`MySQL + Redis → Nacos → Java → Nginx`，至少分布在三台 Windows Server。

| 轮次检查 | 通过标准 |
|---|---|
| 随机开机顺序 | 上游未 READY 时下游零 start Operation |
| CP 最后启动 | Agent 恢复心跳后完整等待 120 秒稳定窗口 |
| 缺少任一节点 | Group 为 `WAITING_FOR_NODES`，零部分子图动作 |
| Agent 进程重启 | boot_id 不变，不创建新 AUTO Run |
| 单机 OS 重启 | 新 epoch 只创建一个 AUTO Run；已 ACTIVE 上游只 probe |
| start/probe 失败 | 可达下游 `BLOCKED`，独立分支继续，根因可见 |
| CP 中途强退 | 重启后沿原 Run/operation_id 继续，不从头重放 |

连续完成 10 轮成功冷启动并记录 Run ID、epoch 和开机次序；每轮 AUTO Run 都必须 `SUCCEEDED` 且全部 Step `READY`。start/probe 失败、Agent 断线等故障注入另建 MANUAL Run 和场景证据，不得占用或冒充十个成功轮次。只有零乱序、零重复 AUTO Run、零未知服务操作，并且所有独立故障场景都有确定可见根因时，才可把 MVP 标为“真实环境验收通过”。

每轮证据应按 [Recovery MVP 三机断电验收证据合同](contracts/recovery-mvp-evidence-v1.md) 汇总为本地 JSON，并在验收工作站用发布包中的只读校验器复核：

```powershell
.\dist-recovery\winsw-recovery-evidence-validator\winsw-recovery-evidence-validator.exe `
  .\acceptance\campaign.json `
  --report .\acceptance\campaign.report.json
```

从源码仓库运行时也可使用 `python .\scripts\validate_recovery_evidence.py ...`，但两者必须生成同一 Schema/报告。校验器不连接或控制远程主机；它复算 epoch/Run 唯一性、Kahn 拓扑层、READY 放行顺序、失败下游 BLOCKED 和 Operation/allowlist 对账。CP 最后启动、进程/OS 重启、缺节点、Agent 断线和 CP 中途强退还必须引用带 SHA-256 与复核人的现场证据，报告会把这些项目单列为 `manual_proof_records`。模板在源码与发布包中都位于 `examples/recovery-evidence.template.json`。`PASS` 不验证人工证据内容，也不证明导出未删改，操作者和最终复核人仍须负责。

## 8. 故障处置

- `UNKNOWN`：先人工核对 Agent Operation 与 SCM 实际状态；不得自动重发或伪造成功。
- `BLOCKED_PRECONDITION`：先 disarm，修复 Automatic/未安装/离线等问题，再重新 arm 建立新 baseline。
- `DRIFT/配置修改`：不属于本 MVP；继续用现有 GUI 管理 WinSW XML。修改后重新核实业务服务仍为 Manual。
- Token 疑似泄漏：立即停止实验网络入口、替换所有 Agent/CP 配置并重启；MVP 没有在线轮换或 TLS。

### 8.1 CP v4→v5 容量迁移失败

若启动错误指出 Agent、当前可见服务或 Group 超过 1024，MVP **不支持原地删除历史修库**。1024 项应原子升到 v5；1025 项必须保持 v4 和全部原数据并 fail closed。处置顺序固定为：

1. 保持 `winsw-recovery-control-plane` 停止；不得反复启动、删库或用 SQL 解除外键。
2. 从 `control-plane.json` 解析实际 `database_path`。服务停止后，把数据库主文件及同名前缀的 `-wal`、`-shm`（若存在）复制到 DataDirectory 之外的受控目录，同时复制配置；对全部副本计算 SHA-256，记录时间、主机和操作者。原文件一律只读保留。
3. 现有 MVP 没有受支持的 v4 原地压缩/删除工具。若必须保留原 Group、代理审计和历史状态，停止发布并提交专门的离线迁移脚本评审；未通过评审不得继续。
4. 可接受重建 CP 状态时，先从已冻结的部署清单导出 Agent、服务映射、Group、依赖与 readiness；把配置中的 `database_path` 改为 DataDirectory 内一个**不存在的新文件名**，再启动当前 v5 包。等待全部 Agent 重新注册后，按清单重建恢复组，先手工 Run，通过后再 arm。旧 v4 库与哈希作为审计证据保留，绝不覆盖。
5. 重建验证必须证明新库 `schema_versions` 的 `control-plane=5`、公开三类集合均不超过 1024、Dashboard/Group/Run 可读，并重新执行 `PostInstall` 与一次受控手工恢复。任一步失败即停止服务并保留新旧两套证据。

回滚到旧程序只有在旧 Frozen 包、其清单与 wrapper 切换方案已经单独评审时才允许；本仓库的 install-only 脚本不是升级/回滚器。没有已批准旧包时，安全回滚状态是“CP 保持停止、业务服务不由编排器动作”，而不是强行用新程序打开超限 v4 库。
