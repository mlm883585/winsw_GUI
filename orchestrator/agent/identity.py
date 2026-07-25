from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID


_DMTF_PATTERN = re.compile(
    r"^(?P<date>\d{14})\.(?P<microseconds>\d{6})(?P<sign>[+-])(?P<offset>\d{3})$"
)
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class BootIdentityUnavailable(RuntimeError):
    """The Windows OS boot marker cannot be determined safely."""


class BootMarkerProvider(Protocol):
    def get_boot_marker(self) -> str: ...


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    agent_id: UUID
    boot_id: UUID
    agent_instance_id: UUID
    instance_generation: int


def dmtf_datetime_to_filetime(value: str) -> str:
    """Convert WMI DMTF datetime to a canonical UTC FILETIME decimal string."""
    match = _DMTF_PATTERN.fullmatch(value)
    if match is None:
        raise BootIdentityUnavailable("WMI LastBootUpTime has an unsupported format")
    try:
        local_naive = datetime.strptime(match.group("date"), "%Y%m%d%H%M%S").replace(
            microsecond=int(match.group("microseconds"))
        )
        offset_minutes = int(match.group("offset"))
        if match.group("sign") == "-":
            offset_minutes = -offset_minutes
        local = local_naive.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
        utc = local.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise BootIdentityUnavailable("WMI LastBootUpTime is invalid") from exc
    delta = utc - _FILETIME_EPOCH
    ticks = (delta.days * 86_400 + delta.seconds) * 10_000_000 + delta.microseconds * 10
    if ticks <= 0:
        raise BootIdentityUnavailable("WMI LastBootUpTime predates the FILETIME epoch")
    return str(ticks)


class WmiBootMarkerProvider:
    def get_boot_marker(self) -> str:
        try:
            import win32com.client  # type: ignore[import-not-found]

            locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
            service = locator.ConnectServer(".", "root\\cimv2")
            rows = list(service.ExecQuery("SELECT LastBootUpTime FROM Win32_OperatingSystem"))
            if len(rows) != 1:
                raise BootIdentityUnavailable("WMI did not return exactly one operating system row")
            raw = str(rows[0].LastBootUpTime)
            return dmtf_datetime_to_filetime(raw)
        except BootIdentityUnavailable:
            raise
        except Exception as exc:
            raise BootIdentityUnavailable("cannot query Windows LastBootUpTime through WMI") from exc
