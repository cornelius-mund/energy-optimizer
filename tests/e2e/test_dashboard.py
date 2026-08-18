"""Browser end-to-end coverage for the unified dashboard."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Protocol

import httpx
import pytest
from playwright.sync_api import Page, Request, Route, expect
from pydantic import TypeAdapter

from energy_optimizer.providers.interfaces import (
    ElectricityPriceData,
    PvGenerationData,
    SourceMetadata,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

pytestmark = pytest.mark.e2e

UTC: Final = timezone.utc


class LiveServer(Protocol):
    """The live-server fixture contract needed by browser scenarios."""

    base_url: str
    data_directory: Path


def _window() -> tuple[datetime, datetime]:
    """Return a future two-hour UTC window stable for the duration of one test."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )
    return start, start + timedelta(hours=2)


def _input_value(timestamp: datetime) -> str:
    """Format a UTC timestamp for an HTML datetime-local control."""
    return timestamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def _load_range(page: Page, start: datetime, end: datetime) -> None:
    """Submit one UTC dashboard range and wait for the resulting status."""
    page.locator("#start-date").fill(_input_value(start))
    page.locator("#end-date").fill(_input_value(end))
    page.locator("#range-form button[type=submit]").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")


def _household_payload(
    start: datetime,
    values: list[float],
    *,
    retrieved_at: datetime | None = None,
) -> dict[str, object]:
    """Build a persisted household-load submission for the public API."""
    observation = retrieved_at or datetime.now(UTC)
    return {
        "schema_version": "1",
        "start_time": start.isoformat(),
        "interval_minutes": 60,
        "load_kw": values,
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "household_load"},
        "retrieved_at": observation.isoformat(),
        "latest_observation_at": observation.isoformat(),
    }


def _seed_household(
    client: httpx.Client,
    start: datetime,
    values: list[float],
    *,
    retrieved_at: datetime | None = None,
) -> None:
    """Seed household actuals through the public HTTP provider endpoint."""
    response = client.post(
        "/api/v1/household-load",
        json=_household_payload(start, values, retrieved_at=retrieved_at),
    )
    assert response.status_code == 200, response.text


def _seed_forecasts(
    server: LiveServer,
    start: datetime,
    *,
    price_start: datetime | None = None,
) -> None:
    """Seed normalized forecast records as a real provider would persist them."""
    retrieved_at = datetime.now(UTC)
    effective_price_start = price_start or start
    store = ProviderDataStore(server.data_directory)
    store.save(
        ProviderDataKey("pv-generation", "forecast.solar", "pv_generation"),
        TypeAdapter(PvGenerationData),
        PvGenerationData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            generation_kw=(0.2, 1.4),
            unit="kW",
            source=SourceMetadata(provider="forecast.solar", entity_id="pv_generation"),
            retrieved_at=retrieved_at,
            expires_at=start + timedelta(hours=2),
        ),
    )
    timestamps = (
        effective_price_start,
        effective_price_start + timedelta(hours=1),
    )
    store.save(
        ProviderDataKey("electricity-prices", "awattar.de", "de"),
        TypeAdapter(ElectricityPriceData),
        ElectricityPriceData(
            schema_version="1",
            timestamps=timestamps,
            interval_minutes=60,
            import_price_eur_per_kwh=(0.18, 0.22),
            export_price_eur_per_kwh=(0.08, 0.10),
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=retrieved_at,
            expires_at=effective_price_start + timedelta(hours=2),
        ),
    )


def test_actuals_render_from_api_to_browser(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify persisted actuals become visible chart points in Chromium."""
    start, end = _window()
    _seed_household(e2e_api, start, [1.2, 1.0])

    page.goto(f"{e2e_server.base_url}/dashboard/")
    _load_range(page, start, end)

    expect(page.locator("#content")).to_be_visible()
    expect(page.locator("#status")).to_have_text("2 data points loaded.")
    expect(page.locator("#power-chart")).to_be_visible()
    expect(page.locator("#power-series-paths path")).to_have_count(1)
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#details")).to_contain_text("home-assistant")
    expect(page.locator("#details")).to_contain_text("kW")


def test_forecast_tab_renders_pv_and_prices(e2e_server: LiveServer, page: Page) -> None:
    """Verify forecast data drives separate charts and a data-driven legend."""
    start, end = _window()
    _seed_forecasts(e2e_server, start)

    page.goto(f"{e2e_server.base_url}/dashboard/")
    page.locator("#forecast-tab").click()
    _load_range(page, start, end)

    expect(page.locator("#scenario-badge")).to_contain_text("Forecast inputs")
    expect(page.locator("#chart-heading")).to_have_text("PV and price forecast")
    expect(page.locator("#power-chart")).to_be_visible()
    expect(page.locator("#price-chart")).to_be_visible()
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#price-points circle")).to_have_count(4)
    expect(page.locator("#legend .legend-item")).to_have_count(3)
    expect(page.locator("#legend")).to_contain_text("PV generation (kW)")
    expect(page.locator("#legend")).to_contain_text("Import price (EUR/kWh)")
    expect(page.locator("#legend")).to_contain_text("Export price (EUR/kWh)")


def test_invalid_range_is_rejected_without_an_api_request(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify client-side range validation prevents an invalid dashboard load."""
    requests: list[str] = []

    def record_request(request: Request) -> None:
        url = request.url
        if "/api/v1/dashboard/data" in url:
            requests.append(url)

    page.on("request", record_request)
    page.goto(f"{e2e_server.base_url}/dashboard/")
    expect(page.locator("#status")).to_have_class("status error")
    requests.clear()

    start, _ = _window()
    page.locator("#start-date").fill(_input_value(start))
    page.locator("#end-date").fill(_input_value(start))
    page.locator("#range-form button[type=submit]").click()

    expect(page.locator("#status")).to_have_text(
        "End time must be later than the start time."
    )
    expect(page.locator("#status")).to_have_class("status error")
    assert requests == []


def test_range_is_aligned_to_available_coverage(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify an out-of-range request is corrected to the provider coverage."""
    start, end = _window()
    _seed_household(e2e_api, start, [1.2, 1.0])

    page.goto(f"{e2e_server.base_url}/dashboard/")
    _load_range(page, start - timedelta(hours=1), end + timedelta(hours=1))

    expect(page.locator("#start-date")).to_have_value(_input_value(start))
    expect(page.locator("#end-date")).to_have_value(_input_value(end))
    expect(page.locator("#status")).to_have_text("2 data points loaded.")


def test_unavailable_actuals_are_presented_as_an_error(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a real unavailable dashboard response is visible to the user."""
    page.goto(f"{e2e_server.base_url}/dashboard/")

    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#status")).to_contain_text(
        "no persisted household-load provider data is available"
    )
    expect(page.locator("#power-chart")).to_be_hidden()
    expect(page.locator("#price-chart")).to_be_hidden()


def test_backend_error_is_presented_in_the_dashboard(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a failed dashboard request does not leave a blank interface."""

    def fail_dashboard_request(route: Route) -> None:
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"detail": "dashboard backend is unavailable"}),
        )

    page.route("**/api/v1/dashboard/data**", fail_dashboard_request)
    page.goto(f"{e2e_server.base_url}/dashboard/")

    expect(page.locator("#status")).to_have_text("dashboard backend is unavailable")
    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#content")).to_be_hidden()


def test_partial_coverage_is_shown_as_a_gap(e2e_server: LiveServer, page: Page) -> None:
    """Verify missing hourly observations remain gaps rather than zeros."""
    start, end = _window()
    _seed_forecasts(e2e_server, start, price_start=start + timedelta(hours=1))

    page.goto(f"{e2e_server.base_url}/dashboard/")
    page.locator("#forecast-tab").click()
    _load_range(page, start, end + timedelta(hours=1))

    expect(page.locator("#status")).to_have_text(
        "Partial coverage is available. Missing intervals are shown as gaps."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#price-points circle")).to_have_count(4)
    for path in page.locator(".series-line").all():
        path_data = path.get_attribute("d")
        assert path_data is not None
        assert path_data.count("M") == 1


def test_stale_actuals_are_shown_with_a_warning(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify old but valid actuals remain visible with a freshness warning."""
    start, end = _window()
    old = datetime.now(UTC) - timedelta(days=2)
    _seed_household(e2e_api, start, [1.2, 1.0], retrieved_at=old)

    page.goto(f"{e2e_server.base_url}/dashboard/")
    _load_range(page, start, end)

    expect(page.locator("#status")).to_have_text(
        "Data is available, but its freshness window has expired."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-chart")).to_be_visible()
