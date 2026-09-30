"""Core HTTP API tests."""

import pytest
from fastapi.testclient import TestClient


def test_health_returns_service_status_and_version(client: TestClient) -> None:
    with client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_optimize_accepts_a_valid_hourly_request(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    with client:
        response = client.post("/optimize", json=valid_request)

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "hours": 2,
    }


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
