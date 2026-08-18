"""Tests for runtime configuration loading."""

from pathlib import Path

import pytest

from energy_optimizer.config import (
    BatteryConstantConfiguration,
    ConfigurationError,
    HomeAssistantBatteryEntityConfiguration,
    HomeAssistantConfiguration,
    load_configuration,
)

CONFIG_MARKER = """  household_load_entities:
    - entity_id: sensor.household_load
      state_class: total_increasing
      unit: kWh
      operation: add
"""

VALID_CONFIGURATION = f"""
time_resolution_minutes: 60
grid:
  maximum_import_kw: 10
  maximum_export_kw: 5
solver:
  name: highs
  time_limit_seconds: 30
home_assistant:
  base_url: http://homeassistant.local:8123
  token: test-token
{CONFIG_MARKER}
  timeout_seconds: 10
  max_data_age_seconds: 7200
"""


def test_load_configuration_returns_typed_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIGURATION, encoding="utf-8")

    configuration = load_configuration(path)

    assert configuration.time_resolution_minutes == 60
    assert configuration.grid.maximum_export_kw == 5
    assert configuration.solver.name == "highs"
    assert configuration.home_assistant is not None
    assert configuration.home_assistant.base_url.host == "homeassistant.local"
    assert configuration.home_assistant.token.get_secret_value() == "test-token"
    assert configuration.home_assistant.max_data_age_seconds == 7200


def test_load_configuration_returns_explicit_energy_entity_mappings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load_entities:\n"
            "    - entity_id: sensor.household_energy\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "    - entity_id: sensor.ev_energy\n"
            "      state_class: total\n"
            "      unit: kWh\n"
            "      operation: subtract\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    assert [
        (entity.entity_id, entity.state_class, entity.unit, entity.operation)
        for entity in configuration.home_assistant.household_load_entities or []
    ] == [
        ("sensor.household_energy", "total_increasing", "kWh", "add"),
        ("sensor.ev_energy", "total", "kWh", "subtract"),
    ]
    assert all(
        entity.maximum_interval_energy_kwh == 100
        for entity in configuration.home_assistant.household_load_entities or []
    )
    assert configuration.home_assistant.household_load_source_id == "household_load"


def test_load_configuration_accepts_entity_physical_energy_limits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load_entities:\n"
            "    - entity_id: sensor.household_energy\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "      maximum_interval_energy_kwh: 15\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    entities = configuration.home_assistant.household_load_entities
    assert entities is not None
    entity = entities[0]
    assert entity.maximum_interval_energy_kwh == 15


def test_load_configuration_rejects_non_positive_entity_physical_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load_entities:\n"
            "    - entity_id: sensor.household_energy\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "      maximum_interval_energy_kwh: 0\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="maximum_interval_energy_kwh"):
        load_configuration(path)


def test_load_configuration_returns_grid_flow_entity_mappings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  grid_import_entities:\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "  grid_export_entities:\n"
            "    - entity_id: sensor.grid_export\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    assert [
        entity.entity_id
        for entity in configuration.home_assistant.grid_import_entities or []
    ] == ["sensor.grid_import"]
    assert [
        entity.entity_id
        for entity in configuration.home_assistant.grid_export_entities or []
    ] == ["sensor.grid_export"]
    assert configuration.home_assistant.grid_flow_source_id == "grid_flow"


def test_load_configuration_returns_battery_entity_mappings(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  battery:\n"
            "    state_of_charge:\n"
            "      entity_id: sensor.battery_soc\n"
            "      unit: '%'\n"
            "    capacity:\n"
            "      entity_id: sensor.battery\n"
            "      attribute: capacity_kwh\n"
            "      unit: kWh\n"
            "    minimum_soc:\n"
            "      entity_id: sensor.battery\n"
            "      attribute: minimum_soc_kwh\n"
            "      unit: kWh\n"
            "    maximum_soc:\n"
            "      entity_id: sensor.battery\n"
            "      attribute: maximum_soc_kwh\n"
            "      unit: kWh\n"
            "    maximum_charge:\n"
            "      entity_id: sensor.battery\n"
            "      attribute: maximum_charge_kw\n"
            "      unit: kW\n"
            "    maximum_discharge:\n"
            "      entity_id: sensor.battery\n"
            "      attribute: maximum_discharge_kw\n"
            "      unit: kW\n"
            "    battery_efficiency:\n"
            "      entity_id: sensor.battery\n"
            "      attribute: battery_efficiency\n"
            "      unit: ratio\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    assert configuration.home_assistant.battery is not None
    assert configuration.home_assistant.battery.state_of_charge.unit == "%"
    capacity = configuration.home_assistant.battery.capacity
    assert isinstance(capacity, HomeAssistantBatteryEntityConfiguration)
    assert capacity.attribute == "capacity_kwh"
    assert configuration.home_assistant.battery_source_id == "battery"


def test_load_configuration_accepts_battery_constants(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  battery:\n"
            "    state_of_charge:\n"
            "      entity_id: sensor.battery_soc\n"
            "      unit: '%'\n"
            "    capacity:\n"
            "      value: 28.7\n"
            "      unit: kWh\n"
            "    minimum_soc:\n"
            "      value: 5\n"
            "      unit: '%'\n"
            "    maximum_soc:\n"
            "      value: 100\n"
            "      unit: '%'\n"
            "    maximum_charge:\n"
            "      value: 12\n"
            "      unit: kW\n"
            "    maximum_discharge:\n"
            "      value: 12\n"
            "      unit: kW\n"
            "    battery_efficiency:\n"
            "      value: 0.85\n"
            "      unit: ratio\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    battery = configuration.home_assistant.battery
    assert battery is not None
    assert isinstance(battery.capacity, BatteryConstantConfiguration)
    assert battery.capacity.value == 28.7
    assert battery.maximum_soc.unit == "%"


def test_load_configuration_accepts_calculated_efficiency_configuration(
    tmp_path: Path,
) -> None:
    del tmp_path

    def energy_entity(name: str) -> dict[str, object]:
        return {
            "entity_id": f"sensor.{name}",
            "state_class": "total_increasing",
            "unit": "kWh",
            "operation": "add",
        }

    def leg(name: str) -> dict[str, list[dict[str, object]]]:
        return {
            "energy_in": [energy_entity(f"{name}_in")],
            "energy_out": [energy_entity(f"{name}_out")],
        }

    configuration = HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            "battery": {
                "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                "capacity": 28.7,
                "minimum_soc": 5,
                "maximum_soc": 100,
                "maximum_charge": 12,
                "maximum_discharge": 12,
                "efficiency_calculation": {
                    "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                    "battery": leg("battery"),
                    "inverter_charge": leg("charge"),
                    "inverter_discharge": leg("discharge"),
                },
            },
        }
    )

    assert configuration.battery is not None
    battery = configuration.battery
    assert battery.efficiency_calculation is not None
    assert battery.efficiency_calculation.battery.energy_in[0].entity_id == (
        "sensor.battery_in"
    )


def test_load_configuration_warns_when_fixed_battery_efficiency_overrides_calculated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    energy_entity = {
        "entity_id": "sensor.energy",
        "state_class": "total_increasing",
        "unit": "kWh",
        "operation": "add",
    }
    battery = {
        "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
        "capacity": 10,
        "minimum_soc": 1,
        "maximum_soc": 10,
        "maximum_charge": 4,
        "maximum_discharge": 4,
        "battery_efficiency": 0.85,
        "efficiency_calculation": {
            "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
            "battery": {
                "energy_in": [energy_entity],
                "energy_out": [energy_entity],
            },
            "inverter_charge": {
                "energy_in": [energy_entity],
                "energy_out": [energy_entity],
            },
            "inverter_discharge": {
                "energy_in": [energy_entity],
                "energy_out": [energy_entity],
            },
        },
    }
    HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            "battery": battery,
        }
    )

    assert "fixed_over_calculated" in caplog.text


def test_load_configuration_accepts_canonical_numeric_battery_constants(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  battery:\n"
            "    state_of_charge:\n"
            "      entity_id: sensor.battery_soc\n"
            "      unit: '%'\n"
            "    capacity: 28.7\n"
            "    minimum_soc: 5\n"
            "    maximum_soc: 100\n"
            "    maximum_charge: 12\n"
            "    maximum_discharge: 12\n"
            "    battery_efficiency: 0.85\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    battery = configuration.home_assistant.battery
    assert battery is not None
    assert isinstance(battery.capacity, BatteryConstantConfiguration)
    assert battery.capacity.value == 28.7
    assert battery.capacity.unit == "kWh"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("capacity", -1, "greater than zero"),
        ("maximum_charge", 0, "greater than zero"),
        ("minimum_soc", 101, "must not exceed 100"),
        ("battery_efficiency", 1.1, "no greater than one"),
    ],
)
def test_load_configuration_rejects_invalid_battery_constants(
    tmp_path: Path,
    field: str,
    value: float,
    message: str,
) -> None:
    path = tmp_path / "config.yaml"
    document = VALID_CONFIGURATION.replace(
        CONFIG_MARKER,
        "  battery:\n"
        "    state_of_charge:\n"
        "      entity_id: sensor.battery_soc\n"
        "      unit: '%'\n"
        "    capacity: 28.7\n"
        "    minimum_soc: 5\n"
        "    maximum_soc: 100\n"
        "    maximum_charge: 12\n"
        "    maximum_discharge: 12\n"
        "    battery_efficiency: 0.85\n",
    )
    path.write_text(
        document.replace(f"    {field}: ", f"    {field}: {value} # "),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(path)


def test_load_configuration_rejects_invalid_battery_mapping_unit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  battery:\n"
            "    state_of_charge:\n"
            "      entity_id: sensor.battery_soc\n"
            "      unit: W\n"
            "    capacity:\n"
            "      entity_id: sensor.battery_capacity\n"
            "      unit: kWh\n"
            "    minimum_soc:\n"
            "      entity_id: sensor.battery_minimum_soc\n"
            "      unit: kWh\n"
            "    maximum_soc:\n"
            "      entity_id: sensor.battery_maximum_soc\n"
            "      unit: kWh\n"
            "    maximum_charge:\n"
            "      entity_id: sensor.battery_maximum_charge\n"
            "      unit: kW\n"
            "    maximum_discharge:\n"
            "      entity_id: sensor.battery_maximum_discharge\n"
            "      unit: kW\n"
            "    battery_efficiency:\n"
            "      entity_id: sensor.battery_efficiency\n"
            "      unit: ratio\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="battery.state_of_charge"):
        load_configuration(path)


def test_load_configuration_rejects_reused_battery_entity_and_attribute(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    document = VALID_CONFIGURATION.replace(
        CONFIG_MARKER,
        "  battery:\n"
        "    state_of_charge:\n"
        "      entity_id: sensor.battery\n"
        "      attribute: value\n"
        "      unit: '%'\n"
        "    capacity:\n"
        "      entity_id: sensor.battery\n"
        "      attribute: value\n"
        "      unit: kWh\n"
        "    minimum_soc:\n"
        "      entity_id: sensor.battery_minimum_soc\n"
        "      unit: kWh\n"
        "    maximum_soc:\n"
        "      entity_id: sensor.battery_maximum_soc\n"
        "      unit: kWh\n"
        "    maximum_charge:\n"
        "      entity_id: sensor.battery_maximum_charge\n"
        "      unit: kW\n"
        "    maximum_discharge:\n"
        "      entity_id: sensor.battery_maximum_discharge\n"
        "      unit: kW\n"
        "    battery_efficiency:\n"
        "      entity_id: sensor.battery_efficiency\n"
        "      unit: ratio\n",
    )
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="reuse"):
        load_configuration(path)


def test_load_configuration_allows_grid_only_home_assistant_provider(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  grid_import_entities:\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "  grid_export_entities:\n"
            "    - entity_id: sensor.grid_export\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    assert configuration.home_assistant.household_load_entities is None


def test_load_configuration_allows_cross_category_energy_entity_reuse(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load_entities:\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "  grid_import_entities:\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "  grid_export_entities:\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    household_load_entities = configuration.home_assistant.household_load_entities
    grid_import_entities = configuration.home_assistant.grid_import_entities
    assert household_load_entities is not None
    assert grid_import_entities is not None
    assert household_load_entities[0].entity_id == "sensor.grid_import"
    assert grid_import_entities[0].entity_id == "sensor.grid_import"


def test_load_configuration_rejects_duplicate_energy_entities_within_category(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  grid_import_entities:\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n"
            "  grid_export_entities:\n"
            "    - entity_id: sensor.grid_export\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="grid_import.*duplicates"):
        load_configuration(path)


def test_load_configuration_rejects_incomplete_grid_flow_mapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  grid_import_entities:\n"
            "    - entity_id: sensor.grid_import\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="configured together"):
        load_configuration(path)


def test_load_configuration_does_not_match_unconfigured_provider_sources(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(CONFIG_MARKER, ""),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert (
        configuration.is_configured_household_load_source(
            "home-assistant", "household_load"
        )
        is False
    )
    assert (
        configuration.is_configured_grid_flow_source("home-assistant", "grid_flow")
        is False
    )


def test_load_configuration_uses_explicit_single_entity_mapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIGURATION, encoding="utf-8")

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    assert configuration.home_assistant.household_load_entities is not None
    assert configuration.home_assistant.household_load_entities[0].state_class == (
        "total_increasing"
    )
    assert configuration.home_assistant.household_load_entities[0].unit == "kWh"
    assert configuration.home_assistant.household_load_source_id == "household_load"


def test_load_configuration_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Configuration file not found"):
        load_configuration(tmp_path / "missing.yaml")


def test_load_configuration_rejects_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace("maximum_import_kw: 10", "maximum_import_kw: 0"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="grid.maximum_import_kw"):
        load_configuration(path)


def test_load_configuration_rejects_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("grid: [", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Invalid YAML"):
        load_configuration(path)


def test_load_configuration_allows_no_home_assistant_provider(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.split("home_assistant:", 1)[0], encoding="utf-8"
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is None


def test_load_configuration_returns_forecast_solar_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION
        + "forecast_solar:\n"
        + "  latitude: 52.52\n"
        + "  longitude: 13.41\n"
        + "  declination_degrees: 35\n"
        + "  azimuth_degrees: 0\n"
        + "  peak_power_kw: 8\n",
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.forecast_solar is not None
    assert configuration.forecast_solar.base_url.host == "api.forecast.solar"
    assert configuration.forecast_solar.pv_generation_source_id == "pv_generation"


def test_load_configuration_returns_awattar_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION + "awattar:\n  timeout_seconds: 5\n",
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.awattar is not None
    assert configuration.awattar.base_url.host == "api.awattar.de"
    assert configuration.awattar.electricity_price_source_id == "de"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latitude", 91),
        ("longitude", 181),
        ("declination_degrees", 91),
        ("azimuth_degrees", 181),
        ("peak_power_kw", 0),
    ],
)
def test_load_configuration_rejects_invalid_forecast_solar_settings(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION
        + "forecast_solar:\n"
        + "  latitude: 52.52\n"
        + "  longitude: 13.41\n"
        + "  declination_degrees: 35\n"
        + "  azimuth_degrees: 0\n"
        + "  peak_power_kw: 8\n",
        encoding="utf-8",
    )
    defaults = {
        "latitude": 52.52,
        "longitude": 13.41,
        "declination_degrees": 35,
        "azimuth_degrees": 0,
        "peak_power_kw": 8,
    }
    document = path.read_text(encoding="utf-8").replace(
        f"  {field}: {defaults[field]}\n",
        f"  {field}: {value}\n",
    )
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=f"forecast_solar.{field}"):
        load_configuration(path)


def test_load_configuration_rejects_invalid_home_assistant_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace("timeout_seconds: 10", "timeout_seconds: 0"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="home_assistant.timeout_seconds"):
        load_configuration(path)


def test_load_configuration_allows_optional_freshness_threshold(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace("  max_data_age_seconds: 7200\n", ""),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    assert configuration.home_assistant.max_data_age_seconds is None


def test_load_configuration_returns_persistence_directory(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION + "persistence:\n  directory: /var/lib/provider-data\n",
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.persistence is not None
    assert configuration.persistence.directory == Path("/var/lib/provider-data")


def test_load_configuration_returns_orchestration_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION
        + "persistence:\n  directory: provider-data\n"
        + "orchestration:\n"
        + "  enabled: true\n"
        + "  startup_fetch: false\n"
        + "  sources:\n"
        + "    household_load:\n"
        + "      interval_seconds: 300\n"
        + "      history_lookback_seconds: 3600\n"
        + "  optimization:\n"
        + "    enabled: true\n"
        + "    required_sources: [household_load]\n",
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.orchestration is not None
    assert configuration.orchestration.startup_fetch is False
    schedule = configuration.orchestration.sources["household_load"]
    assert schedule.interval_seconds == 300
    assert schedule.history_lookback_seconds == 3600
    assert configuration.orchestration.optimization.required_sources == [
        "household_load"
    ]


def test_load_configuration_rejects_removed_horizon_setting(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION
        + "persistence:\n  directory: provider-data\n"
        + "orchestration:\n"
        + "  sources:\n"
        + "    household_load:\n"
        + "      interval_seconds: 300\n"
        + "      horizon_hours: 12\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="horizon_hours"):
        load_configuration(path)


def test_load_configuration_rejects_orchestration_without_persistence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION
        + "orchestration:\n"
        + "  enabled: true\n"
        + "  sources:\n"
        + "    household_load:\n"
        + "      interval_seconds: 300\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="persistence"):
        load_configuration(path)


def test_load_configuration_rejects_unconfigured_plan_source(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION
        + "persistence:\n  directory: provider-data\n"
        + "orchestration:\n"
        + "  optimization:\n"
        + "    enabled: true\n"
        + "    required_sources: [pv_generation]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="not configured as sources"):
        load_configuration(path)


def test_load_configuration_allows_persistence_without_provider(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.split("home_assistant:", 1)[0]
        + "persistence:\n  directory: provider-data\n",
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.persistence is not None
    assert configuration.home_assistant is None
