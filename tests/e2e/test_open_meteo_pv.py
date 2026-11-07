"""Browser coverage for the multi-plane Open-Meteo PV forecast."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import DASHBOARD_TIME_ZONE, _configuration, running_service
from energy_optimizer.config import PvPlaneConfiguration
from fake_world import plane_irradiance_w_per_m2, running_fake_world

pytestmark = pytest.mark.e2e

PERFORMANCE_FACTOR: Final = 0.8
PLANES: Final = (
    PvPlaneConfiguration(
        name="West roof", tilt_degrees=22, azimuth_degrees=90, peak_power_kw=10
    ),
    PvPlaneConfiguration(
        name="Facade", tilt_degrees=80, azimuth_degrees=0, peak_power_kw=2
    ),
)
FORECAST_TIMEOUT_SECONDS: Final = 30
FIRST_SHOWN_HOUR_OF_DAY: Final = 10
SHOWN_HOURS: Final = 2


def _open_meteo_configuration(world_url: str, data_directory: Path) -> dict[str, Any]:
    configuration = _configuration(data_directory)
    del configuration["forecast_solar"]
    configuration["open_meteo_pv"] = {
        "latitude": 52.52,
        "longitude": 13.41,
        "planes": [plane.model_dump() for plane in PLANES],
        "performance_factor": PERFORMANCE_FACTOR,
        "base_url": world_url,
    }
    configuration["orchestration"] = {
        "enabled": True,
        "sources": {"pv_generation": {"interval_seconds": 600}},
    }
    return configuration


def _expected_generation_kw(hour_of_day: int) -> float:
    nameplate_kw = sum(
        plane_irradiance_w_per_m2(hour_of_day, plane.tilt_degrees)
        / 1000
        * plane.peak_power_kw
        for plane in PLANES
    )
    return nameplate_kw * PERFORMANCE_FACTOR


def _shown_window() -> tuple[datetime, datetime]:
    """Two hours of tomorrow's sunshine, always inside the two-day forecast."""
    tomorrow = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(
        days=1
    )
    start = tomorrow.replace(hour=FIRST_SHOWN_HOUR_OF_DAY)
    return start, start + timedelta(hours=SHOWN_HOURS)


def _dashboard_input(moment: datetime) -> str:
    return moment.astimezone(ZoneInfo(DASHBOARD_TIME_ZONE)).strftime("%Y-%m-%dT%H:%M")


def _wait_for_forecast(service_url: str, window: tuple[datetime, datetime]) -> None:
    start, end = window
    deadline = time.monotonic() + FORECAST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        body = httpx.get(
            f"{service_url}/api/v1/dashboard/data",
            params={
                "scenario_kind": "forecast",
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            },
            timeout=5,
        ).json()
        if body.get("series"):
            return
        time.sleep(0.5)
    raise AssertionError("the service did not store the Open-Meteo PV forecast")


@pytest.fixture(scope="module")
def open_meteo_service(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("open-meteo")
    with running_fake_world() as (world_url, world):
        configuration = _open_meteo_configuration(world_url, root / "provider-data")
        with running_service(configuration, root) as service_url:
            _wait_for_forecast(service_url, _shown_window())
            assert world.requests.count("/v1/forecast") >= len(PLANES)
            yield service_url


def test_forecast_tab_shows_the_open_meteo_pv_forecast_of_all_planes(
    open_meteo_service: str, page: Page
) -> None:
    start, end = _shown_window()

    page.goto(f"{open_meteo_service}/dashboard/")
    expect(page.locator("#status")).not_to_contain_text("Loading")
    page.locator("#forecast-tab").click()
    # The tab's first load widens the range to the stored coverage; a range
    # entered before it finishes would be overwritten.
    expect(page.locator("#power-points circle").first).to_be_visible()
    page.locator("#start-date").fill(_dashboard_input(start))
    page.locator("#end-date").fill(_dashboard_input(end))
    page.locator("#range-form button[type=submit]").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")

    expect(page.locator("#power-legend .legend-item")).to_have_text(
        ["PV generation (kW)"]
    )
    points = page.locator("#power-points circle")
    expect(points).to_have_count(SHOWN_HOURS)
    for hour in range(SHOWN_HOURS):
        label = points.nth(hour).get_attribute("aria-label") or ""
        shown_kw = float(label.rsplit(": ", 1)[1].removesuffix(" kW"))
        expected_kw = _expected_generation_kw(FIRST_SHOWN_HOUR_OF_DAY + hour)
        assert shown_kw == pytest.approx(expected_kw)
