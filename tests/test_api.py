"""Tests for the HTTP API."""

from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from energy_optimizer.api import app


def test_health_returns_service_status_and_version(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    configuration = tmp_path / "config.yaml"
    configuration.write_text(
        """
time_resolution_minutes: 60
grid:
  maximum_import_kw: 10
  maximum_export_kw: 10
solver:
  name: highs
  time_limit_seconds: 60
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENERGY_OPTIMIZER_CONFIG", str(configuration))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}
