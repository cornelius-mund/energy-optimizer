"""Fixtures for browser tests against a real Energy Optimizer process."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from energy_optimizer.storage import ProviderDataStore

# The service shows Berlin time while the browser runs in New York, two zones
# that never agree, so a dashboard that used the browser's zone fails every test.
DASHBOARD_TIME_ZONE = "Europe/Berlin"
BROWSER_TIME_ZONE = "America/New_York"


def _free_port() -> int:
    """Reserve an available local TCP port for the subprocess."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _energy_flow(entity: str) -> dict[str, Any]:
    """Describe an energy flow measured by one cumulative kWh counter."""
    counter = {
        "entity_id": f"sensor.{entity}",
        "state_class": "total_increasing",
        "unit": "kWh",
    }
    return {"terms": [{"operation": "add", "entities": [counter]}]}


def _configuration(data_directory: Path) -> dict[str, Any]:
    """Return a deterministic configuration with all dashboard data sources."""
    state_of_charge = {"entity_id": "sensor.battery_soc", "unit": "%"}
    efficiency_legs = {
        leg: {
            f"energy_{direction}": _energy_flow(f"{entity}_{direction}")
            for direction in ("in", "out")
        }
        for leg, entity in (
            ("battery", "battery"),
            ("inverter_charge", "charge"),
            ("inverter_discharge", "discharge"),
        )
    }
    return {
        "time_resolution_minutes": 60,
        "timezone": DASHBOARD_TIME_ZONE,
        "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
        "solver": {"name": "highs", "time_limit_seconds": 60},
        "persistence": {"directory": str(data_directory)},
        "forecast_solar": {
            "latitude": 52.52,
            "longitude": 13.41,
            "declination_degrees": 35,
            "azimuth_degrees": 0,
            "peak_power_kw": 8,
            "max_data_age_seconds": 7200,
        },
        "awattar": {},
        "home_assistant": {
            "base_url": "http://homeassistant.local:8123",
            "token": "test-token",
            "household_load": _energy_flow("household_energy"),
            "grid_import": _energy_flow("grid_import"),
            "grid_export": _energy_flow("grid_export"),
            "battery": {
                "state_of_charge": state_of_charge,
                "capacity": 10,
                "minimum_soc": 5,
                "maximum_soc": 100,
                "maximum_charge": 4,
                "maximum_discharge": 4,
                "efficiency_calculation": {
                    "state_of_charge": state_of_charge,
                    **efficiency_legs,
                },
            },
            "timeout_seconds": 10,
            "max_data_age_seconds": 3600,
        },
        "orchestration": {"enabled": False},
    }


def _wait_for_server(
    process: subprocess.Popen[str], base_url: str, log_path: Path
) -> None:
    """Wait until the live service answers health checks or report its log."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "E2E server exited before becoming ready:\n"
                + log_path.read_text(encoding="utf-8", errors="replace")
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError("E2E server did not become ready within 30 seconds")


@pytest.fixture(scope="session")
def data_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Give the live service an isolated persistence directory."""
    return tmp_path_factory.mktemp("e2e") / "provider-data"


@pytest.fixture(scope="session")
def base_url(data_directory: Path) -> Iterator[str]:
    """Run the production package entry point and return where it listens.

    Playwright resolves the relative URLs that pages open against this address.
    """
    root = data_directory.parent
    configuration_path = root / "config.yaml"
    # JSON is valid YAML, so the service reads it like any other configuration.
    configuration_path.write_text(
        json.dumps(_configuration(data_directory)), encoding="utf-8"
    )
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    project = Path(__file__).parents[2]
    environment = {
        **os.environ,
        "ENERGY_OPTIMIZER_CONFIG": str(configuration_path),
        "ENERGY_OPTIMIZER_FRONTEND_DIRECTORY": str(project / "frontend"),
        "ENERGY_OPTIMIZER_HOST": "127.0.0.1",
        "ENERGY_OPTIMIZER_PORT": str(port),
    }
    log_path = root / "service.log"
    # The service logs every request. An unread pipe would fill after a few hundred
    # requests and block the service, so its output goes to a file instead.
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "energy_optimizer"],
            cwd=project,
            env=environment,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_server(process, url, log_path)
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _delete_files(directory: Path) -> None:
    """Empty the persistence directory, creating it first when it is missing."""
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.iterdir():
        if path.is_file():
            path.unlink()


@pytest.fixture(autouse=True)
def clean_e2e_storage(base_url: str, data_directory: Path) -> Iterator[None]:
    """Keep browser scenarios independent while reusing the live process."""
    _delete_files(data_directory)
    yield
    _delete_files(data_directory)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Run every page in a zone that differs from the configured dashboard zone."""
    return {**browser_context_args, "timezone_id": BROWSER_TIME_ZONE}


@pytest.fixture
def e2e_api(base_url: str) -> Iterator[httpx.Client]:
    """Provide an HTTP client connected to the live service."""
    with httpx.Client(base_url=base_url, timeout=5) as client:
        yield client


@pytest.fixture
def provider_store(data_directory: Path) -> ProviderDataStore:
    """Persist normalized provider data where the live service reads it."""
    return ProviderDataStore(data_directory)
