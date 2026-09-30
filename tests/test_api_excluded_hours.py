"""The excluded-hours endpoint: every hour left out of imported history, with causes."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from energy_optimizer.api import app
from energy_optimizer.exclusions import (
    MAX_DATA_POINTS_PER_ENTITY_HOUR,
    ExcludedDataPoint,
    ExclusionCause,
    HourExclusion,
)
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    merge_battery_efficiency_history,
)
from energy_optimizer.providers.interfaces import HouseholdLoadData
from energy_optimizer.providers.interfaces import (
    SourceMetadata as ProviderSourceMetadata,
)
from energy_optimizer.storage import ProviderDataStore
from test_api_historic_assets import (
    GRID_KEY,
    HOUSEHOLD_KEY,
    START,
    Environment,
    battery_history,
    corrupt,
    grid_flow,
    seed_battery,
    seed_grid,
    seed_household,
    write_configuration,
)

ENDPOINT = "/api/v1/dashboard/excluded-hours"
WINDOW = {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-01T04:00:00Z"}


@pytest.fixture
def environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Environment]:
    """Run a fully configured application; tests choose what data to seed."""
    write_configuration(tmp_path, monkeypatch)
    with TestClient(app) as client:
        yield Environment(
            client, ProviderDataStore(tmp_path / "provider-data"), tmp_path
        )


def get(
    client: TestClient, params: dict[str, str] | None = None, status: int = 200
) -> Any:
    response = client.get(ENDPOINT, params=params or WINDOW)
    assert response.status_code == status, response.text
    return response.json()


def test_every_source_is_listed_in_hour_order_with_its_causes(
    environment: Environment,
) -> None:
    seed_household(environment.store, excluded=(1, 3))
    seed_grid(environment.store, grid_flow(excluded=(1,)))
    seed_battery(environment.store, battery_history(excluded=(2,)))

    body = get(environment.client)

    assert body["schema_version"] == "1"
    assert body["requested_start_time"] == "2026-01-01T00:00:00Z"
    assert body["requested_end_time"] == "2026-01-01T04:00:00Z"
    assert body["excluded_hour_count"] == 4
    assert [(item["hour_start"], item["source"]) for item in body["hours"]] == [
        ("2026-01-01T01:00:00Z", "household_load"),
        ("2026-01-01T01:00:00Z", "grid_flow"),
        ("2026-01-01T02:00:00Z", "battery_efficiency"),
        ("2026-01-01T03:00:00Z", "household_load"),
    ]
    assert body["sources"] == [
        {
            "source": "household_load",
            "status": "available",
            "reason": None,
            "excluded_hour_count": 2,
        },
        {
            "source": "grid_flow",
            "status": "available",
            "reason": None,
            "excluded_hour_count": 1,
        },
        {
            "source": "battery_efficiency",
            "status": "available",
            "reason": None,
            "excluded_hour_count": 1,
        },
    ]
    assert body["summary"] == [
        {
            "source": "household_load",
            "reason": "counter_decrease",
            "excluded_hour_count": 2,
        },
        {
            "source": "grid_flow",
            "reason": "counter_decrease",
            "excluded_hour_count": 1,
        },
        {
            "source": "battery_efficiency",
            "reason": "counter_decrease",
            "excluded_hour_count": 1,
        },
    ]
    [cause] = body["hours"][0]["causes"]
    assert cause == {
        "reason": "counter_decrease",
        "message": "sensor.household_energy is excluded in hour 1",
        "entity_id": "sensor.household_energy",
        "data_point_count": 1,
        "data_points": [
            {
                "timestamp": "2026-01-01T01:00:00Z",
                "state": "2",
                "unit": "kWh",
                "entity_id": None,
                "previous_timestamp": None,
                "previous_value": None,
                "value": None,
                "step_kwh": None,
                "maximum_kwh": None,
            }
        ],
    }


def test_hours_the_provider_no_longer_holds_are_listed_as_history_unavailable(
    environment: Environment,
) -> None:
    """Every source persisted one hour, then Home Assistant only had hour 3."""
    later = START + timedelta(hours=3)
    seed_household(environment.store, (1.0,))
    seed_household(environment.store, (4.0,), start=later)
    seed_grid(environment.store, grid_flow((0.5,)))
    seed_grid(environment.store, replace(grid_flow((3.5,)), start_time=later))
    seed_battery(
        environment.store,
        merge_battery_efficiency_history(
            battery_history(intervals=1),
            replace(battery_history(intervals=1), start_time=later),
        ),
    )

    body = get(environment.client)

    assert body["excluded_hour_count"] == 6
    assert [(item["hour_start"], item["source"]) for item in body["hours"]] == [
        ("2026-01-01T01:00:00Z", "household_load"),
        ("2026-01-01T01:00:00Z", "grid_flow"),
        ("2026-01-01T01:00:00Z", "battery_efficiency"),
        ("2026-01-01T02:00:00Z", "household_load"),
        ("2026-01-01T02:00:00Z", "grid_flow"),
        ("2026-01-01T02:00:00Z", "battery_efficiency"),
    ]
    assert [source["excluded_hour_count"] for source in body["sources"]] == [2, 2, 2]
    assert body["summary"] == [
        {"source": source, "reason": "history_unavailable", "excluded_hour_count": 2}
        for source in ("household_load", "grid_flow", "battery_efficiency")
    ]
    for item in body["hours"]:
        assert item["causes"] == [
            {
                "reason": "history_unavailable",
                "message": (
                    "The provider holds no history from 2026-01-01T01:00:00+00:00 "
                    "until 2026-01-01T03:00:00+00:00 (2 hours), so these hours "
                    "cannot be imported."
                ),
                "entity_id": None,
                "data_point_count": 0,
                "data_points": [],
            }
        ]


def test_only_hours_inside_the_requested_range_are_returned(
    environment: Environment,
) -> None:
    seed_household(environment.store, excluded=(0, 1, 2, 3))

    body = get(
        environment.client,
        {"start_time": "2026-01-01T01:00:00Z", "end_time": "2026-01-01T03:00:00Z"},
    )

    assert [item["hour_start"] for item in body["hours"]] == [
        "2026-01-01T01:00:00Z",
        "2026-01-01T02:00:00Z",
    ]
    assert body["excluded_hour_count"] == 2
    # A range before all data still names the source as checked and available.
    empty = get(
        environment.client,
        {"start_time": "2025-12-31T00:00:00Z", "end_time": "2025-12-31T04:00:00Z"},
    )
    assert empty["hours"] == []
    assert empty["excluded_hour_count"] == 0
    assert empty["summary"] == []
    assert empty["sources"][0]["status"] == "available"


def test_a_hour_counts_once_per_distinct_reason_in_the_summary(
    environment: Environment,
) -> None:
    hour_start = START + timedelta(hours=1)
    twice = HourExclusion(
        hour_start,
        (
            ExclusionCause.of("unavailable", "a", "sensor.a", []),
            ExclusionCause.of("unavailable", "b", "sensor.b", []),
            ExclusionCause.of("counter_decrease", "c", "sensor.c", []),
        ),
    )
    environment.store.save(
        HOUSEHOLD_KEY,
        TypeAdapter(HouseholdLoadData),
        HouseholdLoadData(
            schema_version="1",
            start_time=START,
            interval_minutes=60,
            load_kw=(1.0, None),
            unit="kW",
            source=ProviderSourceMetadata("home-assistant", "household_load"),
            retrieved_at=START,
            latest_observation_at=START,
            exclusions=(twice,),
        ),
    )

    body = get(environment.client)

    assert body["summary"] == [
        {
            "source": "household_load",
            "reason": "counter_decrease",
            "excluded_hour_count": 1,
        },
        {
            "source": "household_load",
            "reason": "unavailable",
            "excluded_hour_count": 1,
        },
    ]
    assert [cause["reason"] for cause in body["hours"][0]["causes"]] == [
        "unavailable",
        "unavailable",
        "counter_decrease",
    ]


def test_the_stored_bound_on_data_points_is_reported_with_the_full_count(
    environment: Environment,
) -> None:
    points = [
        ExcludedDataPoint(START + timedelta(minutes=index), "unavailable")
        for index in range(MAX_DATA_POINTS_PER_ENTITY_HOUR)
    ]
    cause = ExclusionCause(
        "unavailable", "many", "sensor.a", tuple(points), data_point_count=120
    )
    environment.store.save(
        HOUSEHOLD_KEY,
        TypeAdapter(HouseholdLoadData),
        HouseholdLoadData(
            schema_version="1",
            start_time=START,
            interval_minutes=60,
            load_kw=(None,),
            unit="kW",
            source=ProviderSourceMetadata("home-assistant", "household_load"),
            retrieved_at=START,
            latest_observation_at=START,
            exclusions=(HourExclusion(START, (cause,)),),
        ),
    )

    [hour] = get(environment.client)["hours"]

    [returned] = hour["causes"]
    assert returned["data_point_count"] == 120
    assert len(returned["data_points"]) == MAX_DATA_POINTS_PER_ENTITY_HOUR


def test_unconfigured_sources_and_sources_without_history_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_configuration(tmp_path, monkeypatch, grid=False, battery=None)
    with TestClient(app) as client:
        body = get(client)

    assert body["excluded_hour_count"] == 0
    assert body["hours"] == []
    assert [
        (item["source"], item["status"], item["reason"]) for item in body["sources"]
    ] == [
        (
            "household_load",
            "unavailable",
            "no persisted household-load data is available yet",
        ),
        (
            "grid_flow",
            "not_configured",
            "no Home Assistant grid import and export entities are configured",
        ),
        (
            "battery_efficiency",
            "not_configured",
            "battery efficiency history is retained only when "
            "battery.efficiency_calculation is configured",
        ),
    ]


def test_configured_sources_without_persisted_history_are_unavailable(
    environment: Environment,
) -> None:
    body = get(environment.client)

    assert [(item["source"], item["status"]) for item in body["sources"]] == [
        ("household_load", "unavailable"),
        ("grid_flow", "unavailable"),
        ("battery_efficiency", "unavailable"),
    ]
    assert all(item["reason"] for item in body["sources"])


def test_a_configuration_without_persistence_reports_every_source_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_configuration(tmp_path, monkeypatch, persistence=False)
    with TestClient(app) as client:
        body = get(client)

    assert {item["status"] for item in body["sources"]} == {"unavailable"}
    assert all(
        "persistence is not configured" in item["reason"] for item in body["sources"]
    )


def test_corrupt_persisted_data_is_withheld_without_hiding_other_sources(
    environment: Environment,
) -> None:
    seed_household(environment.store, excluded=(1,))
    seed_grid(environment.store, grid_flow(excluded=(2,)))
    corrupt(environment.directory, GRID_KEY)

    body = get(environment.client)

    statuses = {item["source"]: item for item in body["sources"]}
    assert statuses["grid_flow"]["status"] == "invalid"
    assert "withheld" in statuses["grid_flow"]["reason"]
    assert statuses["household_load"]["status"] == "available"
    assert [item["source"] for item in body["hours"]] == ["household_load"]


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        (
            {"start_time": "2026-01-01T00:00:00", "end_time": "2026-01-01T04:00:00Z"},
            "must include a timezone",
        ),
        (
            {
                "start_time": "2026-01-01T00:30:00Z",
                "end_time": "2026-01-01T04:00:00Z",
            },
            "aligned to the hour",
        ),
        (
            {
                "start_time": "2026-01-01T04:00:00Z",
                "end_time": "2026-01-01T04:00:00Z",
            },
            "later than start_time",
        ),
        (
            {
                "start_time": "2016-01-01T00:00:00Z",
                "end_time": "2026-01-01T04:00:00Z",
            },
            "must not exceed",
        ),
    ],
)
def test_the_range_is_validated_like_the_other_dashboard_endpoints(
    environment: Environment, params: dict[str, str], detail: str
) -> None:
    body = get(environment.client, params, status=422)

    assert detail in body["detail"]


def test_a_missing_range_boundary_is_rejected(environment: Environment) -> None:
    assert (
        environment.client.get(
            ENDPOINT, params={"start_time": "2026-01-01T00:00:00Z"}
        ).status_code
        == 422
    )


def test_the_endpoint_is_documented_in_the_generated_contract(
    environment: Environment,
) -> None:
    schema = environment.client.get("/openapi.json").json()

    operation = schema["paths"][ENDPOINT]["get"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "start_time",
        "end_time",
    }
    reasons = schema["components"]["schemas"]["ExclusionCause"]["properties"]["reason"][
        "enum"
    ]
    assert "counter_decrease" in reasons
    assert "flagged_by_earlier_version" in reasons
    assert "history_unavailable" in reasons
