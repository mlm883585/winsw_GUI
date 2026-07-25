"""Application-owned filesystem locations."""

from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile


class AppPathsError(RuntimeError):
    """Raised when an application directory cannot be prepared safely."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved paths rooted beside the source tree or frozen executable."""

    root: Path
    settings_file: Path
    services_dir: Path
    logs_dir: Path
    bin_dir: Path

    @classmethod
    def from_root(cls, root: Path) -> "AppPaths":
        """Build a path set from an explicit root, primarily for injection/tests."""
        resolved_root = Path(root).resolve()
        return cls(
            root=resolved_root,
            settings_file=resolved_root / "settings.json",
            services_dir=resolved_root / "services",
            logs_dir=resolved_root / "logs",
            bin_dir=resolved_root / "bin",
        )

    @classmethod
    def discover(cls) -> "AppPaths":
        """Resolve the application root without consulting the working directory."""
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
        else:
            root = Path(__file__).resolve().parents[1]
        return cls.from_root(root)

    def ensure_runtime_dirs(self) -> None:
        """Create application directories and verify each is writable."""
        directories = (self.root, self.services_dir, self.logs_dir, self.bin_dir)
        for directory in directories:
            if directory != self.root and directory.parent != self.root:
                raise AppPathsError(
                    f"运行目录必须直接位于应用目录下: {directory}"
                )
            is_junction = getattr(directory, "is_junction", lambda: False)
            if directory.is_symlink() or is_junction():
                raise AppPathsError(f"运行目录不能是符号链接或联接: {directory}")
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AppPathsError(f"无法创建运行目录 {directory}: {exc}") from exc

            try:
                resolved = directory.resolve(strict=True)
            except OSError as exc:
                raise AppPathsError(f"无法解析运行目录 {directory}: {exc}") from exc
            if resolved != directory:
                raise AppPathsError(
                    f"运行目录不能重定向到其他位置: {directory} -> {resolved}"
                )

            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".winsw-gui-write-", dir=directory
                ):
                    pass
            except OSError as exc:
                raise AppPathsError(f"无法写入运行目录 {directory}: {exc}") from exc

        if (
            self.settings_file.parent != self.root
            or self.settings_file.name != "settings.json"
        ):
            raise AppPathsError(
                f"设置文件必须位于应用目录下: {self.settings_file}"
            )
        if self.settings_file.is_symlink():
            raise AppPathsError(f"设置文件不能是符号链接: {self.settings_file}")
        if self.settings_file.exists() and not self.settings_file.is_file():
            raise AppPathsError(f"设置路径不是普通文件: {self.settings_file}")
