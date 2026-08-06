"""Tests for the HTTP API."""

from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from energy_optimizer.api import app


def test_health_returns_service_status_and_version(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def valid_request() -> dict[str, object]:
    return {
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "pv_generation_kw": [0.0, 0.4],
        "import_price_eur_per_kwh": [0.30, 0.25],
        "export_price_eur_per_kwh": [0.08, 0.08],
    }


def test_optimize_accepts_a_valid_hourly_request(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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

    with TestClient(app) as client:
        response = client.post("/optimize", json=valid_request())

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "hours": 2,
    }


def test_optimize_rejects_invalid_hourly_inputs(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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
    request = valid_request()
    request["pv_generation_kw"] = [0.0]

    with TestClient(app) as client:
        response = client.post("/optimize", json=request)

    assert response.status_code == 422
    assert "time-series lengths must match" in response.text


def test_optimize_rejects_non_hourly_interval(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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
    request = valid_request()
    request["interval_minutes"] = 30

    with TestClient(app) as client:
        response = client.post("/optimize", json=request)

    assert response.status_code == 422
    assert "interval_minutes" in response.text


def test_optimize_rejects_naive_start_time(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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
    request = valid_request()
    request["start_time"] = "2026-01-01T00:00:00"

    with TestClient(app) as client:
        response = client.post("/optimize", json=request)

    assert response.status_code == 422
    assert "timezone" in response.text


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


def test_battery_contract_accepts_a_valid_request(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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

    with TestClient(app) as client:
        response = client.post("/api/v1/battery", json=battery_request())

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
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


def test_battery_contract_allows_direct_submissions_without_source(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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
    request = battery_request()
    request.pop("source")

    with TestClient(app) as client:
        response = client.post("/api/v1/battery", json=request)

    assert response.status_code == 200
    assert response.json()["source"] is None


def test_battery_contract_rejects_invalid_payloads(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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
    invalid_requests = [
        {**battery_request(), "state_of_charge_kwh": []},
        {**battery_request(), "state_of_charge_kwh": [11.0]},
        {**battery_request(), "minimum_soc_kwh": 11.0},
        {**battery_request(), "maximum_soc_kwh": 1.0},
        {**battery_request(), "initial_soc_kwh": 1.0},
        {**battery_request(), "maximum_charge_kw": 0.0},
        {**battery_request(), "charge_efficiency": 0.0},
        {**battery_request(), "interval_minutes": 30},
        {**battery_request(), "start_time": "2026-01-01T00:00:00"},
        {**battery_request(), "schema_version": "2"},
        {**battery_request(), "unexpected": True},
    ]

    with TestClient(app) as client:
        responses = [
            client.post("/api/v1/battery", json=request) for request in invalid_requests
        ]

    assert all(response.status_code == 422 for response in responses)


def test_battery_contract_rejects_more_than_one_week(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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
    request = battery_request()
    request["state_of_charge_kwh"] = [5.0] * 169

    with TestClient(app) as client:
        response = client.post("/api/v1/battery", json=request)

    assert response.status_code == 422
    assert "168" in response.text
