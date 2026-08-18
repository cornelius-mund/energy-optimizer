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


def test_calculation_surfaces_state_of_charge_balance_warning() -> None:
    result = calculate_battery_efficiency(
        history(),
        calculation_configuration().model_copy(
            update={"soc_balance_tolerance_kwh": 0.5}
        ),
        capacity_kwh=20,
        now=START,
    )

    assert result.status == "ok"
    assert any("balance deviates" in warning for warning in result.warnings)


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
