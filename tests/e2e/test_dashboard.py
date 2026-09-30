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

from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    HourExclusion,
)
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyData,
    BatteryEfficiencyHistoryData,
    ElectricityPriceData,
    GridFlowData,
    HouseholdLoadData,
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


def _assistive_annotations(page: Page) -> list[str]:
    """Return tooltip and accessible-label text attached to the efficiency summary."""
    texts: list[str] = page.locator("#efficiency-summary").evaluate(
        """(summary) => [...summary.querySelectorAll("[title], [aria-label]")]
            .flatMap((element) => [
              element.getAttribute("title"),
              element.getAttribute("aria-label"),
            ])
            .filter(Boolean)"""
    )
    return texts


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
    import_prices: tuple[float, ...] = (0.18, 0.22),
    export_prices: tuple[float, ...] = (0.08, 0.10),
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
    if len(import_prices) != len(export_prices):
        raise ValueError("price directions must contain the same number of values")
    timestamps = tuple(
        effective_price_start + timedelta(hours=index)
        for index in range(len(import_prices))
    )
    store.save(
        ProviderDataKey("electricity-prices", "awattar.de", "de"),
        TypeAdapter(ElectricityPriceData),
        ElectricityPriceData(
            schema_version="1",
            timestamps=timestamps,
            interval_minutes=60,
            import_price_eur_per_kwh=import_prices,
            export_price_eur_per_kwh=export_prices,
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=retrieved_at,
            expires_at=effective_price_start + timedelta(hours=2),
        ),
    )


def _past_window() -> tuple[datetime, datetime]:
    """Return a completed two-hour UTC window, since actuals only cover the past."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=6
    )
    return start, start + timedelta(hours=2)


def _seed_grid_flow(client: httpx.Client, start: datetime) -> None:
    """Seed grid import and export through the public HTTP provider endpoint."""
    observation = datetime.now(UTC)
    response = client.post(
        "/api/v1/grid-flow",
        json={
            "schema_version": "1",
            "start_time": start.isoformat(),
            "interval_minutes": 60,
            "import_kw": [0.8, 1.6],
            "export_kw": [0.0, 0.4],
            "unit": "kW",
            "source": {"provider": "home-assistant", "entity_id": "grid_flow"},
            "retrieved_at": observation.isoformat(),
            "latest_observation_at": observation.isoformat(),
        },
    )
    assert response.status_code == 200, response.text


def _seed_price_and_battery_history(server: LiveServer, start: datetime) -> None:
    """Seed retained price and battery-state history as orchestration persists it."""
    observation = datetime.now(UTC)
    store = ProviderDataStore(server.data_directory)
    store.save(
        ProviderDataKey("electricity-price-history", "awattar.de", "de"),
        TypeAdapter(ElectricityPriceData),
        ElectricityPriceData(
            schema_version="1",
            timestamps=(start, start + timedelta(hours=1)),
            interval_minutes=60,
            import_price_eur_per_kwh=(0.31, 0.27),
            export_price_eur_per_kwh=(0.09, 0.07),
            unit="EUR/kWh",
            source=SourceMetadata(provider="awattar.de", entity_id="de"),
            retrieved_at=observation,
            expires_at=observation + timedelta(hours=24),
        ),
    )
    intervals = (0.5, 0.5)
    store.save(
        ProviderDataKey(
            "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
        ),
        TypeAdapter(BatteryEfficiencyHistoryData),
        BatteryEfficiencyHistoryData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            battery_energy_in_kwh=intervals,
            battery_energy_out_kwh=intervals,
            inverter_charge_energy_in_kwh=intervals,
            inverter_charge_energy_out_kwh=intervals,
            inverter_discharge_energy_in_kwh=intervals,
            inverter_discharge_energy_out_kwh=intervals,
            state_of_charge_percent=(35.0, 45.0, 55.0),
            unit="kWh",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency_history"
            ),
            retrieved_at=observation,
            latest_observation_at=observation,
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


def test_historic_tab_renders_every_available_asset_with_its_availability(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify all persisted asset actuals reach the browser, by unit and source."""
    start, end = _past_window()
    _seed_household(e2e_api, start, [1.2, 1.0])
    _seed_grid_flow(e2e_api, start)
    _seed_price_and_battery_history(e2e_server, start)

    page.goto(f"{e2e_server.base_url}/dashboard/")
    _load_range(page, start, end)

    expect(page.locator("#status")).to_have_text("12 data points loaded.")
    expect(page.locator("#chart-heading")).to_have_text("Historic energy data")
    expect(page.locator("#power-series-paths path")).to_have_count(3)
    expect(page.locator("#power-points circle")).to_have_count(6)
    expect(page.locator("#power-axis-unit")).to_have_text("kW")
    expect(page.locator("#price-series-paths path")).to_have_count(2)
    expect(page.locator("#price-points circle")).to_have_count(4)
    expect(page.locator("#price-axis-unit")).to_have_text("EUR/kWh")
    expect(page.locator("#battery-chart")).to_be_visible()
    expect(page.locator("#battery-axis-unit")).to_have_text("%")
    expect(page.locator("#battery-points circle")).to_have_count(2)
    expect(page.locator("#legend .legend-item")).to_have_count(6)
    expect(page.locator("#legend")).to_contain_text("Grid import (kW)")
    expect(page.locator("#legend")).to_contain_text("Battery state of charge (%)")
    expect(page.locator("#details")).to_contain_text("home-assistant / grid_flow")
    expect(page.locator("#details")).to_contain_text("awattar.de / de")
    battery_point = page.locator("#battery-points circle").first
    assert "35" in (battery_point.get_attribute("aria-label") or "")
    # Assets without any importer are named with the reason instead of hidden.
    expect(page.locator("#details")).to_contain_text("PV generation")
    expect(page.locator("#details")).to_contain_text("Not configured")
    expect(page.locator("#details")).to_contain_text("Electric vehicle")
    expect(page.locator("#details")).to_contain_text("Heat pump")


def test_historic_tab_withholds_invalid_asset_data_but_keeps_valid_series(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify corrupt persisted data is reported and never drawn as actuals."""
    start, end = _past_window()
    _seed_household(e2e_api, start, [1.2, 1.0])
    _seed_grid_flow(e2e_api, start)
    for path in e2e_server.data_directory.glob("grid-flow-*"):
        path.write_text("{corrupt\n", encoding="utf-8")

    page.goto(f"{e2e_server.base_url}/dashboard/")
    _load_range(page, start, end)

    expect(page.locator("#status")).to_have_text(
        "2 data points loaded. Invalid data withheld: Grid import and export."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-series-paths path")).to_have_count(1)
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#details")).to_contain_text("Invalid data withheld")
    expect(page.locator("#legend")).not_to_contain_text("Grid import")


def test_efficiency_tab_renders_battery_and_inverter_components(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify calculated component diagnostics are visible in the browser."""
    start, end = _window()
    retrieved_at = datetime.now(UTC)
    ProviderDataStore(e2e_server.data_directory).save(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
        BatteryEfficiencyData(
            schema_version="1",
            status="insufficient_data",
            inverter_charge_efficiency=0.95,
            inverter_discharge_efficiency=0.8,
            battery_efficiency=0.85,
            round_trip_efficiency=0.646,
            history_start=start,
            history_end=end - timedelta(hours=1),
            battery_throughput_kwh=5,
            charge_throughput_kwh=10,
            discharge_throughput_kwh=10,
            complete_cycle_count=1,
            unit="ratio",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency"
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=retrieved_at,
            defaulted_components=("inverter_charge_efficiency",),
        ),
    )

    page.goto(f"{e2e_server.base_url}/dashboard/")
    page.locator("#efficiency-tab").click()

    expect(page.locator("#efficiency-summary")).to_be_visible()
    expect(page.locator("#efficiency-metrics")).to_be_visible()
    expect(page.locator("#efficiency-metrics dt")).to_have_count(4)
    expect(page.locator("#efficiency-metrics")).to_contain_text(
        "Completed battery cycles"
    )
    expect(page.locator("#efficiency-metrics dd")).to_have_text(
        ["5.00 kWh", "10.00 kWh", "10.00 kWh", "1 cycles"]
    )
    expect(page.locator("#range-form")).to_be_hidden()
    expect(page.locator("#range-heading")).to_have_text("Complete retained history")
    expect(page.locator("#efficiency-summary dt")).to_have_count(4)
    expect(page.locator("#efficiency-summary .efficiency-value")).to_have_count(4)
    values = page.locator("#efficiency-summary .efficiency-value").all_text_contents()
    assert values == [
        "0.8500",
        "0.9500",
        "0.8000",
        "0.6460",
    ]
    expect(page.locator("#efficiency-summary")).to_contain_text("0.9500 ratio")
    expect(page.locator("svg#efficiency-chart")).to_have_count(0)
    expect(page.locator("#status")).to_have_text("4 efficiency values loaded.")
    expect(page.locator("#chart-note")).to_contain_text(
        "retained battery and inverter history"
    )
    expect(page.locator("#details")).not_to_contain_text("Freshness")
    expect(page.locator("#details")).to_contain_text("Coverage")
    expect(page.locator("#details")).to_contain_text("Retrieved")
    expect(page.locator("#efficiency-summary")).to_contain_text(
        "Battery round-trip efficiency"
    )
    expect(page.locator("#efficiency-summary")).to_contain_text(
        "Inverter charge efficiency"
    )
    expect(
        page.locator("#efficiency-summary .efficiency-status-calculated")
    ).to_have_count(2)
    expect(
        page.locator("#efficiency-summary .efficiency-status-defaulted")
    ).to_have_count(1)
    expect(
        page.locator("#efficiency-summary .efficiency-status-calculated_with_defaults")
    ).to_have_count(1)
    expect(page.locator("#efficiency-summary dd")).to_have_text(
        [
            "0.8500 ratio (calculated)",
            "0.9500 ratio (default)",
            "0.8000 ratio (calculated)",
            "0.6460 ratio (calculated with defaults)",
        ]
    )
    assert page.locator("#efficiency-summary").inner_text().count("(default)") == 1
    annotations = _assistive_annotations(page)
    assert len(annotations) == 1
    assert not any("default" in text.lower() for text in annotations)
    expect(page.locator("#legend")).to_be_empty()


def test_efficiency_tab_shows_one_annotation_for_each_fallback_status(
    e2e_server: LiveServer, page: Page
) -> None:
    """Keep unavailable and invalid fallbacks distinct without a default marker."""
    start, end = _window()
    retrieved_at = datetime.now(UTC)
    ProviderDataStore(e2e_server.data_directory).save(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
        BatteryEfficiencyData(
            schema_version="1",
            status="invalid",
            inverter_charge_efficiency=0.95,
            inverter_discharge_efficiency=0.8,
            battery_efficiency=0.95,
            round_trip_efficiency=0.722,
            history_start=start,
            history_end=end - timedelta(hours=1),
            battery_throughput_kwh=0,
            charge_throughput_kwh=0,
            discharge_throughput_kwh=10,
            complete_cycle_count=0,
            unit="ratio",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency"
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=retrieved_at,
            defaulted_components=(
                "battery_efficiency",
                "inverter_charge_efficiency",
                "round_trip_efficiency",
            ),
            component_statuses={
                "battery_efficiency": "unavailable",
                "inverter_charge_efficiency": "invalid",
                "inverter_discharge_efficiency": "calculated",
                "round_trip_efficiency": "invalid",
            },
        ),
    )

    page.goto(f"{e2e_server.base_url}/dashboard/")
    page.locator("#efficiency-tab").click()

    expect(page.locator("#efficiency-summary dd")).to_have_text(
        [
            "0.9500 ratio (unavailable)",
            "0.9500 ratio (invalid)",
            "0.8000 ratio (calculated)",
            "0.7220 ratio (invalid)",
        ]
    )
    assert "(default)" not in page.locator("#efficiency-summary").inner_text()
    annotations = _assistive_annotations(page)
    assert len(annotations) == 3
    assert not any("default" in text.lower() for text in annotations)
    expect(page.locator("#efficiency-metrics dd")).to_have_text(
        ["0.00 kWh", "0.00 kWh", "10.00 kWh", "0 cycles"]
    )


@pytest.mark.parametrize(
    ("throughput_kwh", "expected_texts"),
    [
        pytest.param(
            (350.123456, 1234.5678, 0.999),
            ["350.12 kWh", "1234.57 kWh", "1.00 kWh", "3 cycles"],
            id="fractional",
        ),
        pytest.param(
            (0.0, 0.0, 0.0),
            ["0.00 kWh", "0.00 kWh", "0.00 kWh", "3 cycles"],
            id="zero",
        ),
        pytest.param(
            (5.0, 10.0, 7.0),
            ["5.00 kWh", "10.00 kWh", "7.00 kWh", "3 cycles"],
            id="whole",
        ),
    ],
)
def test_efficiency_tab_formats_throughput_to_two_decimals(
    e2e_server: LiveServer,
    e2e_api: httpx.Client,
    page: Page,
    throughput_kwh: tuple[float, float, float],
    expected_texts: list[str],
) -> None:
    """Show every throughput with two decimals while the API keeps full precision."""
    start, end = _window()
    retrieved_at = datetime.now(UTC)
    battery, charge, discharge = throughput_kwh
    ProviderDataStore(e2e_server.data_directory).save(
        ProviderDataKey("battery-efficiency", "home-assistant", "battery_efficiency"),
        TypeAdapter(BatteryEfficiencyData),
        BatteryEfficiencyData(
            schema_version="1",
            status="ok",
            inverter_charge_efficiency=0.95,
            inverter_discharge_efficiency=0.8,
            battery_efficiency=0.85,
            round_trip_efficiency=0.646,
            history_start=start,
            history_end=end - timedelta(hours=1),
            battery_throughput_kwh=battery,
            charge_throughput_kwh=charge,
            discharge_throughput_kwh=discharge,
            complete_cycle_count=3,
            unit="ratio",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency"
            ),
            retrieved_at=retrieved_at,
            latest_observation_at=retrieved_at,
        ),
    )

    page.goto(f"{e2e_server.base_url}/dashboard/")
    page.locator("#efficiency-tab").click()

    expect(page.locator("#efficiency-metrics dt")).to_have_text(
        [
            "Battery throughput",
            "Inverter charge throughput",
            "Inverter discharge throughput",
            "Completed battery cycles",
        ]
    )
    expect(page.locator("#efficiency-metrics dd")).to_have_text(expected_texts)
    response = e2e_api.get(
        "/api/v1/dashboard/data",
        params={
            "scenario_kind": "efficiency",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    metrics = {item["id"]: item["value"] for item in response.json()["metrics"]}
    assert metrics == {
        "battery_throughput": battery,
        "inverter_charge_throughput": charge,
        "inverter_discharge_throughput": discharge,
        "completed_battery_cycles": 3,
    }


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
    price_ticks = page.locator("#price-labels .axis-label:not(.x-axis-label)")
    expect(price_ticks).to_have_count(5)
    tick_labels = price_ticks.all_text_contents()
    tick_values = [float(label) for label in tick_labels]
    assert min(tick_values) <= 0.08
    assert max(tick_values) >= 0.22
    assert max(tick_values) < 0.5
    assert all(len(label.rsplit(".", 1)[-1]) >= 2 for label in tick_labels)


def test_price_axis_handles_negative_flat_values(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a flat negative price series gets a finite focused domain."""
    start, end = _window()
    _seed_forecasts(
        e2e_server,
        start,
        import_prices=(-0.10, -0.10),
        export_prices=(-0.10, -0.10),
    )

    page.goto(f"{e2e_server.base_url}/dashboard/")
    page.locator("#forecast-tab").click()
    _load_range(page, start, end)

    price_ticks = page.locator("#price-labels .axis-label:not(.x-axis-label)")
    expect(price_ticks).to_have_count(5)
    tick_values = [float(label) for label in price_ticks.all_text_contents()]
    assert min(tick_values) < -0.10
    assert max(tick_values) > -0.10
    assert max(tick_values) < 0


def test_price_axis_handles_flat_zero_values(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a flat zero price series gets a domain on both sides of zero."""
    start, end = _window()
    _seed_forecasts(
        e2e_server,
        start,
        import_prices=(0.0, 0.0),
        export_prices=(0.0, 0.0),
    )

    page.goto(f"{e2e_server.base_url}/dashboard/")
    page.locator("#forecast-tab").click()
    _load_range(page, start, end)

    price_ticks = page.locator("#price-labels .axis-label:not(.x-axis-label)")
    expect(price_ticks).to_have_count(5)
    tick_values = [float(label) for label in price_ticks.all_text_contents()]
    assert min(tick_values) < 0 < max(tick_values)


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
    price_ticks = page.locator("#price-labels .axis-label:not(.x-axis-label)")
    expect(price_ticks).to_have_count(5)
    assert all(
        float(label) == float(label) for label in price_ticks.all_text_contents()
    )
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


def _excluded_stamp(timestamp: datetime) -> str:
    """Format a UTC timestamp the way the dashboard tables show it."""
    return timestamp.strftime("%Y-%m-%d %H:%MZ")


def _seed_excluded_history(server: LiveServer, start: datetime) -> None:
    """Persist history with excluded hours in every source, as an import would."""
    observation = datetime.now(UTC)
    store = ProviderDataStore(server.data_directory)
    unavailable = HourExclusion(
        start + timedelta(hours=1),
        (
            ExclusionCause.of(
                "unavailable",
                "sensor.household_energy reported an unknown or unavailable state.",
                "sensor.household_energy",
                [
                    ExcludedDataPoint(
                        start + timedelta(hours=1, minutes=30), "unavailable", "kWh"
                    )
                ],
            ),
        ),
    )
    decrease = HourExclusion(
        start + timedelta(hours=3),
        (
            ExclusionCause.of(
                "counter_decrease",
                "sensor.household_energy decreased.",
                "sensor.household_energy",
                [
                    ExcludedDataPoint(
                        start + timedelta(hours=3, minutes=10),
                        "699.5",
                        "kWh",
                        previous_timestamp=start + timedelta(hours=2, minutes=50),
                        previous_value=700.0,
                        value=699.5,
                        step_kwh=-0.5,
                        maximum_kwh=100.0,
                    )
                ],
            ),
        ),
    )
    store.save(
        ProviderDataKey("household-load", "home-assistant", "household_load"),
        TypeAdapter(HouseholdLoadData),
        HouseholdLoadData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            load_kw=(1.2, None, 1.0, None),
            unit="kW",
            source=SourceMetadata(
                provider="home-assistant", entity_id="household_load"
            ),
            retrieved_at=observation,
            latest_observation_at=observation,
            exclusions=(unavailable, decrease),
        ),
    )
    store.save(
        ProviderDataKey("grid-flow", "home-assistant", "grid_flow"),
        TypeAdapter(GridFlowData),
        GridFlowData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            import_kw=(0.8, None, 0.6, 0.5),
            export_kw=(0.0, None, 0.1, 0.2),
            unit="kW",
            source=SourceMetadata(provider="home-assistant", entity_id="grid_flow"),
            retrieved_at=observation,
            latest_observation_at=observation,
            exclusions=(
                HourExclusion(
                    start + timedelta(hours=1),
                    (
                        ExclusionCause.of(
                            "not_finite",
                            "sensor.grid_import reported a NaN or infinite state.",
                            "sensor.grid_import",
                            [
                                ExcludedDataPoint(
                                    start + timedelta(hours=1, minutes=5), "nan", "kWh"
                                )
                            ],
                        ),
                    ),
                ),
            ),
        ),
    )
    intervals: tuple[float | None, ...] = (0.5, None)
    store.save(
        ProviderDataKey(
            "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
        ),
        TypeAdapter(BatteryEfficiencyHistoryData),
        BatteryEfficiencyHistoryData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            battery_energy_in_kwh=intervals,
            battery_energy_out_kwh=intervals,
            inverter_charge_energy_in_kwh=intervals,
            inverter_charge_energy_out_kwh=intervals,
            inverter_discharge_energy_in_kwh=intervals,
            inverter_discharge_energy_out_kwh=intervals,
            state_of_charge_percent=(35.0, None, None),
            unit="kWh",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency_history"
            ),
            retrieved_at=observation,
            latest_observation_at=observation,
            exclusions=(
                HourExclusion(
                    start + timedelta(hours=1),
                    (
                        ExclusionCause.of(
                            "soc_out_of_range",
                            "sensor.battery_soc reported a state of charge outside "
                            "0 to 100 percent.",
                            "sensor.battery_soc",
                            [
                                ExcludedDataPoint(
                                    start + timedelta(hours=1, minutes=45), "150", "%"
                                )
                            ],
                        ),
                    ),
                ),
            ),
        ),
    )


def test_excluded_hours_tab_lists_every_excluded_hour_and_its_data_point(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify excluded hours are listed with their exact data points and reasons."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=8
    )
    end = start + timedelta(hours=4)
    _seed_excluded_history(e2e_server, start)

    page.goto(f"{e2e_server.base_url}/dashboard/")
    expect(page.locator("#status")).not_to_contain_text("Loading")
    page.locator("#excluded-tab").click()
    _load_range(page, start, end)

    expect(page.locator("#excluded-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#scenario-badge")).to_contain_text("Excluded hours")
    expect(page.locator("#excluded-content")).to_be_visible()
    expect(page.locator("#content")).to_be_hidden()
    expect(page.locator("#status")).to_have_text("4 excluded hours listed.")
    expect(page.locator("#excluded-empty")).to_be_hidden()

    expect(page.locator("#excluded-sources li")).to_have_text(
        [
            "Household load: Available",
            "Grid import and export: Available",
            "Battery efficiency: Available",
        ]
    )
    expect(page.locator("#excluded-summary li")).to_have_text(
        [
            "Household load · counter_decrease: 1 hour",
            "Household load · unavailable: 1 hour",
            "Grid import and export · not_finite: 1 hour",
            "Battery efficiency · soc_out_of_range: 1 hour",
        ]
    )

    rows = page.locator("#excluded-rows tr")
    expect(rows).to_have_count(4)
    hour_1 = _excluded_stamp(start + timedelta(hours=1))
    unavailable = rows.filter(has=page.locator('code:text-is("unavailable")'))
    expect(unavailable).to_have_count(1)
    expect(unavailable.locator("td").nth(0)).to_have_text(hour_1)
    expect(unavailable.locator("td").nth(1)).to_have_text("Household load")
    expect(unavailable.locator("td").nth(2)).to_have_text("sensor.household_energy")
    expect(unavailable.locator("td").nth(3)).to_contain_text(
        "reported an unknown or unavailable state"
    )
    expect(unavailable.locator("td").nth(4)).to_have_text(
        _excluded_stamp(start + timedelta(hours=1, minutes=30))
    )
    expect(unavailable.locator("td").nth(5)).to_have_text("unavailable")

    decrease = rows.filter(has=page.locator('code:text-is("counter_decrease")'))
    expect(decrease.locator("td").nth(5)).to_have_text("699.5")
    expect(decrease.locator("td").nth(6)).to_contain_text("previous 700 kWh")
    expect(decrease.locator("td").nth(6)).to_contain_text("step -0.5 kWh")
    expect(decrease.locator("td").nth(6)).to_contain_text("maximum 100 kWh")

    grid = rows.filter(has=page.locator('code:text-is("not_finite")'))
    expect(grid.locator("td").nth(1)).to_have_text("Grid import and export")
    expect(grid.locator("td").nth(2)).to_have_text("sensor.grid_import")
    expect(grid.locator("td").nth(5)).to_have_text("nan")
    battery = rows.filter(has=page.locator('code:text-is("soc_out_of_range")'))
    expect(battery.locator("td").nth(2)).to_have_text("sensor.battery_soc")
    expect(battery.locator("td").nth(5)).to_have_text("150")

    # A window without exclusions says so instead of showing an empty table.
    _load_range(page, start - timedelta(hours=4), start)
    expect(page.locator("#status")).to_have_text("0 excluded hours listed.")
    expect(page.locator("#excluded-empty")).to_have_text(
        "No hours were excluded in this window."
    )
    expect(page.locator("#excluded-table")).to_be_hidden()

    # The charts show the same hours as gaps, never as zeros.
    page.locator("#actuals-tab").click()
    _load_range(page, start, end)
    expect(page.locator("#excluded-content")).to_be_hidden()
    expect(page.locator("#status")).to_have_text(
        "Partial coverage is available. Missing intervals are shown as gaps."
    )
    # Household load has two valid hours; grid import and export three each.
    expect(page.locator("#power-points circle")).to_have_count(8)
    for path in page.locator("#power-series-paths path").all():
        assert (path.get_attribute("d") or "").count("M") == 2


def test_excluded_hours_tab_reports_sources_without_history(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a source that has no persisted history is named, not hidden."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=8
    )

    page.goto(f"{e2e_server.base_url}/dashboard/")
    expect(page.locator("#status")).not_to_contain_text("Loading")
    page.locator("#excluded-tab").click()
    _load_range(page, start, start + timedelta(hours=4))

    expect(page.locator("#excluded-content")).to_be_visible()
    expect(page.locator("#excluded-sources li")).to_have_count(3)
    expect(page.locator("#excluded-sources")).to_contain_text(
        "Household load: Unavailable "
        "(no persisted household-load data is available yet)"
    )
    expect(page.locator("#excluded-table")).to_be_hidden()
    # Nothing was checked, so it must not claim that nothing was excluded.
    expect(page.locator("#excluded-empty")).to_be_hidden()
