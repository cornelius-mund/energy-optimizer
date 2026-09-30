"""Provider-data persistence and history API tests."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from energy_optimizer.api import app
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    HourExclusion,
)
from energy_optimizer.providers.interfaces import HouseholdLoadData, SourceMetadata
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUSEHOLD_KEY = ProviderDataKey("household-load", "home-assistant", "household_load")


def at(hour: int) -> str:
    """Return the ISO timestamp of ``hour`` hours after the start of the data."""
    return (START + timedelta(hours=hour)).isoformat()


def get_historic(client: TestClient, start: str, end: str) -> Any:
    return client.get(
        "/api/v1/historic/household-load",
        params={"start_time": start, "end_time": end},
    )


def later_submission(
    request: dict[str, object], start_hour: int, load_kw: list[float]
) -> dict[str, object]:
    """Return ``request`` moved to ``start_hour``, observed one hour afterwards."""
    return request | {
        "start_time": at(start_hour),
        "load_kw": load_kw,
        "retrieved_at": at(start_hour + 1),
        "latest_observation_at": at(start_hour + 1),
    }


def test_grid_flow_provider_data_is_persisted_and_retrieved_after_restart(
    persistence_client: TestClient,
    grid_flow_persisted_request: dict[str, object],
) -> None:
    with persistence_client as client:
        write_response = client.post(
            "/api/v1/grid-flow", json=grid_flow_persisted_request
        )
        read_response = client.get("/api/v1/grid-flow")

    assert write_response.status_code == 200
    assert read_response.status_code == 200
    assert read_response.json() == write_response.json()

    with TestClient(app) as restarted_client:
        restarted_response = restarted_client.get("/api/v1/grid-flow")

    assert restarted_response.status_code == 200
    assert restarted_response.json() == read_response.json()


@pytest.mark.parametrize(
    ("path", "request_fixture"),
    [
        ("/api/v1/grid-flow", "grid_flow_request"),
        ("/api/v1/household-load", "household_load_request"),
    ],
)
def test_direct_submission_is_not_persisted(
    persistence_client: TestClient,
    request: pytest.FixtureRequest,
    path: str,
    request_fixture: str,
) -> None:
    submission = dict(request.getfixturevalue(request_fixture))
    submission.pop("source")

    with persistence_client as client:
        write_response = client.post(path, json=submission)
        read_response = client.get(path)

    assert write_response.status_code == 200
    assert read_response.status_code == 404


def test_household_load_provider_data_is_persisted_and_retrieved_after_restart(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    with persistence_client as client:
        write_response = client.post(
            "/api/v1/household-load", json=household_load_request
        )
        read_response = client.get("/api/v1/household-load")

    assert write_response.status_code == 200
    assert read_response.status_code == 200
    assert read_response.json() == {
        "status": "validated",
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "load_kw": [1.2, 1.0],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "household_load"},
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
    }

    with TestClient(app) as restarted_client:
        restarted_response = restarted_client.get("/api/v1/household-load")

    assert restarted_response.status_code == 200
    assert restarted_response.json() == read_response.json()


def test_household_load_provider_data_is_merged_on_persistence(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    second = later_submission(household_load_request, 1, [9.0, 3.0])

    with persistence_client as client:
        first_response = client.post(
            "/api/v1/household-load", json=household_load_request
        )
        second_response = client.post("/api/v1/household-load", json=second)
        read_response = client.get("/api/v1/household-load")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert read_response.status_code == 200
    assert second_response.json()["start_time"] == "2026-01-01T00:00:00Z"
    assert second_response.json()["load_kw"] == [1.2, 9.0, 3.0]
    assert read_response.json() == second_response.json()


def test_household_load_submission_after_a_gap_persists_the_gap_as_null_hours(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    later = later_submission(household_load_request, 5, [9.0])

    with persistence_client as client:
        first = client.post("/api/v1/household-load", json=household_load_request)
        second = client.post("/api/v1/household-load", json=later)
        historic = get_historic(client, at(0), at(6))

    assert first.status_code == 200
    assert second.status_code == 200
    assert historic.status_code == 200
    assert historic.json()["load_kw"] == [1.2, 1.0, None, None, None, 9.0]


def test_historic_household_load_returns_requested_range_and_metadata(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    with persistence_client as client:
        client.post("/api/v1/household-load", json=household_load_request)
        response = get_historic(client, at(1), at(2))

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "data_type": "household_load",
        "schema_version": "1",
        "start_time": "2026-01-01T01:00:00Z",
        "end_time": "2026-01-01T02:00:00Z",
        "interval_minutes": 60,
        "timestamps": ["2026-01-01T01:00:00Z"],
        "load_kw": [1.0],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "household_load"},
        "coverage_start_time": "2026-01-01T01:00:00Z",
        "coverage_end_time": "2026-01-01T02:00:00Z",
        "available_start_time": "2026-01-01T00:00:00Z",
        "available_end_time": "2026-01-01T02:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
        "freshness": "unknown",
        "freshness_checked_at": response.json()["freshness_checked_at"],
    }


def test_historic_household_load_returns_null_for_an_excluded_hour(
    persistence_configuration: Path,
) -> None:
    """Persist three hours whose middle hour is excluded and serve them."""
    hour_start = START + timedelta(hours=1)
    excluded = HourExclusion(
        hour_start,
        (
            ExclusionCause.of(
                "counter_decrease",
                "sensor.household_energy decreased from 3 kWh to 2 kWh",
                "sensor.household_energy",
                [ExcludedDataPoint(hour_start, state="2", unit="kWh")],
            ),
        ),
    )
    ProviderDataStore(persistence_configuration.parent / "provider-data").save(
        HOUSEHOLD_KEY,
        TypeAdapter(HouseholdLoadData),
        HouseholdLoadData(
            schema_version="1",
            start_time=START,
            interval_minutes=60,
            load_kw=(1.0, None, 3.0),
            unit="kW",
            source=SourceMetadata("home-assistant", "household_load"),
            retrieved_at=START,
            latest_observation_at=START + timedelta(hours=3),
            exclusions=(excluded,),
        ),
    )

    with TestClient(app) as client:
        whole_range = get_historic(client, at(0), at(3))
        excluded_only = get_historic(client, at(1), at(2))
        stored = client.get("/api/v1/household-load")

    assert whole_range.status_code == 200
    body = whole_range.json()
    assert body["status"] == "validated"
    assert body["timestamps"] == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        "2026-01-01T02:00:00Z",
    ]
    assert body["load_kw"] == [1.0, None, 3.0]
    assert "quality" not in body
    assert "validation_status" not in body
    assert excluded_only.status_code == 200
    assert excluded_only.json()["status"] == "validated"
    assert excluded_only.json()["timestamps"] == ["2026-01-01T01:00:00Z"]
    assert excluded_only.json()["load_kw"] == [None]
    assert stored.status_code == 200
    assert stored.json()["load_kw"] == [1.0, None, 3.0]


def test_historic_household_load_serves_suspect_hours_of_legacy_data_as_null(
    persistence_configuration: Path,
) -> None:
    directory = persistence_configuration.parent / "provider-data"
    directory.mkdir()
    valid = {"status": "valid", "reason": None, "entity_id": None}
    suspect = {
        "status": "suspect",
        "reason": "reset_recovery",
        "entity_id": "sensor.household_energy",
    }
    quality = [valid, suspect, valid]
    (directory / f"household-load-{HOUSEHOLD_KEY.digest()}.ndjson").write_bytes(
        b"".join(
            json.dumps(
                {
                    "timestamp": at(hour),
                    "load_kw": float(hour + 1),
                    "quality": quality[hour],
                    "schema_version": "1",
                    "unit": "kW",
                    "source": {
                        "provider": "home-assistant",
                        "entity_id": "household_load",
                    },
                    "retrieved_at": START.isoformat(),
                    "latest_observation_at": at(3),
                }
            ).encode()
            + b"\n"
            for hour in range(3)
        )
    )

    with TestClient(app) as client:
        response = get_historic(client, at(0), at(3))

    assert response.status_code == 200
    assert response.json()["status"] == "validated"
    assert response.json()["load_kw"] == [1.0, None, 3.0]
    assert "quality" not in response.json()


def test_historic_household_load_reports_empty_range(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    with persistence_client as client:
        client.post("/api/v1/household-load", json=household_load_request)
        response = get_historic(client, at(24), at(25))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["timestamps"] == []
    assert body["load_kw"] == []
    assert body["coverage_start_time"] is None
    assert body["coverage_end_time"] is None


def test_historic_household_load_reports_stale_but_valid_history(
    persistence_configuration: Path,
    household_load_request: dict[str, object],
) -> None:
    persistence_configuration.write_text(
        persistence_configuration.read_text(encoding="utf-8").replace(
            "  timeout_seconds: 10", "  timeout_seconds: 10\n  max_data_age_seconds: 1"
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        client.post("/api/v1/household-load", json=household_load_request)
        response = get_historic(client, at(0), at(1))

    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    assert "validation_status" not in response.json()
    assert "quality" not in response.json()


def test_historic_household_load_reports_corrupt_persistence(
    tmp_path: Path, persistence_configuration: Path
) -> None:
    store = tmp_path / "provider-data"
    store.mkdir()
    (store / f"household-load-{HOUSEHOLD_KEY.digest()}.ndjson").write_text(
        "invalid\n", encoding="utf-8"
    )

    with TestClient(app) as client:
        response = get_historic(client, at(0), at(1))

    assert response.status_code == 503
    assert "could not recover" in response.json()["detail"]


def test_historic_household_load_rejects_invalid_ranges(
    persistence_client: TestClient,
) -> None:
    with persistence_client as client:
        responses = [
            get_historic(client, at(1), at(0)),
            get_historic(client, "2026-01-01T00:00:00", at(1)),
        ]

    assert all(response.status_code == 422 for response in responses)


def test_household_load_persistence_reports_missing_configuration(
    client: TestClient,
) -> None:
    with client as test_client:
        response = test_client.get("/api/v1/household-load")

    assert response.status_code == 503
    assert "persistence is not configured" in response.text
