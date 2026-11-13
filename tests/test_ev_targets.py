"""Dated owner requirements: persistence, HTTP validation and real solver behavior."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from energy_optimizer.api import app
from energy_optimizer.ev_targets import (
    EvTargetInput,
    active_targets,
    add_target,
    remove_target,
    target_hour,
)
from energy_optimizer.optimization.plan_generation import (
    PlanGenerationError,
    PlanGenerator,
)
from energy_optimizer.orchestration import ProviderDataSnapshot
from energy_optimizer.storage import ProviderDataStore
from energy_optimizer.storage_errors import ProviderDataStoreError
from test_ev_planning import configuration, ev_responses, importer
from test_orchestration import START, data, orchestrator_for
from test_plan_generation import (
    FIRST_PLANNED_HOUR,
    NOW,
    flat_history,
    prices,
    pv_forecast,
    snapshot,
)


def test_targets_persist_sort_expire_and_can_be_removed(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    later = add_target(
        store,
        EvTargetInput(deadline=NOW + timedelta(days=2), minimum_soc_percent=70),
        NOW,
    )
    earlier = add_target(
        store,
        EvTargetInput(deadline=NOW + timedelta(days=1), minimum_soc_percent=60),
        NOW,
    )
    restored = ProviderDataStore(tmp_path)
    assert active_targets(restored, NOW) == (earlier, later)
    assert active_targets(restored, earlier.deadline) == (later,)
    assert remove_target(restored, later.id, NOW)
    assert not remove_target(restored, uuid4(), NOW)


@pytest.mark.parametrize("value", [-1, 101, float("nan"), float("inf")])
def test_invalid_percent_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        EvTargetInput(deadline=NOW, minimum_soc_percent=value)


def test_partial_hour_deadlines_use_the_preceding_boundary() -> None:
    assert (
        target_hour(
            FIRST_PLANNED_HOUR + timedelta(hours=2, minutes=30), FIRST_PLANNED_HOUR, 4
        )
        == 2
    )
    assert (
        target_hour(FIRST_PLANNED_HOUR + timedelta(hours=5), FIRST_PLANNED_HOUR, 4)
        is None
    )


def test_replanning_uses_fresh_existing_data_without_fetching(tmp_path: Path) -> None:
    fetched: list[datetime] = []
    snapshots: list[ProviderDataSnapshot] = []

    def fetch(now: datetime, _: object) -> object:
        fetched.append(now)
        return data(now)

    runtime = orchestrator_for(
        tmp_path,
        fetch=fetch,
        plan_generator=snapshots.append,
        optimization_enabled=True,
    )
    runtime.run_due(START)
    assert runtime.replan(START + timedelta(seconds=10))[0] == "created"
    assert fetched == [START]
    assert len(snapshots) == 2
    assert runtime.replan(START + timedelta(hours=2))[0] == "not-ready"


@pytest.mark.parametrize("expired", [False, True])
def test_an_expired_or_out_of_horizon_target_does_not_change_the_problem(
    tmp_path: Path,
    expired: bool,
) -> None:
    store = ProviderDataStore(tmp_path)
    add_target(
        store,
        EvTargetInput(deadline=NOW + timedelta(days=7), minimum_soc_percent=100),
        NOW,
    )
    generator = PlanGenerator(configuration(), store)
    inputs = snapshot(
        household_load=flat_history(1),
        pv_generation=pv_forecast([0.0] * 4),
        electricity_prices=prices([0.1] * 4),
        ev=importer(ev_responses("50")).fetch(now=NOW),
    )
    if expired:
        inputs = ProviderDataSnapshot(
            captured_at=NOW + timedelta(days=8), data=inputs.data
        )
    assert generator.planning_problem(inputs).ev is None


def test_a_requirement_before_the_first_complete_hour_is_reported(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    add_target(
        store,
        EvTargetInput(
            deadline=FIRST_PLANNED_HOUR + timedelta(minutes=30), minimum_soc_percent=70
        ),
        NOW,
    )
    with pytest.raises(PlanGenerationError, match="first complete"):
        PlanGenerator(configuration(), store).planning_problem(
            snapshot(
                household_load=flat_history(1),
                pv_generation=pv_forecast([0.0] * 4),
                electricity_prices=prices([0.1] * 4),
                ev=importer(ev_responses("50")).fetch(now=NOW),
            )
        )


def test_the_real_solver_satisfies_multiple_dated_requirements(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    for hours, percent in ((2, 60), (4, 65)):
        add_target(
            store,
            EvTargetInput(
                deadline=FIRST_PLANNED_HOUR + timedelta(hours=hours),
                minimum_soc_percent=percent,
            ),
            NOW,
        )
    stored = PlanGenerator(configuration(), store)(
        snapshot(
            household_load=flat_history(1),
            pv_generation=pv_forecast([0.0] * 4),
            electricity_prices=prices([0.1, 0.2, 0.3, 0.4]),
            ev=importer(ev_responses("50")).fetch(now=NOW),
        )
    )
    assert stored.plan.has_schedule()
    assert stored.plan.hours[1].ev is not None
    assert stored.plan.hours[1].ev.state_of_charge_kwh >= 36 - 1e-6
    assert stored.plan.hours[3].ev is not None
    assert stored.plan.hours[3].ev.state_of_charge_kwh >= 39 - 1e-6


def test_an_unreachable_dated_requirement_is_explicitly_infeasible(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    add_target(
        store,
        EvTargetInput(
            deadline=FIRST_PLANNED_HOUR + timedelta(hours=1), minimum_soc_percent=100
        ),
        NOW,
    )
    with pytest.raises(PlanGenerationError, match="infeasible"):
        PlanGenerator(configuration(), store)(
            snapshot(
                household_load=flat_history(1),
                pv_generation=pv_forecast([0.0] * 4),
                electricity_prices=prices([0.1] * 4),
                ev=importer(ev_responses("50")).fetch(now=NOW),
            )
        )


@pytest.fixture
def target_client(minimal_configuration: Path) -> TestClient:
    with minimal_configuration.open("a", encoding="utf-8") as output:
        output.write(
            f"\npersistence:\n  directory: {minimal_configuration.parent / 'targets'}\n"
        )
        output.write(
            "home_assistant:\n  base_url: http://ha.test\n  token: test\n"
            "  timeout_seconds: 5\n  ev:\n"
            "    state_of_charge: {entity_id: sensor.car, unit: '%'}\n"
            "    plugged_in: {entity_id: binary_sensor.car}\n"
            "    capacity_kwh: 60\n    minimum_charge_kw: 1.4\n"
            "    maximum_charge_kw: 11\n"
        )
    return TestClient(app)


def test_api_add_list_delete_and_restart(target_client: TestClient) -> None:
    with target_client as client:
        response = client.post(
            "/api/v1/ev/targets",
            json={
                "deadline": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
                "minimum_soc_percent": 70,
            },
        )
        assert response.status_code == 201
        target_id = response.json()["targets"][0]["id"]
        assert response.json()["planning_status"] == "unavailable"
    with target_client as client:
        assert client.get("/api/v1/ev/targets").json()["targets"][0]["id"] == target_id
        assert client.delete(f"/api/v1/ev/targets/{target_id}").json()["targets"] == []
        assert client.delete(f"/api/v1/ev/targets/{target_id}").status_code == 404


@pytest.mark.parametrize("deadline", ["2020-01-01T08:00:00Z", "2030-01-01T08:00:00"])
def test_api_rejects_past_or_naive_deadlines(
    target_client: TestClient, deadline: str
) -> None:
    with target_client as client:
        assert (
            client.post(
                "/api/v1/ev/targets",
                json={"deadline": deadline, "minimum_soc_percent": 70},
            ).status_code
            == 422
        )


def test_api_reports_missing_ev(client: TestClient) -> None:
    with client:
        assert client.get("/api/v1/ev/targets").status_code == 503


def test_storage_failure_is_reported(
    target_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise ProviderDataStoreError("unreadable")

    with target_client as client:
        monkeypatch.setattr(ProviderDataStore, "load", fail)
        assert client.get("/api/v1/ev/targets").status_code == 503
