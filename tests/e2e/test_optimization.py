"""Schedule optimization through the running service and the real solver."""

import httpx
import pytest

pytestmark = pytest.mark.e2e

DAY_AHEAD_REQUEST = {
    "start_time": "2026-01-01T00:00:00+00:00",
    "interval_minutes": 60,
    "load_kw": [1.0, 2.0, 3.0, 4.0],
    "pv_generation_kw": [0.0, 0.0, 0.0, 0.0],
    "import_price_eur_per_kwh": [0.30, 0.10, 0.20, 0.50],
    "export_price_eur_per_kwh": [0.05, 0.05, 0.05, 0.05],
}


def test_the_running_service_returns_an_optimal_schedule(e2e_api: httpx.Client) -> None:
    response = e2e_api.post("/optimize", json=DAY_AHEAD_REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "optimal"
    assert body["objective_eur"] == pytest.approx(3.1)
    assert [hour["grid_import_kw"] for hour in body["schedule"]] == pytest.approx(
        DAY_AHEAD_REQUEST["load_kw"]
    )
    assert sum(hour["import_cost_eur"] for hour in body["schedule"]) == pytest.approx(
        body["objective_eur"]
    )


def test_the_running_service_reports_a_load_above_the_grid_limit(
    e2e_api: httpx.Client,
) -> None:
    overloaded = {**DAY_AHEAD_REQUEST, "load_kw": [1.0, 2.0, 10.5, 4.0]}

    response = e2e_api.post("/optimize", json=overloaded)

    assert response.status_code == 200
    assert response.json()["status"] == "infeasible"
    assert response.json()["schedule"] == []
