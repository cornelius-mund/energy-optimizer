"""Fixtures for browser tests against a real Energy Optimizer process."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest


@dataclass(frozen=True)
class E2EServer:
    """Address and isolated persistence directory for the live test service."""

    base_url: str
    data_directory: Path


def _free_port() -> int:
    """Reserve an available local TCP port for the subprocess."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _configuration(path: Path, data_directory: Path) -> None:
    """Write a deterministic configuration with all dashboard data sources."""
    path.write_text(
        f"""
time_resolution_minutes: 60
grid:
  maximum_import_kw: 10
  maximum_export_kw: 10
solver:
  name: highs
  time_limit_seconds: 60
persistence:
  directory: {data_directory}
forecast_solar:
  latitude: 52.52
  longitude: 13.41
  declination_degrees: 35
  azimuth_degrees: 0
  peak_power_kw: 8
  max_data_age_seconds: 7200
awattar: {{}}
home_assistant:
  base_url: http://homeassistant.local:8123
  token: test-token
  household_load_entities:
    - entity_id: sensor.household_energy
      state_class: total_increasing
      unit: kWh
      operation: add
  timeout_seconds: 10
  max_data_age_seconds: 3600
orchestration:
  enabled: false
""",
        encoding="utf-8",
    )


def _wait_for_server(process: subprocess.Popen[str], base_url: str) -> None:
    """Wait until the live service answers health checks or report startup output."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise RuntimeError(
                "E2E server exited before becoming ready:\n" + (output or "")
            )
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except urllib.error.URLError, TimeoutError:
            time.sleep(0.1)
    raise RuntimeError("E2E server did not become ready within 30 seconds")


@pytest.fixture(scope="session")
def e2e_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[E2EServer]:
    """Run the production package entry point on an isolated local port."""
    root = tmp_path_factory.mktemp("e2e")
    data_directory = root / "provider-data"
    configuration_path = root / "config.yaml"
    _configuration(configuration_path, data_directory)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update(
        {
            "ENERGY_OPTIMIZER_CONFIG": str(configuration_path),
            "ENERGY_OPTIMIZER_FRONTEND_DIRECTORY": str(
                Path(__file__).parents[2] / "frontend"
            ),
            "ENERGY_OPTIMIZER_HOST": "127.0.0.1",
            "ENERGY_OPTIMIZER_PORT": str(port),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "energy_optimizer"],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_server(process, base_url)
        yield E2EServer(base_url=base_url, data_directory=data_directory)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


@pytest.fixture(autouse=True)
def clean_e2e_storage(e2e_server: E2EServer) -> Iterator[None]:
    """Keep browser scenarios independent while reusing the live process."""
    e2e_server.data_directory.mkdir(parents=True, exist_ok=True)
    for path in e2e_server.data_directory.iterdir():
        if path.is_file():
            path.unlink()
    yield
    for path in e2e_server.data_directory.iterdir():
        if path.is_file():
            path.unlink()


@pytest.fixture
def e2e_api(e2e_server: E2EServer) -> Iterator[httpx.Client]:
    """Provide an HTTP client connected to the live service."""
    with httpx.Client(base_url=e2e_server.base_url, timeout=5) as client:
        yield client
