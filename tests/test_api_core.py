"""Core HTTP API tests."""

import pytest
from fastapi.testclient import TestClient


def test_health_returns_service_status_and_version(client: TestClient) -> None:
    with client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def planned_hour(**values: object) -> dict[str, object]:
    return {
        key: pytest.approx(value) if isinstance(value, float) else value
        for key, value in values.items()
    }


def test_optimize_returns_the_cheapest_schedule_for_a_feasible_request(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    with client:
        response = client.post("/optimize", json=valid_request)

    assert response.status_code == 200
    assert response.json() == {
        "status": "optimal",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "hours": 2,
        "objective_eur": pytest.approx(0.51),
        "schedule": [
            planned_hour(
                start_time="2026-01-01T00:00:00Z",
                load_kw=1.2,
                pv_generation_kw=0.0,
                pv_self_consumption_kw=0.0,
                pv_curtailment_kw=0.0,
                grid_import_kw=1.2,
                grid_export_kw=0.0,
                import_cost_eur=0.36,
                export_revenue_eur=0.0,
            ),
            planned_hour(
                start_time="2026-01-01T01:00:00Z",
                load_kw=1.0,
                pv_generation_kw=0.4,
                pv_self_consumption_kw=0.4,
                pv_curtailment_kw=0.0,
                grid_import_kw=0.6,
                grid_export_kw=0.0,
                import_cost_eur=0.15,
                export_revenue_eur=0.0,
            ),
        ],
    }


def test_optimize_exports_the_pv_surplus_and_credits_the_revenue(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    with client:
        response = client.post(
            "/optimize", json={**valid_request, "pv_generation_kw": [0.0, 3.0]}
        )

    body = response.json()
    assert body["status"] == "optimal"
    assert body["objective_eur"] == pytest.approx(0.20)
    assert body["schedule"][1]["grid_export_kw"] == pytest.approx(2.0)
    assert body["schedule"][1]["export_revenue_eur"] == pytest.approx(0.16)


def test_optimize_reports_a_load_above_the_import_limit_as_infeasible(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    with client:
        response = client.post(
            "/optimize", json={**valid_request, "load_kw": [1.2, 10.5]}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "infeasible"
    assert body["objective_eur"] is None
    assert body["schedule"] == []


@pytest.mark.parametrize(
    ("override", "expected_text"),
    [
        ({"pv_generation_kw": [0.0]}, "time-series lengths must match"),
        ({"interval_minutes": 30}, "interval_minutes"),
        ({"start_time": "2026-01-01T00:00:00"}, "timezone"),
    ],
    ids=["mismatched-series-lengths", "non-hourly-interval", "naive-start-time"],
)
def test_optimize_rejects_invalid_hourly_inputs(
    client: TestClient,
    valid_request: dict[str, object],
    override: dict[str, object],
    expected_text: str,
) -> None:
    with client:
        response = client.post("/optimize", json={**valid_request, **override})

    assert response.status_code == 422
    assert expected_text in response.text
