# Progress Log: Recovery MVP 现场部署与三机验收

## Session: 2026-07-16

### Continuation: 2026-07-17

- **Status:** Phase 1R in progress；独立完成度审计发现真实P1，原Frozen候选已过期，Phase 2暂缓。
- Re-read `task_plan.md`, `findings.md`, and `progress.md` at continuation start.
- Ran planning-with-files session catch-up; it reported six messages consisting of the new environment/goal continuation and the current status update, with no lost implementation decision.
- Re-ran `git diff --stat`; as before, it only sees the ten already tracked files while the Recovery MVP implementation remains untracked, so verification will inspect the filesystem and execute tests directly.
- First stable full regression after all prior agents stopped: `194 passed, 3 failed, 4 warnings`. All three failures are stale tests constructing `AgentReport.services=[]` after the public contract was tightened to at least one service; no production behavior failed.
- Updated the three stale fixtures with one valid observed MySQL service; targeted heartbeat and endpoint-source tests now pass `3/3` without relaxing the public schema.
- Stable full regression after fixture repair: `197 passed, 4 warnings` in 41.58s. The warnings are the known third-party jsonschema/OpenAPI validator deprecations; there are no product test failures.
- `git diff --check` and `python -m compileall -q orchestrator` both pass on the stable worktree.

### Phase 0: 可部署候选基线

- **Status:** complete
- 已继承并核对上一阶段结论：合同、实现、构建、安全门禁、证据校验器与只读烟雾均已完成。
- 验证基线：129 项测试通过；330 个发布文件完整匹配；Windows PowerShell 5.1 烟雾为 `SideEffects=NONE`。

### Phase 1: 方案到实现完成度审计与缺口编码

- **Status:** reopened / in_progress
- **Started:** 2026-07-16
- **Completed:** 2026-07-17
- Completion evidence: 最终稳定回归269 passed；静态检查通过；`dist-recovery-frozen-20260717`完整性331/331、包内Frozen smoke、example/evidence负向门禁、安装事务8场景、listener所有权、清理与二进制新鲜度全部PASS；真实三机/10轮证据明确保留给Phase 2–7。
- Reopened: 独立复审随后动态确认Operation错绑、blocked reason容量、集合容量和Web readiness编辑缺口；已有Frozen包不再是部署来源。
- First correction batch: 67 passed/4 upstream warnings；完成显式null、canonical UUID、strict group数值、Run reason及配置交叉校验。
- Config security sub-suite: 39 passed；compileall/diff-check通过，Agent/CP网络组合和PBKDF2完整性已闭合。
- Operation binding: POST/GET全字段绑定、稳定AGENT_PROTOCOL_MISMATCH、零probe/下游放行；48项相关及当时全量300 passed。
- Blocked reason capacity: 101问题确定收敛为99代表项+1汇总并持久BLOCKED；专项2 passed。
- Actions taken:
  - 定位并完整读取 `planning-with-files` v3.5.1 技能说明。
  - 检查根目录和 `.planning`，确认此前没有规划材料。
  - 运行 session catch-up，未发现未同步计划上下文。
  - 阅读三份模板和 Windows 初始化脚本。
  - 使用技能脚本初始化 `task_plan.md`、`findings.md`、`progress.md`。
  - 立即将真实目标、当前基线、剩余阶段、风险、所需输入和错误写入三份文件。
  - 新一轮 session catch-up 检测到未同步上下文；回读三份规划文件。
  - 发现活动目标是根据方案文档编码，而原计划错误地跳过了逐项实现审计。
  - 更新任务计划：新增当前 Phase 1“方案到实现完成度审计与缺口编码”，现场阶段后移。
  - 按 catch-up 建议运行 `git diff --stat` 并盘点合同、API、实现、脚本和测试文件。
  - 确认大部分 Recovery MVP 文件尚未被 Git 跟踪，后续以当前工作树文件内容为权威证据。
  - 完整回读 Recovery MVP 冻结合同与追踪矩阵，并提取身份 fencing、崩溃窗口、epoch/settle、持久化和测试层级等高风险要求。
  - 启动三个只读并行审计：Agent、Control Plane/恢复引擎、API/部署/追踪证据。
  - 搜索占位实现与高风险持久化标识；未发现显式 TODO/NotImplemented，占位风险转为语义和事务边界审计。
  - 在审计开始时重新运行全量测试：129 passed、4 个第三方弃用警告。
  - 确认 Coverage.py 7.15.2 可用，准备辅助定位未覆盖核心分支。
  - 执行 Coverage.py 全量测试：总体 80%；记录 Agent operations/probes/SCM 与 CP client/app/recovery/store 为优先语义审计区。
  - 盘点证据生成链和测试名称；发现当前只有离线校验器、没有自动 evidence exporter，并列出需要证明是否已间接覆盖的合同场景。
  - 审计 SQLite 公共边界：实现具备事务化顺序迁移，但测试证据不足，且 busy_timeout 参数未强制为正数。
  - 修改 `SQLiteDatabase`：拒绝非正 `busy_timeout_ms`；新增顺序迁移、失败回滚、外键和 PRAGMA 测试。
  - Agent 并行审计动态复现探针地址 TOCTOU、DNS fail-open、动作正文额外字段被忽略和框架错误体不统一；已记录为待修复项。
  - 检查 CP 安全路由与现有测试，标记注册/心跳 Token 缺失/错误路径为待确认。
  - 确认 CP ingress 实际执行 Token+peer 校验；同时确认通用 HTTPException/500 错误体未统一。
  - 对照 OpenAPI 与 ERR-01/ACT-01，决定采用文档先行：新增三类框架错误码，并为动作 API冻结严格空正文。
  - 定位两份 OpenAPI 的动作路径与 ErrorCode Schema，确认需要同步修改线上格式，不能只改 FastAPI 实现。
  - Agent 审计新增：ServiceSlug regex 漂移、HTTP `:0`/空端口错误回退至80。
  - Agent 审计动态复现跨重启 allowlist 映射变化可把旧 Operation 执行到新 Windows Service；确定 PREPARED fail、DISPATCHING UNKNOWN 的修复方向。
  - 将修复范围提升到身份层：持久化 allowlist 绑定并在启动时拒绝 local id 历史改绑，避免后续新 Run 操作错误服务。
  - 对照追踪矩阵发现 local id→Windows service 不可改绑尚未成文；准备先补合同 SVC-05 和稳定错误码。
  - 文档先行补充 SVC-05、严格空动作正文及框架错误码；两份 OpenAPI 同步新增 EmptyActionRequest 和 ErrorCode。
  - 公共实现已收紧 ServiceSlug，并加入 EmptyActionRequest、统一 HTTPException/500 ErrorResponse 处理器。
  - 读取两端动作路由的精确签名与导入区，准备以拆分补丁加入严格空正文，避免重复首次上下文失败。
  - 两端动作路由已加入共享 EmptyActionRequest；读取 Agent App/Config 测试结构，准备补额外正文、统一错误体和数字开头 slug 回归。
  - 新增 Agent/CP 非空动作正文、框架错误脱敏、无 CORS、CP ingress Token 和非法 slug 回归测试。
  - 审计两端配置、示例与秘密生成器：确认示例可被错误放行、CP `/0` 默认、Agent `/0` 允许、CP loader 泄密风险及发布包缺少无 Python 秘密生成入口。
  - 读取共享安全实现、CP CLI 和 config-check 测试，确定 Frozen `--generate-secrets` 的条件参数与示例测试迁移方式。
  - 修正测试夹具属性后，先单测失败用例通过，再运行相关 23 项测试全部通过。
  - CP 审计新增：数据库状态无 CHECK/可终态回退、多个模型边界漂移、Run Detail 缺 probe 时间。
  - CP/交付审计新增：BLOCKED_PRECONDITION 原因丢失、Run 时间线/发现入口不足、AUTO Run 无可导出列表。
  - CP 审计确认 lease/settle 使用墙钟，可被时钟跳变提前触发；加入 monotonic 与重启保守重置修复清单。
  - Agent 审计复现 slowloris 可突破单次 probe 总 timeout；交付审计确认 WinSW 安装失败无回滚且会阻断重试。
  - 确认 Agent stop 的 FAILED 处理与冻结动作矩阵冲突，决定按现有权威统一为成功目标态。
  - 完成配置安全收口验证：原始示例 sentinel 被拒绝、CP 配置解析错误脱敏、CP 来源 CIDR 边界和冻结 CLI 秘密生成合同均通过。
  - 启动 RecoveryRun 公开可发现列表 P0 子任务；回读合同、追踪矩阵、CP OpenAPI 与规划材料，确认当前仅有详情 API，冻结按 `(created_at, run_id)` 倒序的过滤绑定 keyset 游标方案。
  - RecoveryRun 列表文档阶段完成：合同与追踪矩阵新增 RUN-12；CP OpenAPI 新增管理员 `GET /api/v1/recovery-runs`、三类过滤、有界 limit、不透明 cursor、`items + next_cursor` 响应和固定 422 边界。
  - RecoveryRun 列表实现阶段完成：新增严格响应模型、Store keyset 查询/游标编解码、管理员 API；Dashboard 加入最近 10 个 AUTO/MANUAL Run 并链接既有详情页，未扩展到 exporter、数据库状态约束或 monotonic。
  - RecoveryRun 列表验收测试首轮通过：`tests/test_recovery_run_listing.py` 与 OpenAPI 合同共 8 passed；覆盖 Scheduler AUTO 公开发现、同时间戳分页、过滤、鉴权、固定 422 错误体及 Dashboard 详情链接。
  - 同步并行 Store monotonic lease 合同：Recovery MVP、HB-04、DB-05 增加 CP 重启 fail-closed 与新有效心跳重建租约；列表相关 Store/App/Engine/OpenAPI 回归共 35 passed。
  - RecoveryRun 列表最终定向回归在并行 lease 改动合入后为 38 passed；`compileall` 通过，`git diff --check` 无空白错误（仅既有 Git LF→CRLF 提示）。
  - 完成列表实现只读收尾：确认 API 路由顺序、过滤绑定 cursor、单连接 Run 装配和 Dashboard 链接均保持局部边界，无新增页面或静态依赖。
  - 并行 Store v2 状态约束合入后的最终联合回归由主任务确认：Run listing、Store、Engine、App、OpenAPI 与三机集成共 42 passed，仅 4 条既有上游弃用警告；RecoveryRun 公开发现子任务完成。
  - 回读 Evidence v1，确认全自动 exporter 尚非冻结 P0；将 AUTO Run 公开发现入口列为先决条件。
  - 读取 CP Store 状态更新路径，确认数据库缺少状态 CHECK 且应用层可写任意字符串，形成下一批迁移/转换守卫修复设计。
  - 对照 DB-05/SETTLE-01 检查 RecoveryEngine：确认 monotonic 已注入但未保护 AUTO settle，确定采用进程内候选 tracker 的保守修复，CP 重启后重新完整计时。
  - 完成 CP monotonic lease/settle 的调用面设计：accepted heartbeat 才更新 tick，所有安全决策改用 tick；重启后保守离线；同候选 monotonic 满窗可抵抗 wall clock 前后跳。
  - 实现 RecoveryEngine 进程内 candidate monotonic guard：候选变化/缺节点/异常/非候选状态清零，wall clock 前跳不能提前，后跳不能无限延长，CP 重启后重新完整汇聚。
  - 新增线程安全 `MonotonicLeaseRegistry` 及边界/时钟异常测试，为 Store 在线判定接入做准备。
  - 收到 Agent 子任务验证：其 71 项全部通过；全量仅 Host Preflight/example 安全迁移测试失败，已归入 root 修复范围。
  - 修复 Host Preflight 成功夹具：从故意无效 example 派生临时配置并注入测试强秘密，保持原始示例 fail closed；整文件 5 项通过。
  - 盘点 Store liveness 测试调用面，确定只有显式离线断言和 CP 代理动作的 wall timestamp 篡改场景需迁移到可控 monotonic tick；即时注册/列表/三机集成保持不变。
  - 将 monotonic lease 接入 CP Store 的完整 online-only 决策面，并新增 CP 重启/重复 heartbeat fail-closed 证明。
  - 审计后台任务生命期，确认 scheduler 遇一次意外异常会永久退出，Run task 异常也未被 done callback 观测；列为下一项可观测性/自愈修复。
  - 实现 scheduler 异常监督、指数退避、每轮 durable Run 重新发现和 task 异常日志；RecoveryEngine 16 项通过。
  - 完成 CP 状态完整性 v2 迁移设计：白名单/终态 trigger + 应用转换守卫 + 旧库非法数据原子拒绝，待 Run 列表子任务结束后实施。
  - 与 Run 列表子任务确认文件边界，回读其最新 Store 后开始实施状态 migration/转换守卫。
  - 实现 CP Store v2 状态域/终态 triggers 与应用层枚举转换守卫，进入迁移/绕过测试阶段。
  - 完成 CP Store v2 原子升级、直接 SQL 绕过与终态不回退测试；Store 11 项通过。
  - 实现 CP Store v3 BLOCKED_PRECONDITION 原因持久化与状态/原因一致性 trigger，进入测试同步阶段。
  - 完成 Store v3 持久化/重启/隔离解除/trigger 测试：12 passed；等待公共模型/OpenAPI同步后跑API联合回归。
  - 补充 v2→v3 旧 BLOCKED 数据迁移测试，以明确 unavailable 原因代替虚构根因；Store 13 passed。
  - 审计管理会话退出路径，发现 logout CSRF 未复用常量时间比较，列为小范围安全一致性修复。
  - 完成 logout CSRF 常量时间复用与会话生命周期测试，目标测试 1 passed。
  - 完成 Group BLOCKED 原因可视化与 HTML 转义；合并 Run Detail 全时间线/根因链专项，UI 2 passed。
  - 复核依赖与构建边界：版本精确锁定、Python 3.13、PyInstaller onedir；目标机无运行时下载。
  - 在并行改动汇合点运行 `git diff --check` 与 `compileall`：均通过；仅 Git 提示既有工作树未来 LF→CRLF 转换，无 whitespace error。
  - 联合验证 Run 列表、Store、Engine、CP API、OpenAPI 和三机集成：42 passed、4 个上游弃用警告。
  - Agent 真并发 admission/幂等/服务锁修复完成：专项 76 passed，operations 5 轮稳定。
  - 启动 Run Detail 可解释性子任务；限定模板/样式/专门 UI 测试，确认现页遗漏 Run/Step/attempt 完整时间、可读根因与 dependency chain。
  - 完成 Run Detail 模板/样式实现：展示 Run/Step 四类时间、probe attempt 起止/观察时间与结果；根因和有序依赖链解析为服务名/local id/status，并支持同页锚点跳转；无时间明确显示“未发生”。
  - 新增 `tests/test_run_detail_ui.py`，首轮 1 passed；证明四种 Step 终态、三层时间、probe 证据、根因/依赖链解析与 HTML 转义安全。
  - Run Detail 最终专项复核 1 passed：进一步断言 READY 成功探针、FAILED 失败探针、UNKNOWN/BLOCKED 可读消息及缺失时间位置；公共模型并发中间态由 root 后续联合回归。
  - 启动 CP proxy dispatch 崩溃窗专项；回读规划与 prepare/dispatch/replay 路径，确认需覆盖 prepare 前后、Agent响应丢失、save后重启、异指纹冲突及目标快照漂移五类边界。
  - 启动 Recovery API/模型/配置边界一致性专项：回读 planning-with-files 流程及三份规划材料，冻结审计范围为 group/manual/probe/集合/display_name/45秒租约/cookie 名称/ingress 401/403，不触碰 Store、RecoveryEngine、Operations 或安装器。
  - 初步对照冻结合同：确认 probe 范围/默认、45秒 lease 和 401/403 已成文；发现 interval 的 OpenAPI integer 与模型 float 漂移，group/manual/集合边界需先补文档。
  - 接收并纳入 `RecoveryGroup.blocked_reasons` 公开合同补充；仅修改合同/OpenAPI/模型/测试，Store v3 持久化继续由主任务负责。
  - 完成第一轮模型/OpenAPI 差异盘点：定位 group/manual 文本边界、readiness interval 类型、display_name nullability、集合上限、AgentSummary 45 秒常量及 ingress 缺失 403。
  - 递归提取两份 OpenAPI 与 Pydantic JSON Schema 的全部数组边界，确认大多数集合无界；形成以 1024 服务/16384 依赖/100 reason 或列表为核心的最小 MVP 容量方案，待核对消费与存储路径后冻结。
  - 回读计划后冻结 group/manual 文本策略：name 128、description 非 null 1024、manual reason 非空时 512；Patch 省略可接受但显式 null 拒绝，避免运行时与 NOT NULL 存储漂移。
  - 核对 Agent SCM 与 CP 存储后冻结 display_name 为非 null 1..256；冻结所有公开集合容量表（服务类1024、依赖16384、reason/警告/Run页100）。
  - 核对 RecoveryEngine probe 消费与现有测试后冻结数值类型：timeout 为可小数 number，interval/deadline 为整秒 integer；确认 CP 既有 Forbidden 响应可用于 ingress 403。
  - 文档先行完成：Recovery 合同写死文本、数值类型、容量、display_name、manual reason、CP ingress 401/403 与 blocked_reasons；追踪矩阵新增/扩展 HB-02/HB-04/PRB-06/GRP-04/GRP-05/RUN-09/SEC-02 可执行场景。
  - 公共 Pydantic 模型同步第一版：加入统一集合容量、group/manual/display_name 边界、readiness integer interval/deadline、45秒 Literal、严格 PreconditionIssue 与 blocked_reasons 状态不变量；未知字段仍由 StrictModel 拒绝。
  - CP 公开 Schema/UI 收口完成：RecoveryGroup description 改为非 nullable 0..1024，新增严格 PreconditionIssue 与 required blocked_reasons，补齐 Group/Run/Step 全部冻结数组容量；Groups 新建/编辑表单同步 128/1024 并加入 PATCH 交互，OpenAPI/UI 专项 9 passed。
- Files created/modified:
  - `task_plan.md`（创建并填充）
  - `findings.md`（创建并填充）
  - `progress.md`（创建并填充）

### Phase 2: 目标清单与部署授权

- **Status:** pending（Phase 1R重新冻结后恢复）
- **Started:** 2026-07-17
- 已开始审计无需真实主机即可补齐的部署输入合同与只读采集链路；现有文件清单尚未发现inventory专用工件。
- 已确认现有配置模型与PreInstall之间缺少跨主机输入层；冻结最小补齐范围为严格inventory校验、配置草案渲染和本机只读事实采集，不扩展远程资产管理。
- 已选定复用现有CP Frozen CLI承载离线inventory准备，复用公共readiness与Agent配置模型，保持三个onedir交付结构不变。
- 独立完成度审计复现两项公开API P1：CP proxy action显式`null`仍会202并dispatch；Agent operation查询接受compact/braced/URN UUID。inventory工作暂停，先修副作用与输入合同。
- 同批审计新增恢复组settle/max_parallel写字段会强转字符串/bool；纳入同一P1严格输入修复批次。
- P1批次扩展为：全公开HTTP canonical UUIDv4输入、Agent endpoint/listen/CP CIDR跨字段一致性，以及Manual Run reason OpenAPI/真实API边界；均须先证明拒绝发生在副作用/持久化前。
- 新增最高优先级：RecoveryEngine必须验证Agent Operation与当前Step/member/key/fingerprint完整绑定；错绑响应不得保存、探针或放行下游，Run保守UNKNOWN。
- Phase 2工具只读审计完成；新增确认CP listen_host、密码哈希完整性以及网络边界门禁缺口。先完成不依赖现场的协议/配置修复，再实现inventory/facts；远程网络门禁保留到有目标清单后执行。
- 完成度审计复现101个preflight issue会违反blocked_reasons上限并把组卡在SETTLING；纳入高优先修复，要求确定性99项+1条截断汇总且调度继续可解释。
- 公开集合maxItems=1024缺写侧容量门禁，1025组会使列表500；纳入Store原子封顶修复，暂不扩MVP分页。
- Groups最小Web readiness编辑器不能显示/删除现有TCP/HTTP探针；纳入交付P1，要求按服务预填、保存刷新和显式回退SCM。
- 当前阻塞：尚未取得独立CP节点、至少三台业务节点、准确Windows Service名称、readiness、网络/EDR边界及现场执行方式。
- 安全边界：不在聊天中收集Token、管理员密码或session secret；这些值只在目标机本地生成并保存。
- 下一动作：用户提供非秘密主机/服务清单并明确授权后，生成逐机配置草案和只读PreInstall执行清单；在再次回读计划前不安装、不启动业务服务。

## Test Results

| Test | Expected | Actual | Status |
|---|---|---|---|
| 既有计划检测 | 明确是否需恢复旧计划 | 根目录和 `.planning` 均无计划 | PASS |
| session catch-up | 报告未同步上下文或静默完成 | 静默完成，无待恢复内容 | PASS |
| 技能初始化 | 创建三份规划文件 | 三份文件均创建成功 | PASS |
| 规划内容落盘 | 不是空模板，包含真实阶段与阻塞 | 已记录目标、6 个后续阶段、决策、风险和输入 | PASS |
| 活动目标对齐 | 计划必须覆盖“根据方案文档进行编码” | 已新增合同到实现审计与缺口编码阶段 | PASS |
| 当前工作树全量回归 | 既有基线仍应通过 | 129 passed，4 warnings | PASS |
| 覆盖率盲区扫描 | 找出核心未触达分支 | orchestrator 总体 80%，已定位七个重点模块 | PASS（仅审计线索） |
| SQLite 数据合同 | 正数 busy timeout、顺序迁移、原子回滚、FK | `tests/test_common.py` 8 passed | PASS |
| API 严格正文/框架错误/slug | 合同与两份 OpenAPI 一致 | 23 passed，4 个上游警告 | PASS |
| 配置秘密/CIDR/CLI 安全边界 | 示例不得直接投产、错误不得泄密、Agent 只信任 CP host prefix | `tests/test_config_check_cli.py` + `tests/test_agent_config.py`：16 passed | PASS |
| RecoveryRun 公开发现 | AUTO无需已知ID、稳定分页、过滤/鉴权/错误体、Dashboard可点详情 | 新增测试与OpenAPI合同：8 passed | PASS |
| AUTO settle monotonic | 墙钟前后跳不改变连续窗口；CP 重启后保守重计时 | `tests/test_recovery_engine.py`：13 passed | PASS |
| CP monotonic lease registry | 启动 fail closed、45 秒边界、clock anomaly、renew | `tests/test_control_plane_leases.py`：3 passed | PASS |
| Host Preflight 示例安全迁移 | 原始 sentinel 不放宽，临时强秘密配置通过且输出不泄密 | `tests/test_host_preflight_contract.py`：5 passed | PASS |
| CP Store monotonic liveness | 列表、preflight、settle、Run 与 proxy action 不受 wall jump；重启保守离线 | Store/Engine/三机 15 passed；proxy action 1 passed | PASS |
| Scheduler 自愈/可观测 | 单轮失败不杀死 scheduler；Run task 异常可见且按持久状态重启 | `tests/test_recovery_engine.py`：16 passed | PASS |
| CP 状态持久化不变量 | 域值、终态、RUNNING回退、脏 v1 原子拒绝 | `tests/test_control_plane_store.py`：11 passed | PASS |
| BLOCKED 原因持久化 | preflight 原因跨重启保留、外部修复不解隔离、disarm清除 | `tests/test_control_plane_store.py`：12 passed | PASS |
| 管理退出 CSRF | 未登录/错误 token 不清会话，正确 token 常量时间验证并退出 | 目标测试 1 passed | PASS |
| CP 可解释性 UI | Group 隔离原因 + Run/Step/Probe 时间线 + 根因/依赖链 + 转义 | UI 2 passed | PASS |
| 静态完整性 | 无 diff whitespace error，Python 全包可编译 | `git diff --check` + `compileall` | PASS |
| CP Run 列表联合回归 | AUTO 可发现、cursor/API/UI 与 Store/Engine/OpenAPI 一致 | 42 passed，4 warnings | PASS |
| Agent 真并发合同 | 同 key唯一、异指纹冲突、同服务串行、跨服务并行、重启 replay | Agent 76 passed；并发集 5轮通过 | PASS |
| 2026-07-17 stable full regression | All current CT/UT/IT suites green after model and Store v3 convergence | 197 passed, 4 upstream warnings | PASS |
| 2026-07-17 fresh Frozen build | 从稳定工作树重建三个 PyInstaller onedir 和完整发布树 | `dist-recovery-20260717` 构建 exit 0，耗时 76.1 秒 | PASS（待完整性/烟雾复核） |
| CP probe 崩溃恢复 deadline | attempt 已持久化但 Step 终态前崩溃时，迟到成功不得恢复为 READY | Engine 17 passed；桥接/三机/UI 4 passed；compileall/diff check 通过 | PASS |
| Frozen smoke 配置合同 | 脚本生成配置必须被当前 `ControlPlaneConfig` 接受 | cookie 固定为 `recovery_admin_session`；packaging/config 36 passed | PASS（需重建后实跑） |
| CP 公开 Schema/UI 冻结边界 | 非 nullable description、严格 blocked reason、全部数组容量、Groups 新建/编辑限长 | `tests/test_openapi_contracts.py` + `tests/test_group_blocked_ui.py`：9 passed，4 个上游弃用警告 | PASS |
| CP Store v4 PreconditionIssue | 直接持久化与 v3→v4 升级均严格、原子、可恢复 | `tests/test_control_plane_store.py`：23 passed | PASS |
| 审计前 Frozen 二进制 smoke | 用修复后的源码 smoke 脚本驱动现有两个 onedir，且无残留副作用 | health/LAB_HTTP/Agent/SCM/login/dashboard/group 均通过；listener=0、temp=0 | PASS（包内脚本仍需重建后复跑） |
| CP dispatch 三个崩溃窗 | key前、Agent已接受但响应丢失、operation_id已持久后的恢复均不重复副作用 | `tests/test_recovery_engine.py`：19 passed；新增两项精确恢复测试 | PASS |
| Store v4 Unicode/JSON 对抗 | NUL、UUID尾随NUL、孤立surrogate、脏v3必须拒绝；合法emoji规范化 | Store + OpenAPI 36 passed，4条上游弃用warning | PASS |
| Store v4 连接级严格校验复审 | 字面反斜线不误拒，BLOB/CAST非UTF8拒绝，UUID真正规范化，每连接UDF fail closed | common + Store + OpenAPI 51 passed，4条上游弃用warning | PASS |
| Store v4 最终独立对抗复审 | 原四反例、旧无UDF连接、正常连接、emoji/UUID全部闭环 | 42 passed；无剩余P0/P1 | PASS |
| 发布P1端口/WinSW TOCTOU | 全listener归属与final wrapper锁定复验 | PS5.1 parse、listener harness、安装8场景harness PASS；pytest 31 passed | PASS |
| Agent P1长期可靠性/合同 | mapping CLI、heartbeat监督、UUID输入输出、固定fingerprint、单实例部署约束 | Agent/安全/CLI/OpenAPI 114 passed，4条上游warning | PASS |
| 2026-07-17 final stable full regression | 所有Agent停止写入后的完整CT/UT/IT/PowerShell契约回归 | 232 passed，4条上游弃用warning，38.96秒 | PASS |
| Agent动作矩阵最终证据 | 24状态/动作组合、SCM失败、三动作journal屏障与恢复 | operations 58、Agent 115、子任务全量266 passed；query失败漂移已修 | PASS（需root稳定复跑/重建） |
| Agent quarantine排队竞态 | B在A设隔离前排队，锁内二检必须拒绝且零第二副作用 | operations59、Agent116、子任务全量267 passed | PASS（需root稳定复跑/重建） |
| CP Readiness strict数值合同 | 三kind拒绝string/bool/伪整数，PUT 422且零持久化 | 专项17、子任务全量269 passed；compileall/diff check通过 | PASS（需root稳定复跑/重建） |
| 最终P1后root稳定回归 | 所有写入停止后的完整测试与静态检查 | 269 passed/4上游warning/32.28秒；compileall/diff/pip check通过 | PASS |
| 最终源码静态检查 | Python语法、tracked whitespace、依赖一致性 | compileall/diff check/pip check均通过；仅LF→CRLF提示 | PASS |
| 最新 Frozen 候选重建 | 从269项全绿且最终P1修复后的稳定工作树生成三个onedir与发布树 | `dist-recovery-frozen-20260717` exit0，67.3秒；仅既知tzdata hidden import warning | PASS（待全部包内门禁） |
| 最新 Frozen 包内完整性 | 精确文件集、清单内SHA-256、reparse与副作用 | 331/331，missing/extra/mismatch=0，side_effects=NONE | PASS（清单自身待带外哈希） |
| 最新 Frozen 包内烟雾 | 包内Agent/CP实际启动、SCM只读观察、心跳、登录和Dashboard | health ok、Agent=1、EventLog ACTIVE、Dashboard 200、Group=0、无action/Run | PASS |
| 最新 Frozen example安全失败 | 未编辑Agent/CP示例走配置校验，且不得因usage错误假阳性 | 两端exit2、config_valid=false、稳定脱敏JSON、usage=false | PASS |
| 最新 Frozen evidence负向 | 空白证据模板不得伪造验收通过 | exit1、verdict=FAIL、16 issues | PASS |
| 最新安装事务故障注入 | 八类篡改/失败/回滚/所有权/残留场景保持事务与fail-closed | 8 scenarios PASS，side_effects=TEST_TEMP_ONLY | PASS |
| 最新listener所有权故障注入 | 唯一合法通过，合法+冒充混合占用必须失败 | 2 scenarios PASS，side_effects=NONE | PASS |
| 最新本机清理与二进制新鲜度 | 无进程/监听/smoke临时目录/Recovery服务；EXE晚于关键源码 | 0/0/0/0，Agent/CP fresh=true | PASS |
| 最新清单带外信任 | 独立计算清单自身SHA-256并解释文件计数口径 | `760851a85e145c4bb0b572d2193c16b7c9e9629900258249f5b618b1dda00366`；331受管+清单自身=332 | PASS |
| Phase 1→2规划一致性 | Phase 1全部完成，Phase 2成为当前阶段且规划材料格式干净 | complete/current/in_progress均正确，Phase 1未勾选0，尾随空白0 | PASS |
| 复审第一批公开输入/配置修复 | null零副作用、canonical UUID、strict数值、reason及配置安全 | 67 passed，4条上游弃用warning | PASS（源码已前移，旧Frozen过期） |
| 最终 Frozen 干净重建 | 从232项全绿稳定工作树生成三个onedir与发布树 | `dist-recovery-final-20260717` exit0，150.8秒 | PASS（待包内验证） |
| 最终包内完整性 | 精确文件集、SHA-256、reparse与无副作用 | 331/331，missing/extra/mismatch=0，side_effects=NONE | PASS |
| 最终包内 Frozen smoke | 新Agent/CP二进制、SCM只读、心跳、登录/Dashboard | health ok、Agent=1、EventLog ACTIVE、Dashboard 200、Group=0、无action/Run | PASS |
| 最终包 example fail-closed | 未编辑Agent/CP example必须走配置校验而非usage错误 | 两端exit2、config_valid=false、稳定脱敏JSON | PASS |
| 最终 Frozen evidence负向 | 空模板不得伪造验收通过 | exit1、verdict=FAIL、16 issues | PASS |
| 最终安装事务故障注入 | 篡改、安装/启动/回滚/所有权/残留8场景 | outcome=PASS，side_effects=TEST_TEMP_ONLY | PASS |
| 最终listener所有权故障注入 | 唯一合法通过，合法+冒充混合必须失败 | outcome=PASS，side_effects=NONE | PASS |
| 最终本机清理复核 | 无Frozen进程、监听、smoke临时目录、Recovery服务 | 0/0/0/0 | PASS |

| Phase 1R 容量与 readiness 联合定向回归 | Agent/Service/Group 容量原子拒绝、HTTP 422、Web 回显/删除/fallback | 5 passed，16.40秒 | PASS（仍待旧数据库升级决策与全量） |
| Phase 1R 容量独立对抗审计 | 并发最后名额、旧 v4 超限启动与 GET 行为 | BEGIN IMMEDIATE 并发正确；旧 v4 1025 可启动后 GET 500 | BLOCKER：需 v5 fail-closed migration |
| Phase 1R 历史服务容量审计 | unreported 服务、Group 成员与心跳查询增长 | 发现 stale 新成员可保存、历史服务无 retention/index/硬上限 | OPEN：先冻结最小语义再编码 |
| Phase 1R 历史服务 MVP 决策 | stale Group 配置、tombstone 审计引用、性能边界 | 新增 stale 拒绝；既有可保留/移除；v5 partial index；不做 GC/硬上限 | DECIDED，待实现/测试/文档 |
| Phase 1R 容量 v5 与 stale 语义 | 三类迁移边界、索引、Group stale 规则、HTTP错误 | 11 passed，13.21秒 | PASS（待文档/独立终审/全量） |
| Phase 1R 容量/Web 文档冻结 | 双 Store 并发、OpenAPI语义、readiness UI、运维恢复 | 15 passed，4个上游弃用warning，2.54秒；diff-check通过 | PASS，两个缺口关闭 |
| Phase 1R v5 首次全量回归 | 全部 CT/UT/IT | 313 passed、1个旧schema版本断言失败、4 warnings，51.09秒 | TEST BASELINE FIX REQUIRED |
| Phase 1R schema 版本断言修复 | 正常v5与超限保持v4 | 4 passed，0.62秒 | PASS，允许重新全量 |
| Phase 1R 稳定全量回归 | 全部 CT/UT/IT | 314 passed，4个既有上游弃用warning，54.16秒 | PASS |
| Phase 1R 静态/依赖门禁 | Python编译、tracked whitespace、依赖一致性 | compileall/diff-check/pip check通过；仅LF→CRLF提示 | PASS |
| Phase 1R 独立最终冻结审计 | 容量/Web/blocked/Operation真实链路 | P0=0，P1=1；真实AgentClient畸形Operation未落协议错误码 | BLOCKER，暂停重建 |
| Phase 1R 畸形 Operation 协议修复 | 真实AgentClient POST/GET、脱敏、Engine隔离 | 子任务31 passed，compileall/diff-check通过 | IMPLEMENTED，待root复跑/终审 |
| Phase 1R 畸形 Operation root联合回归 | AgentClient/RecoveryEngine/CP App | 41 passed，3.50秒 | PASS，P1关闭 |
| Phase 1R 最终稳定全量回归 | 全部 CT/UT/IT（含真实畸形Operation链路） | 321 passed，4个既有上游弃用warning，48.02秒 | PASS |
| Phase 1R 最终源码静态门禁 | 编译、whitespace、依赖 | compileall/diff-check/pip check通过 | PASS |
| Phase 1R 独立复审闭环 | 畸形Operation修复与RUN-13 | P0=0、P1=0；12 passed/19 deselected | PASS，允许重建Frozen |
| Phase 1R 全新 Frozen 候选构建 | 三个PyInstaller onedir与发布树 | `dist-recovery-phase1r-20260717` exit0，69.9秒；仅既知tzdata warning | PASS（待包内验证） |
| Phase 1R 新包完整性 | manifest文件集与SHA-256 | 331/331，missing/extra/mismatch=0，side_effects=NONE | PASS（清单自身待带外哈希） |
| Phase 1R 新包 Frozen smoke | 包内Agent/CP、心跳、SCM只读、登录/Dashboard | health ok、Agent=1、EventLog ACTIVE、Dashboard200、Group0、无action/Run | PASS |
| Phase 1R 新包 example负向 | Agent/CP未编辑示例拒绝 | 两端exit2、config_valid=false、稳定脱敏JSON、usage=false | PASS |
| Phase 1R 新包 evidence负向 | 空白验收模板拒绝 | exit1、verdict=FAIL、16 issues | PASS |

| Phase 1R 新候选安装事务故障注入 | 8类篡改、安装/启动/回滚/所有权/残留 | outcome=PASS，side_effects=TEST_TEMP_ONLY | PASS |
| Phase 1R 新候选listener所有权故障注入 | 唯一合法通过，合法+冒充混合必须失败 | outcome=PASS，side_effects=NONE | PASS |
| Phase 1R 新候选清单带外信任 | 独立计算清单自身SHA-256与文件计数口径 | `ceccf51d5c76ff0b2afb824f64c5f9a6ef50c477896cab282752ba8331e90bb5`；331受管+清单=332，122127072 bytes | PASS |
| Phase 1R 新候选二进制新鲜度 | 两端EXE必须晚于当前运行时源码/模板/静态资源 | 最新源码03:22:35Z；Agent03:25:33Z、CP03:25:54Z，fresh=true/true | PASS |
| Phase 1R 新候选本机收尾 | 本次构建后/近30分钟烟雾目录、进程、四端口监听、Recovery服务 | 0/0/0/0/0；2个2026-07-16历史目录不属于本次 | PASS |
| Phase 1R 最终whitespace复核 | `git diff --check` | exit 0，仅既知LF→CRLF提示 | PASS |
| Phase 1R 独立包绑定收尾审计 | harness发布树绑定、Evidence EXE与复制资源 | 缺口已由后续发布树harness、10/10哈希与三EXE新鲜度证据关闭 | CLOSED |
| Phase 1R 发布树绑定安装事务harness | 发布树复制安装器的8个故障场景 | outcome=PASS，side_effects=TEST_TEMP_ONLY | PASS |
| Phase 1R 发布树绑定listener harness | 发布树复制preflight的2个所有权场景 | outcome=PASS，side_effects=NONE | PASS |
| Phase 1R 发布复制资源绑定 | 4脚本、3示例、WinSW lock、运维文档、evidence合同 | 10/10源→包SHA-256一致 | PASS |
| Phase 1R Evidence Validator新鲜度 | entry+acceptance+common共11个源码输入 | 最新输入02:52:14Z，EXE03:26:08Z，fresh=true | PASS |
| Phase 1R 严格本机清理终态 | smoke临时目录、相关进程、四端口监听、Recovery服务 | 0/0/0/0；2个已审计历史smoke夹具安全删除 | PASS |
| Phase 1R 候选绑定后不可变性复验 | manifest文件集、受管SHA与带外清单SHA | 331/331、差异0；清单SHA仍为`ceccf...e90bb5` | PASS |
| Phase 1R 最终独立冻结复核 | 全部技术冻结门槛 | 无剩余阻塞，允许进入Phase 2 | PASS |
| Phase 2入口复核 | 阶段状态与目标清单入口 | Phase 1 complete、Phase 2 in_progress；仓库无现成inventory模板，等待用户提供非秘密目标事实 | BLOCKED ON USER INPUT |

## Error Log

| Timestamp | Error | Attempt | Resolution |
|---|---|---:|---|
| 2026-07-16 | 组合 `rg` 技能搜索返回退出码 1，但标准输出包含有效技能路径 | 1 | 识别为部分搜索根无匹配；直接读取已发现路径，没有重复失败命令 |
| 2026-07-16 | 组合实现补丁因 Agent 端点签名上下文不匹配而未应用 | 1 | 确认无部分修改；改为拆分补丁并先读取精确签名 |
| 2026-07-16 | 动作正文回归测试引用不存在的 `AgentStore.db` | 1 | 不重复整组测试；先确认 Store 属性，再修正测试查询 |
| 2026-07-16 | 与交付子 Agent 并发修改 packaging test 导致版本竞态失败 | 1 | 不判断为产品失败；等待子任务结束后从稳定工作树重新验证 |
| 2026-07-16 | 假定存在独立安装器测试文件，但仓库没有 `tests/test_install_recovery_service.py` | 1 | 安装器正文读取成功；后续在既有 packaging contract 测试中精确定位，不重复错误路径 |
| 2026-07-16 | RecoveryRun 组合读取使用 `rg orchestrator/common/*.py`，PowerShell/Windows 路径通配导致退出码1并丢失并行输出 | 1 | 改读明确文件路径；不再重复该命令形态 |
| 2026-07-16 | 收尾并行读取中尾随空白 `rg` 无匹配返回1，使组合工具丢失其他成功输出 | 1 | 改用不会把“零匹配”视为错误的显式检查；不重复该组合 |
| 2026-07-16 | 收尾第二次把可选工具配置文件查询混入并行读取，无文件时再次使组合输出丢失 | 2 | 执行3-strike替代策略：后续并行组合只含确定成功的正文读取，可选检查独立且显式判断存在性 |
| 2026-07-16 | 假定 CP 有独立 `control_plane/models.py`，组合读取时该路径不存在 | 1 | 已确认公开模型位于 `common/models.py`；保留已成功取得的 Store 审计结果，后续不重复错误路径 |
| 2026-07-16 | Agent 子任务全量测试中 Host Preflight 期待原始 CP example 通过，和新 sentinel 安全策略冲突 | 1 | 不放宽配置校验；待修改 Host Preflight 测试以使用临时注入强秘密的配置，再重跑全量 |
| 2026-07-16 | Host Preflight 测试首次修复误从 `control_plane.auth` 导入不存在的 `hash_password` | 1 | 用源码搜索定位到 `common.security`；避免重复错误导入 |
| 2026-07-16 | 在 PowerShell 中把未展开的 `test_control_plane*.py` 直接作为 rg 路径，触发 Windows 路径错误 | 1 | 改用 rg 的 `-g` 文件过滤语法，避免重复同一失败命令 |
| 2026-07-16 | Run Detail 规划更新补丁因并发 Agent 插入内容导致上下文失效 | 1 | 确认无部分应用；回读精确位置后用最小上下文追加并保留并发内容 |
| 2026-07-16 | logout CSRF 组合补丁匹配不到 App 的精确换行签名，整体未应用 | 1 | 确认无部分修改；改用拆分、最小上下文补丁 |
| 2026-07-16 | Run Detail 联合测试撞上 Store v3/RecoveryGroup 模型并行迁移中间态，1项响应校验失败 | 1 | UI专项通过且未越界修改模型；等待模型子任务完成后联合回归 |
| 2026-07-16 | 假定组详情有独立 `group_detail.html`，但当前 UI 合并在 `groups.html` | 1 | 保留已读取的 groups 页面事实；后续使用实际模板名，不重复错误路径 |
| 2026-07-16 | probe 小数值复合 `rg` 正则没有匹配并返回 1 | 1 | 停止重复该表达式；后续按具体字段做简单搜索并显式接受零匹配 |

| 2026-07-16 | Run Detail 联合回归期间 Store v3 已输出 `blocked_reasons`，公共 RecoveryGroup 模型仍拒绝额外字段 | 1 | 识别为并发版本竞态并通知 root/合同模型子任务；不在 UI 范围修复，待稳定后重跑 |
| 2026-07-17 | Full suite: 2 heartbeat tests timed out and endpoint mismatch test raised Pydantic ValidationError because their report fixtures used an empty service list | 1 | Preserve the minItems=1 contract; update only test fixtures to include one valid ObservedService, then rerun targeted/full suites |
| 2026-07-17 | Assumed a separate `agent/service_observer.py` while inspecting the heartbeat callback | 1 | The callback contract is fully defined in `heartbeat.py`; use the public `ObservedService` model in fixtures |
| 2026-07-17 | 冻结包重建通过 `shell_command` 启动时误设 1 秒命令超时，返回 exit 124 | 1 | 先查残留进程与半成品；确认状态后改用长命令超时、外层短周期等待，避免重复同一失败动作 |
| 2026-07-17 | 发布终审只读命令把 `foreach` 结果直接接管道，两次报 `ParserError: empty pipe element` | 2 | 均无写入；提前切换为逐行无管道输出，彻底停用该命令形态 |
| 2026-07-17 | 同时更新 Error Log 与 Test Results 的补丁因测试行精确上下文漂移而整体未应用 | 1 | 确认无部分修改；拆成独立补丁，并先定位测试表尾部后追加 |
| 2026-07-17 | 发布审计组合查询了不存在的可选 `C:\Approved` WinSW 资产路径，命令 exit 1 | 1 | 无副作用；改为先判断路径存在性，再单独查询有效位置 |
| 2026-07-17 | 再次尝试同时更新 progress 的 Error Log/Test Results 时测试表上下文未命中，补丁整体未应用 | 1 | 停止跨表多 hunk；错误表与测试表逐个定位、逐个补丁 |
| 2026-07-17 | 发布 P1 首轮故障注入的 ACL 恢复测试替身忽略 `acl_apply_started`，与真实实现语义不一致 | 1 | 仅修测试替身，使 ACL 前失败不虚构 restore；产品门禁与断言保持不变后重跑 |
| 2026-07-17 | Unicode 对抗脚本直接打印 emoji 到 GBK 控制台，产生 `UnicodeEncodeError` 并中止 | 1 | 改用 `ascii()`/转义和布尔验证；不重复直接输出非 ASCII 样本 |
| 2026-07-17 | 发布 P1 pytest 子进程的 PS5.1 未自动加载 `Get-AuthenticodeSignature`，相关集 30 passed/1 failed | 1 | 直接 harness 已通过；仅在故障注入 fake 固定 NotSigned OS 边界，产品验签逻辑不放宽 |
| 2026-07-17 | Unicode 合同补丁的 JS raw template 被 Markdown 反引号截断，解析阶段 SyntaxError | 1 | 未调用 apply_patch、无部分修改；逐文件使用普通转义字符串 |
| 2026-07-17 | Agent P1 的 Store+CLI 组合补丁因 `store.py` import 上下文不符而未应用 | 1 | 无部分修改；先读精确头部，再拆成 typed error 与 CLI 最小补丁 |
| 2026-07-17 | 最终示例检查误用位置参数，argparse返回2恰与期望相同，产生退出码假阳性 | 1 | 作废结果；按usage改为 `--config PATH --check-config`，并验证输出是配置拒绝而非usage错误 |
| 2026-07-17 | 正确参数的示例检查猜测错误文案为 `configuration is invalid`，两端实际文案不同导致断言失败 | 1 | 两端已确认exit2且非usage；改为先读权威测试，再校验稳定JSON字段，不继续猜字符串 |
| 2026-07-17 | 并行PowerShell harness调用遗漏安装事务必填 `RepositoryRoot`，exit1并遮蔽另一脚本结果 | 1 | 先读param块，按正确参数分别执行；不重复无参组合调用 |
| 2026-07-17 | 动作矩阵首轮57 passed/1 failed，确定复现PREPARED后SCM query异常错误分类 | 1 | 新增严格Worker观察边界；修后operations 58/Agent115/全量266 passed |
| 2026-07-17 | CP Readiness strict子任务首次spawn触达线程上限 | 1 | 不重复spawn；复用活跃冻结审计Agent实现同一限定范围 |
| 2026-07-17 | 试图一次同步三份规划文件时使用了无效的多文件 hunk 分隔，`apply_patch` 校验失败 | 1 | 已确认补丁完全未应用；改为逐文件精确补丁 |
| 2026-07-17 | Phase 2 CLI/打包测试检索误含不存在的 `pyproject.toml`，`rg` 返回路径错误 | 1 | 已读取的现存测试输出仍可用；后续只检索文件清单中确认存在的路径，不重复原组合 |
| 2026-07-17 | `wait_agent` 使用1000ms，低于允许的10000ms最小值 | 1 | 未执行等待、无副作用；后续使用合法超时并继续本地审计 |
| 2026-07-17 | 新P1规划同步补丁因一处决策文本上下文不精确而整体未应用 | 1 | 已确认无部分修改；重新定位精确行后拆分、同步记录，不重复原补丁 |
| 2026-07-17 | 配置子任务尝试运行未安装的ruff，返回`No module named ruff` | 1 | 不安装或重复调用非项目依赖；以compileall、diff-check和39项定向测试作为当前静态/动态证据 |
| 2026-07-17 | blocked-reason首次补丁对`_json_dump`上下文假设错误，整体未应用 | 1 | 无部分修改；已读取真实多行实现，改用拆分精确补丁 |
| 2026-07-17 | candidate结果检索的复合`rg`正则括号未闭合 | 1 | 不重复该表达式；改用字面搜索和明确行读取 |
| 2026-07-17 | 容量修复首版误设计为409/CAPACITY_EXCEEDED，与合同“超限422/VALIDATION_ERROR”冲突，补丁整体未应用 | 1 | 回读权威合同后取消新错误码，改为原子422门禁 |
| 2026-07-17 | 容量升级语义审计引用了不存在的 `docs/operations/recovery-mvp-deployment.md` | 1 | 未造成文件修改；先从文档清单定位实际运维文档，再继续审计，不重复错误路径 |
| 2026-07-17 | 容量 Store/API 测试组合补丁因 CP 测试函数名上下文不匹配而整体拒绝 | 1 | 无部分修改；拆为两个精确补丁并先定位真实插入点，不重复原组合 |
| 2026-07-17 | Web readiness 子任务误查不存在的 `static/style.css` | 1 | 已确认真实样式文件为 `static/app.css`；不重复错误路径，功能实现不受影响 |
| 2026-07-17 | Web readiness 子任务用于获取行号的 `rg` 正则转义错误 | 1 | 无产品副作用；改用 `Select-String` 成功定位，不重复错误表达式 |
| 2026-07-17 | 固定 CP service ID 检索把可零匹配 `rg` 与安装器正文组合，导致整体 exit1 | 1 | 正文已确认 `winsw-recovery-control-plane`；后续不重复组合查询，直接据权威安装器写运维步骤 |
| 2026-07-17 | v5 首次全量测试唯一失败：旧 Store 测试仍断言 CP schema version=4 | 1 | 产品新增顺序 migration 后当前版本应为5；先修单一测试基线并定向验证，不原样重跑全量 |
| 2026-07-17 | 首次改单一版本断言的补丁上下文过宽，误改了“超限迁移版本保持4”测试 | 1 | 通过全文件位置审计在测试运行前发现；精确按函数上下文修正两处，避免掩盖原子回滚合同 |
| 2026-07-17 | 搜索畸形Operation覆盖时引用不存在的 `tests/test_control_plane_agent_client.py` | 1 | 无修改；使用已确认存在的 AgentClient/Store 测试文件继续，不重复错误路径 |

| 2026-07-17 | 续作规划同步补丁误把 `progress.md` 的 Next Update Trigger 当成 `findings.md` 章节，`apply_patch` 校验失败 | 1 | 已确认无部分修改；分别读取文件尾部真实锚点，改为精确、分文件上下文更新，不重复错误假设 |
| 2026-07-17 | Phase 1R只读审计子任务的复合`rg`正则括号/引号未闭合，exit 1 | 1 | 无副作用；已切换`Select-String -SimpleMatch`并成功定位，不重复该表达式 |
| 2026-07-17 | 收尾脚本把`git diff --check`的PowerShell警告记录直接深度JSON化，产生“JSON truncated”提示 | 1 | 关键计数与exit code已完整输出且门禁明确；后续只记录exit和简短warning摘要，不再序列化完整ErrorRecord |
| 2026-07-17 | Host Facts首轮9项测试有1项失败：PS5.1测试替身把单元素`listen_addresses`序列化为字符串 | 1 | 生产路径已有数组边界；子任务仅修测试替身类型并增加Invoke层字段白名单后重跑，不改变合同 |
| 2026-07-17 | Host Facts复跑9项全绿，但pytest写`.pytest_cache`遇到WinError 5 warning | 1 | 不影响断言；后续使用`-p no:cacheprovider`，不改变仓库ACL或测试环境 |
| 2026-07-17 | 共享探针扩大Agent App测试时6项setup因默认TEMP pytest目录ACL WinError 5 | 1 | 43项探针仍通过；后续改用工作区唯一`--basetemp`且禁用cacheprovider，不重复默认temp路径 |
| 2026-07-17 | Phase 2 packaging首跑因默认TEMP ACL出现15个setup错误 | 1 | 改用工作区`--basetemp`后新增2项与相关23项通过，不改系统ACL |
| 2026-07-17 | Packaging完整集CP help在并行`orchestrator.deployment`尚未落盘时导入失败 | 1 | 识别为并行中间态；先验证其余23项，待Inventory完成后完整重跑 |
| 2026-07-17 | Architecture/Roadmap同步补丁因架构表行相邻关系假设错误而未应用 | 1 | 无部分修改；已读取精确段落，改用独立单行锚点，不重复原上下文 |
| 2026-07-17 | Root首次工作区basetemp调用未创建`.test-tmp`父目录，Agent App 6项setup FileNotFound | 1 | 43项探针通过；后续先显式创建/验证父目录再传唯一子目录 |

## 5-Question Reboot Check

| Question | Answer |
|---|---|
| Where am I? | Phase 2：Frozen候选已闭环，开始收集非秘密目标清单与部署授权 |
| Where am I going? | 目标清单 → PreInstall → 安装/PostInstall → 恢复组 → 10 轮断电 → 验收交付 |
| What's the goal? | 完成方案编码，并用自动化与真实三机证据证明 Recovery MVP 满足冻结合同 |
| What have I learned? | Frozen必须同时绑定源码、复制资源、三个EXE和发布树脚本，不能只凭源码测试或manifest自证 |
| What have I done? | Phase 1R全部门槛通过并独立复核；当前Frozen目录为`dist-recovery-phase1r-20260717` |

## Next Update Trigger

- 每完成一项复审缺口修复或动态发现新漂移后，立即更新 `findings.md`。
- Phase 2形成任何目标机配置或执行预检前，先回读`task_plan.md`和`findings.md`并核对授权与非秘密清单。
- 任一预检错误出现时，同时更新本文件错误日志和 `task_plan.md` 错误表。

## 2026-07-17 Phase 2 工具续作

- 重新完整读取 `planning-with-files` skill，运行session catchup，并回读`task_plan.md`、`findings.md`、`progress.md`与`git diff --stat`。
- 当前实现子阶段：非秘密Deployment Inventory合同/校验渲染CLI、本机只读facts采集、测试与新Frozen候选；不等待真实主机信息，也不执行远程扫描、SCM或Firewall写入。
- `dist-recovery-phase1r-20260717`保持只读回退基线；Phase 2新增内容使用新发布目录。
- Host Facts合同测试：9 passed；唯一warning为pytest cache权限，后续禁用cacheprovider。
- 新建`docs/contracts/recovery-deployment-inventory-v1.md`与`examples/deployment-inventory.example.json`；文档先行冻结输入、交叉约束、输出、CLI、安全边界和验收矩阵，下一步按合同实现模型与测试。
- JSON example解析PASS；`python -m pytest -q -p no:cacheprovider tests/test_recovery_host_facts_contract.py`为9 passed/0 warning。
- 更新`docs/recovery-mvp-operations.md`：加入Host Facts与Inventory命令、秘密注入边界和草案阻止PreInstall的唯一发布顺序。
- 更新`docs/contracts/recovery-mvp-v1.md`打包部署MUST：发布Inventory合同/example/facts，草案fail-closed且facts零远程/零写入。
- 修改`orchestrator/control_plane/__main__.py`接入Inventory prepare模式及脱敏机器输出；待并行Inventory模块落地后补CLI测试并校准返回类型。
- 扩展`tests/test_config_check_cli.py`：prepare成功/失败/既有目录/模式互斥、manifest复算与秘密注入前后权威配置检查。
- Packaging交付：构建复制3件、源/包/manifest三方SHA测试；新增2项PASS，相关集23 PASS/1临时deselect，无测试dist残留。
- Root复核packaging路径/清理/hash断言正确；记录待补CP help中的`--prepare-deployment`与`--output-dir`。
- 更新architecture/roadmap：部署准备边界、Inventory链接、旧Frozen与新工具编码中状态均明确。
- 更新`recovery-mvp-traceability.md`新增DEP-07 Inventory与DEP-08 Host Facts端到端追踪。
- Root完成共享探针目标验证器代码/Agent调用点/测试复核，确认二次验证与错误语义保持。
- 工作区隔离basetemp下`test_probe_targets + test_agent_probes + test_agent_app`为49 passed/0 warning。
## 2026-07-17：Phase 2 Inventory 实现审计接管

- 已完整审阅 `orchestrator/deployment/inventory.py` 与公开导出。
- 已确认模型与渲染主体已落地，但 CLI 调用签名、manifest 哈希字段、Windows 路径约束及多网卡地址唯一性仍需修正。
- 原实现代理未完成测试；下一步由主任务补齐 `tests/test_deployment_inventory.py`，并运行 Inventory、CLI、探针与打包回归。
- 已修正 CP `--prepare-deployment` 入口，改为调用完整 `prepare_deployment` 流程；manifest 输入哈希键统一为 `inventory_sha256`。
- 已加固 Windows data directory（非法字符、尾随空格/点、保留设备名）及 Agent 所有声明接口地址的全局唯一性，并同步权威合同。
- `python -m compileall -q orchestrator` 通过；全仓相关路径中已无 `input_sha256` 旧名称或错误的 `render_deployment` 外部调用。
- Inventory CLI 首轮测试尚未进入用例：`uv run pytest` 入口未把仓库根加入模块搜索路径，收集阶段报 `ModuleNotFoundError: orchestrator`。该结果不是产品失败；下一次改用 `uv run python -m pytest`，不重复相同命令。
- `uv run python -m pytest` 又在测试前因用户级 uv cache ACL 失败；未修改系统/用户 ACL。后续使用已能成功运行 `compileall` 的当前 Python 直接执行 pytest。
- CP `--prepare-deployment` CLI 专项已通过：`7 passed, 28 deselected`。真实子进程链路验证了成功渲染、manifest/文件哈希、secret sentinel 双向配置门禁、失败脱敏、已有目录保护和模式互斥。
- 收敛工作树上的完整打包合同已通过：`24 passed`。此前因 Inventory 模块尚未落盘而暂时排除的 CP help 用例现已恢复通过，Phase 2 新脚本/示例/合同也通过源码—包内—manifest 三重哈希证明。
- Inventory 独立对抗测试首跑 `92 passed, 2 failed`；两项仅因并发编写期间测试仍调用旧 `input_sha256` 关键字。产品新增的全局接口地址、Windows 路径和 `inventory_sha256` 规则已在其余用例中通过，代理正只修测试签名后复跑。
- 独立测试签名已收敛，`tests/test_deployment_inventory.py` 最终 `94 passed in 0.89s`，且 `git diff --check` 通过。Inventory 严格模型、DAG、探针目标与原子渲染已具备独立合同证明。
- 冻结前独立审计发现 3 项新增 P1，当前 Inventory 阶段仍保持进行中：schema version strict integer、prepare 模式惰性加载 Web 栈、临时目录清理失败不可静默。将先修产品并补回归，再重新认定合同通过。
- 三项 P1 产品修复已落地：schema version 前置类型检查、Web 栈正常启动分支惰性导入、临时清理不再 `ignore_errors` 且验证目录消失。公开 renderer 同时改为内部函数，消除调用者伪造输入哈希的 P2 面。
- 收敛后的 Inventory + 全部配置 CLI 回归通过：`135 passed in 16.49s`。其中新增证明覆盖 strict `schema_version` bool/float、1025 服务、非 64 位节点、清理失败显式化及 prepare 模式不导入 uvicorn/CP App；compileall 与目标 diff-check 同时通过。
- Host Facts 主线审计已开始；现有 9 项测试尚未覆盖非法输入 hostname 采集、端口提供程序失败被误报为空闲、link-local 与 Inventory 不兼容、以及空 OS/接口/服务字段仍 PASS。阶段保持进行中，先修合同和测试再复跑。
- Host Facts 又确认真实 CLI 参数绑定泄漏缺口：非法端口文本当前在脚本逻辑前以 exit 1/PowerShell错误退出。修复将把候选端口改为脚本内严格解析，并新增真实 CLI 脱敏 JSON 反例。
- Host Facts 加固及合同同步完成，Windows PowerShell 5.1 专项 `12 passed in 3.08s`：真实非法端口 CLI 已统一为脱敏 JSON/exit 2，非法输入零 collector，地址过滤与 Inventory 一致，OS/接口/服务/端口采集失败均 fail closed。
- 独立审计剩余输出边界已关闭：移除未文档化 `PassThru`/别名，Tunnel 仅按地址规则判断，collector 对象逐字段按基础类型重建，异常对象/额外属性不可透传；服务状态和端口事实矛盾均 fail closed。扩展后的 PS5.1 专项 `16 passed in 4.39s`。
- 已回读构建器与部署手册并确认新发布目录 `dist-recovery-phase2-20260717` 尚不存在；Phase 2 前三项规划已标记完成，下一步先跑收敛打包合同，再构建该独立 Frozen 候选。
- Host Facts 最终修改后的完整打包合同再次通过：`24 passed in 8.23s`。发布脚本对 Phase 2 文件的复制和 manifest 哈希规则已在构建前收敛。
- Phase 2 Frozen 首次构建调用因错误设置 1 秒 shell timeout 在 pip 阶段返回 exit 124，构建结果未知。按规划规则暂停重试，先审计残留进程与 `dist-recovery-phase2-20260717` 半成品。
- 残留审计的 CIM 进程查询被当前权限拒绝，且组合调用未保留目录结果；不提权，改用普通进程摘要与单独目录检查。
- 非特权检查确认无残留 Python 进程，输出目录仅有受构建器管理的 `.build` 半成品。将用 10 分钟命令超时继续同一构建，并以短周期 wait 观察。
- `dist-recovery-phase2-20260717` 已用 Python 3.13.5 / PyInstaller 6.16.0 成功构建三个 onedir 包，构建脚本 exit 0（68 秒）。下一步独立复算发布 manifest 并执行 Frozen 离线功能/烟雾验证。
- 新包 `verify_recovery_distribution.ps1` 返回 PASS：expected/actual 均 334 个受管文件，无缺失、额外或哈希错误。尚需记录 manifest 自身哈希并执行 Frozen prepare/烟雾。
- Frozen Inventory prepare 已成功；manifest 自身 SHA-256 为 `1c8e6d3df090acf93df753fd6644929615bc409264c7ef82d2195fbd681c8635`。随后只读全栈烟雾因当前账户 WMI `Win32_OperatingSystem` PermissionDenied 失败，Agent 按合同拒绝伪造 boot identity，CP正常；这是目标机权限门禁未满足，需先核对清理再决定发布状态。
- 烟雾受管进程/端口已清理，但失败临时树仍含 8 个文件，暴露随机烟雾秘密残留风险。当前 Phase 2 包撤回为未冻结；先清理本次明确 temp 目录、修复脚本无条件安全清理、补测试并重建。
- 已按“系统 temp 直接子目录 + `winsw-recovery-smoke-` 前缀”双重校验，删除本次明确残留树并复核不存在。源码烟雾脚本当前仅在成功时清理，且没有独立失败清理测试；下一步改为 finally 无条件安全清理。
- 烟雾脚本已改为 finally 无条件调用受管清理函数；函数仅允许系统 temp 的直接 `winsw-recovery-smoke-*` 非 reparse 目录。真实删除与越界保留专项 `2 passed`，当前候选需重建以纳入修复。
- Phase 2 onedir 已在同一受管目录重建成功（exit 0，66.2 秒），包含无条件烟雾清理修复。此前 manifest/hash/prepare 证据作废，必须以本次重建结果重新计算。
- 重建后分发验证 PASS（334/334，零异常），manifest SHA-256 更新为 `7d8af82d5bf7c6c0695090b61bf8e0743a466c07923b2c8cc92b6658d9b4180e`；Frozen prepare 再次成功。相同受限权限下不重复已知 WMI 失败，下一步请求在非沙箱只读权限下执行烟雾。
- 非沙箱只读 Frozen 烟雾通过：CP/Agent health=`ok`、LAB_HTTP、AgentCount=1、EventLog 状态 INSTALLED/ACTIVE/AUTOSTART_ENABLED、0 Group、Dashboard 200、SideEffects=NONE。确认先前失败由受限 WMI 权限造成，不是 Frozen 产品缺陷。
- 全仓回归通过：`467 passed, 4 warnings in 85.53s`。4 条均为 `jsonschema.RefResolver` / OpenAPI validator 已知弃用警告，无测试失败。进入最终 diff/发布树/文档状态审计。
- roadmap/README 已同步 Phase 2 工具与 Frozen 候选完成状态，并继续保留真实三机 10 轮未通过/非生产可用边界；task plan 的发布候选项已完成。
- 最终 compileall、全仓 diff-check、发布树 334 文件复验通过；4 个 Phase 2 关键交付的源码/包内/manifest 哈希三方一致。Git 仅输出 CRLF 转换提示，exit 0。
- Frozen fail-closed 复核通过：CP/3 Agent sentinel 草案全部固定 exit2，包内 Host Facts 非法端口脱敏 FAIL。最终清理范围已限定为 `.test-tmp`、`.pytest-tmp-packaging-agent`、`.pytest-tmp-probe-validator-20260717` 三个本轮测试目录，不触碰用户代码或历史发布包。
- 三个受管测试临时目录已在逐一校验为仓库直接子目录后清理，结果 `CLEAN`；系统烟雾临时树、loopback listener 和 Frozen 进程也均无残留。

## 2026-07-17：Phase 2 编码交付结论

- Deployment Inventory、Host Facts、CLI、文档、打包与新 Frozen 候选均已实现并验证；当前编码阶段完成。
- 发布候选：`dist-recovery-phase2-20260717`；文件 334/334；manifest 自身 SHA-256：`7d8af82d5bf7c6c0695090b61bf8e0743a466c07923b2c8cc92b6658d9b4180e`。
- 自动化证据：467 passed；Frozen prepare、sentinel fail-closed、包内 Host Facts、真实 WMI/SCM + loopback 烟雾全部通过。
- 下一步不能靠本机推测：需用户提供独立 CP 与至少三台业务服务器的 Host Facts/非秘密 Inventory，并明确现场只读预检授权；在此之前不安装、不注册、不启动任何 Recovery 或业务服务。

## 2026-07-17：完整目标续审

- 已重新读取当前工作树、Phase 2–7 未完成项和追踪矩阵；没有把先前“编码完成”结论直接当作目标完成。
- 已启动三路只读独立审计：Agent/CP 核心状态机、Web/验收证据、部署/安装链。
- 主线检索确认 Run now/retry/AUTO发现、Run Detail和 evidence validator 均有实现；下一步等待独立逐项审计，优先处理仍可本地编码的缺口。
- Web/验收审计已排除“缺少机器证据导出 API”这一疑点，但发现 evidence 语义校验与最小 Web 可操作性共四项 P1 候选；下一步读取实现与现有测试，先写失败复现，再修产品。

## 2026-07-17：最终独立审计转入 Phase 1S

- 三路只读审计均已收敛：未发现 P0；发现 CP 手工 Operation 绑定/脱敏、Evidence false-PASS、Groups/Run 快照 Web、PreInstall 端口 fail-open、安装器并发事务五类本地 P1。
- 两项 P2 为 WMI boot identity 稳定 CLI 错误和单服务 Web Operation 终态轮询；将在不扩大 MVP 范围的前提下评估。
- `task_plan.md` 已重新打开本地编码阶段并阻断旧 Frozen 进入现场；下一步按“先复现、后最小修复、定向回归”的顺序并行处理互不重叠的代码域。
- 部署审计追加构建输出根污染 P1；已交由同一部署链任务与端口查询、安装互斥一起按独立失败复现修复。当前已验证的 Phase 2 包本身无未知顶层项，但后续重建必须经过新门禁。
- Web 红灯证据完成：`test_group_readiness_ui.py + test_run_detail_ui.py` 为 2 failed，分别精确命中依赖编辑控件缺失和 Run 冻结快照缺失；下一步只改 Web 展示/交互后重跑同两项。
