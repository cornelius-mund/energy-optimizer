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
from home_assistant_fixtures import aggregate_settings

CONFIG_MARKER = """  household_load:
    terms:
      - operation: add
        entities:
          - entity_id: sensor.household_load
            state_class: total_increasing
            unit: kWh
"""

GRID_FLOW_CONFIGURATION = (
    "  grid_import:\n"
    "    terms:\n"
    "      - operation: add\n"
    "        entities:\n"
    "          - entity_id: sensor.grid_import\n"
    "            state_class: total_increasing\n"
    "            unit: kWh\n"
    "  grid_export:\n"
    "    terms:\n"
    "      - operation: add\n"
    "        entities:\n"
    "          - entity_id: sensor.grid_export\n"
    "            state_class: total_increasing\n"
    "            unit: kWh\n"
)

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
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "      - operation: subtract\n"
            "        entities:\n"
            "          - entity_id: sensor.ev_energy\n"
            "            state_class: total\n"
            "            unit: kWh\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    household_load = configuration.home_assistant.household_load
    assert household_load is not None
    assert household_load.part == "net"
    assert [
        (
            term.operation,
            [
                (entity.entity_id, entity.state_class, entity.unit)
                for entity in term.entities
            ],
        )
        for term in household_load.terms
    ] == [
        ("add", [("sensor.household_energy", "total_increasing", "kWh")]),
        ("subtract", [("sensor.ev_energy", "total", "kWh")]),
    ]
    assert all(
        entity.maximum_interval_energy_kwh == 100
        for term in household_load.terms
        for entity in term.entities
    )
    assert configuration.home_assistant.household_load_source_id == "household_load"


def test_load_configuration_accepts_entity_physical_energy_limits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "            maximum_interval_energy_kwh: 15\n",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    household_load = configuration.home_assistant.household_load
    assert household_load is not None
    entity = household_load.terms[0].entities[0]
    assert entity.maximum_interval_energy_kwh == 15


def test_load_configuration_rejects_non_positive_entity_physical_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "            maximum_interval_energy_kwh: 0\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="maximum_interval_energy_kwh"):
        load_configuration(path)


def household_entity_configuration(tmp_path: Path, entity_lines: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            f"{entity_lines}"
            "            unit: kWh\n",
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("state_class", ["total", "total_increasing"])
@pytest.mark.parametrize("tolerance", ["0.01", "0", "0.5"])
def test_decrease_tolerance_is_rejected_as_an_unknown_key(
    tmp_path: Path, state_class: str, tolerance: str
) -> None:
    """A counter decrease of any size excludes its hours, so nothing tolerates it.

    ``decrease_tolerance_kwh`` no longer exists. A configuration that still sets
    it must fail at load and name the key, instead of silently ignoring a setting
    that used to change which data is imported.
    """
    path = household_entity_configuration(
        tmp_path,
        f"            state_class: {state_class}\n"
        f"            decrease_tolerance_kwh: {tolerance}\n",
    )

    with pytest.raises(ConfigurationError) as error:
        load_configuration(path)

    assert (
        "home_assistant.household_load.terms.0.entities.0.decrease_tolerance_kwh: "
        "Extra inputs are not permitted"
    ) in str(error.value)


def test_decrease_tolerance_is_rejected_for_every_energy_expression(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  grid_import:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.grid_import\n"
            "            state_class: total\n"
            "            unit: kWh\n"
            "            decrease_tolerance_kwh: 0.01\n"
            "  grid_export:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.grid_export\n"
            "            state_class: total\n"
            "            unit: kWh\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="decrease_tolerance_kwh"):
        load_configuration(path)


@pytest.mark.parametrize("state_class", ["total", "total_increasing"])
def test_energy_entity_accepts_a_maximum_interval_limit_for_either_state_class(
    tmp_path: Path, state_class: str
) -> None:
    path = household_entity_configuration(
        tmp_path,
        f"            state_class: {state_class}\n"
        "            maximum_interval_energy_kwh: 25\n",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    household_load = configuration.home_assistant.household_load
    assert household_load is not None
    entity = household_load.terms[0].entities[0]
    assert entity.state_class == state_class
    assert entity.maximum_interval_energy_kwh == 25


def test_load_configuration_returns_grid_flow_entity_mappings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(CONFIG_MARKER, GRID_FLOW_CONFIGURATION),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    grid_import = configuration.home_assistant.grid_import
    grid_export = configuration.home_assistant.grid_export
    assert grid_import is not None
    assert grid_export is not None
    assert grid_import.entity_ids == ("sensor.grid_import",)
    assert grid_export.entity_ids == ("sensor.grid_export",)
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
        }

    def leg(name: str) -> dict[str, dict[str, object]]:
        return {
            "energy_in": aggregate_settings(add=[energy_entity(f"{name}_in")]),
            "energy_out": aggregate_settings(add=[energy_entity(f"{name}_out")]),
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
    assert battery.efficiency_calculation.battery.energy_in.entity_ids == (
        "sensor.battery_in",
    )


def test_load_configuration_warns_when_fixed_battery_efficiency_overrides_calculated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    energy_entity = {
        "entity_id": "sensor.energy",
        "state_class": "total_increasing",
        "unit": "kWh",
    }
    energy_leg = {
        "energy_in": aggregate_settings(add=[energy_entity]),
        "energy_out": aggregate_settings(add=[energy_entity]),
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
            "battery": energy_leg,
            "inverter_charge": energy_leg,
            "inverter_discharge": energy_leg,
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
        VALID_CONFIGURATION.replace(CONFIG_MARKER, GRID_FLOW_CONFIGURATION),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    assert configuration.home_assistant.household_load is None


def test_load_configuration_allows_cross_category_energy_entity_reuse(
    tmp_path: Path,
) -> None:
    def aggregate(name: str) -> str:
        return (
            f"  {name}:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.grid_import\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
        )

    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            aggregate("household_load")
            + aggregate("grid_import")
            + aggregate("grid_export"),
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is not None
    household_load = configuration.home_assistant.household_load
    grid_import = configuration.home_assistant.grid_import
    assert household_load is not None
    assert grid_import is not None
    assert household_load.entity_ids == ("sensor.grid_import",)
    assert grid_import.entity_ids == ("sensor.grid_import",)


def test_load_configuration_rejects_duplicate_energy_entities_within_aggregate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  grid_import:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.grid_import\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "          - entity_id: sensor.grid_import\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "  grid_export:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.grid_export\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError,
        match="grid_import.*energy aggregation entities must not contain duplicates",
    ):
        load_configuration(path)


def test_load_configuration_rejects_energy_entity_repeated_across_terms(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "      - operation: subtract\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError,
        match="household_load.*energy aggregation entities must not contain duplicates",
    ):
        load_configuration(path)


def test_load_configuration_rejects_repeated_operation_in_one_aggregate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.other_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError,
        match="an energy aggregation must not contain more than one term for the "
        "same operation",
    ):
        load_configuration(path)


@pytest.mark.parametrize(
    ("aggregate", "location"),
    [
        pytest.param(
            "  household_load:\n    terms: []\n",
            "home_assistant.household_load.terms",
            id="no-terms",
        ),
        pytest.param(
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities: []\n",
            "home_assistant.household_load.terms.0.entities",
            id="no-entities",
        ),
        pytest.param(
            "  household_load:\n"
            "    - entity_id: sensor.household_energy\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n",
            "home_assistant.household_load",
            id="old-entity-list",
        ),
        pytest.param(
            "  household_load:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n"
            "            operation: add\n",
            "home_assistant.household_load.terms.0.entities.0.operation",
            id="operation-on-an-entity",
        ),
        pytest.param(
            "  household_load:\n"
            "    part: negative\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.household_energy\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n",
            "home_assistant.household_load.part",
            id="unknown-part",
        ),
        pytest.param(
            "  household_load_entities:\n"
            "    - entity_id: sensor.household_energy\n"
            "      state_class: total_increasing\n"
            "      unit: kWh\n"
            "      operation: add\n",
            "home_assistant.household_load_entities",
            id="old-key-name",
        ),
    ],
)
def test_load_configuration_rejects_malformed_energy_aggregate(
    tmp_path: Path, aggregate: str, location: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(CONFIG_MARKER, aggregate),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match=location):
        load_configuration(path)


def test_every_energy_aggregate_accepts_the_positive_part() -> None:
    entity = {
        "entity_id": "sensor.energy",
        "state_class": "total_increasing",
        "unit": "kWh",
    }
    plain = aggregate_settings(add=[entity])
    positive = aggregate_settings(add=[entity], part="positive")

    configuration = HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            "household_load": positive,
            "grid_import": positive,
            "grid_export": plain,
            "battery": {
                "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                "capacity": 10,
                "minimum_soc": 1,
                "maximum_soc": 10,
                "maximum_charge": 4,
                "maximum_discharge": 4,
                "efficiency_calculation": {
                    "state_of_charge": {"entity_id": "sensor.soc", "unit": "%"},
                    "battery": {"energy_in": plain, "energy_out": plain},
                    "inverter_charge": {"energy_in": plain, "energy_out": positive},
                    "inverter_discharge": {"energy_in": positive, "energy_out": plain},
                },
            },
        }
    )

    assert configuration.household_load is not None
    assert configuration.grid_import is not None
    assert configuration.grid_export is not None
    assert configuration.household_load.part == "positive"
    assert configuration.grid_import.part == "positive"
    assert configuration.grid_export.part == "net"
    assert configuration.battery is not None
    calculation = configuration.battery.efficiency_calculation
    assert calculation is not None
    assert calculation.battery.energy_in.part == "net"
    assert calculation.inverter_charge.energy_out.part == "positive"
    assert calculation.inverter_discharge.energy_in.part == "positive"


def test_load_configuration_rejects_incomplete_grid_flow_mapping(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID_CONFIGURATION.replace(
            CONFIG_MARKER,
            "  grid_import:\n"
            "    terms:\n"
            "      - operation: add\n"
            "        entities:\n"
            "          - entity_id: sensor.grid_import\n"
            "            state_class: total_increasing\n"
            "            unit: kWh\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError,
        match="grid_import and grid_export must be configured together",
    ):
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
    household_load = configuration.home_assistant.household_load
    assert household_load is not None
    entity = household_load.terms[0].entities[0]
    assert entity.state_class == "total_increasing"
    assert entity.unit == "kWh"
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


def test_the_example_configuration_loads() -> None:
    """The documented example must stay valid as settings are added or removed."""
    example = Path(__file__).parents[1] / "config.example.yaml"

    configuration = load_configuration(example)

    assert configuration.home_assistant is not None
    household_load = configuration.home_assistant.household_load
    assert household_load is not None
    assert [
        entity.maximum_interval_energy_kwh
        for term in household_load.terms
        for entity in term.entities
    ] == [15, 11]
