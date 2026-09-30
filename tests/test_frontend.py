"""Tests for the dependency-free dashboard asset contract."""

from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"


def test_dashboard_document_preserves_structure_and_accessibility_contract() -> None:
    """Keep static checks for markup that browser tests cannot replace."""
    document = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert '<main class="shell">' in document
    assert "<title>Energy dashboard | Energy Optimizer</title>" in document
    assert "<h1>Energy dashboard</h1>" in document
    assert document.count('type="datetime-local"') == 2
    assert document.count('step="3600"') == 2
    assert 'id="end-label">End (exclusive)' in document
    assert "The end time is exclusive" in document
    assert 'aria-live="polite"' in document
    assert 'id="chart"' in document
    assert 'id="power-chart"' in document
    assert 'id="price-chart"' in document
    assert 'id="battery-chart"' in document
    assert "Battery state of charge (%)" in document
    assert 'id="efficiency-summary"' in document
    assert 'id="efficiency-metrics"' in document
    assert 'id="range-help"' in document
    assert 'id="efficiency-chart"' not in document
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "form.hidden = efficiency" in app
    assert "Complete retained history" in app
    assert 'scenario !== "efficiency"' in app
    assert 'role="tab"' in document
    assert 'aria-controls="dashboard-panel"' in document
    assert 'id="point-tooltip"' in document
    assert 'id="excluded-tab"' in document
    assert 'id="excluded-content"' in document
    assert 'id="excluded-table"' in document
    assert "No hours were excluded in this window." in document

    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert ".dashboard-grid[hidden] { display: none; }" in styles
    assert ".excluded-grid[hidden] { display: none; }" in styles


def test_dashboard_gives_each_chart_its_own_legend_beside_it() -> None:
    """Keep one legend container after each chart and no shared legend above them."""
    document = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert 'id="legend"' not in document
    assert '"#legend"' not in app
    assert "legend-0" not in app, "legend entries derive from the rendered series"
    order = [
        document.index(f'id="{kind}-{part}"')
        for kind in ("power", "price", "battery")
        for part in ("panel", "chart", "legend")
    ]
    assert order == sorted(order)
    for kind in ("power", "price", "battery"):
        legend = document[document.index(f'id="{kind}-legend"') :]
        assert legend.startswith(
            f'id="{kind}-legend" class="chart-legend" role="group"'
        )
        assert f'"#{kind}-legend"' in app
    assert 'entry.type = "button"' in app
    assert '"aria-pressed"' in app
    assert (
        ".chart-panel { display: grid; grid-template-columns: minmax(0, 1fr) 160px;"
        in styles
    )
    mobile = styles[styles.index("@media (max-width: 760px)") :]
    assert ".chart-panel { grid-template-columns: minmax(0, 1fr);" in mobile


def test_dashboard_markup_names_no_fixed_zone_for_displayed_times() -> None:
    """Show the configured zone from the script; the markup only names the API's UTC."""
    document = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "(UTC" not in document
    assert "UTC time window" not in document + app
    assert "UTC date and time" not in document + app
    assert document.count("UTC") == 1
    assert "API\n            timestamps are UTC" in document
    for identifier in (
        "zone-name",
        "start-label",
        "end-label",
        "excluded-hour-heading",
        "excluded-point-heading",
    ):
        assert f'id="{identifier}"' in document
        assert f'"#{identifier}"' in app
    assert 'fetch("/api/v1/dashboard/settings")' in app
    assert "timeZone" in app
    assert 'timeZone: "UTC"' not in app


def test_efficiency_summary_renders_status_from_one_source() -> None:
    """Render each efficiency row's annotation from the structured status only."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "item.calculation_status" in app
    assert "is_default" not in app
    assert "efficiency-default-marker" not in app
    assert "efficiency-default-marker" not in styles


def test_efficiency_metrics_format_energy_to_two_decimals() -> None:
    """Format energy metrics by unit rather than rendering the raw number."""
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")

    assert "kWh: (value) => value.toFixed(2)" in app
    assert "cycles: (value) => String(Math.round(value))" in app
    assert "formatMetricValue(metric)" in app
    assert "`${metric.value} ${metric.unit}`" not in app


def test_dashboard_documents_dependency_and_license_status() -> None:
    """Document the intentionally dependency-free frontend."""
    notices = (FRONTEND / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")

    assert "no third-party runtime or development dependencies" in notices
