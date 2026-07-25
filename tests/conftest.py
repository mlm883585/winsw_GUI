"""Global test isolation guards."""

import subprocess
from tkinter import filedialog, messagebox

import pytest
import requests


@pytest.fixture(autouse=True)
def block_external_side_effects(monkeypatch):
    """Fail fast on accidental dialogs, network access, or real commands."""

    def blocked(kind):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"unexpected external side effect: {kind}")

        return fail

    for name in (
        "askyesno",
        "askyesnocancel",
        "showerror",
        "showinfo",
        "showwarning",
    ):
        monkeypatch.setattr(messagebox, name, blocked(f"messagebox.{name}"))
    for name in ("askopenfilename", "askdirectory"):
        monkeypatch.setattr(filedialog, name, blocked(f"filedialog.{name}"))
    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        blocked("network request"),
    )
    monkeypatch.setattr(subprocess, "run", blocked("subprocess.run"))
