import sys
from pathlib import Path

import pytest

import core.app_paths as app_paths_module
from core.app_paths import AppPaths, AppPathsError


def test_from_root_builds_all_application_paths(tmp_path):
    root = tmp_path / "应用 root"

    paths = AppPaths.from_root(root)

    assert paths.root == root.resolve()
    assert paths.settings_file == root.resolve() / "settings.json"
    assert paths.services_dir == root.resolve() / "services"
    assert paths.logs_dir == root.resolve() / "logs"
    assert paths.bin_dir == root.resolve() / "bin"


def test_discover_uses_source_root_independent_of_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.chdir(tmp_path)

    paths = AppPaths.discover()

    expected_root = Path(app_paths_module.__file__).resolve().parents[1]
    assert paths.root == expected_root


def test_discover_uses_frozen_executable_directory(tmp_path, monkeypatch):
    executable = tmp_path / "安装 目录" / "WinSW_GUI.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    paths = AppPaths.discover()

    assert paths.root == executable.parent.resolve()


def test_ensure_runtime_dirs_creates_and_probes_all_directories(tmp_path):
    paths = AppPaths.from_root(tmp_path / "app")

    paths.ensure_runtime_dirs()

    assert paths.root.is_dir()
    assert paths.services_dir.is_dir()
    assert paths.logs_dir.is_dir()
    assert paths.bin_dir.is_dir()
    assert list(paths.root.rglob(".winsw-gui-write-*")) == []


def test_ensure_runtime_dirs_reports_unwritable_directory(tmp_path, monkeypatch):
    paths = AppPaths.from_root(tmp_path)

    def deny_probe(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(app_paths_module.tempfile, "NamedTemporaryFile", deny_probe)

    with pytest.raises(AppPathsError, match=r"无法写入.*") as exc_info:
        paths.ensure_runtime_dirs()

    assert str(paths.root) in str(exc_info.value)


@pytest.mark.parametrize("directory_name", ["services", "logs", "bin"])
def test_runtime_directory_cannot_redirect_outside_portable_root(
    tmp_path, directory_name
):
    root = tmp_path / "app"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    redirected = root / directory_name
    try:
        redirected.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")
    paths = AppPaths.from_root(root)

    with pytest.raises(AppPathsError):
        paths.ensure_runtime_dirs()

    assert list(outside.iterdir()) == []


def test_settings_path_must_not_be_a_directory(tmp_path):
    paths = AppPaths.from_root(tmp_path)
    paths.settings_file.mkdir()

    with pytest.raises(AppPathsError):
        paths.ensure_runtime_dirs()


def test_runtime_symlink_rejection_is_covered_without_os_link_privileges(
    tmp_path, monkeypatch
):
    paths = AppPaths.from_root(tmp_path)
    real_is_symlink = Path.is_symlink

    def simulated_is_symlink(path):
        return path == paths.services_dir or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", simulated_is_symlink)

    with pytest.raises(AppPathsError):
        paths.ensure_runtime_dirs()
