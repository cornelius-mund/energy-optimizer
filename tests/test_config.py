"""Tests for runtime configuration loading."""

import textwrap
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import yaml

from energy_optimizer.config import (
    BatteryConstantConfiguration,
    Configuration,
    ConfigurationError,
    HomeAssistantBatteryEntityConfiguration,
    HomeAssistantConfiguration,
    HomeAssistantEnergyEntityConfiguration,
    load_configuration,
)
from home_assistant_fixtures import aggregate_settings


def dump(**sections: object) -> str:
    return yaml.safe_dump(sections, sort_keys=False)


def home_assistant_sections(**sections: object) -> str:
    return textwrap.indent(dump(**sections), "  ")


def energy_entity(
    entity_id: str, state_class: str = "total_increasing", **extra: object
) -> dict[str, object]:
    return {"entity_id": entity_id, "state_class": state_class, "unit": "kWh", **extra}


def aggregate(*terms: tuple[str, list[dict[str, object]]]) -> dict[str, object]:
    """Build an energy aggregation from ``(operation, entities)`` terms."""
    return {
        "terms": [
            {"operation": operation, "entities": entities}
            for operation, entities in terms
        ]
    }


def adding(*entities: dict[str, object]) -> dict[str, object]:
    return aggregate(("add", list(entities)))


def added(*entity_ids: str) -> dict[str, object]:
    return adding(*(energy_entity(entity_id) for entity_id in entity_ids))


def sensor(entity_id: str, unit: str, **fields: str) -> dict[str, str]:
    return {"entity_id": entity_id, **fields, "unit": unit}


HOUSEHOLD_LOAD = home_assistant_sections(household_load=added("sensor.household_load"))

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
{HOUSEHOLD_LOAD}
  timeout_seconds: 10
  max_data_age_seconds: 7200
"""
WITHOUT_HOME_ASSISTANT = VALID_CONFIGURATION.split("home_assistant:", 1)[0]
PERSISTENCE = "persistence:\n  directory: provider-data\n"

GRID_FLOW = {
    "grid_import": added("sensor.grid_import"),
    "grid_export": added("sensor.grid_export"),
}
FORECAST_SOLAR = {
    "latitude": 52.52,
    "longitude": 13.41,
    "declination_degrees": 35,
    "azimuth_degrees": 0,
    "peak_power_kw": 8,
}
BATTERY_SOC = sensor("sensor.battery_soc", "%")
CANONICAL_BATTERY = {
    "state_of_charge": BATTERY_SOC,
    "capacity": 28.7,
    "minimum_soc": 5,
    "maximum_soc": 100,
    "maximum_charge": 12,
    "maximum_discharge": 12,
    "battery_efficiency": 0.85,
}
SEPARATE_BATTERY_SENSORS = {
    "minimum_soc": sensor("sensor.battery_minimum_soc", "kWh"),
    "maximum_soc": sensor("sensor.battery_maximum_soc", "kWh"),
    "maximum_charge": sensor("sensor.battery_maximum_charge", "kW"),
    "maximum_discharge": sensor("sensor.battery_maximum_discharge", "kW"),
    "battery_efficiency": sensor("sensor.battery_efficiency", "ratio"),
}


def document_with(**sections: object) -> str:
    """Return the valid document with ``sections`` replacing its household load."""
    return VALID_CONFIGURATION.replace(
        HOUSEHOLD_LOAD, home_assistant_sections(**sections)
    )


def household_document(**entity_fields: Any) -> str:
    """Return a document whose household load adds one entity with these fields."""
    entity = energy_entity("sensor.household_energy", **entity_fields)
    return document_with(household_load=adding(entity))


def load(tmp_path: Path, document: str) -> Configuration:
    path = tmp_path / "config.yaml"
    path.write_text(document, encoding="utf-8")
    return load_configuration(path)


def load_home_assistant(tmp_path: Path, document: str) -> HomeAssistantConfiguration:
    home_assistant = load(tmp_path, document).home_assistant
    assert home_assistant is not None
    return home_assistant


def load_household_entity(
    tmp_path: Path, document: str
) -> HomeAssistantEnergyEntityConfiguration:
    household_load = load_home_assistant(tmp_path, document).household_load
    assert household_load is not None
    return household_load.terms[0].entities[0]


def validate_home_assistant(**sections: Any) -> HomeAssistantConfiguration:
    return HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.test:8123",
            "token": "test-token",
            "timeout_seconds": 5,
            **sections,
        }
    )


def battery_with_calculation(
    calculation: dict[str, Any], **fields: Any
) -> dict[str, Any]:
    """Build battery settings whose efficiency is calculated from energy legs."""
    soc = sensor("sensor.soc", "%")
    return {
        "state_of_charge": soc,
        "capacity": 10,
        "minimum_soc": 1,
        "maximum_soc": 10,
        "maximum_charge": 4,
        "maximum_discharge": 4,
        **fields,
        "efficiency_calculation": {"state_of_charge": soc, **calculation},
    }


def test_load_configuration_returns_typed_values(tmp_path: Path) -> None:
    configuration = load(tmp_path, VALID_CONFIGURATION)

    assert configuration.time_resolution_minutes == 60
    assert configuration.grid.maximum_export_kw == 5
    assert configuration.solver.name == "highs"
    assert configuration.home_assistant is not None
    assert configuration.home_assistant.base_url.host == "homeassistant.local"
    assert configuration.home_assistant.token.get_secret_value() == "test-token"
    assert configuration.home_assistant.max_data_age_seconds == 7200


def test_load_configuration_defaults_the_timezone_to_utc(tmp_path: Path) -> None:
    assert load(tmp_path, VALID_CONFIGURATION).timezone == "UTC"


@pytest.mark.parametrize(
    "zone",
    ["UTC", "Europe/Berlin", "America/New_York", "Africa/Casablanca", "Etc/GMT+5"],
)
def test_load_configuration_returns_a_whole_hour_timezone(
    tmp_path: Path, zone: str
) -> None:
    configuration = load(tmp_path, f"timezone: {zone}\n" + VALID_CONFIGURATION)

    assert configuration.timezone == zone


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("''", "not a known IANA time zone name"),
        ("Foo/Bar", "not a known IANA time zone name"),
        ("utc", "case-sensitive"),
        ("../etc/passwd", "not a known IANA time zone name"),
        ("Asia/Kolkata", "whole number of hours"),
        ("Asia/Kathmandu", "whole number of hours"),
        ("Australia/Lord_Howe", "whole number of hours"),
    ],
)
def test_load_configuration_rejects_unusable_timezones(
    tmp_path: Path, value: str, reason: str
) -> None:
    with pytest.raises(ConfigurationError, match=r"timezone: .*" + reason):
        load(tmp_path, f"timezone: {value}\n" + VALID_CONFIGURATION)


@pytest.mark.parametrize("value", ["null", "5", "[Europe/Berlin]"])
def test_load_configuration_rejects_a_timezone_that_is_not_a_name(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ConfigurationError, match="timezone"):
        load(tmp_path, f"timezone: {value}\n" + VALID_CONFIGURATION)


def test_load_configuration_returns_explicit_energy_entity_mappings(
    tmp_path: Path,
) -> None:
    document = document_with(
        household_load=aggregate(
            ("add", [energy_entity("sensor.household_energy")]),
            ("subtract", [energy_entity("sensor.ev_energy", "total")]),
        )
    )

    home_assistant = load_home_assistant(tmp_path, document)

    household_load = home_assistant.household_load
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
    assert home_assistant.household_load_source_id == "household_load"


def test_load_configuration_accepts_entity_physical_energy_limits(
    tmp_path: Path,
) -> None:
    document = household_document(maximum_interval_energy_kwh=15)

    entity = load_household_entity(tmp_path, document)

    assert entity.maximum_interval_energy_kwh == 15


@pytest.mark.parametrize("state_class", ["total", "total_increasing"])
@pytest.mark.parametrize("tolerance", [0.01, 0, 0.5])
def test_decrease_tolerance_is_rejected_as_an_unknown_key(
    tmp_path: Path, state_class: str, tolerance: float
) -> None:
    """A counter decrease of any size excludes its hours, so nothing tolerates it.

    ``decrease_tolerance_kwh`` no longer exists. A configuration that still sets
    it must fail at load and name the key, instead of silently ignoring a setting
    that used to change which data is imported.
    """
    document = household_document(
        state_class=state_class, decrease_tolerance_kwh=tolerance
    )

    with pytest.raises(ConfigurationError) as error:
        load(tmp_path, document)

    assert (
        "home_assistant.household_load.terms.0.entities.0.decrease_tolerance_kwh: "
        "Extra inputs are not permitted"
    ) in str(error.value)


@pytest.mark.parametrize("state_class", ["total", "total_increasing"])
def test_energy_entity_accepts_a_maximum_interval_limit_for_either_state_class(
    tmp_path: Path, state_class: str
) -> None:
    document = household_document(
        state_class=state_class, maximum_interval_energy_kwh=25
    )

    entity = load_household_entity(tmp_path, document)

    assert entity.state_class == state_class
    assert entity.maximum_interval_energy_kwh == 25


def test_load_configuration_returns_grid_flow_entity_mappings(tmp_path: Path) -> None:
    home_assistant = load_home_assistant(tmp_path, document_with(**GRID_FLOW))

    grid_import = home_assistant.grid_import
    grid_export = home_assistant.grid_export
    assert grid_import is not None
    assert grid_export is not None
    assert grid_import.entity_ids == ("sensor.grid_import",)
    assert grid_export.entity_ids == ("sensor.grid_export",)
    assert home_assistant.grid_flow_source_id == "grid_flow"


def test_load_configuration_returns_battery_entity_mappings(tmp_path: Path) -> None:
    battery = partial(sensor, "sensor.battery")
    document = document_with(
        battery={
            "state_of_charge": BATTERY_SOC,
            "capacity": battery("kWh", attribute="capacity_kwh"),
            "minimum_soc": battery("kWh", attribute="minimum_soc_kwh"),
            "maximum_soc": battery("kWh", attribute="maximum_soc_kwh"),
            "maximum_charge": battery("kW", attribute="maximum_charge_kw"),
            "maximum_discharge": battery("kW", attribute="maximum_discharge_kw"),
            "battery_efficiency": battery("ratio", attribute="battery_efficiency"),
        }
    )

    home_assistant = load_home_assistant(tmp_path, document)

    assert home_assistant.battery is not None
    assert home_assistant.battery.state_of_charge.unit == "%"
    capacity = home_assistant.battery.capacity
    assert isinstance(capacity, HomeAssistantBatteryEntityConfiguration)
    assert capacity.attribute == "capacity_kwh"
    assert home_assistant.battery_source_id == "battery"


def test_load_configuration_accepts_battery_constants(tmp_path: Path) -> None:
    document = document_with(
        battery={
            "state_of_charge": BATTERY_SOC,
            "capacity": {"value": 28.7, "unit": "kWh"},
            "minimum_soc": {"value": 5, "unit": "%"},
            "maximum_soc": {"value": 100, "unit": "%"},
            "maximum_charge": {"value": 12, "unit": "kW"},
            "maximum_discharge": {"value": 12, "unit": "kW"},
            "battery_efficiency": {"value": 0.85, "unit": "ratio"},
        }
    )

    battery = load_home_assistant(tmp_path, document).battery

    assert battery is not None
    assert isinstance(battery.capacity, BatteryConstantConfiguration)
    assert battery.capacity.value == 28.7
    assert battery.maximum_soc.unit == "%"


def test_load_configuration_accepts_calculated_efficiency_configuration() -> None:
    def leg(name: str) -> dict[str, dict[str, object]]:
        return {
            "energy_in": aggregate_settings(add=[energy_entity(f"sensor.{name}_in")]),
            "energy_out": aggregate_settings(add=[energy_entity(f"sensor.{name}_out")]),
        }

    configuration = validate_home_assistant(
        battery=battery_with_calculation(
            {
                "battery": leg("battery"),
                "inverter_charge": leg("charge"),
                "inverter_discharge": leg("discharge"),
            },
            capacity=28.7,
            minimum_soc=5,
            maximum_soc=100,
            maximum_charge=12,
            maximum_discharge=12,
        )
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
    energy = aggregate_settings(add=[energy_entity("sensor.energy")])
    energy_leg = {"energy_in": energy, "energy_out": energy}

    validate_home_assistant(
        battery=battery_with_calculation(
            {
                "battery": energy_leg,
                "inverter_charge": energy_leg,
                "inverter_discharge": energy_leg,
            },
            battery_efficiency=0.85,
        )
    )

    assert "fixed_over_calculated" in caplog.text


def test_load_configuration_accepts_canonical_numeric_battery_constants(
    tmp_path: Path,
) -> None:
    document = document_with(battery=CANONICAL_BATTERY)

    battery = load_home_assistant(tmp_path, document).battery

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
    tmp_path: Path, field: str, value: float, message: str
) -> None:
    document = document_with(battery={**CANONICAL_BATTERY, field: value})

    with pytest.raises(ConfigurationError, match=message):
        load(tmp_path, document)


def test_load_configuration_allows_grid_only_home_assistant_provider(
    tmp_path: Path,
) -> None:
    home_assistant = load_home_assistant(tmp_path, document_with(**GRID_FLOW))

    assert home_assistant.household_load is None


def test_load_configuration_allows_cross_category_energy_entity_reuse(
    tmp_path: Path,
) -> None:
    reused = added("sensor.grid_import")
    document = document_with(
        household_load=reused, grid_import=reused, grid_export=reused
    )

    home_assistant = load_home_assistant(tmp_path, document)

    household_load = home_assistant.household_load
    grid_import = home_assistant.grid_import
    assert household_load is not None
    assert grid_import is not None
    assert household_load.entity_ids == ("sensor.grid_import",)
    assert grid_import.entity_ids == ("sensor.grid_import",)


REJECTED_DOCUMENTS = {
    "non-positive-entity-physical-limit": (
        household_document(maximum_interval_energy_kwh=0),
        "maximum_interval_energy_kwh",
    ),
    "decrease-tolerance-in-any-energy-expression": (
        document_with(
            grid_import=adding(
                energy_entity(
                    "sensor.grid_import", "total", decrease_tolerance_kwh=0.01
                )
            ),
            grid_export=adding(energy_entity("sensor.grid_export", "total")),
        ),
        "decrease_tolerance_kwh",
    ),
    "battery-mapping-unit": (
        document_with(
            battery={
                "state_of_charge": sensor("sensor.battery_soc", "W"),
                "capacity": sensor("sensor.battery_capacity", "kWh"),
                **SEPARATE_BATTERY_SENSORS,
            }
        ),
        "battery.state_of_charge",
    ),
    "reused-battery-entity-and-attribute": (
        document_with(
            battery={
                "state_of_charge": sensor("sensor.battery", "%", attribute="value"),
                "capacity": sensor("sensor.battery", "kWh", attribute="value"),
                **SEPARATE_BATTERY_SENSORS,
            }
        ),
        "reuse",
    ),
    "duplicate-entities-within-aggregate": (
        document_with(
            grid_import=added("sensor.grid_import", "sensor.grid_import"),
            grid_export=added("sensor.grid_export"),
        ),
        "grid_import.*energy aggregation entities must not contain duplicates",
    ),
    "entity-repeated-across-terms": (
        document_with(
            household_load=aggregate(
                ("add", [energy_entity("sensor.household_energy")]),
                ("subtract", [energy_entity("sensor.household_energy")]),
            )
        ),
        "household_load.*energy aggregation entities must not contain duplicates",
    ),
    "repeated-operation-in-one-aggregate": (
        document_with(
            household_load=aggregate(
                ("add", [energy_entity("sensor.household_energy")]),
                ("add", [energy_entity("sensor.other_energy")]),
            )
        ),
        "an energy aggregation must not contain more than one term for the "
        "same operation",
    ),
    "aggregate-without-terms": (
        document_with(household_load={"terms": []}),
        "home_assistant.household_load.terms",
    ),
    "term-without-entities": (
        document_with(household_load=adding()),
        "home_assistant.household_load.terms.0.entities",
    ),
    "old-entity-list": (
        document_with(
            household_load=[energy_entity("sensor.household_energy", operation="add")]
        ),
        "home_assistant.household_load",
    ),
    "operation-on-an-entity": (
        document_with(
            household_load=adding(
                energy_entity("sensor.household_energy", operation="add")
            )
        ),
        "home_assistant.household_load.terms.0.entities.0.operation",
    ),
    "unknown-part": (
        document_with(
            household_load={"part": "negative", **added("sensor.household_energy")}
        ),
        "home_assistant.household_load.part",
    ),
    "old-key-name": (
        document_with(
            household_load_entities=[
                energy_entity("sensor.household_energy", operation="add")
            ]
        ),
        "home_assistant.household_load_entities",
    ),
    "incomplete-grid-flow-mapping": (
        document_with(grid_import=added("sensor.grid_import")),
        "grid_import and grid_export must be configured together",
    ),
    "invalid-grid-value": (
        VALID_CONFIGURATION.replace("maximum_import_kw: 10", "maximum_import_kw: 0"),
        "grid.maximum_import_kw",
    ),
    "malformed-yaml": ("grid: [", "Invalid YAML"),
    "invalid-home-assistant-setting": (
        VALID_CONFIGURATION.replace("timeout_seconds: 10", "timeout_seconds: 0"),
        "home_assistant.timeout_seconds",
    ),
    "removed-horizon-setting": (
        VALID_CONFIGURATION
        + PERSISTENCE
        + dump(
            orchestration={
                "sources": {
                    "household_load": {"interval_seconds": 300, "horizon_hours": 12}
                }
            }
        ),
        "horizon_hours",
    ),
    "orchestration-without-persistence": (
        VALID_CONFIGURATION
        + dump(
            orchestration={
                "enabled": True,
                "sources": {"household_load": {"interval_seconds": 300}},
            }
        ),
        "persistence",
    ),
    "unconfigured-plan-source": (
        VALID_CONFIGURATION
        + PERSISTENCE
        + dump(
            orchestration={
                "optimization": {"enabled": True, "required_sources": ["pv_generation"]}
            }
        ),
        "not configured as sources",
    ),
}


@pytest.mark.parametrize(
    ("document", "match"),
    REJECTED_DOCUMENTS.values(),
    ids=list(REJECTED_DOCUMENTS),
)
def test_load_configuration_rejects_invalid_documents(
    tmp_path: Path, document: str, match: str
) -> None:
    with pytest.raises(ConfigurationError, match=match):
        load(tmp_path, document)


def test_every_energy_aggregate_accepts_the_positive_part() -> None:
    plain = aggregate_settings(add=[energy_entity("sensor.energy")])
    positive = aggregate_settings(add=[energy_entity("sensor.energy")], part="positive")

    configuration = validate_home_assistant(
        household_load=positive,
        grid_import=positive,
        grid_export=plain,
        battery=battery_with_calculation(
            {
                "battery": {"energy_in": plain, "energy_out": plain},
                "inverter_charge": {"energy_in": plain, "energy_out": positive},
                "inverter_discharge": {"energy_in": positive, "energy_out": plain},
            }
        ),
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


def test_load_configuration_does_not_match_unconfigured_provider_sources(
    tmp_path: Path,
) -> None:
    configuration = load(tmp_path, VALID_CONFIGURATION.replace(HOUSEHOLD_LOAD, ""))

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
    home_assistant = load_home_assistant(tmp_path, VALID_CONFIGURATION)

    household_load = home_assistant.household_load
    assert household_load is not None
    entity = household_load.terms[0].entities[0]
    assert entity.state_class == "total_increasing"
    assert entity.unit == "kWh"
    assert home_assistant.household_load_source_id == "household_load"


def test_load_configuration_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Configuration file not found"):
        load_configuration(tmp_path / "missing.yaml")


def test_load_configuration_allows_no_home_assistant_provider(tmp_path: Path) -> None:
    assert load(tmp_path, WITHOUT_HOME_ASSISTANT).home_assistant is None


def test_load_configuration_returns_forecast_solar_settings(tmp_path: Path) -> None:
    document = VALID_CONFIGURATION + dump(forecast_solar=FORECAST_SOLAR)

    configuration = load(tmp_path, document)

    assert configuration.forecast_solar is not None
    assert configuration.forecast_solar.base_url.host == "api.forecast.solar"
    assert configuration.forecast_solar.pv_generation_source_id == "pv_generation"


def test_load_configuration_returns_awattar_settings(tmp_path: Path) -> None:
    configuration = load(
        tmp_path, VALID_CONFIGURATION + dump(awattar={"timeout_seconds": 5})
    )

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
    tmp_path: Path, field: str, value: object
) -> None:
    document = VALID_CONFIGURATION + dump(
        forecast_solar={**FORECAST_SOLAR, field: value}
    )

    with pytest.raises(ConfigurationError, match=f"forecast_solar.{field}"):
        load(tmp_path, document)


def test_load_configuration_allows_optional_freshness_threshold(
    tmp_path: Path,
) -> None:
    document = VALID_CONFIGURATION.replace("  max_data_age_seconds: 7200\n", "")

    assert load_home_assistant(tmp_path, document).max_data_age_seconds is None


def test_load_configuration_returns_persistence_directory(tmp_path: Path) -> None:
    document = VALID_CONFIGURATION + dump(
        persistence={"directory": "/var/lib/provider-data"}
    )

    configuration = load(tmp_path, document)

    assert configuration.persistence is not None
    assert configuration.persistence.directory == Path("/var/lib/provider-data")


def test_load_configuration_returns_orchestration_settings(tmp_path: Path) -> None:
    document = (
        VALID_CONFIGURATION
        + PERSISTENCE
        + dump(
            orchestration={
                "enabled": True,
                "startup_fetch": False,
                "sources": {
                    "household_load": {
                        "interval_seconds": 300,
                        "history_lookback_seconds": 3600,
                    }
                },
                "optimization": {
                    "enabled": True,
                    "required_sources": ["household_load"],
                },
            }
        )
    )

    configuration = load(tmp_path, document)

    assert configuration.orchestration is not None
    assert configuration.orchestration.startup_fetch is False
    schedule = configuration.orchestration.sources["household_load"]
    assert schedule.interval_seconds == 300
    assert schedule.history_lookback_seconds == 3600
    assert configuration.orchestration.optimization.required_sources == [
        "household_load"
    ]


def test_load_configuration_allows_persistence_without_provider(
    tmp_path: Path,
) -> None:
    configuration = load(tmp_path, WITHOUT_HOME_ASSISTANT + PERSISTENCE)

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
