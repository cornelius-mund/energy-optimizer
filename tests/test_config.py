"""Tests for runtime configuration loading."""

from pathlib import Path

import pytest

from energy_optimizer.config import ConfigurationError, load_configuration

VALID_CONFIGURATION = """
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
  household_load_entity_id: sensor.household_load
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
        VALID_CONFIGURATION.replace(
            "home_assistant:\n  base_url: http://homeassistant.local:8123\n"
            "  token: test-token\n  household_load_entity_id: sensor.household_load\n"
            "  timeout_seconds: 10\n"
            "  max_data_age_seconds: 7200\n",
            "",
        ),
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.home_assistant is None


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
        + "      horizon_hours: 12\n"
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
    assert schedule.horizon_hours == 12
    assert schedule.history_lookback_seconds == 3600
    assert configuration.orchestration.optimization.required_sources == [
        "household_load"
    ]


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
        VALID_CONFIGURATION.replace(
            "home_assistant:\n  base_url: http://homeassistant.local:8123\n"
            "  token: test-token\n  household_load_entity_id: sensor.household_load\n"
            "  timeout_seconds: 10\n  max_data_age_seconds: 7200\n",
            "",
        )
        + "persistence:\n  directory: provider-data\n",
        encoding="utf-8",
    )

    configuration = load_configuration(path)

    assert configuration.persistence is not None
    assert configuration.home_assistant is None
