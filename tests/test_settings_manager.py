import json
import os

import pytest

import core.settings_manager as settings_module
from core.app_paths import AppPaths
from core.settings_manager import SettingsManager


EXPECTED_DEFAULTS = {
    "winsw_management_mode": "auto",
    "winsw_custom_path": "",
    "window_geometry": "1200x800+100+100",
    "main_sash_pos": 300,
    "right_sash_pos": 500,
}


def test_accepts_shared_app_paths_instance(tmp_path):
    paths = AppPaths.from_root(tmp_path)

    manager = SettingsManager(paths)

    assert manager.app_paths is paths
    assert manager.settings_file == paths.settings_file


@pytest.mark.parametrize("contents", [None, "", "   ", "{broken", "[]"])
def test_missing_empty_or_damaged_file_uses_defaults(tmp_path, contents):
    settings_file = tmp_path / "settings.json"
    if contents is not None:
        settings_file.write_text(contents, encoding="utf-8")

    manager = SettingsManager(settings_file)

    assert manager.settings_file == settings_file
    assert manager.settings == EXPECTED_DEFAULTS
    if contents is None:
        assert not settings_file.exists()


def test_load_merges_defaults_and_preserves_additional_settings(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"window_geometry": "800x600", "future_option": True}),
        encoding="utf-8",
    )

    manager = SettingsManager(settings_file)

    assert manager.get("window_geometry") == "800x600"
    assert manager.get("winsw_management_mode") == "auto"
    assert manager.get("future_option") is True


def test_save_is_atomic_and_fsyncs_temp_file(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"window_geometry": "old"}', encoding="utf-8")
    manager = SettingsManager(settings_file)
    manager.set("window_geometry", "900x700")
    replace_call = {}
    fsync_calls = []
    real_replace = os.replace
    real_fsync = os.fsync

    def tracking_replace(source, destination):
        replace_call["source"] = source
        replace_call["destination"] = destination
        return real_replace(source, destination)

    def tracking_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(settings_module.os, "replace", tracking_replace)
    monkeypatch.setattr(settings_module.os, "fsync", tracking_fsync)

    manager.save_settings()

    assert fsync_calls
    assert replace_call["source"].parent == settings_file.parent
    assert replace_call["destination"] == settings_file
    assert json.loads(settings_file.read_text(encoding="utf-8"))[
        "window_geometry"
    ] == "900x700"
    assert list(tmp_path.iterdir()) == [settings_file]


def test_replace_failure_cleans_temp_and_preserves_original(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    original = b'{"window_geometry": "old"}'
    settings_file.write_bytes(original)
    manager = SettingsManager(settings_file)
    manager.set("window_geometry", "new")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(settings_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        manager.save_settings()

    assert settings_file.read_bytes() == original
    assert list(tmp_path.iterdir()) == [settings_file]


def test_write_failure_cleans_temp_and_preserves_original(tmp_path):
    settings_file = tmp_path / "settings.json"
    original = b'{"window_geometry": "old"}'
    settings_file.write_bytes(original)
    manager = SettingsManager(settings_file)
    manager.set("not_json_serializable", object())

    with pytest.raises(TypeError):
        manager.save_settings()

    assert settings_file.read_bytes() == original
    assert list(tmp_path.iterdir()) == [settings_file]


def test_fsync_failure_cleans_temp_and_preserves_original(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    original = b'{"window_geometry": "old"}'
    settings_file.write_bytes(original)
    manager = SettingsManager(settings_file)
    manager.set("window_geometry", "new")

    def fail_fsync(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(settings_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        manager.save_settings()

    assert settings_file.read_bytes() == original
    assert list(tmp_path.iterdir()) == [settings_file]
