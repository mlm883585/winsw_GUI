from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from orchestrator.common.errors import ApiError, ErrorCode
from orchestrator.common.db import SQLiteDatabase
from orchestrator.common.models import MAX_SERVICE_ITEMS
from orchestrator.control_plane.store import (
    ControlPlaneStore,
    _migration_v1,
    _migration_v2,
    _migration_v3,
)


BASE = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


def service(local_id: str, windows_name: str | None = None) -> dict[str, object]:
    return {
        "local_service_id": local_id,
        "windows_service_name": windows_name or local_id.upper(),
        "display_name": local_id.title(),
        "installation_state": "INSTALLED",
        "runtime_state": "INACTIVE",
        "startup_state": "AUTOSTART_DISABLED",
        "last_observed_at": BASE.isoformat(),
    }


def report(
    agent_id: UUID,
    boot_id: UUID,
    instance_id: UUID,
    *,
    generation: int = 1,
    sequence: int = 1,
    services: list[dict[str, object]] | None = None,
    endpoint: str = "http://10.20.0.11:8765",
) -> dict[str, object]:
    return {
        "agent_id": str(agent_id),
        "boot_id": str(boot_id),
        "agent_instance_id": str(instance_id),
        "instance_generation": generation,
        "sequence": sequence,
        "version": "0.1.0",
        "endpoint": endpoint,
        "hostname": "APP-SRV-01",
        "services": services or [],
    }


def populate_legacy_v4_capacity(
    database: SQLiteDatabase, collection: str, count: int
) -> str:
    """Create v4-shaped rows after intentionally rewinding an empty v5 database."""

    ControlPlaneStore(database)
    timestamp = BASE.isoformat()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE schema_versions SET version=4 WHERE component='control-plane'"
        )
        connection.execute("DROP INDEX idx_services_active_agent_local")
        if collection == "agents":
            connection.executemany(
                """
                INSERT INTO agents(
                    agent_id, boot_id, agent_instance_id, instance_generation,
                    hostname, endpoint, peer_ip, version, last_sequence,
                    last_received_at, created_at, updated_at
                ) VALUES(?, ?, ?, 1, ?, 'http://10.20.0.11:8765',
                         '10.20.0.11', '0.1.0', 1, ?, ?, ?)
                """,
                (
                    (
                        str(uuid4()),
                        str(uuid4()),
                        str(uuid4()),
                        f"host-{index:04d}",
                        timestamp,
                        timestamp,
                        timestamp,
                    )
                    for index in range(count)
                ),
            )
            return "SELECT COUNT(*) FROM agents"
        if collection == "active managed services":
            agent_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO agents(
                    agent_id, boot_id, agent_instance_id, instance_generation,
                    hostname, endpoint, peer_ip, version, last_sequence,
                    last_received_at, created_at, updated_at
                ) VALUES(?, ?, ?, 1, 'capacity-host', 'http://10.20.0.11:8765',
                         '10.20.0.11', '0.1.0', 1, ?, ?, ?)
                """,
                (
                    agent_id,
                    str(uuid4()),
                    str(uuid4()),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.executemany(
                """
                INSERT INTO services(
                    managed_service_id, agent_id, local_service_id,
                    windows_service_name, display_name, installation_state,
                    runtime_state, startup_state, last_observed_at, mirrored_at,
                    seen_in_last_report
                ) VALUES(?, ?, ?, ?, ?, 'INSTALLED', 'INACTIVE',
                         'AUTOSTART_DISABLED', ?, ?, 1)
                """,
                (
                    (
                        str(uuid4()),
                        agent_id,
                        f"svc-{index:04d}",
                        f"Service{index:04d}",
                        f"Service {index:04d}",
                        timestamp,
                        timestamp,
                    )
                    for index in range(count)
                ),
            )
            return "SELECT COUNT(*) FROM services WHERE seen_in_last_report=1"
        if collection == "recovery groups":
            connection.executemany(
                """
                INSERT INTO recovery_groups(
                    group_id, name, description, state,
                    node_settle_window_seconds, max_parallel_services,
                    created_at, updated_at, blocked_reasons_json
                ) VALUES(?, ?, '', 'DISARMED', 120, 4, ?, ?, '[]')
                """,
                (
                    (str(uuid4()), f"group-{index:04d}", timestamp, timestamp)
                    for index in range(count)
                ),
            )
            return "SELECT COUNT(*) FROM recovery_groups"
    raise AssertionError(f"unknown legacy collection {collection}")


def test_agent_fencing_and_duplicate_sequence_does_not_renew_lease(tmp_path) -> None:
    ticks = {"now": 0.0}
    store = ControlPlaneStore(
        tmp_path / "cp.sqlite3", monotonic=lambda: ticks["now"]
    )
    agent_id, boot_id, instance_id = uuid4(), uuid4(), uuid4()
    first = report(agent_id, boot_id, instance_id, services=[service("mysql")])

    accepted = store.register_agent(first, "10.20.0.11", now=BASE)
    duplicate = store.ingest_heartbeat(
        agent_id, first, "10.20.0.11", now=BASE + timedelta(seconds=30)
    )

    assert accepted["accepted"] is True
    assert duplicate == {
        "accepted": False,
        "ignored": True,
        "reason": "DUPLICATE_OR_OUT_OF_ORDER",
        "received_at": "2026-07-16T08:00:30.000Z",
        "lease_updated_at": None,
    }
    ticks["now"] = 46.0
    assert store.list_agents(now=BASE + timedelta(seconds=46))[0]["online_status"] == "OFFLINE"

    with pytest.raises(ApiError) as stale:
        store.register_agent(
            report(agent_id, boot_id, uuid4(), services=[service("mysql")]),
            "10.20.0.11",
            now=BASE + timedelta(seconds=31),
        )
    assert stale.value.code is ErrorCode.STALE_AGENT_INSTANCE

    next_instance = uuid4()
    store.register_agent(
        report(
            agent_id,
            boot_id,
            next_instance,
            generation=2,
            services=[service("mysql")],
        ),
        "10.20.0.11",
        now=BASE + timedelta(seconds=32),
    )
    summary = store.list_agents(now=BASE + timedelta(seconds=32))[0]
    assert summary["agent_instance_id"] == str(next_instance)
    assert summary["last_sequence"] == 1


def test_cp_restart_requires_a_new_accepted_report_before_agent_is_online(tmp_path) -> None:
    database = tmp_path / "cp.sqlite3"
    ticks = {"now": 0.0}
    agent_id, boot_id, instance_id = uuid4(), uuid4(), uuid4()
    first = report(agent_id, boot_id, instance_id, services=[service("mysql")])
    initial = ControlPlaneStore(database, monotonic=lambda: ticks["now"])
    initial.register_agent(first, "10.20.0.11", now=BASE)
    assert initial.list_agents()[0]["online_status"] == "ONLINE"

    restarted = ControlPlaneStore(database, monotonic=lambda: ticks["now"])
    assert restarted.list_agents()[0]["online_status"] == "OFFLINE"
    duplicate = restarted.ingest_heartbeat(
        agent_id, first, "10.20.0.11", now=BASE + timedelta(seconds=1)
    )
    assert duplicate["ignored"] is True
    assert restarted.list_agents()[0]["online_status"] == "OFFLINE"

    accepted = restarted.ingest_heartbeat(
        agent_id,
        report(
            agent_id,
            boot_id,
            instance_id,
            sequence=2,
            services=[service("mysql")],
        ),
        "10.20.0.11",
        now=BASE + timedelta(seconds=2),
    )
    assert accepted["accepted"] is True
    assert restarted.list_agents()[0]["online_status"] == "ONLINE"


def test_agent_endpoint_must_match_socket_peer(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "cp.sqlite3")
    with pytest.raises(ApiError) as mismatch:
        store.register_agent(
            report(
                uuid4(),
                uuid4(),
                uuid4(),
                endpoint="http://10.20.0.99:8765",
                services=[service("mysql")],
            ),
            "10.20.0.11",
            now=BASE,
        )
    assert mismatch.value.code is ErrorCode.ENDPOINT_SOURCE_MISMATCH
    assert store.list_agents() == []


def test_public_agent_and_service_collections_enforce_global_capacity_atomically(
    tmp_path,
) -> None:
    store = ControlPlaneStore(tmp_path / "inventory-capacity.sqlite3")
    first_agent = uuid4()
    first_services = [
        service(f"svc-{index:04d}") for index in range(MAX_SERVICE_ITEMS - 1)
    ]
    store.register_agent(
        report(first_agent, uuid4(), uuid4(), services=first_services),
        "10.20.0.11",
        now=BASE,
    )
    second_agent, second_boot, second_instance = uuid4(), uuid4(), uuid4()
    store.register_agent(
        report(
            second_agent,
            second_boot,
            second_instance,
            services=[service("last-slot")],
            endpoint="http://10.20.0.12:8765",
        ),
        "10.20.0.12",
        now=BASE,
    )
    assert len(store.list_services()) == MAX_SERVICE_ITEMS

    with pytest.raises(ApiError) as exceeded:
        store.ingest_heartbeat(
            second_agent,
            report(
                second_agent,
                second_boot,
                second_instance,
                sequence=2,
                services=[service("last-slot"), service("one-too-many")],
                endpoint="http://10.20.0.12:8765",
            ),
            "10.20.0.12",
            now=BASE + timedelta(seconds=1),
        )
    assert exceeded.value.status_code == 422
    assert exceeded.value.code is ErrorCode.VALIDATION_ERROR
    assert len(store.list_services()) == MAX_SERVICE_ITEMS
    second_summary = next(
        item for item in store.list_agents() if item["agent_id"] == str(second_agent)
    )
    assert second_summary["last_sequence"] == 1


def test_agent_collection_capacity_rejects_new_agent_atomically(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orchestrator.control_plane.store.MAX_SERVICE_ITEMS", 2)
    store = ControlPlaneStore(tmp_path / "agent-capacity.sqlite3")
    for index in range(2):
        store.register_agent(
            report(
                uuid4(),
                uuid4(),
                uuid4(),
                services=[service(f"svc-{index}")],
                endpoint=f"http://10.20.0.{index + 11}:8765",
            ),
            f"10.20.0.{index + 11}",
            now=BASE,
        )

    with pytest.raises(ApiError) as exceeded:
        store.register_agent(
            report(
                uuid4(),
                uuid4(),
                uuid4(),
                services=[service("svc-over-capacity")],
                endpoint="http://10.20.0.13:8765",
            ),
            "10.20.0.13",
            now=BASE,
        )
    assert exceeded.value.status_code == 422
    assert exceeded.value.code is ErrorCode.VALIDATION_ERROR
    assert exceeded.value.message == "Control Plane Agent capacity was reached"
    assert len(store.list_agents()) == 2


def test_independent_stores_cannot_oversell_last_agent_capacity_slot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("orchestrator.control_plane.store.MAX_SERVICE_ITEMS", 1)
    for attempt in range(3):
        database_path = tmp_path / f"concurrent-capacity-{attempt}.sqlite3"
        stores = (ControlPlaneStore(database_path), ControlPlaneStore(database_path))
        barrier = Barrier(2)

        def register(index: int) -> str:
            barrier.wait()
            try:
                stores[index].register_agent(
                    report(
                        uuid4(),
                        uuid4(),
                        uuid4(),
                        services=[service(f"svc-{index}")],
                        endpoint=f"http://10.20.0.{index + 11}:8765",
                    ),
                    f"10.20.0.{index + 11}",
                    now=BASE,
                )
            except ApiError as exc:
                return exc.code.value
            return "ACCEPTED"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(register, range(2)))
        assert sorted(outcomes) == ["ACCEPTED", "VALIDATION_ERROR"]
        persisted = ControlPlaneStore(database_path)
        assert len(persisted.list_agents()) == 1
        assert len(persisted.list_services()) == 1


def test_recovery_group_collection_enforces_exact_public_capacity(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "group-capacity.sqlite3")
    for index in range(MAX_SERVICE_ITEMS):
        store.create_group({"name": f"group-{index:04d}"})
    assert len(store.list_groups()) == MAX_SERVICE_ITEMS

    with pytest.raises(ApiError) as exceeded:
        store.create_group({"name": "group-over-capacity"})
    assert exceeded.value.status_code == 422
    assert exceeded.value.code is ErrorCode.VALIDATION_ERROR
    assert len(store.list_groups()) == MAX_SERVICE_ITEMS


@pytest.mark.parametrize(
    "collection", ("agents", "active managed services", "recovery groups")
)
def test_v5_capacity_migration_accepts_exact_limit_and_adds_active_index(
    tmp_path, collection: str
) -> None:
    database = SQLiteDatabase(tmp_path / f"v4-{collection.replace(' ', '-')}-exact.sqlite3")
    count_query = populate_legacy_v4_capacity(database, collection, MAX_SERVICE_ITEMS)

    ControlPlaneStore(database)

    with database.read() as connection:
        assert connection.execute(
            "SELECT version FROM schema_versions WHERE component='control-plane'"
        ).fetchone()[0] == 5
        assert connection.execute(count_query).fetchone()[0] == MAX_SERVICE_ITEMS
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='index' AND name='idx_services_active_agent_local'
            """
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    "collection", ("agents", "active managed services", "recovery groups")
)
def test_v5_capacity_migration_rejects_oversized_v4_atomically(
    tmp_path, collection: str
) -> None:
    database = SQLiteDatabase(tmp_path / f"v4-{collection.replace(' ', '-')}-oversized.sqlite3")
    count_query = populate_legacy_v4_capacity(database, collection, MAX_SERVICE_ITEMS + 1)

    with pytest.raises(
        sqlite3.IntegrityError,
        match=rf"cannot migrate: {collection} exceed public collection capacity {MAX_SERVICE_ITEMS}",
    ):
        ControlPlaneStore(database)

    with database.read() as connection:
        assert connection.execute(
            "SELECT version FROM schema_versions WHERE component='control-plane'"
        ).fetchone()[0] == 4
        assert connection.execute(count_query).fetchone()[0] == MAX_SERVICE_ITEMS + 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='index' AND name='idx_services_active_agent_local'
            """
        ).fetchone()[0] == 0


def test_stale_service_can_be_retained_or_removed_but_not_newly_added(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "stale-member.sqlite3")
    agent_id, boot_id, instance_id = uuid4(), uuid4(), uuid4()
    store.register_agent(
        report(
            agent_id,
            boot_id,
            instance_id,
            services=[service("mysql"), service("nacos")],
        ),
        "10.20.0.11",
        now=BASE,
    )
    by_local = {item["local_service_id"]: item for item in store.list_services()}
    mysql_id = by_local["mysql"]["managed_service_id"]
    nacos_id = by_local["nacos"]["managed_service_id"]
    group = store.create_group({"name": "stale-member"})
    store.replace_members(group["group_id"], [mysql_id])

    store.ingest_heartbeat(
        agent_id,
        report(
            agent_id,
            boot_id,
            instance_id,
            sequence=2,
            services=[service("nacos")],
        ),
        "10.20.0.11",
        now=BASE + timedelta(seconds=1),
    )
    retained = store.replace_members(group["group_id"], [mysql_id, nacos_id])
    assert {item["managed_service_id"] for item in retained["members"]} == {
        mysql_id,
        nacos_id,
    }

    removed = store.replace_members(group["group_id"], [nacos_id])
    assert [item["managed_service_id"] for item in removed["members"]] == [nacos_id]
    with pytest.raises(ApiError) as stale_addition:
        store.replace_members(group["group_id"], [mysql_id, nacos_id])
    assert stale_addition.value.status_code == 404
    assert stale_addition.value.code is ErrorCode.SERVICE_NOT_ALLOWLISTED
    assert stale_addition.value.detail == {"unreported": [mysql_id]}
    assert [
        item["managed_service_id"]
        for item in store.get_group(group["group_id"])["members"]
    ] == [nacos_id]

    store.ingest_heartbeat(
        agent_id,
        report(
            agent_id,
            boot_id,
            instance_id,
            sequence=3,
            services=[service("mysql"), service("nacos")],
        ),
        "10.20.0.11",
        now=BASE + timedelta(seconds=2),
    )
    returned_mysql = next(
        item for item in store.list_services() if item["local_service_id"] == "mysql"
    )
    assert returned_mysql["managed_service_id"] == mysql_id
    restored = store.replace_members(group["group_id"], [mysql_id, nacos_id])
    assert {item["managed_service_id"] for item in restored["members"]} == {
        mysql_id,
        nacos_id,
    }


def test_dependency_replace_is_atomic_and_cycle_is_rejected(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: BASE)
    agent_id, boot_id, instance_id = uuid4(), uuid4(), uuid4()
    store.register_agent(
        report(
            agent_id,
            boot_id,
            instance_id,
            services=[service("mysql"), service("nacos")],
        ),
        "10.20.0.11",
        now=BASE,
    )
    by_local = {item["local_service_id"]: item for item in store.list_services(now=BASE)}
    group = store.create_group({"name": "core", "node_settle_window_seconds": 120})
    group = store.replace_members(
        group["group_id"],
        [by_local["mysql"]["managed_service_id"], by_local["nacos"]["managed_service_id"]],
    )
    valid = [
        {
            "managed_service_id": by_local["nacos"]["managed_service_id"],
            "prerequisite_managed_service_id": by_local["mysql"]["managed_service_id"],
        }
    ]
    store.replace_dependencies(group["group_id"], valid)

    with pytest.raises(ApiError) as cycle:
        store.replace_dependencies(
            group["group_id"],
            [
                *valid,
                {
                    "managed_service_id": by_local["mysql"]["managed_service_id"],
                    "prerequisite_managed_service_id": by_local["nacos"]["managed_service_id"],
                },
            ],
        )
    assert cycle.value.code is ErrorCode.DEPENDENCY_CYCLE
    assert store.get_group(group["group_id"])["dependencies"] == valid


def test_arm_epoch_settle_and_auto_run_are_durable_and_unique(tmp_path) -> None:
    current = {"now": BASE}
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: current["now"])
    agent_id, boot_id, instance_id = uuid4(), uuid4(), uuid4()
    store.register_agent(
        report(agent_id, boot_id, instance_id, services=[service("mysql")]),
        "10.20.0.11",
        now=BASE,
    )
    managed_id = store.list_services(now=BASE)[0]["managed_service_id"]
    group = store.create_group({"name": "core", "node_settle_window_seconds": 1})
    store.replace_members(group["group_id"], [managed_id])
    armed = store.arm_group(group["group_id"], now=BASE)
    baseline = armed["baseline_epoch"]

    new_boot, new_instance = uuid4(), uuid4()
    current["now"] = BASE + timedelta(seconds=1)
    store.register_agent(
        report(
            agent_id,
            new_boot,
            new_instance,
            generation=2,
            services=[service("mysql")],
        ),
        "10.20.0.11",
        now=current["now"],
    )
    first = store.evaluate_group_candidate(group["group_id"], current["now"])
    assert first["decision"] == "SETTLING"
    assert first["epoch"] != baseline

    current["now"] = BASE + timedelta(seconds=2)
    store.ingest_heartbeat(
        agent_id,
        report(
            agent_id,
            new_boot,
            new_instance,
            generation=2,
            sequence=2,
            services=[service("mysql")],
        ),
        "10.20.0.11",
        now=current["now"],
    )
    ready = store.evaluate_group_candidate(group["group_id"], current["now"])
    assert ready["decision"] == "READY"

    run = store.create_run_from_group(
        group["group_id"], "AUTO", ready["epoch"], now=current["now"]
    )
    duplicate = store.create_run_from_group(
        group["group_id"], "AUTO", ready["epoch"], now=current["now"]
    )
    assert duplicate["run_id"] == run["run_id"]
    assert run["members_snapshot"][0]["managed_service_id"] == managed_id
    assert run["steps"][0]["topology_level"] == 0
    assert store.service_lock(managed_id)["run_id"] == run["run_id"]


def test_second_boot_change_fences_stale_ready_epoch_before_auto_run(tmp_path) -> None:
    current = {"now": BASE}
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: current["now"])
    agent_id = uuid4()
    store.register_agent(
        report(agent_id, uuid4(), uuid4(), services=[service("mysql")]),
        "10.20.0.11",
        now=BASE,
    )
    managed_id = store.list_services(now=BASE)[0]["managed_service_id"]
    group = store.create_group({"name": "core", "node_settle_window_seconds": 1})
    store.replace_members(group["group_id"], [managed_id])
    store.arm_group(group["group_id"], now=BASE)

    first_new_boot, first_instance = uuid4(), uuid4()
    current["now"] = BASE + timedelta(seconds=1)
    store.register_agent(
        report(
            agent_id,
            first_new_boot,
            first_instance,
            generation=2,
            services=[service("mysql")],
        ),
        "10.20.0.11",
        now=current["now"],
    )
    store.evaluate_group_candidate(group["group_id"], current["now"])
    current["now"] = BASE + timedelta(seconds=2)
    ready = store.evaluate_group_candidate(group["group_id"], current["now"])
    assert ready["decision"] == "READY"

    # A second reboot lands after READY was read but before Run creation.
    current["now"] = BASE + timedelta(seconds=2, milliseconds=100)
    store.register_agent(
        report(
            agent_id,
            uuid4(),
            uuid4(),
            generation=3,
            services=[service("mysql")],
        ),
        "10.20.0.11",
        now=current["now"],
    )
    with pytest.raises(ApiError) as stale_epoch:
        store.create_run_from_group(
            group["group_id"], "AUTO", ready["epoch"], now=current["now"]
        )
    assert stale_epoch.value.code is ErrorCode.GROUP_NOT_READY
    with store.db.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM recovery_runs").fetchone()[0] == 0
    reset = store.evaluate_group_candidate(group["group_id"], current["now"])
    assert reset["decision"] == "SETTLING"
    assert reset["epoch"] != ready["epoch"]


def test_succeeded_run_cannot_be_retried(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: BASE)
    agent_id = uuid4()
    store.register_agent(
        report(agent_id, uuid4(), uuid4(), services=[service("redis")]),
        "10.20.0.11",
        now=BASE,
    )
    managed_id = store.list_services(now=BASE)[0]["managed_service_id"]
    group = store.create_group({"name": "core"})
    store.replace_members(group["group_id"], [managed_id])
    run = store.create_run_from_group(group["group_id"], "MANUAL")
    store.finish_run(run["run_id"], "SUCCEEDED")

    with pytest.raises(ApiError) as rejected:
        store.retry_run(run["run_id"])
    assert rejected.value.code is ErrorCode.GROUP_NOT_READY


def test_blocked_precondition_cannot_be_bypassed_by_manual_run(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: BASE)
    agent_id = uuid4()
    store.register_agent(
        report(agent_id, uuid4(), uuid4(), services=[service("mysql")]),
        "10.20.0.11",
        now=BASE,
    )
    managed_id = store.list_services(now=BASE)[0]["managed_service_id"]
    group = store.create_group({"name": "core"})
    store.replace_members(group["group_id"], [managed_id])
    store.arm_group(group["group_id"], now=BASE)
    with store.db.transaction() as connection:
        connection.execute(
            """
            UPDATE recovery_groups
            SET state='BLOCKED_PRECONDITION',
                blocked_reasons_json='[{"code":"TEST_BLOCK","message":"test isolation"}]'
            WHERE group_id=?
            """,
            (group["group_id"],),
        )

    with pytest.raises(ApiError) as isolated:
        store.create_run_from_group(group["group_id"], "MANUAL", now=BASE)
    assert isolated.value.code is ErrorCode.GROUP_NOT_READY
    assert store.get_group(group["group_id"], now=BASE)["state"] == "BLOCKED_PRECONDITION"

    store.disarm_group(group["group_id"], now=BASE)
    run = store.create_run_from_group(group["group_id"], "MANUAL", now=BASE)
    assert run["status"] == "PENDING"


def test_run_dispatch_journal_attempts_and_terminal_release_are_atomic(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: BASE)
    agent_id = uuid4()
    store.register_agent(
        report(agent_id, uuid4(), uuid4(), services=[service("redis")]),
        "10.20.0.11",
        now=BASE,
    )
    managed_id = store.list_services(now=BASE)[0]["managed_service_id"]
    first_group = store.create_group({"name": "first"})
    second_group = store.create_group({"name": "second"})
    store.replace_members(first_group["group_id"], [managed_id])
    store.replace_members(second_group["group_id"], [managed_id])
    run = store.create_run_from_group(first_group["group_id"], "MANUAL", reason="test")

    with pytest.raises(ApiError) as locked:
        store.create_run_from_group(second_group["group_id"], "MANUAL")
    assert locked.value.code is ErrorCode.SERVICE_IN_ACTIVE_RUN

    step_id = run["steps"][0]["step_id"]
    first_key, second_key = uuid4(), uuid4()
    assert store.assign_step_dispatch(step_id, first_key) == str(first_key)
    assert store.assign_step_dispatch(step_id, second_key) == str(first_key)
    operation_id = uuid4()
    assert store.assign_step_operation(step_id, operation_id) == str(operation_id)
    store.record_probe_attempt(
        step_id,
        {
            "passed": True,
            "observed_at": BASE.isoformat(),
            "latency_ms": 4,
            "code": "READY",
            "message": "ready",
        },
        BASE,
        BASE + timedelta(milliseconds=4),
    )
    persisted = store.get_run(run["run_id"])
    assert persisted["steps"][0]["dispatch_idempotency_key"] == str(first_key)
    assert persisted["steps"][0]["operation_id"] == str(operation_id)
    assert persisted["steps"][0]["probe_attempts"][0]["result"]["passed"] is True

    store.finish_run(run["run_id"], "SUCCEEDED")
    assert store.service_lock(managed_id) is None
    assert store.get_group(first_group["group_id"])["state"] == "DISARMED"
    next_run = store.create_run_from_group(second_group["group_id"], "MANUAL")
    assert next_run["run_id"] != run["run_id"]


def test_state_domains_and_terminal_outcomes_are_enforced_in_store_and_sqlite(
    tmp_path,
) -> None:
    store = ControlPlaneStore(tmp_path / "cp.sqlite3", now=lambda: BASE)
    with store.db.read() as connection:
        assert connection.execute(
            "SELECT version FROM schema_versions WHERE component='control-plane'"
        ).fetchone()[0] == 5

    agent_id = uuid4()
    store.register_agent(
        report(agent_id, uuid4(), uuid4(), services=[service("mysql")]),
        "10.20.0.11",
        now=BASE,
    )
    managed_id = store.list_services()[0]["managed_service_id"]
    group = store.create_group({"name": "state-guards"})
    store.replace_members(group["group_id"], [managed_id])
    run = store.create_run_from_group(group["group_id"], "MANUAL")
    step_id = run["steps"][0]["step_id"]

    with pytest.raises(ValueError, match="invalid RecoveryRun status"):
        store.update_run(run["run_id"], "NOT_A_STATUS")
    with pytest.raises(ValueError, match="invalid RecoveryStep status"):
        store.update_step(step_id, "NOT_A_STATUS")
    with pytest.raises(sqlite3.IntegrityError, match="invalid recovery_groups.state"):
        with store.db.transaction() as connection:
            connection.execute(
                "UPDATE recovery_groups SET state='NOT_A_STATE' WHERE group_id=?",
                (group["group_id"],),
            )
    with pytest.raises(sqlite3.IntegrityError, match="blocked_reasons invariant"):
        with store.db.transaction() as connection:
            connection.execute(
                "UPDATE recovery_groups SET state='BLOCKED_PRECONDITION' WHERE group_id=?",
                (group["group_id"],),
            )

    store.update_run(run["run_id"], "RUNNING")
    with pytest.raises(ValueError, match="cannot return to PENDING"):
        store.update_run(run["run_id"], "PENDING")
    store.update_step(step_id, "READY")
    with pytest.raises(ValueError, match="terminal RecoveryStep"):
        store.update_step(step_id, "PROBING")
    with pytest.raises(sqlite3.IntegrityError, match="terminal recovery_steps.status"):
        with store.db.transaction() as connection:
            connection.execute(
                "UPDATE recovery_steps SET status='PROBING' WHERE step_id=?",
                (step_id,),
            )

    store.finish_run(run["run_id"], "SUCCEEDED")
    assert store.finish_run(run["run_id"], "SUCCEEDED")["status"] == "SUCCEEDED"
    with pytest.raises(ValueError, match="terminal RecoveryRun"):
        store.finish_run(run["run_id"], "FAILED")
    with pytest.raises(ValueError, match="terminal RecoveryRun"):
        store.update_run(run["run_id"], "RUNNING")
    with pytest.raises(sqlite3.IntegrityError, match="terminal recovery_runs.status"):
        with store.db.transaction() as connection:
            connection.execute(
                "UPDATE recovery_runs SET status='RUNNING' WHERE run_id=?",
                (run["run_id"],),
            )


def test_v2_migration_rejects_legacy_invalid_state_atomically(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "legacy.sqlite3")
    database.initialize("control-plane", [_migration_v1])
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO recovery_groups(
                group_id, name, description, state, node_settle_window_seconds,
                max_parallel_services, created_at, updated_at
            ) VALUES(?, 'legacy', '', 'INVALID_LEGACY_STATE', 120, 4, ?, ?)
            """,
            (str(uuid4()), BASE.isoformat(), BASE.isoformat()),
        )

    with pytest.raises(sqlite3.IntegrityError, match="cannot migrate"):
        ControlPlaneStore(database)

    with database.read() as connection:
        assert connection.execute(
            "SELECT version FROM schema_versions WHERE component='control-plane'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'enforce_%'"
        ).fetchone()[0] == 0


def test_auto_precondition_reasons_persist_until_disarm(tmp_path) -> None:
    current = {"now": BASE}
    ticks = {"now": 0.0}
    database = tmp_path / "blocked.sqlite3"
    store = ControlPlaneStore(
        database,
        now=lambda: current["now"],
        monotonic=lambda: ticks["now"],
    )
    agent_id, old_boot, old_instance = uuid4(), uuid4(), uuid4()
    store.register_agent(
        report(
            agent_id,
            old_boot,
            old_instance,
            services=[service("mysql")],
        ),
        "10.20.0.11",
        now=BASE,
    )
    managed_id = store.list_services()[0]["managed_service_id"]
    group = store.create_group(
        {"name": "blocked-reasons", "node_settle_window_seconds": 1}
    )
    store.replace_members(group["group_id"], [managed_id])
    store.arm_group(group["group_id"], now=BASE)

    changed_service = service("mysql")
    changed_service["startup_state"] = "AUTOSTART_ENABLED"
    current["now"] = BASE + timedelta(seconds=1)
    store.register_agent(
        report(
            agent_id,
            uuid4(),
            uuid4(),
            generation=2,
            services=[changed_service],
        ),
        "10.20.0.11",
        now=current["now"],
    )
    assert store.evaluate_group_candidate(group["group_id"], current["now"])[
        "decision"
    ] == "SETTLING"
    current["now"] += timedelta(seconds=1)
    blocked = store.evaluate_group_candidate(group["group_id"], current["now"])
    assert blocked["decision"] == "BLOCKED"

    persisted = store.get_group(group["group_id"])
    assert persisted["state"] == "BLOCKED_PRECONDITION"
    assert persisted["blocked_reasons"] == [
        {
            "agent_id": str(agent_id),
            "code": "STARTUP_NOT_MANUAL",
            "managed_service_id": managed_id,
            "message": "Service startup must be manual",
        }
    ]

    restarted = ControlPlaneStore(
        database,
        now=lambda: current["now"],
        monotonic=lambda: ticks["now"],
    )
    with restarted.db.transaction() as connection:
        connection.execute(
            "UPDATE services SET startup_state='AUTOSTART_DISABLED' WHERE managed_service_id=?",
            (managed_id,),
        )
    still_blocked = restarted.evaluate_group_candidate(
        group["group_id"], current["now"]
    )
    assert still_blocked["decision"] == "UNCHANGED"
    assert restarted.get_group(group["group_id"])["blocked_reasons"] == persisted[
        "blocked_reasons"
    ]

    disarmed = restarted.disarm_group(group["group_id"], now=current["now"])
    assert disarmed["state"] == "DISARMED"
    assert disarmed["blocked_reasons"] == []


def test_auto_precondition_reasons_are_bounded_with_deterministic_summary(
    tmp_path,
) -> None:
    current = {"now": BASE}
    store = ControlPlaneStore(tmp_path / "bounded-blocked.sqlite3", now=lambda: current["now"])
    agent_id = uuid4()
    initial_services = [service(f"svc-{index:03d}") for index in range(101)]
    store.register_agent(
        report(
            agent_id,
            uuid4(),
            uuid4(),
            services=initial_services,
        ),
        "10.20.0.11",
        now=BASE,
    )
    managed_ids = [item["managed_service_id"] for item in store.list_services()]
    group = store.create_group(
        {"name": "bounded-blocked-reasons", "node_settle_window_seconds": 1}
    )
    store.replace_members(group["group_id"], managed_ids)
    store.arm_group(group["group_id"], now=BASE)

    invalid_services = []
    for item in initial_services:
        changed = dict(item)
        changed["startup_state"] = "AUTOSTART_ENABLED"
        invalid_services.append(changed)
    current["now"] = BASE + timedelta(seconds=1)
    store.register_agent(
        report(
            agent_id,
            uuid4(),
            uuid4(),
            generation=2,
            services=invalid_services,
        ),
        "10.20.0.11",
        now=current["now"],
    )
    assert store.evaluate_group_candidate(group["group_id"], current["now"])[
        "decision"
    ] == "SETTLING"

    current["now"] += timedelta(seconds=1)
    blocked = store.evaluate_group_candidate(group["group_id"], current["now"])
    assert blocked["decision"] == "BLOCKED"
    assert len(blocked["errors"]) == 100
    assert all(
        item["code"] == "STARTUP_NOT_MANUAL" for item in blocked["errors"][:-1]
    )
    assert blocked["errors"][-1] == {
        "code": "PRECONDITION_ISSUES_TRUNCATED",
        "message": "2 additional precondition issues were omitted",
    }
    assert [item["managed_service_id"] for item in blocked["errors"][:-1]] == sorted(
        item["managed_service_id"] for item in blocked["errors"][:-1]
    )

    persisted = store.get_group(group["group_id"])
    assert persisted["state"] == "BLOCKED_PRECONDITION"
    assert persisted["blocked_reasons"] == blocked["errors"]


def test_v3_migration_marks_legacy_blocked_reason_as_unavailable(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "legacy-blocked.sqlite3")
    database.initialize("control-plane", [_migration_v1, _migration_v2])
    group_id = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO recovery_groups(
                group_id, name, description, state, node_settle_window_seconds,
                max_parallel_services, created_at, updated_at
            ) VALUES(?, 'legacy-blocked', '', 'BLOCKED_PRECONDITION', 120, 4, ?, ?)
            """,
            (group_id, BASE.isoformat(), BASE.isoformat()),
        )

    upgraded = ControlPlaneStore(database)
    group = upgraded.get_group(group_id)
    assert group["blocked_reasons"] == [
        {
            "code": "LEGACY_REASON_UNAVAILABLE",
            "message": "Blocked reason was not persisted before schema v3",
        }
    ]


@pytest.mark.parametrize(
    "blocked_reasons",
    [
        ["not-an-object"],
        [{"code": "", "message": "message"}],
        [{"code": "CODE", "message": ""}],
        [{"code": "CODE", "message": "message", "unknown": True}],
        [
            {
                "code": "CODE",
                "message": "message",
                "managed_service_id": "00000000-0000-0000-0000-000000000000",
            }
        ],
        [
            {
                "code": "CODE",
                "message": "message",
                "managed_service_ids": [str(uuid4()), str(uuid4())],
            }
        ],
        [{"code": "CODE", "message": "message", "managed_service_ids": None}],
        [{"code": "\x00" + ("x" * 100), "message": "message"}],
        [
            {
                "code": "CODE",
                "message": "message",
                "managed_service_id": str(uuid4()) + "\x00junk",
            }
        ],
        [{"code": "CODE", "message": "\ud800"}],
        [{"code": "CODE", "message": "message"}] * 101,
    ],
)
def test_v4_rejects_invalid_blocked_reason_shapes(
    tmp_path, blocked_reasons: list[object]
) -> None:
    # Make the generated two-ID case a deterministic case-insensitive duplicate.
    if (
        len(blocked_reasons) == 1
        and isinstance(blocked_reasons[0], dict)
        and isinstance(blocked_reasons[0].get("managed_service_ids"), list)
        and len(blocked_reasons[0]["managed_service_ids"]) == 2
    ):
        duplicate = str(uuid4())
        blocked_reasons[0]["managed_service_ids"] = [duplicate, duplicate.upper()]

    store = ControlPlaneStore(tmp_path / "v4-shape.sqlite3")
    group = store.create_group({"name": "v4-shape"})
    with pytest.raises(sqlite3.IntegrityError, match="blocked_reasons invariant"):
        with store.db.transaction() as connection:
            connection.execute(
                """
                UPDATE recovery_groups
                SET state='BLOCKED_PRECONDITION', blocked_reasons_json=?
                WHERE group_id=?
                """,
                (json.dumps(blocked_reasons), group["group_id"]),
            )
    assert store.get_group(group["group_id"])["state"] == "DISARMED"


def test_v4_accepts_strict_blocked_reason_with_nullable_ids(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "v4-valid.sqlite3")
    group = store.create_group({"name": "v4-valid"})
    service_id = str(uuid4())
    reason = {
        "code": "TEST_BLOCK",
        "message": "strict persisted issue",
        "managed_service_id": None,
        "agent_id": None,
        "managed_service_ids": [service_id],
    }
    with store.db.transaction() as connection:
        connection.execute(
            """
            UPDATE recovery_groups
            SET state='BLOCKED_PRECONDITION', blocked_reasons_json=?
            WHERE group_id=?
            """,
            (json.dumps([reason]), group["group_id"]),
        )
    assert store.get_group(group["group_id"])["blocked_reasons"] == [reason]


def test_v4_accepts_literal_surrogate_escape_text(tmp_path) -> None:
    store = ControlPlaneStore(tmp_path / "v4-literal-escape.sqlite3")
    group = store.create_group({"name": "v4-literal-escape"})
    reason = {"code": "CODE", "message": r"literal \ud800 text"}
    with store.db.transaction() as connection:
        connection.execute(
            """
            UPDATE recovery_groups
            SET state='BLOCKED_PRECONDITION', blocked_reasons_json=?
            WHERE group_id=?
            """,
            (json.dumps([reason]), group["group_id"]),
        )
    assert store.get_group(group["group_id"])["blocked_reasons"] == [reason]


@pytest.mark.parametrize("cast_as_text", [False, True])
def test_v4_rejects_non_utf8_blob_storage(tmp_path, cast_as_text: bool) -> None:
    store = ControlPlaneStore(tmp_path / f"v4-blob-{cast_as_text}.sqlite3")
    group = store.create_group({"name": "v4-blob"})
    invalid = b'[{"code":"C","message":"\xed\xa0\x80"}]'
    value_sql = "CAST(? AS TEXT)" if cast_as_text else "?"
    with pytest.raises((sqlite3.IntegrityError, sqlite3.OperationalError)):
        with store.db.transaction() as connection:
            connection.execute(
                f"""
                UPDATE recovery_groups
                SET state='BLOCKED_PRECONDITION', blocked_reasons_json={value_sql}
                WHERE group_id=?
                """,
                (invalid, group["group_id"]),
            )
    assert store.get_group(group["group_id"])["state"] == "DISARMED"


def test_v4_migration_rejects_dirty_v3_atomically(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "dirty-v3.sqlite3")
    database.initialize("control-plane", [_migration_v1, _migration_v2, _migration_v3])
    group_id = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO recovery_groups(
                group_id, name, description, state, node_settle_window_seconds,
                max_parallel_services, created_at, updated_at, blocked_reasons_json
            ) VALUES(?, 'dirty-v3', '', 'BLOCKED_PRECONDITION', 120, 4, ?, ?, ?)
            """,
            (
                group_id,
                BASE.isoformat(),
                BASE.isoformat(),
                '[{"code":"","message":"invalid but accepted by v3"}]',
            ),
        )

    with pytest.raises(sqlite3.IntegrityError, match="cannot migrate"):
        ControlPlaneStore(database)

    with database.read() as connection:
        version = connection.execute(
            "SELECT version FROM schema_versions WHERE component='control-plane'"
        ).fetchone()[0]
        triggers = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='trigger'
              AND name LIKE 'enforce_recovery_groups_blocked_reasons_%'
            """
        ).fetchone()[0]
    assert version == 3
    assert triggers == 2


def test_v4_migration_rejects_unpaired_surrogate_atomically(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "surrogate-v3.sqlite3")
    database.initialize("control-plane", [_migration_v1, _migration_v2, _migration_v3])
    group_id = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO recovery_groups(
                group_id, name, description, state, node_settle_window_seconds,
                max_parallel_services, created_at, updated_at, blocked_reasons_json
            ) VALUES(?, 'surrogate-v3', '', 'BLOCKED_PRECONDITION', 120, 4, ?, ?, ?)
            """,
            (
                group_id,
                BASE.isoformat(),
                BASE.isoformat(),
                r'[{"code":"CODE","message":"\ud800"}]',
            ),
        )

    with pytest.raises(sqlite3.IntegrityError, match="cannot migrate"):
        ControlPlaneStore(database)
    with database.read() as connection:
        assert connection.execute(
            "SELECT version FROM schema_versions WHERE component='control-plane'"
        ).fetchone()[0] == 3


def test_v4_migration_normalizes_valid_surrogate_pair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "emoji-v3.sqlite3")
    database.initialize("control-plane", [_migration_v1, _migration_v2, _migration_v3])
    group_id = str(uuid4())
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO recovery_groups(
                group_id, name, description, state, node_settle_window_seconds,
                max_parallel_services, created_at, updated_at, blocked_reasons_json
            ) VALUES(?, 'emoji-v3', '', 'BLOCKED_PRECONDITION', 120, 4, ?, ?, ?)
            """,
            (
                group_id,
                BASE.isoformat(),
                BASE.isoformat(),
                r'[{"code":"CODE","message":"\ud83d\ude00"}]',
            ),
        )

    store = ControlPlaneStore(database)
    assert store.get_group(group_id)["blocked_reasons"][0]["message"] == "\U0001f600"
    with database.read() as connection:
        raw = connection.execute(
            "SELECT blocked_reasons_json FROM recovery_groups WHERE group_id=?",
            (group_id,),
        ).fetchone()[0]
    assert "\U0001f600" in raw
    assert "\\ud83d" not in raw.lower()


def test_v4_migration_normalizes_noncanonical_uuid(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "uuid-v3.sqlite3")
    database.initialize("control-plane", [_migration_v1, _migration_v2, _migration_v3])
    group_id = str(uuid4())
    service_id = uuid4()
    reason = {
        "code": "CODE",
        "message": "normalize UUID",
        "managed_service_id": service_id.hex.upper(),
    }
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO recovery_groups(
                group_id, name, description, state, node_settle_window_seconds,
                max_parallel_services, created_at, updated_at, blocked_reasons_json
            ) VALUES(?, 'uuid-v3', '', 'BLOCKED_PRECONDITION', 120, 4, ?, ?, ?)
            """,
            (
                group_id,
                BASE.isoformat(),
                BASE.isoformat(),
                json.dumps([reason]),
            ),
        )

    store = ControlPlaneStore(database)
    persisted = store.get_group(group_id)["blocked_reasons"][0]
    assert persisted["managed_service_id"] == str(service_id)
