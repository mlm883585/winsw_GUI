from pathlib import Path
import tkinter as tk
from tkinter import ttk

from core.app_paths import AppPaths


class ServiceListView(ttk.Frame):
    """ 左侧的服务列表视图 """

    def __init__(self, parent, app_paths_or_service_dir, select_callback):
        super().__init__(parent)
        self.select_callback = select_callback
        if isinstance(app_paths_or_service_dir, AppPaths):
            self.app_paths = app_paths_or_service_dir
            service_dir = app_paths_or_service_dir.services_dir
        else:
            self.app_paths = None
            service_dir = app_paths_or_service_dir
        self.service_dir = Path(service_dir)

        # --- UI Elements ---
        # selectmode=EXTENDED 支持 Ctrl/Shift 多选，用于批量操作
        self.listbox = tk.Listbox(self, exportselection=False, selectmode=tk.EXTENDED)
        self.listbox.pack(expand=True, fill="both", padx=5, pady=5)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)

        self.refresh_list()

    def refresh_list(self, selected_filename=None):
        """刷新服务列表，扫描services目录"""
        self.listbox.delete(0, tk.END)
        try:
            files = [path.name for path in self.service_dir.iterdir()
                     if path.is_file() and path.suffix.lower() == ".xml"]
            for filename in sorted(files):
                self.listbox.insert(tk.END, filename)
        except FileNotFoundError:
            # services目录可能不存在
            pass
        return self.select_filename(selected_filename) if selected_filename else False

    def select_filename(self, filename):
        """Select a filename without synthesizing a user selection event."""
        self.listbox.selection_clear(0, tk.END)
        if not filename:
            return False
        for index in range(self.listbox.size()):
            if self.listbox.get(index) == filename:
                self.listbox.selection_set(index)
                self.listbox.activate(index)
                self.listbox.see(index)
                return True
        return False

    def on_select(self, event):
        """当用户在列表中选择项目时调用。

        仅在恰好选中 1 项时把配置载入右侧编辑区；多选时不改动编辑区，
        避免反复重载，此时控制按钮会进入批量模式。
        """
        selection_indices = self.listbox.curselection()
        if len(selection_indices) != 1:
            return

        filename = self.listbox.get(selection_indices[0])

        # 调用主窗口传递的回调函数
        if self.select_callback:
            self.select_callback(filename)

    def get_selected_filename(self):
        """获取当前选中的第一个文件名（单服务操作用）"""
        selection_indices = self.listbox.curselection()
        if not selection_indices:
            return None
        return self.listbox.get(selection_indices[0])

    def get_selected_filenames(self):
        """获取当前选中的所有文件名（批量操作用）"""
        return [self.listbox.get(i) for i in self.listbox.curselection()]
