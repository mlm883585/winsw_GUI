import tkinter as tk

import pytest

from gui.service_list_view import ServiceListView


@pytest.fixture
def tk_root():
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


def test_service_list_uses_the_injected_directory(tmp_path, tk_root):
    services_dir = tmp_path / "服务 配置"
    services_dir.mkdir()
    (services_dir / "Bravo.xml").write_text("<service/>", encoding="utf-8")
    (services_dir / "Alpha.xml").write_text("<service/>", encoding="utf-8")
    (services_dir / "ignore.txt").write_text("ignored", encoding="utf-8")

    view = ServiceListView(tk_root, services_dir, lambda _filename: None)

    assert view.listbox.get(0, tk.END) == ("Alpha.xml", "Bravo.xml")


def test_refresh_can_restore_a_specific_selection(tmp_path, tk_root):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    (services_dir / "One.xml").write_text("<service/>", encoding="utf-8")
    (services_dir / "Two.xml").write_text("<service/>", encoding="utf-8")
    view = ServiceListView(tk_root, services_dir, lambda _filename: None)

    restored = view.refresh_list("Two.xml")

    assert restored is True
    assert view.get_selected_filename() == "Two.xml"


def test_select_filename_reports_when_the_item_is_missing(tmp_path, tk_root):
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    view = ServiceListView(tk_root, services_dir, lambda _filename: None)

    assert view.select_filename("Missing.xml") is False
    assert view.get_selected_filename() is None
