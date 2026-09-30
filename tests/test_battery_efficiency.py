"""Tests for measured battery and inverter efficiency calculation."""

import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from _pytest.logging import LogCaptureFixture

from energy_optimizer.config import (
    BatteryEfficiencyLegConfiguration,
    HomeAssistantBatteryEfficiencyConfiguration,
    HomeAssistantBatteryEntityConfiguration,
    HomeAssistantEnergyEntityConfiguration,
)
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
    HourExclusion,
)
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    HomeAssistantBatteryEfficiencyImporter,
    bound_battery_efficiency_history,
    calculate_battery_efficiency,
    merge_battery_efficiency_history,
)
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyHistoryData,
    SourceMetadata,
)
from home_assistant_fixtures import (
    FakeHomeAssistant,
    home_assistant_configuration_factory,
    home_assistant_history_payload,
    home_assistant_jittery_total_readings,
    import_and_build,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def entity(
    name: str, operation: str = "add", state_class: str = "total_increasing"
) -> dict[str, object]:
    return {
        "entity_id": f"sensor.{name}",
        "state_class": state_class,
        "unit": "kWh",
        "operation": operation,
    }


def calculation_configuration() -> HomeAssistantBatteryEfficiencyConfiguration:
    def leg(name: str) -> BatteryEfficiencyLegConfiguration:
        return BatteryEfficiencyLegConfiguration(
            energy_in=[
                HomeAssistantEnergyEntityConfiguration.model_validate(
                    entity(f"{name}_in")
                )
            ],
            energy_out=[
                HomeAssistantEnergyEntityConfiguration.model_validate(
                    entity(f"{name}_out")
                )
            ],
        )

    return HomeAssistantBatteryEfficiencyConfiguration(
        battery=leg("battery"),
        inverter_charge=leg("charge"),
        inverter_discharge=leg("discharge"),
        state_of_charge=HomeAssistantBatteryEntityConfiguration(
            entity_id="sensor.soc", unit="%"
        ),
        minimum_battery_throughput_kwh=1,
        minimum_inverter_charge_throughput_kwh=1,
        minimum_inverter_discharge_throughput_kwh=1,
    )


def exclusion(
    hour: int,
    reason: ExclusionReason = "counter_decrease",
    entity_id: str = "sensor.battery_in",
) -> HourExclusion:
    hour_start = START + timedelta(hours=hour)
    return HourExclusion(
        hour_start,
        (
            ExclusionCause.of(
                reason,
                f"{entity_id} is excluded in hour {hour}",
                entity_id,
                [ExcludedDataPoint(hour_start, state="2", unit="kWh")],
            ),
        ),
    )


def history(
    *,
    battery_in: tuple[float | None, ...] = (0, 5, 0, 5, 0, 5),
    battery_out: tuple[float | None, ...] = (0, 4, 0, 4, 0, 4),
    soc: tuple[float | None, ...] = (50, 100, 50, 100, 50, 100, 50),
    excluded: tuple[int, ...] = (),
) -> BatteryEfficiencyHistoryData:
    """Build an aligned history; an excluded hour has no value in any leg."""

    def leg(values: tuple[float | None, ...]) -> tuple[float | None, ...]:
        return tuple(
            None if index in excluded else value for index, value in enumerate(values)
        )

    return BatteryEfficiencyHistoryData(
        schema_version="1",
        start_time=START,
        interval_minutes=60,
        battery_energy_in_kwh=leg(battery_in),
        battery_energy_out_kwh=leg(battery_out),
        inverter_charge_energy_in_kwh=leg((10,) * len(battery_in)),
        inverter_charge_energy_out_kwh=leg((9,) * len(battery_in)),
        inverter_discharge_energy_in_kwh=leg((10,) * len(battery_in)),
        inverter_discharge_energy_out_kwh=leg((8,) * len(battery_in)),
        state_of_charge_percent=soc,
        unit="kWh",
        source=SourceMetadata(provider="home-assistant", entity_id="history"),
        retrieved_at=START,
        latest_observation_at=START,
        exclusions=tuple(exclusion(index) for index in sorted(excluded)),
    )


def test_calculation_uses_one_battery_round_trip_and_two_inverter_components() -> None:
    result = calculate_battery_efficiency(
        history(), calculation_configuration(), capacity_kwh=10, now=START
    )

    assert result.status == "ok"
    assert result.battery_efficiency == pytest.approx(0.8)
    assert result.inverter_charge_efficiency == pytest.approx(0.9)
    assert result.inverter_discharge_efficiency == pytest.approx(0.8)
    assert result.round_trip_efficiency == pytest.approx(0.576)
    assert result.complete_cycle_count == 2


def test_calculation_clamps_measurement_noise_above_one() -> None:
    result = calculate_battery_efficiency(
        history(battery_out=(0, 6, 0, 6, 0, 6)), calculation_configuration(), now=START
    )

    assert result.status == "ok"
    assert result.battery_efficiency == 1


def test_calculation_defaults_only_unavailable_components() -> None:
    configuration = calculation_configuration().model_copy(
        update={"minimum_inverter_charge_throughput_kwh": 100}
    )

    result = calculate_battery_efficiency(
        history(), configuration, capacity_kwh=10, now=START
    )

    assert result.status == "insufficient_data"
    assert result.battery_efficiency == pytest.approx(0.8)
    assert result.inverter_charge_efficiency == pytest.approx(0.95)
    assert result.inverter_discharge_efficiency == pytest.approx(0.8)
    assert result.round_trip_efficiency == pytest.approx(0.608)
    assert result.defaulted_components == ("inverter_charge_efficiency",)
    assert result.component_statuses == {
        "battery_efficiency": "calculated",
        "inverter_charge_efficiency": "defaulted",
        "inverter_discharge_efficiency": "calculated",
        "round_trip_efficiency": "calculated_with_defaults",
    }


def test_calculation_requires_a_complete_cycle() -> None:
    result = calculate_battery_efficiency(
        history(soc=(50, 100, 50, 80, 50, 80, 50)),
        calculation_configuration(),
        now=START,
    )

    assert result.status == "insufficient_data"
    assert "complete full-SoC" in result.warnings[0]
    assert result.battery_efficiency == pytest.approx(0.95)
    assert result.round_trip_efficiency == pytest.approx(0.684)
    assert result.defaulted_components == ("battery_efficiency",)
    assert result.component_statuses is not None
    assert result.component_statuses["battery_efficiency"] == "unavailable"
    assert (
        result.component_statuses["round_trip_efficiency"] == "calculated_with_defaults"
    )


def test_calculation_rejects_zero_denominator() -> None:
    result = calculate_battery_efficiency(
        history(
            battery_in=(0, 0, 0, 0, 0, 0),
            battery_out=(0, 0, 0, 0, 0, 0),
        ),
        calculation_configuration(),
        now=START,
    )

    assert result.status == "invalid"
    assert "denominator" in result.warnings[0]


def test_calculation_reports_no_balance_warning_when_capacity_available() -> None:
    result = calculate_battery_efficiency(
        history(), calculation_configuration(), capacity_kwh=10, now=START
    )

    assert result.status == "ok"
    assert not any("inconsistent" in warning for warning in result.warnings)


def test_calculation_skips_balance_check_without_capacity() -> None:
    result = calculate_battery_efficiency(
        history(), calculation_configuration(), now=START
    )

    assert result.status == "ok"
    assert any("not checked" in warning for warning in result.warnings)


def test_calculation_flags_physically_impossible_soc_change() -> None:
    data = BatteryEfficiencyHistoryData(
        schema_version="1",
        start_time=START,
        interval_minutes=60,
        # Hour 0 is physically impossible: only 1 kWh was measured flowing
        # into the battery, but the state of charge implies 5 kWh was stored.
        battery_energy_in_kwh=(1, 0, 6, 0),
        battery_energy_out_kwh=(0, 4, 0, 4),
        inverter_charge_energy_in_kwh=(10, 10, 10, 10),
        inverter_charge_energy_out_kwh=(9, 9, 9, 9),
        inverter_discharge_energy_in_kwh=(10, 10, 10, 10),
        inverter_discharge_energy_out_kwh=(8, 8, 8, 8),
        state_of_charge_percent=(50, 100, 50, 100, 50),
        unit="kWh",
        source=SourceMetadata(provider="home-assistant", entity_id="history"),
        retrieved_at=START,
        latest_observation_at=START,
    )

    result = calculate_battery_efficiency(
        data, calculation_configuration(), capacity_kwh=10, now=START
    )

    assert result.status == "ok"
    assert any("physically inconsistent" in warning for warning in result.warnings)
    assert any("1 of 4" in warning for warning in result.warnings)


def test_calculation_does_not_flag_ordinary_conversion_losses() -> None:
    data = BatteryEfficiencyHistoryData(
        schema_version="1",
        start_time=START,
        interval_minutes=60,
        # Every interval stores or delivers less than measured, i.e. ordinary
        # losses, never more than physically possible.
        battery_energy_in_kwh=(5, 0, 6, 0),
        battery_energy_out_kwh=(0, 4, 0, 4),
        inverter_charge_energy_in_kwh=(10, 10, 10, 10),
        inverter_charge_energy_out_kwh=(9, 9, 9, 9),
        inverter_discharge_energy_in_kwh=(10, 10, 10, 10),
        inverter_discharge_energy_out_kwh=(8, 8, 8, 8),
        state_of_charge_percent=(50, 100, 50, 100, 50),
        unit="kWh",
        source=SourceMetadata(provider="home-assistant", entity_id="history"),
        retrieved_at=START,
        latest_observation_at=START,
    )

    result = calculate_battery_efficiency(
        data, calculation_configuration(), capacity_kwh=10, now=START
    )

    assert result.status == "ok"
    assert not any("inconsistent" in warning for warning in result.warnings)


def test_calculation_uses_all_history_since_configured_start() -> None:
    configuration = calculation_configuration().model_copy(
        update={"history_start": START.replace(hour=2)}
    )

    result = calculate_battery_efficiency(
        history(), configuration, capacity_kwh=10, now=START
    )

    assert result.status == "ok"
    assert result.history_start == START.replace(hour=2)
    assert result.battery_efficiency == pytest.approx(0.8)


@pytest.mark.parametrize(
    "soc",
    [
        pytest.param((50, 100, 50, 100, 50, 100, 50), id="soc-intact"),
        # The importer drops both state-of-charge boundaries of an excluded hour.
        pytest.param((50, None, None, 100, 50, 100, 50), id="soc-next-to-hour"),
    ],
)
def test_calculation_skips_a_full_soc_cycle_that_contains_an_excluded_hour(
    soc: tuple[float | None, ...],
) -> None:
    """Hour 2 lies in the first full-SoC cycle, hours 1 and 2, so it is not used.

    Only the cycle over hours 3 and 4 remains: 5 kWh out of 10 kWh in. Using the
    first cycle as well would give (4 + 5) / (5 + 10) = 0.6 instead.
    """
    battery_in = (0, 5, 0, 10, 0, 5)
    battery_out = (0, 4, 0, 5, 0, 4)

    result = calculate_battery_efficiency(
        history(battery_in=battery_in, battery_out=battery_out, soc=soc, excluded=(2,)),
        calculation_configuration(),
        capacity_kwh=10,
        now=START,
    )
    all_valid = calculate_battery_efficiency(
        history(battery_in=battery_in, battery_out=battery_out),
        calculation_configuration(),
        capacity_kwh=10,
        now=START,
    )

    assert all_valid.complete_cycle_count == 2
    assert all_valid.battery_efficiency == pytest.approx(0.6)
    assert result.complete_cycle_count == 1
    assert result.battery_throughput_kwh == 10
    assert result.battery_efficiency == pytest.approx(0.5)
    # The inverter legs sum the five hours that have values.
    assert result.charge_throughput_kwh == 50
    assert result.discharge_throughput_kwh == 50
    assert result.inverter_charge_efficiency == pytest.approx(0.9)
    assert result.inverter_discharge_efficiency == pytest.approx(0.8)
    assert result.round_trip_efficiency == pytest.approx(0.5 * 0.9 * 0.8)
    assert result.status == "ok"


@pytest.mark.parametrize("excluded_hour", [0, 5])
def test_calculation_keeps_full_soc_cycles_that_avoid_the_excluded_hour(
    excluded_hour: int,
) -> None:
    result = calculate_battery_efficiency(
        history(excluded=(excluded_hour,)),
        calculation_configuration(),
        capacity_kwh=10,
        now=START,
    )

    assert result.complete_cycle_count == 2
    assert result.battery_throughput_kwh == 10
    assert result.battery_efficiency == pytest.approx(0.8)
    assert result.charge_throughput_kwh == 50
    assert result.status == "ok"


def test_calculation_falls_back_to_the_default_when_every_cycle_is_excluded() -> None:
    """Hour 1 lies in the first cycle (hours 1, 2), hour 3 in the second (3, 4)."""
    one_cycle = calculate_battery_efficiency(
        history(excluded=(1,)), calculation_configuration(), capacity_kwh=10, now=START
    )
    no_cycle = calculate_battery_efficiency(
        history(excluded=(1, 3)),
        calculation_configuration(),
        capacity_kwh=10,
        now=START,
    )

    assert one_cycle.complete_cycle_count == 1
    assert one_cycle.battery_efficiency == pytest.approx(0.8)
    assert no_cycle.complete_cycle_count == 0
    assert no_cycle.battery_throughput_kwh == 0
    assert no_cycle.battery_efficiency == pytest.approx(0.95)
    assert no_cycle.component_statuses is not None
    assert no_cycle.component_statuses["battery_efficiency"] == "unavailable"
    assert no_cycle.status == "insufficient_data"
    assert any("complete full-SoC" in warning for warning in no_cycle.warnings)


@pytest.mark.parametrize(
    ("excluded", "soc"),
    [
        pytest.param((0,), (None, 100, 50, 100, 50), id="excluded-hour"),
        pytest.param((), (None, 100, 50, 100, 50), id="soc-boundary-without-value"),
    ],
)
def test_calculation_ignores_excluded_hours_in_the_soc_balance_check(
    excluded: tuple[int, ...], soc: tuple[float | None, ...]
) -> None:
    """Hour 0 stores 5 kWh while only 1 kWh was measured, but it has no data."""
    result = calculate_battery_efficiency(
        history(
            battery_in=(1, 0, 6, 0),
            battery_out=(0, 4, 0, 4),
            soc=soc,
            excluded=excluded,
        ),
        calculation_configuration(),
        capacity_kwh=10,
        now=START,
    )

    assert result.status == "ok"
    assert not any("inconsistent" in warning for warning in result.warnings)


def importer_configuration(state_class: str = "total_increasing") -> Any:
    def leg(name: str) -> dict[str, list[dict[str, object]]]:
        return {
            "energy_in": [entity(f"{name}_in", state_class=state_class)],
            "energy_out": [entity(f"{name}_out", state_class=state_class)],
        }

    factory = home_assistant_configuration_factory(
        battery={
            "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
            "capacity": {"value": 10, "unit": "kWh"},
            "minimum_soc": {"value": 1, "unit": "kWh"},
            "maximum_soc": {"value": 10, "unit": "kWh"},
            "maximum_charge": {"value": 4, "unit": "kW"},
            "maximum_discharge": {"value": 4, "unit": "kW"},
            "efficiency_calculation": {
                "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                "battery": leg("battery"),
                "inverter_charge": leg("charge"),
                "inverter_discharge": leg("discharge"),
            },
        }
    )
    return factory()


def test_importer_fetches_and_aligns_home_assistant_history() -> None:
    configuration = importer_configuration()
    start = START
    end = START + timedelta(hours=4)

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        if entity_id == "sensor.soc":
            readings = [
                (f"2026-01-01T0{hour}:00:00+00:00", str(value))
                for hour, value in enumerate((50, 60, 70, 80, 90))
            ]
            return httpx.Response(
                200,
                json=home_assistant_history_payload(
                    entity_id, readings, unit="%", state_class="measurement"
                ),
            )
        return httpx.Response(
            200,
            json=home_assistant_history_payload(entity_id),
        )

    importer_client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        data = import_and_build(importer, importer_client, start, end, now=end)
    finally:
        importer_client.close()

    assert data.start_time == start
    assert len(data.battery_energy_in_kwh) == 4
    assert len(data.state_of_charge_percent) == 5


SIX_LEGS = (
    "battery_energy_in_kwh",
    "battery_energy_out_kwh",
    "inverter_charge_energy_in_kwh",
    "inverter_charge_energy_out_kwh",
    "inverter_discharge_energy_in_kwh",
    "inverter_discharge_energy_out_kwh",
)


def soc_readings() -> list[tuple[str, str]]:
    return [
        (f"2026-01-01T0{hour}:00:00+00:00", str(value))
        for hour, value in enumerate((50, 60, 70, 80, 90))
    ]


@pytest.mark.parametrize(
    ("state_class", "counter", "previous", "value"),
    [
        ("total_increasing", ("0", "1", "3", "2", "10"), 3.0, 2.0),
        ("total", ("10", "11", "12", "9", "10"), 12.0, 9.0),
    ],
)
def test_importer_excludes_the_hours_of_a_counter_decrease_and_keeps_the_rest(
    state_class: str, counter: tuple[str, ...], previous: float, value: float
) -> None:
    """A decrease never fails the fetch, whatever its size or state class.

    The battery input counter decreases between 02:00 and 03:00. That excludes
    the hours of both observations (hours 1 and 2), and the step directly after
    the decrease (03:00 to 04:00) excludes hours 2 and 3. Only hour 0 is valid.
    The exclusion applies to all six legs and to the state of charge around it.
    """
    configuration = importer_configuration(state_class)
    end = START + timedelta(hours=4)

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        if entity_id == "sensor.soc":
            return httpx.Response(
                200,
                json=home_assistant_history_payload(
                    entity_id, soc_readings(), unit="%", state_class="measurement"
                ),
            )
        if entity_id == "sensor.battery_in":
            readings = [
                (f"2026-01-01T0{hour}:00:00+00:00", state)
                for hour, state in enumerate(counter)
            ]
            return httpx.Response(
                200,
                json=home_assistant_history_payload(
                    entity_id, readings, state_class=state_class
                ),
            )
        return httpx.Response(
            200,
            json=home_assistant_history_payload(entity_id, state_class=state_class),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        data = import_and_build(importer, client, START, end, now=end)
    finally:
        client.close()

    for name in SIX_LEGS:
        assert getattr(data, name) == (1.0, None, None, None), name
    # An excluded hour drops both state-of-charge values that bracket it.
    assert data.state_of_charge_percent == (50.0, None, None, None, None)
    assert [item.hour_start for item in data.exclusions] == [
        START + timedelta(hours=hour) for hour in (1, 2, 3)
    ]
    assert [[cause.reason for cause in item.causes] for item in data.exclusions] == [
        ["counter_decrease"],
        ["counter_decrease", "step_after_decrease"],
        ["step_after_decrease"],
    ]
    decrease = data.exclusions[0].causes[0]
    assert decrease.entity_id == "sensor.battery_in"
    assert decrease.data_point_count == 1
    (point,) = decrease.data_points
    assert point.state == str(int(value))
    assert point.timestamp == START + timedelta(hours=3)
    assert point.previous_timestamp == START + timedelta(hours=2)
    assert (point.previous_value, point.value) == (previous, value)
    assert point.step_kwh == pytest.approx(value - previous)
    assert all(
        cause.entity_id == "sensor.battery_in"
        for item in data.exclusions
        for cause in item.causes
    )

    result = calculate_battery_efficiency(
        data, calculation_configuration(), capacity_kwh=10, now=end
    )
    assert result.complete_cycle_count == 0
    assert result.charge_throughput_kwh == 1.0
    assert result.status == "insufficient_data"
    assert result.component_statuses is not None
    assert result.component_statuses["battery_efficiency"] == "unavailable"


def test_importer_excludes_a_one_watt_hour_dip_in_every_leg() -> None:
    """A 1 Wh decrease is excluded like any other; nothing is tolerated.

    All six legs use ``total`` counters without ``last_reset``. The input side
    of each leg dips in hour 0 and the output side in hour 1. Both hours are
    excluded in every leg and the untouched hours 2 and 3 hold exactly 1 kWh.
    """
    configuration = importer_configuration("total")
    end = START + timedelta(hours=4)

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        if entity_id == "sensor.soc":
            return httpx.Response(
                200,
                json=home_assistant_history_payload(
                    entity_id, soc_readings(), unit="%", state_class="measurement"
                ),
            )
        dip_hour = 0 if entity_id.endswith("_in") else 1
        return httpx.Response(
            200,
            json=home_assistant_history_payload(
                entity_id,
                home_assistant_jittery_total_readings(3200.0, dip_hour),
                state_class="total",
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        data = import_and_build(importer, client, START, end, now=end)
    finally:
        client.close()

    for name in SIX_LEGS:
        assert getattr(data, name) == pytest.approx((None, None, 1.0, 1.0)), name
    assert data.state_of_charge_percent == (None, None, None, 80.0, 90.0)
    reasons_by_hour = {
        item.hour_start: {(cause.reason, cause.entity_id) for cause in item.causes}
        for item in data.exclusions
    }
    assert reasons_by_hour == {
        START: {
            (reason, f"sensor.{leg}_in")
            for reason in ("counter_decrease", "step_after_decrease")
            for leg in ("battery", "charge", "discharge")
        },
        START + timedelta(hours=1): {
            (reason, f"sensor.{leg}_out")
            for reason in ("counter_decrease", "step_after_decrease")
            for leg in ("battery", "charge", "discharge")
        },
    }
    decrease = next(
        cause
        for cause in data.exclusions[0].causes
        if cause.reason == "counter_decrease"
    )
    assert decrease.data_points[0].step_kwh == pytest.approx(-0.001)
    assert decrease.data_points[0].previous_value == pytest.approx(3200.5)
    assert decrease.data_points[0].value == pytest.approx(3200.499)
    assert all(
        cause.data_point_count == len(cause.data_points)
        for item in data.exclusions
        for cause in item.causes
    )


def test_importer_reports_home_assistant_history_failure() -> None:
    configuration = importer_configuration()
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        import_and_build(importer, client, START, START + timedelta(hours=4))
    client.close()


def efficiency_home_assistant(
    soc_states: list[tuple[datetime, str]],
) -> FakeHomeAssistant:
    """Serve six hourly counters per leg and the given state-of-charge changes."""
    hours = range(7)
    counters = {
        f"sensor.{leg}_{side}": [
            (START + timedelta(hours=hour), str(hour)) for hour in hours
        ]
        for leg in ("battery", "charge", "discharge")
        for side in ("in", "out")
    }
    return FakeHomeAssistant(
        {**counters, "sensor.soc": soc_states},
        units={"sensor.soc": "%"},
        state_classes={"sensor.soc": "measurement"},
    )


def build_efficiency_history(
    home_assistant: FakeHomeAssistant, end_hours: int = 4
) -> BatteryEfficiencyHistoryData:
    client = home_assistant.client()
    try:
        return import_and_build(
            HomeAssistantBatteryEfficiencyImporter(importer_configuration()),
            client,
            START,
            START + timedelta(hours=end_hours),
            now=START + timedelta(hours=end_hours),
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    ("changes", "excluded_hours", "reason", "state", "expected_soc"),
    [
        pytest.param(
            [(0, "50"), (60, "not-a-number")],
            (0, 1, 2, 3),
            "non_numeric",
            "not-a-number",
            (None, None, None, None, None),
            id="non-numeric-never-recovers",
        ),
        pytest.param(
            [(0, "50"), (90, "unavailable"), (150, "60")],
            (1, 2),
            "unavailable",
            "unavailable",
            (50.0, None, None, None, 60.0),
            id="unavailable-until-a-valid-sample-returns",
        ),
        pytest.param(
            [(0, "50"), (60, "150")],
            (0, 1, 2, 3),
            "soc_out_of_range",
            "150",
            (None, None, None, None, None),
            id="above-100-percent",
        ),
        pytest.param(
            [(0, "50"), (60, "-5")],
            (0, 1, 2, 3),
            "soc_out_of_range",
            "-5",
            (None, None, None, None, None),
            id="below-0-percent",
        ),
        pytest.param(
            [(0, "50"), (60, "nan")],
            (0, 1, 2, 3),
            "not_finite",
            "nan",
            (None, None, None, None, None),
            id="not-finite",
        ),
    ],
)
def test_importer_excludes_the_hours_of_an_invalid_state_of_charge_sample(
    changes: list[tuple[int, str]],
    excluded_hours: tuple[int, ...],
    reason: str,
    state: str,
    expected_soc: tuple[float | None, ...],
) -> None:
    """A bad state of charge never fails the fetch and is never carried forward.

    The invalid state stays in force until the next valid sample, so every hour
    in which it is in force is excluded in all legs, including the hour in which
    a valid sample returns. The valid state before it is not carried across it.
    """
    home_assistant = efficiency_home_assistant(
        [(START + timedelta(minutes=minutes), value) for minutes, value in changes]
    )

    data = build_efficiency_history(home_assistant)

    expected_energy = tuple(
        None if hour in excluded_hours else 1.0 for hour in range(4)
    )
    for name in SIX_LEGS:
        assert getattr(data, name) == expected_energy, name
    assert data.state_of_charge_percent == expected_soc
    assert [item.hour_start for item in data.exclusions] == [
        START + timedelta(hours=hour) for hour in excluded_hours
    ]
    for item in data.exclusions:
        (cause,) = item.causes
        assert cause.reason == reason
        assert cause.entity_id == "sensor.soc"
        assert cause.data_point_count == 1
        assert cause.data_points[0].state == state


def test_importer_rejects_state_of_charge_history_that_starts_too_late() -> None:
    # The first observation lies in the hour after the last requested boundary, so
    # no complete hour of state of charge exists.
    home_assistant = efficiency_home_assistant(
        [(START + timedelta(hours=4, minutes=30), "50")]
    )

    with pytest.raises(HomeAssistantError, match="no complete state-of-charge history"):
        build_efficiency_history(home_assistant)


def test_importer_carries_state_of_charge_forward_through_unchanged_hours() -> None:
    # Home Assistant only reports changes. Each hour takes the last value seen
    # before its closing boundary, so the 03:30 change is visible from hour 3.
    home_assistant = efficiency_home_assistant(
        [(START, "50"), (START + timedelta(hours=3, minutes=30), "70")]
    )

    data = build_efficiency_history(home_assistant)

    assert data.state_of_charge_percent == (50.0, 50.0, 50.0, 70.0, 70.0)


@pytest.mark.parametrize(
    ("start_time", "end_time", "message"),
    [
        (START + timedelta(minutes=30), START + timedelta(hours=4), "start on an hour"),
        (START, START + timedelta(hours=4, minutes=30), "end on an hour"),
        (START + timedelta(hours=4), START, "end on an hour"),
        (datetime(2026, 1, 1), START + timedelta(hours=4), "must include a timezone"),
    ],
)
def test_importer_validates_the_requested_period_when_planning(
    start_time: datetime, end_time: datetime, message: str
) -> None:
    importer = HomeAssistantBatteryEfficiencyImporter(importer_configuration())

    with pytest.raises(HomeAssistantError, match=message):
        importer.plan(start_time, end_time)


def test_importer_requires_a_calculation_configuration() -> None:
    configuration = home_assistant_configuration_factory()()

    with pytest.raises(HomeAssistantError, match="is not configured"):
        HomeAssistantBatteryEfficiencyImporter(configuration)


def test_importer_skips_empty_soc_history_chunks_until_history_is_available() -> None:
    configuration = importer_configuration()
    start = START
    end = START + timedelta(days=8)
    soc_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal soc_requests
        entity_id = request.url.params["filter_entity_id"]
        if entity_id != "sensor.soc":
            return httpx.Response(
                200,
                json=home_assistant_history_payload(entity_id),
            )
        soc_requests += 1
        if soc_requests == 1:
            # Home Assistant returns no series for the period before the
            # entity's retained history begins.
            return httpx.Response(200, json=[])
        readings = [
            (
                (START + timedelta(days=7, hours=hour)).isoformat(),
                str(50 + hour),
            )
            for hour in range(26)
        ]
        return httpx.Response(
            200,
            json=home_assistant_history_payload(
                entity_id,
                readings,
                unit="%",
                state_class="measurement",
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        data = import_and_build(importer, client, start, end, now=end)
    finally:
        client.close()

    assert soc_requests == 2
    assert data.start_time == START + timedelta(days=7)
    assert len(data.state_of_charge_percent) == 25
    assert data.state_of_charge_percent[0] == 50.0
    assert data.state_of_charge_percent[-1] == 74.0


@pytest.mark.parametrize("empty_payload", [[], [[]]])
def test_importer_rejects_soc_history_with_no_usable_records(
    empty_payload: list[object],
) -> None:
    configuration = importer_configuration()

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        if entity_id == "sensor.soc":
            return httpx.Response(200, json=empty_payload)
        return httpx.Response(
            200,
            json=home_assistant_history_payload(entity_id),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        with pytest.raises(
            HomeAssistantError, match="no usable history for sensor.soc"
        ):
            import_and_build(importer, client, START, START + timedelta(hours=1))
    finally:
        client.close()


def test_importer_carries_forward_soc_across_an_unchanged_state_gap() -> None:
    """Home Assistant only logs a row when a state changes.

    An hour with no new SOC row does not mean the value is missing; it means
    the value has not changed since the previous observation. The importer
    must carry that value forward instead of failing.
    """
    configuration = importer_configuration()
    start = START
    end = START + timedelta(hours=4)

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        if entity_id != "sensor.soc":
            return httpx.Response(200, json=home_assistant_history_payload(entity_id))
        # No row is recorded for hours 1 and 2: the SOC value did not change
        # between the hour-0 and hour-3 observations.
        readings = [
            ("2026-01-01T00:00:00+00:00", "50"),
            ("2026-01-01T03:00:00+00:00", "50"),
            ("2026-01-01T04:00:00+00:00", "60"),
        ]
        return httpx.Response(
            200,
            json=home_assistant_history_payload(
                entity_id, readings, unit="%", state_class="measurement"
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        data = import_and_build(importer, client, start, end, now=end)
    finally:
        client.close()

    assert data.state_of_charge_percent == (50.0, 50.0, 50.0, 50.0, 60.0)


def test_importer_logs_soc_history_requests_at_debug_level(
    caplog: LogCaptureFixture,
) -> None:
    configuration = importer_configuration()
    start = START
    end = START + timedelta(hours=1)

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        return httpx.Response(200, json=home_assistant_history_payload(entity_id))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        with caplog.at_level(logging.DEBUG, logger="energy_optimizer.providers"):
            import_and_build(importer, client, start, end, now=end)
    finally:
        client.close()

    history_requests = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_request")
    ]
    assert history_requests
    assert all(record.levelno == logging.DEBUG for record in history_requests)


@pytest.mark.parametrize(
    ("pv_yield", "expected_charge_out", "expected_soc"),
    [
        (0.5, 0.738 - 0.5, (50.0, 55.0)),
        (1.54, None, (None, None)),
    ],
)
def test_importer_excludes_a_negative_combined_hour_instead_of_clamping_it(
    pv_yield: float,
    expected_charge_out: float | None,
    expected_soc: tuple[float | None, ...],
) -> None:
    """PV yield exceeding battery charging makes the combined hour negative.

    The ``inverter_charge`` leg subtracts the PV yield from the energy the
    battery was charged with. A negative net value is not repaired: the hour is
    excluded in all legs with reason ``combined_negative``, the fetch does not
    fail, and a non-negative net value is imported as it is.
    """

    def leg(name: str) -> dict[str, list[dict[str, object]]]:
        return {
            "energy_in": [entity(f"{name}_in")],
            "energy_out": [entity(f"{name}_out")],
        }

    configuration = home_assistant_configuration_factory(
        battery={
            "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
            "capacity": {"value": 10, "unit": "kWh"},
            "minimum_soc": {"value": 1, "unit": "kWh"},
            "maximum_soc": {"value": 10, "unit": "kWh"},
            "maximum_charge": {"value": 4, "unit": "kW"},
            "maximum_discharge": {"value": 4, "unit": "kW"},
            "efficiency_calculation": {
                "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                "battery": leg("battery"),
                "inverter_charge": {
                    "energy_in": [entity("charge_in")],
                    "energy_out": [
                        entity("charging_battery_energy"),
                        entity("pv_yield", "subtract"),
                    ],
                },
                "inverter_discharge": leg("discharge"),
            },
        }
    )()
    start = START
    end = START + timedelta(hours=1)

    def reading(value: float) -> list[tuple[str, str]]:
        return [
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", str(value)),
        ]

    responses = {
        "sensor.soc": home_assistant_history_payload(
            "sensor.soc",
            [("2026-01-01T00:00:00+00:00", "50"), ("2026-01-01T01:00:00+00:00", "55")],
            unit="%",
            state_class="measurement",
        ),
        "sensor.battery_in": home_assistant_history_payload(
            "sensor.battery_in", reading(0.5)
        ),
        "sensor.battery_out": home_assistant_history_payload(
            "sensor.battery_out", reading(0.0)
        ),
        "sensor.charge_in": home_assistant_history_payload(
            "sensor.charge_in", reading(0.2)
        ),
        # Battery charged 0.738 kWh from all sources this hour.
        "sensor.charging_battery_energy": home_assistant_history_payload(
            "sensor.charging_battery_energy", reading(0.738)
        ),
        # A PV yield above the battery charge means the surplus was exported
        # rather than stored.
        "sensor.pv_yield": home_assistant_history_payload(
            "sensor.pv_yield", reading(pv_yield)
        ),
        "sensor.discharge_in": home_assistant_history_payload(
            "sensor.discharge_in", reading(0.0)
        ),
        "sensor.discharge_out": home_assistant_history_payload(
            "sensor.discharge_out", reading(0.0)
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        entity_id = request.url.params["filter_entity_id"]
        return httpx.Response(200, json=responses[entity_id])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        data = import_and_build(importer, client, start, end, now=end)
    finally:
        client.close()

    assert data.state_of_charge_percent == expected_soc
    if expected_charge_out is not None:
        assert data.inverter_charge_energy_out_kwh == pytest.approx(
            (expected_charge_out,)
        )
        assert data.inverter_charge_energy_in_kwh == (0.2,)
        assert data.exclusions == ()
        return
    for name in SIX_LEGS:
        assert getattr(data, name) == (None,), name
    (item,) = data.exclusions
    assert item.hour_start == START
    (cause,) = item.causes
    assert cause.reason == "combined_negative"
    assert cause.entity_id is None
    assert "-0.802" in cause.message
    assert cause.data_point_count == 2
    assert [point.entity_id for point in cause.data_points] == [
        "sensor.charging_battery_energy",
        "sensor.pv_yield",
    ]
    assert [point.step_kwh for point in cause.data_points] == pytest.approx(
        [0.738, -1.54]
    )


@pytest.mark.parametrize(
    ("battery_in", "battery_out", "pv_yield", "expected_discharge_in"),
    [
        # The battery discharges and PV reaches the inverter on top of it.
        (0.0, 0.5, 0.3, 0.8),
        # The battery keeps part of the PV yield, which never reaches the inverter.
        (0.2, 0.5, 0.3, 0.6),
        # With an idle battery only the PV yield reaches the inverter.
        (0.0, 0.0, 0.4, 0.4),
    ],
)
def test_importer_adds_the_pv_yield_to_the_inverter_discharge_input(
    battery_in: float,
    battery_out: float,
    pv_yield: float,
    expected_discharge_in: float,
) -> None:
    """The inverter draws battery discharge and PV yield from the DC bus.

    The ``inverter_discharge`` input of the example configuration is the battery
    discharge plus the PV yield minus the energy the battery took in during the
    same hour. Subtracting the PV yield instead would shrink the input whenever
    PV produces, and would make an hour of PV alone negative.
    """

    def leg(name: str) -> dict[str, list[dict[str, object]]]:
        return {
            "energy_in": [entity(f"{name}_in")],
            "energy_out": [entity(f"{name}_out")],
        }

    configuration = home_assistant_configuration_factory(
        battery={
            "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
            "capacity": {"value": 10, "unit": "kWh"},
            "minimum_soc": {"value": 1, "unit": "kWh"},
            "maximum_soc": {"value": 10, "unit": "kWh"},
            "maximum_charge": {"value": 4, "unit": "kW"},
            "maximum_discharge": {"value": 4, "unit": "kW"},
            "efficiency_calculation": {
                "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                "battery": leg("battery"),
                "inverter_charge": leg("charge"),
                "inverter_discharge": {
                    "energy_in": [
                        entity("battery_out"),
                        entity("battery_in", "subtract"),
                        entity("pv_yield"),
                    ],
                    "energy_out": [entity("discharge_out")],
                },
            },
        }
    )()
    end = START + timedelta(hours=1)

    def reading(value: float) -> list[tuple[str, str]]:
        return [
            ("2026-01-01T00:00:00+00:00", "0"),
            ("2026-01-01T01:00:00+00:00", str(value)),
        ]

    counters = {
        "sensor.battery_in": battery_in,
        "sensor.battery_out": battery_out,
        "sensor.charge_in": 0.2,
        "sensor.charge_out": 0.1,
        "sensor.pv_yield": pv_yield,
        "sensor.discharge_out": 0.1,
    }
    responses = {
        "sensor.soc": home_assistant_history_payload(
            "sensor.soc",
            [("2026-01-01T00:00:00+00:00", "50"), ("2026-01-01T01:00:00+00:00", "55")],
            unit="%",
            state_class="measurement",
        ),
        **{
            entity_id: home_assistant_history_payload(entity_id, reading(value))
            for entity_id, value in counters.items()
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=responses[request.url.params["filter_entity_id"]]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration)
    try:
        data = import_and_build(importer, client, START, end, now=end)
    finally:
        client.close()

    assert data.exclusions == ()
    assert data.inverter_discharge_energy_in_kwh == pytest.approx(
        (expected_discharge_in,)
    )
    assert data.inverter_discharge_energy_out_kwh == pytest.approx((0.1,))


def test_merge_extends_persisted_history_with_new_hours() -> None:
    existing = history()
    incoming = history(
        battery_in=(5,),
        battery_out=(4,),
        soc=(50, 100),
    )
    incoming = incoming.__class__(
        **{
            **incoming.__dict__,
            "start_time": existing.start_time
            + timedelta(hours=len(existing.battery_energy_in_kwh)),
        }
    )

    merged = merge_battery_efficiency_history(existing, incoming)

    assert merged.start_time == existing.start_time
    assert len(merged.battery_energy_in_kwh) == len(
        existing.battery_energy_in_kwh
    ) + len(incoming.battery_energy_in_kwh)
    assert len(merged.state_of_charge_percent) == len(merged.battery_energy_in_kwh) + 1
    assert merged.battery_energy_in_kwh[-1] == 5
    assert merged.state_of_charge_percent[-1] == 100


def test_merge_rejects_a_non_contiguous_incoming_history() -> None:
    existing = history()
    incoming = history()  # starts at the same time instead of right after

    with pytest.raises(HomeAssistantError, match="not contiguous"):
        merge_battery_efficiency_history(existing, incoming)


def test_merge_returns_the_incoming_history_when_nothing_is_persisted() -> None:
    incoming = history()

    merged = merge_battery_efficiency_history(None, incoming)

    assert merged == incoming


def test_merge_keeps_the_exclusions_and_missing_values_of_both_histories() -> None:
    existing = history(excluded=(1,), soc=(50, None, None, 100, 50, 100, 50))
    # Hour 7 is the second hour of the incoming history, so its boundary values 0
    # and 1 are unavailable. Value 0 is the one the existing history ended on.
    incoming = replace(
        history(
            battery_in=(5, 5),
            battery_out=(4, 4),
            soc=(None, None, 50),
            excluded=(1,),
        ),
        start_time=START + timedelta(hours=6),
        exclusions=(exclusion(7, "unavailable", "sensor.soc"),),
    )

    merged = merge_battery_efficiency_history(existing, incoming)

    assert merged.start_time == START
    assert merged.battery_energy_in_kwh == (0, None, 0, 5, 0, 5, 5, None)
    assert merged.inverter_charge_energy_out_kwh == (9, None, 9, 9, 9, 9, 9, None)
    assert merged.state_of_charge_percent == (
        50,
        None,
        None,
        100,
        50,
        100,
        None,
        None,
        50,
    )
    assert merged.exclusions == (
        exclusion(1),
        exclusion(7, "unavailable", "sensor.soc"),
    )


def test_bounding_history_drops_the_exclusions_of_trimmed_hours() -> None:
    data = history(excluded=(0, 4))

    bounded = bound_battery_efficiency_history(data, max_hours=4)

    assert bounded.start_time == START + timedelta(hours=2)
    assert bounded.battery_energy_in_kwh == data.battery_energy_in_kwh[2:]
    assert bounded.battery_energy_in_kwh == (0, 5, None, 5)
    assert bounded.state_of_charge_percent == data.state_of_charge_percent[2:]
    assert bounded.exclusions == (exclusion(4),)
