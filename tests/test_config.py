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
"""


def test_load_configuration_returns_typed_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIGURATION, encoding="utf-8")

    configuration = load_configuration(path)

    assert configuration.time_resolution_minutes == 60
    assert configuration.grid.maximum_export_kw == 5
    assert configuration.solver.name == "highs"


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
