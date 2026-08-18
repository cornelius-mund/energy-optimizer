"""Tests for the dependency-free dashboard asset contract."""

from pathlib import Path

FRONTEND = Path(__file__).parents[1] / "frontend"


def test_dashboard_assets_include_accessible_actuals_view() -> None:
    document = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert '<main class="shell">' in document
    assert 'aria-live="polite"' in document
    assert 'id="chart"' in document
    assert "Historic actuals" in document
    assert "Forecast" in document
    assert 'role="tab"' in document
    javascript = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "imported actuals" in javascript
    assert "adjustStartDate" in javascript
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


def test_dashboard_styles_define_mobile_layout() -> None:
    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert "@media (max-width: 760px)" in styles
    assert ".dashboard-grid { display: block; }" in styles
    assert ".range-form { display: grid;" in styles
    assert ".point-tooltip" in styles


def test_dashboard_documents_dependency_and_license_status() -> None:
    notices = (FRONTEND / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")

    assert "no third-party runtime or development dependencies" in notices
