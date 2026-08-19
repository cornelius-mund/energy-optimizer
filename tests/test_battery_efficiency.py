"""Tests for measured battery and inverter efficiency calculation."""

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

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


def test_calculation_requires_a_complete_cycle() -> None:
    result = calculate_battery_efficiency(
        history(soc=(50, 100, 50, 80, 50, 80, 50)),
        calculation_configuration(),
        now=START,
    )

    assert result.status == "insufficient_data"
    assert "complete full-SoC" in result.warnings[0]


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


def test_importer_reports_home_assistant_history_failure() -> None:
    configuration = importer_configuration()
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    importer = HomeAssistantBatteryEfficiencyImporter(configuration, client)

    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        importer.fetch(START, START + timedelta(hours=4))
    client.close()


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
