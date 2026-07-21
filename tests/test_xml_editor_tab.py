import tkinter as tk

import pytest

from gui.tabs.xml_editor_tab import XmlEditorTab


@pytest.fixture
def tk_root():
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


def make_tab(root, xml_text="<service><id>Service1</id></service>"):
    callbacks = {
        "get_config": lambda: {"id": "Service1"},
        "set_config": lambda _config: None,
        "to_xml_string": lambda _config: xml_text,
        "from_xml_string": lambda _text: {"id": "Service1"},
    }
    return XmlEditorTab(root, callbacks)


def test_loaded_xml_is_clean_and_text_edits_are_dirty(tk_root):
    tab = make_tab(tk_root)

    tab.load_from_ui()
    assert tab.is_dirty() is False

    tab.text_widget.insert(tk.END, "\n<!-- draft -->")
    assert tab.is_dirty() is True


def test_reloading_xml_discards_the_draft_and_marks_it_clean(tk_root):
    tab = make_tab(tk_root)
    tab.load_from_ui()
    tab.text_widget.insert(tk.END, "\n<!-- draft -->")

    tab.load_from_ui()

    assert tab.get_xml_text() == "<service><id>Service1</id></service>"
    assert tab.is_dirty() is False


def test_mark_clean_accepts_the_current_xml_as_the_new_baseline(tk_root):
    tab = make_tab(tk_root)
    tab.load_from_ui()
    tab.text_widget.insert(tk.END, "\n<!-- applied -->")

    tab.mark_clean()

    assert tab.is_dirty() is False
