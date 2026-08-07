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
