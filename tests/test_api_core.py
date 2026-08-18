"""Core HTTP API tests."""

from fastapi.testclient import TestClient


def test_health_returns_service_status_and_version(client: TestClient) -> None:
    with client as test_client:
        response = test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_optimize_accepts_a_valid_hourly_request(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    with client as test_client:
        response = test_client.post("/optimize", json=valid_request)

    assert response.status_code == 200
    assert response.json() == {
        "status": "validated",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "hours": 2,
    }


def test_optimize_rejects_invalid_hourly_inputs(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    request = valid_request.copy()
    request["pv_generation_kw"] = [0.0]

    with client as test_client:
        response = test_client.post("/optimize", json=request)

    assert response.status_code == 422
    assert "time-series lengths must match" in response.text


def test_optimize_rejects_non_hourly_interval(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    request = valid_request.copy()
    request["interval_minutes"] = 30

    with client as test_client:
        response = test_client.post("/optimize", json=request)

    assert response.status_code == 422
    assert "interval_minutes" in response.text


def test_optimize_rejects_naive_start_time(
    client: TestClient, valid_request: dict[str, object]
) -> None:
    request = valid_request.copy()
    request["start_time"] = "2026-01-01T00:00:00"

    with client as test_client:
        response = test_client.post("/optimize", json=request)

    assert response.status_code == 422
    assert "timezone" in response.text
