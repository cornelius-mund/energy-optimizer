"""Browser end-to-end coverage for the unified dashboard."""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from playwright.sync_api import FloatRect, Locator, Page, Request, Route, expect
from pydantic import TypeAdapter

from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
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

# The time zone in the e2e service configuration. The browser runs in another
# zone, so every expectation below proves the configured zone is the one shown.
DASHBOARD_ZONE: Final = ZoneInfo("Europe/Berlin")

Window = tuple[datetime, datetime]

# The legend entries of the charts that the historic tab draws, by chart.
HISTORIC_LEGENDS: Final = {
    "power": ["Household load (kW)", "Grid import (kW)", "Grid export (kW)"],
    "price": ["Import price (EUR/kWh)", "Export price (EUR/kWh)"],
    "battery": ["Battery state of charge (%)"],
}
CHARTS: Final = tuple(HISTORIC_LEGENDS)
# The accessible names of the power and price charts while all their lines show.
POWER_NAME: Final = "Household load and Grid import and Grid export (kW)"
PRICE_NAME: Final = "Import price and Export price (EUR/kWh)"


def _hour(hours_from_now: int) -> datetime:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return now + timedelta(hours=hours_from_now)


def _window(hours_from_now: int = 1) -> Window:
    """Return a two-hour UTC window stable for the duration of one test.

    The default lies in the future for forecasts; actuals only cover the past.
    """
    start = _hour(hours_from_now)
    return start, start + timedelta(hours=2)


def _input_value(timestamp: datetime) -> str:
    """Format an instant as the dashboard-zone wall time of a datetime-local control."""
    return timestamp.astimezone(DASHBOARD_ZONE).strftime("%Y-%m-%dT%H:%M")


def _stamp(timestamp: datetime) -> str:
    """Format an instant the way the dashboard shows it in its configured zone.

    A local time that the clock shows twice, the hour after clocks go back, also
    carries its UTC offset so the two occurrences differ.
    """
    local = timestamp.astimezone(DASHBOARD_ZONE)
    text = local.strftime("%Y-%m-%d %H:%M")
    repeated = local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
    return text + local.isoformat()[-6:] if repeated else text


def _submit_range(page: Page, start: str, end: str) -> None:
    """Enter dashboard-zone wall times exactly as a user would and submit them."""
    page.locator("#start-date").fill(start)
    page.locator("#end-date").fill(end)
    page.locator("#range-form button[type=submit]").click()


def _load_range(page: Page, window: Window, tab: str = "") -> None:
    """Submit one range, on the named tab if any, and wait for the load to finish."""
    if tab:
        page.locator(f"#{tab}-tab").click()
    start, end = window
    _submit_range(page, _input_value(start), _input_value(end))
    expect(page.locator("#status")).not_to_contain_text("Loading")


def _open_dashboard(page: Page, tab: str = "", window: Window | None = None) -> None:
    """Open the dashboard, wait for its first load, then select a tab and a range."""
    page.goto("/dashboard/")
    expect(page.locator("#status")).not_to_contain_text("Loading")
    if window:
        _load_range(page, window, tab)
    elif tab:
        page.locator(f"#{tab}-tab").click()


def _data_requests(page: Page) -> list[dict[str, str]]:
    """Record the query of every dashboard data and excluded-hours request."""
    queries: list[dict[str, str]] = []

    def record(request: Request) -> None:
        url = urlparse(request.url)
        if url.path in ("/api/v1/dashboard/data", "/api/v1/dashboard/excluded-hours"):
            queries.append({k: v[0] for k, v in parse_qs(url.query).items()})

    page.on("request", record)
    return queries


def _console_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda error: errors.append(str(error)))
    return errors


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


def _box(page: Page, selector: str) -> FloatRect:
    box = page.locator(selector).bounding_box()
    assert box is not None, f"{selector} is not rendered"
    return box


def _axis_ticks(page: Page, chart: str, count: int | None = None) -> list[str]:
    """Return the value-axis tick labels of one chart, bottom to top.

    With a ``count``, first wait until the chart draws exactly that many.
    """
    ticks = page.locator(f"#{chart}-labels .axis-label:not(.x-axis-label)")
    if count is not None:
        expect(ticks).to_have_count(count)
    return ticks.all_text_contents()


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


def _expect_no_native_chart_tooltip(page: Page) -> None:
    """Expect that nothing in any chart makes the browser draw its own tooltip.

    A native tooltip is browser interface and not part of the page's DOM, so this
    asserts its causes: a `<title>` element or a `title` attribute in a chart.
    """
    for chart in CHARTS:
        svg = page.locator(f"#{chart}-chart")
        expect(svg.locator("title")).to_have_count(0)
        expect(svg.locator("[title]")).to_have_count(0)
        assert svg.get_attribute("title") is None


def _expect_chart_semantics(
    page: Page, chart: str, name: str, description: str
) -> None:
    """Expect a chart to be an image with the given accessible name and description."""
    svg = page.locator(f"#{chart}-chart")
    expect(svg).to_have_role("img")
    expect(svg).to_have_accessible_name(name)
    expect(svg).to_have_accessible_description(description)


def _reason_rows(rows: Locator, reason: str) -> Locator:
    return rows.filter(has=rows.page.locator(f'code:text-is("{reason}")'))


def _expect_cells(row: Locator, cells: dict[int, str]) -> None:
    for index, text in cells.items():
        expect(row.locator("td").nth(index)).to_have_text(text)


def _source(entity_id: str, provider: str = "home-assistant") -> SourceMetadata:
    return SourceMetadata(provider=provider, entity_id=entity_id)


def _save(store: ProviderDataStore, data_type: str, data: Any) -> None:
    """Persist a record under the key that its own source metadata names."""
    key = ProviderDataKey(data_type, data.source.provider, data.source.entity_id)
    store.save(key, TypeAdapter(type(data)), data)


def _actuals[ModelT](
    model: Callable[..., ModelT], entity_id: str, start: datetime, **fields: Any
) -> ModelT:
    """Build an hourly record of one Home Assistant source, retrieved just now."""
    observation = datetime.now(UTC)
    return model(
        schema_version="1",
        start_time=start,
        interval_minutes=60,
        source=_source(entity_id),
        retrieved_at=observation,
        latest_observation_at=observation,
        **fields,
    )


def _forecast[ModelT](
    model: Callable[..., ModelT],
    provider: str,
    entity_id: str,
    expires_at: datetime,
    **fields: Any,
) -> ModelT:
    """Build an hourly forecast-like record of one provider, retrieved just now."""
    return model(
        schema_version="1",
        interval_minutes=60,
        source=_source(entity_id, provider),
        retrieved_at=datetime.now(UTC),
        expires_at=expires_at,
        **fields,
    )


def _save_household(
    store: ProviderDataStore,
    start: datetime,
    load_kw: tuple[float | None, ...],
    *exclusions: HourExclusion,
) -> None:
    household = _actuals(
        HouseholdLoadData,
        "household_load",
        start,
        unit="kW",
        load_kw=load_kw,
        exclusions=exclusions,
    )
    _save(store, "household-load", household)


def _save_grid_flow(
    store: ProviderDataStore,
    start: datetime,
    import_kw: tuple[float | None, ...],
    export_kw: tuple[float | None, ...],
    *exclusions: HourExclusion,
) -> None:
    grid_flow = _actuals(
        GridFlowData,
        "grid_flow",
        start,
        unit="kW",
        import_kw=import_kw,
        export_kw=export_kw,
        exclusions=exclusions,
    )
    _save(store, "grid-flow", grid_flow)


def _battery_history(
    start: datetime,
    intervals: tuple[float | None, ...],
    state_of_charge: tuple[float | None, ...],
    *exclusions: HourExclusion,
) -> BatteryEfficiencyHistoryData:
    """Build retained battery history whose six energy legs all read ``intervals``."""
    return _actuals(
        BatteryEfficiencyHistoryData,
        "battery_efficiency_history",
        start,
        unit="kWh",
        state_of_charge_percent=state_of_charge,
        exclusions=exclusions,
        **{
            f"{leg}_energy_{direction}_kwh": intervals
            for leg in ("battery", "inverter_charge", "inverter_discharge")
            for direction in ("in", "out")
        },
    )


def _exclusion(
    hour: datetime,
    reason: ExclusionReason,
    message: str,
    entity_id: str,
    point: tuple[datetime, str, str],
    **step: Any,
) -> HourExclusion:
    """Exclude one hour for one reason, backed by one ``(time, state, unit)`` point."""
    cause = ExclusionCause.of(
        reason, message, entity_id, [ExcludedDataPoint(*point, **step)]
    )
    return HourExclusion(hour, (cause,))


def _unavailable(hour: datetime) -> HourExclusion:
    """Exclude an hour whose household energy counter was unavailable at :30."""
    return _exclusion(
        hour,
        "unavailable",
        "sensor.household_energy reported an unknown or unavailable state.",
        "sensor.household_energy",
        (hour + timedelta(minutes=30), "unavailable", "kWh"),
    )


def _post_actuals(
    client: httpx.Client,
    path: str,
    entity_id: str,
    start: datetime,
    retrieved_at: datetime | None,
    **series: list[float],
) -> None:
    """Submit actuals through the public HTTP provider endpoint."""
    observation = (retrieved_at or datetime.now(UTC)).isoformat()
    response = client.post(
        f"/api/v1/{path}",
        json={
            "schema_version": "1",
            "start_time": start.isoformat(),
            "interval_minutes": 60,
            **series,
            "unit": "kW",
            "source": {"provider": "home-assistant", "entity_id": entity_id},
            "retrieved_at": observation,
            "latest_observation_at": observation,
        },
    )
    assert response.status_code == 200, response.text


def _seed_household(
    client: httpx.Client,
    start: datetime,
    values: list[float],
    retrieved_at: datetime | None = None,
) -> None:
    _post_actuals(
        client, "household-load", "household_load", start, retrieved_at, load_kw=values
    )


def _seed_grid_flow(client: httpx.Client, start: datetime) -> None:
    grid_flow = {"import_kw": [0.8, 1.6], "export_kw": [0.0, 0.4]}
    _post_actuals(client, "grid-flow", "grid_flow", start, None, **grid_flow)


def _seed_forecasts(
    store: ProviderDataStore,
    start: datetime,
    *,
    price_start: datetime | None = None,
    import_prices: tuple[float, ...] = (0.18, 0.22),
    export_prices: tuple[float, ...] = (0.08, 0.10),
) -> None:
    """Seed normalized forecast records as a real provider would persist them."""
    first_price = price_start or start
    hours = range(len(import_prices))
    pv = _forecast(
        PvGenerationData,
        "forecast.solar",
        "pv_generation",
        start + timedelta(hours=2),
        start_time=start,
        generation_kw=(0.2, 1.4),
        unit="kW",
    )
    prices = _forecast(
        ElectricityPriceData,
        "awattar.de",
        "de",
        first_price + timedelta(hours=len(hours)),
        timestamps=tuple(first_price + timedelta(hours=hour) for hour in hours),
        import_price_eur_per_kwh=import_prices,
        export_price_eur_per_kwh=export_prices,
        unit="EUR/kWh",
    )
    _save(store, "pv-generation", pv)
    _save(store, "electricity-prices", prices)


def _seed_price_and_battery_history(store: ProviderDataStore, start: datetime) -> None:
    """Seed retained price and battery-state history as orchestration persists it."""
    prices = _forecast(
        ElectricityPriceData,
        "awattar.de",
        "de",
        datetime.now(UTC) + timedelta(hours=24),
        timestamps=(start, start + timedelta(hours=1)),
        import_price_eur_per_kwh=(0.31, 0.27),
        export_price_eur_per_kwh=(0.09, 0.07),
        unit="EUR/kWh",
    )
    _save(store, "electricity-price-history", prices)
    history = _battery_history(start, (0.5, 0.5), (35.0, 45.0, 55.0))
    _save(store, "battery-efficiency-history", history)


def _seed_efficiency(store: ProviderDataStore, start: datetime, **fields: Any) -> None:
    """Persist a battery efficiency result; ``fields`` replace the calculated ones."""
    retrieved_at = datetime.now(UTC)
    calculated: dict[str, Any] = {
        "schema_version": "1",
        "status": "ok",
        "inverter_charge_efficiency": 0.95,
        "inverter_discharge_efficiency": 0.8,
        "battery_efficiency": 0.85,
        "round_trip_efficiency": 0.646,
        "history_start": start,
        "history_end": start + timedelta(hours=1),
        "unit": "ratio",
        "source": _source("battery_efficiency"),
        "retrieved_at": retrieved_at,
        "latest_observation_at": retrieved_at,
    }
    _save(store, "battery-efficiency", BatteryEfficiencyData(**calculated | fields))


def _seed_excluded_history(store: ProviderDataStore, start: datetime) -> None:
    """Persist history with excluded hours in every source, as an import would.

    Every source first holds hours 0 to 3 and is then refreshed with hour 5 only,
    as after Home Assistant purged the hours between: hour 4 becomes an excluded
    hour that the production merge creates.
    """

    def at(hours: int, minutes: int = 0) -> datetime:
        return start + timedelta(hours=hours, minutes=minutes)

    decrease = _exclusion(
        at(3),
        "counter_decrease",
        "sensor.household_energy decreased.",
        "sensor.household_energy",
        (at(3, 10), "699.5", "kWh"),
        previous_timestamp=at(2, 50),
        previous_value=700.0,
        value=699.5,
        step_kwh=-0.5,
        maximum_kwh=100.0,
    )
    not_finite = _exclusion(
        at(1),
        "not_finite",
        "sensor.grid_import reported a NaN or infinite state.",
        "sensor.grid_import",
        (at(1, 5), "nan", "kWh"),
    )
    soc_out_of_range = _exclusion(
        at(1),
        "soc_out_of_range",
        "sensor.battery_soc reported a state of charge outside 0 to 100 percent.",
        "sensor.battery_soc",
        (at(1, 45), "150", "%"),
    )
    _save_household(store, start, (1.2, None, 1.0, None), _unavailable(at(1)), decrease)
    _save_household(store, at(5), (2.0,))
    _save_grid_flow(
        store, start, (0.8, None, 0.6, 0.5), (0.0, None, 0.1, 0.2), not_finite
    )
    _save_grid_flow(store, at(5), (0.4,), (0.3,))
    history = merge_battery_efficiency_history(
        _battery_history(
            start,
            (0.5, None, 0.5, 0.5),
            (35.0, None, None, 40.0, 45.0),
            soc_out_of_range,
        ),
        _battery_history(at(5), (0.5,), (50.0, 55.0)),
    )
    _save(store, "battery-efficiency-history", history)


def _seed_repeated_hour_exclusions(store: ProviderDataStore, start: datetime) -> None:
    """Exclude the two hours that show the same local time when clocks go back.

    ``start`` is the first of six hours; the third and fourth are excluded.
    """
    exclusions = [_unavailable(start + timedelta(hours=hour)) for hour in (2, 3)]
    _save_household(store, start, (1.0, 1.0, None, None, 1.0, 1.0), *exclusions)


@pytest.fixture
def historic_window(e2e_api: httpx.Client, provider_store: ProviderDataStore) -> Window:
    """Seed actuals for the power, price, and battery charts and return their window."""
    start, end = _window(-6)
    _seed_household(e2e_api, start, [1.2, 1.0])
    _seed_grid_flow(e2e_api, start)
    _seed_price_and_battery_history(provider_store, start)
    return start, end


@pytest.fixture
def historic_dashboard(historic_window: Window, page: Page) -> Window:
    """Open the dashboard on the seeded actuals and return their window."""
    _open_dashboard(page, window=historic_window)
    return historic_window


def test_actuals_render_from_api_to_browser(e2e_api: httpx.Client, page: Page) -> None:
    start, end = _window()
    _seed_household(e2e_api, start, [1.2, 1.0])

    _open_dashboard(page, window=(start, end))

    expect(page.locator("#content")).to_be_visible()
    expect(page.locator("#status")).to_have_text("2 data points loaded.")
    expect(page.locator("#power-chart")).to_be_visible()
    expect(page.locator("#power-series-paths path")).to_have_count(1)
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#details")).to_contain_text("home-assistant")
    expect(page.locator("#details")).to_contain_text("kW")


@pytest.mark.usefixtures("historic_dashboard")
def test_historic_tab_renders_every_available_asset_with_its_availability(
    page: Page,
) -> None:
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
    for chart, entries in HISTORIC_LEGENDS.items():
        expect(page.locator(f"#{chart}-legend .legend-item")).to_have_text(entries)
    battery_point = page.locator("#battery-points circle").first
    assert "35" in (battery_point.get_attribute("aria-label") or "")
    # Assets without any importer are named with the reason instead of hidden.
    for text in (
        "home-assistant / grid_flow",
        "awattar.de / de",
        "PV generation",
        "Not configured",
        "Electric vehicle",
        "Heat pump",
    ):
        expect(page.locator("#details")).to_contain_text(text)


def test_historic_tab_withholds_invalid_asset_data_but_keeps_valid_series(
    e2e_api: httpx.Client, provider_store: ProviderDataStore, page: Page
) -> None:
    """Verify corrupt persisted data is reported and never drawn as actuals."""
    start, end = _window(-6)
    _seed_household(e2e_api, start, [1.2, 1.0])
    _seed_grid_flow(e2e_api, start)
    for path in provider_store.directory.glob("grid-flow-*"):
        path.write_text("{corrupt\n", encoding="utf-8")

    _open_dashboard(page, window=(start, end))

    expect(page.locator("#status")).to_have_text(
        "2 data points loaded. Invalid data withheld: Grid import and export."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-series-paths path")).to_have_count(1)
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#details")).to_contain_text("Invalid data withheld")
    expect(page.locator("#power-legend")).not_to_contain_text("Grid import")
    expect(page.locator("#power-legend .legend-item")).to_have_count(1)


@pytest.mark.usefixtures("historic_dashboard")
def test_each_chart_has_its_own_legend_beside_it(page: Page) -> None:
    expect(page.locator("#legend")).to_have_count(0)
    for chart, expected in HISTORIC_LEGENDS.items():
        entries = page.locator(f"#{chart}-legend .legend-item")
        expect(entries).to_have_text(expected)
        for index in range(len(expected)):
            entry = entries.nth(index)
            expect(entry).to_have_attribute("aria-pressed", "true")
            assert entry.evaluate("(node) => node.tagName") == "BUTTON"
            assert entry.get_attribute("type") == "button"
        svg = _box(page, f"#{chart}-chart")
        legend = _box(page, f"#{chart}-legend")
        assert legend["x"] >= svg["x"] + svg["width"]
        colors = _swatch_and_line_colors(page, chart)
        assert len(colors) == len(expected)
        assert all(swatch == stroke for swatch, stroke in colors.values())
        assert len({swatch for swatch, _ in colors.values()}) == len(colors)


def test_legend_entries_show_and_hide_lines_and_rescale_the_axis(
    historic_window: Window, page: Page
) -> None:
    errors = _console_errors(page)
    queries = _data_requests(page)
    _open_dashboard(page, window=historic_window)
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
    # A chart with every line hidden keeps a name that says so, not an empty one.
    _expect_chart_semantics(
        page,
        "power",
        "No series shown (kW)",
        "Every series of this chart is hidden. Use the legend to show one.",
    )
    expect(page.locator("#price-chart")).to_have_accessible_name(PRICE_NAME)

    for index in range(3):
        entries.nth(index).click()
    expect(page.locator("#power-series-paths path")).to_have_count(3)
    assert _line_paths(page, "power") == original_paths
    assert _axis_ticks(page, "power") == original_ticks
    expect(page.locator("#power-chart")).to_have_accessible_name(POWER_NAME)
    assert errors == []


@pytest.mark.usefixtures("historic_dashboard")
def test_hidden_series_stay_hidden_until_the_page_is_reloaded(page: Page) -> None:
    grid_import = page.locator("#power-legend [data-series-id=grid_import_actual]")
    grid_import.click()
    expect(page.locator("#power-series-paths path")).to_have_count(2)

    page.locator("#range-form button[type=submit]").click()
    expect(page.locator("#status")).not_to_contain_text("Loading")
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

    _open_dashboard(page)
    expect(page.locator("#power-legend .legend-item")).to_have_count(3)
    expect(grid_import).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#power-series-paths path")).to_have_count(3)


@pytest.mark.usefixtures("historic_dashboard")
def test_legend_entries_are_operable_from_the_keyboard(page: Page) -> None:
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


@pytest.mark.usefixtures("historic_dashboard")
def test_tooltip_names_its_series_and_stays_inside_its_chart(page: Page) -> None:
    """Verify the tooltip names its line, fits its chart, and is the only one."""
    for chart in CHARTS:
        # The last point of a chart is the one nearest its right edge.
        point = page.locator(f"#{chart}-points circle").last
        label = point.get_attribute("aria-label") or ""
        name, remainder = label.split(", ", 1)
        stamp, value = remainder.split(": ", 1)
        point.hover()

        expect(page.locator("#point-tooltip")).to_have_text(
            f"{name} · {stamp} · {value}"
        )
        box = _box(page, "#point-tooltip")
        svg = _box(page, f"#{chart}-chart")
        legend = _box(page, f"#{chart}-legend")
        assert box["x"] >= svg["x"]
        assert box["x"] + box["width"] <= svg["x"] + svg["width"]
        assert box["y"] >= svg["y"]
        assert box["y"] + box["height"] <= svg["y"] + svg["height"]
        assert box["x"] + box["width"] <= legend["x"]
        assert point.get_attribute("aria-label") == label

    _expect_no_native_chart_tooltip(page)
    _expect_chart_semantics(
        page,
        "power",
        POWER_NAME,
        "Household load in kW; Grid import in kW; Grid export in kW. "
        "Missing intervals remain gaps.",
    )
    _expect_chart_semantics(
        page,
        "price",
        PRICE_NAME,
        "Import price in EUR/kWh; Export price in EUR/kWh. "
        "Missing intervals remain gaps.",
    )
    _expect_chart_semantics(
        page,
        "battery",
        "Battery state of charge (%)",
        "Battery state of charge in %. Missing intervals remain gaps.",
    )

    # The accessible name follows the lines that are shown.
    grid_import = page.locator("#power-legend [data-series-id=grid_import_actual]")
    grid_import.click()
    _expect_chart_semantics(
        page,
        "power",
        "Household load and Grid export (kW)",
        "Household load in kW; Grid export in kW. Missing intervals remain gaps.",
    )
    _expect_no_native_chart_tooltip(page)
    grid_import.click()
    expect(page.locator("#power-chart")).to_have_accessible_name(POWER_NAME)


def test_tooltip_of_every_line_holds_its_name_time_and_value(
    historic_dashboard: Window, page: Page
) -> None:
    """Verify every line's tooltip reads `<line> · <time> · <value> <unit>`."""
    start, _ = historic_dashboard
    seeded_lines = {
        "power": (
            ("Household load", (1.2, 1.0), "kW"),
            ("Grid import", (0.8, 1.6), "kW"),
            ("Grid export", (0.0, 0.4), "kW"),
        ),
        "price": (
            ("Import price", (0.31, 0.27), "EUR/kWh"),
            ("Export price", (0.09, 0.07), "EUR/kWh"),
        ),
        "battery": (("Battery state of charge", (35.0, 45.0), "%"),),
    }

    for chart, lines in seeded_lines.items():
        points = page.locator(f"#{chart}-points circle")
        expect(points).to_have_count(2 * len(lines))
        # A chart draws its lines in order, and every line has one point per hour.
        for line_index, (name, values, unit) in enumerate(lines):
            for hour, value in enumerate(values):
                points.nth(2 * line_index + hour).hover()
                stamp = _stamp(start + timedelta(hours=hour))
                expect(page.locator("#point-tooltip")).to_have_text(
                    f"{name} · {stamp} · {value:g} {unit}"
                )
    _expect_no_native_chart_tooltip(page)


def test_legends_are_placed_below_their_charts_on_narrow_screens(
    historic_window: Window, page: Page
) -> None:
    page.set_viewport_size({"width": 600, "height": 900})
    _open_dashboard(page, window=historic_window)

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
    provider_store: ProviderDataStore, page: Page
) -> None:
    _seed_efficiency(
        provider_store,
        _hour(1),
        status="insufficient_data",
        battery_throughput_kwh=5,
        charge_throughput_kwh=10,
        discharge_throughput_kwh=10,
        complete_cycle_count=1,
        defaulted_components=("inverter_charge_efficiency",),
    )

    _open_dashboard(page, "efficiency")

    summary = page.locator("#efficiency-summary")
    metrics = page.locator("#efficiency-metrics")
    details = page.locator("#details")
    expect(summary).to_be_visible()
    expect(metrics).to_be_visible()
    expect(metrics.locator("dt")).to_have_count(4)
    expect(metrics).to_contain_text("Completed battery cycles")
    expect(metrics.locator("dd")).to_have_text(
        ["5.00 kWh", "10.00 kWh", "10.00 kWh", "1 cycles"]
    )
    expect(page.locator("#range-form")).to_be_hidden()
    expect(page.locator("#range-heading")).to_have_text("Complete retained history")
    expect(summary.locator("dt")).to_have_count(4)
    expect(summary.locator(".efficiency-value")).to_have_count(4)
    values = summary.locator(".efficiency-value").all_text_contents()
    assert values == ["0.8500", "0.9500", "0.8000", "0.6460"]
    expect(summary).to_contain_text("0.9500 ratio")
    expect(page.locator("svg#efficiency-chart")).to_have_count(0)
    expect(page.locator("#status")).to_have_text("4 efficiency values loaded.")
    expect(page.locator("#chart-note")).to_contain_text(
        "retained battery and inverter history"
    )
    expect(details).not_to_contain_text("Freshness")
    expect(details).to_contain_text("Coverage")
    expect(details).to_contain_text("Retrieved")
    expect(summary).to_contain_text("Battery round-trip efficiency")
    expect(summary).to_contain_text("Inverter charge efficiency")
    expect(summary.locator(".efficiency-status-calculated")).to_have_count(2)
    expect(summary.locator(".efficiency-status-defaulted")).to_have_count(1)
    expect(
        summary.locator(".efficiency-status-calculated_with_defaults")
    ).to_have_count(1)
    expect(summary.locator("dd")).to_have_text(
        [
            "0.8500 ratio (calculated)",
            "0.9500 ratio (default)",
            "0.8000 ratio (calculated)",
            "0.6460 ratio (calculated with defaults)",
        ]
    )
    assert summary.inner_text().count("(default)") == 1
    annotations = _assistive_annotations(page)
    assert len(annotations) == 1
    assert not any("default" in text.lower() for text in annotations)
    expect(page.locator(".chart-legend .legend-item")).to_have_count(0)
    expect(page.locator(".chart-panel:visible")).to_have_count(0)


def test_efficiency_tab_shows_one_annotation_for_each_fallback_status(
    provider_store: ProviderDataStore, page: Page
) -> None:
    """Keep unavailable and invalid fallbacks distinct without a default marker."""
    _seed_efficiency(
        provider_store,
        _hour(1),
        status="invalid",
        battery_efficiency=0.95,
        round_trip_efficiency=0.722,
        battery_throughput_kwh=0,
        charge_throughput_kwh=0,
        discharge_throughput_kwh=10,
        complete_cycle_count=0,
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
    )

    _open_dashboard(page, "efficiency")

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
        (
            (350.123456, 1234.5678, 0.999),
            ["350.12 kWh", "1234.57 kWh", "1.00 kWh", "3 cycles"],
        ),
        ((0.0, 0.0, 0.0), ["0.00 kWh", "0.00 kWh", "0.00 kWh", "3 cycles"]),
        ((5.0, 10.0, 7.0), ["5.00 kWh", "10.00 kWh", "7.00 kWh", "3 cycles"]),
    ],
    ids=["fractional", "zero", "whole"],
)
def test_efficiency_tab_formats_throughput_to_two_decimals(
    provider_store: ProviderDataStore,
    e2e_api: httpx.Client,
    page: Page,
    throughput_kwh: tuple[float, float, float],
    expected_texts: list[str],
) -> None:
    """Show every throughput with two decimals while the API keeps full precision."""
    start, end = _window()
    battery, charge, discharge = throughput_kwh
    _seed_efficiency(
        provider_store,
        start,
        battery_throughput_kwh=battery,
        charge_throughput_kwh=charge,
        discharge_throughput_kwh=discharge,
        complete_cycle_count=3,
    )

    _open_dashboard(page, "efficiency")

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


def test_forecast_tab_renders_pv_and_prices(
    provider_store: ProviderDataStore, page: Page
) -> None:
    start, end = _window()
    _seed_forecasts(provider_store, start)

    _open_dashboard(page, "forecast", (start, end))

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
    tick_labels = _axis_ticks(page, "price", count=5)
    tick_values = [float(label) for label in tick_labels]
    assert min(tick_values) <= 0.08
    assert max(tick_values) >= 0.22
    assert max(tick_values) < 0.5
    assert all(len(label.rsplit(".", 1)[-1]) >= 2 for label in tick_labels)

    _expect_no_native_chart_tooltip(page)
    _expect_chart_semantics(
        page,
        "power",
        "PV generation (kW)",
        "PV generation in kW. Missing intervals remain gaps.",
    )
    _expect_chart_semantics(
        page,
        "price",
        PRICE_NAME,
        "Import price in EUR/kWh; Export price in EUR/kWh. "
        "Missing intervals remain gaps.",
    )


@pytest.mark.parametrize(
    ("price", "ceiling"),
    [(-0.10, 0.0), (0.0, math.inf)],
    ids=["negative", "zero"],
)
def test_price_axis_handles_flat_values(
    provider_store: ProviderDataStore, page: Page, price: float, ceiling: float
) -> None:
    """Verify a flat price series gets a finite domain around its value.

    A negative series must stay below zero; a zero one extends to both sides.
    """
    start, end = _window()
    prices = (price, price)
    _seed_forecasts(provider_store, start, import_prices=prices, export_prices=prices)

    _open_dashboard(page, "forecast", (start, end))

    tick_values = [float(label) for label in _axis_ticks(page, "price", count=5)]
    assert min(tick_values) < price < max(tick_values) < ceiling


def test_invalid_range_is_rejected_without_an_api_request(page: Page) -> None:
    queries = _data_requests(page)
    _open_dashboard(page)
    expect(page.locator("#status")).to_have_class("status error")
    queries.clear()

    start, _ = _window()
    _submit_range(page, _input_value(start), _input_value(start))

    expect(page.locator("#status")).to_have_text(
        "End time must be later than the start time."
    )
    expect(page.locator("#status")).to_have_class("status error")
    assert queries == []


def test_range_is_aligned_to_available_coverage(
    e2e_api: httpx.Client, page: Page
) -> None:
    start, end = _window()
    _seed_household(e2e_api, start, [1.2, 1.0])

    _open_dashboard(page, window=(start - timedelta(hours=1), end + timedelta(hours=1)))

    expect(page.locator("#start-date")).to_have_value(_input_value(start))
    expect(page.locator("#end-date")).to_have_value(_input_value(end))
    expect(page.locator("#status")).to_have_text("2 data points loaded.")


def test_unavailable_actuals_are_presented_as_an_error(page: Page) -> None:
    _open_dashboard(page)

    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#status")).to_contain_text(
        "no persisted household-load provider data is available"
    )
    expect(page.locator("#power-chart")).to_be_hidden()
    expect(page.locator("#price-chart")).to_be_hidden()


def test_backend_error_is_presented_in_the_dashboard(page: Page) -> None:
    """Verify a failed dashboard request does not leave a blank interface."""
    page.route(
        "**/api/v1/dashboard/data**",
        lambda route: route.fulfill(
            status=503, json={"detail": "dashboard backend is unavailable"}
        ),
    )
    _open_dashboard(page)

    expect(page.locator("#status")).to_have_text("dashboard backend is unavailable")
    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#content")).to_be_hidden()


def test_partial_coverage_is_shown_as_a_gap(
    provider_store: ProviderDataStore, page: Page
) -> None:
    start, end = _window()
    _seed_forecasts(provider_store, start, price_start=start + timedelta(hours=1))

    _open_dashboard(page, "forecast", (start, end + timedelta(hours=1)))

    expect(page.locator("#status")).to_have_text(
        "Partial coverage is available. Missing intervals are shown as gaps."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-points circle")).to_have_count(2)
    expect(page.locator("#price-points circle")).to_have_count(4)
    assert all(
        float(label) == float(label) for label in _axis_ticks(page, "price", count=5)
    )
    for path in page.locator(".series-line").all():
        path_data = path.get_attribute("d")
        assert path_data is not None
        assert path_data.count("M") == 1


def test_stale_actuals_are_shown_with_a_warning(
    e2e_api: httpx.Client, page: Page
) -> None:
    start, end = _window()
    old = datetime.now(UTC) - timedelta(days=2)
    _seed_household(e2e_api, start, [1.2, 1.0], retrieved_at=old)

    _open_dashboard(page, window=(start, end))

    expect(page.locator("#status")).to_have_text(
        "Data is available, but its freshness window has expired."
    )
    expect(page.locator("#status")).to_have_class("status warning")
    expect(page.locator("#power-chart")).to_be_visible()


def test_excluded_hours_tab_lists_every_excluded_hour_and_its_data_point(
    provider_store: ProviderDataStore, page: Page
) -> None:
    """Verify excluded hours are listed with their exact data points and reasons.

    Hour 4 is an hour that Home Assistant no longer held when the sources were
    refreshed, so every source lists it as ``history_unavailable``.
    """
    start = _hour(-8)
    end = start + timedelta(hours=6)
    _seed_excluded_history(provider_store, start)

    _open_dashboard(page, "excluded", (start, end))

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
            "PV generation history: Not configured "
            "(no Home Assistant PV-generation entities are configured)",
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
    unavailable = _reason_rows(rows, "unavailable")
    expect(unavailable).to_have_count(1)
    _expect_cells(
        unavailable,
        {
            0: _stamp(start + timedelta(hours=1)),
            1: "Household load",
            2: "sensor.household_energy",
            4: _stamp(start + timedelta(hours=1, minutes=30)),
            5: "unavailable",
        },
    )
    expect(unavailable.locator("td").nth(3)).to_contain_text(
        "reported an unknown or unavailable state"
    )

    decrease = _reason_rows(rows, "counter_decrease")
    _expect_cells(decrease, {5: "699.5"})
    for text in ("previous 700 kWh", "step -0.5 kWh", "maximum 100 kWh"):
        expect(decrease.locator("td").nth(6)).to_contain_text(text)

    not_finite = {1: "Grid import and export", 2: "sensor.grid_import", 5: "nan"}
    _expect_cells(_reason_rows(rows, "not_finite"), not_finite)
    soc_out_of_range = {2: "sensor.battery_soc", 5: "150"}
    _expect_cells(_reason_rows(rows, "soc_out_of_range"), soc_out_of_range)

    # The gap hour of each source names the missing range and has no entity and no
    # data point, because nothing was recorded for it.
    gap = _reason_rows(rows, "history_unavailable")
    expect(gap).to_have_count(3)
    gap_columns = {
        1: [_stamp(start + timedelta(hours=4))] * 3,
        2: ["Household load", "Grid import and export", "Battery efficiency"],
        3: ["-"] * 3,
        5: ["-"] * 3,
        6: ["-"] * 3,
        7: [""] * 3,
    }
    for column, texts in gap_columns.items():
        expect(gap.locator(f"td:nth-child({column})")).to_have_text(texts)
    expect(gap.locator("td:nth-child(4) .excluded-message")).to_have_text(
        [
            "The provider holds no history from "
            f"{start + timedelta(hours=4):%Y-%m-%dT%H:%M:%S}+00:00 until "
            f"{start + timedelta(hours=5):%Y-%m-%dT%H:%M:%S}+00:00 (1 hour), "
            "so this hour cannot be imported."
        ]
        * 3
    )

    # A window without exclusions says so instead of showing an empty table.
    _load_range(page, (start - timedelta(hours=4), start))
    expect(page.locator("#status")).to_have_text("0 excluded hours listed.")
    expect(page.locator("#excluded-empty")).to_have_text(
        "No hours were excluded in this window."
    )
    expect(page.locator("#excluded-table")).to_be_hidden()

    # The charts show the same hours as gaps, never as zeros.
    _load_range(page, (start, end), "actuals")
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


def test_excluded_hours_tab_reports_sources_without_history(page: Page) -> None:
    start = _hour(-8)

    _open_dashboard(page, "excluded", (start, start + timedelta(hours=4)))

    expect(page.locator("#excluded-content")).to_be_visible()
    expect(page.locator("#excluded-sources li")).to_have_count(4)
    expect(page.locator("#excluded-sources")).to_contain_text(
        "Household load: Unavailable "
        "(no persisted household-load data is available yet)"
    )
    expect(page.locator("#excluded-table")).to_be_hidden()
    # Nothing was checked, so it must not claim that nothing was excluded.
    expect(page.locator("#excluded-empty")).to_be_hidden()


def test_dashboard_settings_report_the_configured_zone(e2e_api: httpx.Client) -> None:
    """Keep the e2e expectations tied to the zone the service really runs with."""
    response = e2e_api.get("/api/v1/dashboard/settings")

    assert response.status_code == 200, response.text
    assert response.json() == {"timezone": DASHBOARD_ZONE.key}


def test_dashboard_names_the_configured_zone_instead_of_utc(page: Page) -> None:
    """Verify headings, labels, and help name the zone; UTC only labels the API."""
    _open_dashboard(page)

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
    provider_store: ProviderDataStore, page: Page
) -> None:
    """Verify prices crossing local midnight read in Berlin time, not New York time."""
    # 19:00Z is 21:00 in Berlin and 15:00 in New York; six hours reach 02:00 on
    # the next local day, past local midnight at 22:00Z.
    start = datetime(2026, 9, 30, 19, tzinfo=UTC)
    prices = (0.10, 0.12, 0.14, 0.16, 0.18, 0.20)
    _seed_forecasts(
        provider_store,
        start,
        import_prices=prices,
        export_prices=tuple(price - 0.05 for price in prices),
    )
    queries = _data_requests(page)
    _open_dashboard(page, "forecast")
    expect(page.locator("#status")).not_to_contain_text("Loading")
    queries.clear()

    # A window wider than the coverage is corrected once to the coverage, which
    # the controls then show as Berlin wall times.
    _load_range(page, (start - timedelta(hours=1), start + timedelta(hours=8)))

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
        assert _stamp(start + timedelta(hours=hour)) in label
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
        [_stamp(start + timedelta(hours=hour)) for hour in (0, 1, 3, 4, 5)]
    )
    expect(page.locator("#details")).to_contain_text(
        "2026-09-30 21:00 to 2026-09-30 23:00"
    )


def test_range_controls_request_whole_utc_hours_for_the_configured_zone(
    page: Page,
) -> None:
    queries = _data_requests(page)
    _open_dashboard(page)
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
    page: Page, day: str, start_utc: str, end_utc: str, hours: int
) -> None:
    page.clock.set_fixed_time(datetime.fromisoformat(f"{day}T12:00:00+00:00"))
    queries = _data_requests(page)
    _open_dashboard(page)

    next_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    expect(page.locator("#start-date")).to_have_value(f"{day}T00:00")
    expect(page.locator("#end-date")).to_have_value(f"{next_day}T00:00")
    assert [(q["start_time"], q["end_time"]) for q in queries] == [(start_utc, end_utc)]
    span = datetime.fromisoformat(end_utc) - datetime.fromisoformat(start_utc)
    assert span == timedelta(hours=hours)


def test_repeated_local_hour_is_two_chart_points_and_two_excluded_rows(
    provider_store: ProviderDataStore, page: Page
) -> None:
    # Clocks go back at 01:00Z on 2026-10-25, so 02:00 to 03:00 local time occurs
    # at 00:00Z under CEST and again at 01:00Z under CET.
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    end = start + timedelta(hours=6)
    _seed_forecasts(
        provider_store,
        start,
        import_prices=(0.10, 0.12, 0.14, 0.16, 0.18, 0.20),
        export_prices=(0.05, 0.06, 0.07, 0.08, 0.09, 0.10),
    )
    _seed_repeated_hour_exclusions(provider_store, start)
    _open_dashboard(page)

    _load_range(page, (start, end), "forecast")
    points = page.locator("#price-points circle")
    expect(points).to_have_count(12)
    labels = [points.nth(index).get_attribute("aria-label") or "" for index in range(6)]
    assert labels[2] == "Import price, 2026-10-25 02:00+02:00: 0.14 EUR/kWh"
    assert labels[3] == "Import price, 2026-10-25 02:00+01:00: 0.16 EUR/kWh"
    assert "2026-10-25 01:00:" in labels[1]
    assert "2026-10-25 03:00:" in labels[4]
    assert len(set(labels)) == 6

    _load_range(page, (start, end), "excluded")
    expect(page.locator("#status")).to_have_text("2 excluded hours listed.")
    rows = page.locator("#excluded-rows tr")
    expect(rows).to_have_count(2)
    expect(rows.locator("td:nth-child(1)")).to_have_text(
        ["2026-10-25 02:00+02:00", "2026-10-25 02:00+01:00"]
    )
    expect(rows.locator("td:nth-child(5)")).to_have_text(
        ["2026-10-25 02:30+02:00", "2026-10-25 02:30+01:00"]
    )


def test_range_controls_resolve_repeated_and_skipped_local_times(page: Page) -> None:
    queries = _data_requests(page)
    _open_dashboard(page)
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
                status=503, json={"detail": "settings backend is unavailable"}
            ),
            "The dashboard settings could not be loaded (HTTP 503)",
        ),
        (
            lambda route: route.abort(),
            "The dashboard settings could not be loaded; check the connection",
        ),
        (
            lambda route: route.fulfill(json={"timezone": "Foo/Bar"}),
            "This browser does not support the configured time zone Foo/Bar",
        ),
    ],
    ids=["http-error", "network-error", "zone-rejected-by-browser"],
)
def test_unusable_time_zone_is_reported_and_no_data_is_requested(
    page: Page, respond: Callable[[Route], object], message: str
) -> None:
    """Verify the dashboard never falls back to a guessed zone."""
    page.route("**/api/v1/dashboard/settings", respond)
    queries = _data_requests(page)

    page.goto("/dashboard/")

    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#status")).to_contain_text(message)
    expect(page.locator("#start-date")).to_be_disabled()
    expect(page.locator("#start-date")).to_have_value("")
    for tab in ("#forecast-tab", "#excluded-tab", "#actuals-tab"):
        page.locator(tab).click()
        expect(page.locator("#status")).to_have_class("status error")
        expect(page.locator("#status")).to_contain_text(message)
    assert queries == []
