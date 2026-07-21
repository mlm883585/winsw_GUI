from gui.tabs.log_viewer_tab import LogViewerTab


def test_configured_log_path_is_used_even_when_it_does_not_exist(tmp_path):
    config_path = tmp_path / "services" / "actual-file.xml"
    configured_log_dir = tmp_path / "future-logs"

    paths = LogViewerTab.resolve_log_paths(
        {"id": "DifferentId", "logpath": str(configured_log_dir)},
        config_path,
    )

    assert paths["wrapper.log"] == configured_log_dir / "actual-file.wrapper.log"
    assert paths["out.log"] == configured_log_dir / "actual-file.out.log"
    assert paths["err.log"] == configured_log_dir / "actual-file.err.log"


def test_missing_log_path_uses_the_config_file_directory(tmp_path):
    config_path = tmp_path / "services" / "service-file.xml"

    paths = LogViewerTab.resolve_log_paths({"id": "Service1"}, config_path)

    assert paths["wrapper.log"] == config_path.parent / "service-file.wrapper.log"


def test_relative_log_path_is_resolved_from_the_config_directory(tmp_path):
    config_path = tmp_path / "services" / "Service1.xml"

    paths = LogViewerTab.resolve_log_paths(
        {"id": "Service1", "logpath": "logs"},
        config_path,
    )

    assert paths["wrapper.log"] == config_path.parent / "logs" / "Service1.wrapper.log"
