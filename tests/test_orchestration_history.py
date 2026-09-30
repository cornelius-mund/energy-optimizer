"""Tests for the three-phase orchestration cycle and its shared history import.

Every due source first plans the Home Assistant history it needs, all plans are
imported once, and only then does each source build and persist its record.
"""

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

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
    OrchestrationConfiguration,
    PersistenceConfiguration,
    SolverConfiguration,
)
from energy_optimizer.orchestration import (
    ProviderOrchestrator,
    ProviderRegistration,
    build_configured_orchestrator,
)
from energy_optimizer.providers import home_assistant_energy
from energy_optimizer.providers.home_assistant_history import (
    HistoryNeed,
    HistoryPlan,
    HomeAssistantHistory,
    HomeAssistantHistoryImporter,
)
from energy_optimizer.providers.interfaces import (
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
    home_assistant_configuration_factory,
    plan_without_needs,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
ONE_HOUR = timedelta(hours=1)
RETENTION = timedelta(hours=87_672)
HOUSEHOLD_KEY = ProviderDataKey("household-load", "home-assistant", "household_load")
GRID_KEY = ProviderDataKey("grid-flow", "home-assistant", "grid_flow")
HISTORY_KEY = ProviderDataKey(
    "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
)
LOAD_ADAPTER = TypeAdapter(HouseholdLoadData)
GRID_ADAPTER = TypeAdapter(GridFlowData)

SOC = "sensor.battery_state_of_charge"
# Kilowatt-hours that each counter of the example configuration gains per hour.
RATES = {
    "sensor.household_energy": 3,
    "sensor.ev_energy": 1,
    "sensor.grid_import_energy": 2,
    "sensor.grid_export_energy": 1,
    "sensor.battery_energy_in": 2,
    "sensor.battery_energy_out": 2,
    "sensor.ac_into_inverter": 3,
    "sensor.mppt_energy": 1,
    "sensor.inverter_to_ac": 2,
}


def energy(
    entity_id: str,
    state_class: str = "total_increasing",
    **settings: Any,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state_class": state_class,
        "unit": "kWh",
        **settings,
    }


# The entity mapping of config.example.yaml: household load and grid import share
# sensor.grid_import_energy, and the efficiency legs repeat the battery and MPPT
# counters. Together they make 15 history fetches for 10 distinct entities.
# Household load is household + grid import - EV; inverter charge out is battery
# in - MPPT; inverter discharge in is battery out + MPPT - battery in.
HOUSEHOLD = aggregate_settings(
    add=[energy("sensor.household_energy"), energy("sensor.grid_import_energy")],
    subtract=[energy("sensor.ev_energy", "total")],
)
GRID_IMPORT = aggregate_settings(add=[energy("sensor.grid_import_energy")])
GRID_EXPORT = aggregate_settings(add=[energy("sensor.grid_export_energy")])


def efficiency_legs(part: Literal["net", "positive"]) -> dict[str, Any]:
    """The efficiency legs of the example; ``part`` applies to the two DC legs."""
    return {
        "battery": {
            "energy_in": aggregate_settings(add=[energy("sensor.battery_energy_in")]),
            "energy_out": aggregate_settings(add=[energy("sensor.battery_energy_out")]),
        },
        "inverter_charge": {
            "energy_in": aggregate_settings(add=[energy("sensor.ac_into_inverter")]),
            "energy_out": aggregate_settings(
                add=[energy("sensor.battery_energy_in")],
                subtract=[energy("sensor.mppt_energy")],
                part=part,
            ),
        },
        "inverter_discharge": {
            "energy_in": aggregate_settings(
                add=[
                    energy("sensor.battery_energy_out"),
                    energy("sensor.mppt_energy"),
                ],
                subtract=[energy("sensor.battery_energy_in")],
                part=part,
            ),
            "energy_out": aggregate_settings(add=[energy("sensor.inverter_to_ac")]),
        },
    }


EFFICIENCY = efficiency_legs("positive")
SCHEDULES = {
    "household_load": DataSourceScheduleConfiguration(
        interval_seconds=3600, history_lookback_seconds=3600
    ),
    "grid_flow": DataSourceScheduleConfiguration(interval_seconds=3600),
    "battery_efficiency": DataSourceScheduleConfiguration(interval_seconds=86_400),
}


LOAD_AND_GRID = {name: SCHEDULES[name] for name in ("household_load", "grid_flow")}


def hourly_states(rate: float, hours: int = 12) -> list[tuple[datetime, str]]:
    return [
        (BASE + timedelta(hours=hour), str(rate * hour)) for hour in range(hours + 1)
    ]


def with_samples(
    states: Sequence[tuple[datetime, str]], *samples: tuple[datetime, str]
) -> list[tuple[datetime, str]]:
    """Replace the state at a sample's time or add it, keeping time order."""
    return sorted((dict(states) | dict(samples)).items())


def example_home_assistant(
    replaced: Mapping[str, Sequence[tuple[datetime, str]]] | None = None,
    rates: Mapping[str, float] | None = None,
    **overrides: Any,
) -> FakeHomeAssistant:
    """Serve every counter of the example.

    ``replaced`` overrides single samples and ``rates`` the hourly gain of whole
    counters.
    """
    states = {
        entity: hourly_states(rate)
        for entity, rate in {**RATES, **(rates or {})}.items()
    }
    states[SOC] = [
        (BASE + timedelta(hours=hour), str(40 + 10 * (hour % 5))) for hour in range(13)
    ]
    for entity, samples in (replaced or {}).items():
        states[entity] = with_samples(states[entity], *samples)
    return FakeHomeAssistant(
        states,
        units={SOC: "%"},
        state_classes={SOC: "measurement", "sensor.ev_energy": "total"},
        **overrides,
    )


def runtime_configuration(
    tmp_path: Path,
    *,
    household: dict[str, Any] = HOUSEHOLD,
    grid_import: dict[str, Any] = GRID_IMPORT,
    grid_export: dict[str, Any] = GRID_EXPORT,
    efficiency: dict[str, Any] | None = EFFICIENCY,
    schedules: dict[str, DataSourceScheduleConfiguration] = SCHEDULES,
) -> Configuration:
    home_assistant: dict[str, Any] = {
        "base_url": "http://homeassistant.test:8123",
        "token": "test-token",
        "household_load": household,
        "grid_import": grid_import,
        "grid_export": grid_export,
        "timeout_seconds": 5,
    }
    if efficiency is not None:
        soc = {"entity_id": SOC, "unit": "%"}
        home_assistant["battery"] = {
            "state_of_charge": soc,
            "capacity": 10,
            "minimum_soc": 1,
            "maximum_soc": 10,
            "maximum_charge": 4,
            "maximum_discharge": 4,
            "efficiency_calculation": {"state_of_charge": soc, **efficiency},
        }
    return Configuration(
        time_resolution_minutes=60,
        grid=GridConfiguration(maximum_import_kw=10, maximum_export_kw=10),
        solver=SolverConfiguration(name="highs", time_limit_seconds=60),
        home_assistant=HomeAssistantConfiguration.model_validate(home_assistant),
        persistence=PersistenceConfiguration(directory=tmp_path),
        orchestration=OrchestrationConfiguration(enabled=True, sources=schedules),
    )


def chunks(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Return the contiguous seven-day request chunks of one range."""
    ranges: list[tuple[datetime, datetime]] = []
    while start < end:
        stop = min(start + timedelta(days=7), end)
        ranges.append((start, stop))
        start = stop
    return ranges


def requested_ranges(
    home_assistant: FakeHomeAssistant,
) -> dict[str, list[tuple[datetime, datetime]]]:
    return {
        entity: home_assistant.requested_ranges(entity)
        for entity in home_assistant.requested_entities()
    }


def statuses(cycle: Any) -> dict[str, str]:
    return {run.source: run.status for run in cycle.provider_runs}


@pytest.mark.parametrize("part", ["net", "positive"])
def test_a_pv_surplus_hour_is_zero_for_the_positive_part_and_excluded_for_net(
    tmp_path: Path, caplog: LogCaptureFixture, part: Literal["net", "positive"]
) -> None:
    """PV yield above the battery charge makes the AC-sourced charge negative.

    On a DC-coupled system that is an ordinary hour: PV charged the battery and
    the surplus was exported. The positive part imports it as no AC charging and
    reports the clamp in the log, so the hour does not remove the full-charge
    cycle it sits in. The net part treats the negative value as invalid data.
    """
    home_assistant = example_home_assistant(rates={"sensor.mppt_energy": 3})
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path, efficiency=efficiency_legs(part)),
        store,
        home_assistant_client=client,
    )
    assert orchestrator is not None

    try:
        with caplog.at_level(logging.INFO):
            cycle = orchestrator.run_due(BASE + 6 * ONE_HOUR + timedelta(minutes=30))
    finally:
        client.close()

    assert statuses(cycle)["battery_efficiency"] == "success"
    history = store.load(HISTORY_KEY, TypeAdapter(BatteryEfficiencyHistoryData))
    assert history is not None
    summary = [
        record.getMessage()
        for record in caplog.records
        if "event=home_assistant_history_aggregate" in record.getMessage()
        and "label=battery efficiency inverter_charge output" in record.getMessage()
    ]

    if part == "positive":
        # Hourly: battery in 2 - PV yield 3 = -1 kWh, imported as 0.
        assert history.exclusions == ()
        assert history.inverter_charge_energy_out_kwh == pytest.approx((0.0,) * 6)
        # Battery out 2 + PV yield 3 - battery in 2 stays positive.
        assert history.inverter_discharge_energy_in_kwh == pytest.approx((3.0,) * 6)
        assert history.battery_energy_in_kwh == pytest.approx((2.0,) * 6)
        (line,) = summary
        assert "part=positive" in line
        assert "clamped_hour_count=6" in line
        assert "excluded_hour_count=0" in line
    else:
        assert history.inverter_charge_energy_out_kwh == (None,) * 6
        assert history.battery_energy_in_kwh == (None,) * 6
        assert {
            cause.reason for item in history.exclusions for cause in item.causes
        } == {"combined_negative"}
        assert len(history.exclusions) == 6
        (line,) = summary
        assert "part=net" in line
        assert "clamped_hour_count=0" in line
        assert "excluded_hour_count=6" in line


def test_a_bootstrap_and_an_incremental_cycle_request_each_entity_once(
    tmp_path: Path,
) -> None:
    home_assistant = example_home_assistant()
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path), store, home_assistant_client=client
    )
    assert orchestrator is not None

    try:
        bootstrap = orchestrator.run_due(BASE + 6 * ONE_HOUR + timedelta(minutes=30))
        bootstrap_requests = requested_ranges(home_assistant)
        home_assistant.requests.clear()
        incremental = orchestrator.run_due(
            BASE + 9 * ONE_HOUR + timedelta(minutes=30), force=True
        )
        incremental_requests = requested_ranges(home_assistant)
    finally:
        client.close()

    assert statuses(bootstrap) == {
        "household_load": "success",
        "grid_flow": "success",
        "battery_efficiency": "success",
    }
    assert statuses(incremental) == statuses(bootstrap)

    # Bootstrap: ten years of history. Every one of the 10 distinct entities is
    # imported as one sequence of seven-day chunks. The entity that household
    # load and grid import share covers the earlier household lookback, and the
    # state of charge ends at the last complete hour, like every energy entity.
    end = BASE + 6 * ONE_HOUR
    start = end - RETENTION
    assert bootstrap_requests == {
        "sensor.household_energy": chunks(start - ONE_HOUR, end),
        "sensor.ev_energy": chunks(start - ONE_HOUR, end),
        "sensor.grid_import_energy": chunks(start - ONE_HOUR, end),
        "sensor.grid_export_energy": chunks(start, end),
        "sensor.battery_energy_in": chunks(start, end),
        "sensor.battery_energy_out": chunks(start, end),
        "sensor.ac_into_inverter": chunks(start, end),
        "sensor.mppt_energy": chunks(start, end),
        "sensor.inverter_to_ac": chunks(start, end),
        SOC: chunks(start, end),
    }

    # Steady state: only the three new hours, one request per distinct entity.
    persisted_end = BASE + 6 * ONE_HOUR
    new_end = BASE + 9 * ONE_HOUR
    assert incremental_requests == {
        "sensor.household_energy": [(persisted_end - ONE_HOUR, new_end)],
        "sensor.ev_energy": [(persisted_end - ONE_HOUR, new_end)],
        "sensor.grid_import_energy": [(persisted_end - ONE_HOUR, new_end)],
        "sensor.grid_export_energy": [(persisted_end, new_end)],
        "sensor.battery_energy_in": [(persisted_end, new_end)],
        "sensor.battery_energy_out": [(persisted_end, new_end)],
        "sensor.ac_into_inverter": [(persisted_end, new_end)],
        "sensor.mppt_energy": [(persisted_end, new_end)],
        "sensor.inverter_to_ac": [(persisted_end, new_end)],
        SOC: [(persisted_end, new_end)],
    }
    assert sum(len(ranges) for ranges in incremental_requests.values()) == 10

    # The shared series feed the same results an independent fetch would give.
    load = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
    grid = store.load(GRID_KEY, GRID_ADAPTER)
    history = store.load(HISTORY_KEY, TypeAdapter(BatteryEfficiencyHistoryData))
    assert load is not None
    assert grid is not None
    assert history is not None
    assert load.start_time == BASE
    assert load.load_kw == pytest.approx((4.0,) * 9)
    assert grid.import_kw == pytest.approx((2.0,) * 9)
    assert grid.export_kw == pytest.approx((1.0,) * 9)
    # The battery and MPPT counters feed several legs from one import each.
    assert history.battery_energy_in_kwh == pytest.approx((2.0,) * 9)
    assert history.inverter_charge_energy_in_kwh == pytest.approx((3.0,) * 9)
    assert history.inverter_charge_energy_out_kwh == pytest.approx((1.0,) * 9)
    assert history.inverter_discharge_energy_in_kwh == pytest.approx((1.0,) * 9)
    assert load.exclusions == ()
    assert grid.exclusions == ()
    assert history.exclusions == ()


def test_identical_normalizations_within_a_cycle_are_computed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    computed: Counter[str] = Counter()
    original = home_assistant_energy.normalize_counter_history

    def counting(entity: Any, *arguments: Any) -> Any:
        computed[entity.entity_id] += 1
        return original(entity, *arguments)

    monkeypatch.setattr(home_assistant_energy, "normalize_counter_history", counting)
    home_assistant = example_home_assistant()
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path),
        ProviderDataStore(tmp_path),
        home_assistant_client=client,
    )
    assert orchestrator is not None

    try:
        orchestrator.run_due(BASE + 6 * ONE_HOUR)
    finally:
        client.close()

    # The efficiency legs use battery in three times, battery out and MPPT twice
    # each, with identical settings and windows: those are normalized once. The
    # entity that household load and grid import share has two different windows:
    # twice.
    assert computed == Counter(
        {
            "sensor.household_energy": 1,
            "sensor.ev_energy": 1,
            "sensor.grid_import_energy": 2,
            "sensor.grid_export_energy": 1,
            "sensor.battery_energy_in": 1,
            "sensor.battery_energy_out": 1,
            "sensor.ac_into_inverter": 1,
            "sensor.mppt_energy": 1,
            "sensor.inverter_to_ac": 1,
        }
    )


def test_a_shared_entity_is_fetched_once_and_normalized_with_each_aggregates_limit(
    tmp_path: Path,
) -> None:
    shared = "sensor.shared_energy"
    # A 150 kWh jump within one hour.
    jump = [
        (BASE + timedelta(hours=hour), str(kwh))
        for hour, kwh in enumerate([0, 1, 2, 152, 153])
    ]
    home_assistant = FakeHomeAssistant(
        {shared: jump, "sensor.grid_export_energy": hourly_states(1, 4)}
    )
    configuration = runtime_configuration(
        tmp_path,
        household=aggregate_settings(
            add=[energy(shared, maximum_interval_energy_kwh=10)]
        ),
        grid_import=aggregate_settings(
            add=[energy(shared, maximum_interval_energy_kwh=500)]
        ),
        efficiency=None,
        schedules={
            "household_load": SCHEDULES["grid_flow"],
            "grid_flow": SCHEDULES["grid_flow"],
        },
    )
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        configuration, store, home_assistant_client=client
    )
    assert orchestrator is not None

    try:
        cycle = orchestrator.run_due(BASE + 4 * ONE_HOUR)
    finally:
        client.close()

    # One request sequence serves both aggregates.
    end = BASE + 4 * ONE_HOUR
    assert home_assistant.requested_ranges(shared) == chunks(end - RETENTION, end)
    # The household limit of 10 kWh excludes the hour of the jump; the grid limit
    # of 500 kWh accepts it.
    assert statuses(cycle) == {"household_load": "success", "grid_flow": "success"}
    load = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
    grid = store.load(GRID_KEY, GRID_ADAPTER)
    assert load is not None
    assert grid is not None
    assert load.load_kw == (1.0, 1.0, None, 1.0)
    (excluded,) = load.exclusions
    assert excluded.hour_start == BASE + 2 * ONE_HOUR
    (cause,) = excluded.causes
    assert cause.reason == "step_above_maximum"
    assert cause.entity_id == shared
    (point,) = cause.data_points
    assert point.previous_timestamp == BASE + 2 * ONE_HOUR
    assert point.timestamp == BASE + 3 * ONE_HOUR
    assert (point.previous_value, point.value) == (2.0, 152.0)
    assert (point.step_kwh, point.maximum_kwh) == (150.0, 10.0)
    assert grid.import_kw == (1.0, 1.0, 150.0, 1.0)
    assert grid.exclusions == ()


HOUSEHOLD_ONLY = {
    "household_load": DataSourceScheduleConfiguration(interval_seconds=3600)
}


def household_configuration(tmp_path: Path, entity_id: str) -> Configuration:
    return runtime_configuration(
        tmp_path,
        household=aggregate_settings(add=[energy(entity_id)]),
        efficiency=None,
        schedules=HOUSEHOLD_ONLY,
    )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("unavailable", "unavailable"),
        ("unknown", "unavailable"),
        ("nan", "not_finite"),
        ("inf", "not_finite"),
        ("not-a-number", "non_numeric"),
        ("-5", "negative_value"),
    ],
)
def test_a_permanently_bad_sample_never_blocks_a_later_refresh(
    tmp_path: Path, state: str, reason: str
) -> None:
    """A bad sample excludes its own hour and never stops the history advancing.

    Before excluded hours, a bad sample made the whole fetch raise. Nothing was
    persisted, so the persisted end never moved past the sample and every later
    refresh requested the same range and failed the same way, for as long as
    Home Assistant kept the sample.
    """
    entity = "sensor.household_energy"
    bad_at = BASE + 3 * ONE_HOUR + timedelta(minutes=30)
    home_assistant = FakeHomeAssistant(
        {entity: with_samples(hourly_states(3), (bad_at, state))}
    )
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        household_configuration(tmp_path, entity), store, home_assistant_client=client
    )
    assert orchestrator is not None

    try:
        first = orchestrator.run_due(BASE + 6 * ONE_HOUR + timedelta(minutes=30))
        after_first = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
        home_assistant.requests.clear()
        second = orchestrator.run_due(BASE + 9 * ONE_HOUR + timedelta(minutes=30))
        after_second = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
    finally:
        client.close()

    # Hour 3 covers (03:00, 04:00], which holds the bad sample. Every other hour
    # of the first import is valid, and 3 kWh per hour is 3 kW.
    assert statuses(first) == {"household_load": "success"}
    assert after_first is not None
    assert after_first.start_time == BASE
    assert after_first.load_kw == (3.0, 3.0, 3.0, None, 3.0, 3.0)
    (excluded,) = after_first.exclusions
    assert excluded.hour_start == BASE + 3 * ONE_HOUR
    (cause,) = excluded.causes
    assert cause.reason == reason
    assert cause.entity_id == entity
    (point,) = cause.data_points
    assert (point.timestamp, point.state) == (bad_at, state)

    # The second refresh runs from the persisted end and never asks for the
    # bad sample again.
    assert statuses(second) == {"household_load": "success"}
    assert home_assistant.requested_ranges(entity) == [
        (BASE + 6 * ONE_HOUR, BASE + 9 * ONE_HOUR)
    ]
    assert after_second is not None
    assert len(after_second.load_kw) == 9 > len(after_first.load_kw)
    assert after_second.load_kw == (3.0, 3.0, 3.0, None) + (3.0,) * 5
    assert after_second.exclusions == after_first.exclusions


def test_an_entity_that_stays_unavailable_excludes_hours_across_refreshes(
    tmp_path: Path,
) -> None:
    """An outage that outlasts a refresh excludes hours on both sides of it."""
    entity = "sensor.household_energy"
    # Home Assistant records only changes: the entity goes unavailable at 05:30
    # and reports nothing until it returns at 08:00.
    home_assistant = FakeHomeAssistant(
        {
            entity: with_samples(
                [
                    (timestamp, state)
                    for timestamp, state in hourly_states(3)
                    if not BASE + 5 * ONE_HOUR < timestamp < BASE + 8 * ONE_HOUR
                ],
                (BASE + 5 * ONE_HOUR + timedelta(minutes=30), "unavailable"),
            )
        }
    )
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        household_configuration(tmp_path, entity), store, home_assistant_client=client
    )
    assert orchestrator is not None

    try:
        first = orchestrator.run_due(BASE + 6 * ONE_HOUR + timedelta(minutes=30))
        after_first = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
        second = orchestrator.run_due(BASE + 9 * ONE_HOUR + timedelta(minutes=30))
        after_second = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
    finally:
        client.close()

    assert statuses(first) == statuses(second) == {"household_load": "success"}
    # The outage covers hours 5 and 6 and ends with the return in hour 7, whose
    # step is unknown, so all three have no value. Hour 8 is the first valid one.
    assert after_first is not None
    assert after_first.load_kw == (3.0, 3.0, 3.0, 3.0, 3.0, None)
    assert after_second is not None
    assert after_second.load_kw == (3.0,) * 5 + (None,) * 3 + (3.0,)
    assert [item.hour_start for item in after_second.exclusions] == [
        BASE + hour * ONE_HOUR for hour in (5, 6, 7)
    ]
    # The second refresh sees the outage only as the state that Home Assistant
    # reports as in force at the start of the requested period.
    outage = after_second.exclusions[1].causes[0]
    assert outage.reason == "unavailable"
    assert [(point.timestamp, point.state) for point in outage.data_points] == [
        (BASE + 6 * ONE_HOUR, "unavailable")
    ]


def test_each_source_logs_its_excluded_hours_once_per_refresh(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    home_assistant = example_home_assistant(
        replaced={
            # Household load and grid flow both read the grid import counter.
            "sensor.grid_import_energy": [
                (BASE + 2 * ONE_HOUR + timedelta(minutes=30), "unavailable"),
                (BASE + 3 * ONE_HOUR, "unavailable"),
            ],
            # Only the efficiency source reads the battery counters.
            "sensor.battery_energy_in": [
                (BASE + 4 * ONE_HOUR + timedelta(minutes=30), "nan")
            ],
        }
    )
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path), store, home_assistant_client=client
    )
    assert orchestrator is not None

    def summaries() -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if "provider_hours_excluded" in record.getMessage()
        ]

    try:
        with caplog.at_level(logging.INFO, logger="energy_optimizer.orchestration"):
            first = orchestrator.run_due(BASE + 6 * ONE_HOUR + timedelta(minutes=30))
            first_summaries = summaries()
            caplog.clear()
            second = orchestrator.run_due(
                BASE + 9 * ONE_HOUR + timedelta(minutes=30), force=True
            )
            second_summaries = summaries()
    finally:
        client.close()

    success = {
        "household_load": "success",
        "grid_flow": "success",
        "battery_efficiency": "success",
    }
    assert statuses(first) == statuses(second) == success
    # One warning per source, with the counts of the hours it just imported.
    prefix = "event=provider_hours_excluded component=orchestration operation=refresh"
    assert first_summaries == [
        f"{prefix} source=household_load excluded_hour_count=2 reasons=unavailable:2 "
        "first_hour=2026-01-01T02:00:00+00:00 last_hour=2026-01-01T03:00:00+00:00",
        f"{prefix} source=grid_flow excluded_hour_count=2 reasons=unavailable:2 "
        "first_hour=2026-01-01T02:00:00+00:00 last_hour=2026-01-01T03:00:00+00:00",
        f"{prefix} source=battery_efficiency excluded_hour_count=1 "
        "reasons=not_finite:1 first_hour=2026-01-01T04:00:00+00:00 "
        "last_hour=2026-01-01T04:00:00+00:00",
    ]
    # The persisted excluded hours are not reported again by a clean refresh.
    assert second_summaries == []

    load = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
    grid = store.load(GRID_KEY, GRID_ADAPTER)
    history = store.load(HISTORY_KEY, TypeAdapter(BatteryEfficiencyHistoryData))
    assert load is not None
    assert grid is not None
    assert history is not None
    assert load.load_kw == (4.0, 4.0, None, None) + (4.0,) * 5
    # Grid flow excludes an hour for both channels, though only the import
    # counter was bad.
    assert grid.import_kw == (2.0, 2.0, None, None) + (2.0,) * 5
    assert grid.export_kw == (1.0, 1.0, None, None) + (1.0,) * 5
    # Every leg of the efficiency history loses the hour that one counter lost.
    for values in (
        history.battery_energy_in_kwh,
        history.battery_energy_out_kwh,
        history.inverter_charge_energy_in_kwh,
        history.inverter_charge_energy_out_kwh,
        history.inverter_discharge_energy_in_kwh,
        history.inverter_discharge_energy_out_kwh,
    ):
        assert len(values) == 9
        assert [index for index, value in enumerate(values) if value is None] == [4]


class RecordingHistoryImporter(HomeAssistantHistoryImporter):
    """Record when an import starts and finishes."""

    def __init__(self, home_assistant: FakeHomeAssistant, events: list[str]) -> None:
        super().__init__(
            home_assistant_configuration_factory()(), home_assistant.client()
        )
        self.events = events

    def import_history(self, needs: Any) -> HomeAssistantHistory:
        self.events.append("import started")
        history = super().import_history(needs)
        self.events.append("import finished")
        return history


def load_data(now: datetime, source: str) -> HouseholdLoadData:
    return HouseholdLoadData(
        schema_version="1",
        start_time=now.replace(minute=0, second=0, microsecond=0),
        interval_minutes=60,
        load_kw=(1.0,),
        unit="kW",
        source=SourceMetadata(provider="test", entity_id=source),
        retrieved_at=now,
        latest_observation_at=now,
    )


def registration(
    name: str,
    plan: Any,
) -> ProviderRegistration:
    return ProviderRegistration(
        name=name,
        data_type=name.replace("_", "-"),
        adapter=LOAD_ADAPTER,
        plan=plan,
        is_fresh=lambda data, now: True,
    )


def schedules(*names: str) -> OrchestrationConfiguration:
    return OrchestrationConfiguration(
        enabled=True,
        sources={
            name: DataSourceScheduleConfiguration(interval_seconds=60) for name in names
        },
    )


def counter_plan(name: str, entity_id: str, events: list[str] | None = None) -> Any:
    """Plan a source that reads one counter's last two hours."""

    def plan(now: datetime, schedule: Any) -> HistoryPlan[HouseholdLoadData]:
        if events is not None:
            events.append(f"plan {name}")
        need = HistoryNeed(entity_id, "counter", now - 2 * ONE_HOUR, now)

        def build(history: HomeAssistantHistory) -> HouseholdLoadData:
            if events is not None:
                events.append(f"build {name}")
            history.window(entity_id, "counter", need.start_time, need.end_time)
            return load_data(now, name)

        return HistoryPlan(needs=(need,), build=build)

    return plan


def self_contained_plan(name: str, events: list[str]) -> Any:
    """Plan a source that needs no Home Assistant history."""

    def plan(now: datetime, schedule: Any) -> HistoryPlan[HouseholdLoadData]:
        events.append(f"plan {name}")

        def build() -> HouseholdLoadData:
            events.append(f"build {name}")
            return load_data(now, name)

        return plan_without_needs(build)

    return plan


def purged_home_assistant(first_hour: int, last_hour: int) -> FakeHomeAssistant:
    """Serve the example counters only from ``first_hour``, as after a purge."""
    hours = range(first_hour, last_hour + 1)
    states = {
        entity: [(BASE + hour * ONE_HOUR, str(rate * hour)) for hour in hours]
        for entity, rate in RATES.items()
    }
    states[SOC] = [
        (BASE + hour * ONE_HOUR, str(40 + 10 * (hour % 5))) for hour in hours
    ]
    return FakeHomeAssistant(
        states,
        units={SOC: "%"},
        state_classes={SOC: "measurement", "sensor.ev_energy": "total"},
    )


def test_hours_home_assistant_no_longer_holds_are_excluded_and_never_block_refreshes(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    """Hours purged from the recorder while the service was down cannot be
    fetched by any retry, so they become excluded hours and the refresh moves on.
    """
    store = ProviderDataStore(tmp_path)
    first_client = example_home_assistant().client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path), store, home_assistant_client=first_client
    )
    assert orchestrator is not None
    try:
        first = orchestrator.run_due(BASE + 6 * ONE_HOUR + timedelta(minutes=30))
    finally:
        first_client.close()
    # Home Assistant now retains hour 9 onward only; hours 6 to 8 are gone.
    purged = purged_home_assistant(first_hour=9, last_hour=12)
    client = purged.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path), store, home_assistant_client=client
    )
    assert orchestrator is not None

    try:
        with caplog.at_level(logging.WARNING):
            second = orchestrator.run_due(
                BASE + 12 * ONE_HOUR + timedelta(minutes=30), force=True
            )
    finally:
        client.close()

    success = {
        "household_load": "success",
        "grid_flow": "success",
        "battery_efficiency": "success",
    }
    assert statuses(first) == statuses(second) == success
    load = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
    grid = store.load(GRID_KEY, GRID_ADAPTER)
    efficiency = store.load(HISTORY_KEY, TypeAdapter(BatteryEfficiencyHistoryData))
    assert load is not None
    assert grid is not None
    assert efficiency is not None
    missing = (None,) * 3
    assert load.start_time == grid.start_time == efficiency.start_time == BASE
    assert load.load_kw[6:9] == missing
    assert load.load_kw[:6] + load.load_kw[9:] == pytest.approx((4.0,) * 9)
    assert grid.import_kw[6:9] == grid.export_kw[6:9] == missing
    assert grid.import_kw[:6] + grid.import_kw[9:] == pytest.approx((2.0,) * 9)
    for leg in (
        efficiency.battery_energy_in_kwh,
        efficiency.inverter_charge_energy_out_kwh,
        efficiency.inverter_discharge_energy_in_kwh,
    ):
        assert len(leg) == 12
        assert leg[6:9] == missing
        assert None not in leg[:6] + leg[9:]
    assert len(efficiency.state_of_charge_percent) == 12 + 1
    assert efficiency.state_of_charge_percent[6:10] == (None,) * 4
    gap_hours = [BASE + hour * ONE_HOUR for hour in (6, 7, 8)]
    for excluded in (load.exclusions, grid.exclusions, efficiency.exclusions):
        assert [item.hour_start for item in excluded] == gap_hours
        assert all(
            [cause.reason for cause in item.causes] == ["history_unavailable"]
            for item in excluded
        )
        assert (
            "2026-01-01T06:00:00+00:00 until 2026-01-01T09:00:00+00:00 (3 hours)"
            in (excluded[0].causes[0].message)
        )
    # One warning per source names the missing hours, so the gap is visible.
    unavailable = [
        record.getMessage()
        for record in caplog.records
        if "event=provider_history_unavailable" in record.getMessage()
    ]
    assert unavailable == [
        "event=provider_history_unavailable component=storage operation=merge "
        f"source={source} hour_count=3 first_hour=2026-01-01T06:00:00+00:00 "
        "last_hour=2026-01-01T08:00:00+00:00"
        for source in ("household_load", "grid_flow", "battery_efficiency")
    ]

    # The next refresh continues after the new end instead of asking again for
    # the hours Home Assistant does not hold.
    later = purged_home_assistant(first_hour=9, last_hour=14)
    later_client = later.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path), store, home_assistant_client=later_client
    )
    assert orchestrator is not None
    try:
        third = orchestrator.run_due(
            BASE + 14 * ONE_HOUR + timedelta(minutes=30), force=True
        )
    finally:
        later_client.close()
    assert statuses(third) == success
    requested = requested_ranges(later)
    persisted_end = BASE + 12 * ONE_HOUR
    assert requested["sensor.grid_export_energy"] == [
        (persisted_end, BASE + 14 * ONE_HOUR)
    ]
    assert requested["sensor.household_energy"] == [
        (persisted_end - ONE_HOUR, BASE + 14 * ONE_HOUR)
    ]
    assert requested[SOC] == [(persisted_end, BASE + 14 * ONE_HOUR)]
    final = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
    assert final is not None
    assert final.load_kw[6:9] == missing
    assert final.load_kw[:6] + final.load_kw[9:] == pytest.approx((4.0,) * 11)
    assert len(final.exclusions) == 3


def test_every_plan_precedes_the_import_and_the_import_precedes_every_build(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    home_assistant = FakeHomeAssistant(
        {
            "sensor.a": hourly_states(1),
            "sensor.b": hourly_states(1),
        }
    )
    importer = RecordingHistoryImporter(home_assistant, events)
    orchestrator = ProviderOrchestrator(
        schedules("first", "second", "third"),
        [
            registration("first", counter_plan("first", "sensor.a", events)),
            registration("second", counter_plan("second", "sensor.b", events)),
            registration("third", self_contained_plan("third", events)),
        ],
        ProviderDataStore(tmp_path),
        history_importer=importer,
    )

    cycle = orchestrator.run_due(BASE + 4 * ONE_HOUR)

    assert statuses(cycle) == {
        "first": "success",
        "second": "success",
        "third": "success",
    }
    assert events == [
        "plan first",
        "plan second",
        "plan third",
        "import started",
        "import finished",
        "build first",
        "build second",
        "build third",
    ]
    # The two sources needed different entities: each was requested once.
    assert home_assistant.requested_entities() == ["sensor.a", "sensor.b"]


def test_a_source_that_is_not_due_or_has_nothing_missing_causes_no_request(
    tmp_path: Path,
) -> None:
    home_assistant = example_home_assistant()
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path),
        ProviderDataStore(tmp_path),
        home_assistant_client=client,
    )
    assert orchestrator is not None
    now = BASE + 6 * ONE_HOUR + timedelta(minutes=30)

    try:
        orchestrator.run_due(now)
        home_assistant.requests.clear()
        not_due = orchestrator.run_due(now + timedelta(minutes=10))
        nothing_missing = orchestrator.run_due(now + timedelta(minutes=20), force=True)
    finally:
        client.close()

    assert {run.error for run in not_due.provider_runs} == {"source is not due"}
    # Household load and grid flow have every completed hour. The efficiency
    # history is recalculated from the persisted record, again without a request.
    assert [
        (run.source, run.status, run.error) for run in nothing_missing.provider_runs
    ] == [
        ("household_load", "skipped", "no missing completed hours"),
        ("grid_flow", "skipped", "no missing completed hours"),
        ("battery_efficiency", "success", None),
    ]
    assert home_assistant.requests == []


def test_a_cycle_with_only_forecast_and_price_sources_makes_no_home_assistant_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeForecastSolarImporter:
        def __init__(self, _: ForecastSolarConfiguration) -> None:
            pass

        def fetch(
            self, start_time: datetime, *, now: datetime | None = None
        ) -> PvGenerationData:
            assert now is not None
            return PvGenerationData(
                schema_version="1",
                start_time=start_time,
                interval_minutes=60,
                generation_kw=(1.0,),
                unit="kW",
                source=SourceMetadata(
                    provider="forecast.solar", entity_id="pv_generation"
                ),
                retrieved_at=now,
                expires_at=start_time + timedelta(hours=24),
            )

        def is_fresh(self, _: PvGenerationData, now: datetime) -> bool:
            return True

    class FakeAwattarImporter:
        def __init__(self, _: AwattarConfiguration) -> None:
            pass

        def fetch(
            self, start_time: datetime, *, now: datetime | None = None
        ) -> ElectricityPriceData:
            assert now is not None
            return ElectricityPriceData(
                schema_version="1",
                timestamps=(start_time,),
                interval_minutes=60,
                import_price_eur_per_kwh=(0.3,),
                export_price_eur_per_kwh=(0.1,),
                unit="EUR/kWh",
                source=SourceMetadata(provider="awattar.de", entity_id="de"),
                retrieved_at=now,
                expires_at=start_time + timedelta(hours=1),
            )

        def is_fresh(self, _: ElectricityPriceData, now: datetime) -> bool:
            return True

    monkeypatch.setattr(
        "energy_optimizer.orchestration.ForecastSolarImporter",
        FakeForecastSolarImporter,
    )
    monkeypatch.setattr(
        "energy_optimizer.orchestration.AwattarImporter", FakeAwattarImporter
    )

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Home Assistant request: {request.url}")

    configuration = runtime_configuration(
        tmp_path,
        efficiency=None,
        schedules={
            "pv_generation": DataSourceScheduleConfiguration(interval_seconds=3600),
            "electricity_prices": DataSourceScheduleConfiguration(
                interval_seconds=3600
            ),
        },
    ).model_copy(
        update={
            "forecast_solar": ForecastSolarConfiguration(
                latitude=52.52,
                longitude=13.41,
                declination_degrees=35,
                azimuth_degrees=0,
                peak_power_kw=8,
            ),
            "awattar": AwattarConfiguration(),
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(refuse))
    orchestrator = build_configured_orchestrator(
        configuration, ProviderDataStore(tmp_path), home_assistant_client=client
    )
    assert orchestrator is not None

    try:
        cycle = orchestrator.run_due(BASE + 2 * ONE_HOUR)
    finally:
        client.close()

    assert statuses(cycle) == {
        "pv_generation": "success",
        "electricity_prices": "success",
    }


def test_a_failing_entity_fails_only_the_sources_that_need_it(tmp_path: Path) -> None:
    home_assistant = example_home_assistant()
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path, efficiency=None, schedules=LOAD_AND_GRID),
        store,
        home_assistant_client=client,
    )
    assert orchestrator is not None

    try:
        orchestrator.run_due(BASE + 6 * ONE_HOUR + timedelta(minutes=30))
        last_valid_load = store.load(HOUSEHOLD_KEY, LOAD_ADAPTER)
        home_assistant.failures["sensor.ev_energy"] = 503
        cycle = orchestrator.run_due(
            BASE + 9 * ONE_HOUR + timedelta(minutes=30), force=True
        )
    finally:
        client.close()

    # Only household load reads sensor.ev_energy, and the error names it.
    assert statuses(cycle) == {"household_load": "failed", "grid_flow": "success"}
    error = next(
        run.error for run in cycle.provider_runs if run.source == "household_load"
    )
    assert error is not None
    assert "sensor.ev_energy" in error
    assert "HTTP 503" in error
    # The failed source keeps its last valid data; the other source advanced.
    assert store.load(HOUSEHOLD_KEY, LOAD_ADAPTER) == last_valid_load
    grid = store.load(GRID_KEY, GRID_ADAPTER)
    assert grid is not None
    assert len(grid.import_kw) == 9


def test_an_entity_shared_with_a_failing_source_still_reaches_the_other_source(
    tmp_path: Path,
) -> None:
    home_assistant = example_home_assistant()
    store = ProviderDataStore(tmp_path)
    client = home_assistant.client()
    orchestrator = build_configured_orchestrator(
        runtime_configuration(tmp_path, efficiency=None, schedules=LOAD_AND_GRID),
        store,
        home_assistant_client=client,
    )
    assert orchestrator is not None

    try:
        # sensor.grid_import_energy is read by household load and by grid flow.
        home_assistant.failures["sensor.grid_import_energy"] = 503
        cycle = orchestrator.run_due(BASE + 6 * ONE_HOUR)
    finally:
        client.close()

    assert statuses(cycle) == {"household_load": "failed", "grid_flow": "failed"}
    assert store.load(HOUSEHOLD_KEY, LOAD_ADAPTER) is None
    assert store.load(GRID_KEY, GRID_ADAPTER) is None


def test_an_exception_while_planning_one_source_fails_only_that_source(
    tmp_path: Path,
) -> None:
    def failing_plan(now: datetime, schedule: Any) -> HistoryPlan[HouseholdLoadData]:
        raise RuntimeError("cannot plan")

    home_assistant = FakeHomeAssistant({"sensor.a": hourly_states(1)})
    events: list[str] = []
    store = ProviderDataStore(tmp_path)
    orchestrator = ProviderOrchestrator(
        schedules("broken", "healthy"),
        [
            registration("broken", failing_plan),
            registration("healthy", counter_plan("healthy", "sensor.a", events)),
        ],
        store,
        history_importer=RecordingHistoryImporter(home_assistant, events),
    )

    cycle = orchestrator.run_due(BASE + 4 * ONE_HOUR)

    assert [(run.source, run.status, run.error) for run in cycle.provider_runs] == [
        ("broken", "failed", "cannot plan"),
        ("healthy", "success", None),
    ]
    assert events == [
        "plan healthy",
        "import started",
        "import finished",
        "build healthy",
    ]
    assert store.load(ProviderDataKey("broken", "test", "broken"), LOAD_ADAPTER) is None
    assert store.load(ProviderDataKey("healthy", "test", "healthy"), LOAD_ADAPTER)


def test_planning_one_entity_with_two_kinds_fails_every_source_that_needs_history(
    tmp_path: Path,
) -> None:
    def state_plan(now: datetime, schedule: Any) -> HistoryPlan[HouseholdLoadData]:
        return HistoryPlan(
            needs=(HistoryNeed("sensor.a", "state", now - ONE_HOUR, now),),
            build=lambda history: load_data(now, "state_source"),
        )

    home_assistant = FakeHomeAssistant({"sensor.a": hourly_states(1)})
    events: list[str] = []
    orchestrator = ProviderOrchestrator(
        schedules("counter_source", "state_source", "independent"),
        [
            registration("counter_source", counter_plan("counter_source", "sensor.a")),
            registration("state_source", state_plan),
            registration(
                "independent",
                lambda now, schedule: plan_without_needs(
                    lambda: load_data(now, "independent")
                ),
            ),
        ],
        ProviderDataStore(tmp_path),
        history_importer=RecordingHistoryImporter(home_assistant, events),
    )

    cycle = orchestrator.run_due(BASE + 4 * ONE_HOUR)

    assert statuses(cycle) == {
        "counter_source": "failed",
        "state_source": "failed",
        "independent": "success",
    }
    assert all(
        "both counter and state" in (run.error or "")
        for run in cycle.provider_runs
        if run.status == "failed"
    )
    assert home_assistant.requests == []


def test_history_needs_without_a_history_importer_fail_the_source(
    tmp_path: Path,
) -> None:
    orchestrator = ProviderOrchestrator(
        schedules("needy"),
        [registration("needy", counter_plan("needy", "sensor.a"))],
        ProviderDataStore(tmp_path),
    )

    cycle = orchestrator.run_due(BASE + 4 * ONE_HOUR)

    assert cycle.provider_runs[0].status == "failed"
    assert "history importer is required" in (cycle.provider_runs[0].error or "")
