"""Provider-data persistence and history API tests."""

from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from energy_optimizer.api import app
from energy_optimizer.storage import ProviderDataKey


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


def test_grid_flow_direct_submission_is_not_persisted(
    persistence_client: TestClient, grid_flow_request: dict[str, object]
) -> None:
    request = grid_flow_request.copy()
    request.pop("source")

    with persistence_client as client:
        write_response = client.post("/api/v1/grid-flow", json=request)
        read_response = client.get("/api/v1/grid-flow")

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
        "source": {
            "provider": "home-assistant",
            "entity_id": "household_load",
        },
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
    first = household_load_request.copy()
    second = household_load_request.copy()
    second.update(
        {
            "start_time": "2026-01-01T01:00:00+00:00",
            "load_kw": [9.0, 3.0],
            "retrieved_at": "2026-01-01T02:00:00+00:00",
            "latest_observation_at": "2026-01-01T02:00:00+00:00",
        }
    )

    with persistence_client as client:
        first_response = client.post("/api/v1/household-load", json=first)
        second_response = client.post("/api/v1/household-load", json=second)
        read_response = client.get("/api/v1/household-load")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert read_response.status_code == 200
    assert second_response.json()["start_time"] == "2026-01-01T00:00:00Z"
    assert second_response.json()["load_kw"] == [1.2, 9.0, 3.0]
    assert read_response.json() == second_response.json()


def test_historic_household_load_returns_requested_range_and_metadata(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    with persistence_client as client:
        client.post("/api/v1/household-load", json=household_load_request)
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-01T01:00:00+00:00",
                "end_time": "2026-01-01T02:00:00+00:00",
            },
        )

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
        "quality": [{"status": "valid", "reason": None, "entity_id": None}],
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "household_load"},
        "coverage_start_time": "2026-01-01T01:00:00Z",
        "coverage_end_time": "2026-01-01T02:00:00Z",
        "available_start_time": "2026-01-01T00:00:00Z",
        "available_end_time": "2026-01-01T02:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T01:00:00Z",
        "validation_status": "valid",
        "freshness": "unknown",
        "freshness_checked_at": response.json()["freshness_checked_at"],
    }


def test_historic_household_load_reports_empty_range(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    with persistence_client as client:
        client.post("/api/v1/household-load", json=household_load_request)
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-02T00:00:00+00:00",
                "end_time": "2026-01-02T01:00:00+00:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "empty"
    assert body["timestamps"] == []
    assert body["load_kw"] == []
    assert body["coverage_start_time"] is None
    assert body["coverage_end_time"] is None


def test_historic_household_load_reports_stale_but_valid_history(
    persistence_configuration: Path,
    monkeypatch: MonkeyPatch,
    household_load_request: dict[str, object],
) -> None:
    configuration = persistence_configuration
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            "  timeout_seconds: 10", "  timeout_seconds: 10\n  max_data_age_seconds: 1"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))

    with TestClient(app) as client:
        client.post("/api/v1/household-load", json=household_load_request)
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    assert response.json()["validation_status"] == "valid"


def test_historic_household_load_reports_corrupt_persistence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    persistence_configuration: Path,
) -> None:
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(persistence_configuration))
    store = tmp_path / "provider-data"
    store.mkdir()
    key = "household-load-"
    provider_key = ProviderDataKey("household-load", "home-assistant", "household_load")
    (store / f"{key}{provider_key.digest()}.ndjson").write_text(
        "invalid\n", encoding="utf-8"
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/historic/household-load",
            params={
                "start_time": "2026-01-01T00:00:00+00:00",
                "end_time": "2026-01-01T01:00:00+00:00",
            },
        )

    assert response.status_code == 503
    assert "could not recover" in response.json()["detail"]


def test_historic_household_load_rejects_invalid_ranges(
    persistence_client: TestClient,
) -> None:
    with persistence_client as client:
        responses = [
            client.get(
                "/api/v1/historic/household-load",
                params={
                    "start_time": "2026-01-01T01:00:00+00:00",
                    "end_time": "2026-01-01T00:00:00+00:00",
                },
            ),
            client.get(
                "/api/v1/historic/household-load",
                params={
                    "start_time": "2026-01-01T00:00:00",
                    "end_time": "2026-01-01T01:00:00+00:00",
                },
            ),
        ]

    assert all(response.status_code == 422 for response in responses)


def test_household_load_direct_submission_is_not_persisted(
    persistence_client: TestClient, household_load_request: dict[str, object]
) -> None:
    request = household_load_request.copy()
    request.pop("source")

    with persistence_client as client:
        write_response = client.post("/api/v1/household-load", json=request)
        read_response = client.get("/api/v1/household-load")

    assert write_response.status_code == 200
    assert read_response.status_code == 404


def test_household_load_persistence_reports_missing_configuration(
    client: TestClient,
) -> None:
    with client as test_client:
        response = test_client.get("/api/v1/household-load")

    assert response.status_code == 503
    assert "persistence is not configured" in response.text
