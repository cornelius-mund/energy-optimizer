"""Tests for scheduled provider retrieval and optimization triggers."""

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter
from pytest import LogCaptureFixture

from energy_optimizer.config import (
    AwattarConfiguration,
    Configuration,
    DataSourceScheduleConfiguration,
    ForecastSolarConfiguration,
    GridConfiguration,
    HomeAssistantConfiguration,
    OptimizationTriggerConfiguration,
    OrchestrationConfiguration,
    PersistenceConfiguration,
    SolverConfiguration,
)
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
    HourExclusion,
)
from energy_optimizer.orchestration import (
    OrchestrationError,
    ProviderDataSnapshot,
    ProviderOrchestrator,
    ProviderRegistration,
    build_configured_orchestrator,
)
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    HomeAssistantBatteryEfficiencyImporter,
)
from energy_optimizer.providers.home_assistant_history import HistoryPlan
from energy_optimizer.providers.interfaces import (
    BatteryData,
    BatteryEfficiencyData,
    BatteryEfficiencyHistoryData,
    ElectricityPriceData,
    GridFlowData,
    HouseholdLoadData,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore
from home_assistant_fixtures import (
    FakeHomeAssistant,
    home_assistant_history_payload,
    home_assistant_jittery_total_readings,
    plan_without_needs,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ADAPTER = TypeAdapter(HouseholdLoadData)
PV_ADAPTER = TypeAdapter(PvGenerationData)
GRID_FLOW_ADAPTER = TypeAdapter(GridFlowData)
BATTERY_ADAPTER = TypeAdapter(BatteryData)


def data(
    now: datetime,
    value: float = 1.0,
    source: str = "household_load",
    entity_id: str | None = None,
) -> HouseholdLoadData:
    return HouseholdLoadData(
        schema_version="1",
        start_time=now.replace(minute=0, second=0, microsecond=0),
        interval_minutes=60,
        load_kw=(value,),
        unit="kW",
        source=SourceMetadata(
            provider=source, entity_id=entity_id or f"sensor.{source}"
        ),
        retrieved_at=now,
        latest_observation_at=now,
    )


def exclusion(
    hour_start: datetime,
    *reasons: ExclusionReason,
    entity_id: str = "sensor.household_energy",
) -> HourExclusion:
    """Build one excluded hour with one cause per reason."""
    return HourExclusion(
        hour_start,
        tuple(
            ExclusionCause.of(
                reason,
                f"{entity_id} is excluded ({reason})",
                entity_id,
                [ExcludedDataPoint(hour_start + timedelta(minutes=30), "raw")],
            )
            for reason in reasons
        ),
    )


def pv_data(now: datetime, value: float = 1.0) -> PvGenerationData:
    start = now.replace(minute=0, second=0, microsecond=0)
    return PvGenerationData(
        schema_version="1",
        start_time=start,
        interval_minutes=60,
        generation_kw=(value,),
        unit="kW",
        source=SourceMetadata(provider="forecast.solar", entity_id="pv_generation"),
        retrieved_at=now,
        expires_at=start + timedelta(hours=24),
    )


def grid_flow_data(now: datetime, value: float = 1.0) -> GridFlowData:
    start = now.replace(minute=0, second=0, microsecond=0)
    return GridFlowData(
        schema_version="1",
        start_time=start,
        interval_minutes=60,
        import_kw=(value,),
        export_kw=(value / 2,),
        unit="kW",
        source=SourceMetadata(provider="home-assistant", entity_id="grid_flow"),
        retrieved_at=now,
        latest_observation_at=now,
    )


def battery_data(now: datetime) -> BatteryData:
    return BatteryData(
        schema_version="1",
        start_time=now,
        interval_minutes=60,
        state_of_charge_kwh=(5.0,),
        capacity_kwh=10.0,
        minimum_soc_kwh=2.0,
        maximum_soc_kwh=10.0,
        initial_soc_kwh=5.0,
        maximum_charge_kw=4.0,
        maximum_discharge_kw=4.0,
        battery_efficiency=0.95,
        unit="kWh",
        power_unit="kW",
        source=SourceMetadata(provider="home-assistant", entity_id="battery"),
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
        plan=lambda now, schedule: plan_without_needs(lambda: fetch(now, schedule)),
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
                plan=lambda now, schedule: plan_without_needs(
                    lambda: fetch(now, schedule)
                ),
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


def test_refresh_with_excluded_hours_succeeds_and_does_not_block_the_plan(
    tmp_path: Path,
) -> None:
    plans: list[ProviderDataSnapshot] = []

    def generate(snapshot: ProviderDataSnapshot) -> object:
        plans.append(snapshot)
        return snapshot

    excluded = exclusion(START, "unavailable")

    def fetch(_: datetime, __: Any) -> HouseholdLoadData:
        return replace(data(START), load_kw=(None,), exclusions=(excluded,))

    store = ProviderDataStore(tmp_path)
    orchestrator = ProviderOrchestrator(
        configuration(optimization_enabled=True),
        [registration(fetch)],
        store,
        plan_generator=generate,
    )

    cycle = orchestrator.run_due(START)

    run = cycle.provider_runs[0]
    assert run.status == "success"
    assert run.error is None
    assert cycle.plan_status == "created"
    assert len(plans) == 1
    persisted = store.load(
        ProviderDataKey("household-load", "household_load", "sensor.household_load"),
        ADAPTER,
    )
    assert persisted is not None
    assert persisted.load_kw == (None,)
    assert persisted.exclusions == (excluded,)


def test_excluded_hours_are_logged_once_per_refresh_with_reason_counts(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    def fetch(_: datetime, __: Any) -> HouseholdLoadData:
        hours = [START + timedelta(hours=hour) for hour in range(4)]
        return replace(
            data(START),
            load_kw=(None, 1.0, None, None),
            exclusions=(
                exclusion(hours[0], "unavailable"),
                exclusion(hours[2], "counter_decrease", "step_after_decrease"),
                exclusion(hours[3], "unavailable", "counter_decrease"),
            ),
        )

    orchestrator = ProviderOrchestrator(
        configuration(),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
    )

    with caplog.at_level(logging.INFO, logger="energy_optimizer.orchestration"):
        cycle = orchestrator.run_due(START)

    assert cycle.provider_runs[0].status == "success"
    summaries = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=provider_hours_excluded")
    ]
    assert len(summaries) == 1
    assert summaries[0].levelno == logging.WARNING
    assert summaries[0].name == "energy_optimizer.orchestration"
    # Every excluded hour counts once per distinct reason.
    assert summaries[0].getMessage() == (
        "event=provider_hours_excluded component=orchestration operation=refresh "
        "source=household_load excluded_hour_count=3 "
        "reasons=counter_decrease:2,step_after_decrease:1,unavailable:2 "
        "first_hour=2026-01-01T00:00:00+00:00 last_hour=2026-01-01T03:00:00+00:00"
    )
    # The summary carries no raw state; the causes themselves are persisted.
    assert "raw" not in summaries[0].getMessage()


def test_no_excluded_hours_summary_is_logged_when_nothing_is_excluded(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        return data(now)

    orchestrator = ProviderOrchestrator(
        configuration(), [registration(fetch)], ProviderDataStore(tmp_path)
    )

    with caplog.at_level(logging.DEBUG, logger="energy_optimizer.orchestration"):
        cycle = orchestrator.run_due(START)

    assert cycle.provider_runs[0].status == "success"
    assert not [
        record
        for record in caplog.records
        if "provider_hours_excluded" in record.getMessage()
    ]


def test_orchestrator_rejects_unregistered_configured_source(tmp_path: Path) -> None:
    configured = OrchestrationConfiguration(
        enabled=True,
        sources={"unknown": DataSourceScheduleConfiguration(interval_seconds=60)},
    )

    with pytest.raises(OrchestrationError, match="unknown"):
        ProviderOrchestrator(configured, [], ProviderDataStore(tmp_path))


def test_configured_home_assistant_orchestrator_uses_aggregate_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeHomeAssistantImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[HouseholdLoadData]:
            del end_time, history_lookback_seconds
            return plan_without_needs(
                lambda: data(
                    now or start_time,
                    source="home-assistant",
                    entity_id="household_load",
                )
            )

        def is_fresh(self, _: HouseholdLoadData, now: datetime) -> bool:
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantLoadImporter",
        FakeHomeAssistantImporter,
    )
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "household_load_entities": [
                    {
                        "entity_id": "sensor.household_energy",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=configuration(),
    )
    store = ProviderDataStore(tmp_path)

    orchestrator = build_configured_orchestrator(runtime_configuration, store)

    assert orchestrator is not None
    cycle = orchestrator.run_due(START)
    assert cycle.provider_runs[0].status == "success"
    persisted = store.load(
        ProviderDataKey("household-load", "home-assistant", "household_load"),
        ADAPTER,
    )
    assert persisted is not None
    assert persisted.source.entity_id == "household_load"


def test_configured_forecast_solar_orchestrator_persists_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeForecastSolarImporter:
        def __init__(self, _: ForecastSolarConfiguration) -> None:
            pass

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            *,
            now: datetime | None = None,
        ) -> PvGenerationData:
            del end_time
            return pv_data(now or start_time)

        def is_fresh(self, _: PvGenerationData, now: datetime) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.ForecastSolarImporter",
        FakeForecastSolarImporter,
    )
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        forecast_solar=ForecastSolarConfiguration(
            latitude=52.52,
            longitude=13.41,
            declination_degrees=35,
            azimuth_degrees=0,
            peak_power_kw=8,
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={
                "pv_generation": DataSourceScheduleConfiguration(interval_seconds=300)
            },
        ),
    )
    store = ProviderDataStore(tmp_path)

    orchestrator = build_configured_orchestrator(runtime_configuration, store)

    assert orchestrator is not None
    cycle = orchestrator.run_due(START)
    assert cycle.provider_runs[0].source == "pv_generation"
    assert cycle.provider_runs[0].status == "success"
    persisted = store.load(
        ProviderDataKey("pv-generation", "forecast.solar", "pv_generation"),
        PV_ADAPTER,
    )
    assert persisted is not None
    assert persisted.generation_kw == (1.0,)


def grid_flow_runtime_configuration(tmp_path: Path) -> Configuration:
    return Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "grid_import_entities": [
                    {
                        "entity_id": "sensor.grid_import",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "grid_export_entities": [
                    {
                        "entity_id": "sensor.grid_export",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={
                "grid_flow": DataSourceScheduleConfiguration(interval_seconds=300)
            },
        ),
    )


def test_configured_grid_flow_orchestrator_persists_grid_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeGridFlowImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[GridFlowData]:
            del start_time, end_time, history_lookback_seconds
            return plan_without_needs(lambda: grid_flow_data(now or START))

        def is_fresh(self, _: GridFlowData, now: datetime) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantGridFlowImporter",
        FakeGridFlowImporter,
    )
    runtime_configuration = grid_flow_runtime_configuration(tmp_path)
    store = ProviderDataStore(tmp_path)

    orchestrator = build_configured_orchestrator(runtime_configuration, store)

    assert orchestrator is not None
    cycle = orchestrator.run_due(START)
    assert cycle.provider_runs[0].source == "grid_flow"
    assert cycle.provider_runs[0].status == "success"
    persisted = store.load(
        ProviderDataKey("grid-flow", "home-assistant", "grid_flow"),
        GRID_FLOW_ADAPTER,
    )
    assert persisted is not None
    assert persisted.import_kw == (1.0,)


GRID_KEY = ProviderDataKey("grid-flow", "home-assistant", "grid_flow")


class RecordingGridFlowImporter:
    """Return one hour of grid flow per requested hour and record each request."""

    requests: list[tuple[datetime, datetime | None]] = []

    def __init__(self, _: HomeAssistantConfiguration) -> None:
        pass

    def plan(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HistoryPlan[GridFlowData]:
        del history_lookback_seconds
        assert end_time is not None and now is not None
        self.requests.append((start_time, end_time))
        # Keep bootstrap requests cheap: only the newest two hours are retained,
        # as when Home Assistant history begins later than requested.
        first = (
            start_time
            if end_time - start_time <= timedelta(hours=24)
            else end_time - timedelta(hours=2)
        )
        count = int((end_time - first).total_seconds() // 3600)
        return plan_without_needs(
            lambda: GridFlowData(
                schema_version="1",
                start_time=first,
                interval_minutes=60,
                import_kw=tuple(float(index + 1) for index in range(count)),
                export_kw=(0.5,) * count,
                unit="kW",
                source=SourceMetadata(provider="home-assistant", entity_id="grid_flow"),
                retrieved_at=now,
                latest_observation_at=now,
            )
        )

    def is_fresh(self, _: GridFlowData, now: datetime) -> bool:
        del now
        return True


@pytest.fixture
def grid_flow_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[datetime, datetime | None]]:
    RecordingGridFlowImporter.requests = []
    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantGridFlowImporter",
        RecordingGridFlowImporter,
    )
    return RecordingGridFlowImporter.requests


def test_grid_flow_bootstrap_requests_the_complete_retention_window(
    tmp_path: Path,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(
        grid_flow_runtime_configuration(tmp_path), store
    )
    assert orchestrator is not None
    now = START + timedelta(hours=5, minutes=20)

    cycle = orchestrator.run_due(now)

    end = START + timedelta(hours=5)
    assert cycle.provider_runs[0].status == "success"
    assert grid_flow_requests == [(end - timedelta(hours=87_672), end)]


def test_grid_flow_refresh_requests_only_missing_hours_and_keeps_history(
    tmp_path: Path,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(
        grid_flow_runtime_configuration(tmp_path), store
    )
    assert orchestrator is not None

    orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    second = orchestrator.run_due(START + timedelta(hours=8, minutes=1), force=True)

    assert second.provider_runs[0].status == "success"
    assert grid_flow_requests[-1] == (
        START + timedelta(hours=5),
        START + timedelta(hours=8),
    )
    history = store.load(GRID_KEY, GRID_FLOW_ADAPTER)
    assert history is not None
    assert history.start_time == START + timedelta(hours=3)
    assert history.import_kw == (1.0, 2.0, 1.0, 2.0, 3.0)


def test_grid_flow_refresh_is_skipped_when_all_completed_hours_are_persisted(
    tmp_path: Path,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(
        grid_flow_runtime_configuration(tmp_path), store
    )
    assert orchestrator is not None
    orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    request_count = len(grid_flow_requests)

    cycle = orchestrator.run_due(START + timedelta(hours=5, minutes=30), force=True)

    assert cycle.provider_runs[0].status == "skipped"
    assert cycle.provider_runs[0].error == "no missing completed hours"
    assert len(grid_flow_requests) == request_count


def test_grid_flow_failure_preserves_the_retained_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(
        grid_flow_runtime_configuration(tmp_path), store
    )
    assert orchestrator is not None
    orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    retained = store.load(GRID_KEY, GRID_FLOW_ADAPTER)

    def fail(*_: object, **__: object) -> HistoryPlan[GridFlowData]:
        raise RuntimeError("Home Assistant is unavailable")

    monkeypatch.setattr(RecordingGridFlowImporter, "plan", fail)
    cycle = orchestrator.run_due(START + timedelta(hours=8, minutes=1), force=True)

    assert cycle.provider_runs[0].status == "failed"
    assert cycle.provider_runs[0].error == "Home Assistant is unavailable"
    assert store.load(GRID_KEY, GRID_FLOW_ADAPTER) == retained


def test_grid_flow_gap_in_fetched_hours_is_rejected_without_losing_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(
        grid_flow_runtime_configuration(tmp_path), store
    )
    assert orchestrator is not None
    orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    retained = store.load(GRID_KEY, GRID_FLOW_ADAPTER)
    original_plan = RecordingGridFlowImporter.plan

    def skip_first_requested_hour(
        self: RecordingGridFlowImporter,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HistoryPlan[GridFlowData]:
        return original_plan(
            self,
            start_time + timedelta(hours=1),
            end_time,
            history_lookback_seconds,
            now=now,
        )

    monkeypatch.setattr(RecordingGridFlowImporter, "plan", skip_first_requested_hour)
    cycle = orchestrator.run_due(START + timedelta(hours=8, minutes=1), force=True)

    assert cycle.provider_runs[0].status == "failed"
    assert "contiguous" in (cycle.provider_runs[0].error or "")
    assert store.load(GRID_KEY, GRID_FLOW_ADAPTER) == retained


PRICE_KEY = ProviderDataKey("electricity-prices", "awattar.de", "de")
PRICE_HISTORY_KEY = ProviderDataKey("electricity-price-history", "awattar.de", "de")
PRICE_ADAPTER = TypeAdapter(ElectricityPriceData)


class FakeAwattarImporter:
    """Return a 3-hour forecast starting at the requested hour."""

    def __init__(self, _: AwattarConfiguration) -> None:
        pass

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> ElectricityPriceData:
        del end_time
        assert now is not None
        hours = tuple(start_time + timedelta(hours=index) for index in range(3))
        base = start_time.hour / 100
        return ElectricityPriceData(
            schema_version="1",
            timestamps=hours,
            interval_minutes=60,
            import_price_eur_per_kwh=tuple(base + 0.30 for _ in hours),
            export_price_eur_per_kwh=tuple(base + 0.10 for _ in hours),
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=now,
            expires_at=start_time + timedelta(hours=3),
        )

    def is_fresh(self, _: ElectricityPriceData, now: datetime) -> bool:
        del now
        return True


def price_runtime_configuration(tmp_path: Path) -> Configuration:
    return Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        awattar=AwattarConfiguration(),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={
                "electricity_prices": DataSourceScheduleConfiguration(
                    interval_seconds=3600
                )
            },
        ),
    )


def test_price_refresh_retains_elapsed_hours_that_the_forecast_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "energy_optimizer.orchestration.AwattarImporter", FakeAwattarImporter
    )
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(
        price_runtime_configuration(tmp_path), store
    )
    assert orchestrator is not None

    orchestrator.run_due(START + timedelta(hours=1, minutes=5))
    cycle = orchestrator.run_due(START + timedelta(hours=4, minutes=5), force=True)

    assert cycle.provider_runs[0].status == "success"
    forecast = store.load(PRICE_KEY, PRICE_ADAPTER)
    history = store.load(PRICE_HISTORY_KEY, PRICE_ADAPTER)
    assert forecast is not None and history is not None
    assert forecast.timestamps == tuple(START + timedelta(hours=h) for h in (4, 5, 6))
    assert history.timestamps == tuple(
        START + timedelta(hours=h) for h in (1, 2, 3, 4, 5, 6)
    )
    assert history.import_price_eur_per_kwh == pytest.approx(
        (0.31, 0.31, 0.31, 0.34, 0.34, 0.34)
    )


def test_price_history_failure_never_blocks_the_forecast_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "energy_optimizer.orchestration.AwattarImporter", FakeAwattarImporter
    )
    store = ProviderDataStore(tmp_path)
    for suffix in (".json", ".json.bak"):
        (
            tmp_path / f"electricity-price-history-{PRICE_HISTORY_KEY.digest()}{suffix}"
        ).write_text("{corrupt", encoding="utf-8")
    orchestrator = build_configured_orchestrator(
        price_runtime_configuration(tmp_path), store
    )
    assert orchestrator is not None

    cycle = orchestrator.run_due(START + timedelta(hours=1, minutes=5))

    assert cycle.provider_runs[0].status == "success"
    forecast = store.load(PRICE_KEY, PRICE_ADAPTER)
    assert forecast is not None
    assert forecast.timestamps[0] == START + timedelta(hours=1)


def test_configured_battery_orchestrator_persists_battery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeBatteryImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def fetch(self, *, now: datetime | None = None) -> BatteryData:
            return battery_data(now or START)

        def is_fresh(self, _: BatteryData, *, now: datetime | None = None) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantBatteryImporter",
        FakeBatteryImporter,
    )
    battery_entities = {
        "state_of_charge": {
            "entity_id": "sensor.battery_soc",
            "unit": "%",
        },
        "capacity": {"entity_id": "sensor.battery_capacity", "unit": "kWh"},
        "minimum_soc": {"entity_id": "sensor.battery_minimum", "unit": "kWh"},
        "maximum_soc": {"entity_id": "sensor.battery_maximum", "unit": "kWh"},
        "maximum_charge": {"entity_id": "sensor.battery_charge", "unit": "kW"},
        "maximum_discharge": {
            "entity_id": "sensor.battery_discharge",
            "unit": "kW",
        },
        "battery_efficiency": {
            "entity_id": "sensor.battery_efficiency",
            "unit": "ratio",
        },
    }
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "battery": battery_entities,
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={"battery": DataSourceScheduleConfiguration(interval_seconds=300)},
        ),
    )
    store = ProviderDataStore(tmp_path)

    orchestrator = build_configured_orchestrator(runtime_configuration, store)

    assert orchestrator is not None
    cycle = orchestrator.run_due(START)
    assert cycle.provider_runs[0].source == "battery"
    assert cycle.provider_runs[0].status == "success"
    persisted = store.load(
        ProviderDataKey("battery", "home-assistant", "battery"), BATTERY_ADAPTER
    )
    assert persisted is not None
    assert persisted.state_of_charge_kwh == (5.0,)


def test_configured_efficiency_orchestrator_persists_daily_calculation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeEfficiencyImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
            del end_time
            return plan_without_needs(
                lambda: BatteryEfficiencyHistoryData(
                    schema_version="1",
                    start_time=start_time,
                    interval_minutes=60,
                    battery_energy_in_kwh=(0, 5, 0, 5, 0, 5),
                    battery_energy_out_kwh=(0, 4, 0, 4, 0, 4),
                    inverter_charge_energy_in_kwh=(10, 10, 10, 10, 10, 10),
                    inverter_charge_energy_out_kwh=(9, 9, 9, 9, 9, 9),
                    inverter_discharge_energy_in_kwh=(10, 10, 10, 10, 10, 10),
                    inverter_discharge_energy_out_kwh=(8, 8, 8, 8, 8, 8),
                    state_of_charge_percent=(50, 100, 50, 100, 50, 100, 50),
                    unit="kWh",
                    source=SourceMetadata(
                        provider="home-assistant",
                        entity_id="battery_efficiency_history",
                    ),
                    retrieved_at=now or START,
                    latest_observation_at=now or START,
                )
            )

        def is_fresh(
            self,
            _: BatteryEfficiencyHistoryData,
            *,
            now: datetime | None = None,
        ) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantBatteryEfficiencyImporter",
        FakeEfficiencyImporter,
    )
    energy_entity = {
        "entity_id": "sensor.energy",
        "state_class": "total_increasing",
        "unit": "kWh",
        "operation": "add",
    }
    leg = {"energy_in": [energy_entity], "energy_out": [energy_entity]}
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "timeout_seconds": 5,
                "battery": {
                    "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                    "capacity": 10,
                    "minimum_soc": 1,
                    "maximum_soc": 10,
                    "maximum_charge": 4,
                    "maximum_discharge": 4,
                    "efficiency_calculation": {
                        "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                        "battery": leg,
                        "inverter_charge": leg,
                        "inverter_discharge": leg,
                    },
                },
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={
                "battery_efficiency": DataSourceScheduleConfiguration(
                    interval_seconds=86400
                )
            },
        ),
    )
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(runtime_configuration, store)

    assert orchestrator is not None
    cycle = orchestrator.run_due(START + timedelta(hours=6))

    assert cycle.provider_runs[0].status == "success"
    result = store.load(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
    )
    assert result is not None
    assert result.battery_efficiency == pytest.approx(0.8)


def test_configured_efficiency_orchestrator_fetches_only_missing_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch_calls: list[tuple[datetime, datetime]] = []

    class FakeIncrementalEfficiencyImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
            fetch_calls.append((start_time, end_time))
            hours = int((end_time - start_time).total_seconds() // 3600)
            return plan_without_needs(
                lambda: BatteryEfficiencyHistoryData(
                    schema_version="1",
                    start_time=start_time,
                    interval_minutes=60,
                    battery_energy_in_kwh=(1.0,) * hours,
                    battery_energy_out_kwh=(0.0,) * hours,
                    inverter_charge_energy_in_kwh=(1.0,) * hours,
                    inverter_charge_energy_out_kwh=(1.0,) * hours,
                    inverter_discharge_energy_in_kwh=(1.0,) * hours,
                    inverter_discharge_energy_out_kwh=(1.0,) * hours,
                    state_of_charge_percent=(50.0,) * (hours + 1),
                    unit="kWh",
                    source=SourceMetadata(
                        provider="home-assistant",
                        entity_id="battery_efficiency_history",
                    ),
                    retrieved_at=now or start_time,
                    latest_observation_at=now or end_time,
                )
            )

        def is_fresh(self, *_: object, **__: object) -> bool:
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantBatteryEfficiencyImporter",
        FakeIncrementalEfficiencyImporter,
    )
    energy_entity = {
        "entity_id": "sensor.energy",
        "state_class": "total_increasing",
        "unit": "kWh",
        "operation": "add",
    }
    leg = {"energy_in": [energy_entity], "energy_out": [energy_entity]}
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "timeout_seconds": 5,
                "battery": {
                    "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                    "capacity": 10,
                    "minimum_soc": 1,
                    "maximum_soc": 10,
                    "maximum_charge": 4,
                    "maximum_discharge": 4,
                    "efficiency_calculation": {
                        "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                        "history_start": START.isoformat(),
                        "battery": leg,
                        "inverter_charge": leg,
                        "inverter_discharge": leg,
                    },
                },
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={
                "battery_efficiency": DataSourceScheduleConfiguration(
                    interval_seconds=3600
                )
            },
        ),
    )
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(runtime_configuration, store)
    assert orchestrator is not None

    orchestrator.run_due(START + timedelta(hours=1), force=True)
    orchestrator.run_due(START + timedelta(hours=2), force=True)

    assert fetch_calls == [
        (START, START + timedelta(hours=1)),
        (START + timedelta(hours=1), START + timedelta(hours=2)),
    ]
    history_key = ProviderDataKey(
        "battery-efficiency-history",
        "home-assistant",
        "battery_efficiency_history",
    )
    persisted = store.load(history_key, TypeAdapter(BatteryEfficiencyHistoryData))
    assert persisted is not None
    assert persisted.start_time == START
    assert len(persisted.battery_energy_in_kwh) == 2
    assert len(persisted.state_of_charge_percent) == 3


def efficiency_runtime_configuration(
    tmp_path: Path, leg: dict[str, Any]
) -> Configuration:
    """Configure hourly efficiency calculation with one entity mapping for every leg."""
    return Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "timeout_seconds": 5,
                "battery": {
                    "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                    "capacity": 10,
                    "minimum_soc": 1,
                    "maximum_soc": 10,
                    "maximum_charge": 4,
                    "maximum_discharge": 4,
                    "efficiency_calculation": {
                        "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                        "history_start": START.isoformat(),
                        "battery": leg,
                        "inverter_charge": leg,
                        "inverter_discharge": leg,
                    },
                },
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={
                "battery_efficiency": DataSourceScheduleConfiguration(
                    interval_seconds=3600
                )
            },
        ),
    )


EFFICIENCY_HISTORY_KEY = ProviderDataKey(
    "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
)
EFFICIENCY_RESULT_KEY = ProviderDataKey(
    "battery-efficiency", "home-assistant", "battery_efficiency"
)


def test_configured_efficiency_orchestrator_persists_through_an_excluded_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    """An excluded hour must not block persistence or force a full history refetch.

    Regression test for issue #157: previously, any suspect hour anywhere in
    the requested window made the importer raise before the history could be
    saved, so the source could never advance past it and kept re-requesting
    the entire configured history on every scheduled attempt.
    """

    fetch_calls: list[tuple[datetime, datetime]] = []
    excluded = exclusion(START, "counter_decrease", entity_id="sensor.energy")

    class FakeExcludingEfficiencyImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
            fetch_calls.append((start_time, end_time))
            hours = int((end_time - start_time).total_seconds() // 3600)
            # Only the window that starts at START has an excluded first hour.
            dropped = {0} if start_time == START else set()

            def leg(value: float) -> tuple[float | None, ...]:
                return tuple(
                    None if index in dropped else value for index in range(hours)
                )

            return plan_without_needs(
                lambda: BatteryEfficiencyHistoryData(
                    schema_version="1",
                    start_time=start_time,
                    interval_minutes=60,
                    battery_energy_in_kwh=leg(1.0),
                    battery_energy_out_kwh=leg(0.0),
                    inverter_charge_energy_in_kwh=leg(1.0),
                    inverter_charge_energy_out_kwh=leg(1.0),
                    inverter_discharge_energy_in_kwh=leg(1.0),
                    inverter_discharge_energy_out_kwh=leg(1.0),
                    # A state of charge is a boundary: it is dropped next to an
                    # excluded hour.
                    state_of_charge_percent=tuple(
                        None if index in dropped or index - 1 in dropped else 50.0
                        for index in range(hours + 1)
                    ),
                    unit="kWh",
                    source=SourceMetadata(
                        provider="home-assistant",
                        entity_id="battery_efficiency_history",
                    ),
                    retrieved_at=now or start_time,
                    latest_observation_at=now or end_time,
                    exclusions=(excluded,) if dropped else (),
                )
            )

        def is_fresh(self, *_: object, **__: object) -> bool:
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantBatteryEfficiencyImporter",
        FakeExcludingEfficiencyImporter,
    )
    energy_entity = {
        "entity_id": "sensor.energy",
        "state_class": "total_increasing",
        "unit": "kWh",
        "operation": "add",
    }
    leg = {"energy_in": [energy_entity], "energy_out": [energy_entity]}
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(
        efficiency_runtime_configuration(tmp_path, leg), store
    )
    assert orchestrator is not None

    with caplog.at_level(logging.WARNING, logger="energy_optimizer.orchestration"):
        first_cycle = orchestrator.run_due(START + timedelta(hours=1), force=True)
        second_cycle = orchestrator.run_due(START + timedelta(hours=2), force=True)

    # The excluded hour neither fails nor marks the run.
    assert first_cycle.provider_runs[0].status == "success"
    assert first_cycle.provider_runs[0].error is None
    assert second_cycle.provider_runs[0].status == "success"
    # The first refresh reports its incoming excluded hour once. The second brings
    # none of its own, and the persisted excluded hour is not reported again.
    assert [
        record.getMessage()
        for record in caplog.records
        if "provider_hours_excluded" in record.getMessage()
    ] == [
        "event=provider_hours_excluded component=orchestration operation=refresh "
        "source=battery_efficiency excluded_hour_count=1 reasons=counter_decrease:1 "
        "first_hour=2026-01-01T00:00:00+00:00 last_hour=2026-01-01T00:00:00+00:00"
    ]

    # The history is persisted despite the excluded hour, so the second attempt
    # only requests the newly missing hour instead of refetching the entire
    # configured history again.
    assert fetch_calls == [
        (START, START + timedelta(hours=1)),
        (START + timedelta(hours=1), START + timedelta(hours=2)),
    ]
    persisted = store.load(
        EFFICIENCY_HISTORY_KEY, TypeAdapter(BatteryEfficiencyHistoryData)
    )
    assert persisted is not None
    assert persisted.start_time == START
    assert persisted.battery_energy_in_kwh == (None, 1.0)
    assert persisted.battery_energy_out_kwh == (None, 0.0)
    assert persisted.inverter_charge_energy_in_kwh == (None, 1.0)
    assert persisted.inverter_charge_energy_out_kwh == (None, 1.0)
    assert persisted.inverter_discharge_energy_in_kwh == (None, 1.0)
    assert persisted.inverter_discharge_energy_out_kwh == (None, 1.0)
    assert persisted.state_of_charge_percent == (None, None, 50.0)
    assert persisted.exclusions == (excluded,)
    result = store.load(EFFICIENCY_RESULT_KEY, TypeAdapter(BatteryEfficiencyData))
    assert result is not None
    # The excluded hour contributes nothing and is not estimated.
    assert result.charge_throughput_kwh == pytest.approx(1.0)


@pytest.mark.parametrize("dip_kwh", [0.001, 2.0])
def test_configured_efficiency_orchestrator_persists_through_total_counter_decreases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dip_kwh: float,
) -> None:
    """Every decrease of a ``total`` counter excludes hours, however small.

    Regression test for issue #179: a 1 Wh decrease of a ``total`` counter
    without ``last_reset`` used to fail the whole refresh, so nothing was
    persisted and every run requested the complete history again. The real
    importer and aggregator run here against a mocked Home Assistant. A 1 Wh
    dip is no longer tolerated as jitter: it excludes the same hours as a 2 kWh
    dip, and the refresh still persists.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        if entity_id == "sensor.soc":
            return httpx.Response(
                200,
                json=home_assistant_history_payload(
                    entity_id,
                    [
                        (f"2026-01-01T{hour:02d}:00:00+00:00", str(50 + 10 * hour))
                        for hour in range(5)
                    ],
                    unit="%",
                    state_class="measurement",
                ),
            )
        # The charging counter dips in the first requested hour and the
        # discharging counter in the third, so both fetches meet a decrease.
        dip_hour = 0 if entity_id == "sensor.charging_battery_energy" else 2
        return httpx.Response(
            200,
            json=home_assistant_history_payload(
                entity_id,
                home_assistant_jittery_total_readings(100.0, dip_hour, dip_kwh=dip_kwh),
                state_class="total",
            ),
        )

    fetch_calls: list[tuple[datetime, datetime]] = []

    class RecordingEfficiencyImporter(HomeAssistantBatteryEfficiencyImporter):
        def plan(
            self,
            start_time: datetime,
            end_time: datetime,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
            fetch_calls.append((start_time, end_time))
            return super().plan(start_time, end_time, now=now)

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantBatteryEfficiencyImporter",
        RecordingEfficiencyImporter,
    )
    leg = {
        "energy_in": [
            {
                "entity_id": "sensor.charging_battery_energy",
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
        "energy_out": [
            {
                "entity_id": "sensor.discharging_battery_energy",
                "state_class": "total",
                "unit": "kWh",
                "operation": "add",
            }
        ],
    }
    store = ProviderDataStore(tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    orchestrator = build_configured_orchestrator(
        efficiency_runtime_configuration(tmp_path, leg),
        store,
        home_assistant_client=client,
    )
    assert orchestrator is not None

    try:
        first_cycle = orchestrator.run_due(START + timedelta(hours=2), force=True)
        second_cycle = orchestrator.run_due(START + timedelta(hours=4), force=True)
    finally:
        client.close()

    assert first_cycle.provider_runs[0].status == "success"
    assert second_cycle.provider_runs[0].status == "success"
    # The second run requests only the two hours missing after the first run.
    assert fetch_calls == [
        (START, START + timedelta(hours=2)),
        (START + timedelta(hours=2), START + timedelta(hours=4)),
    ]

    persisted = store.load(
        EFFICIENCY_HISTORY_KEY, TypeAdapter(BatteryEfficiencyHistoryData)
    )
    assert persisted is not None
    assert persisted.start_time == START
    # Hour 0 holds the charging dip and hour 2 the discharging dip. An hour that
    # is excluded for one counter is excluded for every leg, and each of the
    # others counts exactly 1 kWh.
    for values in (
        persisted.battery_energy_in_kwh,
        persisted.battery_energy_out_kwh,
        persisted.inverter_charge_energy_in_kwh,
        persisted.inverter_charge_energy_out_kwh,
        persisted.inverter_discharge_energy_in_kwh,
        persisted.inverter_discharge_energy_out_kwh,
    ):
        assert values == (None, 1.0, None, 1.0)
    assert [item.hour_start for item in persisted.exclusions] == [
        START,
        START + timedelta(hours=2),
    ]
    for item, entity_id, peak in zip(
        persisted.exclusions,
        ("sensor.charging_battery_energy", "sensor.discharging_battery_energy"),
        # The counter reaches ``base + hour + 0.5`` before it dips.
        (100.5, 102.5),
    ):
        # The decrease and the step directly after it are both untrusted.
        assert [cause.reason for cause in item.causes] == [
            "counter_decrease",
            "step_after_decrease",
        ]
        assert {cause.entity_id for cause in item.causes} == {entity_id}
        decrease = item.causes[0].data_points[0]
        assert decrease.state == f"{peak - dip_kwh:.3f}"
        assert decrease.previous_value == pytest.approx(peak)
        assert decrease.step_kwh == pytest.approx(-dip_kwh)
    result = store.load(EFFICIENCY_RESULT_KEY, TypeAdapter(BatteryEfficiencyData))
    assert result is not None
    assert result.charge_throughput_kwh == pytest.approx(2.0)
    assert result.discharge_throughput_kwh == pytest.approx(2.0)


def test_configured_forecast_solar_orchestrator_rejects_fast_polling(
    tmp_path: Path,
) -> None:
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        forecast_solar=ForecastSolarConfiguration(
            latitude=52.52,
            longitude=13.41,
            declination_degrees=35,
            azimuth_degrees=0,
            peak_power_kw=8,
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(
            enabled=True,
            sources={
                "pv_generation": DataSourceScheduleConfiguration(interval_seconds=299)
            },
        ),
    )

    with pytest.raises(OrchestrationError, match="at least 300 seconds"):
        build_configured_orchestrator(
            runtime_configuration, ProviderDataStore(tmp_path)
        )


def test_configured_home_assistant_failure_preserves_persisted_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeHomeAssistantImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[HouseholdLoadData]:
            del start_time, end_time, history_lookback_seconds, now
            raise RuntimeError(
                "Home Assistant returned no history for sensor.household_energy "
                "in the requested period"
            )

        def is_fresh(self, _: HouseholdLoadData, now: datetime) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantLoadImporter",
        FakeHomeAssistantImporter,
    )
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "household_load_entities": [
                    {
                        "entity_id": "sensor.household_energy",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=configuration(),
    )
    store = ProviderDataStore(tmp_path)
    key = ProviderDataKey("household-load", "home-assistant", "household_load")
    store.save(
        key,
        ADAPTER,
        data(
            START,
            value=2.5,
            source="home-assistant",
            entity_id="household_load",
        ),
    )

    orchestrator = build_configured_orchestrator(runtime_configuration, store)
    assert orchestrator is not None
    cycle = orchestrator.run_due(START + timedelta(hours=2))

    assert cycle.provider_runs[0].status == "failed"
    assert "no history for sensor.household_energy" in (
        cycle.provider_runs[0].error or ""
    )
    persisted = store.load(key, ADAPTER)
    assert persisted is not None
    assert persisted.load_kw == (2.5,)


def test_configured_home_assistant_fetch_bootstraps_to_ten_year_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[datetime, datetime | None, float, datetime | None]] = []

    class FakeHomeAssistantImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[HouseholdLoadData]:
            calls.append((start_time, end_time, history_lookback_seconds, now))
            return plan_without_needs(
                lambda: data(
                    end_time or start_time,
                    source="home-assistant",
                    entity_id="household_load",
                )
            )

        def is_fresh(self, _: HouseholdLoadData, now: datetime) -> bool:
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantLoadImporter",
        FakeHomeAssistantImporter,
    )
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "household_load_entities": [
                    {
                        "entity_id": "sensor.household_energy",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=configuration(),
    )
    orchestrator = build_configured_orchestrator(
        runtime_configuration, ProviderDataStore(tmp_path)
    )
    assert orchestrator is not None

    now = datetime(2026, 1, 1, 12, 34, tzinfo=timezone.utc)
    cycle = orchestrator.run_due(now)

    assert cycle.provider_runs[0].status == "success"
    assert calls == [
        (
            datetime(2016, 1, 1, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            0,
            now,
        )
    ]


def test_configured_home_assistant_persists_short_bootstrap_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained_start = datetime(2025, 12, 31, 22, tzinfo=timezone.utc)

    class FakeHomeAssistantImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[HouseholdLoadData]:
            del start_time, end_time, history_lookback_seconds, now
            return plan_without_needs(
                lambda: HouseholdLoadData(
                    schema_version="1",
                    start_time=retained_start,
                    interval_minutes=60,
                    load_kw=(1.0, 2.0),
                    unit="kW",
                    source=SourceMetadata(
                        provider="home-assistant", entity_id="household_load"
                    ),
                    retrieved_at=START,
                    latest_observation_at=START,
                )
            )

        def is_fresh(self, _: HouseholdLoadData, now: datetime) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantLoadImporter",
        FakeHomeAssistantImporter,
    )
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "household_load_entities": [
                    {
                        "entity_id": "sensor.household_energy",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=configuration(),
    )
    store = ProviderDataStore(tmp_path)
    orchestrator = build_configured_orchestrator(runtime_configuration, store)
    assert orchestrator is not None

    cycle = orchestrator.run_due(START)

    assert cycle.provider_runs[0].status == "success"
    key = ProviderDataKey("household-load", "home-assistant", "household_load")
    persisted = store.load(key, ADAPTER)
    assert persisted is not None
    assert persisted.start_time == retained_start
    assert persisted.load_kw == (1.0, 2.0)


def test_configured_home_assistant_fetch_starts_after_persisted_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[datetime, datetime | None, float, datetime | None]] = []

    class FakeHomeAssistantImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[HouseholdLoadData]:
            calls.append((start_time, end_time, history_lookback_seconds, now))
            return plan_without_needs(
                lambda: data(
                    start_time,
                    source="home-assistant",
                    entity_id="household_load",
                )
            )

        def is_fresh(self, _: HouseholdLoadData, now: datetime) -> bool:
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantLoadImporter",
        FakeHomeAssistantImporter,
    )
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "household_load_entities": [
                    {
                        "entity_id": "sensor.household_energy",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=configuration(),
    )
    store = ProviderDataStore(tmp_path)
    key = ProviderDataKey("household-load", "home-assistant", "household_load")
    store.save(
        key,
        ADAPTER,
        data(
            datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
            source="home-assistant",
            entity_id="household_load",
        ),
    )
    orchestrator = build_configured_orchestrator(runtime_configuration, store)
    assert orchestrator is not None

    now = datetime(2026, 1, 1, 12, 34, tzinfo=timezone.utc)
    orchestrator.run_due(now)

    assert calls[0] == (
        datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        0,
        now,
    )


def test_configured_home_assistant_skips_when_all_completed_hours_are_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[datetime, datetime | None, float, datetime | None]] = []

    class FakeHomeAssistantImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[HouseholdLoadData]:
            calls.append((start_time, end_time, history_lookback_seconds, now))
            return plan_without_needs(
                lambda: data(
                    start_time,
                    source="home-assistant",
                    entity_id="household_load",
                )
            )

        def is_fresh(self, _: HouseholdLoadData, now: datetime) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantLoadImporter",
        FakeHomeAssistantImporter,
    )
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "household_load_entities": [
                    {
                        "entity_id": "sensor.household_energy",
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    }
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=configuration(interval_seconds=300),
    )
    store = ProviderDataStore(tmp_path)
    key = ProviderDataKey("household-load", "home-assistant", "household_load")
    store.save(
        key,
        ADAPTER,
        data(
            datetime(2026, 1, 1, 10, tzinfo=timezone.utc),
            source="home-assistant",
            entity_id="household_load",
        ),
    )
    history_files = tuple(sorted(tmp_path.glob("*.ndjson*")))
    before = {path: path.read_bytes() for path in history_files}
    orchestrator = build_configured_orchestrator(runtime_configuration, store)
    assert orchestrator is not None

    first_cycle = orchestrator.run_due(datetime(2026, 1, 1, 11, tzinfo=timezone.utc))
    not_due_cycle = orchestrator.run_due(
        datetime(2026, 1, 1, 11, 4, tzinfo=timezone.utc)
    )
    second_cycle = orchestrator.run_due(
        datetime(2026, 1, 1, 11, 5, tzinfo=timezone.utc)
    )

    assert calls == []
    assert first_cycle.provider_runs[0].status == "skipped"
    assert first_cycle.provider_runs[0].error == "no missing completed hours"
    assert not_due_cycle.provider_runs[0].status == "skipped"
    assert not_due_cycle.provider_runs[0].error == "source is not due"
    assert second_cycle.provider_runs[0].status == "skipped"
    assert second_cycle.provider_runs[0].error == "no missing completed hours"
    assert {path: path.read_bytes() for path in history_files} == before


def test_household_load_refresh_excludes_a_negative_combined_hour_and_moves_on(
    tmp_path: Path,
) -> None:
    """A negative combined hour is excluded, never clamped, and never fails a refresh.

    Regression test for issue #172: previously the negative combined hour made
    the whole fetch fail, so the bootstrap persisted nothing and every later
    incremental refresh failed again until the hour left Home Assistant's
    retained history. Here both counters are valid in the hour; only the
    difference between them is not.
    """
    add_entity = "sensor.grid_import_energy"
    subtract_entity = "sensor.inverter_energy"

    def hourly(values: list[float]) -> list[tuple[datetime, str]]:
        return [
            (START + timedelta(hours=hour), str(value))
            for hour, value in enumerate(values)
        ]

    # Hourly energy of the add counter is 1.0, 0.5, 1.0, 2.0, 1.5 kWh and of the
    # subtract counter 0.5, 1.0, 0.5, 1.0, 1.0 kWh, so the hours net to
    # 0.5, -0.5, 0.5, 1.0, and 0.5 kWh.
    home_assistant = FakeHomeAssistant(
        {
            add_entity: hourly([10, 11, 11.5, 12.5, 14.5, 16]),
            subtract_entity: hourly([5, 5.5, 6.5, 7, 8, 9]),
        }
    )
    client = home_assistant.client()
    runtime_configuration = Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(
            {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "household_load_entities": [
                    {
                        "entity_id": add_entity,
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "add",
                    },
                    {
                        "entity_id": subtract_entity,
                        "state_class": "total_increasing",
                        "unit": "kWh",
                        "operation": "subtract",
                    },
                ],
                "timeout_seconds": 5,
            }
        ),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=configuration(),
    )
    store = ProviderDataStore(tmp_path)
    key = ProviderDataKey("household-load", "home-assistant", "household_load")
    orchestrator = build_configured_orchestrator(
        runtime_configuration, store, home_assistant_client=client
    )
    assert orchestrator is not None

    try:
        bootstrap_cycle = orchestrator.run_due(
            datetime(2026, 1, 1, 3, 30, tzinfo=timezone.utc)
        )
        bootstrapped = store.load(key, ADAPTER)
        home_assistant.requests.clear()
        incremental_cycle = orchestrator.run_due(
            datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)
        )
        refreshed = store.load(key, ADAPTER)
    finally:
        client.close()

    assert bootstrap_cycle.provider_runs[0].status == "success"
    assert bootstrap_cycle.provider_runs[0].error is None
    assert bootstrapped is not None
    assert bootstrapped.start_time == START
    assert bootstrapped.load_kw == (0.5, None, 0.5)
    (excluded,) = bootstrapped.exclusions
    assert excluded.hour_start == START + timedelta(hours=1)
    (cause,) = excluded.causes
    assert cause.reason == "combined_negative"
    assert cause.entity_id is None
    assert "-0.5 kWh" in cause.message
    # Every contributing entity is listed with its signed energy in the hour.
    assert [(point.entity_id, point.step_kwh) for point in cause.data_points] == [
        (add_entity, 0.5),
        (subtract_entity, -1.0),
    ]

    # The persisted history advanced past the excluded hour: the refresh
    # requested only hours 3 and 4 and did not fail again.
    assert incremental_cycle.provider_runs[0].status == "success"
    assert {
        entity: home_assistant.requested_ranges(entity)
        for entity in home_assistant.requested_entities()
    } == {
        add_entity: [(START + timedelta(hours=3), START + timedelta(hours=5))],
        subtract_entity: [(START + timedelta(hours=3), START + timedelta(hours=5))],
    }
    assert refreshed is not None
    assert refreshed.start_time == START
    assert refreshed.load_kw == (0.5, None, 0.5, 1.0, 0.5)
    assert refreshed.exclusions == bootstrapped.exclusions
