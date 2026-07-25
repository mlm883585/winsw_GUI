import os
import re
from copy import deepcopy
from pathlib import Path
import sys
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox, filedialog

# 模块导入
from core.config_manager import (
    ConfigConflictError,
    ConfigError,
    ConfigManager,
)
from core.winsw_manager import WinSWManager
from gui.actions_panel import ActionsPanel
from gui.output_console import OutputConsole
from gui.service_list_view import ServiceListView
from gui.settings_window import SettingsWindow
from gui.tabs.account_tab import AccountTab
from gui.tabs.advanced_tab import AdvancedTab
from gui.tabs.basic_info_tab import BasicInfoTab
from gui.tabs.environment_tab import EnvironmentTab
from gui.tabs.execution_tab import ExecutionTab
from gui.tabs.log_viewer_tab import LogViewerTab
from gui.tabs.logging_tab import LoggingTab
from gui.tabs.recovery_tab import RecoveryTab
from gui.tabs.xml_editor_tab import XmlEditorTab


class MainWindow(ttk.Frame):
    def __init__(self, parent, settings_manager, app_version, app_paths):
        super().__init__(parent)
        self.parent = parent
        self.settings_manager = settings_manager
        self.app_version = app_version
        self.app_paths = app_paths

        self.config_manager = ConfigManager(app_paths)
        self.current_document = self.config_manager.new_document()
        self.current_config = deepcopy(self.current_document.values)
        self.current_filepath = None
        self._previous_selected_filename = None

        self.create_menu(parent)
        self.create_widgets()
        self.winsw_manager = WinSWManager(
            self.console.log, self.settings_manager, app_paths
        )
        self.apply_stored_settings()

        # 在UI创建完毕后，设置回调和重定向输出
        self.setup_console_redirect()

        self._set_current_config_to_ui(self.current_document.values)
        self.xml_editor_tab.load_from_ui()
        self.service_list.refresh_list()
        # 这条 print 现在会安全地输出到UI控制台
        print("WinSW GUI 初始化完成。")

    def create_menu(self, root):
        self.menubar = tk.Menu(root)
        root.config(menu=self.menubar)

        tools_menu = tk.Menu(self.menubar, tearoff=0)
        tools_menu.add_command(label="设置...", command=self.open_settings_window)
        self.menubar.add_cascade(label="工具", menu=tools_menu)

        help_menu = tk.Menu(self.menubar, tearoff=0)
        help_menu.add_command(label="项目主页 (GitHub)", command=self.open_link)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self.show_about_dialog)
        self.menubar.add_cascade(label="帮助", menu=help_menu)

    def setup_console_redirect(self):
        """
        将标准输出和错误安全地重定向到UI控制台。
        这样可以确保 print() 在任何情况下（包括打包后）都能正常工作。
        """
        self.winsw_manager.log = self.console.log

        # 定义一个拥有 write 方法的类，用于替换 sys.stdout
        class ConsoleRedirector:
            def __init__(self, console_log_method):
                self.console_log_method = console_log_method

            def write(self, message):
                # 调用UI控制台的log方法来显示信息
                self.console_log_method(message)

            def flush(self):
                # 在此场景下，flush方法无需任何操作
                pass

        # 创建重定向器实例并替换
        redirector = ConsoleRedirector(self.console.log)
        sys.stdout = redirector
        sys.stderr = redirector

    def open_link(self):
        webbrowser.open_new(r"https://github.com/ztxtech/winsw_GUI")

    def show_about_dialog(self):
        """显示关于对话框"""
        messagebox.showinfo(
            "关于 WinSW 图形化管理工具",
            f"版本: {self.app_version}\n"
            "作者: ztxtech\n"
            "这是一个用于管理 WinSW 服务的图形化界面工具。"
        )

    def open_settings_window(self):
        SettingsWindow(self.parent, self.settings_manager)

    def create_widgets(self):
        self.main_paned_window = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.main_paned_window.pack(expand=True, fill="both")

        left_frame = ttk.Frame(self.main_paned_window)
        self.service_list = ServiceListView(
            left_frame, self.app_paths, self.on_service_selected
        )
        self.service_list.pack(expand=True, fill="both")
        self.main_paned_window.add(left_frame, weight=1)

        right_container_frame = ttk.Frame(self.main_paned_window)
        self.main_paned_window.add(right_container_frame, weight=4)

        self.right_paned_window = ttk.PanedWindow(right_container_frame, orient=tk.VERTICAL)
        self.right_paned_window.pack(expand=True, fill="both")

        right_top_frame = ttk.Frame(self.right_paned_window)
        self.right_paned_window.add(right_top_frame, weight=4)

        callbacks = {
            'new': self.new_service, 'save': self.save_service, 'import': self.import_service_xml,
            'delete': self.delete_service_config, 'install': self.install_service,
            'uninstall': self.uninstall_service, 'start': self.start_service, 'stop': self.stop_service,
            'restart': self.restart_service, 'status': self.status_service, 'refresh': self.refresh_service
        }
        self.actions_panel = ActionsPanel(right_top_frame, callbacks)
        self.actions_panel.pack(fill="x", padx=5, pady=5)

        notebook = ttk.Notebook(right_top_frame)
        notebook.pack(expand=True, fill="both", padx=5, pady=(0, 5))

        # 实例化所有Tab
        self.basic_info_tab, self.execution_tab, self.environment_tab = BasicInfoTab(notebook), ExecutionTab(notebook,
                                                                                                             self.autofill_from_executable), EnvironmentTab(
            notebook)
        self.logging_tab, self.recovery_tab, self.account_tab = LoggingTab(notebook), RecoveryTab(notebook), AccountTab(
            notebook)
        self.advanced_tab = AdvancedTab(notebook)
        xml_editor_callbacks = {
            'get_xml': self._get_xml_from_ui,
            'apply_xml': self._apply_xml_from_editor,
        }
        self.xml_editor_tab = XmlEditorTab(notebook, xml_editor_callbacks)
        self.log_viewer_tab = LogViewerTab(notebook)

        tabs = {"基本信息": self.basic_info_tab, "执行与参数": self.execution_tab, "环境变量": self.environment_tab,
                "日志记录": self.logging_tab, "恢复机制": self.recovery_tab,
                "服务账户": self.account_tab, "高级选项": self.advanced_tab, "XML源码": self.xml_editor_tab,
                "日志查看": self.log_viewer_tab}
        for text, tab in tabs.items(): notebook.add(tab, text=text)

        self.console = OutputConsole(self.right_paned_window)
        self.right_paned_window.add(self.console, weight=1)

    def apply_stored_settings(self):
        try:
            self.parent.geometry(self.settings_manager.get('window_geometry'))
            self.parent.after(100,
                              lambda: self.main_paned_window.sashpos(0, self.settings_manager.get('main_sash_pos')))
            self.parent.after(100,
                              lambda: self.right_paned_window.sashpos(0, self.settings_manager.get('right_sash_pos')))
        except tk.TclError as e:
            print(f"应用保存的设置时出错 (可能是首次启动): {e}")

    def save_current_settings(self):
        self.settings_manager.set('window_geometry', self.parent.winfo_geometry())
        self.settings_manager.set('main_sash_pos', self.main_paned_window.sashpos(0))
        self.settings_manager.set('right_sash_pos', self.right_paned_window.sashpos(0))
        self.settings_manager.save_settings()
        print("窗口状态已保存。")

    def request_close(self):
        if not self._confirm_navigation():
            return False
        try:
            self.save_current_settings()
        except OSError as exc:
            messagebox.showerror("保存设置失败", f"无法保存窗口设置：\n{exc}")
            return False
        self.log_viewer_tab.stop_monitoring()
        return True

    def _get_current_config_from_ui(self) -> dict:
        config = self.basic_info_tab.get_data()
        config.update(self.execution_tab.get_data())
        config.update(self.environment_tab.get_data())
        config.update(self.logging_tab.get_data())
        config.update(self.recovery_tab.get_data())
        config.update(self.account_tab.get_data())
        config.update(self.advanced_tab.get_data())
        return config

    def _set_current_config_to_ui(self, config):
        self.current_config = deepcopy(config)
        self.basic_info_tab.set_data(self.current_config)
        self.execution_tab.set_data(self.current_config)
        self.environment_tab.set_data(self.current_config)
        self.logging_tab.set_data(self.current_config)
        self.recovery_tab.set_data(self.current_config)
        self.account_tab.set_data(self.current_config)
        self.advanced_tab.set_data(self.current_config)

    def _document_from_ui(self):
        document = deepcopy(self.current_document)
        return self.config_manager.merge_ui_data(
            document, self._get_current_config_from_ui()
        )

    def _get_xml_from_ui(self):
        return self.config_manager.to_xml_string(self._document_from_ui())

    def _apply_xml_from_editor(self, xml_text):
        document = deepcopy(self.current_document)
        self.config_manager.apply_xml(document, xml_text)
        self.current_document = document
        self._set_current_config_to_ui(document.values)
        return True

    def _resolve_xml_draft(self):
        if not self.xml_editor_tab.is_dirty():
            return True

        decision = messagebox.askyesnocancel(
            "XML 源码尚未应用",
            "XML 源码有未应用的修改。\n\n"
            "选择“是”应用到表单，选择“否”放弃草稿，选择“取消”留在当前页面。",
        )
        if decision is None:
            return False
        if decision is False:
            self.xml_editor_tab.load_from_ui()
            return True

        try:
            self._apply_xml_from_editor(self.xml_editor_tab.get_xml_text())
            self.xml_editor_tab.mark_clean()
            return True
        except ConfigError as exc:
            messagebox.showerror("解析失败", f"无法应用 XML：\n{exc}")
            return False

    def _confirm_navigation(self):
        """Resolve XML and form changes before replacing the current document."""
        if not self._resolve_xml_draft():
            return False

        document = self._document_from_ui()
        if not self.config_manager.is_dirty(document):
            return True

        decision = messagebox.askyesnocancel(
            "存在未保存的修改",
            "是否先保存当前服务配置？\n\n"
            "选择“否”将放弃修改，选择“取消”返回当前配置。",
        )
        if decision is None:
            return False
        if decision is True:
            return self.save_service(resolve_xml=False)
        self.config_manager.revert(self.current_document)
        self._set_current_config_to_ui(self.current_document.values)
        self.xml_editor_tab.load_from_ui()
        return True

    def _activate_document(self, document, selected_filename=None):
        self.log_viewer_tab.clear_context()
        self.current_document = document
        self.current_filepath = document.source_path
        self._set_current_config_to_ui(document.values)
        self.xml_editor_tab.load_from_ui()
        self._previous_selected_filename = selected_filename
        if selected_filename:
            self.service_list.select_filename(selected_filename)
        else:
            self.service_list.listbox.selection_clear(0, tk.END)
        if document.source_path is not None:
            self.log_viewer_tab.start_monitoring(
                document.values, document.source_path
            )

    def on_service_selected(self, filename: str):
        if filename == self._previous_selected_filename:
            return True
        previous = self._previous_selected_filename
        if not self._confirm_navigation():
            self.service_list.select_filename(previous)
            return False
        fallback_selection = self._previous_selected_filename
        print(f"已选择服务: {filename}")
        try:
            document = self.config_manager.load_managed(filename)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("加载失败", f"无法加载配置文件：\n{exc}")
            self.service_list.select_filename(fallback_selection)
            return False
        self._activate_document(document, filename)
        print("配置已加载到UI。")
        return True

    def new_service(self):
        if not self._confirm_navigation():
            return False
        print("正在创建新配置...")
        self._activate_document(self.config_manager.new_document())
        print("UI已重置为新配置。")
        return True

    def autofill_from_executable(self, exe_path: str):
        if not self.basic_info_tab.id_var.get() and not self.basic_info_tab.name_var.get():
            try:
                raw_name, _ = os.path.splitext(os.path.basename(exe_path))
                service_id = re.sub(r"[^A-Za-z0-9]", "", raw_name)
                if not service_id:
                    service_id = "Service"
                if not self.config_manager.is_valid_service_id(service_id):
                    service_id = f"{service_id}Service"
                self.basic_info_tab.id_var.set(service_id)
                self.basic_info_tab.name_var.set(raw_name or service_id)
                print(f"已自动填充服务ID和名称: '{service_id}'")
            except Exception as e:
                print(f"自动填充失败: {e}")

    def save_service(self, resolve_xml=True):
        if resolve_xml and not self._resolve_xml_draft():
            return False

        config_data = self._get_current_config_from_ui()
        service_id = config_data.get('id')
        if (
            service_id
            and not config_data.get('logpath')
            and self.current_document.source_path is None
            and self.current_document.origin_path is None
        ):
            default_log_path = self.app_paths.logs_dir / service_id
            config_data['logpath'] = str(default_log_path)
            print(f"日志目录为空，已自动设置为: {default_log_path}")

        document = deepcopy(self.current_document)
        self.config_manager.merge_ui_data(document, config_data)
        try:
            try:
                saved_path = self.config_manager.save(document)
            except ConfigConflictError as conflict:
                if not conflict.overwritable:
                    raise
                if not messagebox.askyesno(
                    "配置已存在",
                    f"目标配置 '{conflict.existing.name}' 已存在。\n确定要覆盖吗？",
                ):
                    return False
                saved_path = self.config_manager.save(document, allow_overwrite=True)
        except (ConfigError, OSError) as exc:
            messagebox.showerror("保存失败", f"无法保存服务配置：\n{exc}")
            return False

        self.current_document = document
        self.current_filepath = saved_path
        self._set_current_config_to_ui(document.values)
        self.service_list.refresh_list(saved_path.name)
        self._previous_selected_filename = saved_path.name
        self.xml_editor_tab.load_from_ui()
        self.log_viewer_tab.start_monitoring(document.values, saved_path)
        print(f"配置已安全保存到: {saved_path}")
        return True

    def delete_service_config(self):
        selected_file = self.service_list.get_selected_filename()
        if not selected_file:
            messagebox.showwarning("操作无效", "请先从列表中选择一个服务配置。")
            return False
        if selected_file == self._previous_selected_filename and not self._confirm_navigation():
            return False
        selected_file = self.service_list.get_selected_filename()
        if not selected_file:
            return False
        if messagebox.askyesno("确认删除", f"你确定要删除配置文件 '{selected_file}' 吗？\n此操作不可恢复。"):
            try:
                candidate = self.app_paths.services_dir / selected_file
                if candidate.is_symlink():
                    raise OSError("配置文件不能是符号链接")
                target = candidate.resolve(strict=True)
                if target.parent != self.config_manager.services_dir or target.suffix.lower() != '.xml':
                    raise OSError("配置文件路径不安全")
                target.unlink()
                print(f"配置文件 '{selected_file}' 已删除。")
                self.service_list.refresh_list()
                self._activate_document(self.config_manager.new_document())
                return True
            except OSError as e:
                messagebox.showerror("删除失败", f"无法删除文件: {e}")
        return False

    def import_service_xml(self):
        filepath = filedialog.askopenfilename(title="选择要导入的WinSW XML配置文件", filetypes=[("XML files", "*.xml")])
        if not filepath:
            return False
        if not self._confirm_navigation():
            return False
        try:
            document = self.config_manager.load_external(filepath)
            self._activate_document(document)
            print(f"已打开外部配置 '{Path(filepath).name}'，保存前不会修改源文件。")
            return True
        except (ConfigError, OSError) as e:
            messagebox.showerror("导入失败", f"无法导入文件: {e}")
            return False

    def _execute_service_command(self, command_func):
        if not self._resolve_xml_draft():
            return
        document = self._document_from_ui()
        try:
            self.config_manager.validate(document)
        except ConfigError as exc:
            messagebox.showerror("配置无效", str(exc))
            return

        service_id = document.values.get('id')
        if not messagebox.askyesno("确认操作", f"你确定要对服务 '{service_id}' 执行此操作吗？"):
            return

        if not self.save_service(resolve_xml=False):
            return
        command_func(self.current_document.source_path)

    def install_service(self):
        self._execute_service_command(self.winsw_manager.install)

    def uninstall_service(self):
        self._execute_service_command(self.winsw_manager.uninstall)

    def start_service(self):
        self._execute_service_command(self.winsw_manager.start)

    def stop_service(self):
        self._execute_service_command(self.winsw_manager.stop)

    def restart_service(self):
        self._execute_service_command(self.winsw_manager.restart)

    def status_service(self):
        self._execute_service_command(self.winsw_manager.status)

    def refresh_service(self):
        self._execute_service_command(self.winsw_manager.refresh)
