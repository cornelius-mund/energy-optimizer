"""Tests for the HTTP API."""

import json
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from energy_optimizer.api import (
    MAX_HORIZON_HOURS,
    app,
    configured_frontend_directory,
    dashboard_redirect,
)
from energy_optimizer.storage import ProviderDataKey


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


def test_dashboard_is_served_by_the_application(
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
        response = client.get("/dashboard/")

    assert response.status_code == 200
    assert "Historic energy data" in response.text


def test_dashboard_serves_its_static_assets(
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
        html = client.get("/dashboard/")
        javascript = client.get("/dashboard/app.js")
        stylesheet = client.get("/dashboard/styles.css")

    assert html.status_code == 200
    assert javascript.status_code == 200
    assert stylesheet.status_code == 200


def test_dashboard_root_redirects_to_the_trailing_slash_path(
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
        response = client.get("/dashboard", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard/"


def test_missing_dashboard_assets_return_service_unavailable(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("energy_optimizer.api.FRONTEND_DIRECTORY", tmp_path / "missing")

    try:
        dashboard_redirect()
    except HTTPException as error:
        assert error.status_code == 503
        assert "dashboard assets" in str(error.detail)
    else:
        raise AssertionError("missing dashboard assets must not redirect")


def test_frontend_directory_can_be_configured_for_installed_deployments(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_FRONTEND_DIRECTORY", str(tmp_path))

    assert configured_frontend_directory() == tmp_path


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


def test_electricity_price_contract_accepts_a_valid_request(
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
        response = client.post(
            "/api/v1/electricity-prices", json=electricity_price_request()
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "schema_version": "1",
        "timestamps": [
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:00:00Z",
        ],
        "interval_minutes": 60,
        "import_price_eur_per_kwh": [0.30, 0.25],
        "export_price_eur_per_kwh": [0.08, 0.08],
        "unit": "EUR/kWh",
        "source": {"provider": "day-ahead-market", "entity_id": None},
        "retrieved_at": "2025-12-31T23:00:00Z",
        "expires_at": "2026-01-01T03:00:00Z",
    }


def test_electricity_price_contract_accepts_negative_and_boundary_prices(
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
    request = electricity_price_request()
    request["import_price_eur_per_kwh"] = [-100.0, 100.0]
    request["export_price_eur_per_kwh"] = [-100.0, 100.0]

    with TestClient(app) as client:
        response = client.post("/api/v1/electricity-prices", json=request)

    assert response.status_code == 200


def test_electricity_price_contract_rejects_invalid_payloads(
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
        ({**electricity_price_request(), "timestamps": []}, "timestamps"),
        (
            {
                **electricity_price_request(),
                "timestamps": [
                    "2026-01-01T01:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ],
            },
            "ascending",
        ),
        (
            {
                **electricity_price_request(),
                "timestamps": [
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ],
            },
            "ascending",
        ),
        (
            {
                **electricity_price_request(),
                "timestamps": [
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T04:00:00+00:00",
                ],
            },
            "spaced",
        ),
        (
            {**electricity_price_request(), "import_price_eur_per_kwh": [0.30]},
            "same length",
        ),
        (
            {
                **electricity_price_request(),
                "export_price_eur_per_kwh": [100.1, 0.08],
            },
            "export_price_eur_per_kwh",
        ),
        (
            {
                **electricity_price_request(),
                "import_price_eur_per_kwh": [-100.1, 0.25],
            },
            "import_price_eur_per_kwh",
        ),
        ({**electricity_price_request(), "unit": "EUR/MWh"}, "unit"),
        (
            {
                **electricity_price_request(),
                "timestamps": ["2026-01-01T00:00:00"],
            },
            "timestamps",
        ),
        (
            {
                **electricity_price_request(),
                "retrieved_at": "2026-01-01T04:00:00+00:00",
            },
            "retrieved_at",
        ),
        (
            {
                **electricity_price_request(),
                "expires_at": "2026-01-01T01:00:00+00:00",
            },
            "expires_at",
        ),
        ({**electricity_price_request(), "unexpected": True}, "unexpected"),
    ]

    with TestClient(app) as client:
        responses = [
            (client.post("/api/v1/electricity-prices", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_electricity_price_contract_rejects_non_finite_values(
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
                "/api/v1/electricity-prices",
                content=(
                    '{"schema_version":"1",'
                    '"timestamps":["2026-01-01T00:00:00+00:00"],'
                    '"interval_minutes":60,"import_price_eur_per_kwh":[0.1,'
                    f"{value}],"
                    '"export_price_eur_per_kwh":[0.08,0.08],"unit":"EUR/kWh",'
                    '"source":{"provider":"day-ahead-market"},'
                    '"retrieved_at":"2025-12-31T23:00:00+00:00",'
                    '"expires_at":"2026-01-01T03:00:00+00:00"}'
                ),
                headers={"content-type": "application/json"},
            )
            for value in ("NaN", "Infinity", "-Infinity")
        ]

    assert all(response.status_code == 422 for response in responses)
    assert all("import_price_eur_per_kwh" in response.text for response in responses)


def test_electricity_price_contract_rejects_more_than_ten_years(
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
    request = electricity_price_request()
    horizon = MAX_HORIZON_HOURS + 1
    request["timestamps"] = ["2026-01-01T00:00:00+00:00"] * horizon
    request["import_price_eur_per_kwh"] = [0.30] * horizon
    request["export_price_eur_per_kwh"] = [0.08] * horizon

    with TestClient(app) as client:
        response = client.post("/api/v1/electricity-prices", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


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
        ({**battery_request(), "state_of_charge_kwh": []}, "state_of_charge_kwh"),
        ({**battery_request(), "state_of_charge_kwh": [11.0]}, "state_of_charge_kwh"),
        ({**battery_request(), "minimum_soc_kwh": 11.0}, "minimum_soc_kwh"),
        ({**battery_request(), "maximum_soc_kwh": 100001.0}, "maximum_soc_kwh"),
        ({**battery_request(), "maximum_soc_kwh": 1.0}, "minimum_soc_kwh"),
        ({**battery_request(), "initial_soc_kwh": 1.0}, "initial_soc_kwh"),
        ({**battery_request(), "maximum_charge_kw": 0.0}, "maximum_charge_kw"),
        ({**battery_request(), "charge_efficiency": 0.0}, "charge_efficiency"),
        ({**battery_request(), "interval_minutes": 30}, "interval_minutes"),
        (
            {**battery_request(), "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**battery_request(), "schema_version": "2"}, "schema_version"),
        ({**battery_request(), "unit": "kW"}, "unit"),
        ({**battery_request(), "unexpected": True}, "unexpected"),
    ]

    with TestClient(app) as client:
        responses = [
            (client.post("/api/v1/battery", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_battery_contract_rejects_non_finite_values(
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

    non_finite_values = (float("nan"), float("inf"), float("-inf"))
    requests = []
    for value in non_finite_values:
        request = battery_request()
        request["state_of_charge_kwh"] = [5.0, value]
        requests.append((request, "state_of_charge_kwh"))

    scalar_fields = (
        "capacity_kwh",
        "minimum_soc_kwh",
        "maximum_soc_kwh",
        "initial_soc_kwh",
        "maximum_charge_kw",
        "maximum_discharge_kw",
        "charge_efficiency",
        "discharge_efficiency",
    )
    for field in scalar_fields:
        for value in non_finite_values:
            request = battery_request()
            request[field] = value
            requests.append((request, field))

    with TestClient(app) as client:
        responses = [
            (
                client.post(
                    "/api/v1/battery",
                    content=json.dumps(request),
                    headers={"content-type": "application/json"},
                ),
                expected_text,
            )
            for request, expected_text in requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_battery_contract_rejects_more_than_ten_years(
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
    request["state_of_charge_kwh"] = [5.0] * (MAX_HORIZON_HOURS + 1)

    with TestClient(app) as client:
        response = client.post("/api/v1/battery", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


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


def persistence_configuration(tmp_path: Path) -> Path:
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
    return configuration


def grid_flow_persisted_request() -> dict[str, object]:
    request = grid_flow_request()
    request["source"] = {
        "provider": "home-assistant",
        "entity_id": "grid_flow",
    }
    return request


def test_grid_flow_provider_data_is_persisted_and_retrieved_after_restart(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )

    with TestClient(app) as client:
        write_response = client.post(
            "/api/v1/grid-flow", json=grid_flow_persisted_request()
        )
        read_response = client.get("/api/v1/grid-flow")

    assert write_response.status_code == 200
    assert read_response.status_code == 200
    assert read_response.json() == write_response.json()

    with TestClient(app) as restarted_client:
        restarted_response = restarted_client.get("/api/v1/grid-flow")

    assert restarted_response.status_code == 200
    assert restarted_response.json() == read_response.json()


def test_grid_flow_direct_submission_is_not_persisted(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )
    request = grid_flow_request()
    request.pop("source")

    with TestClient(app) as client:
        write_response = client.post("/api/v1/grid-flow", json=request)
        read_response = client.get("/api/v1/grid-flow")

    assert write_response.status_code == 200
    assert read_response.status_code == 404


def test_household_load_provider_data_is_persisted_and_retrieved_after_restart(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )

    with TestClient(app) as client:
        write_response = client.post(
            "/api/v1/household-load", json=household_load_request()
        )
        read_response = client.get("/api/v1/household-load")

    assert write_response.status_code == 200
    assert read_response.status_code == 200
    assert read_response.json() == {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "household_load",
        },
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
    }

    with TestClient(app) as restarted_client:
        restarted_response = restarted_client.get("/api/v1/household-load")

    assert restarted_response.status_code == 200
    assert restarted_response.json() == read_response.json()


def test_household_load_provider_data_is_merged_on_persistence(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )
    first = household_load_request()
    second = household_load_request()
    second.update(
        {
            "start_time": "2026-01-01T01:00:00+00:00",
            "load_kw": [9.0, 3.0],
            "retrieved_at": "2026-01-01T02:00:00+00:00",
            "latest_observation_at": "2026-01-01T02:00:00+00:00",
        }
    )

    with TestClient(app) as client:
        first_response = client.post("/api/v1/household-load", json=first)
        second_response = client.post("/api/v1/household-load", json=second)
        read_response = client.get("/api/v1/household-load")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert read_response.status_code == 200
    assert second_response.json()["start_time"] == "2026-01-01T00:00:00Z"
    assert second_response.json()["load_kw"] == [1.2, 9.0, 3.0]
    assert read_response.json() == second_response.json()


def test_historic_household_load_returns_requested_range_and_metadata(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )
    request = household_load_request()

    with TestClient(app) as client:
        client.post("/api/v1/household-load", json=request)
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-01T01:00:00+00:00",
                "end_time": "2026-01-01T02:00:00+00:00",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "data_type": "household_load",
        "schema_version": "1",
        "start_time": "2026-01-01T01:00:00Z",
        "end_time": "2026-01-01T02:00:00Z",
        "interval_minutes": 60,
        "timestamps": ["2026-01-01T01:00:00Z"],
        "load_kw": [1.0],
        "quality": [{"status": "valid", "reason": None, "entity_id": None}],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "household_load"},
        "coverage_start_time": "2026-01-01T01:00:00Z",
        "coverage_end_time": "2026-01-01T02:00:00Z",
        "available_start_time": "2026-01-01T00:00:00Z",
        "available_end_time": "2026-01-01T02:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
        "validation_status": "valid",
        "freshness": "unknown",
        "freshness_checked_at": response.json()["freshness_checked_at"],
    }


def test_historic_household_load_reports_empty_range(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )

    with TestClient(app) as client:
        client.post("/api/v1/household-load", json=household_load_request())
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-02T00:00:00+00:00",
                "end_time": "2026-01-02T01:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["timestamps"] == []
    assert body["load_kw"] == []
    assert body["coverage_start_time"] is None
    assert body["coverage_end_time"] is None


def test_historic_household_load_reports_stale_but_valid_history(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    configuration = persistence_configuration(tmp_path)
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "  timeout_seconds: 10", "  timeout_seconds: 10\n  max_data_age_seconds: 1"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))

    with TestClient(app) as client:
        client.post("/api/v1/household-load", json=household_load_request())
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    assert response.json()["validation_status"] == "valid"


def test_historic_household_load_reports_corrupt_persistence(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )
    store = tmp_path / "provider-data"
    store.mkdir()
    key = "household-load-"
    provider_key = ProviderDataKey("household-load", "home-assistant", "household_load")
    (store / f"{key}{provider_key.digest()}.ndjson").write_text(
        "invalid\n", encoding="utf-8"
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
            },
        )

    assert response.status_code == 503
    assert "could not recover" in response.json()["detail"]


def test_historic_household_load_rejects_invalid_ranges(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )

    with TestClient(app) as client:
        responses = [
            client.get(
                "/api/v1/historic/household-load",
                params={
                    "start_time": "2026-01-01T01:00:00+00:00",
                    "end_time": "2026-01-01T00:00:00+00:00",
                },
            ),
            client.get(
                "/api/v1/historic/household-load",
                params={
                    "start_time": "2026-01-01T00:00:00",
                    "end_time": "2026-01-01T01:00:00+00:00",
                },
            ),
        ]

    assert all(response.status_code == 422 for response in responses)


def test_household_load_direct_submission_is_not_persisted(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration(tmp_path))
    )
    request = household_load_request()
    request.pop("source")

    with TestClient(app) as client:
        write_response = client.post("/api/v1/household-load", json=request)
        read_response = client.get("/api/v1/household-load")

    assert write_response.status_code == 200
    assert read_response.status_code == 404


def test_household_load_persistence_reports_missing_configuration(
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
        response = client.get("/api/v1/household-load")

    assert response.status_code == 503
    assert "persistence is not configured" in response.text


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


def test_grid_flow_contract_accepts_a_valid_request(
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
        response = client.post("/api/v1/grid-flow", json=grid_flow_request())

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "import_kw": [1.2, 1.0],
        "export_kw": [0.0, 0.4],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.grid_import",
        },
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
    }


def test_grid_flow_contract_allows_direct_submissions_without_source(
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
    request = grid_flow_request()
    request.pop("source")

    with TestClient(app) as client:
        response = client.post("/api/v1/grid-flow", json=request)

    assert response.status_code == 200
    assert response.json()["source"] is None


def test_grid_flow_contract_rejects_invalid_payloads(
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
        ({**grid_flow_request(), "import_kw": []}, "import_kw"),
        ({**grid_flow_request(), "import_kw": [-0.1]}, "import_kw"),
        ({**grid_flow_request(), "import_kw": [1000.1]}, "import_kw"),
        ({**grid_flow_request(), "export_kw": [1000.1]}, "export_kw"),
        ({**grid_flow_request(), "interval_minutes": 30}, "interval_minutes"),
        (
            {**grid_flow_request(), "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**grid_flow_request(), "schema_version": "2"}, "schema_version"),
        ({**grid_flow_request(), "unit": "W"}, "unit"),
        ({**grid_flow_request(), "unexpected": True}, "unexpected"),
        ({**grid_flow_request(), "source": {"provider": ""}}, "provider"),
        (
            {
                **grid_flow_request(),
                "source": {"provider": "home-assistant", "unexpected": True},
            },
            "extra",
        ),
        ({**grid_flow_request(), "export_kw": [0.0]}, "same number"),
    ]

    with TestClient(app) as client:
        responses = [
            (client.post("/api/v1/grid-flow", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_grid_flow_contract_rejects_non_finite_values(
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
                "/api/v1/grid-flow",
                content=(
                    '{"schema_version":"1",'
                    '"start_time":"2026-01-01T00:00:00+00:00",'
                    '"interval_minutes":60,"import_kw":[0.0,'
                    f'{value},1.0],"export_kw":[0.0,0.0,0.0],"unit":"kW",'
                    '"retrieved_at":"2026-01-01T00:00:00+00:00",'
                    '"latest_observation_at":"2026-01-01T01:00:00+00:00"}'
                ),
                headers={"content-type": "application/json"},
            )
            for value in ("NaN", "Infinity", "-Infinity")
        ]

    assert all(response.status_code == 422 for response in responses)
    assert all("import_kw" in response.text for response in responses)


def test_grid_flow_contract_rejects_more_than_ten_years(
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
    request = grid_flow_request()
    request["import_kw"] = [1.0] * (MAX_HORIZON_HOURS + 1)
    request["export_kw"] = [0.0] * (MAX_HORIZON_HOURS + 1)

    with TestClient(app) as client:
        response = client.post("/api/v1/grid-flow", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


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
            "entity_id": "household_load",
        },
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
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


def test_household_load_contract_returns_observation_metadata(
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
    assert response.json()["retrieved_at"] == "2026-01-01T00:00:00Z"
    assert response.json()["latest_observation_at"] == "2026-01-01T01:00:00Z"


def test_household_load_contract_rejects_naive_observation_timestamp(
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
        (
            {
                **household_load_request(),
                "latest_observation_at": "2026-01-01T01:00:00",
            },
            "timezone",
        ),
    ]

    with TestClient(app) as client:
        responses = [
            (client.post("/api/v1/household-load", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


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
                    '],"unit":"kW",'
                    '"retrieved_at":"2026-01-01T00:00:00+00:00",'
                    '"latest_observation_at":"2026-01-01T01:00:00+00:00"}'
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


def test_pv_generation_contract_accepts_a_valid_request(
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
        response = client.post("/api/v1/pv-generation", json=pv_generation_request())

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "generation_kw": [0.0, 2.4],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.pv_generation",
        },
    }


def test_pv_generation_contract_rejects_invalid_payloads(
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
        ({**pv_generation_request(), "generation_kw": []}, "generation_kw"),
        ({**pv_generation_request(), "generation_kw": [-0.1]}, "generation_kw"),
        ({**pv_generation_request(), "generation_kw": [1000.1]}, "generation_kw"),
        ({**pv_generation_request(), "interval_minutes": 30}, "interval_minutes"),
        (
            {**pv_generation_request(), "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**pv_generation_request(), "schema_version": "2"}, "schema_version"),
        ({**pv_generation_request(), "unit": "W"}, "unit"),
        ({**pv_generation_request(), "unexpected": True}, "unexpected"),
    ]

    with TestClient(app) as client:
        responses = [
            (client.post("/api/v1/pv-generation", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_pv_generation_contract_rejects_non_finite_values(
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
                "/api/v1/pv-generation",
                content=(
                    '{"schema_version":"1",'
                    '"start_time":"2026-01-01T00:00:00+00:00",'
                    '"interval_minutes":60,"generation_kw":[0.0,'
                    f"{value}"
                    '],"unit":"kW"}'
                ),
                headers={"content-type": "application/json"},
            )
            for value in ("NaN", "Infinity", "-Infinity")
        ]

    assert all(response.status_code == 422 for response in responses)
    assert all("generation_kw" in response.text for response in responses)


def test_pv_generation_contract_rejects_more_than_ten_years(
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
    request = pv_generation_request()
    request["generation_kw"] = [1.0] * (MAX_HORIZON_HOURS + 1)

    with TestClient(app) as client:
        response = client.post("/api/v1/pv-generation", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text
