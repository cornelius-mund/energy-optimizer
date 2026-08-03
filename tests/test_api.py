"""Tests for the HTTP API."""

from fastapi.testclient import TestClient

from energy_optimizer.api import app

client = TestClient(app)


def test_health_returns_service_status_and_version() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}
