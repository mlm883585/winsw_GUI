from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse


REPOSITORY = Path(__file__).resolve().parents[1]
LOCK_PATH = REPOSITORY / "deployment" / "winsw-x64-v2.12.0.lock.json"


def test_winsw_lock_pins_official_stable_x64_asset() -> None:
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))

    assert lock["schema_version"] == 1
    assert lock["component"] == "WinSW"
    assert lock["version"] == "2.12.0"
    assert lock["release_channel"] == "stable-2.x"
    assert lock["release_is_draft"] is False
    assert lock["release_is_prerelease"] is False
    assert lock["architecture"] == "x64"
    assert lock["asset_name"] == "WinSW-x64.exe"
    assert lock["size_bytes"] == 18_243_033
    assert lock["authenticode_status"] == "NotSigned"

    release = urlparse(lock["release_url"])
    download = urlparse(lock["download_url"])
    assert release.scheme == download.scheme == "https"
    assert release.hostname == download.hostname == "github.com"
    assert release.path == "/winsw/winsw/releases/tag/v2.12.0"
    assert download.path == "/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
    assert "/latest/" not in lock["download_url"].casefold()
    assert re.fullmatch(r"[0-9a-f]{64}", lock["sha256"])


def test_installer_can_consume_lock_without_runtime_version_discovery() -> None:
    script = (REPOSITORY / "scripts" / "install_recovery_service.ps1").read_text(
        encoding="utf-8"
    )

    assert "$WinSWLockPath" in script
    assert 'schema_version -ne 1' in script
    assert 'architecture -ne "x64"' in script
    assert 'asset_name -ne "WinSW-x64.exe"' in script
    assert "$lockObject.download_url" in script
    assert "$lockObject.sha256" in script
    assert "$actualSizeBytes -ne $expectedSizeBytes" in script
    assert "Get-AuthenticodeSignature" in script
    assert 'Assert-WinSWIntegrity -LiteralPath $stagedWinSW' in script
    assert 'Assert-WinSWIntegrity -LiteralPath $Journal.winsw_path' in script
    assert script.index('Assert-WinSWIntegrity -LiteralPath $Journal.winsw_path') < script.index(
        '-Purpose "WinSW install"'
    )
    assert "/latest/" in script
    assert "releases/latest" not in script.casefold()
