import tkinter as tk

import pytest

from gui.tabs.basic_info_tab import BasicInfoTab
from gui.tabs.execution_tab import ExecutionTab


@pytest.fixture
def tk_root():
    root = tk.Tk()
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


def test_description_preserves_intentional_leading_and_trailing_whitespace(tk_root):
    tab = BasicInfoTab(tk_root)
    description = "  first line\nsecond line\n\n"

    tab.set_data({"description": description})

    assert tab.get_data()["description"] == description


def test_arguments_preserve_intentional_leading_and_trailing_whitespace(tk_root):
    tab = ExecutionTab(tk_root, None)
    arguments = '  --name "value"\n--second line\n'

    tab.set_data({"arguments": arguments})

    assert tab.get_data()["arguments"] == arguments
