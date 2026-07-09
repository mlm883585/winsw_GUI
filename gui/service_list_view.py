import os
import tkinter as tk
from tkinter import ttk


class ServiceListView(ttk.Frame):
    """ 左侧的服务列表视图 """

    def __init__(self, parent, select_callback):
        super().__init__(parent)
        self.select_callback = select_callback
        self.service_dir = "services"

        # --- UI Elements ---
        # selectmode=EXTENDED 支持 Ctrl/Shift 多选，用于批量操作
        self.listbox = tk.Listbox(self, exportselection=False, selectmode=tk.EXTENDED)
        self.listbox.pack(expand=True, fill="both", padx=5, pady=5)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)

        self.refresh_list()

    def refresh_list(self):
        """刷新服务列表，扫描services目录"""
        self.listbox.delete(0, tk.END)
        try:
            files = [f for f in os.listdir(self.service_dir) if f.endswith(".xml")]
            for filename in sorted(files):
                self.listbox.insert(tk.END, filename)
        except FileNotFoundError:
            # services目录可能不存在
            pass

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
