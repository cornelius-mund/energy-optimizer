"""Tests for scheduled provider retrieval and optimization triggers."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest
from pydantic import TypeAdapter

from energy_optimizer.config import (
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
from energy_optimizer.orchestration import (
    OrchestrationError,
    ProviderDataSnapshot,
    ProviderOrchestrator,
    ProviderRegistration,
    build_configured_orchestrator,
)
from energy_optimizer.providers.interfaces import (
    BatteryData,
    BatteryEfficiencyData,
    BatteryEfficiencyHistoryData,
    GridFlowData,
    HouseholdLoadData,
    IntervalQuality,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

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


def test_suspect_refresh_does_not_create_plan(tmp_path: Path) -> None:
    plans: list[ProviderDataSnapshot] = []

    def generate(snapshot: ProviderDataSnapshot) -> object:
        plans.append(snapshot)
        return snapshot

    def fetch(_: datetime, __: Any) -> HouseholdLoadData:
        return HouseholdLoadData(
            **{
                **data(START).__dict__,
                "quality": (
                    IntervalQuality(
                        status="suspect",
                        reason="reset_recovery",
                        entity_id="sensor.household_energy",
                    ),
                ),
            }
        )

    orchestrator = ProviderOrchestrator(
        configuration(optimization_enabled=True),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
        plan_generator=generate,
    )

    cycle = orchestrator.run_due(START)

    assert cycle.provider_runs[0].status == "suspect"
    assert cycle.plan_status == "not-ready"
    assert plans == []


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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HouseholdLoadData:
            del end_time, history_lookback_seconds
            return data(
                now or start_time,
                source="home-assistant",
                entity_id="household_load",
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


def test_configured_grid_flow_orchestrator_persists_grid_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeGridFlowImporter:
        def __init__(self, _: HomeAssistantConfiguration) -> None:
            pass

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> GridFlowData:
            del start_time, end_time, history_lookback_seconds
            return grid_flow_data(now or START)

        def is_fresh(self, _: GridFlowData, now: datetime) -> bool:
            del now
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.HomeAssistantGridFlowImporter",
        FakeGridFlowImporter,
    )
    runtime_configuration = Configuration(
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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime,
            *,
            now: datetime | None = None,
        ) -> BatteryEfficiencyHistoryData:
            del end_time
            return BatteryEfficiencyHistoryData(
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
                    provider="home-assistant", entity_id="battery_efficiency_history"
                ),
                retrieved_at=now or START,
                latest_observation_at=now or START,
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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime,
            *,
            now: datetime | None = None,
        ) -> BatteryEfficiencyHistoryData:
            fetch_calls.append((start_time, end_time))
            hours = int((end_time - start_time).total_seconds() // 3600)
            return BatteryEfficiencyHistoryData(
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
                    provider="home-assistant", entity_id="battery_efficiency_history"
                ),
                retrieved_at=now or start_time,
                latest_observation_at=now or end_time,
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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HouseholdLoadData:
            del start_time, end_time, history_lookback_seconds, now
            raise RuntimeError(
                "Home Assistant entity sensor.household_energy has no usable "
                "history after skipping unknown or unavailable records"
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
    assert "no usable history" in (cycle.provider_runs[0].error or "")
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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HouseholdLoadData:
            calls.append((start_time, end_time, history_lookback_seconds, now))
            return data(
                end_time or start_time,
                source="home-assistant",
                entity_id="household_load",
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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HouseholdLoadData:
            del start_time, end_time, history_lookback_seconds, now
            return HouseholdLoadData(
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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HouseholdLoadData:
            calls.append((start_time, end_time, history_lookback_seconds, now))
            return data(
                start_time,
                source="home-assistant",
                entity_id="household_load",
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

        def fetch(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HouseholdLoadData:
            calls.append((start_time, end_time, history_lookback_seconds, now))
            return data(
                start_time,
                source="home-assistant",
                entity_id="household_load",
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
