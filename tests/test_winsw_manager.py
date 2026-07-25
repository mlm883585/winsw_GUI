from types import SimpleNamespace

from core.winsw_manager import WinSWManager


class StubSettings:
    def __init__(self, values=None):
        self.values = values or {
            "winsw_management_mode": "auto",
            "winsw_custom_path": "",
        }

    def get(self, key):
        return self.values.get(key)


def test_status_uses_the_exact_saved_config_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    services_dir = tmp_path / "services"
    bin_dir.mkdir()
    services_dir.mkdir()
    config_path = services_dir / "filename-does-not-match-id.xml"
    config_path.write_text("<service><id>Service1</id></service>", encoding="utf-8")
    winsw_path = bin_dir / "winsw-x64.exe"
    winsw_path.write_bytes(b"winsw")
    paths = SimpleNamespace(bin_dir=bin_dir, services_dir=services_dir)
    commands = []

    def fake_run(command_parts, **kwargs):
        commands.append((command_parts, kwargs))
        return SimpleNamespace(stdout="Running", stderr="", returncode=0)

    monkeypatch.setattr("core.winsw_manager.subprocess.run", fake_run)
    manager = WinSWManager(lambda _message: None, StubSettings(), paths)

    manager.status(config_path)

    assert commands[0][0] == [str(winsw_path), "status", str(config_path.resolve())]


def test_command_rejects_a_missing_config_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    services_dir = tmp_path / "services"
    bin_dir.mkdir()
    services_dir.mkdir()
    (bin_dir / "winsw-x64.exe").write_bytes(b"winsw")
    paths = SimpleNamespace(bin_dir=bin_dir, services_dir=services_dir)
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("core.winsw_manager.subprocess.run", fake_run)
    manager = WinSWManager(lambda _message: None, StubSettings(), paths)

    result = manager.start(services_dir / "missing.xml")

    assert result is None
    assert called is False


def test_command_rejects_config_outside_services_root(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    services_dir = tmp_path / "services"
    bin_dir.mkdir()
    services_dir.mkdir()
    (bin_dir / "winsw-x64.exe").write_bytes(b"winsw")
    outside = tmp_path / "outside.xml"
    outside.write_text("<service/>", encoding="utf-8")
    paths = SimpleNamespace(bin_dir=bin_dir, services_dir=services_dir)
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("core.winsw_manager.subprocess.run", fake_run)
    manager = WinSWManager(lambda _message: None, StubSettings(), paths)

    result = manager.stop(outside)

    assert result is None
    assert called is False
