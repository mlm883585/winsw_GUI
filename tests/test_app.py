import sys
import tkinter as tk

from core.app_paths import AppPaths
from main import App


def test_app_uses_injected_portable_paths_instead_of_working_directory(
    tmp_path, monkeypatch
):
    portable_root = tmp_path / "便携 程序"
    working_directory = tmp_path / "elsewhere"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)
    paths = AppPaths.from_root(portable_root)
    root = tk.Tk()
    root.withdraw()
    old_stdout, old_stderr = sys.stdout, sys.stderr

    try:
        app = App(root, paths)

        assert app.settings_manager.app_paths is paths
        assert app.main_window.config_manager.app_paths is paths
        assert app.main_window.winsw_manager.app_paths is paths
        assert app.main_window.service_list.app_paths is paths
        assert app.settings_manager.settings_file == paths.settings_file
        assert paths.services_dir.is_dir()
        assert paths.logs_dir.is_dir()
        assert paths.bin_dir.is_dir()
        assert not (working_directory / "services").exists()
        app.main_window.log_viewer_tab.stop_monitoring()
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        root.destroy()
