# Phase 1 — 本机多服务批量操作 + 服务模板（已实现）

> 状态：**已完成**（as-built）。本文档记录已落地的设计与实现，作为后续阶段的基线参考。

## 1. 目标与价值

在不改动整体架构的前提下，为现有单机 Tkinter 应用补齐两项能力，作为 Agent 化 / Web 化的地基：

1. **多选 + 批量操作**：服务列表支持多选，7 个控制操作（安装/卸载/启动/停止/重启/状态/刷新）可一次作用于多个服务。
2. **服务模板**：从 `templates/*.xml` 一键新建服务，并内置 java/nodejs/nacos/mysql/redis/nginx 六个模板。

## 2. 范围

| In（本阶段做） | Out（不做，留待后续） |
|----------------|------------------------|
| 本机多选、批量执行 WinSW 命令 | 跨机器、远程执行（Phase 2/3） |
| 从模板一键新建服务 | 依赖关系、就绪探针、编排（Phase 4） |
| 6 个内置服务模板 | 模板携带平台级依赖/探针字段（这些是 CP 侧数据，Phase 4，**不入 XML**） |

## 3. 详细设计

### 3.1 多选与"单服务编辑"的协调
右侧编辑区一次只能编辑一个服务，因此定下规则：
- 列表 `selectmode=tk.EXTENDED`（支持 Ctrl/Shift 多选）。
- **恰好 1 项选中** → 沿用原行为：把该服务配置载入右侧编辑区。
- **≥2 项选中** → 不改动编辑区（避免反复重载），控制按钮进入批量模式。
- 控制按钮**自动判别**单/批量，不新增按钮行。

### 3.2 批量执行流程
`MainWindow._execute_batch_command(command_func, action_name)`：
1. 取 `service_list.get_selected_filenames()`。
2. 一次性确认（显示 N 个服务）。
3. 逐个 `config = config_manager.load_from_xml(services/<file>)` → `command_func(config)`，按 `[i/N]` 打印进度。
4. **基于磁盘已保存 XML**，不触发 `save_service`（与单服务"先保存当前编辑"行为区分，日志中提示）。

`_dispatch_command()` 按选中数量分发：≥2 走批量，否则走原 `_execute_service_command`（保证单选零回归）。

### 3.3 模板机制
- `core/template_manager.py`：`list_templates()` 扫描 `templates/*.xml` 返回 `[(显示名, 路径)]`；`load_template()` 复用 `ConfigManager.load_from_xml`。
- `MainWindow.new_from_template()` 弹出模态选择框（Toplevel + Listbox，双击/确定选定）；`_apply_template()` 以 `new_service()` 为蓝本把模板载入为未保存新配置，用户改 `id` 后保存。

## 4. 关键约束与已知坑

- **模板只用 ConfigManager 支持的标签**：`id/name/description/executable/arguments/workingdirectory/env/log(mode)/onfailure/serviceaccount/resetfailure/priority/stoptimeout/logpath/interactive`。其余标签（如 WinSW 原生 `<depend>`）会在 save 时被丢弃。
  - 澄清：WinSW `<depend>` 是**同机 OS 级**（SCM）依赖，与平台的**跨机** `depends_on` 是两回事；后者及探针是 **CP 侧数据、不进 XML**（见 architecture §7.3）。因此 Phase 4 **不需要**为平台依赖/探针扩展 ConfigManager 的 XML 标签；仅当未来想支持同机 OS 级 `<depend>` 才涉及 ConfigManager 扩展（可选、正交）。
- **XML 注释不能含 `--`**：实现中曾因 `mysql.xml` 注释里写 `--console` 触发解析失败，已修正。新增模板务必避免注释内出现双横线。

## 5. 改动清单

| 文件 | 改动 |
|------|------|
| `gui/service_list_view.py` | `selectmode=EXTENDED`；新增 `get_selected_filenames()`；`on_select` 仅单选时载入 |
| `gui/main_window.py` | 接入 `TemplateManager`；新增 `new_from_template()`/`_apply_template()`/`_execute_batch_command()`/`_dispatch_command()`；7 个控制命令改自动分发 |
| `gui/actions_panel.py` | 配置管理行新增"从模板新建"按钮 |
| `core/template_manager.py`（新增） | `list_templates()` / `load_template()` |
| `templates/{java,nodejs,nacos,mysql,redis,nginx}.xml`（新增） | 6 个服务模板 |

## 6. 验证结果（已通过）

- 4 个改动文件字节编译通过。
- 7 个模板经 `load→save→load` 往返，14 个核心字段零丢失。
- 无 display 的 GUI 构建冒烟通过（回调接线 / 多选模式 / 新方法 / 模板计数）。
- 待真实环境人工走查：模板新建落盘、多选批量 `[i/N]` 输出、单选回归。

## 7. 对后续阶段的启示

- `WinSWManager` 的 7 个方法均以 `config` dict 为输入、自行定位 XML——这一契约让 Phase 2 把它整体下沉为 Agent 后端几乎零改造。
- 平台级依赖/探针归 **CP 表、不进 XML**（architecture §7.3），故 `ConfigManager` 的 XML 标签集**无需**为此扩展。模板的"默认探针"（architecture §5）在从模板建服务时用于**播种 CP 探针表**，而非写入 XML。
