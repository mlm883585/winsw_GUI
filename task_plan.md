# Task Plan: Recovery MVP 合同实现、部署与三机验收

## Goal

根据冻结方案文档完成 Recovery MVP 编码并逐项证明实现符合合同，再将其部署到独立 Control Plane 管理节点和至少三台 Windows Server 业务节点，以连续 10 轮随机冷启动证据证明严格依赖恢复满足合同。

## Success Criteria

- 目标节点全部通过 `PreInstall(Frozen)` 与 `PostInstall`。
- 所有业务服务保持 `Manual`，Agent 与 Control Plane 为 `Automatic`。
- 恢复链至少覆盖 `MySQL + Redis → Nacos → Java → Nginx`。
- 连续 10 轮随机冷启动中：零乱序启动、零重复 AUTO Run、零未知服务操作。
- 所有失败演练均有确定状态、阻塞根因和可导出的证据。
- 冻结 evidence validator 对最终证据返回 PASS。
- 每项 Recovery MVP MUST 均能映射到实现、自动化测试或明确的真实主机验收证据；不能仅凭测试总数推断完成。

## Current Phase

Phase 1S — 最终独立审计缺口修复（完成后重新冻结 Phase 2 候选）

## Phases

### Phase 0: 已有候选基线

- [x] 冻结 Recovery MVP 合同、OpenAPI 与追踪矩阵
- [x] 完成 Agent、Control Plane、严格 DAG 与最小 Web
- [x] 生成三个 PyInstaller onedir 包
- [x] 完成 WinSW lock、安装前后预检和离线证据校验器
- [x] 通过 Windows PowerShell 5.1、129 项测试和只读全栈烟雾
- **Status:** complete

> Phase 0 只证明已有候选包和既有测试通过，不等价于方案要求已逐项完成；Phase 1 负责重新审计当前工作树。

### Phase 1: 方案到实现完成度审计与缺口编码

- [x] 从 Recovery MVP 合同、Agent/CP OpenAPI、追踪矩阵和部署手册提取全部编码要求
- [x] 审计 Agent：身份、allowlist、SCM、Operation journal、幂等、心跳、探针和恢复
- [x] 审计 Control Plane：租约、服务镜像、恢复组、epoch、严格 DAG、崩溃恢复和 Web
- [x] 审计持久化、迁移、安全边界、打包脚本和证据导出链路
- [x] 对每个未实现或证据不足项补充代码与测试
- [x] 全量回归、冻结包重构建并更新实现追踪结果
- [x] 修复CP显式null、canonical UUIDv4、严格组数值、Run reason Schema与配置交叉校验
- [x] 修复RecoveryEngine Agent Operation语义错绑
- [x] 修复真实AgentClient缺失/非法Operation响应未分类为AGENT_PROTOCOL_MISMATCH
- [x] 修复blocked_reasons超过100导致组卡SETTLING
- [x] 修复公开集合超过1024导致响应500
- [x] 补齐最小Web readiness回显与删除
- [x] 稳定全量回归、重新构建并验证Frozen包
- **Status:** complete

### Phase 1S: 最终独立审计缺口修复

- [ ] 修复 CP 手工代理的 Agent Operation 完整语义绑定与错误脱敏
- [ ] 修复 Evidence validator 的时间单调性、导出时序与阻塞依赖链校验，并保留人工复核字段
- [ ] 补齐 Groups 依赖可编辑标识与 Run Detail 冻结依赖/探针快照
- [ ] 修复 PreInstall 端口提供程序失败被误判为空闲
- [ ] 为安装事务增加按角色与数据目录隔离的跨进程互斥
- [ ] 修复构建器复用输出根时把未知顶层内容纳入发布清单的 fail-open
- [ ] 评估并处理 WMI boot identity 稳定 CLI 错误与单服务 Operation 轮询两个 P2
- [ ] 定向回归、全量回归、重建并复验 Frozen 候选
- **Status:** in_progress

> 2026-07-17 的三路独立续审没有发现 P0，但发现上述可本地修复的 P1；因此部署仍保持阻断，原 Phase 2 Frozen 仅作为上一审计点，不能代表即将修复后的源码。

### Phase 2: 目标清单与部署授权

- [x] 冻结非秘密 Deployment Inventory v1 合同与示例
- [x] 实现 Control Plane `--prepare-deployment` 严格校验和配置草案渲染
- [x] 实现本机只读 Host Facts 采集脚本（零远程扫描、零系统写入）
- [x] 补齐测试、发布树复制与新 Frozen 候选验证
- [ ] 收集独立管理节点及至少三台业务节点的主机名、IP 和 Windows Server 版本
- [ ] 冻结每台机器的 `service_id → Windows Service Name` allowlist
- [ ] 冻结 readiness 类型、端口或 HTTP 路径
- [ ] 确认防火墙端口、CP 来源 IP、执行方式及 EDR 放行
- [ ] 确认 Token、管理员密码和 session secret 仅在目标机本地生成/保存
- [ ] 在任何服务注册或启动前回读本计划并取得明确部署授权
- **Status:** in_progress

> 现场输入仍是进入 Phase 3 的外部前置条件；同时必须先完成 Phase 1S、本地回归并重建 Frozen，不能用旧候选进入现场预检。

### Phase 3: 只读部署前检查

- [ ] 在每台目标机复算 `SHA256SUMS.txt`
- [ ] 创建本地配置和专用数据目录
- [ ] 执行 `PreInstall(Frozen)`，保存 JSON 报告
- [ ] 处理所有 FAIL；WARN 仅按合同允许的首次安装 ACL 差异处置
- [ ] 更新 findings.md 和 progress.md 后再决定是否安装
- **Status:** pending

### Phase 4: 安装与安装后复核

- [ ] 在管理节点安装固定 Control Plane 服务
- [ ] 在业务节点安装固定 Agent 服务
- [ ] 使用 WinSW v2.12.0 lock 校验版本、大小和 SHA-256
- [ ] 执行 `PostInstall` 并核对 SCM、父进程链、端口、SQLite 和 ACL
- [ ] 验证心跳、节点在线状态和 allowlist 服务镜像
- **Status:** pending

### Phase 5: 恢复组与受控演练

- [ ] 配置严格 DAG 和每服务最多一个 readiness
- [ ] 验证环检测、必需节点集合和业务服务 Manual 门禁
- [ ] 执行一次手动 Run，验证上游 READY 后才放行下游
- [ ] 启用自动恢复并记录 boot epoch 基线，确认不会立即误启动
- **Status:** pending

### Phase 6: 三机断电与失败场景验收

- [ ] 连续完成 10 轮随机冷启动
- [ ] 覆盖 CP 最后启动、Agent 单独重启、单节点重启和必需节点缺失
- [ ] 覆盖 start 失败、探针超时、Agent 断线、CP 中途退出及未知服务拒绝
- [ ] 导出完整 Run、Step、Operation、Probe 和人工动作证据
- [ ] 用冻结 validator 验证证据为 PASS
- **Status:** pending

### Phase 7: 验收结论与交付

- [ ] 独立 Reader Test 复核现场证据和根因可解释性
- [ ] 更新 roadmap/readme 的 MVP 状态；未通过则保持实验候选
- [ ] 形成部署清单、已知风险、回滚步骤和最终验收结论
- **Status:** pending

## Required Inputs / Current Blockers

| 输入 | 当前状态 |
|---|---|
| CP 管理节点主机名/IP/Windows 版本 | 待用户提供 |
| 至少三台业务节点清单 | 待用户提供 |
| 每台机器准确 Windows Service 名称 | 待用户提供 |
| readiness 端口或 HTTP 路径 | 待用户提供 |
| WinRM/RDP/现场执行方式 | 待用户选择 |
| 防火墙与 EDR/WinSW 放行 | 待现场确认 |

## Decisions Made

| Decision | Rationale |
|---|---|
| 使用 `planning-with-files` 根目录兼容模式 | 当前仓库没有既有计划，且当前只有一个连续部署任务 |
| 先完成实现审计，再进入现场阶段 | 活动目标是根据方案文档编码；既有测试通过不能证明全部 MUST 已实现 |
| 目标清单与授权完成前不安装 | 主机、服务映射和执行授权会直接决定部署行为，不能推测 |
| 保持 HTTP + Token | 用户已接受普通受控局域网实验边界；不得标记为生产安全方案 |
| 不接管 WinSW XML/ServiceConfig | Recovery MVP 只编排预先注册的 Windows Service |
| 不在聊天中收集秘密 | Token、密码和 session secret 必须在目标机本地生成与保存 |
| 真实三机证据通过前不宣称 MVP 可用 | 当前只完成构建机和只读部署门禁，尚未验证断电恢复 |
| 先修 P0 安全与持久化不变量，再处理格式漂移 | 防止错误服务副作用、探针越界和不可恢复数据库状态 |
| Phase 2工具新增前先修新发现的公开API P1 | 显式null可产生真实代理副作用，非规范UUID绕过公开输入合同，风险高于部署便利性 |
| RecoveryEngine Operation语义绑定先于所有Phase 2新增 | 错绑SUCCEEDED可能违反严格依赖并启动下游，属于恢复正确性核心边界 |
| stale 服务只可由既有 Group 保留或移除，不可新加入 | 防止保存必然无法 arm 的新配置，同时保留服务暂时离开 allowlist 时的稳定 UUID 与可解释根因 |
| MVP 不自动 GC 或硬封顶 tombstone 服务 | 多张审计/组/锁表以 RESTRICT 引用服务；采用 active partial index 与“allowlist/local ID 稳定、异常轮换视为配置或凭据事件”的运维约束，避免无恢复工具的硬上限卡死心跳 |
| Frozen重建必须等待真实AgentClient畸形Operation分类修复 | 当前虽fail closed，但缺字段/非法UUID被记为普通UNKNOWN且failure_code为空，违反RUN-13稳定可观测合同 |
| Phase 2先实现非秘密清单机器边界 | 当前单机配置模型无法在生成配置前证明独立CP、至少3个Agent、服务映射、readiness归属与严格DAG；工具只渲染含无效secret sentinel的草案，不扫描远程主机或修改系统 |
| 保留Phase 1R Frozen目录不原地覆盖 | Phase 2新增工具会改变源码与发布内容；旧目录继续作为已验证回退点，新候选必须使用新目录并重跑全部发布门禁 |
| `--prepare-deployment` 必须保持离线最小加载 | Inventory 合同明确成功路径不得加载 uvicorn App；Web 依赖只允许在正常 CP 启动分支惰性导入 |
| 临时目录清理失败必须显式失败 | 原子发布不只是不生成目标目录；不能用 `ignore_errors=True` 隐藏遗留中间文件，清理异常应归一为脱敏 `DeploymentRenderError` |
| Phase 2 Frozen 候选使用 `dist-recovery-phase2-20260717` | 该路径是仓库根下尚不存在的直接 `dist-*` 子目录，满足构建器约束；保留已验证 Phase 1R 目录作为回退点，不原地覆盖 |
| 烟雾临时目录成功与失败都必须清理 | 失败日志已在异常分支输出；保留配置/SQLite会遗留随机 Token 与 session secret。只能在解析后确认路径属于系统 temp 且名称符合受管前缀时递归删除 |
| 最终独立审计的 P1 必须先于现场清单推进 | 错绑 Operation、Evidence false-PASS、端口查询 fail-open 和安装并发竞态会直接破坏正确性或安全门禁；旧 Frozen 不再作为现场候选 |
| 构建输出根遇到未知顶层项必须拒绝且不得删除 | 旧证据、笔记或秘密不能被静默纳入 `SHA256SUMS.txt`；门禁必须发生在 Python/PyInstaller 等构建副作用前 |

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 技能定位的组合 `rg` 命令返回退出码 1，但已输出目标路径 | 1 | 判断为部分搜索根无匹配导致的聚合退出码；直接读取已发现的 `SKILL.md`，未重复原命令 |
| 跨模型/错误/两端 App 的组合补丁找不到 Agent 端点预期上下文，整体未应用 | 1 | 不重复原补丁；拆分公共层与 App 层，先读取精确函数签名 |
| 新增动作正文测试错误引用 `AgentStore.db`，导致 1 failed | 1 | 行为响应已为422；读取 Store 真实持久化属性后只修测试夹具 |
| 子 Agent 正在修改发布验证器时并发运行 packaging tests，出现收集/执行版本竞态 | 1 | 停止触碰该文件；等待子任务完成后再验证最终版本，后续测试排除活跃代理写入范围 |
| 读取安装器及其预期独立测试时引用了不存在的 `tests/test_install_recovery_service.py` | 1 | 已获得安装器正文；不重复该路径，后续从 `test_packaging_contract.py` 中定位安装器契约测试 |
| RecoveryRun 子任务的组合读取在 PowerShell 下对 `orchestrator/common/*.py` 使用 `rg`，Windows 通配路径返回 1 并使组合输出丢失 | 1 | 不重复该组合；改为读取明确文件路径，后续只让 `rg` 搜索目录或显式文件列表 |
| RecoveryRun 收尾并行读取两次把“可选文件/匹配不存在”的退出码1混入组合，导致其他只读输出被丢弃 | 2 | 停止把任何可零匹配命令放入并行组合；只读正文单独执行，可选检查使用明确存在性分支 |
| 审计 CP 模型时假定存在 `orchestrator/control_plane/models.py` | 1 | CP 公开模型实际集中在 `orchestrator/common/models.py`；后续按现有模块定位，不重复错误路径 |
| Agent 子任务全量回归中 Host Preflight 仍把故意无效的 CP example 当作应通过配置，导致 1 failed | 1 | Agent 范围 71 passed；该失败归入 root 的 example/preflight 合同迁移，需让测试生成临时强秘密配置而非放宽 sentinel 校验 |
| 修复 Host Preflight 夹具时从错误的 `control_plane.auth` 导入 `hash_password`，测试收集失败 | 1 | 已用 `rg` 定位权威实现为 `orchestrator.common.security`，只修导入后重跑目标测试 |
| PowerShell 未展开传给 `rg` 的 `tests/test_control_plane*.py` glob，Windows 报路径语法错误 | 1 | 后续对 `tests` 根目录搜索并使用 `-g 'test_control_plane*.py'` 过滤，不重复 shell glob |
| Run Detail 首次规划更新因其他 Agent 并发插入内容导致补丁上下文失效 | 1 | 确认补丁无部分应用；重新读取精确相邻行后以最小上下文追加，不覆盖并发内容 |
| logout CSRF 的跨 auth/app/tests 组合补丁因 App 函数签名换行上下文不匹配而整体未应用 | 1 | 已确认无部分变更；拆为 auth、App 精确单行与测试三个补丁，不重复组合上下文 |
| Run Detail 联合回归在 Store v3 已输出 `blocked_reasons`、公共模型子任务尚未落地的短暂窗口出现 1 failed | 1 | UI 专项已通过；不让 UI 子任务越界，等待负责模型/OpenAPI的子任务完成后从稳定工作树联合重跑 |
| 读取组 UI 时假定存在 `templates/group_detail.html`，实际组列表与编辑器合并在 `groups.html` | 1 | 已成功读取权威 `groups.html`；不重复缺失路径，Run 详情按测试/模板清单另行定位 |
| 搜索 probe 小数测试值的复合 `rg` 正则无匹配返回退出码 1 | 1 | 不重复该复杂正则；直接按字段分别搜索或读取模型消费路径，零匹配不再混入组合命令 |
| 稳定态全量回归在模型容量收紧后出现3项旧夹具失败：两项 heartbeat 返回空 services，一项 endpoint 测试也构造空 services | 1 | 194项已通过；合同/Agent配置要求至少1个allowlist服务，不放宽模型，改为让三项测试构造最小合法 ObservedService 后重跑 |
| 查 heartbeat 观察器时假定存在 `orchestrator/agent/service_observer.py` | 1 | HeartbeatReporter 的实际回调类型已从 `heartbeat.py` 取得；不重复缺失路径，测试直接构造 `ObservedService` |
| 首次冻结包重建给 `shell_command` 设置了 1 秒调用超时，返回 exit 124，构建是否残留尚未确认 | 1 | 不原样重试；先检查构建进程和 `dist-recovery-20260717` 半成品，再使用长命令超时并由外层短周期 yield/wait 观察 |
| 发布终审的只读 PowerShell 清单命令在 `foreach` 后直接接管道，两次触发 `ParserError: empty pipe element` | 2 | 均无副作用；提前采用 3-strike 替代路线，完全取消尾部管道并逐行输出，不再使用该语法形态 |
| 同步审计错误与发现的多文件补丁使用了无效 hunk 分隔，`apply_patch` 拒绝且未产生部分修改 | 1 | 拆成每个文件独立、带精确上下文的补丁；不重复原补丁结构 |
| 同步发布审计错误与构建测试结果的双 hunk 补丁因测试表精确上下文校验失败而整体未应用 | 1 | 已确认无部分修改；先定位目标行，再按单一 hunk 分别更新 Error Log 与 Test Results |
| 发布审计把不存在的可选 `C:\Approved` WinSW 资产路径与仓库查询组合，命令 exit 1 且无输出 | 1 | 无副作用；后续先 `Test-Path`，再分别查询存在的路径，不重复可选路径组合 |
| progress 跨 Error Log/Test Results 的第二个多 hunk 补丁再次因测试表上下文未命中而整体拒绝 | 1 | 停用跨表多 hunk 更新；先定位后分别使用单一 hunk，避免重复该失败形态 |
| 发布 P1 首轮故障注入中，测试替身 `Restore-RecoveryAcl` 无视 `journal.acl_apply_started`，把 ACL 前失败误记为已恢复 | 1 | 产品代码无失败证据；仅让替身遵循真实实现的 journal 门禁后重跑，不放宽产品断言 |
| Unicode 对抗实验直接向 GBK PowerShell 控制台打印 emoji，触发 `UnicodeEncodeError`，脚本在第二个样本中止 | 1 | 已取得孤立 surrogate 失败证据；后续只输出 ASCII 转义/布尔结果，不重复直接打印非 ASCII 值 |
| 发布 P1 相关 pytest 为 30 passed/1 failed：pytest 子进程内 PS5.1 未自动加载 `Microsoft.PowerShell.Security/Get-AuthenticodeSignature` | 1 | 直接 harness 已通过；故障注入固定该 OS 边界为 NotSigned fake，不放宽产品对真实签名状态的校验，再重跑 |
| Unicode 合同多文件补丁使用 JavaScript raw template literal，Markdown 反引号提前终止字符串并触发 SyntaxError | 1 | 工具未调用、无文件修改；改为逐文件普通字符串并正确转义反斜线，不重复 raw template 形态 |
| Agent P1 首次 Store+CLI 组合补丁误判 `store.py` import 相邻上下文，`apply_patch` 校验失败 | 1 | 无部分修改；已先读取精确文件头，拆为 typed exception/raises 与 CLI 两个最小补丁，不重复组合上下文 |
| 最终示例拒绝检查把配置路径作为位置参数传入；两个CLI因参数语法错误同样返回2，形成假阳性 | 1 | 从usage确认必须使用 `--config PATH --check-config`；作废该证据，按正确语法重跑并核对稳定配置错误而非仅退出码 |
| 正确CLI语法的示例检查假定错误文本包含 `configuration is invalid`，实际稳定输出使用不同文案，断言失败 | 1 | 已确认两端均非usage且exit2；不重复猜测文案，先从权威CLI测试定位结构化字段，再按JSON字段验证 |
| 并行重跑PowerShell harness时未先读取参数，安装事务脚本缺必填 `RepositoryRoot`，组合调用exit1且另一结果丢失 | 1 | 不重复无参/组合调用；先读取两个param块，再分别按权威参数运行，避免一个失败遮蔽另一个 |
| 动作矩阵首轮专项为57 passed/1 failed，暴露Worker首次SCM query异常被错误分类为REJECTED | 1 | 测试正确捕获产品漂移；新增严格Worker观察边界，保持admission/recovery语义，修复后58 passed |
| 尝试为CP Readiness strict修复再开子Agent时返回 thread limit reached | 1 | 不重复创建；将明确范围交给仍活跃的最终冻结审计Agent实现，避免等待空槽 |
| Phase 2 CLI/打包测试组合检索包含不存在的 `pyproject.toml`，导致 `rg` 报路径错误 | 1 | 保留已成功读取的测试证据；后续只对经 `rg --files` 确认存在的文件检索，不重复该组合 |
| 等待并行审计时误将 `wait_agent` 超时设为1000ms，低于工具最小10000ms | 1 | 未发生等待或文件副作用；后续使用10000ms以上并继续本地工作，不重复无效参数 |
| 同步新P1发现的三文件补丁因`task_plan.md`决策行写成“探针绕过”而实际为“探针越界”导致整体校验失败 | 1 | 已确认无部分修改；按当前精确行拆分补丁并同时记录该错误，不重复原多文件上下文 |
| 配置修复子任务调用了未安装的ruff模块 | 1 | 未修改环境；不重复该命令，继续使用项目已有compileall/diff-check/pytest门禁 |
| blocked-reason补丁假定`_json_dump`为单行return，实际为多行`to_jsonable_python`实现，补丁校验失败 | 1 | 已确认无部分修改；读取精确helper和调用点后拆分应用，不重复原上下文 |
| 定位candidate result测试用法时复合`rg`正则括号未闭合，命令exit1 | 1 | Store正文已成功读取；后续使用简单字面搜索/明确行范围，不重复该正则 |
| 容量补丁假定需新增409/CAPACITY_EXCEEDED，但冻结合同已明确超限为422/VALIDATION_ERROR，且文档上下文未命中 | 1 | 整体无部分修改；遵循现有权威合同，取消新错误码并拆分为Store原子422门禁与测试 |
| 容量升级语义检索时假定存在 `docs/operations/recovery-mvp-deployment.md`，实际路径不存在 | 1 | 保留已成功读取的 Store/DB 证据；后续先用 `rg --files docs` 定位真实运维文档，不重复错误路径 |
| 容量 Store/API 双文件测试补丁假定了不准确的 CP 测试函数名，导致整组校验失败 | 1 | 已确认补丁未应用；先定位现有函数精确名称，再逐文件使用最小上下文补丁，不重复组合补丁 |
| Web 探针编辑审计假定存在 `static/style.css`，实际样式文件是 `static/app.css` | 1 | 子任务未重复错误路径，已用真实文件完成实现与测试；root 后续按文件清单复核 |
| Web 探针编辑子任务一次行号检索使用了错误转义的 `rg` 正则 | 1 | 无代码副作用；已改用 `Select-String` 取得行号，不重复该正则 |
| 查询固定 Control Plane service ID 时把有价值的安装器正文与可零匹配的 preflight `rg` 组合，后者使命令整体 exit 1 | 1 | 已从成功输出确认 service ID 为 `winsw-recovery-control-plane`；不重复组合查询，运维步骤直接使用权威安装器值 |
| v5 首次全量回归有一项旧断言仍固定期待 CP schema version 4 | 1 | 313项已通过且失败只在版本断言；不重复全量，先把权威当前版本断言更新为5并运行该目标/Store集，再重新全量 |
| 修旧 schema 断言时使用过宽上下文，误把“v5 超限迁移应保持 v4”改成5，未命中真正失败断言 | 1 | 已用位置审计发现且尚未运行测试；按测试函数名精确恢复超限断言为4、把新库断言改为5，再定向验证两项 |
| 畸形Operation测试检索时假定存在 `tests/test_control_plane_agent_client.py` | 1 | 已从同次输出取得RecoveryEngine命中；后续只读取真实的 `test_control_plane_store_agent_client.py` 和经文件清单确认的测试，不重复错误路径 |
| Phase 1S 三文件规划同步补丁遗漏 `*** End Patch`，工具在校验前拒绝 | 1 | 无文件修改；补齐完整补丁边界并把本错误同步记录，不重复不完整 patch |

| Run Detail 联合 UI/CP 回归撞上 Store v3/公共模型并发版本窗口：Store 已返回 `blocked_reasons`，模型尚未接收 | 1 | 不越界修改 common models；专项 UI 测试已通过，等待合同/模型子任务收敛后从稳定工作树重跑 |

| 续作规划同步补丁误把 `progress.md` 的 Next Update Trigger 当作 `findings.md` 章节，补丁未应用 | 1 | 已确认无部分修改；读取真实文件尾部并改用精确锚点，不重复错误上下文 |
| Phase 1R只读审计子任务的复合`rg`正则括号/引号未闭合，exit 1 | 1 | 无副作用；子任务已改用`Select-String -SimpleMatch`定位，不重复该正则 |
| 收尾脚本把Git换行提示的PowerShell ErrorRecord深度JSON化，输出出现truncated提示 | 1 | 门禁exit 0和所有计数完整；后续只保留简短摘要，不重复完整ErrorRecord序列化 |
| Host Facts首轮9项测试有1项失败：PowerShell 5.1测试替身把单元素`listen_addresses`序列化为字符串 | 1 | 生产路径已有数组包裹；仅修测试替身保持`object[]`并补Invoke层字段白名单，不放宽输出Schema |
| Host Facts复跑9项全绿，但pytest无法写现有`.pytest_cache`并产生WinError 5 warning | 1 | 产品测试已通过；后续测试加`-p no:cacheprovider`避免无关缓存写入，不修改ACL或环境 |
| 共享探针扩大到Agent App测试时6项setup因默认`%TEMP%\pytest-of-maoliang` ACL WinError 5 | 1 | 探针43项仍通过且非产品失败；后续统一使用工作区内显式`--basetemp`并禁用cacheprovider，不重复默认temp命令 |
| Phase 2 packaging首次测试因默认TEMP ACL出现15个setup错误 | 1 | 改用工作区唯一`--basetemp`后新增2项与23项相关集通过；不修改系统ACL |
| Packaging完整集的CP help在并行Inventory模块尚未落盘时导入失败 | 1 | 属于受控并行中间态；排除该1项验证其余23项，待模块完成后从收敛工作树重跑完整集，不在packaging范围伪造模块 |
| Architecture/Roadmap同步补丁假定“恢复编排”后紧邻“配置权威”，实际中间还有控制台/通信行，补丁未应用 | 1 | 已确认无部分修改；读取精确表格后以单行锚点分别补入，不重复相邻行假设 |
| Root首次工作区`--basetemp .test-tmp/<uuid>`未先创建父`.test-tmp`，Agent App 6项setup FileNotFound | 1 | 探针43项仍通过；下一次先创建并验证工作区父目录，再使用唯一子目录，不重复缺父目录调用 |
| Inventory CLI 专项使用 `uv run pytest` 控制台入口时，测试收集无法导入工作区 `orchestrator` | 1 | 编译已证明源码可导入；不重复该入口，改用 `uv run python -m pytest` 保持仓库根进入 `sys.path` |
| 改用 `uv run python -m pytest` 时 uv 无权初始化用户级 cache，命令在测试前退出 | 1 | 不修改用户 cache ACL、不申请无必要提权；项目依赖已在当前 Python 可用，后续直接使用 `python -m pytest` |
| Inventory 独立测试首跑为 92 passed/2 failed：测试编写期间公开渲染参数由旧 `input_sha256` 收敛为 `inventory_sha256` | 1 | 属于受控并行签名窗口而非产品失败；仅让负责代理更新两处测试调用后复跑，不回退已冻结的 manifest/API 名称 |
| 读取 Inventory strict 参数测试时把正文读取与可能零匹配的容量 `rg` 组合，后者 exit 1 使整次工具标记失败 | 1 | 已取得所需正文且无副作用；不重复组合，后续对已知位置直接读取并单独添加缺失测试 |
| Host Facts 只读审计首个 Unicode stdout 探针因外层 PowerShell 与 `python -c` 嵌套引号冲突触发 ParserError | 1 | 子进程未执行且无文件副作用；不重复嵌套引号，审计改用 `-EncodedCommand` 或直接字节捕获 |
| Phase 2 Frozen 构建错误沿用了 1 秒 shell timeout，命令 exit 124 且已开始 pip 阶段 | 1 | 这是此前已知错误模式；不直接重跑。先检查 Python/PyInstaller 子进程及新输出目录半成品，再使用足够长命令超时并通过 yield/wait 观察 |
| 构建残留检查使用 `Get-CimInstance Win32_Process` 时当前权限被拒绝，且并行组合遮蔽了目录检查输出 | 1 | 不申请仅为诊断的提权、不重复 CIM；改用 `Get-Process` 的只读进程摘要，并把输出目录检查单独执行 |
| Phase 2 Frozen 只读烟雾在 113.9 秒后失败：当前账户访问 `Win32_OperatingSystem.LastBootUpTime` 被 WMI 拒绝，Agent fail closed，CP正常启动但Agent health超时 | 1 | 不重复同权限烟雾、不放宽启动身份合同；先确认脚本已清理18765/18766与受管进程，保留日志作为环境证据。目标 Windows Server 以计划安装身份/提升权限执行真实烟雾与PreInstall |

## Maintenance Rules

- 每次关键发现后更新 `findings.md`。
- 每个阶段完成后同时更新本文件和 `progress.md`。
- 所有错误立即记录；同一失败不得原样重试。
- 重要部署决策前先回读本文件和相关 findings。
- 每两次查看、搜索或浏览操作后，立即把关键结论写入 `findings.md`。
