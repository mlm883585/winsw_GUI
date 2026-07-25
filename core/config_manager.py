import os
import re
import tempfile
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.dom import Node, minidom
from xml.parsers import expat

from core.app_paths import AppPaths


class ConfigError(Exception):
    """Base class for configuration lifecycle failures."""


class ConfigParseError(ConfigError, ValueError):
    """Raised when XML is unsafe, malformed, or structurally ambiguous."""


class ConfigValidationError(ConfigError, ValueError):
    """Raised when modeled values cannot form a valid WinSW configuration."""


class ManagedPathError(ConfigError, ValueError):
    """Raised when a managed file escapes the flat services directory."""


class ConfigConflictError(ConfigError, FileExistsError):
    """Raised when a Windows-case-insensitive target already exists."""

    def __init__(self, target: Path, existing: Path, *, overwritable: bool = True):
        self.target = target
        self.existing = existing
        self.overwritable = overwritable
        super().__init__(f"Configuration target already exists: {existing}")


@dataclass
class ConfigDocument:
    """An editable WinSW XML document and its persisted state."""

    root: ET.Element
    current_values: dict
    source_path: Optional[Path] = None
    origin_path: Optional[Path] = None
    baseline_xml: Optional[str] = None
    baseline_values: dict = field(default_factory=dict)
    leading_nodes: list[ET.Element] = field(default_factory=list)
    trailing_nodes: list[ET.Element] = field(default_factory=list)
    is_unsaved: bool = False

    @property
    def values(self) -> dict:
        """Short alias used by UI integrations."""
        return self.current_values


class ConfigManager:
    """
    负责加载、保存和管理WinSW的XML配置。
    """
    # 将logpath移到SIMPLE_TAGS中，作为顶级元素处理
    SIMPLE_TAGS = [
        'id', 'name', 'description', 'executable', 'arguments',
        'workingdirectory', 'resetfailure', 'priority', 'stoptimeout',
        'logpath'
    ]
    ELEMENT_ORDER = {
        "id": 10,
        "name": 20,
        "description": 30,
        "executable": 40,
        "arguments": 50,
        "workingdirectory": 60,
        "env": 70,
        "logpath": 80,
        "log": 90,
        "onfailure": 100,
        "resetfailure": 110,
        "serviceaccount": 120,
        "priority": 130,
        "stoptimeout": 140,
        "interactive": 150,
    }
    RESERVED_SERVICE_IDS = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}"
        for prefix in ("COM", "LPT")
        for number in range(1, 10)
    }

    def __init__(self, app_paths_or_services_dir):
        if isinstance(app_paths_or_services_dir, AppPaths):
            self.app_paths = app_paths_or_services_dir
            services_dir = app_paths_or_services_dir.services_dir
        else:
            self.app_paths = None
            services_dir = app_paths_or_services_dir
        self.services_dir = Path(services_dir).resolve()

    def new_document(self) -> ConfigDocument:
        values = self.get_default_config()
        root = self._to_xml_root(values)
        values = self._values_from_root(root)
        document = ConfigDocument(
            root=root,
            current_values=deepcopy(values),
            baseline_values=deepcopy(values),
        )
        document.baseline_xml = self.to_xml_string(document)
        return document

    def is_dirty(self, document: ConfigDocument) -> bool:
        if document.baseline_xml is None:
            return True
        return (
            document.is_unsaved
            or self.to_xml_string(document) != document.baseline_xml
            or document.current_values != document.baseline_values
        )

    def load_managed(self, file_path) -> ConfigDocument:
        path = self._managed_source_path(file_path)
        root, leading_nodes, trailing_nodes = self._parse_document(path.read_bytes())
        values = self._values_from_root(root)
        document = ConfigDocument(
            root=root,
            current_values=values,
            source_path=path,
            baseline_values=deepcopy(values),
            leading_nodes=leading_nodes,
            trailing_nodes=trailing_nodes,
        )
        document.baseline_xml = self.to_xml_string(document)
        return document

    def load_external(self, file_path) -> ConfigDocument:
        path = Path(file_path).resolve(strict=True)
        root, leading_nodes, trailing_nodes = self._parse_document(path.read_bytes())
        values = self._values_from_root(root)
        document = ConfigDocument(
            root=root,
            current_values=values,
            origin_path=path,
            baseline_values=deepcopy(values),
            leading_nodes=leading_nodes,
            trailing_nodes=trailing_nodes,
            is_unsaved=True,
        )
        document.baseline_xml = self.to_xml_string(document)
        return document

    def apply_xml(self, document: ConfigDocument, xml_text) -> ConfigDocument:
        root, leading_nodes, trailing_nodes = self._parse_document(xml_text)
        values = self._values_from_root(root)
        document.root = root
        document.leading_nodes = leading_nodes
        document.trailing_nodes = trailing_nodes
        document.current_values = values
        return document

    def merge_ui_data(self, document: ConfigDocument, ui_data: dict) -> ConfigDocument:
        """Merge modeled UI values without rebuilding the XML document."""
        old_values = document.current_values
        baseline_root = (
            self._parse_xml(document.baseline_xml)
            if document.baseline_xml is not None
            else None
        )

        for tag in self.SIMPLE_TAGS:
            if tag not in ui_data or ui_data[tag] == old_values.get(tag, ""):
                continue
            if (
                baseline_root is not None
                and ui_data[tag] == document.baseline_values.get(tag, "")
            ):
                self._restore_simple_value(
                    document.root, baseline_root, tag, ui_data[tag]
                )
            else:
                self._set_simple_element(document.root, tag, ui_data[tag])

        if (
            "interactive" in ui_data
            and bool(ui_data["interactive"]) != bool(old_values.get("interactive", False))
        ):
            if (
                baseline_root is not None
                and bool(ui_data["interactive"])
                == bool(document.baseline_values.get("interactive", False))
            ):
                self._restore_interactive(
                    document.root, baseline_root, bool(ui_data["interactive"])
                )
            else:
                interactive = document.root.find("interactive")
                if ui_data["interactive"]:
                    if interactive is None:
                        interactive = ET.Element("interactive")
                        self._insert_known(document.root, interactive)
                    interactive.text = "true"
                elif interactive is not None:
                    if interactive.attrib or list(interactive):
                        interactive.text = "false"
                    else:
                        document.root.remove(interactive)

        if "log_mode" in ui_data and ui_data["log_mode"] != old_values.get("log_mode"):
            if (
                baseline_root is not None
                and ui_data["log_mode"]
                == document.baseline_values.get("log_mode")
            ):
                self._restore_log_mode(
                    document.root, baseline_root, str(ui_data["log_mode"] or "")
                )
            else:
                log = document.root.find("log")
                if log is None:
                    log = ET.Element("log")
                    self._insert_known(document.root, log)
                mode = str(ui_data["log_mode"] or "")
                if mode:
                    log.set("mode", mode)
                else:
                    log.attrib.pop("mode", None)

        if (
            "environments" in ui_data
            and ui_data["environments"] != old_values.get("environments", [])
        ):
            self._merge_environments(document.root, ui_data["environments"])

        if "onfailure" in ui_data and ui_data["onfailure"] != old_values.get("onfailure", []):
            self._merge_onfailure(document.root, ui_data["onfailure"])

        if "serviceaccount" in ui_data:
            old_account = old_values.get("serviceaccount", {})
            desired_account = deepcopy(old_account)
            desired_account.update(ui_data["serviceaccount"] or {})
            if self._normalized_account(desired_account) != self._normalized_account(
                old_account
            ):
                if (
                    baseline_root is not None
                    and self._normalized_account(desired_account)
                    == self._normalized_account(
                        document.baseline_values.get("serviceaccount", {})
                    )
                ):
                    self._restore_service_account(
                        document.root, baseline_root, desired_account
                    )
                else:
                    self._merge_service_account(
                        document.root, old_account, desired_account
                    )

        document.current_values = self._values_from_root(document.root)
        return document

    def save(self, document: ConfigDocument, allow_overwrite: bool = False) -> Path:
        """Atomically persist a document within ``services_dir``."""
        self._validate_for_save(document)
        service_id = document.current_values["id"]
        baseline_id = document.baseline_values.get("id", "")
        id_changed = (
            document.source_path is not None
            and service_id.casefold() != str(baseline_id).casefold()
        )

        self.services_dir.mkdir(parents=True, exist_ok=True)
        if (
            document.source_path is not None
            and service_id.casefold() == str(baseline_id).casefold()
        ):
            target = self._validate_managed_path(document.source_path, must_exist=True)
            conflict = self._find_case_insensitive_match(target.name, exclude=target)
        else:
            requested_target = self.services_dir / f"{service_id}.xml"
            conflict = self._find_case_insensitive_match(requested_target.name)
            target = conflict or self._validate_managed_path(
                requested_target, must_exist=False
            )

        protected_paths = []
        if document.origin_path is not None:
            protected_paths.append(document.origin_path)
        if id_changed and document.source_path is not None:
            protected_paths.append(document.source_path)
        for protected_path in protected_paths:
            if self._paths_refer_to_same_file(target, protected_path):
                raise ConfigConflictError(
                    self.services_dir / f"{service_id}.xml",
                    Path(protected_path),
                    overwritable=False,
                )

        if conflict is not None and not allow_overwrite:
            raise ConfigConflictError(
                (self.services_dir / f"{service_id}.xml").resolve(), conflict
            )

        xml_text = self.to_xml_string(document)
        replace_existing = (
            conflict is not None
            or (document.source_path is not None and not id_changed)
        )
        try:
            self._atomic_write(
                target,
                xml_text.encode("utf-8"),
                replace_existing=replace_existing,
            )
        except FileExistsError as exc:
            existing = self._find_case_insensitive_match(target.name) or target
            raise ConfigConflictError(
                self.services_dir / f"{service_id}.xml", existing
            ) from exc

        persisted_path = target.resolve(strict=True)
        document.source_path = persisted_path
        document.current_values = self._values_from_root(document.root)
        document.baseline_xml = xml_text
        document.baseline_values = deepcopy(document.current_values)
        document.is_unsaved = False
        return persisted_path

    def validate(self, document: ConfigDocument) -> None:
        """Validate a document without creating directories or writing files."""
        self._validate_for_save(document)

    def revert(self, document: ConfigDocument) -> ConfigDocument:
        """Restore the in-memory document to its persisted/imported baseline."""
        if document.baseline_xml is None:
            raise ConfigError("Configuration document has no baseline to restore")
        root, leading_nodes, trailing_nodes = self._parse_document(
            document.baseline_xml
        )
        document.root = root
        document.leading_nodes = leading_nodes
        document.trailing_nodes = trailing_nodes
        document.current_values = deepcopy(document.baseline_values)
        return document

    def to_xml_string(self, document: ConfigDocument) -> str:
        root = deepcopy(document.root)
        ET.indent(root, space="  ")
        root_xml = ET.tostring(
            root,
            encoding="unicode",
            short_empty_elements=True,
        )
        parts = ["<?xml version='1.0' encoding='utf-8'?>"]
        parts.extend(
            ET.tostring(node, encoding="unicode")
            for node in document.leading_nodes
        )
        parts.append(root_xml)
        parts.extend(
            ET.tostring(node, encoding="unicode")
            for node in document.trailing_nodes
        )
        xml = "\n".join(parts)
        return xml.replace("\r\n", "\n").replace("\r", "\n") + "\n"

    def _managed_source_path(self, file_path) -> Path:
        path = Path(file_path)
        if not path.is_absolute():
            path = self.services_dir / path
        return self._validate_managed_path(path, must_exist=True)

    def _validate_managed_path(self, path, must_exist: bool) -> Path:
        candidate = Path(path)
        if candidate.is_symlink():
            raise ManagedPathError(
                f"Managed configurations cannot be symbolic links: {candidate}"
            )
        try:
            resolved = candidate.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise ManagedPathError(f"Invalid managed path: {candidate}") from exc

        if resolved.parent != self.services_dir:
            raise ManagedPathError(
                f"Managed configurations must be direct children of {self.services_dir}"
            )
        if resolved.suffix.casefold() != ".xml":
            raise ManagedPathError("Managed configurations must use the .xml extension")
        if must_exist and not resolved.is_file():
            raise ManagedPathError(f"Managed configuration is not a file: {resolved}")
        return resolved

    def _validate_for_save(self, document: ConfigDocument) -> None:
        service_id = str(document.current_values.get("id", ""))
        self.validate_service_id(service_id)
        executable = str(document.current_values.get("executable", ""))
        if not executable.strip():
            raise ConfigValidationError("Executable must not be blank")

    @classmethod
    def validate_service_id(cls, service_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9]+", service_id):
            raise ConfigValidationError(
                "Service ID must contain ASCII letters and digits only"
            )
        if service_id.upper() in cls.RESERVED_SERVICE_IDS:
            raise ConfigValidationError(
                f"Service ID is a reserved Windows device name: {service_id}"
            )

    @classmethod
    def is_valid_service_id(cls, service_id: str) -> bool:
        try:
            cls.validate_service_id(service_id)
            return True
        except ConfigValidationError:
            return False

    def _find_case_insensitive_match(self, filename: str, exclude=None):
        if not self.services_dir.exists():
            return None
        excluded = (
            self._validate_managed_path(exclude, must_exist=True)
            if exclude is not None
            else None
        )
        for candidate in self.services_dir.iterdir():
            if (
                candidate.name.casefold() != filename.casefold()
                or candidate.suffix.casefold() != ".xml"
            ):
                continue
            resolved = self._validate_managed_path(candidate, must_exist=True)
            if (
                excluded is not None
                and self._paths_refer_to_same_file(resolved, excluded)
            ):
                continue
            return resolved
        return None

    @staticmethod
    def _paths_refer_to_same_file(first, second) -> bool:
        first_path = Path(first)
        second_path = Path(second)
        try:
            return os.path.samefile(first_path, second_path)
        except (FileNotFoundError, OSError):
            first_key = os.path.normcase(str(first_path.resolve(strict=False)))
            second_key = os.path.normcase(str(second_path.resolve(strict=False)))
            return first_key.casefold() == second_key.casefold()

    @staticmethod
    def _atomic_write(
        target: Path, data: bytes, *, replace_existing: bool
    ) -> None:
        fd = None
        temp_path = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
            temp_path = Path(temp_name)
            stream = os.fdopen(fd, "wb")
            fd = None
            with stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace_existing:
                os.replace(temp_path, target)
                temp_path = None
            elif os.name == "nt":
                os.rename(temp_path, target)
                temp_path = None
            else:
                os.link(temp_path, target)
                temp_path.unlink()
                temp_path = None
        finally:
            if fd is not None:
                os.close(fd)
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def _set_simple_element(self, root: ET.Element, tag: str, value) -> None:
        element = root.find(tag)
        text = "" if value is None else str(value)
        if not text:
            if element is not None:
                root.remove(element)
            return
        if element is None:
            element = ET.Element(tag)
            self._insert_known(root, element)
        element.text = text

    @staticmethod
    def _restore_singleton(
        root: ET.Element, baseline_root: ET.Element, tag: str
    ) -> None:
        current = root.find(tag)
        baseline = baseline_root.find(tag)
        if current is not None:
            index = list(root).index(current)
            root.remove(current)
        elif baseline is not None:
            index = min(list(baseline_root).index(baseline), len(root))
        else:
            return
        if baseline is not None:
            root.insert(index, deepcopy(baseline))

    def _restore_simple_value(
        self,
        root: ET.Element,
        baseline_root: ET.Element,
        tag: str,
        desired_value,
    ) -> None:
        current = root.find(tag)
        baseline = baseline_root.find(tag)
        if baseline is not None:
            if current is None:
                self._restore_singleton(root, baseline_root, tag)
            else:
                current.text = baseline.text
            return
        if current is None:
            return
        text = "" if desired_value is None else str(desired_value)
        if not text or (not current.attrib and not list(current)):
            root.remove(current)
        else:
            current.text = text

    def _restore_interactive(
        self,
        root: ET.Element,
        baseline_root: ET.Element,
        desired_value: bool,
    ) -> None:
        current = root.find("interactive")
        baseline = baseline_root.find("interactive")
        if baseline is not None:
            if current is None:
                self._restore_singleton(root, baseline_root, "interactive")
            else:
                current.text = baseline.text
            return
        if current is None:
            return
        if current.attrib or list(current):
            current.text = "true" if desired_value else "false"
        else:
            root.remove(current)

    def _restore_log_mode(
        self,
        root: ET.Element,
        baseline_root: ET.Element,
        desired_mode: str,
    ) -> None:
        current = root.find("log")
        baseline = baseline_root.find("log")
        if baseline is not None:
            if current is None:
                self._restore_singleton(root, baseline_root, "log")
            elif "mode" in baseline.attrib:
                current.set("mode", baseline.get("mode", ""))
            else:
                current.attrib.pop("mode", None)
            return
        if current is None:
            return
        has_unknown_content = bool(
            {name for name in current.attrib if name != "mode"} or list(current)
        )
        if has_unknown_content:
            if desired_mode and desired_mode != "append":
                current.set("mode", desired_mode)
            else:
                current.attrib.pop("mode", None)
        else:
            root.remove(current)

    def _restore_service_account(
        self,
        root: ET.Element,
        baseline_root: ET.Element,
        desired_account: dict,
    ) -> None:
        current = root.find("serviceaccount")
        baseline = baseline_root.find("serviceaccount")
        modeled_tags = ("username", "password", "allowservicelogon")
        modeled_tag_set = set(modeled_tags)
        if baseline is not None:
            if current is None:
                self._restore_singleton(root, baseline_root, "serviceaccount")
                return
            for tag in modeled_tags:
                current_child = current.find(tag)
                baseline_child = baseline.find(tag)
                if baseline_child is None:
                    if current_child is not None:
                        if (
                            tag == "allowservicelogon"
                            and (current_child.attrib or list(current_child))
                        ):
                            current_child.text = "false"
                        else:
                            current.remove(current_child)
                elif current_child is None:
                    index = min(list(baseline).index(baseline_child), len(current))
                    current.insert(index, deepcopy(baseline_child))
                else:
                    current_child.text = baseline_child.text
            return

        if current is None:
            return
        has_unknown_content = bool(current.attrib) or any(
            child.tag not in modeled_tag_set
            or bool(child.attrib)
            or bool(list(child))
            for child in current
        )
        if not has_unknown_content:
            root.remove(current)
            return
        self._set_account_child(
            current, "username", str(desired_account.get("username", ""))
        )
        self._set_account_child(current, "password", "")
        allow_logon = current.find("allowservicelogon")
        if allow_logon is not None and (allow_logon.attrib or list(allow_logon)):
            allow_logon.text = "false"
        else:
            self._set_account_child(current, "allowservicelogon", "")

    @staticmethod
    def _normalized_account(account: dict) -> tuple:
        return (
            str(account.get("username", "")),
            str(account.get("password", "")),
            bool(account.get("allowservicelogon", False)),
        )

    def _insert_known(self, parent: ET.Element, element: ET.Element) -> None:
        desired_order = self.ELEMENT_ORDER.get(element.tag, 10_000)
        for index, child in enumerate(parent):
            if not isinstance(child.tag, str):
                continue
            child_order = self.ELEMENT_ORDER.get(child.tag)
            if child_order is not None and child_order > desired_order:
                parent.insert(index, element)
                return
        parent.append(element)

    @staticmethod
    def _insert_after_last(parent: ET.Element, element: ET.Element, tag: str) -> None:
        positions = [index for index, child in enumerate(parent) if child.tag == tag]
        if positions:
            parent.insert(positions[-1] + 1, element)
        else:
            parent.append(element)

    def _merge_environments(self, root: ET.Element, desired_items) -> None:
        existing = root.findall("env")
        by_name = {}
        for element in existing:
            by_name.setdefault(element.get("name", ""), []).append(element)

        used = set()
        occurrence = {}
        for item in desired_items or []:
            name = str(item.get("name", ""))
            position = occurrence.get(name, 0)
            occurrence[name] = position + 1
            matches = by_name.get(name, [])
            if position < len(matches):
                element = matches[position]
            else:
                element = ET.Element("env")
                self._insert_after_last(root, element, "env")
            used.add(element)
            element.set("name", name)
            element.set("value", str(item.get("value", "")))

        for element in existing:
            if element not in used:
                root.remove(element)

    def _merge_onfailure(self, root: ET.Element, desired_items) -> None:
        existing = root.findall("onfailure")
        desired_items = list(desired_items or [])
        for index, item in enumerate(desired_items):
            if index < len(existing):
                element = existing[index]
            else:
                element = ET.Element("onfailure")
                self._insert_after_last(root, element, "onfailure")
            action = str(item.get("action", ""))
            delay = str(item.get("delay", ""))
            if action:
                element.set("action", action)
            else:
                element.attrib.pop("action", None)
            if delay:
                element.set("delay", delay)
            else:
                element.attrib.pop("delay", None)

        for element in existing[len(desired_items):]:
            root.remove(element)

    def _merge_service_account(self, root, old_account, desired_account) -> None:
        account = root.find("serviceaccount")
        desired = {
            "username": str(desired_account.get("username", "")),
            "password": str(desired_account.get("password", "")),
            "allowservicelogon": bool(desired_account.get("allowservicelogon", False)),
        }
        old = {
            "username": str(old_account.get("username", "")),
            "password": str(old_account.get("password", "")),
            "allowservicelogon": bool(old_account.get("allowservicelogon", False)),
        }
        created = account is None and desired != old
        if created:
            account = ET.Element("serviceaccount")
            self._insert_known(root, account)
        if account is None:
            return

        if created:
            self._set_account_child(account, "username", desired["username"])
            self._set_account_child(account, "password", desired["password"])
            self._set_account_child(
                account,
                "allowservicelogon",
                "true" if desired["allowservicelogon"] else "",
            )
            return

        for tag in ("username", "password"):
            if desired[tag] != old[tag]:
                self._set_account_child(account, tag, desired[tag])
        if desired["allowservicelogon"] != old["allowservicelogon"]:
            allow_logon = account.find("allowservicelogon")
            if (
                not desired["allowservicelogon"]
                and allow_logon is not None
                and (allow_logon.attrib or list(allow_logon))
            ):
                allow_logon.text = "false"
            else:
                self._set_account_child(
                    account,
                    "allowservicelogon",
                    "true" if desired["allowservicelogon"] else "",
                )

        if not account.attrib and not list(account):
            root.remove(account)

    @staticmethod
    def _set_account_child(account: ET.Element, tag: str, value: str) -> None:
        element = account.find(tag)
        if not value:
            if element is not None:
                account.remove(element)
            return
        if element is None:
            order = {"username": 0, "password": 1, "allowservicelogon": 2}
            desired_order = order[tag]
            insert_at = len(account)
            for index, child in enumerate(account):
                child_order = order.get(child.tag)
                if child_order is not None and child_order > desired_order:
                    insert_at = index
                    break
            element = ET.Element(tag)
            account.insert(insert_at, element)
        element.text = value

    @staticmethod
    def _validate_xml_safety(raw: bytes) -> None:
        parser = expat.ParserCreate()

        def reject_declaration(*_args):
            raise ConfigParseError("DTD and entity declarations are not allowed")

        parser.StartDoctypeDeclHandler = reject_declaration
        parser.EntityDeclHandler = reject_declaration
        parser.UnparsedEntityDeclHandler = reject_declaration
        parser.ExternalEntityRefHandler = reject_declaration
        try:
            parser.Parse(raw, True)
        except ConfigParseError:
            raise
        except (expat.ExpatError, UnicodeError) as exc:
            raise ConfigParseError(f"Invalid XML: {exc}") from exc

    def _parse_document(
        self, xml_text
    ) -> tuple[ET.Element, list[ET.Element], list[ET.Element]]:
        raw = xml_text if isinstance(xml_text, bytes) else str(xml_text).encode("utf-8")
        self._validate_xml_safety(raw)
        try:
            dom = minidom.parseString(raw)
        except (expat.ExpatError, UnicodeError) as exc:
            raise ConfigParseError(f"Invalid XML: {exc}") from exc

        parser = ET.XMLParser(
            target=ET.TreeBuilder(insert_comments=True, insert_pis=True)
        )
        try:
            root = ET.fromstring(dom.documentElement.toxml(encoding="utf-8"), parser)
            leading_nodes = []
            trailing_nodes = []
            destination = leading_nodes
            for node in dom.childNodes:
                if node is dom.documentElement:
                    destination = trailing_nodes
                elif node.nodeType == Node.COMMENT_NODE:
                    destination.append(ET.Comment(node.data))
                elif node.nodeType == Node.PROCESSING_INSTRUCTION_NODE:
                    destination.append(
                        ET.ProcessingInstruction(node.target, node.data)
                    )
        except (ET.ParseError, UnicodeError) as exc:
            raise ConfigParseError(f"Invalid XML: {exc}") from exc
        finally:
            dom.unlink()

        if root.tag != "service":
            raise ConfigParseError("The XML root must be <service>")

        single_tags = set(self.SIMPLE_TAGS) | {"interactive", "log", "serviceaccount"}
        for tag in single_tags:
            if len(root.findall(tag)) > 1:
                raise ConfigParseError(f"Duplicate <{tag}> element")

        service_account = root.find("serviceaccount")
        if service_account is not None:
            for tag in ("username", "password", "allowservicelogon"):
                if len(service_account.findall(tag)) > 1:
                    raise ConfigParseError(
                        f"Duplicate <serviceaccount>/<{tag}> element"
                    )
        return root, leading_nodes, trailing_nodes

    def _parse_xml(self, xml_text) -> ET.Element:
        root, _leading_nodes, _trailing_nodes = self._parse_document(xml_text)
        return root

    def _values_from_root(self, root: ET.Element) -> dict:
        values = self.get_default_config()
        values["log_mode"] = "append"
        for tag in self.SIMPLE_TAGS:
            element = root.find(tag)
            if element is not None:
                values[tag] = element.text or ""

        interactive = root.find("interactive")
        if interactive is None:
            values["interactive"] = False
        else:
            values["interactive"] = (interactive.text or "").strip().lower() not in {
                "false",
                "0",
                "no",
            }

        log = root.find("log")
        if log is not None:
            values["log_mode"] = log.get("mode", "append")

        values["onfailure"] = [
            {"action": element.get("action", ""), "delay": element.get("delay", "")}
            for element in root.findall("onfailure")
        ]
        values["environments"] = [
            {"name": element.get("name", ""), "value": element.get("value", "")}
            for element in root.findall("env")
        ]

        account = root.find("serviceaccount")
        if account is not None:
            username = account.find("username")
            password = account.find("password")
            allow_logon = account.find("allowservicelogon")
            values["serviceaccount"] = {
                "username": username.text if username is not None and username.text else "",
                "password": password.text if password is not None and password.text else "",
                "allowservicelogon": (
                    allow_logon is not None
                    and (allow_logon.text or "").strip().lower() in {"true", "1", "yes"}
                ),
            }
        return values

    def get_default_config(self) -> dict:
        """返回一个新服务的默认配置字典。"""
        config = {tag: '' for tag in self.SIMPLE_TAGS}
        config['log_mode'] = 'roll'  # 将默认模式改为更常用的roll
        # logpath 默认值为空字符串，由mainwindow在保存时处理
        config['onfailure'] = []
        config['resetfailure'] = '1 day'
        config['priority'] = 'normal'
        config['stoptimeout'] = '15 sec'
        config['interactive'] = False
        config['serviceaccount'] = {'username': 'LocalSystem'}
        config['environments'] = []
        return config

    def _from_xml_root(self, root) -> dict:
        """从一个XML Element根节点解析配置到字典。"""
        return self._values_from_root(root)

    def load_from_xml(self, file_path: str) -> dict:
        """从XML文件加载配置。"""
        return self._values_from_root(self._parse_xml(Path(file_path).read_bytes()))

    def load_from_xml_string(self, xml_string: str) -> dict:
        """从XML字符串加载配置。"""
        return self._values_from_root(self._parse_xml(xml_string))

    def _to_xml_root(self, config: dict) -> ET.Element:
        """将配置字典转换为一个XML Element根节点。"""
        root = ET.Element("service")

        # 定义一个推荐的元素顺序
        ordered_tags = [
            'id', 'name', 'description', 'executable', 'arguments', 'workingdirectory'
        ]
        for tag in ordered_tags:
            if config.get(tag): ET.SubElement(root, tag).text = config[tag]

        if config.get('environments'):
            for env in config['environments']:
                if env.get('name'): ET.SubElement(root, 'env', attrib=env)

        # --- 关键修正处 ---
        # 1. logpath现在作为顶级元素处理
        # 2. log只处理mode属性
        # 3. 将所有剩余的简单标签（包括logpath）添加到XML中
        remaining_simple_tags = [tag for tag in self.SIMPLE_TAGS if tag not in ordered_tags]
        for tag in remaining_simple_tags:
            if config.get(tag): ET.SubElement(root, tag).text = config[tag]

        if config.get('interactive'): ET.SubElement(root, 'interactive').text = 'true'

        # 只生成带mode属性的log标签，不再包含子元素
        if config.get('log_mode'):
            ET.SubElement(root, 'log', attrib={'mode': config.get('log_mode')})
        # --- 修正结束 ---

        if config.get('onfailure'):
            for action_item in config['onfailure']:
                action = action_item.get('action')
                if not action: continue
                attribs = {'action': action}
                delay = action_item.get('delay')
                if delay: attribs['delay'] = delay
                ET.SubElement(root, 'onfailure', attrib=attribs)

        sa_config = config.get('serviceaccount')
        if sa_config and sa_config.get('username'):
            sa_element = ET.SubElement(root, 'serviceaccount')
            ET.SubElement(sa_element, 'username').text = sa_config['username']
            if sa_config.get('password'): ET.SubElement(sa_element, 'password').text = sa_config['password']
            if sa_config.get('allowservicelogon'): ET.SubElement(sa_element, 'allowservicelogon').text = 'true'

        return root

    def save_to_xml_string(self, config: dict) -> str:
        """将配置字典转换为格式化的XML字符串。"""
        root = self._to_xml_root(config)
        xml_string = ET.tostring(root, 'utf-8')
        parsed_string = minidom.parseString(xml_string)
        # 移除空行
        return os.linesep.join([s for s in parsed_string.toprettyxml(indent="  ").splitlines() if s.strip()])

    def save_to_xml(self, config: dict, file_path: str):
        """Compatibility adapter that enforces managed and atomic saves."""
        requested = Path(file_path)
        exists = requested.exists()
        requested = self._validate_managed_path(requested, must_exist=exists)
        if exists:
            document = self.load_managed(requested)
        else:
            service_id = str(config.get("id", ""))
            if requested.name.casefold() != f"{service_id}.xml".casefold():
                raise ManagedPathError(
                    "New managed configuration filename must match its service ID"
                )
            document = self.new_document()
        self.merge_ui_data(document, config)
        return self.save(document)
