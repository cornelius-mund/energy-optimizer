"""Tests for scheduled provider retrieval and optimization triggers."""

import logging
from collections.abc import Callable
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
    PlanGenerator,
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
    aggregate_settings,
    home_assistant_history_payload,
    home_assistant_jittery_total_readings,
    plan_without_needs,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
ADAPTER = TypeAdapter(HouseholdLoadData)
PV_ADAPTER = TypeAdapter(PvGenerationData)
GRID_FLOW_ADAPTER = TypeAdapter(GridFlowData)
BATTERY_ADAPTER = TypeAdapter(BatteryData)
PRICE_ADAPTER = TypeAdapter(ElectricityPriceData)
EFFICIENCY_ADAPTER = TypeAdapter(BatteryEfficiencyData)
HISTORY_ADAPTER = TypeAdapter(BatteryEfficiencyHistoryData)
LOAD_KEY = ProviderDataKey("household-load", "household_load", "sensor.household_load")
HOUSEHOLD_KEY = ProviderDataKey("household-load", "home-assistant", "household_load")
PV_KEY = ProviderDataKey("pv-generation", "forecast.solar", "pv_generation")
GRID_KEY = ProviderDataKey("grid-flow", "home-assistant", "grid_flow")
BATTERY_KEY = ProviderDataKey("battery", "home-assistant", "battery")
PRICE_KEY = ProviderDataKey("electricity-prices", "awattar.de", "de")
PRICE_HISTORY_KEY = ProviderDataKey("electricity-price-history", "awattar.de", "de")
EFFICIENCY_HISTORY_KEY = ProviderDataKey(
    "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
)
EFFICIENCY_RESULT_KEY = ProviderDataKey(
    "battery-efficiency", "home-assistant", "battery_efficiency"
)
FORECAST_SOLAR = ForecastSolarConfiguration(
    latitude=52.52,
    longitude=13.41,
    declination_degrees=35,
    azimuth_degrees=0,
    peak_power_kw=8,
)

LoadCall = tuple[datetime, datetime | None, float, datetime | None]


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


def home_assistant_load(now: datetime, value: float = 1.0) -> HouseholdLoadData:
    """Build household load as the configured Home Assistant source persists it."""
    return data(now, value, source="home-assistant", entity_id="household_load")


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


def load_persisted[T](
    store: ProviderDataStore, key: ProviderDataKey, adapter: TypeAdapter[T]
) -> T:
    persisted = store.load(key, adapter)
    assert persisted is not None
    return persisted


def registration(fetch: Any) -> ProviderRegistration:
    return ProviderRegistration(
        name="household_load",
        data_type="household-load",
        adapter=ADAPTER,
        plan=lambda now, schedule: plan_without_needs(lambda: fetch(now, schedule)),
        is_fresh=lambda value, now: (
            isinstance(value, HouseholdLoadData)
            and value.latest_observation_at >= now - timedelta(minutes=90)
        ),
    )


def orchestration(
    source: str = "household_load",
    interval_seconds: float = 300,
    optimization_enabled: bool = False,
) -> OrchestrationConfiguration:
    """Orchestrate one ``source``, optionally planning once it has refreshed."""
    return OrchestrationConfiguration(
        enabled=True,
        sources={
            source: DataSourceScheduleConfiguration(interval_seconds=interval_seconds)
        },
        optimization=OptimizationTriggerConfiguration(
            enabled=optimization_enabled,
            required_sources=[source] if optimization_enabled else [],
        ),
    )


def default_fetch(now: datetime, _: Any) -> HouseholdLoadData:
    return data(now)


def orchestrator_for(
    tmp_path: Path,
    fetch: Any = default_fetch,
    plan_generator: PlanGenerator | None = None,
    **options: Any,
) -> ProviderOrchestrator:
    """Orchestrate the household-load source that ``fetch`` provides."""
    return ProviderOrchestrator(
        orchestration(**options),
        [registration(fetch)],
        ProviderDataStore(tmp_path),
        plan_generator=plan_generator,
    )


class PlanRecorder(list[ProviderDataSnapshot]):
    """A plan generator that records the snapshot of every plan it creates."""

    def __call__(self, snapshot: ProviderDataSnapshot) -> object:
        self.append(snapshot)
        return snapshot


def test_startup_fetch_persists_normalized_data(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        calls.append(now)
        return data(now)

    cycle = orchestrator_for(tmp_path, fetch).run_due(START)

    assert calls == [START]
    assert cycle.provider_runs[0].status == "success"
    persisted = load_persisted(ProviderDataStore(tmp_path), LOAD_KEY, ADAPTER)
    assert persisted.load_kw == (1.0,)


def test_source_is_not_fetched_until_its_interval_elapses(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        calls.append(now)
        return data(now)

    orchestrator = orchestrator_for(tmp_path, fetch, interval_seconds=300)

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

    orchestrator = orchestrator_for(tmp_path, fetch, interval_seconds=60)
    orchestrator.run_due(START)
    should_fail = True
    cycle = orchestrator.run_due(START + timedelta(seconds=60))

    assert cycle.provider_runs[0].status == "failed"
    assert cycle.provider_runs[0].error == "provider unavailable"
    persisted = load_persisted(ProviderDataStore(tmp_path), LOAD_KEY, ADAPTER)
    assert persisted.load_kw == (2.0,)


def test_failed_required_refresh_does_not_create_plan(tmp_path: Path) -> None:
    should_fail = False

    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        if should_fail:
            raise RuntimeError("provider unavailable")
        return data(now)

    plans = PlanRecorder()
    orchestrator = orchestrator_for(
        tmp_path, fetch, plans, interval_seconds=60, optimization_enabled=True
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

    orchestrator = orchestrator_for(tmp_path, fetch)
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
    plans = PlanRecorder()

    cycle = orchestrator_for(tmp_path, plan_generator=plans).run_due(START)

    assert cycle.plan_status == "disabled"
    assert plans == []


def test_optimization_reports_unavailable_without_plan_generator(
    tmp_path: Path,
) -> None:
    cycle = orchestrator_for(tmp_path, optimization_enabled=True).run_due(START)

    assert cycle.plan_status == "unavailable"
    assert cycle.plan_error == "optimization plan generator is not configured"


def test_complete_fresh_refresh_creates_one_plan_snapshot(tmp_path: Path) -> None:
    plans = PlanRecorder()
    orchestrator = orchestrator_for(
        tmp_path,
        lambda now, _: data(now, value=3.5),
        plans,
        optimization_enabled=True,
    )

    cycle = orchestrator.run_due(START)

    assert cycle.plan_status == "created"
    assert len(plans) == 1
    assert plans[0].captured_at == START
    provider_data = plans[0].data["household_load"]
    assert isinstance(provider_data, HouseholdLoadData)
    assert provider_data.load_kw == (3.5,)


def test_plan_generation_failure_is_reported(tmp_path: Path) -> None:
    def generate(_: ProviderDataSnapshot) -> object:
        raise RuntimeError("solver unavailable")

    orchestrator = orchestrator_for(
        tmp_path, plan_generator=generate, optimization_enabled=True
    )

    cycle = orchestrator.run_due(START)

    assert cycle.plan_status == "failed"
    assert cycle.plan_error == "solver unavailable"


def test_restarted_orchestrator_loads_persisted_data(tmp_path: Path) -> None:
    def fetch(now: datetime, _: Any) -> HouseholdLoadData:
        return data(now, value=4.0)

    orchestrator_for(tmp_path, fetch).run_due(START)
    loaded: list[object | None] = []

    def load() -> object | None:
        persisted = ProviderDataStore(tmp_path).load(LOAD_KEY, ADAPTER)
        loaded.append(persisted)
        return persisted

    ProviderOrchestrator(
        orchestration(),
        [
            replace(
                registration(fetch),
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
    plans = PlanRecorder()
    orchestrator = orchestrator_for(
        tmp_path,
        lambda _, __: data(START - timedelta(hours=2)),
        plans,
        optimization_enabled=True,
    )

    cycle = orchestrator.run_due(START)

    assert cycle.provider_runs[0].status == "stale"
    assert cycle.plan_status == "not-ready"
    assert plans == []


def test_refresh_with_excluded_hours_succeeds_and_does_not_block_the_plan(
    tmp_path: Path,
) -> None:
    excluded = exclusion(START, "unavailable")

    def fetch(_: datetime, __: Any) -> HouseholdLoadData:
        return replace(data(START), load_kw=(None,), exclusions=(excluded,))

    plans = PlanRecorder()
    orchestrator = orchestrator_for(tmp_path, fetch, plans, optimization_enabled=True)

    cycle = orchestrator.run_due(START)

    run = cycle.provider_runs[0]
    assert run.status == "success"
    assert run.error is None
    assert cycle.plan_status == "created"
    assert len(plans) == 1
    persisted = load_persisted(ProviderDataStore(tmp_path), LOAD_KEY, ADAPTER)
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

    orchestrator = orchestrator_for(tmp_path, fetch)

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
    orchestrator = orchestrator_for(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="energy_optimizer.orchestration"):
        cycle = orchestrator.run_due(START)

    assert cycle.provider_runs[0].status == "success"
    assert not [
        record
        for record in caplog.records
        if "provider_hours_excluded" in record.getMessage()
    ]


def test_orchestrator_rejects_unregistered_configured_source(tmp_path: Path) -> None:
    with pytest.raises(OrchestrationError, match="unknown"):
        ProviderOrchestrator(
            orchestration("unknown", 60), [], ProviderDataStore(tmp_path)
        )


def energy_entity(
    entity_id: str, state_class: str = "total_increasing"
) -> dict[str, str]:
    return {"entity_id": entity_id, "state_class": state_class, "unit": "kWh"}


def home_assistant_settings(**sections: Any) -> HomeAssistantConfiguration:
    return HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            **sections,
        }
    )


def runtime_configuration(
    tmp_path: Path, source: str, interval_seconds: float, **sections: Any
) -> Configuration:
    """Configure orchestration of one ``source`` next to the given sections."""
    return Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=orchestration(source, interval_seconds),
        **sections,
    )


def household_configuration(
    tmp_path: Path, aggregate: dict[str, Any] | None = None
) -> Configuration:
    """Configure household load from one energy counter unless ``aggregate`` is set."""
    return runtime_configuration(
        tmp_path,
        "household_load",
        300,
        home_assistant=home_assistant_settings(
            household_load=aggregate
            or aggregate_settings(add=[energy_entity("sensor.household_energy")])
        ),
    )


def configured(
    configuration: Configuration,
    store: ProviderDataStore,
    home_assistant_client: httpx.Client | None = None,
) -> ProviderOrchestrator:
    orchestrator = build_configured_orchestrator(
        configuration, store, home_assistant_client=home_assistant_client
    )
    assert orchestrator is not None
    return orchestrator


class FakeImporter:
    """Stand-in for a provider importer whose data is always fresh."""

    def __init__(self, _: object) -> None:
        pass

    def is_fresh(self, *_: object, **__: object) -> bool:
        return True


def patch_importer(monkeypatch: pytest.MonkeyPatch, name: str, importer: type) -> None:
    monkeypatch.setattr(f"energy_optimizer.orchestration.{name}", importer)


def fake_load_importer(
    monkeypatch: pytest.MonkeyPatch,
    build: Callable[[datetime, datetime | None, datetime | None], HouseholdLoadData],
) -> list[LoadCall]:
    """Answer every household-load request with ``build`` and record it."""
    calls: list[LoadCall] = []

    class FakeLoadImporter(FakeImporter):
        def plan(
            self,
            start_time: datetime,
            end_time: datetime | None = None,
            history_lookback_seconds: float = 0,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[HouseholdLoadData]:
            calls.append((start_time, end_time, history_lookback_seconds, now))
            return plan_without_needs(lambda: build(start_time, end_time, now))

    patch_importer(monkeypatch, "HomeAssistantLoadImporter", FakeLoadImporter)
    return calls


def test_configured_home_assistant_orchestrator_uses_aggregate_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_load_importer(
        monkeypatch, lambda start_time, _, now: home_assistant_load(now or start_time)
    )
    store = ProviderDataStore(tmp_path)

    cycle = configured(household_configuration(tmp_path), store).run_due(START)

    assert cycle.provider_runs[0].status == "success"
    persisted = load_persisted(store, HOUSEHOLD_KEY, ADAPTER)
    assert persisted.source.entity_id == "household_load"


def test_configured_forecast_solar_orchestrator_persists_forecast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeForecastSolarImporter(FakeImporter):
        def fetch(
            self, start_time: datetime, *_: object, now: datetime | None = None
        ) -> PvGenerationData:
            when = now or start_time
            start = when.replace(minute=0, second=0, microsecond=0)
            return PvGenerationData(
                schema_version="1",
                start_time=start,
                interval_minutes=60,
                generation_kw=(1.0,),
                unit="kW",
                source=SourceMetadata(
                    provider="forecast.solar", entity_id="pv_generation"
                ),
                retrieved_at=when,
                expires_at=start + timedelta(hours=24),
            )

    patch_importer(monkeypatch, "ForecastSolarImporter", FakeForecastSolarImporter)
    store = ProviderDataStore(tmp_path)
    runtime = runtime_configuration(
        tmp_path, "pv_generation", 300, forecast_solar=FORECAST_SOLAR
    )

    cycle = configured(runtime, store).run_due(START)

    assert cycle.provider_runs[0].source == "pv_generation"
    assert cycle.provider_runs[0].status == "success"
    assert load_persisted(store, PV_KEY, PV_ADAPTER).generation_kw == (1.0,)


def grid_flow_configuration(tmp_path: Path) -> Configuration:
    return runtime_configuration(
        tmp_path,
        "grid_flow",
        300,
        home_assistant=home_assistant_settings(
            grid_import=aggregate_settings(add=[energy_entity("sensor.grid_import")]),
            grid_export=aggregate_settings(add=[energy_entity("sensor.grid_export")]),
        ),
    )


def test_configured_grid_flow_orchestrator_persists_grid_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeGridFlowImporter(FakeImporter):
        def plan(
            self, *_: object, now: datetime | None = None
        ) -> HistoryPlan[GridFlowData]:
            when = now or START
            return plan_without_needs(
                lambda: GridFlowData(
                    schema_version="1",
                    start_time=when.replace(minute=0, second=0, microsecond=0),
                    interval_minutes=60,
                    import_kw=(1.0,),
                    export_kw=(0.5,),
                    unit="kW",
                    source=SourceMetadata(
                        provider="home-assistant", entity_id="grid_flow"
                    ),
                    retrieved_at=when,
                    latest_observation_at=when,
                )
            )

    patch_importer(monkeypatch, "HomeAssistantGridFlowImporter", FakeGridFlowImporter)
    store = ProviderDataStore(tmp_path)

    cycle = configured(grid_flow_configuration(tmp_path), store).run_due(START)

    assert cycle.provider_runs[0].source == "grid_flow"
    assert cycle.provider_runs[0].status == "success"
    assert load_persisted(store, GRID_KEY, GRID_FLOW_ADAPTER).import_kw == (1.0,)


class RecordingGridFlowImporter(FakeImporter):
    """Return one hour of grid flow per requested hour and record each request."""

    requests: list[tuple[datetime, datetime | None]] = []

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


@pytest.fixture
def grid_flow_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[datetime, datetime | None]]:
    RecordingGridFlowImporter.requests = []
    patch_importer(
        monkeypatch, "HomeAssistantGridFlowImporter", RecordingGridFlowImporter
    )
    return RecordingGridFlowImporter.requests


@pytest.fixture
def grid_orchestrator(
    tmp_path: Path, grid_flow_requests: list[tuple[datetime, datetime | None]]
) -> ProviderOrchestrator:
    """Orchestrate grid flow after ``grid_flow_requests`` replaced its importer."""
    return configured(grid_flow_configuration(tmp_path), ProviderDataStore(tmp_path))


def test_grid_flow_bootstrap_requests_the_complete_retention_window(
    grid_orchestrator: ProviderOrchestrator,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    cycle = grid_orchestrator.run_due(START + timedelta(hours=5, minutes=20))

    end = START + timedelta(hours=5)
    assert cycle.provider_runs[0].status == "success"
    assert grid_flow_requests == [(end - timedelta(hours=87_672), end)]


def test_grid_flow_refresh_requests_only_missing_hours_and_keeps_history(
    grid_orchestrator: ProviderOrchestrator,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    grid_orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    second = grid_orchestrator.run_due(
        START + timedelta(hours=8, minutes=1), force=True
    )

    assert second.provider_runs[0].status == "success"
    assert grid_flow_requests[-1] == (
        START + timedelta(hours=5),
        START + timedelta(hours=8),
    )
    history = load_persisted(grid_orchestrator.store, GRID_KEY, GRID_FLOW_ADAPTER)
    assert history.start_time == START + timedelta(hours=3)
    assert history.import_kw == (1.0, 2.0, 1.0, 2.0, 3.0)


def test_grid_flow_refresh_is_skipped_when_all_completed_hours_are_persisted(
    grid_orchestrator: ProviderOrchestrator,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    grid_orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    request_count = len(grid_flow_requests)

    cycle = grid_orchestrator.run_due(
        START + timedelta(hours=5, minutes=30), force=True
    )

    assert cycle.provider_runs[0].status == "skipped"
    assert cycle.provider_runs[0].error == "no missing completed hours"
    assert len(grid_flow_requests) == request_count


def test_grid_flow_failure_preserves_the_retained_history(
    grid_orchestrator: ProviderOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    grid_orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    retained = grid_orchestrator.store.load(GRID_KEY, GRID_FLOW_ADAPTER)

    def fail(*_: object, **__: object) -> HistoryPlan[GridFlowData]:
        raise RuntimeError("Home Assistant is unavailable")

    monkeypatch.setattr(RecordingGridFlowImporter, "plan", fail)
    cycle = grid_orchestrator.run_due(START + timedelta(hours=8, minutes=1), force=True)

    assert cycle.provider_runs[0].status == "failed"
    assert cycle.provider_runs[0].error == "Home Assistant is unavailable"
    assert grid_orchestrator.store.load(GRID_KEY, GRID_FLOW_ADAPTER) == retained


def test_grid_flow_gap_in_fetched_hours_is_excluded_as_history_unavailable(
    grid_orchestrator: ProviderOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
    grid_flow_requests: list[tuple[datetime, datetime | None]],
) -> None:
    grid_orchestrator.run_due(START + timedelta(hours=5, minutes=1))
    original_plan = RecordingGridFlowImporter.plan

    def skip_first_requested_hour(
        self: RecordingGridFlowImporter,
        start_time: datetime,
        *arguments: Any,
        **keywords: Any,
    ) -> HistoryPlan[GridFlowData]:
        return original_plan(
            self, start_time + timedelta(hours=1), *arguments, **keywords
        )

    monkeypatch.setattr(RecordingGridFlowImporter, "plan", skip_first_requested_hour)
    cycle = grid_orchestrator.run_due(START + timedelta(hours=8, minutes=1), force=True)

    assert cycle.provider_runs[0].status == "success"
    history = load_persisted(grid_orchestrator.store, GRID_KEY, GRID_FLOW_ADAPTER)
    assert history.start_time == START + timedelta(hours=3)
    assert history.import_kw == (1.0, 2.0, None, 1.0, 2.0)
    assert history.export_kw == (0.5, 0.5, None, 0.5, 0.5)
    assert [
        (item.hour_start, [cause.reason for cause in item.causes])
        for item in history.exclusions
    ] == [(START + timedelta(hours=5), ["history_unavailable"])]

    monkeypatch.setattr(RecordingGridFlowImporter, "plan", original_plan)
    third = grid_orchestrator.run_due(
        START + timedelta(hours=10, minutes=1), force=True
    )

    assert third.provider_runs[0].status == "success"
    assert grid_flow_requests[-1] == (
        START + timedelta(hours=8),
        START + timedelta(hours=10),
    )


class FakeAwattarImporter(FakeImporter):
    """Return a 3-hour forecast starting at the requested hour."""

    def fetch(
        self, start_time: datetime, *_: object, now: datetime | None = None
    ) -> ElectricityPriceData:
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


def price_configuration(tmp_path: Path) -> Configuration:
    return runtime_configuration(
        tmp_path, "electricity_prices", 3600, awattar=AwattarConfiguration()
    )


def test_price_refresh_retains_elapsed_hours_that_the_forecast_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_importer(monkeypatch, "AwattarImporter", FakeAwattarImporter)
    store = ProviderDataStore(tmp_path)
    orchestrator = configured(price_configuration(tmp_path), store)

    orchestrator.run_due(START + timedelta(hours=1, minutes=5))
    cycle = orchestrator.run_due(START + timedelta(hours=4, minutes=5), force=True)

    assert cycle.provider_runs[0].status == "success"
    forecast = load_persisted(store, PRICE_KEY, PRICE_ADAPTER)
    history = load_persisted(store, PRICE_HISTORY_KEY, PRICE_ADAPTER)
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
    patch_importer(monkeypatch, "AwattarImporter", FakeAwattarImporter)
    store = ProviderDataStore(tmp_path)
    for suffix in (".json", ".json.bak"):
        (
            tmp_path / f"electricity-price-history-{PRICE_HISTORY_KEY.digest()}{suffix}"
        ).write_text("{corrupt", encoding="utf-8")
    orchestrator = configured(price_configuration(tmp_path), store)

    cycle = orchestrator.run_due(START + timedelta(hours=1, minutes=5))

    assert cycle.provider_runs[0].status == "success"
    forecast = load_persisted(store, PRICE_KEY, PRICE_ADAPTER)
    assert forecast.timestamps[0] == START + timedelta(hours=1)


def test_configured_battery_orchestrator_persists_battery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeBatteryImporter(FakeImporter):
        def fetch(self, *, now: datetime | None = None) -> BatteryData:
            return BatteryData(
                schema_version="1",
                start_time=now or START,
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
                retrieved_at=now or START,
                latest_observation_at=now or START,
            )

    patch_importer(monkeypatch, "HomeAssistantBatteryImporter", FakeBatteryImporter)
    store = ProviderDataStore(tmp_path)
    runtime = runtime_configuration(
        tmp_path,
        "battery",
        300,
        home_assistant=home_assistant_settings(
            battery={
                "state_of_charge": {"entity_id": "sensor.battery_soc", "unit": "%"},
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
        ),
    )

    cycle = configured(runtime, store).run_due(START)

    assert cycle.provider_runs[0].source == "battery"
    assert cycle.provider_runs[0].status == "success"
    persisted = load_persisted(store, BATTERY_KEY, BATTERY_ADAPTER)
    assert persisted.state_of_charge_kwh == (5.0,)


def efficiency_leg(
    energy_in: str = "sensor.energy",
    energy_out: str = "sensor.energy",
    state_class: str = "total_increasing",
) -> dict[str, Any]:
    return {
        "energy_in": aggregate_settings(add=[energy_entity(energy_in, state_class)]),
        "energy_out": aggregate_settings(add=[energy_entity(energy_out, state_class)]),
    }


def efficiency_configuration(
    tmp_path: Path,
    leg: dict[str, Any] | None = None,
    *,
    interval_seconds: float = 3600,
    history_start: bool = True,
) -> Configuration:
    """Configure hourly efficiency calculation with one entity mapping for every leg."""
    soc = {"entity_id": "sensor.soc", "unit": "%"}
    legs = {
        name: leg or efficiency_leg()
        for name in ("battery", "inverter_charge", "inverter_discharge")
    }
    calculation: dict[str, Any] = {"state_of_charge": soc, **legs}
    if history_start:
        calculation["history_start"] = START.isoformat()
    return runtime_configuration(
        tmp_path,
        "battery_efficiency",
        interval_seconds,
        home_assistant=home_assistant_settings(
            battery={
                "state_of_charge": soc,
                "capacity": 10,
                "minimum_soc": 1,
                "maximum_soc": 10,
                "maximum_charge": 4,
                "maximum_discharge": 4,
                "efficiency_calculation": calculation,
            }
        ),
    )


def fake_efficiency_importer(
    monkeypatch: pytest.MonkeyPatch, excluded: HourExclusion | None = None
) -> list[tuple[datetime, datetime]]:
    """Answer every window with 1 kWh hours and record the requested windows.

    With ``excluded``, the first hour of the window that starts at START is
    excluded, and the state of charge next to it is dropped.
    """
    fetch_calls: list[tuple[datetime, datetime]] = []

    class FakeEfficiencyImporter(FakeImporter):
        def plan(
            self,
            start_time: datetime,
            end_time: datetime,
            *,
            now: datetime | None = None,
        ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
            fetch_calls.append((start_time, end_time))
            hours = int((end_time - start_time).total_seconds() // 3600)
            exclusions = (
                (excluded,) if excluded is not None and start_time == START else ()
            )
            dropped = {0} if exclusions else set()

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
                    exclusions=exclusions,
                )
            )

    patch_importer(
        monkeypatch, "HomeAssistantBatteryEfficiencyImporter", FakeEfficiencyImporter
    )
    return fetch_calls


def test_configured_efficiency_orchestrator_persists_daily_calculation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeEfficiencyImporter(FakeImporter):
        def plan(
            self, start_time: datetime, *_: object, now: datetime | None = None
        ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
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

    patch_importer(
        monkeypatch, "HomeAssistantBatteryEfficiencyImporter", FakeEfficiencyImporter
    )
    store = ProviderDataStore(tmp_path)
    runtime = efficiency_configuration(
        tmp_path, interval_seconds=86400, history_start=False
    )

    cycle = configured(runtime, store).run_due(START + timedelta(hours=6))

    assert cycle.provider_runs[0].status == "success"
    result = load_persisted(store, EFFICIENCY_RESULT_KEY, EFFICIENCY_ADAPTER)
    assert result.battery_efficiency == pytest.approx(0.8)


def test_configured_efficiency_orchestrator_fetches_only_missing_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch_calls = fake_efficiency_importer(monkeypatch)
    store = ProviderDataStore(tmp_path)
    orchestrator = configured(efficiency_configuration(tmp_path), store)

    orchestrator.run_due(START + timedelta(hours=1), force=True)
    orchestrator.run_due(START + timedelta(hours=2), force=True)

    assert fetch_calls == [
        (START, START + timedelta(hours=1)),
        (START + timedelta(hours=1), START + timedelta(hours=2)),
    ]
    persisted = load_persisted(store, EFFICIENCY_HISTORY_KEY, HISTORY_ADAPTER)
    assert persisted.start_time == START
    assert len(persisted.battery_energy_in_kwh) == 2
    assert len(persisted.state_of_charge_percent) == 3


def test_configured_efficiency_orchestrator_persists_through_an_excluded_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    """An excluded hour must not block persistence or force a full history refetch.

    Regression test for issue #157: previously, any suspect hour anywhere in
    the requested window made the importer raise before the history could be
    saved, so the source could never advance past it and kept re-requesting
    the entire configured history on every scheduled attempt.
    """
    excluded = exclusion(START, "counter_decrease", entity_id="sensor.energy")
    fetch_calls = fake_efficiency_importer(monkeypatch, excluded)
    store = ProviderDataStore(tmp_path)
    orchestrator = configured(efficiency_configuration(tmp_path), store)

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
    persisted = load_persisted(store, EFFICIENCY_HISTORY_KEY, HISTORY_ADAPTER)
    assert persisted.start_time == START
    assert persisted.battery_energy_in_kwh == (None, 1.0)
    assert persisted.battery_energy_out_kwh == (None, 0.0)
    assert persisted.inverter_charge_energy_in_kwh == (None, 1.0)
    assert persisted.inverter_charge_energy_out_kwh == (None, 1.0)
    assert persisted.inverter_discharge_energy_in_kwh == (None, 1.0)
    assert persisted.inverter_discharge_energy_out_kwh == (None, 1.0)
    assert persisted.state_of_charge_percent == (None, None, 50.0)
    assert persisted.exclusions == (excluded,)
    result = load_persisted(store, EFFICIENCY_RESULT_KEY, EFFICIENCY_ADAPTER)
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

    patch_importer(
        monkeypatch,
        "HomeAssistantBatteryEfficiencyImporter",
        RecordingEfficiencyImporter,
    )
    store = ProviderDataStore(tmp_path)
    leg = efficiency_leg(
        "sensor.charging_battery_energy", "sensor.discharging_battery_energy", "total"
    )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        orchestrator = configured(
            efficiency_configuration(tmp_path, leg), store, client
        )
        first_cycle = orchestrator.run_due(START + timedelta(hours=2), force=True)
        second_cycle = orchestrator.run_due(START + timedelta(hours=4), force=True)

    assert first_cycle.provider_runs[0].status == "success"
    assert second_cycle.provider_runs[0].status == "success"
    # The second run requests only the two hours missing after the first run.
    assert fetch_calls == [
        (START, START + timedelta(hours=2)),
        (START + timedelta(hours=2), START + timedelta(hours=4)),
    ]

    persisted = load_persisted(store, EFFICIENCY_HISTORY_KEY, HISTORY_ADAPTER)
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
    result = load_persisted(store, EFFICIENCY_RESULT_KEY, EFFICIENCY_ADAPTER)
    assert result.charge_throughput_kwh == pytest.approx(2.0)
    assert result.discharge_throughput_kwh == pytest.approx(2.0)


def test_configured_forecast_solar_orchestrator_rejects_fast_polling(
    tmp_path: Path,
) -> None:
    runtime = runtime_configuration(
        tmp_path, "pv_generation", 299, forecast_solar=FORECAST_SOLAR
    )

    with pytest.raises(OrchestrationError, match="at least 300 seconds"):
        build_configured_orchestrator(runtime, ProviderDataStore(tmp_path))


def test_configured_home_assistant_failure_preserves_persisted_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingLoadImporter(FakeImporter):
        def plan(self, *_: object, **__: object) -> HistoryPlan[HouseholdLoadData]:
            raise RuntimeError(
                "Home Assistant returned no history for sensor.household_energy "
                "in the requested period"
            )

    patch_importer(monkeypatch, "HomeAssistantLoadImporter", FailingLoadImporter)
    store = ProviderDataStore(tmp_path)
    store.save(HOUSEHOLD_KEY, ADAPTER, home_assistant_load(START, value=2.5))
    orchestrator = configured(household_configuration(tmp_path), store)

    cycle = orchestrator.run_due(START + timedelta(hours=2))

    assert cycle.provider_runs[0].status == "failed"
    assert "no history for sensor.household_energy" in (
        cycle.provider_runs[0].error or ""
    )
    persisted = load_persisted(store, HOUSEHOLD_KEY, ADAPTER)
    assert persisted.load_kw == (2.5,)


def test_configured_home_assistant_fetch_bootstraps_to_ten_year_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_load_importer(
        monkeypatch,
        lambda start_time, end_time, _: home_assistant_load(end_time or start_time),
    )
    orchestrator = configured(
        household_configuration(tmp_path), ProviderDataStore(tmp_path)
    )

    now = START + timedelta(hours=12, minutes=34)
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
    fake_load_importer(
        monkeypatch,
        lambda *_: replace(
            home_assistant_load(START), start_time=retained_start, load_kw=(1.0, 2.0)
        ),
    )
    store = ProviderDataStore(tmp_path)

    cycle = configured(household_configuration(tmp_path), store).run_due(START)

    assert cycle.provider_runs[0].status == "success"
    persisted = load_persisted(store, HOUSEHOLD_KEY, ADAPTER)
    assert persisted.start_time == retained_start
    assert persisted.load_kw == (1.0, 2.0)


def test_configured_home_assistant_fetch_starts_after_persisted_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_load_importer(
        monkeypatch, lambda start_time, *_: home_assistant_load(start_time)
    )
    store = ProviderDataStore(tmp_path)
    store.save(
        HOUSEHOLD_KEY,
        ADAPTER,
        home_assistant_load(START + timedelta(hours=10)),
    )
    orchestrator = configured(household_configuration(tmp_path), store)

    now = START + timedelta(hours=12, minutes=34)
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
    calls = fake_load_importer(
        monkeypatch, lambda start_time, *_: home_assistant_load(start_time)
    )
    store = ProviderDataStore(tmp_path)
    store.save(
        HOUSEHOLD_KEY,
        ADAPTER,
        home_assistant_load(START + timedelta(hours=10)),
    )
    history_files = tuple(sorted(tmp_path.glob("*.ndjson*")))
    before = {path: path.read_bytes() for path in history_files}
    orchestrator = configured(household_configuration(tmp_path), store)

    first_cycle = orchestrator.run_due(START + timedelta(hours=11))
    not_due_cycle = orchestrator.run_due(START + timedelta(hours=11, minutes=4))
    second_cycle = orchestrator.run_due(START + timedelta(hours=11, minutes=5))

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
    aggregate = aggregate_settings(
        add=[energy_entity(add_entity)], subtract=[energy_entity(subtract_entity)]
    )
    store = ProviderDataStore(tmp_path)

    with home_assistant.client() as client:
        orchestrator = configured(
            household_configuration(tmp_path, aggregate), store, client
        )
        bootstrap_cycle = orchestrator.run_due(START + timedelta(hours=3, minutes=30))
        bootstrapped = load_persisted(store, HOUSEHOLD_KEY, ADAPTER)
        home_assistant.requests.clear()
        incremental_cycle = orchestrator.run_due(START + timedelta(hours=5, minutes=30))
        refreshed = load_persisted(store, HOUSEHOLD_KEY, ADAPTER)

    assert bootstrap_cycle.provider_runs[0].status == "success"
    assert bootstrap_cycle.provider_runs[0].error is None
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
    assert refreshed.start_time == START
    assert refreshed.load_kw == (0.5, None, 0.5, 1.0, 0.5)
    assert refreshed.exclusions == bootstrapped.exclusions
