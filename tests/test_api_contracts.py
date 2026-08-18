"""Request and response contract tests for the HTTP API."""

import json

from fastapi.testclient import TestClient

from energy_optimizer.api import MAX_HORIZON_HOURS


def test_electricity_price_contract_accepts_a_valid_request(
    client: TestClient, electricity_price_request: dict[str, object]
) -> None:
    with client as test_client:
        response = test_client.post(
            "/api/v1/electricity-prices", json=electricity_price_request
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
    client: TestClient, electricity_price_request: dict[str, object]
) -> None:
    request = electricity_price_request.copy()
    request["import_price_eur_per_kwh"] = [-100.0, 100.0]
    request["export_price_eur_per_kwh"] = [-100.0, 100.0]

    with client as test_client:
        response = test_client.post("/api/v1/electricity-prices", json=request)

    assert response.status_code == 200


def test_electricity_price_contract_rejects_invalid_payloads(
    client: TestClient, electricity_price_request: dict[str, object]
) -> None:
    invalid_requests = [
        ({**electricity_price_request, "timestamps": []}, "timestamps"),
        (
            {
                **electricity_price_request,
                "timestamps": [
                    "2026-01-01T01:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ],
            },
            "ascending",
        ),
        (
            {
                **electricity_price_request,
                "timestamps": [
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ],
            },
            "ascending",
        ),
        (
            {
                **electricity_price_request,
                "timestamps": [
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T04:00:00+00:00",
                ],
            },
            "spaced",
        ),
        (
            {**electricity_price_request, "import_price_eur_per_kwh": [0.30]},
            "same length",
        ),
        (
            {
                **electricity_price_request,
                "export_price_eur_per_kwh": [100.1, 0.08],
            },
            "export_price_eur_per_kwh",
        ),
        (
            {
                **electricity_price_request,
                "import_price_eur_per_kwh": [-100.1, 0.25],
            },
            "import_price_eur_per_kwh",
        ),
        ({**electricity_price_request, "unit": "EUR/MWh"}, "unit"),
        (
            {**electricity_price_request, "timestamps": ["2026-01-01T00:00:00"]},
            "timestamps",
        ),
        (
            {
                **electricity_price_request,
                "retrieved_at": "2026-01-01T04:00:00+00:00",
            },
            "retrieved_at",
        ),
        (
            {
                **electricity_price_request,
                "expires_at": "2026-01-01T01:00:00+00:00",
            },
            "expires_at",
        ),
        ({**electricity_price_request, "unexpected": True}, "unexpected"),
    ]

    with client as test_client:
        responses = [
            (
                test_client.post("/api/v1/electricity-prices", json=request),
                expected_text,
            )
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_electricity_price_contract_rejects_non_finite_values(
    client: TestClient,
) -> None:
    with client as test_client:
        responses = [
            test_client.post(
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
    client: TestClient, electricity_price_request: dict[str, object]
) -> None:
    horizon = MAX_HORIZON_HOURS + 1
    request = electricity_price_request.copy()
    request["timestamps"] = ["2026-01-01T00:00:00+00:00"] * horizon
    request["import_price_eur_per_kwh"] = [0.30] * horizon
    request["export_price_eur_per_kwh"] = [0.08] * horizon

    with client as test_client:
        response = test_client.post("/api/v1/electricity-prices", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


def test_battery_contract_accepts_a_valid_request(
    client: TestClient, battery_request: dict[str, object]
) -> None:
    with client as test_client:
        response = test_client.post("/api/v1/battery", json=battery_request)

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
    client: TestClient, battery_request: dict[str, object]
) -> None:
    request = battery_request.copy()
    request.pop("source")

    with client as test_client:
        response = test_client.post("/api/v1/battery", json=request)

    assert response.status_code == 200
    assert response.json()["source"] is None


def test_battery_contract_rejects_invalid_payloads(
    client: TestClient, battery_request: dict[str, object]
) -> None:
    invalid_requests = [
        ({**battery_request, "state_of_charge_kwh": []}, "state_of_charge_kwh"),
        ({**battery_request, "state_of_charge_kwh": [11.0]}, "state_of_charge_kwh"),
        ({**battery_request, "minimum_soc_kwh": 11.0}, "minimum_soc_kwh"),
        ({**battery_request, "maximum_soc_kwh": 100001.0}, "maximum_soc_kwh"),
        ({**battery_request, "maximum_soc_kwh": 1.0}, "minimum_soc_kwh"),
        ({**battery_request, "initial_soc_kwh": 1.0}, "initial_soc_kwh"),
        ({**battery_request, "maximum_charge_kw": 0.0}, "maximum_charge_kw"),
        ({**battery_request, "charge_efficiency": 0.0}, "charge_efficiency"),
        ({**battery_request, "interval_minutes": 30}, "interval_minutes"),
        (
            {**battery_request, "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**battery_request, "schema_version": "2"}, "schema_version"),
        ({**battery_request, "unit": "kW"}, "unit"),
        ({**battery_request, "unexpected": True}, "unexpected"),
    ]

    with client as test_client:
        responses = [
            (test_client.post("/api/v1/battery", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_battery_contract_rejects_non_finite_values(
    client: TestClient, battery_request: dict[str, object]
) -> None:
    non_finite_values = (float("nan"), float("inf"), float("-inf"))
    requests = []
    for value in non_finite_values:
        request = battery_request.copy()
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
            request = battery_request.copy()
            request[field] = value
            requests.append((request, field))

    with client as test_client:
        responses = [
            (
                test_client.post(
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
    client: TestClient, battery_request: dict[str, object]
) -> None:
    request = battery_request.copy()
    request["state_of_charge_kwh"] = [5.0] * (MAX_HORIZON_HOURS + 1)

    with client as test_client:
        response = test_client.post("/api/v1/battery", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


def test_grid_flow_contract_accepts_a_valid_request(
    client: TestClient, grid_flow_request: dict[str, object]
) -> None:
    with client as test_client:
        response = test_client.post("/api/v1/grid-flow", json=grid_flow_request)

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
    client: TestClient, grid_flow_request: dict[str, object]
) -> None:
    request = grid_flow_request.copy()
    request.pop("source")

    with client as test_client:
        response = test_client.post("/api/v1/grid-flow", json=request)

    assert response.status_code == 200
    assert response.json()["source"] is None


def test_grid_flow_contract_rejects_invalid_payloads(
    client: TestClient, grid_flow_request: dict[str, object]
) -> None:
    invalid_requests = [
        ({**grid_flow_request, "import_kw": []}, "import_kw"),
        ({**grid_flow_request, "import_kw": [-0.1]}, "import_kw"),
        ({**grid_flow_request, "import_kw": [1000.1]}, "import_kw"),
        ({**grid_flow_request, "export_kw": [1000.1]}, "export_kw"),
        ({**grid_flow_request, "interval_minutes": 30}, "interval_minutes"),
        (
            {**grid_flow_request, "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**grid_flow_request, "schema_version": "2"}, "schema_version"),
        ({**grid_flow_request, "unit": "W"}, "unit"),
        ({**grid_flow_request, "unexpected": True}, "unexpected"),
        ({**grid_flow_request, "source": {"provider": ""}}, "provider"),
        (
            {
                **grid_flow_request,
                "source": {"provider": "home-assistant", "unexpected": True},
            },
            "extra",
        ),
        ({**grid_flow_request, "export_kw": [0.0]}, "same number"),
    ]

    with client as test_client:
        responses = [
            (test_client.post("/api/v1/grid-flow", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_grid_flow_contract_rejects_non_finite_values(client: TestClient) -> None:
    with client as test_client:
        responses = [
            test_client.post(
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
    client: TestClient, grid_flow_request: dict[str, object]
) -> None:
    horizon = MAX_HORIZON_HOURS + 1
    request = grid_flow_request.copy()
    request["import_kw"] = [1.0] * horizon
    request["export_kw"] = [0.0] * horizon

    with client as test_client:
        response = test_client.post("/api/v1/grid-flow", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


def test_household_load_contract_accepts_a_valid_request(
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    with client as test_client:
        response = test_client.post(
            "/api/v1/household-load", json=household_load_request
        )

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
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    request = household_load_request.copy()
    request.pop("source")

    with client as test_client:
        response = test_client.post("/api/v1/household-load", json=request)

    assert response.status_code == 200
    assert response.json()["source"] is None


def test_household_load_contract_returns_observation_metadata(
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    with client as test_client:
        response = test_client.post(
            "/api/v1/household-load", json=household_load_request
        )

    assert response.status_code == 200
    assert response.json()["retrieved_at"] == "2026-01-01T00:00:00Z"
    assert response.json()["latest_observation_at"] == "2026-01-01T01:00:00Z"


def test_household_load_contract_rejects_naive_observation_timestamp(
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    request = household_load_request.copy()
    request["latest_observation_at"] = "2026-01-01T01:00:00"

    with client as test_client:
        response = test_client.post("/api/v1/household-load", json=request)

    assert response.status_code == 422
    assert "timezone" in response.text


def test_household_load_contract_rejects_invalid_payloads(
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    invalid_requests = [
        ({**household_load_request, "load_kw": []}, "load_kw"),
        ({**household_load_request, "load_kw": [-0.1]}, "load_kw"),
        ({**household_load_request, "load_kw": [1000.1]}, "load_kw"),
        ({**household_load_request, "interval_minutes": 30}, "interval_minutes"),
        (
            {**household_load_request, "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**household_load_request, "schema_version": "2"}, "schema_version"),
        ({**household_load_request, "unit": "W"}, "unit"),
        ({**household_load_request, "unexpected": True}, "unexpected"),
    ]

    with client as test_client:
        responses = [
            (test_client.post("/api/v1/household-load", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_household_load_contract_rejects_non_finite_values(client: TestClient) -> None:
    with client as test_client:
        responses = [
            test_client.post(
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
    client: TestClient, household_load_request: dict[str, object]
) -> None:
    request = household_load_request.copy()
    request["load_kw"] = [1.0] * (MAX_HORIZON_HOURS + 1)

    with client as test_client:
        response = test_client.post("/api/v1/household-load", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text


def test_pv_generation_contract_accepts_a_valid_request(
    client: TestClient, pv_generation_request: dict[str, object]
) -> None:
    with client as test_client:
        response = test_client.post("/api/v1/pv-generation", json=pv_generation_request)

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
    client: TestClient, pv_generation_request: dict[str, object]
) -> None:
    invalid_requests = [
        ({**pv_generation_request, "generation_kw": []}, "generation_kw"),
        ({**pv_generation_request, "generation_kw": [-0.1]}, "generation_kw"),
        ({**pv_generation_request, "generation_kw": [1000.1]}, "generation_kw"),
        ({**pv_generation_request, "interval_minutes": 30}, "interval_minutes"),
        (
            {**pv_generation_request, "start_time": "2026-01-01T00:00:00"},
            "start_time",
        ),
        ({**pv_generation_request, "schema_version": "2"}, "schema_version"),
        ({**pv_generation_request, "unit": "W"}, "unit"),
        ({**pv_generation_request, "unexpected": True}, "unexpected"),
    ]

    with client as test_client:
        responses = [
            (test_client.post("/api/v1/pv-generation", json=request), expected_text)
            for request, expected_text in invalid_requests
        ]

    assert all(response.status_code == 422 for response, _ in responses)
    assert all(expected_text in response.text for response, expected_text in responses)


def test_pv_generation_contract_rejects_non_finite_values(client: TestClient) -> None:
    with client as test_client:
        responses = [
            test_client.post(
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
    client: TestClient, pv_generation_request: dict[str, object]
) -> None:
    request = pv_generation_request.copy()
    request["generation_kw"] = [1.0] * (MAX_HORIZON_HOURS + 1)

    with client as test_client:
        response = test_client.post("/api/v1/pv-generation", json=request)

    assert response.status_code == 422
    assert str(MAX_HORIZON_HOURS) in response.text
