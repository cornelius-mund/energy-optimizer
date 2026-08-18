from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from energy_optimizer.api import app


@pytest.fixture
def minimal_configuration(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    configuration = tmp_path / "config.yaml"
    configuration.write_text(
        """
time_resolution_minutes: 60
grid:
  maximum_import_kw: 10
  maximum_export_kw: 10
solver:
  name: highs
  time_limit_seconds: 60
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    return configuration


@pytest.fixture
def persistence_configuration(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    configuration = tmp_path / "config.yaml"
    configuration.write_text(
        f"""
time_resolution_minutes: 60
grid:
  maximum_import_kw: 10
  maximum_export_kw: 10
solver:
  name: highs
  time_limit_seconds: 60
persistence:
  directory: {tmp_path / "provider-data"}
home_assistant:
  base_url: http://homeassistant.local:8123
  token: test-token
  household_load_entities:
    - entity_id: sensor.household_energy
      state_class: total_increasing
      unit: kWh
      operation: add
  grid_import_entities:
    - entity_id: sensor.grid_import
      state_class: total_increasing
      unit: kWh
      operation: add
  grid_export_entities:
    - entity_id: sensor.grid_export
      state_class: total_increasing
      unit: kWh
      operation: add
  timeout_seconds: 10
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))
    return configuration


@pytest.fixture
def client(minimal_configuration: Path) -> TestClient:
    return TestClient(app)


@pytest.fixture
def persistence_client(persistence_configuration: Path) -> TestClient:
    return TestClient(app)


@pytest.fixture
def valid_request() -> dict[str, object]:
    return {
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "pv_generation_kw": [0.0, 0.4],
        "import_price_eur_per_kwh": [0.30, 0.25],
        "export_price_eur_per_kwh": [0.08, 0.08],
    }


@pytest.fixture
def electricity_price_request() -> dict[str, object]:
    return {
        "schema_version": "1",
        "timestamps": [
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T01:00:00+00:00",
        ],
        "interval_minutes": 60,
        "import_price_eur_per_kwh": [0.30, 0.25],
        "export_price_eur_per_kwh": [0.08, 0.08],
        "unit": "EUR/kWh",
        "source": {"provider": "day-ahead-market"},
        "retrieved_at": "2025-12-31T23:00:00+00:00",
        "expires_at": "2026-01-01T03:00:00+00:00",
    }


@pytest.fixture
def battery_request() -> dict[str, object]:
    return {
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "state_of_charge_kwh": [5.0, 5.5],
        "capacity_kwh": 10.0,
        "minimum_soc_kwh": 2.0,
        "maximum_soc_kwh": 10.0,
        "initial_soc_kwh": 5.0,
        "maximum_charge_kw": 4.0,
        "maximum_discharge_kw": 4.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "unit": "kWh",
        "power_unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.battery_soc",
        },
    }


@pytest.fixture
def household_load_request() -> dict[str, object]:
    return {
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "household_load",
        },
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "latest_observation_at": "2026-01-01T01:00:00+00:00",
    }


@pytest.fixture
def grid_flow_request() -> dict[str, object]:
    return {
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "import_kw": [1.2, 1.0],
        "export_kw": [0.0, 0.4],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.grid_import",
        },
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "latest_observation_at": "2026-01-01T01:00:00+00:00",
    }


@pytest.fixture
def grid_flow_persisted_request(
    grid_flow_request: dict[str, object],
) -> dict[str, object]:
    request = grid_flow_request.copy()
    request["source"] = {
        "provider": "home-assistant",
        "entity_id": "grid_flow",
    }
    return request


@pytest.fixture
def pv_generation_request() -> dict[str, object]:
    return {
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "generation_kw": [0.0, 2.4],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.pv_generation",
        },
    }
