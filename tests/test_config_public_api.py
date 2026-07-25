import pytest

from core.app_paths import AppPaths
from core.config_manager import ConfigManager, ConfigValidationError


def test_accepts_shared_app_paths_instance(tmp_path):
    paths = AppPaths.from_root(tmp_path)

    manager = ConfigManager(paths)

    assert manager.app_paths is paths
    assert manager.services_dir == paths.services_dir


def test_validate_accepts_a_document_ready_for_a_service_command(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()
    manager.merge_ui_data(
        document,
        {**document.values, "id": "Service1", "executable": "python"},
    )

    assert manager.validate(document) is None


def test_validate_rejects_invalid_data_without_writing(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()
    manager.merge_ui_data(
        document,
        {**document.values, "id": "../outside", "executable": "python"},
    )

    with pytest.raises(ConfigValidationError):
        manager.validate(document)

    assert not (tmp_path / "services").exists()


def test_untouched_new_document_starts_clean_but_becomes_dirty_after_edit(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()

    assert manager.is_dirty(document) is False

    manager.merge_ui_data(document, {**document.values, "id": "Service1"})
    assert manager.is_dirty(document) is True
