"""Owner availability overrides and their real-solver effects."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient

from energy_optimizer.api import app
from energy_optimizer.config import Configuration
from energy_optimizer.ev_presence import (
    EvPresenceInput,
    active_presence,
    add_presence,
    overridden_availability,
    remove_presence,
)
from energy_optimizer.optimization.plan_generation import PlanGenerator
from energy_optimizer.orchestration import ProviderDataSnapshot
from energy_optimizer.storage import ProviderDataStore
from energy_optimizer.storage_errors import ProviderDataStoreError
from test_ev_patterns import _two_weeks_of_commuting
from test_ev_planning import EV_SETTINGS, configuration, ev_responses, importer
from test_orchestration import START, data, orchestrator_for
from test_plan_generation import (
    FIRST_PLANNED_HOUR,
    NOW,
    flat_history,
    prices,
    pv_forecast,
    snapshot,
)


def interval(start_hour: int, end_hour: int, present: bool) -> EvPresenceInput:
    return EvPresenceInput(
        start_time=FIRST_PLANNED_HOUR + timedelta(hours=start_hour),
        end_time=FIRST_PLANNED_HOUR + timedelta(hours=end_hour),
        present=present,
    )


def test_persistence_precedence_adjacency_and_expiry(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    first = add_presence(store, interval(0, 2, False), NOW)
    second = add_presence(store, interval(2, 4, True), NOW)
    restored = ProviderDataStore(tmp_path)
    assert active_presence(restored, NOW) == (first, second)
    hours = [FIRST_PLANNED_HOUR + timedelta(hours=i) for i in range(5)]
    assert overridden_availability(
        (True, True, False, False, False), hours, (first, second)
    ) == (False, False, True, True, False)
    assert active_presence(restored, first.end_time) == (second,)
    assert remove_presence(restored, second.id, NOW)
    assert not remove_presence(restored, uuid4(), NOW)


@pytest.mark.parametrize("present", [False, True])
def test_overlapping_intervals_are_rejected(tmp_path: Path, present: bool) -> None:
    store = ProviderDataStore(tmp_path)
    add_presence(store, interval(0, 3, False), NOW)
    with pytest.raises(ValueError, match="overlapping"):
        add_presence(store, interval(1, 4, present), NOW)


def test_current_hour_is_allowed_but_elapsed_intervals_are_rejected(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    add_presence(
        store,
        EvPresenceInput(
            start_time=NOW.replace(minute=0), end_time=FIRST_PLANNED_HOUR, present=False
        ),
        NOW,
    )
    assert len(active_presence(store, NOW)) == 1
    assert active_presence(store, FIRST_PLANNED_HOUR) == ()
    with pytest.raises(ValueError, match="future"):
        add_presence(store, interval(-3, -2, True), NOW)


def test_driving_energy_remains_independent_of_presence_overrides(
    tmp_path: Path,
) -> None:
    settings = configuration().model_dump(mode="json")
    settings["ev_charging"]["strategy"] = "daily_minimum"
    store = ProviderDataStore(tmp_path)
    generator = PlanGenerator(Configuration.model_validate(settings), store)
    inputs = snapshot(
        household_load=flat_history(1),
        pv_generation=pv_forecast([0.0] * 18),
        electricity_prices=prices([0.1] * 18),
        ev=importer(ev_responses("70", "off")).fetch(now=NOW),
        ev_history=_two_weeks_of_commuting(),
    )
    before = generator.planning_problem(inputs).ev
    assert before is not None and before.consumption_kwh is not None
    assert sum(before.consumption_kwh) > 0
    add_presence(store, interval(0, 18, False), NOW)
    after = generator.planning_problem(inputs).ev
    assert after is not None
    assert after.plugged_in == (False,) * 18
    assert after.consumption_kwh == before.consumption_kwh


def test_replanning_preserves_freshness_and_avoids_provider_requests(
    tmp_path: Path,
) -> None:
    fetched: list[datetime] = []
    plans: list[ProviderDataSnapshot] = []

    def fetch(now: datetime, _: object) -> object:
        fetched.append(now)
        return data(now)

    runtime = orchestrator_for(
        tmp_path, fetch=fetch, plan_generator=plans.append, optimization_enabled=True
    )
    runtime.run_due(START)
    assert runtime.replan(START + timedelta(seconds=10))[0] == "created"
    assert len(plans) == 2
    assert fetched == [START]
    assert runtime.replan(START + timedelta(hours=2))[0] == "not-ready"


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-11-16T08:00:00", "2026-11-16T09:00:00Z"),
        ("2026-11-16T08:30:00Z", "2026-11-16T09:00:00Z"),
        ("2026-11-16T10:00:00Z", "2026-11-16T09:00:00Z"),
    ],
)
def test_invalid_times_are_rejected(start: str, end: str) -> None:
    with pytest.raises(ValueError):
        EvPresenceInput.model_validate(
            {"start_time": start, "end_time": end, "present": False}
        )


@pytest.mark.parametrize("strategy", ["deadline", "immediate", "daily_minimum"])
def test_absence_prevents_charging_and_immediate_energy_moves_to_available_hours(
    tmp_path: Path,
    strategy: str,
) -> None:
    settings = configuration().model_dump(mode="json")
    settings["ev_charging"]["strategy"] = strategy
    settings["ev_charging"]["home_hours"] = {"start": "00:00", "end": "23:00"}
    store = ProviderDataStore(tmp_path)
    add_presence(store, interval(0, 2, False), NOW)
    generator = PlanGenerator(Configuration.model_validate(settings), store)
    stored = generator(
        snapshot(
            household_load=flat_history(1),
            pv_generation=pv_forecast([0.0] * 18),
            electricity_prices=prices([0.1] * 18),
            ev=importer(ev_responses("70", "on")).fetch(now=NOW),
        )
    )
    assert stored.plan.has_schedule()
    charges = [hour.ev.charge_kw for hour in stored.plan.hours if hour.ev]
    assert charges[:2] == pytest.approx([0.0, 0.0], abs=1e-6)
    assert sum(charges) * 0.9 >= 6 - 1e-6
    for hour in stored.plan.hours:
        assert hour.ev is not None
        assert 0 - 1e-6 <= hour.ev.charge_kw <= 4 + 1e-6
        assert hour.grid_import_kw == pytest.approx(hour.load_kw + hour.ev.charge_kw)


@pytest.mark.parametrize("strategy", ["deadline", "immediate"])
def test_presence_allows_an_initially_unplugged_car_to_charge(
    tmp_path: Path, strategy: str
) -> None:
    settings = configuration().model_dump(mode="json")
    settings["ev_charging"]["strategy"] = strategy
    store = ProviderDataStore(tmp_path)
    add_presence(store, interval(0, 2, True), NOW)
    plan = PlanGenerator(Configuration.model_validate(settings), store)(
        snapshot(
            household_load=flat_history(1),
            pv_generation=pv_forecast([0.0] * 18),
            electricity_prices=prices([0.1] * 18),
            ev=importer(ev_responses("70", "off")).fetch(now=NOW),
        )
    ).plan
    assert plan.has_schedule()
    charges = [hour.ev.charge_kw for hour in plan.hours if hour.ev]
    assert sum(charges[:2]) * 0.9 == pytest.approx(6)
    assert charges[2:] == pytest.approx([0.0] * 16, abs=1e-6)


@pytest.fixture
def presence_client(minimal_configuration: Path) -> TestClient:
    with minimal_configuration.open("a", encoding="utf-8") as output:
        output.write(
            "\n"
            + yaml.safe_dump(
                {
                    "persistence": {
                        "directory": str(minimal_configuration.parent / "presence")
                    },
                    "home_assistant": {
                        "base_url": "http://ha.test",
                        "token": "test",
                        "timeout_seconds": 5,
                        "ev": EV_SETTINGS,
                    },
                }
            )
        )
    return TestClient(app)


def test_api_create_restart_overlap_and_remove(presence_client: TestClient) -> None:
    start = (datetime.now(UTC) + timedelta(days=2)).replace(
        minute=0, second=0, microsecond=0
    )
    payload = {
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=2)).isoformat(),
        "present": False,
    }
    with presence_client as client:
        response = client.post("/api/v1/ev/presence", json=payload)
        assert response.status_code == 201
        entry_id = response.json()["overrides"][0]["id"]
        assert response.json()["planning_status"] == "unavailable"
        assert client.post("/api/v1/ev/presence", json=payload).status_code == 422
    with presence_client as client:
        assert (
            client.get("/api/v1/ev/presence").json()["overrides"][0]["id"] == entry_id
        )
        assert (
            client.delete(f"/api/v1/ev/presence/{entry_id}").json()["overrides"] == []
        )
        assert client.delete(f"/api/v1/ev/presence/{entry_id}").status_code == 404


def test_api_rejects_elapsed_intervals(presence_client: TestClient) -> None:
    with presence_client as client:
        assert (
            client.post(
                "/api/v1/ev/presence",
                json={
                    "start_time": "2020-01-01T00:00:00Z",
                    "end_time": "2020-01-02T00:00:00Z",
                    "present": True,
                },
            ).status_code
            == 422
        )


def test_unavailable_configuration_is_reported(client: TestClient) -> None:
    with client:
        assert client.get("/api/v1/ev/presence").status_code == 503


def test_storage_failure_is_reported(
    presence_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> object:
        raise ProviderDataStoreError("unreadable")

    with presence_client as client:
        monkeypatch.setattr(ProviderDataStore, "load", fail)
        assert client.get("/api/v1/ev/presence").status_code == 503
