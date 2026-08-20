"""Tests for measured battery and inverter efficiency calculation."""

import logging
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
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    HomeAssistantBatteryEfficiencyImporter,
    calculate_battery_efficiency,
    merge_battery_efficiency_history,
)
from energy_optimizer.providers.home_assistant_energy import HomeAssistantError
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyHistoryData,
    SourceMetadata,
)
from home_assistant_fixtures import (
    home_assistant_configuration_factory,
    home_assistant_history_payload,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def entity(name: str, operation: str = "add") -> dict[str, object]:
    return {
        "entity_id": f"sensor.{name}",
        "state_class": "total_increasing",
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


def history(
    *,
    battery_in: tuple[float, ...] = (0, 5, 0, 5, 0, 5),
    battery_out: tuple[float, ...] = (0, 4, 0, 4, 0, 4),
    soc: tuple[float, ...] = (50, 100, 50, 100, 50, 100, 50),
) -> BatteryEfficiencyHistoryData:
    return BatteryEfficiencyHistoryData(
        schema_version="1",
        start_time=START,
        interval_minutes=60,
        battery_energy_in_kwh=battery_in,
        battery_energy_out_kwh=battery_out,
        inverter_charge_energy_in_kwh=(10, 10, 10, 10, 10, 10),
        inverter_charge_energy_out_kwh=(9, 9, 9, 9, 9, 9),
        inverter_discharge_energy_in_kwh=(10, 10, 10, 10, 10, 10),
        inverter_discharge_energy_out_kwh=(8, 8, 8, 8, 8, 8),
        state_of_charge_percent=soc,
        unit="kWh",
        source=SourceMetadata(provider="home-assistant", entity_id="history"),
        retrieved_at=START,
        latest_observation_at=START,
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


def importer_configuration() -> Any:
    def leg(name: str) -> dict[str, list[dict[str, object]]]:
        return {
            "energy_in": [entity(f"{name}_in")],
            "energy_out": [entity(f"{name}_out")],
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
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, importer_client)
    try:
        data = importer.fetch(start, end, now=end)
    finally:
        importer_client.close()

    assert data.start_time == start
    assert len(data.battery_energy_in_kwh) == 4
    assert len(data.state_of_charge_percent) == 5


def test_importer_persists_history_despite_a_suspect_interval() -> None:
    """A reset/spike anomaly on one leg must not fail the entire fetch.

    Regression test for issue #157: the aligned history used to reject the
    whole requested window whenever any hour anywhere in it carried a suspect
    quality flag, which meant the source could never persist and always
    retried the same full history range.
    """
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
        if entity_id == "sensor.battery_in":
            # The counter decreases at hour 3 without a matching last_reset
            # change, which HomeAssistantEnergyAggregator correctly flags as
            # a suspect counter_reset for that hour.
            readings = [
                ("2026-01-01T00:00:00+00:00", "0"),
                ("2026-01-01T01:00:00+00:00", "1"),
                ("2026-01-01T02:00:00+00:00", "3"),
                ("2026-01-01T03:00:00+00:00", "2"),
                ("2026-01-01T04:00:00+00:00", "10"),
            ]
            return httpx.Response(
                200,
                json=home_assistant_history_payload(entity_id, readings),
            )
        return httpx.Response(
            200,
            json=home_assistant_history_payload(entity_id),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)
    try:
        data = importer.fetch(start, end, now=end)
    finally:
        client.close()

    assert len(data.battery_energy_in_kwh) == 4
    assert len(data.quality) == 4
    assert data.quality[2].status == "suspect"
    assert [item.status for index, item in enumerate(data.quality) if index != 2] == [
        "valid",
        "valid",
        "valid",
    ]

    result = calculate_battery_efficiency(
        data, calculation_configuration(), capacity_kwh=10, now=end
    )
    assert any(item.status == "suspect" for item in result.quality)


def test_importer_reports_home_assistant_history_failure() -> None:
    configuration = importer_configuration()
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        importer.fetch(START, START + timedelta(hours=4))
    client.close()


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
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)
    try:
        data = importer.fetch(start, end, now=end)
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
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)
    try:
        with pytest.raises(HomeAssistantError, match="no usable state-of-charge"):
            importer.fetch(START, START + timedelta(hours=1))
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
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)
    try:
        data = importer.fetch(start, end, now=end)
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
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)
    try:
        with caplog.at_level(logging.DEBUG, logger="energy_optimizer.providers"):
            importer.fetch(start, end, now=end)
    finally:
        client.close()

    history_requests = [
        record
        for record in caplog.records
        if record.getMessage().startswith("event=home_assistant_history_request")
    ]
    assert history_requests
    assert all(record.levelno == logging.DEBUG for record in history_requests)


def test_importer_clamps_pv_surplus_instead_of_failing_the_refresh() -> None:
    """PV yield exceeding battery charging in one hour is ordinary export.

    The remainder was exported rather than stored, so the ``inverter_charge``
    leg's net directional expression is negative for that hour. This must not
    fail the fetch; the leg's value for that hour is clamped to zero instead.
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
        # Combined PV yield of 1.54 kWh exceeded the battery charge; the
        # surplus was exported rather than stored.
        "sensor.pv_yield": home_assistant_history_payload(
            "sensor.pv_yield", reading(1.54)
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
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)
    try:
        data = importer.fetch(start, end, now=end)
    finally:
        client.close()

    assert data.inverter_charge_energy_out_kwh == (0.0,)
    assert data.inverter_charge_energy_in_kwh == (0.2,)


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
