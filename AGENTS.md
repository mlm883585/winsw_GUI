# Repository Guidelines

## Project Structure & Module Organization

`main.py` starts the Tkinter application. Core paths, XML configuration, settings, and WinSW operations live in `core/`. UI code lives in `gui/`, with feature tabs in `gui/tabs/`. Put icons and screenshots in `etc/`, XML examples in `templates/`, and pytest modules in `tests/`.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt -r requirements-dev.txt` installs runtime and test dependencies.
- `python main.py` launches the application from source on Windows.
- `python -m pytest -q` runs the automated suite.
- `python -m compileall -q core gui main.py` performs a fast syntax check.
- `bash build.sh` packages `dist/WinSW_GUI.exe` with PyInstaller (installed separately).
- `bash clean.sh` removes build output and Python caches.

Run commands from the repository root. GitHub Actions repeats checks on `windows-latest` with Python 3.12. Service operations may require an elevated terminal.

## Coding Style & Naming Conventions

Use four-space indentation and PEP 8 conventions. Name modules, functions, and variables with `snake_case`, classes with `PascalCase`, and constants with `UPPER_SNAKE_CASE`. Group imports as standard library, third-party, then local. Keep WinSW and filesystem behavior out of Tkinter widgets, prefer focused methods, and preserve established Chinese UI terminology.

## Testing Guidelines

Use `pytest`; files must be named `test_<module>.py` and tests `test_<behavior>`. Cover success and failure paths for XML parsing, path validation, atomic persistence, and dirty-state UI flows. Use temporary directories and mocks so tests never access the network or install, modify, or control real Windows services. There is no numeric coverage threshold, but every behavior change needs a regression test plus relevant manual UI verification.

## Runtime Data & Configuration Safety

The application stores `settings.json`, `bin/`, `services/`, and `logs/` beside the source root or frozen EXE. That directory must be writable; do not add a current-working-directory fallback. Service IDs must match `[A-Za-z0-9]+` and must not be Windows device names. External XML imports are editing copies: never overwrite the origin; save validated results into `services/`. Do not commit runtime data, service XML containing credentials, executables, or logs.

## Commit & Pull Request Guidelines

Follow the repository's Conventional Commit style (`feat:`, `fix:`, `docs:`, `chore:`), with an optional scope and one logical change per commit. Pull requests should explain behavior changes, link issues, list test and manual verification results, and include screenshots for visible UI changes.
