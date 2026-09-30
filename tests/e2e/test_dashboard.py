"""Browser end-to-end coverage for the unified dashboard."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Protocol
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.sync_api import FloatRect, Page, Request, Route, expect
from pydantic import TypeAdapter

from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    HourExclusion,
)
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    merge_battery_efficiency_history,
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
# The time zone in the e2e service configuration. The browser runs in another
# zone, so every expectation below proves the configured zone is the one shown.
DASHBOARD_ZONE: Final = ZoneInfo("Europe/Berlin")
BROWSER_ZONE: Final = ZoneInfo("America/New_York")


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
    """Format an instant as the dashboard-zone wall time of a datetime-local control."""
    return timestamp.astimezone(DASHBOARD_ZONE).strftime("%Y-%m-%dT%H:%M")


def _open_dashboard(page: Page, server: LiveServer) -> None:
    """Open the dashboard and wait for its settings and first load to finish."""
    page.goto(f"{server.base_url}/dashboard/")
    expect(page.locator("#status")).not_to_contain_text("Loading")


def _submit_range(page: Page, start: str, end: str) -> None:
    """Enter dashboard-zone wall times exactly as a user would and submit them."""
    page.locator("#start-date").fill(start)
    page.locator("#end-date").fill(end)
    page.locator("#range-form button[type=submit]").click()


def _data_requests(page: Page) -> list[dict[str, str]]:
    """Record the query of every dashboard data and excluded-hours request."""
    queries: list[dict[str, str]] = []

    def record(request: Request) -> None:
        url = urlparse(request.url)
        if url.path in (
            "/api/v1/dashboard/data",
            "/api/v1/dashboard/excluded-hours",
        ):
            queries.append(
                {key: values[0] for key, values in parse_qs(url.query).items()}
            )

    page.on("request", record)
    return queries


def _load_range(page: Page, start: datetime, end: datetime) -> None:
    """Submit one dashboard range and wait for the resulting status."""
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
            expires_at=effective_price_start + timedelta(hours=len(import_prices)),
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

    _open_dashboard(page, e2e_server)
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

    _open_dashboard(page, e2e_server)
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
    expect(page.locator("#legend")).to_have_count(0)
    expect(page.locator("#power-legend .legend-item")).to_have_text(
        ["Household load (kW)", "Grid import (kW)", "Grid export (kW)"]
    )
    expect(page.locator("#price-legend .legend-item")).to_have_text(
        ["Import price (EUR/kWh)", "Export price (EUR/kWh)"]
    )
    expect(page.locator("#battery-legend .legend-item")).to_have_text(
        ["Battery state of charge (%)"]
    )
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

    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)

    expect(page.locator("#status")).to_have_text(
        "2 data points loaded. Invalid data withheld: Grid import and export."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-series-paths path")).to_have_count(1)
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#details")).to_contain_text("Invalid data withheld")
    expect(page.locator("#power-legend")).not_to_contain_text("Grid import")
    expect(page.locator("#power-legend .legend-item")).to_have_count(1)


CHARTS: Final = ("power", "price", "battery")


def _seed_historic_assets(
    e2e_api: httpx.Client, e2e_server: LiveServer
) -> tuple[datetime, datetime]:
    """Seed actuals for the power, price, and battery charts and return their window."""
    start, end = _past_window()
    _seed_household(e2e_api, start, [1.2, 1.0])
    _seed_grid_flow(e2e_api, start)
    _seed_price_and_battery_history(e2e_server, start)
    return start, end


def _console_errors(page: Page) -> list[str]:
    """Record every console error and uncaught page error from now on."""
    errors: list[str] = []
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    return errors


def _box(page: Page, selector: str) -> FloatRect:
    """Return the on-screen bounding box of one element."""
    box = page.locator(selector).bounding_box()
    assert box is not None, f"{selector} is not rendered"
    return box


def _axis_ticks(page: Page, chart: str) -> list[str]:
    """Return the value-axis tick labels of one chart, bottom to top."""
    return page.locator(
        f"#{chart}-labels .axis-label:not(.x-axis-label)"
    ).all_text_contents()


def _line_paths(page: Page, chart: str) -> dict[str, str]:
    """Return the path data of every drawn line of one chart by series id."""
    paths: dict[str, str] = page.locator(f"#{chart}-series-paths path").evaluate_all(
        """(paths) => Object.fromEntries(
          paths.map((path) => [path.dataset.seriesId, path.getAttribute("d")])
        )"""
    )
    return paths


def _swatch_and_line_colors(page: Page, chart: str) -> dict[str, list[str]]:
    """Return each legend swatch color and its line's stroke color by series id."""
    colors: dict[str, list[str]] = page.locator(f"#{chart}-panel").evaluate(
        """(panel) => Object.fromEntries(
          [...panel.querySelectorAll(".legend-item")].map((entry) => {
            const id = entry.dataset.seriesId;
            const line = panel.querySelector(`path[data-series-id="${id}"]`);
            return [id, [
              getComputedStyle(entry.querySelector(".legend-swatch")).backgroundColor,
              getComputedStyle(line).stroke,
            ]];
          })
        )"""
    )
    return colors


def _reload_range(page: Page) -> None:
    """Submit the current range again and wait for the reload to finish."""
    page.locator("#range-form button[type=submit]").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")


def test_each_chart_has_its_own_legend_beside_it(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify every chart lists its own series with matching colors to its right."""
    start, end = _seed_historic_assets(e2e_api, e2e_server)
    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)

    expect(page.locator("#legend")).to_have_count(0)
    expected = {
        "power": ["Household load (kW)", "Grid import (kW)", "Grid export (kW)"],
        "price": ["Import price (EUR/kWh)", "Export price (EUR/kWh)"],
        "battery": ["Battery state of charge (%)"],
    }
    for chart in CHARTS:
        entries = page.locator(f"#{chart}-legend .legend-item")
        expect(entries).to_have_text(expected[chart])
        for index in range(len(expected[chart])):
            entry = entries.nth(index)
            expect(entry).to_have_attribute("aria-pressed", "true")
            assert entry.evaluate("(node) => node.tagName") == "BUTTON"
            assert entry.get_attribute("type") == "button"
        svg = _box(page, f"#{chart}-chart")
        legend = _box(page, f"#{chart}-legend")
        assert legend["x"] >= svg["x"] + svg["width"]
        colors = _swatch_and_line_colors(page, chart)
        assert len(colors) == len(expected[chart])
        assert all(swatch == stroke for swatch, stroke in colors.values())
        assert len({swatch for swatch, _ in colors.values()}) == len(colors)


def test_legend_entries_show_and_hide_lines_and_rescale_the_axis(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify a legend entry hides one line, rescales its axis, and restores it."""
    start, end = _seed_historic_assets(e2e_api, e2e_server)
    errors = _console_errors(page)
    queries = _data_requests(page)
    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)
    original_ticks = _axis_ticks(page, "power")
    original_paths = _line_paths(page, "power")
    original_labels: list[str] = page.locator("#power-points circle").evaluate_all(
        "(points) => points.map((point) => point.getAttribute('aria-label'))"
    )
    other_charts = {
        chart: (_axis_ticks(page, chart), _line_paths(page, chart))
        for chart in ("price", "battery")
    }
    queries.clear()

    grid_import = page.locator("#power-legend [data-series-id=grid_import_actual]")
    grid_import.click()

    expect(grid_import).to_have_attribute("aria-pressed", "false")
    expect(grid_import).to_have_css("text-decoration-line", "line-through")
    expect(grid_import.locator(".legend-swatch")).to_have_css("opacity", "0.3")
    expect(page.locator("#power-series-paths path")).to_have_count(2)
    expect(
        page.locator("#power-series-paths [data-series-id=grid_import_actual]")
    ).to_have_count(0)
    expect(page.locator("#power-points circle")).to_have_count(4)
    remaining: list[str] = page.locator("#power-points circle").evaluate_all(
        "(points) => points.map((point) => point.getAttribute('aria-label'))"
    )
    assert remaining == [
        label for label in original_labels if not label.startswith("Grid import")
    ]
    # Grid import holds the largest power values, so the axis shrinks without it.
    assert float(_axis_ticks(page, "power")[-1]) < float(original_ticks[-1])
    assert set(_line_paths(page, "power")) == {
        "household_load_actual",
        "grid_export_actual",
    }
    for chart, (ticks, paths) in other_charts.items():
        assert _axis_ticks(page, chart) == ticks
        assert _line_paths(page, chart) == paths
    expect(page.locator("#power-legend .legend-item")).to_have_count(3)
    assert queries == [], "toggling a line must not refetch data"

    grid_import.click()

    expect(grid_import).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#power-points circle")).to_have_count(6)
    assert _line_paths(page, "power") == original_paths
    assert _axis_ticks(page, "power") == original_ticks

    entries = page.locator("#power-legend .legend-item")
    for index in range(3):
        entries.nth(index).click()
    expect(page.locator("#power-series-paths path")).to_have_count(0)
    expect(page.locator("#power-points circle")).to_have_count(0)
    expect(page.locator("#power-chart")).to_be_visible()
    expect(page.locator("#power-axis-unit")).to_have_text("kW")
    ticks = _axis_ticks(page, "power")
    assert len(ticks) == 5
    assert (ticks[0], ticks[-1]) == ("0.0", "1.0")
    expect(page.locator("#power-labels .x-axis-label")).to_have_count(2)
    expect(entries).to_have_count(3)
    for index in range(3):
        expect(entries.nth(index)).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#price-series-paths path")).to_have_count(2)

    for index in range(3):
        entries.nth(index).click()
    expect(page.locator("#power-series-paths path")).to_have_count(3)
    assert _line_paths(page, "power") == original_paths
    assert _axis_ticks(page, "power") == original_ticks
    assert errors == []


def test_hidden_series_stay_hidden_until_the_page_is_reloaded(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify hidden lines survive range and tab changes, not a page reload."""
    start, end = _seed_historic_assets(e2e_api, e2e_server)
    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)
    grid_import = page.locator("#power-legend [data-series-id=grid_import_actual]")
    grid_import.click()
    expect(page.locator("#power-series-paths path")).to_have_count(2)

    _reload_range(page)
    expect(grid_import).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#power-series-paths path")).to_have_count(2)
    expect(page.locator("#power-points circle")).to_have_count(4)

    page.locator("#excluded-tab").click()
    expect(page.locator("#excluded-content")).to_be_visible()
    expect(page.locator(".chart-legend:visible")).to_have_count(0)
    page.locator("#actuals-tab").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")
    expect(grid_import).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#power-series-paths path")).to_have_count(2)
    assert "grid_import_actual" not in _line_paths(page, "power")

    _open_dashboard(page, e2e_server)
    expect(page.locator("#power-legend .legend-item")).to_have_count(3)
    expect(grid_import).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#power-series-paths path")).to_have_count(3)


def test_legend_entries_are_operable_from_the_keyboard(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify Tab reaches legend entries and Enter and Space toggle their lines."""
    start, end = _seed_historic_assets(e2e_api, e2e_server)
    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)
    entries = page.locator("#power-legend .legend-item")

    page.locator("#power-points circle").last.focus()
    page.keyboard.press("Tab")
    expect(entries.nth(0)).to_be_focused()

    page.keyboard.press("Enter")
    expect(entries.nth(0)).to_have_attribute("aria-pressed", "false")
    expect(entries.nth(0)).to_be_focused()
    expect(page.locator("#power-series-paths path")).to_have_count(2)

    page.keyboard.press("Space")
    expect(entries.nth(0)).to_have_attribute("aria-pressed", "true")
    expect(entries.nth(0)).to_be_focused()
    expect(page.locator("#power-series-paths path")).to_have_count(3)

    page.keyboard.press("Tab")
    expect(entries.nth(1)).to_be_focused()
    page.keyboard.press("Space")
    expect(entries.nth(1)).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#power-series-paths path")).to_have_count(2)


def test_tooltip_names_its_series_and_stays_inside_its_chart(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify the tooltip names its line and stays clear of the legend."""
    start, end = _seed_historic_assets(e2e_api, e2e_server)
    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)

    for chart in CHARTS:
        # The last point of a chart is the one nearest its right edge.
        point = page.locator(f"#{chart}-points circle").last
        label = point.get_attribute("aria-label") or ""
        name, remainder = label.split(", ", 1)
        stamp, value = remainder.split(": ", 1)
        point.hover()

        tooltip = page.locator("#point-tooltip")
        expect(tooltip).to_have_text(f"{name} · {stamp} · {value}")
        box = _box(page, "#point-tooltip")
        svg = _box(page, f"#{chart}-chart")
        legend = _box(page, f"#{chart}-legend")
        assert box["x"] >= svg["x"]
        assert box["x"] + box["width"] <= svg["x"] + svg["width"]
        assert box["y"] >= svg["y"]
        assert box["y"] + box["height"] <= svg["y"] + svg["height"]
        assert box["x"] + box["width"] <= legend["x"]
        assert (
            page.locator(f"#{chart}-points circle").last.get_attribute("aria-label")
            == label
        )


def test_legends_are_placed_below_their_charts_on_narrow_screens(
    e2e_api: httpx.Client, e2e_server: LiveServer, page: Page
) -> None:
    """Verify each legend sits under its chart and inside the card at 600px."""
    page.set_viewport_size({"width": 600, "height": 900})
    start, end = _seed_historic_assets(e2e_api, e2e_server)
    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)

    card = _box(page, ".chart-card")
    for chart in CHARTS:
        svg = _box(page, f"#{chart}-chart")
        legend = _box(page, f"#{chart}-legend")
        assert legend["y"] >= svg["y"] + svg["height"]
        assert legend["x"] >= card["x"]
        assert legend["x"] + legend["width"] <= card["x"] + card["width"]
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )


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

    _open_dashboard(page, e2e_server)
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
    expect(page.locator(".chart-legend .legend-item")).to_have_count(0)
    expect(page.locator(".chart-panel:visible")).to_have_count(0)


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

    _open_dashboard(page, e2e_server)
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

    _open_dashboard(page, e2e_server)
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
    """Verify forecast data drives separate charts, each with its own legend."""
    start, end = _window()
    _seed_forecasts(e2e_server, start)

    _open_dashboard(page, e2e_server)
    page.locator("#forecast-tab").click()
    _load_range(page, start, end)

    expect(page.locator("#scenario-badge")).to_contain_text("Forecast inputs")
    expect(page.locator("#chart-heading")).to_have_text("PV and price forecast")
    expect(page.locator("#power-chart")).to_be_visible()
    expect(page.locator("#price-chart")).to_be_visible()
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#price-points circle")).to_have_count(4)
    expect(page.locator("#legend")).to_have_count(0)
    expect(page.locator("#power-legend .legend-item")).to_have_text(
        ["PV generation (kW)"]
    )
    expect(page.locator("#price-legend .legend-item")).to_have_text(
        ["Import price (EUR/kWh)", "Export price (EUR/kWh)"]
    )
    expect(page.locator("#battery-panel")).to_be_hidden()
    expect(page.locator("#battery-legend .legend-item")).to_have_count(0)
    import_price = page.locator("#price-legend [data-series-id=import_price_forecast]")
    expect(import_price).to_have_attribute("aria-pressed", "true")
    import_price.click()
    expect(import_price).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#price-series-paths path")).to_have_count(1)
    expect(page.locator("#price-points circle")).to_have_count(2)
    expect(page.locator("#power-series-paths path")).to_have_count(1)
    import_price.click()
    expect(page.locator("#price-series-paths path")).to_have_count(2)
    expect(page.locator("#price-points circle")).to_have_count(4)
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

    _open_dashboard(page, e2e_server)
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

    _open_dashboard(page, e2e_server)
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
    _open_dashboard(page, e2e_server)
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

    _open_dashboard(page, e2e_server)
    _load_range(page, start - timedelta(hours=1), end + timedelta(hours=1))

    expect(page.locator("#start-date")).to_have_value(_input_value(start))
    expect(page.locator("#end-date")).to_have_value(_input_value(end))
    expect(page.locator("#status")).to_have_text("2 data points loaded.")


def test_unavailable_actuals_are_presented_as_an_error(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a real unavailable dashboard response is visible to the user."""
    _open_dashboard(page, e2e_server)

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
    _open_dashboard(page, e2e_server)

    expect(page.locator("#status")).to_have_text("dashboard backend is unavailable")
    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#content")).to_be_hidden()


def test_partial_coverage_is_shown_as_a_gap(e2e_server: LiveServer, page: Page) -> None:
    """Verify missing hourly observations remain gaps rather than zeros."""
    start, end = _window()
    _seed_forecasts(e2e_server, start, price_start=start + timedelta(hours=1))

    _open_dashboard(page, e2e_server)
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

    _open_dashboard(page, e2e_server)
    _load_range(page, start, end)

    expect(page.locator("#status")).to_have_text(
        "Data is available, but its freshness window has expired."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-chart")).to_be_visible()


def _excluded_stamp(timestamp: datetime) -> str:
    """Format an instant the way the dashboard shows it in its configured zone.

    A local time that the clock shows twice, the hour after clocks go back, also
    carries its UTC offset so the two occurrences differ.
    """
    local = timestamp.astimezone(DASHBOARD_ZONE)
    text = local.strftime("%Y-%m-%d %H:%M")
    offset = local.utcoffset()
    assert offset is not None
    if local.replace(fold=0).utcoffset() == local.replace(fold=1).utcoffset():
        return text
    minutes = int(offset.total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    return f"{text}{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _seed_excluded_history(server: LiveServer, start: datetime) -> None:
    """Persist history with excluded hours in every source, as an import would.

    Every source first holds hours 0 to 3 and is then refreshed with hour 5 only,
    as after Home Assistant purged the hours between: hour 4 becomes an excluded
    hour that the production merge creates.
    """
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
        ProviderDataKey("household-load", "home-assistant", "household_load"),
        TypeAdapter(HouseholdLoadData),
        HouseholdLoadData(
            schema_version="1",
            start_time=start + timedelta(hours=5),
            interval_minutes=60,
            load_kw=(2.0,),
            unit="kW",
            source=SourceMetadata(
                provider="home-assistant", entity_id="household_load"
            ),
            retrieved_at=observation,
            latest_observation_at=observation,
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
    store.save(
        ProviderDataKey("grid-flow", "home-assistant", "grid_flow"),
        TypeAdapter(GridFlowData),
        GridFlowData(
            schema_version="1",
            start_time=start + timedelta(hours=5),
            interval_minutes=60,
            import_kw=(0.4,),
            export_kw=(0.3,),
            unit="kW",
            source=SourceMetadata(provider="home-assistant", entity_id="grid_flow"),
            retrieved_at=observation,
            latest_observation_at=observation,
        ),
    )

    def efficiency(
        first_hour: int,
        intervals: tuple[float | None, ...],
        state_of_charge: tuple[float | None, ...],
        exclusions: tuple[HourExclusion, ...] = (),
    ) -> BatteryEfficiencyHistoryData:
        return BatteryEfficiencyHistoryData(
            schema_version="1",
            start_time=start + timedelta(hours=first_hour),
            interval_minutes=60,
            battery_energy_in_kwh=intervals,
            battery_energy_out_kwh=intervals,
            inverter_charge_energy_in_kwh=intervals,
            inverter_charge_energy_out_kwh=intervals,
            inverter_discharge_energy_in_kwh=intervals,
            inverter_discharge_energy_out_kwh=intervals,
            state_of_charge_percent=state_of_charge,
            unit="kWh",
            source=SourceMetadata(
                provider="home-assistant", entity_id="battery_efficiency_history"
            ),
            retrieved_at=observation,
            latest_observation_at=observation,
            exclusions=exclusions,
        )

    soc_out_of_range = HourExclusion(
        start + timedelta(hours=1),
        (
            ExclusionCause.of(
                "soc_out_of_range",
                "sensor.battery_soc reported a state of charge outside "
                "0 to 100 percent.",
                "sensor.battery_soc",
                [ExcludedDataPoint(start + timedelta(hours=1, minutes=45), "150", "%")],
            ),
        ),
    )
    store.save(
        ProviderDataKey(
            "battery-efficiency-history", "home-assistant", "battery_efficiency_history"
        ),
        TypeAdapter(BatteryEfficiencyHistoryData),
        merge_battery_efficiency_history(
            efficiency(
                0,
                (0.5, None, 0.5, 0.5),
                (35.0, None, None, 40.0, 45.0),
                (soc_out_of_range,),
            ),
            efficiency(5, (0.5,), (50.0, 55.0)),
        ),
    )


def test_excluded_hours_tab_lists_every_excluded_hour_and_its_data_point(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify excluded hours are listed with their exact data points and reasons.

    Hour 4 is an hour that Home Assistant no longer held when the sources were
    refreshed, so every source lists it as ``history_unavailable``.
    """
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=8
    )
    end = start + timedelta(hours=6)
    _seed_excluded_history(e2e_server, start)

    _open_dashboard(page, e2e_server)
    expect(page.locator("#status")).not_to_contain_text("Loading")
    page.locator("#excluded-tab").click()
    _load_range(page, start, end)

    expect(page.locator("#excluded-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#scenario-badge")).to_contain_text("Excluded hours")
    expect(page.locator("#excluded-content")).to_be_visible()
    expect(page.locator("#content")).to_be_hidden()
    expect(page.locator("#status")).to_have_text("7 excluded hours listed.")
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
            "Household load · history_unavailable: 1 hour",
            "Household load · unavailable: 1 hour",
            "Grid import and export · history_unavailable: 1 hour",
            "Grid import and export · not_finite: 1 hour",
            "Battery efficiency · history_unavailable: 1 hour",
            "Battery efficiency · soc_out_of_range: 1 hour",
        ]
    )

    rows = page.locator("#excluded-rows tr")
    expect(rows).to_have_count(7)
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

    # The gap hour of each source names the missing range and has no entity and no
    # data point, because nothing was recorded for it.
    unavailable_gap = rows.filter(
        has=page.locator('code:text-is("history_unavailable")')
    )
    expect(unavailable_gap).to_have_count(3)
    expect(unavailable_gap.locator("td:nth-child(1)")).to_have_text(
        [_excluded_stamp(start + timedelta(hours=4))] * 3
    )
    expect(unavailable_gap.locator("td:nth-child(2)")).to_have_text(
        ["Household load", "Grid import and export", "Battery efficiency"]
    )
    for column in (3, 5, 6):
        expect(unavailable_gap.locator(f"td:nth-child({column})")).to_have_text(
            ["-"] * 3
        )
    expect(unavailable_gap.locator("td:nth-child(7)")).to_have_text([""] * 3)
    expect(unavailable_gap.locator("td:nth-child(4) .excluded-message")).to_have_text(
        [
            "The provider holds no history from "
            f"{start + timedelta(hours=4):%Y-%m-%dT%H:%M:%S}+00:00 until "
            f"{start + timedelta(hours=5):%Y-%m-%dT%H:%M:%S}+00:00 (1 hour), "
            "so this hour cannot be imported."
        ]
        * 3
    )

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
    # The gap hour holds no value either: household load has three valid hours
    # (0, 2, and 5), grid import and export four each (0, 2, 3, and 5), and the
    # gap splits every series line into three segments.
    expect(page.locator("#power-points circle")).to_have_count(11)
    for path in page.locator("#power-series-paths path").all():
        assert (path.get_attribute("d") or "").count("M") == 3


def test_excluded_hours_tab_reports_sources_without_history(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a source that has no persisted history is named, not hidden."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=8
    )

    _open_dashboard(page, e2e_server)
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


def _seed_repeated_hour_exclusions(server: LiveServer, start: datetime) -> None:
    """Exclude the two hours that show the same local time when clocks go back.

    ``start`` is the first of six hours; the third and fourth are excluded.
    """
    observation = datetime.now(UTC)

    def exclusion(hour: datetime) -> HourExclusion:
        return HourExclusion(
            hour,
            (
                ExclusionCause.of(
                    "unavailable",
                    "sensor.household_energy reported an unknown or unavailable state.",
                    "sensor.household_energy",
                    [
                        ExcludedDataPoint(
                            hour + timedelta(minutes=30), "unavailable", "kWh"
                        )
                    ],
                ),
            ),
        )

    ProviderDataStore(server.data_directory).save(
        ProviderDataKey("household-load", "home-assistant", "household_load"),
        TypeAdapter(HouseholdLoadData),
        HouseholdLoadData(
            schema_version="1",
            start_time=start,
            interval_minutes=60,
            load_kw=(1.0, 1.0, None, None, 1.0, 1.0),
            unit="kW",
            source=SourceMetadata(
                provider="home-assistant", entity_id="household_load"
            ),
            retrieved_at=observation,
            latest_observation_at=observation,
            exclusions=(
                exclusion(start + timedelta(hours=2)),
                exclusion(start + timedelta(hours=3)),
            ),
        ),
    )


def test_dashboard_settings_report_the_configured_zone(e2e_api: httpx.Client) -> None:
    """Keep the e2e expectations tied to the zone the service really runs with."""
    response = e2e_api.get("/api/v1/dashboard/settings")

    assert response.status_code == 200, response.text
    assert response.json() == {"timezone": DASHBOARD_ZONE.key}


def test_dashboard_names_the_configured_zone_instead_of_utc(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify headings, labels, and help name the zone; UTC only labels the API."""
    _open_dashboard(page, e2e_server)

    expect(page.locator("#zone-name")).to_have_text("Europe/Berlin")
    expect(page.locator("#range-heading")).to_have_text(
        "Choose a time window in Europe/Berlin"
    )
    expect(page.locator("#start-label")).to_have_text("Start (Europe/Berlin)")
    expect(page.locator("#end-label")).to_have_text("End (Europe/Berlin, exclusive)")
    expect(page.locator("#range-help")).to_contain_text("Europe/Berlin")
    expect(page.locator(".controls")).not_to_contain_text("UTC")
    assert page.locator("main").inner_text().count("UTC") == 1
    expect(page.locator(".lede")).to_contain_text("API timestamps are UTC")

    page.locator("#excluded-tab").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")
    expect(page.locator("#range-heading")).to_have_text(
        "Choose a time window in Europe/Berlin"
    )
    expect(page.locator("#range-help")).not_to_contain_text("UTC")
    expect(page.locator("#excluded-hour-heading")).to_have_text("Hour (Europe/Berlin)")
    expect(page.locator("#excluded-point-heading")).to_have_text(
        "Data point (Europe/Berlin)"
    )

    page.locator("#forecast-tab").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")
    expect(page.locator("#range-help")).not_to_contain_text("UTC")


def test_forecast_prices_show_the_next_local_day_in_the_configured_zone(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify prices crossing local midnight read in Berlin time, not New York time."""
    # 19:00Z is 21:00 in Berlin and 15:00 in New York; six hours reach 02:00 on
    # the next local day, past local midnight at 22:00Z.
    start = datetime(2026, 9, 30, 19, tzinfo=UTC)
    prices = (0.10, 0.12, 0.14, 0.16, 0.18, 0.20)
    _seed_forecasts(
        e2e_server,
        start,
        import_prices=prices,
        export_prices=tuple(price - 0.05 for price in prices),
    )
    queries = _data_requests(page)
    _open_dashboard(page, e2e_server)
    page.locator("#forecast-tab").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")
    queries.clear()

    # A window wider than the coverage is corrected once to the coverage, which
    # the controls then show as Berlin wall times.
    _load_range(page, start - timedelta(hours=1), start + timedelta(hours=8))

    expect(page.locator("#start-date")).to_have_value("2026-09-30T21:00")
    expect(page.locator("#end-date")).to_have_value("2026-10-01T03:00")
    assert queries == [
        {
            "start_time": "2026-09-30T18:00:00Z",
            "end_time": "2026-10-01T03:00:00Z",
            "scenario_kind": "forecast",
        },
        {
            "start_time": "2026-09-30T19:00:00Z",
            "end_time": "2026-10-01T01:00:00Z",
            "scenario_kind": "forecast",
        },
    ]

    expect(page.locator("#price-chart")).to_be_visible()
    points = page.locator("#price-points circle")
    expect(points).to_have_count(12)
    for hour in range(6):
        label = points.nth(hour).get_attribute("aria-label") or ""
        assert _excluded_stamp(start + timedelta(hours=hour)) in label
    slot = points.nth(2)
    assert slot.get_attribute("aria-label") == (
        "Import price, 2026-09-30 23:00: 0.14 EUR/kWh"
    )
    assert "17:00" not in (slot.get_attribute("aria-label") or "")
    assert "2026-10-01 00:00" in (points.nth(3).get_attribute("aria-label") or "")
    slot.focus()
    expect(page.locator("#point-tooltip")).to_have_text(
        "Import price · 2026-09-30 23:00 · 0.14 EUR/kWh"
    )
    ticks = page.locator("#price-labels .x-axis-label")
    expect(ticks).to_have_text(
        [_excluded_stamp(start + timedelta(hours=hour)) for hour in (0, 1, 3, 4, 5)]
    )
    expect(page.locator("#details")).to_contain_text(
        "2026-09-30 21:00 to 2026-09-30 23:00"
    )


def test_range_controls_request_whole_utc_hours_for_the_configured_zone(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify Berlin wall times in the controls become whole UTC hours in requests."""
    queries = _data_requests(page)
    _open_dashboard(page, e2e_server)
    queries.clear()

    _submit_range(page, "2026-09-30T00:00", "2026-10-01T00:00")
    expect(page.locator("#status")).not_to_contain_text("Loading")
    page.locator("#excluded-tab").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")

    window = {
        "start_time": "2026-09-29T22:00:00Z",
        "end_time": "2026-09-30T22:00:00Z",
    }
    assert queries == [{**window, "scenario_kind": "actual"}, window]


@pytest.mark.parametrize(
    ("day", "start_utc", "end_utc", "hours"),
    [
        ("2026-03-29", "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z", 23),
        ("2026-06-15", "2026-06-14T22:00:00Z", "2026-06-15T22:00:00Z", 24),
        ("2026-10-25", "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z", 25),
    ],
)
def test_default_range_is_the_local_day_even_when_clocks_change(
    e2e_server: LiveServer,
    page: Page,
    day: str,
    start_utc: str,
    end_utc: str,
    hours: int,
) -> None:
    """Verify the default range spans local midnight to local midnight."""
    page.clock.set_fixed_time(datetime.fromisoformat(f"{day}T12:00:00+00:00"))
    queries = _data_requests(page)
    _open_dashboard(page, e2e_server)

    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    expect(page.locator("#start-date")).to_have_value(f"{day}T00:00")
    expect(page.locator("#end-date")).to_have_value(f"{next_day}T00:00")
    assert [(q["start_time"], q["end_time"]) for q in queries] == [(start_utc, end_utc)]
    parse = datetime.fromisoformat
    span = parse(end_utc.replace("Z", "+00:00")) - parse(
        start_utc.replace("Z", "+00:00")
    )
    assert span == timedelta(hours=hours)


def test_repeated_local_hour_is_two_chart_points_and_two_excluded_rows(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify the hour shown twice when clocks go back stays distinguishable."""
    # Clocks go back at 01:00Z on 2026-10-25, so 02:00 to 03:00 local time occurs
    # at 00:00Z under CEST and again at 01:00Z under CET.
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    end = start + timedelta(hours=6)
    _seed_forecasts(
        e2e_server,
        start,
        import_prices=(0.10, 0.12, 0.14, 0.16, 0.18, 0.20),
        export_prices=(0.05, 0.06, 0.07, 0.08, 0.09, 0.10),
    )
    _seed_repeated_hour_exclusions(e2e_server, start)
    _open_dashboard(page, e2e_server)

    page.locator("#forecast-tab").click()
    _load_range(page, start, end)
    points = page.locator("#price-points circle")
    expect(points).to_have_count(12)
    labels = [points.nth(index).get_attribute("aria-label") or "" for index in range(6)]
    assert labels[2] == "Import price, 2026-10-25 02:00+02:00: 0.14 EUR/kWh"
    assert labels[3] == "Import price, 2026-10-25 02:00+01:00: 0.16 EUR/kWh"
    assert "2026-10-25 01:00:" in labels[1]
    assert "2026-10-25 03:00:" in labels[4]
    assert len(set(labels)) == 6

    page.locator("#excluded-tab").click()
    _load_range(page, start, end)
    expect(page.locator("#status")).to_have_text("2 excluded hours listed.")
    rows = page.locator("#excluded-rows tr")
    expect(rows).to_have_count(2)
    expect(rows.locator("td:nth-child(1)")).to_have_text(
        ["2026-10-25 02:00+02:00", "2026-10-25 02:00+01:00"]
    )
    expect(rows.locator("td:nth-child(5)")).to_have_text(
        ["2026-10-25 02:30+02:00", "2026-10-25 02:30+01:00"]
    )


def test_range_controls_resolve_repeated_and_skipped_local_times(
    e2e_server: LiveServer, page: Page
) -> None:
    """Verify a repeated time means its first occurrence and a skipped one moves on."""
    queries = _data_requests(page)
    _open_dashboard(page, e2e_server)
    queries.clear()

    # 02:00 occurs at 00:00Z and at 01:00Z on 2026-10-25; the first one is used.
    _submit_range(page, "2026-10-25T02:00", "2026-10-25T03:00")
    expect(page.locator("#status")).not_to_contain_text("Loading")
    # 02:00 never occurs on 2026-03-29: clocks jump from 02:00 to 03:00, so it
    # moves ahead by the one-hour gap.
    _submit_range(page, "2026-03-29T02:00", "2026-03-29T04:00")
    expect(page.locator("#status")).not_to_contain_text("Loading")

    assert [(q["start_time"], q["end_time"]) for q in queries] == [
        ("2026-10-25T00:00:00Z", "2026-10-25T02:00:00Z"),
        ("2026-03-29T01:00:00Z", "2026-03-29T02:00:00Z"),
    ]


@pytest.mark.parametrize(
    ("respond", "message"),
    [
        (
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"detail": "settings backend is unavailable"}),
            ),
            "The dashboard settings could not be loaded (HTTP 503)",
        ),
        (
            lambda route: route.abort(),
            "The dashboard settings could not be loaded; check the connection",
        ),
        (
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"timezone": "Foo/Bar"}),
            ),
            "This browser does not support the configured time zone Foo/Bar",
        ),
    ],
    ids=["http-error", "network-error", "zone-rejected-by-browser"],
)
def test_unusable_time_zone_is_reported_and_no_data_is_requested(
    e2e_server: LiveServer,
    page: Page,
    respond: Callable[[Route], object],
    message: str,
) -> None:
    """Verify the dashboard never falls back to a guessed zone."""
    page.route("**/api/v1/dashboard/settings", respond)
    queries = _data_requests(page)

    page.goto(f"{e2e_server.base_url}/dashboard/")

    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#status")).to_contain_text(message)
    expect(page.locator("#start-date")).to_be_disabled()
    expect(page.locator("#start-date")).to_have_value("")
    for tab in ("#forecast-tab", "#excluded-tab", "#actuals-tab"):
        page.locator(tab).click()
        expect(page.locator("#status")).to_have_class("status error")
        expect(page.locator("#status")).to_contain_text(message)
    assert queries == []
