"""Persistent application settings."""

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from core.app_paths import AppPaths


class SettingsManager:
    """Load and atomically save the application's global JSON settings."""

    DEFAULTS = {
        "winsw_management_mode": "auto",
        "winsw_custom_path": "",
        "window_geometry": "1200x800+100+100",
        "main_sash_pos": 300,
        "right_sash_pos": 500,
    }

    def __init__(self, app_paths_or_file: AppPaths | Path):
        if isinstance(app_paths_or_file, AppPaths):
            self.app_paths = app_paths_or_file
            self.settings_file = app_paths_or_file.settings_file
        else:
            self.app_paths = None
            self.settings_file = Path(app_paths_or_file)
        self.settings = self._load_defaults()
        self.load_settings()

    def _load_defaults(self) -> dict[str, Any]:
        """Return an independent copy of the default settings."""
        return self.DEFAULTS.copy()

    def load_settings(self) -> None:
        """Load JSON settings, falling back safely for absent or damaged files."""
        defaults = self._load_defaults()
        try:
            with self.settings_file.open("r", encoding="utf-8") as stream:
                loaded_settings = json.load(stream)
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
            self.settings = defaults
            return

        if not isinstance(loaded_settings, dict):
            self.settings = defaults
            return

        defaults.update(loaded_settings)
        self.settings = defaults

    def save_settings(self) -> None:
        """Atomically replace the settings file with the current settings."""
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.settings_file.name}.",
                suffix=".tmp",
                dir=self.settings_file.parent,
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(self.settings, stream, ensure_ascii=False, indent=4)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())

            os.replace(temp_path, self.settings_file)
        except BaseException:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def get(self, key: str) -> Any:
        """Return a setting value, or ``None`` when the key is unknown."""
        return self.settings.get(key)

    def set(self, key: str, value: Any) -> None:
        """Update an in-memory setting."""
        self.settings[key] = value
