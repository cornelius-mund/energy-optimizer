"""Tests for scheduled provider retrieval and optimization triggers."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from pydantic import TypeAdapter

from energy_optimizer.config import (
    DataSourceScheduleConfiguration,
    OptimizationTriggerConfiguration,
    OrchestrationConfiguration,
)
from energy_optimizer.orchestration import (
    OrchestrationError,
    ProviderDataSnapshot,
    ProviderOrchestrator,
    ProviderRegistration,
)
from energy_optimizer.providers.interfaces import HouseholdLoadData, SourceMetadata
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ADAPTER = TypeAdapter(HouseholdLoadData)


def data(
    now: datetime, value: float = 1.0, source: str = "household_load"
) -> HouseholdLoadData:
    return HouseholdLoadData(
        schema_version="1",
        start_time=now,
        interval_minutes=60,
        load_kw=(value,),
        unit="kW",
        source=SourceMetadata(provider=source, entity_id=f"sensor.{source}"),
        retrieved_at=now,
        latest_observation_at=now,
    )


def registration(
    fetch: Any,
    source: str = "household_load",
    data_type: str = "household-load",
) -> ProviderRegistration:
    return ProviderRegistration(
        name=source,
        data_type=data_type,
        adapter=ADAPTER,
        fetch=fetch,
        is_fresh=lambda value, now: (
            isinstance(value, HouseholdLoadData)
            and value.latest_observation_at >= now - timedelta(minutes=90)
        ),
    )


def configuration(
    *,
    interval_seconds: float = 300,
    startup_fetch: bool = True,
    optimization_enabled: bool = False,
) -> OrchestrationConfiguration:
    return OrchestrationConfiguration(
        enabled=True,
        startup_fetch=startup_fetch,
        sources={
            "household_load": DataSourceScheduleConfiguration(
                interval_seconds=interval_seconds,
                horizon_hours=1,
            )
        },
        optimization=OptimizationTriggerConfiguration(
            enabled=optimization_enabled,
            required_sources=["household_load"] if optimization_enabled else [],
        ),
    )


def test_startup_fetch_persists_normalized_data(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        calls.append(now)
        return data(now)

    orchestrator = ProviderOrchestrator(
        configuration(), [registration(fetch)], ProviderDataStore(tmp_path)
    )

    cycle = orchestrator.run_due(START)

    assert calls == [START]
    assert cycle.provider_runs[0].status == "success"
    key = ProviderDataKey("household-load", "household_load", "sensor.household_load")
    persisted = ProviderDataStore(tmp_path).load(key, ADAPTER)
    assert persisted is not None
    assert persisted.load_kw == (1.0,)


def test_source_is_not_fetched_until_its_interval_elapses(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        calls.append(now)
        return data(now)

    orchestrator = ProviderOrchestrator(
        configuration(interval_seconds=300),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
    )

    orchestrator.run_due(START)
    not_due = orchestrator.run_due(START + timedelta(seconds=299))
    due = orchestrator.run_due(START + timedelta(seconds=300))

    assert calls == [START, START + timedelta(seconds=300)]
    assert not_due.provider_runs[0].status == "skipped"
    assert due.provider_runs[0].status == "success"


def test_failed_refresh_preserves_last_persisted_data(tmp_path: Path) -> None:
    should_fail = False

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        if should_fail:
            raise RuntimeError("provider unavailable")
        return data(now, value=2.0)

    orchestrator = ProviderOrchestrator(
        configuration(interval_seconds=60),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
    )
    orchestrator.run_due(START)
    should_fail = True
    cycle = orchestrator.run_due(START + timedelta(seconds=60))

    assert cycle.provider_runs[0].status == "failed"
    assert cycle.provider_runs[0].error == "provider unavailable"
    key = ProviderDataKey("household-load", "household_load", "sensor.household_load")
    persisted = ProviderDataStore(tmp_path).load(key, ADAPTER)
    assert persisted is not None
    assert persisted.load_kw == (2.0,)


def test_failed_required_refresh_does_not_create_plan(tmp_path: Path) -> None:
    should_fail = False
    plans: list[ProviderDataSnapshot] = []

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        if should_fail:
            raise RuntimeError("provider unavailable")
        return data(now)

    def generate(snapshot: ProviderDataSnapshot) -> object:
        plans.append(snapshot)
        return snapshot

    orchestrator = ProviderOrchestrator(
        configuration(interval_seconds=60, optimization_enabled=True),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
        plan_generator=generate,
    )
    orchestrator.run_due(START)
    should_fail = True
    cycle = orchestrator.run_due(START + timedelta(seconds=60))

    assert cycle.plan_status == "not-ready"
    assert len(plans) == 1


def test_concurrent_cycle_is_skipped_without_duplicate_fetch(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()
    calls: list[datetime] = []

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        calls.append(now)
        started.set()
        release.wait(timeout=5)
        return data(now)

    orchestrator = ProviderOrchestrator(
        configuration(),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
    )
    first_result: list[object] = []

    def run_first_cycle() -> None:
        first_result.append(orchestrator.run_due(START))

    thread = Thread(target=run_first_cycle)
    thread.start()
    assert started.wait(timeout=5)

    concurrent_cycle = orchestrator.run_due(START)

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert concurrent_cycle.provider_runs[0].status == "skipped"
    assert len(calls) == 1
    assert len(first_result) == 1


def test_disabled_optimization_does_not_call_plan_generator(tmp_path: Path) -> None:
    plans: list[ProviderDataSnapshot] = []

    def generate(snapshot: ProviderDataSnapshot) -> object:
        plans.append(snapshot)
        return snapshot

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        return data(now)

    orchestrator = ProviderOrchestrator(
        configuration(),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
        plan_generator=generate,
    )

    cycle = orchestrator.run_due(START)

    assert cycle.plan_status == "disabled"
    assert plans == []


def test_optimization_reports_unavailable_without_plan_generator(
    tmp_path: Path,
) -> None:
    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        return data(now)

    orchestrator = ProviderOrchestrator(
        configuration(optimization_enabled=True),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
    )

    cycle = orchestrator.run_due(START)

    assert cycle.plan_status == "unavailable"
    assert cycle.plan_error == "optimization plan generator is not configured"


def test_complete_fresh_refresh_creates_one_plan_snapshot(tmp_path: Path) -> None:
    plans: list[ProviderDataSnapshot] = []

    def generate(snapshot: ProviderDataSnapshot) -> object:
        plans.append(snapshot)
        return snapshot

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        return data(now, value=3.5)

    orchestrator = ProviderOrchestrator(
        configuration(optimization_enabled=True),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
        plan_generator=generate,
    )

    cycle = orchestrator.run_due(START)

    assert cycle.plan_status == "created"
    assert len(plans) == 1
    assert plans[0].captured_at == START
    provider_data = plans[0].data["household_load"]
    assert isinstance(provider_data, HouseholdLoadData)
    assert provider_data.load_kw == (3.5,)


def test_plan_generation_failure_is_reported(tmp_path: Path) -> None:
    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        return data(now)

    def generate(_: ProviderDataSnapshot) -> object:
        raise RuntimeError("solver unavailable")

    orchestrator = ProviderOrchestrator(
        configuration(optimization_enabled=True),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
        plan_generator=generate,
    )

    cycle = orchestrator.run_due(START)

    assert cycle.plan_status == "failed"
    assert cycle.plan_error == "solver unavailable"


def test_restarted_orchestrator_loads_persisted_data(tmp_path: Path) -> None:
    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        return data(now, value=4.0)

    first = ProviderOrchestrator(
        configuration(),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
    )
    first.run_due(START)

    loaded: list[object | None] = []

    def load() -> object | None:
        persisted = ProviderDataStore(tmp_path).load(
            ProviderDataKey(
                "household-load", "household_load", "sensor.household_load"
            ),
            ADAPTER,
        )
        loaded.append(persisted)
        return persisted

    ProviderOrchestrator(
        configuration(),
        [
            ProviderRegistration(
                name="household_load",
                data_type="household-load",
                adapter=ADAPTER,
                fetch=fetch,
                is_fresh=lambda value, _: isinstance(value, HouseholdLoadData),
                load=load,
            )
        ],
        ProviderDataStore(tmp_path),
    )

    assert loaded
    persisted = loaded[0]
    assert isinstance(persisted, HouseholdLoadData)
    assert persisted.load_kw == (4.0,)


def test_stale_refresh_does_not_create_plan(tmp_path: Path) -> None:
    plans: list[ProviderDataSnapshot] = []

    def generate(snapshot: ProviderDataSnapshot) -> object:
        plans.append(snapshot)
        return snapshot

    def fetch(_: datetime, __: Any) -> HouseholdLoadData:
        return data(START - timedelta(hours=2))

    orchestrator = ProviderOrchestrator(
        configuration(optimization_enabled=True),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
        plan_generator=generate,
    )

    cycle = orchestrator.run_due(START)

    assert cycle.provider_runs[0].status == "stale"
    assert cycle.plan_status == "not-ready"
    assert plans == []


def test_orchestrator_rejects_unregistered_configured_source(tmp_path: Path) -> None:
    configured = OrchestrationConfiguration(
        enabled=True,
        sources={"unknown": DataSourceScheduleConfiguration(interval_seconds=60)},
    )

    with pytest.raises(OrchestrationError, match="unknown"):
        ProviderOrchestrator(configured, [], ProviderDataStore(tmp_path))
