import sys
import tkinter as tk
from copy import deepcopy

import pytest

from core.app_paths import AppPaths
from core.settings_manager import SettingsManager
from gui.main_window import MainWindow


@pytest.fixture
def window_factory(tmp_path):
    roots = []
    old_stdout, old_stderr = sys.stdout, sys.stderr

    def create():
        paths = AppPaths.from_root(tmp_path)
        paths.ensure_runtime_dirs()
        root = tk.Tk()
        root.withdraw()
        roots.append(root)
        settings = SettingsManager(paths)
        window = MainWindow(root, settings, "test", paths)
        return window, paths

    try:
        yield create
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        for root in roots:
            try:
                root.destroy()
            except tk.TclError:
                pass


def test_initial_document_defaults_are_loaded_into_the_ui(window_factory):
    window, _paths = window_factory()

    assert window.logging_tab.log_mode_var.get() == "roll"
    assert window.recovery_tab.reset_var.get() == "1 day"
    assert window.advanced_tab.priority_var.get() == "normal"
    assert window.advanced_tab.stop_timeout_var.get() == "15 sec"
    assert window.account_tab.account_type_var.get() == "Local System"
    assert window.xml_editor_tab.is_dirty() is False


def test_initial_form_round_trip_does_not_create_false_dirty_state(window_factory):
    window, _paths = window_factory()

    document = window._document_from_ui()

    assert window.config_manager.is_dirty(document) is False


def test_save_synchronizes_document_list_xml_and_log_monitor(window_factory):
    window, paths = window_factory()
    window.basic_info_tab.id_var.set("Service1")
    window.execution_tab.executable_var.set("python")

    saved = window.save_service()

    expected_path = paths.services_dir / "Service1.xml"
    assert saved is True
    assert expected_path.is_file()
    assert window.current_document.source_path == expected_path.resolve()
    assert window.service_list.get_selected_filename() == "Service1.xml"
    assert window.xml_editor_tab.is_dirty() is False
    assert window.log_viewer_tab.current_config_path == expected_path.resolve()
    window.log_viewer_tab.stop_monitoring()


def test_cancelling_a_service_command_does_not_save_or_execute(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    window.basic_info_tab.id_var.set("Service1")
    window.execution_tab.executable_var.set("python")
    executed = []
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesno", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(window.winsw_manager, "start", executed.append)

    window.start_service()

    assert list(paths.services_dir.glob("*.xml")) == []
    assert executed == []


def test_cancelling_dirty_navigation_preserves_current_edits(
    window_factory, monkeypatch
):
    window, _paths = window_factory()
    window.basic_info_tab.id_var.set("UnsavedService")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: None
    )

    changed = window.new_service()

    assert changed is False
    assert window.basic_info_tab.id_var.get() == "UnsavedService"


def test_request_close_honors_navigation_cancellation(window_factory, monkeypatch):
    window, paths = window_factory()
    monkeypatch.setattr(window, "_confirm_navigation", lambda: False)

    assert window.request_close() is False
    assert not paths.settings_file.exists()


def test_request_close_saves_window_settings(window_factory, monkeypatch):
    window, paths = window_factory()
    monkeypatch.setattr(window, "_confirm_navigation", lambda: True)

    assert window.request_close() is True
    assert paths.settings_file.is_file()


def test_import_opens_an_unsaved_copy_without_touching_the_origin(
    window_factory, monkeypatch, tmp_path
):
    window, paths = window_factory()
    source = tmp_path / "external.xml"
    original = (
        b"<service><id>Imported1</id><executable>python</executable>"
        b"<startmode>Manual</startmode></service>"
    )
    source.write_bytes(original)
    monkeypatch.setattr(
        "gui.main_window.filedialog.askopenfilename", lambda **_kwargs: str(source)
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an untouched new document must not prompt")
        ),
    )

    assert window.import_service_xml() is True
    assert source.read_bytes() == original
    assert list(paths.services_dir.glob("*.xml")) == []
    assert window.current_document.source_path is None
    assert window.current_document.origin_path == source.resolve()
    assert window.current_document.root.find("startmode").text == "Manual"
    assert window.config_manager.is_dirty(window.current_document) is True


def test_new_document_clears_previous_log_context(window_factory, monkeypatch):
    window, _paths = window_factory()
    window.basic_info_tab.id_var.set("Service1")
    window.execution_tab.executable_var.set("python")
    assert window.save_service() is True
    log_file = next(iter(window.log_viewer_tab.log_paths.values()))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        "gui.tabs.log_viewer_tab.messagebox.showwarning",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "gui.tabs.log_viewer_tab.messagebox.askyesno",
        lambda *_args, **_kwargs: True,
    )

    assert window.new_service() is True
    window.log_viewer_tab.clear_logs()

    assert window.log_viewer_tab.current_config is None
    assert window.log_viewer_tab.current_config_path is None
    assert window.log_viewer_tab.log_paths == {}
    assert log_file.read_text(encoding="utf-8") == "keep"


def test_failed_selection_after_save_restores_newly_saved_selection(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    (paths.services_dir / "A.xml").write_text(
        "<service><id>A</id><executable>python</executable></service>",
        encoding="utf-8",
    )
    (paths.services_dir / "Broken.xml").write_text(
        "<service><id>Broken</service>", encoding="utf-8"
    )
    window.service_list.refresh_list("A.xml")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("loading from a clean document must not prompt")
        ),
    )
    assert window.on_service_selected("A.xml") is True
    window.basic_info_tab.id_var.set("B")
    window.service_list.select_filename("Broken.xml")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.showerror", lambda *_args, **_kwargs: None
    )

    assert window.on_service_selected("Broken.xml") is False

    assert window.current_document.source_path == (
        paths.services_dir / "B.xml"
    ).resolve()
    assert window._previous_selected_filename == "B.xml"
    assert window.service_list.get_selected_filename() == "B.xml"


def test_discarded_form_edits_stay_discarded_when_target_load_fails(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    (paths.services_dir / "A.xml").write_text(
        "<service><id>A</id><executable>python</executable></service>",
        encoding="utf-8",
    )
    (paths.services_dir / "Broken.xml").write_text(
        "<service><id>Broken</service>", encoding="utf-8"
    )
    window.service_list.refresh_list("A.xml")
    assert window.on_service_selected("A.xml") is True
    window.basic_info_tab.desc_text.insert("1.0", "unsaved")
    window.service_list.select_filename("Broken.xml")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.showerror", lambda *_args, **_kwargs: None
    )

    assert window.on_service_selected("Broken.xml") is False

    assert window.service_list.get_selected_filename() == "A.xml"
    assert window.basic_info_tab.desc_text.get("1.0", "end-1c") == ""
    assert window.config_manager.is_dirty(window._document_from_ui()) is False


def test_save_failure_prevents_service_command_execution(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    original_xml = window.config_manager.to_xml_string(window.current_document)
    original_values = deepcopy(window.current_document.current_values)
    window.basic_info_tab.id_var.set("Service1")
    window.execution_tab.executable_var.set("python")
    executed = []
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesno", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.showerror", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        window.config_manager,
        "save",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(window.winsw_manager, "start", executed.append)

    window.start_service()

    assert executed == []
    assert list(paths.services_dir.glob("*.xml")) == []
    assert window.current_document.source_path is None
    assert window.config_manager.to_xml_string(window.current_document) == original_xml
    assert window.current_document.current_values == original_values


def test_builtin_account_round_trip_preserves_password_and_logon_flag(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    source = paths.services_dir / "Service1.xml"
    source.write_text(
        """<service><id>Service1</id><executable>python</executable>
<serviceaccount><username>LocalSystem</username><password>secret</password>
<allowservicelogon>true</allowservicelogon></serviceaccount></service>""",
        encoding="utf-8",
    )
    window.service_list.refresh_list("Service1.xml")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("clean navigation must not prompt")
        ),
    )
    assert window.on_service_selected("Service1.xml") is True

    candidate = window._document_from_ui()
    assert window.config_manager.is_dirty(candidate) is False
    assert window.save_service() is True

    saved = source.read_text(encoding="utf-8")
    assert "<password>secret</password>" in saved
    assert "<allowservicelogon>true</allowservicelogon>" in saved


def test_builtin_account_round_trip_preserves_original_username_casing(
    window_factory
):
    window, paths = window_factory()
    source = paths.services_dir / "Service1.xml"
    source.write_text(
        """<service><id>Service1</id><executable>python</executable>
<serviceaccount><username>localsystem</username></serviceaccount></service>""",
        encoding="utf-8",
    )
    window.service_list.refresh_list("Service1.xml")
    assert window.on_service_selected("Service1.xml") is True

    candidate = window._document_from_ui()

    assert window.config_manager.is_dirty(candidate) is False
    assert candidate.root.findtext("serviceaccount/username") == "localsystem"


@pytest.mark.parametrize("username_xml", ["", "<username />"])
def test_untouched_empty_account_username_preserves_other_credentials(
    window_factory, username_xml
):
    window, paths = window_factory()
    source = paths.services_dir / "Service1.xml"
    source.write_text(
        f"""<service><id>Service1</id><executable>python</executable>
<serviceaccount>{username_xml}<password>secret</password>
<allowservicelogon>true</allowservicelogon></serviceaccount></service>""",
        encoding="utf-8",
    )
    window.service_list.refresh_list("Service1.xml")
    assert window.on_service_selected("Service1.xml") is True

    candidate = window._document_from_ui()

    assert window.config_manager.is_dirty(candidate) is False
    assert candidate.root.findtext("serviceaccount/password") == "secret"
    assert candidate.root.findtext("serviceaccount/allowservicelogon") == "true"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("my-service.exe", "myservice"),
        ("worker_v2.exe", "workerv2"),
        ("中文.exe", "Service"),
        ("CON.exe", "CONService"),
    ],
)
def test_executable_autofill_generates_valid_service_id(
    window_factory, filename, expected
):
    window, _paths = window_factory()

    window.autofill_from_executable(filename)

    assert window.basic_info_tab.id_var.get() == expected


def replace_xml_draft(window, xml_text):
    window.xml_editor_tab.text_widget.delete("1.0", tk.END)
    window.xml_editor_tab.text_widget.insert("1.0", xml_text)


def test_xml_draft_apply_updates_document_and_form(window_factory, monkeypatch):
    window, paths = window_factory()
    replace_xml_draft(
        window,
        """<service><id>Xml1</id><executable>python</executable>
<future keep="yes" /></service>""",
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: True
    )

    assert window._resolve_xml_draft() is True

    assert window.basic_info_tab.id_var.get() == "Xml1"
    assert window.current_document.root.find("future").get("keep") == "yes"
    assert window.xml_editor_tab.is_dirty() is False
    assert list(paths.services_dir.glob("*.xml")) == []


def test_xml_draft_discard_restores_form_xml(window_factory, monkeypatch):
    window, _paths = window_factory()
    expected = window.xml_editor_tab.get_xml_text()
    replace_xml_draft(window, "<not-service />")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: False
    )

    assert window._resolve_xml_draft() is True

    assert window.xml_editor_tab.get_xml_text() == expected
    assert window.xml_editor_tab.is_dirty() is False


def test_xml_draft_cancel_keeps_draft_and_blocks_navigation(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    draft = "<service><id>Draft1</id></service>"
    replace_xml_draft(window, draft)
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: None
    )

    assert window.new_service() is False

    assert window.xml_editor_tab.get_xml_text() == draft
    assert window.xml_editor_tab.is_dirty() is True
    assert list(paths.services_dir.glob("*.xml")) == []


def test_invalid_xml_draft_stays_dirty_and_does_not_change_document(
    window_factory, monkeypatch
):
    window, _paths = window_factory()
    original = window.config_manager.to_xml_string(window.current_document)
    replace_xml_draft(window, "<service>")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.showerror", lambda *_args, **_kwargs: None
    )

    assert window._resolve_xml_draft() is False

    assert window.config_manager.to_xml_string(window.current_document) == original
    assert window.xml_editor_tab.is_dirty() is True


@pytest.mark.parametrize(
    ("decision", "expected_result", "expected_saved"),
    [(True, True, True), (False, True, False), (None, False, False)],
)
def test_form_dirty_guard_save_discard_and_cancel(
    window_factory, monkeypatch, decision, expected_result, expected_saved
):
    window, paths = window_factory()
    window.basic_info_tab.id_var.set("Guard1")
    window.execution_tab.executable_var.set("python")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel",
        lambda *_args, **_kwargs: decision,
    )

    assert window._confirm_navigation() is expected_result

    assert (paths.services_dir / "Guard1.xml").exists() is expected_saved
    if expected_saved:
        window.log_viewer_tab.stop_monitoring()


def test_cancelled_list_switch_restores_previous_selection(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    for service_id in ("A", "B"):
        (paths.services_dir / f"{service_id}.xml").write_text(
            f"<service><id>{service_id}</id><executable>python</executable></service>",
            encoding="utf-8",
        )
    window.service_list.refresh_list("A.xml")
    assert window.on_service_selected("A.xml") is True
    window.basic_info_tab.desc_text.insert("1.0", "unsaved")
    window.service_list.select_filename("B.xml")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: None
    )

    assert window.on_service_selected("B.xml") is False

    assert window.service_list.get_selected_filename() == "A.xml"
    assert window.current_document.source_path == (paths.services_dir / "A.xml").resolve()
    assert window.basic_info_tab.desc_text.get("1.0", "end-1c") == "unsaved"


def test_dirty_import_cancel_keeps_current_form_and_source(
    window_factory, monkeypatch, tmp_path
):
    window, paths = window_factory()
    external = tmp_path / "external.xml"
    original = b"<service><id>External1</id><executable>x</executable></service>"
    external.write_bytes(original)
    window.basic_info_tab.id_var.set("Unsaved1")
    monkeypatch.setattr(
        "gui.main_window.filedialog.askopenfilename", lambda **_kwargs: str(external)
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: None
    )

    assert window.import_service_xml() is False

    assert window.basic_info_tab.id_var.get() == "Unsaved1"
    assert external.read_bytes() == original
    assert list(paths.services_dir.glob("*.xml")) == []


def test_importing_a_managed_file_cannot_overwrite_its_origin(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    source = paths.services_dir / "Imported1.xml"
    original = (
        b"<service><id>Imported1</id><executable>old</executable></service>"
    )
    source.write_bytes(original)
    monkeypatch.setattr(
        "gui.main_window.filedialog.askopenfilename", lambda **_kwargs: str(source)
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.showerror", lambda *_args, **_kwargs: None
    )
    assert window.import_service_xml() is True
    window.execution_tab.executable_var.set("new")

    assert window.save_service() is False

    assert source.read_bytes() == original
    assert window.current_document.source_path is None
    assert window.current_document.origin_path == source.resolve()


def test_dirty_delete_cancel_preserves_configuration(window_factory, monkeypatch):
    window, paths = window_factory()
    source = paths.services_dir / "A.xml"
    source.write_text(
        "<service><id>A</id><executable>python</executable></service>",
        encoding="utf-8",
    )
    window.service_list.refresh_list("A.xml")
    assert window.on_service_selected("A.xml") is True
    window.basic_info_tab.desc_text.insert("1.0", "unsaved")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: None
    )

    assert window.delete_service_config() is False

    assert source.exists()
    assert window.service_list.get_selected_filename() == "A.xml"


def test_gui_conflict_reject_then_accept(window_factory, monkeypatch):
    window, paths = window_factory()
    target = paths.services_dir / "Service1.xml"
    original = b"<service><id>Service1</id><executable>old</executable></service>"
    target.write_bytes(original)
    window.basic_info_tab.id_var.set("Service1")
    window.execution_tab.executable_var.set("new")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesno", lambda *_args, **_kwargs: False
    )

    assert window.save_service() is False
    assert target.read_bytes() == original

    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesno", lambda *_args, **_kwargs: True
    )
    assert window.save_service() is True
    assert "new" in target.read_text(encoding="utf-8")
    window.log_viewer_tab.stop_monitoring()


def test_xml_cancelled_service_command_has_no_disk_or_command_side_effects(
    window_factory, monkeypatch
):
    window, paths = window_factory()
    replace_xml_draft(
        window,
        "<service><id>Xml1</id><executable>python</executable></service>",
    )
    executed = []
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesnocancel", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(window.winsw_manager, "start", executed.append)

    window.start_service()

    assert executed == []
    assert list(paths.services_dir.glob("*.xml")) == []


def test_clearing_custom_username_explicitly_removes_account_credentials(
    window_factory
):
    window, paths = window_factory()
    source = paths.services_dir / "Custom1.xml"
    source.write_text(
        """<service><id>Custom1</id><executable>python</executable>
<serviceaccount><username>domain\\user</username>
<password>secret</password><allowservicelogon>true</allowservicelogon>
</serviceaccount></service>""",
        encoding="utf-8",
    )
    window.service_list.refresh_list("Custom1.xml")
    assert window.on_service_selected("Custom1.xml") is True
    assert window.account_tab.account_type_var.get() == "Custom"
    window.account_tab.username_var.set("")

    candidate = window._document_from_ui()

    assert candidate.root.find("serviceaccount") is None
    assert "secret" not in window.config_manager.to_xml_string(candidate)


def test_delete_rejects_services_directory_redirected_after_startup(
    window_factory, monkeypatch, tmp_path
):
    window, paths = window_factory()
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "A.xml"
    external.write_text(
        "<service><id>A</id><executable>x</executable></service>",
        encoding="utf-8",
    )
    paths.services_dir.rmdir()
    try:
        paths.services_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")
    window.service_list.refresh_list("A.xml")
    monkeypatch.setattr(
        "gui.main_window.messagebox.askyesno", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(
        "gui.main_window.messagebox.showerror", lambda *_args, **_kwargs: None
    )

    assert window.delete_service_config() is False

    assert external.exists()
