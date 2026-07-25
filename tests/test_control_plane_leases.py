from __future__ import annotations

import pytest

from orchestrator.control_plane.leases import MonotonicLeaseRegistry


def test_registry_starts_fail_closed_and_expires_at_exact_boundary() -> None:
    ticks = {"now": 10.0}
    leases = MonotonicLeaseRegistry(45, monotonic=lambda: ticks["now"])

    assert leases.is_online("agent-a") is False
    leases.renew("agent-a")
    ticks["now"] += 44.999
    assert leases.is_online("agent-a") is True
    ticks["now"] += 0.001
    assert leases.is_online("agent-a") is False


def test_registry_is_independent_of_wall_clock_and_renewal_restores_lease() -> None:
    ticks = {"now": 100.0}
    leases = MonotonicLeaseRegistry(45, monotonic=lambda: ticks["now"])

    leases.renew("agent-a")
    # There is intentionally no wall-clock input to move forwards or backwards.
    ticks["now"] = 120.0
    assert leases.is_online("agent-a") is True
    ticks["now"] = 90.0
    assert leases.is_online("agent-a") is False
    ticks["now"] = 121.0
    assert leases.is_online("agent-a") is False
    leases.renew("agent-a")
    assert leases.is_online("agent-a") is True


def test_registry_rejects_non_positive_lease() -> None:
    with pytest.raises(ValueError, match="positive"):
        MonotonicLeaseRegistry(0)
