"""Browser coverage of the plan view in the dashboard."""

from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import Page, Route, expect

from energy_optimizer.optimization import (
    Plan,
    PlannedBatteryHour,
    PlannedEvHour,
    PlannedHour,
    PlanStatus,
)
from energy_optimizer.optimization.plan_generation import (
    PLAN_ADAPTER,
    PLAN_KEY,
    PlanSnapshot,
)
from energy_optimizer.storage import ProviderDataStore

pytestmark = pytest.mark.e2e

DASHBOARD_ZONE: Final = ZoneInfo("Europe/Berlin")
FIRST_HOUR: Final = datetime(2026, 3, 10, 22, tzinfo=UTC)
CREATED_AT: Final = datetime(2026, 3, 10, 21, 30, tzinfo=UTC)
PLAN_HOURS: Final = 3


def _stamp(moment: datetime) -> str:
    return moment.astimezone(DASHBOARD_ZONE).strftime("%Y-%m-%d %H:%M")


def _hour(
    index: int,
    *,
    battery: bool = True,
    electric_vehicle: bool = True,
    away: bool = False,
    reserve_kwh: float = 0.0,
    headroom_kwh: float = 0.0,
) -> PlannedHour:
    return PlannedHour(
        start_time=FIRST_HOUR + timedelta(hours=index),
        load_kw=1.0,
        pv_generation_kw=0.0,
        pv_self_consumption_kw=0.0,
        pv_curtailment_kw=0.0,
        grid_import_kw=1.0 + 4.0 * electric_vehicle + index,
        grid_export_kw=0.0,
        import_cost_eur=0.5 * (index + 1),
        export_revenue_eur=0.0,
        battery=(
            PlannedBatteryHour(
                0.0,
                0.5,
                4.0 - index,
                reserve_kwh=reserve_kwh,
                headroom_kwh=headroom_kwh,
            )
            if battery
            else None
        ),
        ev=(
            PlannedEvHour(
                4.0,
                31.5 + 3.6 * index,
                plugged_in=not away,
                consumption_kwh=12.5 if away else 0.0,
            )
            if electric_vehicle
            else None
        ),
        optimizer_cost_eur=0.5 * (index + 1) - 0.25,
        import_price_eur_per_kwh=0.2 + 0.05 * index,
        export_price_eur_per_kwh=0.07,
    )


def _save_plan(
    store: ProviderDataStore,
    *,
    battery: bool = True,
    electric_vehicle: bool = True,
    price_forecast_from: datetime | None = None,
    away_hour: int | None = None,
    reserve_kwh: float = 0.0,
    headroom_kwh: float = 0.0,
    hour_count: int = PLAN_HOURS,
) -> None:
    hours = tuple(
        _hour(
            index,
            battery=battery,
            electric_vehicle=electric_vehicle,
            away=index == away_hour,
            reserve_kwh=reserve_kwh,
            headroom_kwh=headroom_kwh,
        )
        for index in range(hour_count)
    )
    store.save(
        PLAN_KEY,
        PLAN_ADAPTER,
        PlanSnapshot(
            created_at=CREATED_AT,
            start_time=FIRST_HOUR,
            battery_capacity_kwh=10.0,
            ev_capacity_kwh=60.0 if electric_vehicle else None,
            load_forecast_method="weekday_profile",
            load_forecast_mae_kw=0.12,
            price_forecast_from=price_forecast_from,
            plan=Plan(
                status=PlanStatus.OPTIMAL,
                objective_eur=3.0,
                hours=hours,
                ev_shortfall_kwh=1.5,
                degradation_cost_eur=0.25,
            ),
        ),
    )


def _open_plan(page: Page) -> None:
    page.goto("/dashboard/")
    expect(page.locator("#status")).not_to_contain_text("Loading dashboard")
    page.locator("#plan-tab").click()


def test_the_plan_tab_says_no_plan_exists_before_the_first_one(page: Page) -> None:
    _open_plan(page)

    expect(page.locator("#plan-empty")).to_be_visible()
    expect(page.locator("#plan-table")).to_be_hidden()
    expect(page.locator("#status")).to_have_text("No plan has been created yet.")
    expect(page.locator("#scenario-badge")).to_contain_text("Optimization plan")


def test_the_plan_is_shown_hour_by_hour_with_battery_and_vehicle(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    rows = page.locator("#plan-rows tr")
    expect(rows).to_have_count(PLAN_HOURS)
    headers = page.locator("#plan-head th")
    expect(headers.first).to_have_text("Hour (Europe/Berlin)")
    expect(page.locator("#plan-head")).to_contain_text("Battery state (%)")
    expect(page.locator("#plan-head")).to_contain_text("EV charge (kW)")
    first_cells = rows.nth(0).locator("td")
    expect(first_cells.nth(0)).to_have_text(_stamp(FIRST_HOUR))
    expect(first_cells.nth(1)).to_have_text("1.00")
    expect(first_cells.nth(3)).to_have_text("5.00")
    expect(first_cells.nth(7)).to_have_text("40.0")
    expect(first_cells.nth(8)).to_have_text("4.00")
    expect(first_cells.nth(9)).to_have_text("52.5")
    expect(first_cells.nth(10)).to_have_text("0.500")
    expect(rows.nth(2).locator("td").nth(0)).to_have_text(
        _stamp(FIRST_HOUR + timedelta(hours=2))
    )


def test_the_summary_shows_the_economics_and_how_the_plan_was_made(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    summary = page.locator("#plan-summary")
    expect(summary).to_contain_text("Solver statusoptimal")
    expect(summary).to_contain_text("Objective (EUR)3.000")
    expect(summary).to_contain_text("Import cost (EUR)3.000")
    expect(summary).to_contain_text("Export revenue (EUR)0.000")
    expect(summary).to_contain_text("Battery wear cost (EUR)0.250")
    expect(summary).to_contain_text("EV shortfall at deadline (kWh)1.50")
    expect(summary).to_contain_text("Load forecastweekday_profile (±0.12 kW)")
    expect(summary).to_contain_text(
        f"{_stamp(FIRST_HOUR)} to {_stamp(FIRST_HOUR + timedelta(hours=PLAN_HOURS))}"
    )
    expect(page.locator("#status")).to_contain_text("solver status optimal")


def test_a_plan_without_battery_or_vehicle_has_no_columns_for_them(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store, battery=False, electric_vehicle=False)

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    expect(page.locator("#plan-head")).not_to_contain_text("Battery")
    expect(page.locator("#plan-head")).not_to_contain_text("EV")
    expect(page.locator("#plan-summary")).not_to_contain_text("EV shortfall")


def test_the_plan_is_marked_as_a_plan_and_actuals_are_not(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)
    page.goto("/dashboard/")
    expect(page.locator("#scenario-badge")).to_contain_text("Historic actuals")

    page.locator("#plan-tab").click()

    expect(page.locator("#plan-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#actuals-tab")).to_have_attribute("aria-selected", "false")
    expect(page.locator("#scenario-badge")).to_contain_text("Optimization plan")
    expect(page.locator("#plan-content")).to_contain_text("not a measurement")
    expect(page.locator("#content")).to_be_hidden()

    page.locator("#actuals-tab").click()

    expect(page.locator("#plan-content")).to_be_hidden()


def test_a_failing_plan_request_is_reported_as_an_error(page: Page) -> None:
    def fail(route: Route) -> None:
        route.fulfill(
            status=503,
            content_type="application/json",
            body='{"detail": "provider data persistence is not configured"}',
        )

    page.route("**/plan", fail)

    _open_plan(page)

    expect(page.locator("#status")).to_have_class("status error")
    expect(page.locator("#status")).to_have_text(
        "provider data persistence is not configured"
    )
    expect(page.locator("#plan-table")).to_be_hidden()


def test_the_plan_fits_a_phone_screen_by_scrolling_the_table_only(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)
    page.set_viewport_size({"width": 390, "height": 800})

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1
    wrapper = page.locator("#plan-content .table-wrap")
    assert wrapper.evaluate("el => el.scrollWidth > el.clientWidth") is True


def test_hours_priced_by_the_epex_predictor_forecast_are_marked(
    page: Page, provider_store: ProviderDataStore
) -> None:
    forecast_from = FIRST_HOUR + timedelta(hours=2)
    _save_plan(provider_store, price_forecast_from=forecast_from)

    _open_plan(page)

    rows = page.locator("#plan-rows tr")
    expect(page.locator("#plan-head")).to_contain_text("Price")
    expect(rows.nth(0).locator("td").last).to_have_text("published")
    expect(rows.nth(1).locator("td").last).to_have_text("published")
    expect(rows.nth(2).locator("td").last).to_have_text("forecast")
    expect(page.locator("#plan-summary")).to_contain_text(
        f"Forecast prices from{_stamp(forecast_from)}"
    )
    expect(page.locator("#plan-price-attribution")).to_be_visible()
    expect(page.locator("#plan-price-attribution")).to_contain_text("EPEX Predictor")


def test_a_plan_with_published_prices_only_has_no_price_column(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    expect(page.locator("#plan-head")).not_to_contain_text("Price")
    expect(page.locator("#plan-summary")).not_to_contain_text("Forecast prices")
    expect(page.locator("#plan-price-attribution")).to_be_hidden()


def test_the_learned_presence_and_driving_of_the_car_are_shown(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store, away_hour=1)

    _open_plan(page)

    head = page.locator("#plan-head")
    expect(head).to_contain_text("EV at home")
    expect(head).to_contain_text("EV driving (kWh)")
    away_cells = page.locator("#plan-rows tr").nth(1).locator("td")
    expect(away_cells.nth(10)).to_have_text("no")
    expect(away_cells.nth(11)).to_have_text("12.5")
    expect(page.locator("#plan-rows tr").nth(0).locator("td").nth(10)).to_have_text(
        "yes"
    )


def test_a_car_that_is_always_home_and_never_drives_has_no_presence_columns(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-head")).not_to_contain_text("EV at home")


def test_the_battery_reserve_is_shown_when_the_plan_keeps_one(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store, reserve_kwh=1.5)

    _open_plan(page)

    expect(page.locator("#plan-head")).to_contain_text("Battery reserve (%)")
    reserve_column = (
        page.locator("#plan-head th").all_text_contents().index("Battery reserve (%)")
    )
    expect(
        page.locator("#plan-rows tr").nth(0).locator("td").nth(reserve_column)
    ).to_have_text("15.0")


def test_a_plan_without_a_reserve_has_no_reserve_column(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    expect(page.locator("#plan-head")).not_to_contain_text("Battery reserve")


def test_a_total_row_sums_the_energy_and_the_costs_of_the_plan(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    headers = page.locator("#plan-head th").all_text_contents()
    total = page.locator("#plan-foot tr td")
    expect(total.nth(0)).to_have_text("Total")
    expect(total.nth(headers.index("Grid import (kW)"))).to_have_text("18.00")
    expect(total.nth(headers.index("EV charge (kW)"))).to_have_text("12.00")
    expect(total.nth(headers.index("Cost (EUR)"))).to_have_text("3.000")
    expect(total.nth(headers.index("Battery state (%)"))).to_have_text("-20.0")
    expect(total.nth(headers.index("EV state (%)"))).to_have_text("+12.0")
    cumulative = headers.index("Cumulative (EUR)")
    rows = page.locator("#plan-rows tr")
    expect(rows.nth(0).locator("td").nth(cumulative)).to_have_text("0.500")
    expect(rows.nth(2).locator("td").nth(cumulative)).to_have_text("3.000")


def test_every_column_of_a_long_plan_is_reachable_inside_the_table_box(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store, hour_count=150, away_hour=1, reserve_kwh=1.0)
    page.set_viewport_size({"width": 1280, "height": 800})

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(150)
    wrapper = page.locator("#plan-content .plan-table-wrap")
    box = wrapper.bounding_box()
    assert box is not None and box["height"] <= 800 * 0.7 + 1
    wrapper.scroll_into_view_if_needed()
    ev_charge = page.locator("#plan-head th", has_text="EV charge (kW)")
    ev_charge.evaluate('el => el.scrollIntoView({block: "nearest", inline: "center"})')
    expect(ev_charge).to_be_in_viewport()
    assert wrapper.evaluate("el => el.scrollLeft") > 0
    wrapper.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    expect(page.locator("#plan-head th").first).to_be_in_viewport()
    expect(page.locator("#plan-foot td").first).to_be_in_viewport()


def test_the_optimizer_cost_of_each_hour_is_shown_beside_the_cash_cost(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    headers = page.locator("#plan-head th").all_text_contents()
    cash = headers.index("Cost (EUR)")
    optimizer = headers.index("Cost (Optimizer)")
    first = page.locator("#plan-rows tr").nth(0).locator("td")
    expect(first.nth(cash)).to_have_text("0.500")
    expect(first.nth(optimizer)).to_have_text("0.250")
    total = page.locator("#plan-foot td")
    expect(total.nth(cash)).to_have_text("3.000")
    expect(total.nth(optimizer)).to_have_text("2.250")


def test_the_plan_uses_the_full_width_of_a_large_window(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)
    page.set_viewport_size({"width": 1920, "height": 1000})

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    card = page.locator("#plan-content").bounding_box()
    assert card is not None
    assert card["width"] > 1800
    assert card["x"] >= 0
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1


def test_the_battery_headroom_is_shown_when_the_plan_keeps_one(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store, headroom_kwh=1.0)

    _open_plan(page)

    expect(page.locator("#plan-rows tr")).to_have_count(PLAN_HOURS)
    headers = page.locator("#plan-head th").all_text_contents()
    column = headers.index("Battery headroom (%)")
    expect(page.locator("#plan-rows tr").nth(0).locator("td").nth(column)).to_have_text(
        "10.0"
    )
    expect(page.locator("#plan-foot td").nth(column)).to_have_text("")


def test_the_plan_is_drawn_as_charts_whose_series_can_be_hidden(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-charts")).to_be_visible()
    expect(page.locator("#plan-power-legend .legend-item")).to_have_text(
        [
            "Load (kW)",
            "PV (kW)",
            "Grid import (kW)",
            "Grid export (kW)",
            "Battery charge (kW)",
            "Battery discharge (kW)",
            "EV charge (kW)",
        ]
    )
    expect(page.locator("#plan-state-legend .legend-item")).to_have_text(
        ["Battery state (%)", "EV state (%)"]
    )
    expect(page.locator("#plan-cost-legend .legend-item")).to_have_text(
        ["Cost (EUR)", "Cost (optimizer) (EUR)", "Cumulative cost (EUR)"]
    )
    paths = page.locator("#plan-power-series-paths path")
    expect(paths).to_have_count(7)
    load = page.locator("#plan-power-legend [data-series-id=plan_load]")
    load.click()
    expect(load).to_have_attribute("aria-pressed", "false")
    expect(paths).to_have_count(6)
    page.locator("#plan-state-points circle").first.hover()
    expect(page.locator("#plan-charts .point-tooltip")).to_be_visible()
    expect(page.locator("#plan-charts .point-tooltip")).to_contain_text("Battery state")


def test_a_plan_without_battery_or_car_has_no_state_chart(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store, battery=False, electric_vehicle=False)

    _open_plan(page)

    expect(page.locator("#plan-power-panel")).to_be_visible()
    expect(page.locator("#plan-state-panel")).to_be_hidden()
    expect(page.locator("#plan-power-legend .legend-item")).to_have_count(4)


def test_the_grid_import_cost_chart_shows_hourly_and_running_import_costs(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-import-panel")).to_be_visible()
    expect(page.locator("#plan-import-legend .legend-item")).to_have_text(
        ["Grid import cost (EUR)", "Cumulative grid import cost (EUR)"]
    )
    expect(page.locator("#plan-import-series-paths path")).to_have_count(2)
    running = page.locator(
        "#plan-import-legend [data-series-id=plan_cumulative_import_cost]"
    )
    running.click()
    expect(running).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#plan-import-series-paths path")).to_have_count(1)


def test_the_prices_per_kwh_are_drawn_beside_the_power_chart(
    page: Page, provider_store: ProviderDataStore
) -> None:
    _save_plan(provider_store)

    _open_plan(page)

    expect(page.locator("#plan-price-panel")).to_be_visible()
    expect(page.locator("#plan-price-legend .legend-item")).to_have_text(
        ["Import price (EUR/kWh)", "Export price (EUR/kWh)"]
    )
    expect(page.locator("#plan-price-series-paths path")).to_have_count(2)
    import_price = page.locator("#plan-price-legend [data-series-id=plan_import_price]")
    import_price.click()
    expect(import_price).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#plan-price-series-paths path")).to_have_count(1)
