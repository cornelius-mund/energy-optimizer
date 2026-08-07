"""Tests for the HTTP API."""

from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from energy_optimizer.api import MAX_HORIZON_HOURS, app


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


def household_load_request() -> dict[str, object]:
    return {
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.household_load",
        },
    }


def test_household_load_contract_accepts_a_valid_request(
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
        response = client.post("/api/v1/household-load", json=household_load_request())

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.household_load",
        },
    }


def test_household_load_contract_allows_direct_submissions_without_source(
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
    request = household_load_request()
    request.pop("source")

    with TestClient(app) as client:
        response = client.post("/api/v1/household-load", json=request)

    assert response.status_code == 200
    assert response.json()["source"] is None


def test_household_load_contract_rejects_invalid_payloads(
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
        ({**household_load_request(), "load_kw": []}, "load_kw"),
        ({**household_load_request(), "load_kw": [-0.1]}, "load_kw"),
        ({**household_load_request(), "load_kw": [1000.1]}, "load_kw"),
        ({**household_load_request(), "interval_minutes": 30}, "interval_minutes"),
        (
            {**household_load_request(), "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**household_load_request(), "schema_version": "2"}, "schema_version"),
        ({**household_load_request(), "unit": "W"}, "unit"),
        ({**household_load_request(), "unexpected": True}, "unexpected"),
    ]

    with TestClient(app) as client:
        responses = [
            (client.post("/api/v1/household-load", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_household_load_contract_rejects_non_finite_values(
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
        responses = [
            client.post(
                "/api/v1/household-load",
                content=(
                    '{"schema_version":"1",'
                    '"start_time":"2026-01-01T00:00:00+00:00",'
                    '"interval_minutes":60,"load_kw":[0.0,'
                    f"{value}"  # JSON's non-standard numeric values exercise parsing.
                    '],"unit":"kW"}'
                ),
                headers={"content-type": "application/json"},
            )
            for value in ("NaN", "Infinity", "-Infinity")
        ]

    assert all(response.status_code == 422 for response in responses)
    assert all("load_kw" in response.text for response in responses)


def test_household_load_contract_rejects_more_than_ten_years(
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
    request = household_load_request()
    request["load_kw"] = [1.0] * (MAX_HORIZON_HOURS + 1)

    with TestClient(app) as client:
        response = client.post("/api/v1/household-load", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text
