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


def test_the_running_service_exports_pv_surplus_and_curtails_it_at_negative_prices(
    e2e_api: httpx.Client,
) -> None:
    sunny_day = {
        **DAY_AHEAD_REQUEST,
        "load_kw": [2.0, 2.0],
        "pv_generation_kw": [3.0, 3.0],
        "import_price_eur_per_kwh": [0.30, 0.30],
        "export_price_eur_per_kwh": [0.10, -0.05],
    }

    response = e2e_api.post("/optimize", json=sunny_day)

    body = response.json()
    assert body["status"] == "optimal"
    exporting_hour, curtailing_hour = body["schedule"]
    assert exporting_hour["grid_export_kw"] == pytest.approx(1.0)
    assert exporting_hour["pv_curtailment_kw"] == pytest.approx(0.0, abs=1e-6)
    assert curtailing_hour["grid_export_kw"] == pytest.approx(0.0, abs=1e-6)
    assert curtailing_hour["pv_curtailment_kw"] == pytest.approx(1.0)
    assert body["objective_eur"] == pytest.approx(-0.10)


def test_the_running_service_schedules_the_battery_and_keeps_its_state_of_charge(
    e2e_api: httpx.Client,
) -> None:
    request = {
        **DAY_AHEAD_REQUEST,
        "load_kw": [2.0, 2.0],
        "pv_generation_kw": [0.0, 0.0],
        "import_price_eur_per_kwh": [0.10, 0.50],
        "export_price_eur_per_kwh": [0.05, 0.05],
        "battery": {
            "capacity_kwh": 10.0,
            "initial_soc_percent": 50.0,
            "minimum_soc_percent": 10.0,
            "maximum_soc_percent": 90.0,
            "maximum_charge_kw": 3.0,
            "maximum_discharge_kw": 2.0,
            "charge_efficiency": 0.9,
            "discharge_efficiency": 0.8,
        },
    }

    response = e2e_api.post("/optimize", json=request)

    body = response.json()
    assert body["status"] == "optimal"
    charging_hour, discharging_hour = (hour["battery"] for hour in body["schedule"])
    assert charging_hour["charge_kw"] == pytest.approx(2.5 / 0.9)
    assert charging_hour["state_of_charge_kwh"] == pytest.approx(7.5)
    assert discharging_hour["discharge_kw"] == pytest.approx(2.0)
    assert discharging_hour["state_of_charge_percent"] == pytest.approx(50.0)
    assert body["objective_eur"] == pytest.approx((2.0 + 2.5 / 0.9) * 0.10)


def test_the_running_service_reports_a_load_above_the_grid_limit(
    e2e_api: httpx.Client,
) -> None:
    overloaded = {**DAY_AHEAD_REQUEST, "load_kw": [1.0, 2.0, 10.5, 4.0]}

    response = e2e_api.post("/optimize", json=overloaded)

    assert response.status_code == 200
    assert response.json()["status"] == "infeasible"
    assert response.json()["schedule"] == []
