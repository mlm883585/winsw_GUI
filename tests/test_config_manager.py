from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path

import pytest

import core.config_manager as config_module
from core.config_manager import (
    ConfigConflictError,
    ConfigManager,
    ManagedPathError,
)


def child_text(root: ET.Element, tag: str) -> str | None:
    element = root.find(tag)
    return None if element is None else element.text


def test_new_document_materializes_project_defaults(tmp_path):
    manager = ConfigManager(tmp_path / "services")

    document = manager.new_document()

    assert document.root.tag == "service"
    assert document.current_values["log_mode"] == "roll"
    assert child_text(document.root, "resetfailure") == "1 day"
    assert child_text(document.root, "priority") == "normal"
    assert child_text(document.root, "stoptimeout") == "15 sec"
    assert child_text(document.root.find("serviceaccount"), "username") == "LocalSystem"
    assert document.root.find("log").get("mode") == "roll"
    assert document.source_path is None
    assert document.origin_path is None
    assert not manager.is_dirty(document)


def test_load_managed_uses_effective_defaults_without_materializing_nodes(tmp_path):
    services = tmp_path / "services"
    services.mkdir()
    source = services / "odd-name.xml"
    source.write_text(
        "<service><id>Demo1</id><executable>python</executable></service>",
        encoding="utf-8",
    )
    manager = ConfigManager(services)

    document = manager.load_managed(source)

    assert document.source_path == source.resolve()
    assert document.origin_path is None
    assert document.current_values["log_mode"] == "append"
    assert document.current_values["resetfailure"] == "1 day"
    assert document.current_values["priority"] == "normal"
    assert document.current_values["stoptimeout"] == "15 sec"
    assert document.current_values["serviceaccount"] == {"username": "LocalSystem"}
    assert document.root.find("log") is None
    assert document.root.find("resetfailure") is None
    assert not manager.is_dirty(document)


def test_load_external_tracks_read_only_origin_as_unsaved(tmp_path):
    services = tmp_path / "services"
    external = tmp_path / "incoming.xml"
    external.write_text(
        "<service><id>Imported1</id><executable>%PYTHON%</executable></service>",
        encoding="utf-8",
    )
    manager = ConfigManager(services)

    document = manager.load_external(external)

    assert document.source_path is None
    assert document.origin_path == external.resolve()
    assert manager.is_dirty(document)


def test_parser_preserves_comments_processing_instructions_and_text(tmp_path):
    services = tmp_path / "services"
    services.mkdir()
    source = services / "demo.xml"
    source.write_text(
        """<?xml version="1.0"?>
<service flavor="custom">
  <?vendor keep-this?>
  <!-- before id -->
  <id>Demo1</id>
  <executable>python</executable>
  <arguments>  -m app --label="two words"  </arguments>
  <vendor-option enabled="yes"><nested /></vendor-option>
</service>
""",
        encoding="utf-8",
    )
    manager = ConfigManager(services)

    document = manager.load_managed(source)
    rendered = manager.to_xml_string(document)

    assert document.current_values["arguments"] == '  -m app --label="two words"  '
    assert document.root.get("flavor") == "custom"
    assert "<?vendor keep-this?>" in rendered
    assert "<!-- before id -->" in rendered
    assert '<vendor-option enabled="yes">' in rendered
    assert rendered.index("<?vendor") < rendered.index("<!-- before id -->")
    assert rendered.index("<!-- before id -->") < rendered.index("<id>")
    assert rendered.index("<id>") < rendered.index("<vendor-option")


def test_interactive_false_and_empty_account_boolean_are_safe(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()

    manager.apply_xml(
        document,
        """<service>
  <id>Demo1</id><executable>python</executable>
  <interactive>false</interactive>
  <serviceaccount><username>user</username><allowservicelogon /></serviceaccount>
</service>""",
    )

    assert document.current_values["interactive"] is False
    assert document.current_values["serviceaccount"]["allowservicelogon"] is False


@pytest.mark.parametrize(
    "xml_text",
    [
        "<!DOCTYPE service><service><id>A</id><executable>x</executable></service>",
        "<!DOCTYPE service [<!ENTITY x 'boom'>]><service><id>A</id><executable>&x;</executable></service>",
        "<wrapper><id>A</id><executable>x</executable></wrapper>",
        "<service><id>A</service>",
        "<service><id>A</id><id>B</id><executable>x</executable></service>",
        "<service><id>A</id><executable>x</executable><log/><log/></service>",
        "<service><id>A</id><executable>x</executable><serviceaccount><username>a</username><username>b</username></serviceaccount></service>",
    ],
    ids=["doctype", "entity", "wrong-root", "malformed", "duplicate-id", "duplicate-log", "duplicate-account-field"],
)
def test_apply_xml_rejects_unsafe_or_ambiguous_documents(tmp_path, xml_text):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()
    original = manager.to_xml_string(document)

    with pytest.raises(ValueError):
        manager.apply_xml(document, xml_text)

    assert manager.to_xml_string(document) == original


def test_repeated_env_and_onfailure_nodes_are_valid(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()

    manager.apply_xml(
        document,
        """<service><id>A1</id><executable>x</executable>
<env name="A" value="1"/><env name="A" value="2"/>
<onfailure action="restart"/><onfailure action="none"/>
</service>""",
    )

    assert document.current_values["environments"] == [
        {"name": "A", "value": "1"},
        {"name": "A", "value": "2"},
    ]
    assert document.current_values["onfailure"] == [
        {"action": "restart", "delay": ""},
        {"action": "none", "delay": ""},
    ]


def write_managed(services, name: str, xml_text: str):
    services.mkdir(parents=True, exist_ok=True)
    path = services / name
    path.write_text(xml_text, encoding="utf-8")
    return path


def test_merging_unchanged_effective_defaults_does_not_add_nodes(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        "<service><id>Demo1</id><executable>python</executable></service>",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(document, dict(document.current_values))

    assert document.root.find("log") is None
    assert document.root.find("resetfailure") is None
    assert document.root.find("priority") is None
    assert document.root.find("stoptimeout") is None
    assert document.root.find("serviceaccount") is None
    assert not manager.is_dirty(document)


def test_merge_simple_fields_updates_in_place_and_empty_removes(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        """<service>
  <id keep="yes">Demo1</id>
  <!-- marker -->
  <vendor enabled="yes" />
  <description language="zh">old</description>
  <executable>python</executable>
  <arguments>  -m app  </arguments>
</service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(
        document,
        {"name": "Display name", "description": "", "executable": "%PYTHON%"},
    )

    assert document.root.find("id").get("keep") == "yes"
    assert document.root.findtext("id") == "Demo1"
    assert document.root.findtext("name") == "Display name"
    assert document.root.find("description") is None
    assert document.root.findtext("executable") == "%PYTHON%"
    assert document.root.findtext("arguments") == "  -m app  "
    rendered = manager.to_xml_string(document)
    assert "<!-- marker -->" in rendered
    assert '<vendor enabled="yes"' in rendered
    assert rendered.index("<id") < rendered.index("<!-- marker -->")
    assert rendered.index("<!-- marker -->") < rendered.index("<vendor")
    assert document.current_values["name"] == "Display name"
    assert manager.is_dirty(document)


def test_merge_log_changes_only_mode_and_preserves_advanced_configuration(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        """<service><id>Demo1</id><executable>x</executable>
<log mode="roll-by-size" custom="keep">
  <sizeThreshold>10240</sizeThreshold><keepFiles>7</keepFiles>
</log><vendor />
</service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(document, {"log_mode": "reset"})

    log = document.root.find("log")
    assert log.get("mode") == "reset"
    assert log.get("custom") == "keep"
    assert log.findtext("sizeThreshold") == "10240"
    assert log.findtext("keepFiles") == "7"
    assert document.root[-1].tag == "vendor"


def test_merge_env_matches_name_and_occurrence_and_preserves_extra_attributes(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        """<service><id>Demo1</id><executable>x</executable>
<env name="A" value="1" slot="first"/><vendor/>
<env name="A" value="2" slot="second"/><env name="B" value="3" secret="keep"/>
</service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(
        document,
        {
            "environments": [
                {"name": "A", "value": "10"},
                {"name": "A", "value": "20"},
                {"name": "C", "value": "4"},
            ]
        },
    )

    envs = document.root.findall("env")
    assert [(node.get("name"), node.get("value")) for node in envs] == [
        ("A", "10"),
        ("A", "20"),
        ("C", "4"),
    ]
    assert envs[0].get("slot") == "first"
    assert envs[1].get("slot") == "second"
    assert document.root.find("vendor") is not None
    children = list(document.root)
    assert children.index(document.root.find("vendor")) > children.index(envs[0])
    assert children.index(document.root.find("vendor")) < children.index(envs[1])


def test_merge_onfailure_matches_by_position_and_preserves_extra_attributes(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        """<service><id>Demo1</id><executable>x</executable>
<onfailure action="restart" delay="1 sec" slot="first"/><vendor/>
<onfailure action="none" slot="second"/>
</service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(
        document,
        {
            "onfailure": [
                {"action": "none", "delay": ""},
                {"action": "restart", "delay": "9 sec"},
                {"action": "reboot", "delay": ""},
            ]
        },
    )

    actions = document.root.findall("onfailure")
    assert [(node.get("action"), node.get("delay")) for node in actions] == [
        ("none", None),
        ("restart", "9 sec"),
        ("reboot", None),
    ]
    assert actions[0].get("slot") == "first"
    assert actions[1].get("slot") == "second"
    assert document.root.find("vendor") is not None


def test_merge_account_updates_known_children_only(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        """<service><id>Demo1</id><executable>x</executable>
<serviceaccount custom="keep">
  <username domain="legacy">old</username>
  <domain enabled="yes">KEEP</domain>
  <password encrypted="no">secret</password>
  <allowservicelogon grant="custom" />
</serviceaccount></service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(
        document,
        {
            "serviceaccount": {
                "username": "new-user",
                "password": "",
                "allowservicelogon": True,
            }
        },
    )

    account = document.root.find("serviceaccount")
    assert account.get("custom") == "keep"
    assert account.findtext("username") == "new-user"
    assert account.find("username").get("domain") == "legacy"
    assert account.find("password") is None
    assert account.findtext("domain") == "KEEP"
    assert account.find("domain").get("enabled") == "yes"
    assert account.findtext("allowservicelogon") == "true"
    assert account.find("allowservicelogon").get("grant") == "custom"


def test_reverting_merged_value_returns_loaded_document_to_clean_state(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        "<service><id>Demo1</id><executable>x</executable></service>",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(document, {"executable": "python"})
    assert manager.is_dirty(document)

    manager.merge_ui_data(document, {"executable": "x"})
    assert not manager.is_dirty(document)


def test_reverting_effective_defaults_removes_nodes_absent_from_baseline(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "demo.xml",
        "<service><id>Demo1</id><executable>x</executable></service>",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(
        document,
        {
            "resetfailure": "2 days",
            "log_mode": "roll",
            "serviceaccount": {"username": "custom-user"},
        },
    )
    manager.merge_ui_data(
        document,
        {
            "resetfailure": "1 day",
            "log_mode": "append",
            "serviceaccount": {"username": "LocalSystem"},
        },
    )

    assert document.root.find("resetfailure") is None
    assert document.root.find("log") is None
    assert document.root.find("serviceaccount") is None
    assert not manager.is_dirty(document)


@pytest.mark.parametrize(
    "service_id",
    ["", "contains-dash", "two words", "服务", "CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT9"],
)
def test_save_rejects_invalid_or_reserved_service_ids(tmp_path, service_id):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()
    manager.merge_ui_data(document, {"id": service_id, "executable": "python"})

    with pytest.raises(ValueError) as exc_info:
        manager.save(document)

    assert type(exc_info.value).__name__ == "ConfigValidationError"
    assert not (tmp_path / "services").exists()


@pytest.mark.parametrize("service_id", ["A", "Demo123", "COM10", "CONSOLE"])
@pytest.mark.parametrize("executable", ["python", "%PYTHON%", r"C:\Program Files\Python\python.exe"])
def test_save_accepts_ascii_alphanumeric_ids_and_nonempty_executables(
    tmp_path, service_id, executable
):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()
    manager.merge_ui_data(document, {"id": service_id, "executable": executable})

    saved_path = manager.save(document)

    assert saved_path == (tmp_path / "services" / f"{service_id}.xml").resolve()
    assert saved_path.exists()


@pytest.mark.parametrize("executable", ["", " ", "\t\r\n"])
def test_save_rejects_blank_executable(tmp_path, executable):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()
    manager.merge_ui_data(document, {"id": "Demo1", "executable": executable})

    with pytest.raises(ValueError) as exc_info:
        manager.save(document)

    assert type(exc_info.value).__name__ == "ConfigValidationError"


@pytest.mark.parametrize("candidate_kind", ["outside", "parent", "nested", "non-xml"])
def test_load_managed_rejects_paths_outside_flat_services_directory(tmp_path, candidate_kind):
    services = tmp_path / "services"
    services.mkdir()
    if candidate_kind == "outside":
        candidate = tmp_path / "outside.xml"
    elif candidate_kind == "parent":
        candidate = services / ".." / "outside.xml"
    elif candidate_kind == "nested":
        (services / "nested").mkdir()
        candidate = services / "nested" / "demo.xml"
    else:
        candidate = services / "demo.txt"
    candidate.resolve().write_text(
        "<service><id>Demo1</id><executable>x</executable></service>",
        encoding="utf-8",
    )
    manager = ConfigManager(services)

    with pytest.raises(ValueError) as exc_info:
        manager.load_managed(candidate)

    assert type(exc_info.value).__name__ == "ManagedPathError"


def test_save_is_atomic_and_marks_document_persisted(tmp_path):
    manager = ConfigManager(tmp_path / "中文 space" / "services")
    document = manager.new_document()
    manager.merge_ui_data(
        document,
        {"id": "中文目录1", "executable": "python"},
    )
    # The path may contain non-ASCII characters, while the service ID may not.
    manager.merge_ui_data(document, {"id": "Demo1"})

    saved_path = manager.save(document)

    raw = saved_path.read_bytes()
    assert raw.startswith(b"<?xml")
    assert b"\r\n" not in raw
    assert document.source_path == saved_path
    assert document.origin_path is None
    assert document.baseline_xml == manager.to_xml_string(document)
    assert document.baseline_values == document.current_values
    assert document.baseline_values is not document.current_values
    assert not manager.is_dirty(document)
    assert not list(saved_path.parent.glob(f".{saved_path.name}.*.tmp"))


def test_unchanged_id_saves_back_to_original_mismatched_filename(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "legacy-name.xml",
        "<service><id>Demo1</id><executable>x</executable></service>",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.merge_ui_data(document, {"executable": "python"})

    saved_path = manager.save(document)

    assert saved_path == source.resolve()
    assert not (services / "Demo1.xml").exists()
    assert "python" in source.read_text(encoding="utf-8")


def test_changed_id_saves_new_file_and_preserves_old_file(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Old1.xml",
        "<service><id>Old1</id><executable>x</executable></service>",
    )
    original_bytes = source.read_bytes()
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.merge_ui_data(document, {"id": "New1", "executable": "python"})

    saved_path = manager.save(document)

    assert saved_path == (services / "New1.xml").resolve()
    assert source.read_bytes() == original_bytes
    assert saved_path.exists()
    assert document.source_path == saved_path
    assert not manager.is_dirty(document)


def test_case_only_id_change_saves_original_file_in_place(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "OddFile.xml",
        "<service><id>Demo1</id><executable>x</executable></service>",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.merge_ui_data(document, {"id": "demo1"})

    saved_path = manager.save(document)

    assert saved_path == source.resolve()
    assert len(list(services.glob("*.xml"))) == 1
    assert document.root.findtext("id") == "demo1"


def test_case_insensitive_conflict_requires_explicit_overwrite(tmp_path):
    services = tmp_path / "services"
    existing = write_managed(
        services,
        "DEMO1.XML",
        "<service><id>Other1</id><executable>old</executable></service>",
    )
    original_bytes = existing.read_bytes()
    manager = ConfigManager(services)
    document = manager.new_document()
    manager.merge_ui_data(document, {"id": "demo1", "executable": "new"})

    with pytest.raises(Exception) as exc_info:
        manager.save(document)

    assert type(exc_info.value).__name__ == "ConfigConflictError"
    assert existing.read_bytes() == original_bytes
    assert document.source_path is None

    saved_path = manager.save(document, allow_overwrite=True)
    assert saved_path == existing.resolve()
    assert len(list(services.iterdir())) == 1
    assert "new" in existing.read_text(encoding="utf-8")


def test_external_import_save_never_modifies_origin(tmp_path):
    external = tmp_path / "incoming.xml"
    external.write_text(
        "<service><id>Imported1</id><executable>x</executable></service>",
        encoding="utf-8",
    )
    original_bytes = external.read_bytes()
    manager = ConfigManager(tmp_path / "services")
    document = manager.load_external(external)
    manager.merge_ui_data(document, {"executable": "python"})

    saved_path = manager.save(document)

    assert external.read_bytes() == original_bytes
    assert document.origin_path == external.resolve()
    assert document.source_path == saved_path
    assert saved_path == (tmp_path / "services" / "Imported1.xml").resolve()


def snapshot_document(document):
    return {
        "root": ET.tostring(document.root, encoding="utf-8"),
        "source_path": document.source_path,
        "origin_path": document.origin_path,
        "baseline_xml": document.baseline_xml,
        "baseline_values": deepcopy(document.baseline_values),
        "current_values": deepcopy(document.current_values),
        "leading_nodes": [
            ET.tostring(node, encoding="utf-8") for node in document.leading_nodes
        ],
        "trailing_nodes": [
            ET.tostring(node, encoding="utf-8") for node in document.trailing_nodes
        ],
        "is_unsaved": document.is_unsaved,
    }


def assert_document_snapshot(document, snapshot):
    assert ET.tostring(document.root, encoding="utf-8") == snapshot["root"]
    assert document.source_path == snapshot["source_path"]
    assert document.origin_path == snapshot["origin_path"]
    assert document.baseline_xml == snapshot["baseline_xml"]
    assert document.baseline_values == snapshot["baseline_values"]
    assert document.current_values == snapshot["current_values"]
    assert [
        ET.tostring(node, encoding="utf-8") for node in document.leading_nodes
    ] == snapshot["leading_nodes"]
    assert [
        ET.tostring(node, encoding="utf-8") for node in document.trailing_nodes
    ] == snapshot["trailing_nodes"]
    assert document.is_unsaved == snapshot["is_unsaved"]


def test_replace_failure_preserves_file_and_document_state(tmp_path, monkeypatch):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        "<service><id>Demo1</id><executable>old</executable></service>",
    )
    original_bytes = source.read_bytes()
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.merge_ui_data(document, {"executable": "new"})
    snapshot = snapshot_document(document)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        manager.save(document)

    assert source.read_bytes() == original_bytes
    assert_document_snapshot(document, snapshot)
    assert not list(services.glob(".*.tmp"))


class FailingFile:
    def __init__(self, wrapped, operation):
        self.wrapped = wrapped
        self.operation = operation

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.wrapped.close()

    def write(self, data):
        if self.operation == "write":
            raise OSError("write failed")
        return self.wrapped.write(data)

    def flush(self):
        if self.operation == "flush":
            raise OSError("flush failed")
        return self.wrapped.flush()

    def fileno(self):
        return self.wrapped.fileno()


@pytest.mark.parametrize("operation", ["write", "flush"])
def test_write_or_flush_failure_preserves_file_and_state(
    tmp_path, monkeypatch, operation
):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        "<service><id>Demo1</id><executable>old</executable></service>",
    )
    original_bytes = source.read_bytes()
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.merge_ui_data(document, {"executable": "new"})
    snapshot = snapshot_document(document)
    real_fdopen = os.fdopen

    def failing_fdopen(fd, *args, **kwargs):
        return FailingFile(real_fdopen(fd, *args, **kwargs), operation)

    monkeypatch.setattr(config_module.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match=f"{operation} failed"):
        manager.save(document)

    assert source.read_bytes() == original_bytes
    assert_document_snapshot(document, snapshot)
    assert not list(services.glob(".*.tmp"))


def test_fsync_failure_preserves_file_and_state(tmp_path, monkeypatch):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        "<service><id>Demo1</id><executable>old</executable></service>",
    )
    original_bytes = source.read_bytes()
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.merge_ui_data(document, {"executable": "new"})
    snapshot = snapshot_document(document)

    def fail_fsync(_fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(config_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        manager.save(document)

    assert source.read_bytes() == original_bytes
    assert_document_snapshot(document, snapshot)
    assert not list(services.glob(".*.tmp"))


def test_legacy_xml_string_loader_uses_safe_lossless_parser(tmp_path):
    manager = ConfigManager(tmp_path / "services")

    values = manager.load_from_xml_string(
        """<service><id>Demo1</id><executable>x</executable>
<arguments>  keep surrounding spaces  </arguments>
<interactive>false</interactive></service>"""
    )

    assert values["arguments"] == "  keep surrounding spaces  "
    assert values["interactive"] is False
    assert values["log_mode"] == "append"


def test_legacy_file_loader_does_not_hide_invalid_xml(tmp_path):
    source = tmp_path / "broken.xml"
    source.write_text("<service><id>broken</service>", encoding="utf-8")
    manager = ConfigManager(tmp_path / "services")

    with pytest.raises(ValueError) as exc_info:
        manager.load_from_xml(source)

    assert type(exc_info.value).__name__ == "ConfigParseError"


def test_noop_merge_keeps_new_document_clean(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    document = manager.new_document()

    manager.merge_ui_data(document, deepcopy(document.current_values))

    assert not manager.is_dirty(document)


def test_creating_account_container_writes_effective_username(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        "<service><id>Demo1</id><executable>x</executable></service>",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(
        document,
        {
            "serviceaccount": {
                "username": "LocalSystem",
                "allowservicelogon": True,
            }
        },
    )

    account = document.root.find("serviceaccount")
    assert account is not None
    assert account.findtext("username") == "LocalSystem"
    assert account.findtext("allowservicelogon") == "true"


def test_save_rejects_managed_symlink_that_escapes_services(tmp_path):
    services = tmp_path / "services"
    services.mkdir()
    external = tmp_path / "outside.xml"
    external.write_text(
        "<service><id>Outside1</id><executable>old</executable></service>",
        encoding="utf-8",
    )
    original = external.read_bytes()
    link = services / "Demo1.xml"
    try:
        link.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    manager = ConfigManager(services)
    document = manager.new_document()
    manager.merge_ui_data(document, {"id": "Demo1", "executable": "new"})

    with pytest.raises(ManagedPathError):
        manager.save(document, allow_overwrite=True)

    assert external.read_bytes() == original
    assert document.source_path is None


def test_managed_symlink_rejection_is_covered_without_os_link_privileges(
    tmp_path, monkeypatch
):
    services = tmp_path / "services"
    services.mkdir()
    candidate = services / "Demo1.xml"
    candidate.write_text(
        "<service><id>Other1</id><executable>old</executable></service>",
        encoding="utf-8",
    )
    real_is_symlink = Path.is_symlink

    def simulated_is_symlink(path):
        return path == candidate or real_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", simulated_is_symlink)
    manager = ConfigManager(services)
    document = manager.new_document()
    manager.merge_ui_data(document, {"id": "Demo1", "executable": "new"})

    with pytest.raises(ManagedPathError):
        manager.save(document, allow_overwrite=True)


def test_external_import_cannot_overwrite_its_origin_inside_services(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        "<service><id>Demo1</id><executable>old</executable></service>",
    )
    original = source.read_bytes()
    manager = ConfigManager(services)
    document = manager.load_external(source)
    manager.merge_ui_data(document, {"executable": "new"})

    with pytest.raises(ConfigConflictError):
        manager.save(document, allow_overwrite=True)

    assert source.read_bytes() == original
    assert document.source_path is None
    assert document.origin_path == source.resolve()


def test_id_change_cannot_overwrite_the_original_mismatched_filename(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "New1.xml",
        "<service><id>Old1</id><executable>old</executable></service>",
    )
    original = source.read_bytes()
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.merge_ui_data(document, {"id": "New1", "executable": "new"})

    with pytest.raises(ConfigConflictError):
        manager.save(document, allow_overwrite=True)

    assert source.read_bytes() == original
    assert document.source_path == source.resolve()


def test_utf16_dtd_and_entity_are_rejected(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    xml = """<?xml version="1.0" encoding="utf-16"?>
<!DOCTYPE service [<!ENTITY payload "EXPANDED">]>
<service><id>Demo1</id><executable>&payload;</executable></service>"""

    with pytest.raises(ValueError) as exc_info:
        manager._parse_xml(xml.encode("utf-16"))

    assert type(exc_info.value).__name__ == "ConfigParseError"


def test_declaration_text_in_comments_and_cdata_is_not_rejected(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    source = tmp_path / "safe.xml"
    source.write_text(
        """<?before keep?>
<!-- <!DOCTYPE is only comment text> -->
<service><id>Demo1</id><executable>x</executable>
<arguments><![CDATA[<!ENTITY is only text>]]></arguments></service>
<!--after root-->
""",
        encoding="utf-8",
    )
    document = manager.load_external(source)

    serialized = manager.to_xml_string(document)

    assert "<?before keep?>" in serialized
    assert "<!-- <!DOCTYPE is only comment text> -->" in serialized
    assert "&lt;!ENTITY is only text&gt;" in serialized
    assert "<!--after root-->" in serialized


def test_reverting_explicit_false_interactive_restores_original_node(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        """<service><id>Demo1</id><executable>x</executable>
<interactive custom="keep">false</interactive></service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(document, {"interactive": True})
    manager.merge_ui_data(document, {"interactive": False})

    interactive = document.root.find("interactive")
    assert interactive is not None
    assert interactive.text == "false"
    assert interactive.get("custom") == "keep"
    assert not manager.is_dirty(document)


def test_reverting_effective_log_mode_restores_mode_absence(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        """<service><id>Demo1</id><executable>x</executable>
<log custom="keep"><sizeThreshold>10</sizeThreshold></log></service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(document, {"log_mode": "roll"})
    manager.merge_ui_data(document, {"log_mode": "append"})

    log = document.root.find("log")
    assert log is not None
    assert "mode" not in log.attrib
    assert log.get("custom") == "keep"
    assert log.findtext("sizeThreshold") == "10"
    assert not manager.is_dirty(document)


def test_legacy_save_api_rejects_paths_outside_services(tmp_path):
    manager = ConfigManager(tmp_path / "services")
    outside = tmp_path / "outside.xml"

    with pytest.raises(ManagedPathError):
        manager.save_to_xml(
            {"id": "Demo1", "executable": "python"}, str(outside)
        )

    assert not outside.exists()


def test_external_import_reverts_against_source_baseline_without_becoming_saved(
    tmp_path
):
    source = tmp_path / "external.xml"
    original = """<service><id>Imported1</id><executable>x</executable>
<description custom="keep">old</description>
<interactive custom="keep">false</interactive>
<log custom="keep"><sizeThreshold>10</sizeThreshold></log></service>"""
    source.write_text(original, encoding="utf-8")
    manager = ConfigManager(tmp_path / "services")
    document = manager.load_external(source)

    manager.merge_ui_data(
        document,
        {"description": "new", "interactive": True, "log_mode": "roll"},
    )
    manager.merge_ui_data(
        document,
        {"description": "old", "interactive": False, "log_mode": "append"},
    )

    assert document.root.find("description").get("custom") == "keep"
    assert document.root.find("interactive").get("custom") == "keep"
    assert document.root.findtext("interactive") == "false"
    log = document.root.find("log")
    assert log.get("custom") == "keep"
    assert "mode" not in log.attrib
    assert log.findtext("sizeThreshold") == "10"
    assert manager.is_dirty(document) is True
    assert document.source_path is None


def test_concurrent_target_creation_requires_a_new_explicit_overwrite(tmp_path):
    services = tmp_path / "services"
    manager = ConfigManager(services)
    document = manager.new_document()
    manager.merge_ui_data(document, {"id": "Demo1", "executable": "new"})
    target = services / "Demo1.xml"
    real_find = manager._find_case_insensitive_match
    injected = False

    def create_after_conflict_check(filename, exclude=None):
        nonlocal injected
        result = real_find(filename, exclude=exclude)
        if result is None and exclude is None and not injected:
            injected = True
            target.write_text(
                "<service><id>Demo1</id><executable>other</executable></service>",
                encoding="utf-8",
            )
        return result

    manager._find_case_insensitive_match = create_after_conflict_check

    with pytest.raises(ConfigConflictError):
        manager.save(document)

    assert "other" in target.read_text(encoding="utf-8")
    assert document.source_path is None

    saved = manager.save(document, allow_overwrite=True)
    assert saved == target.resolve()
    assert "new" in target.read_text(encoding="utf-8")


def test_form_revert_after_xml_apply_preserves_new_unknown_metadata(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        """<service><id>Demo1</id><description>old</description>
<executable>x</executable><log /></service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.apply_xml(
        document,
        """<service><id>Demo1</id><description custom="keep">new</description>
<executable>x</executable>
<log custom="keep" mode="roll"><future>yes</future></log></service>""",
    )

    manager.merge_ui_data(document, {"description": "old", "log_mode": "append"})

    description = document.root.find("description")
    assert description.text == "old"
    assert description.get("custom") == "keep"
    log = document.root.find("log")
    assert "mode" not in log.attrib
    assert log.get("custom") == "keep"
    assert log.findtext("future") == "yes"


def test_setting_boolean_false_preserves_unknown_attributes(tmp_path):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        """<service><id>Demo1</id><executable>x</executable>
<interactive custom="keep">true</interactive>
<serviceaccount><username>user</username>
<allowservicelogon custom="keep">true</allowservicelogon>
</serviceaccount></service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)

    manager.merge_ui_data(
        document,
        {
            "interactive": False,
            "serviceaccount": {"allowservicelogon": False},
        },
    )

    interactive = document.root.find("interactive")
    assert interactive is not None
    assert interactive.text == "false"
    assert interactive.get("custom") == "keep"
    allow = document.root.find("serviceaccount/allowservicelogon")
    assert allow is not None
    assert allow.text == "false"
    assert allow.get("custom") == "keep"


def test_reverting_xml_added_account_boolean_preserves_unknown_attributes(
    tmp_path
):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        """<service><id>Demo1</id><executable>x</executable>
<serviceaccount><username>user</username></serviceaccount></service>""",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.apply_xml(
        document,
        """<service><id>Demo1</id><executable>x</executable>
<serviceaccount><username>user</username>
<allowservicelogon custom="keep">true</allowservicelogon>
</serviceaccount></service>""",
    )

    manager.merge_ui_data(
        document, {"serviceaccount": {"allowservicelogon": False}}
    )

    allow = document.root.find("serviceaccount/allowservicelogon")
    assert allow is not None
    assert allow.text == "false"
    assert allow.get("custom") == "keep"


def test_reverting_xml_added_account_from_implicit_default_keeps_metadata(
    tmp_path
):
    services = tmp_path / "services"
    source = write_managed(
        services,
        "Demo1.xml",
        "<service><id>Demo1</id><executable>x</executable></service>",
    )
    manager = ConfigManager(services)
    document = manager.load_managed(source)
    manager.apply_xml(
        document,
        """<service><id>Demo1</id><executable>x</executable>
<serviceaccount><username custom="keep">LocalSystem</username>
<allowservicelogon custom="keep">true</allowservicelogon>
</serviceaccount></service>""",
    )

    manager.merge_ui_data(
        document, {"serviceaccount": {"allowservicelogon": False}}
    )

    account = document.root.find("serviceaccount")
    assert account is not None
    assert account.find("username").get("custom") == "keep"
    allow = account.find("allowservicelogon")
    assert allow is not None
    assert allow.text == "false"
    assert allow.get("custom") == "keep"
