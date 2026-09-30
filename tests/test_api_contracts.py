"""Request and response contract tests for the HTTP API."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from energy_optimizer.api import MAX_HORIZON_HOURS

REQUEST_FIXTURES = {
    "electricity-prices": "electricity_price_request",
    "battery": "battery_request",
    "grid-flow": "grid_flow_request",
    "household-load": "household_load_request",
    "pv-generation": "pv_generation_request",
}
ABSENT = object()
NON_FINITE = "<non-finite>"
HORIZON = MAX_HORIZON_HOURS + 1
MIDNIGHT = "2026-01-01T00:00:00+00:00"
ONE_AM = "2026-01-01T01:00:00+00:00"
FOUR_AM = "2026-01-01T04:00:00+00:00"

VALID_RESPONSES: dict[str, dict[str, object]] = {
    "electricity-prices": {
        "status": "validated",
        "schema_version": "1",
        "timestamps": ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"],
        "interval_minutes": 60,
        "import_price_eur_per_kwh": [0.30, 0.25],
        "export_price_eur_per_kwh": [0.08, 0.08],
        "unit": "EUR/kWh",
        "source": {"provider": "day-ahead-market", "entity_id": None},
        "retrieved_at": "2025-12-31T23:00:00Z",
        "expires_at": "2026-01-01T03:00:00Z",
    },
    "battery": {
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
        "battery_efficiency": 0.9,
        "unit": "kWh",
        "power_unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "sensor.battery_soc"},
    },
    "grid-flow": {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "import_kw": [1.2, 1.0],
        "export_kw": [0.0, 0.4],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "sensor.grid_import"},
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
    },
    "household-load": {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "household_load"},
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
    },
    "pv-generation": {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "generation_kw": [0.0, 2.4],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "sensor.pv_generation"},
    },
}

# Each case overrides fields of the valid request and names the text that the
# 422 response must mention.
INVALID_PAYLOADS: dict[str, list[tuple[dict[str, Any], str]]] = {
    "electricity-prices": [
        ({"timestamps": []}, "timestamps"),
        ({"timestamps": [ONE_AM, MIDNIGHT]}, "ascending"),
        ({"timestamps": [MIDNIGHT, MIDNIGHT]}, "ascending"),
        ({"timestamps": [MIDNIGHT, FOUR_AM]}, "spaced"),
        ({"import_price_eur_per_kwh": [0.30]}, "same length"),
        ({"export_price_eur_per_kwh": [100.1, 0.08]}, "export_price_eur_per_kwh"),
        ({"import_price_eur_per_kwh": [-100.1, 0.25]}, "import_price_eur_per_kwh"),
        ({"unit": "EUR/MWh"}, "unit"),
        ({"timestamps": ["2026-01-01T00:00:00"]}, "timestamps"),
        ({"retrieved_at": FOUR_AM}, "retrieved_at"),
        ({"expires_at": ONE_AM}, "expires_at"),
        ({"unexpected": True}, "unexpected"),
    ],
    "battery": [
        ({"state_of_charge_kwh": []}, "state_of_charge_kwh"),
        ({"state_of_charge_kwh": [11.0]}, "state_of_charge_kwh"),
        ({"minimum_soc_kwh": 11.0}, "minimum_soc_kwh"),
        ({"maximum_soc_kwh": 100001.0}, "maximum_soc_kwh"),
        ({"maximum_soc_kwh": 1.0}, "minimum_soc_kwh"),
        ({"initial_soc_kwh": 1.0}, "initial_soc_kwh"),
        ({"maximum_charge_kw": 0.0}, "maximum_charge_kw"),
        ({"battery_efficiency": 0.0}, "battery_efficiency"),
        ({"interval_minutes": 30}, "interval_minutes"),
        ({"start_time": "2026-01-01T00:00:00"}, "start_time"),
        ({"schema_version": "2"}, "schema_version"),
        ({"unit": "kW"}, "unit"),
        ({"unexpected": True}, "unexpected"),
    ],
    "grid-flow": [
        ({"import_kw": []}, "import_kw"),
        ({"import_kw": [-0.1]}, "import_kw"),
        ({"import_kw": [1000.1]}, "import_kw"),
        ({"export_kw": [1000.1]}, "export_kw"),
        ({"interval_minutes": 30}, "interval_minutes"),
        ({"start_time": "2026-01-01T00:00:00"}, "start_time"),
        ({"schema_version": "2"}, "schema_version"),
        ({"unit": "W"}, "unit"),
        ({"unexpected": True}, "unexpected"),
        ({"source": {"provider": ""}}, "provider"),
        ({"source": {"provider": "home-assistant", "unexpected": True}}, "extra"),
        ({"export_kw": [0.0]}, "same number"),
    ],
    "household-load": [
        ({"load_kw": []}, "load_kw"),
        ({"load_kw": [-0.1]}, "load_kw"),
        ({"load_kw": [1000.1]}, "load_kw"),
        ({"interval_minutes": 30}, "interval_minutes"),
        ({"start_time": "2026-01-01T00:00:00"}, "start_time"),
        ({"schema_version": "2"}, "schema_version"),
        ({"unit": "W"}, "unit"),
        ({"unexpected": True}, "unexpected"),
    ],
    "pv-generation": [
        ({"generation_kw": []}, "generation_kw"),
        ({"generation_kw": [-0.1]}, "generation_kw"),
        ({"generation_kw": [1000.1]}, "generation_kw"),
        ({"interval_minutes": 30}, "interval_minutes"),
        ({"start_time": "2026-01-01T00:00:00"}, "start_time"),
        ({"schema_version": "2"}, "schema_version"),
        ({"unit": "W"}, "unit"),
        ({"unexpected": True}, "unexpected"),
    ],
}

# Each case overrides fields of the valid request, placing NON_FINITE where
# JSON's non-standard numeric literals are substituted, and names the text that
# the 422 response must mention. ABSENT removes a field from the request.
NON_FINITE_PAYLOADS: dict[str, list[tuple[dict[str, Any], str]]] = {
    "electricity-prices": [
        (
            {"timestamps": [MIDNIGHT], "import_price_eur_per_kwh": [0.1, NON_FINITE]},
            "import_price_eur_per_kwh",
        )
    ],
    "battery": [
        ({"state_of_charge_kwh": [5.0, NON_FINITE]}, "state_of_charge_kwh"),
        *(
            ({field: NON_FINITE}, field)
            for field in (
                "capacity_kwh",
                "minimum_soc_kwh",
                "maximum_soc_kwh",
                "initial_soc_kwh",
                "maximum_charge_kw",
                "maximum_discharge_kw",
                "battery_efficiency",
            )
        ),
    ],
    "grid-flow": [
        (
            {
                "source": ABSENT,
                "import_kw": [0.0, NON_FINITE, 1.0],
                "export_kw": [0.0, 0.0, 0.0],
            },
            "import_kw",
        )
    ],
    "household-load": [({"source": ABSENT, "load_kw": [0.0, NON_FINITE]}, "load_kw")],
    "pv-generation": [
        ({"source": ABSENT, "generation_kw": [0.0, NON_FINITE]}, "generation_kw")
    ],
}

OVER_HORIZON_PAYLOADS: dict[str, dict[str, Any]] = {
    "electricity-prices": {
        "timestamps": [MIDNIGHT] * HORIZON,
        "import_price_eur_per_kwh": [0.30] * HORIZON,
        "export_price_eur_per_kwh": [0.08] * HORIZON,
    },
    "battery": {"state_of_charge_kwh": [5.0] * HORIZON},
    "grid-flow": {"import_kw": [1.0] * HORIZON, "export_kw": [0.0] * HORIZON},
    "household-load": {"load_kw": [1.0] * HORIZON},
    "pv-generation": {"generation_kw": [1.0] * HORIZON},
}


@pytest.fixture
def payload(request: pytest.FixtureRequest, endpoint: str) -> dict[str, object]:
    """Provide the valid request body of the parametrized endpoint."""
    valid_request: dict[str, object] = request.getfixturevalue(
        REQUEST_FIXTURES[endpoint]
    )
    return valid_request


def with_overrides(payload: dict[str, object], overrides: dict[str, Any]) -> object:
    body = {**payload, **overrides}
    return {name: value for name, value in body.items() if value is not ABSENT}


@pytest.mark.parametrize("endpoint", VALID_RESPONSES)
def test_contract_accepts_a_valid_request(
    client: TestClient, endpoint: str, payload: dict[str, object]
) -> None:
    with client:
        response = client.post(f"/api/v1/{endpoint}", json=payload)

    assert response.status_code == 200
    assert response.json() == VALID_RESPONSES[endpoint]


def test_electricity_price_contract_accepts_negative_and_boundary_prices(
    client: TestClient, electricity_price_request: dict[str, object]
) -> None:
    request = electricity_price_request.copy()
    request["import_price_eur_per_kwh"] = [-100.0, 100.0]
    request["export_price_eur_per_kwh"] = [-100.0, 100.0]

    with client:
        response = client.post("/api/v1/electricity-prices", json=request)

    assert response.status_code == 200


@pytest.mark.parametrize("endpoint", ["battery", "grid-flow", "household-load"])
def test_contract_allows_direct_submissions_without_source(
    client: TestClient, endpoint: str, payload: dict[str, object]
) -> None:
    payload.pop("source")

    with client:
        response = client.post(f"/api/v1/{endpoint}", json=payload)

    assert response.status_code == 200
    assert response.json()["source"] is None


@pytest.mark.parametrize("endpoint", INVALID_PAYLOADS)
def test_contract_rejects_invalid_payloads(
    client: TestClient, endpoint: str, payload: dict[str, object]
) -> None:
    with client:
        for overrides, expected_text in INVALID_PAYLOADS[endpoint]:
            response = client.post(
                f"/api/v1/{endpoint}", json=with_overrides(payload, overrides)
            )

            assert response.status_code == 422, overrides
            assert expected_text in response.text, overrides


@pytest.mark.parametrize("endpoint", NON_FINITE_PAYLOADS)
def test_contract_rejects_non_finite_values(
    client: TestClient, endpoint: str, payload: dict[str, object]
) -> None:
    with client:
        for overrides, expected_text in NON_FINITE_PAYLOADS[endpoint]:
            for literal in ("NaN", "Infinity", "-Infinity"):
                body = json.dumps(with_overrides(payload, overrides))
                response = client.post(
                    f"/api/v1/{endpoint}",
                    content=body.replace(f'"{NON_FINITE}"', literal),
                    headers={"content-type": "application/json"},
                )

                assert response.status_code == 422, (overrides, literal)
                assert expected_text in response.text, (overrides, literal)


@pytest.mark.parametrize("endpoint", OVER_HORIZON_PAYLOADS)
def test_contract_rejects_more_than_ten_years(
    client: TestClient, endpoint: str, payload: dict[str, object]
) -> None:
    with client:
        response = client.post(
            f"/api/v1/{endpoint}",
            json=with_overrides(payload, OVER_HORIZON_PAYLOADS[endpoint]),
        )

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


def test_household_load_contract_returns_observation_metadata(
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    with client:
        response = client.post("/api/v1/household-load", json=household_load_request)

    assert response.status_code == 200
    assert response.json()["retrieved_at"] == "2026-01-01T00:00:00Z"
    assert response.json()["latest_observation_at"] == "2026-01-01T01:00:00Z"


def test_household_load_contract_rejects_naive_observation_timestamp(
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    request = household_load_request.copy()
    request["latest_observation_at"] = "2026-01-01T01:00:00"

    with client:
        response = client.post("/api/v1/household-load", json=request)

    assert response.status_code == 422
    assert "timezone" in response.text
