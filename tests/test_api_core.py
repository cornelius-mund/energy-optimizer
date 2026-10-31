"""Core HTTP API tests."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from energy_optimizer.api.app import app
from energy_optimizer.optimization import (
    Plan,
    PlannedBatteryHour,
    PlannedHour,
    PlanStatus,
)
from energy_optimizer.optimization.plan_generation import (
    PLAN_ADAPTER,
    PLAN_KEY,
    PlanSnapshot,
)


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
                battery=None,
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
                battery=None,
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


BATTERY = {
    "capacity_kwh": 10.0,
    "initial_soc_percent": 50.0,
    "minimum_soc_percent": 0.0,
    "maximum_soc_percent": 100.0,
    "maximum_charge_kw": 3.0,
    "maximum_discharge_kw": 2.0,
    "charge_efficiency": 1.0,
    "discharge_efficiency": 1.0,
}


def test_optimize_schedules_the_battery_between_cheap_and_expensive_hours(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    request = {
        **valid_request,
        "load_kw": [2.0, 2.0],
        "pv_generation_kw": [0.0, 0.0],
        "import_price_eur_per_kwh": [0.1, 0.5],
        "battery": BATTERY,
    }

    with client:
        response = client.post("/optimize", json=request)

    body = response.json()
    assert body["status"] == "optimal"
    assert body["objective_eur"] == pytest.approx(0.4)
    charging_hour, discharging_hour = body["schedule"]
    assert charging_hour["battery"] == planned_hour(
        charge_kw=2.0,
        discharge_kw=0.0,
        state_of_charge_kwh=7.0,
        state_of_charge_percent=70.0,
    ) | {"discharge_kw": pytest.approx(0.0, abs=1e-6)}
    assert discharging_hour["battery"]["discharge_kw"] == pytest.approx(2.0)
    assert discharging_hour["battery"]["state_of_charge_percent"] == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("override", "expected_text"),
    [
        ({"initial_soc_percent": 95.0, "maximum_soc_percent": 90.0}, "initial"),
        ({"minimum_soc_percent": 60.0}, "initial"),
        ({"final_soc_percent": 100.0, "maximum_soc_percent": 80.0}, "final"),
        ({"minimum_soc_percent": 90.0, "maximum_soc_percent": 10.0}, "minimum_soc"),
        ({"charge_efficiency": 1.2}, "charge_efficiency"),
        ({"capacity_kwh": 0}, "capacity_kwh"),
    ],
    ids=[
        "initial-above-maximum",
        "initial-below-minimum",
        "final-above-maximum",
        "inverted-band",
        "efficiency-above-one",
        "zero-capacity",
    ],
)
def test_optimize_rejects_an_inconsistent_battery(
    client: TestClient,
    valid_request: dict[str, object],
    override: dict[str, float],
    expected_text: str,
) -> None:
    request = {**valid_request, "battery": {**BATTERY, **override}}

    with client:
        response = client.post("/optimize", json=request)

    assert response.status_code == 422
    assert expected_text in response.text


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


def stored_plan() -> PlanSnapshot:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return PlanSnapshot(
        created_at=start - timedelta(minutes=30),
        start_time=start,
        battery_capacity_kwh=10.0,
        plan=Plan(
            status=PlanStatus.OPTIMAL,
            objective_eur=0.3,
            hours=(
                PlannedHour(
                    start_time=start,
                    load_kw=1.0,
                    pv_generation_kw=0.0,
                    pv_self_consumption_kw=0.0,
                    pv_curtailment_kw=0.0,
                    grid_import_kw=1.0,
                    grid_export_kw=0.0,
                    import_cost_eur=0.3,
                    export_revenue_eur=0.0,
                    battery=PlannedBatteryHour(0.0, 0.0, 2.5),
                ),
            ),
        ),
    )


def test_plan_is_not_found_before_the_first_plan_exists(
    persistence_client: TestClient,
) -> None:
    with persistence_client:
        response = persistence_client.get("/plan")

    assert response.status_code == 404


def test_plan_requires_persistence(client: TestClient) -> None:
    with client:
        response = client.get("/plan")

    assert response.status_code == 503


def test_plan_returns_the_stored_plan_with_the_battery_level_in_percent(
    persistence_client: TestClient,
) -> None:
    with persistence_client:
        store = app.state.provider_data_store
        store.save(PLAN_KEY, PLAN_ADAPTER, stored_plan())

        response = persistence_client.get("/plan")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "optimal"
    assert body["created_at"] == "2025-12-31T23:30:00Z"
    assert body["objective_eur"] == pytest.approx(0.3)
    assert body["schedule"][0]["battery"]["state_of_charge_percent"] == pytest.approx(
        25.0
    )
