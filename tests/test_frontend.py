"""Tests for the dependency-free dashboard asset contract."""

from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"


def test_dashboard_assets_include_accessible_actuals_view() -> None:
    document = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert '<main class="shell">' in document
    assert "<title>Energy dashboard | Energy Optimizer</title>" in document
    assert "<h1>Energy dashboard</h1>" in document
    assert document.count('type="datetime-local"') == 2
    assert document.count('step="3600"') == 2
    assert "End (UTC, exclusive)" in document
    assert "The end time is exclusive" in document
    assert 'aria-live="polite"' in document
    assert 'id="chart"' in document
    assert 'id="power-chart"' in document
    assert 'id="price-chart"' in document
    assert "Historic actuals" in document
    assert "Forecast" in document
    assert 'role="tab"' in document
    javascript = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "imported actuals" in javascript
    assert "const alignRangeToCoverage = (data, start, end)" in javascript
    assert "available_start_time" in javascript
    assert "available_end_time" in javascript
    assert "range_aligned_to_coverage" in javascript
    assert "point-tooltip" in document
    assert "pointerenter" in javascript
    assert 'setAttribute("tabindex", "0")' in javascript
    assert "/api/v1/dashboard/data" in javascript
    assert "scenario_kind: scenario" in javascript
    assert "const diagnostic = (level, event" in javascript
    assert 'diagnostic("info", "tab_clicked"' in javascript
    assert 'diagnostic("warn", "data_unavailable"' in javascript
    assert 'diagnostic("error", "data_load_failed"' in javascript
    assert "seriesPaths" in javascript
    assert "Missing intervals are shown as gaps" in javascript
    assert "const hasSeriesData = (series, id)" in javascript
    assert "item.values.some((value) => value !== null)" in javascript
    assert "heading.textContent = forecastTypes.length" in javascript
    assert '"PV generation", "legend-0"' in javascript
    assert '"Import price", "legend-1"' in javascript
    assert '"Export price", "legend-2"' in javascript
    assert "renderHeader(data)" in javascript
    assert "const seriesClass = (item, index)" in javascript
    assert 'export_price_forecast: "series-2"' in javascript
    assert "seriesClass(item, seriesIndex)" in javascript
    assert "const unitForSeries = (item)" in javascript
    assert '"EUR/kWh"' in javascript
    assert 'id="power-axis-unit"' in document
    assert 'id="price-axis-unit"' in document
    assert 'id="power-chart-description"' in document
    assert 'id="price-chart-description"' in document
    assert "Charts are separated by unit" in document
    assert "unitForSeries(item)" in javascript
    assert "const chartSeries = (series)" in javascript
    assert "power: series.filter" in javascript
    assert "price: series.filter" in javascript
    assert "item.textContent = `${label} (${unitForSeries(source)})`" in javascript
    assert "Missing intervals remain gaps." in javascript
    assert "const formatAxisTimestamp = (value)" in javascript
    assert "x-axis-label" in javascript
    assert "start_time: utcTimestamp(start)" in javascript
    assert "end_time: utcTimestamp(end)" in javascript
    assert "End time must be later than the start time." in javascript
    assert "if (!isValidRange(startInput.value, endInput.value))" in javascript


def test_forecast_legend_is_data_driven() -> None:
    javascript = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert (
        'const hasPv = hasSeriesData(series, "pv_generation_forecast");' in javascript
    )
    assert (
        'const hasImportPrice = hasSeriesData(series, "import_price_forecast");'
        in javascript
    )
    assert (
        'const hasExportPrice = hasSeriesData(series, "export_price_forecast");'
        in javascript
    )
    assert ".filter(([id]) => hasSeriesData(series, id))" in javascript
    assert "const labels = forecast" in javascript
    assert "labels.forEach(([" in javascript


def test_forecast_coverage_preserves_price_windows_across_providers() -> None:
    javascript = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert (
        "const start = Math.min(...ranges.map((range) => range.start));" in javascript
    )
    assert "const end = Math.max(...ranges.map((range) => range.end));" in javascript
    assert "load(todayValue, tomorrowValue);" in javascript
    assert "Missing intervals remain gaps" in javascript


def test_dashboard_initial_empty_state_does_not_format_missing_series() -> None:
    javascript = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert (
        'series.some((item) => hasSeriesData([item], "household_load_actual"))'
        in javascript
    )
    assert (
        ' : [["household_load_actual", "Household load", "legend-0"]];'
        not in javascript
    )


def test_dashboard_toggles_svg_visibility_attributes_explicitly() -> None:
    javascript = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert 'element.removeAttribute("hidden");' in javascript
    assert 'element.setAttribute("hidden", "");' in javascript


def test_dashboard_styles_define_mobile_layout() -> None:
    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "@media (max-width: 760px)" in styles
    assert ".dashboard-grid { display: block; }" in styles
    assert ".range-form { display: grid;" in styles
    assert ".point-tooltip" in styles


def test_dashboard_documents_dependency_and_license_status() -> None:
    notices = (FRONTEND / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")

    assert "no third-party runtime or development dependencies" in notices
