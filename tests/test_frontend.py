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
    assert "End (UTC, exclusive)" in document
    assert "The end time is exclusive" in document
    assert 'aria-live="polite"' in document
    assert 'id="chart"' in document
    assert 'id="power-chart"' in document
    assert 'id="price-chart"' in document
    assert 'id="efficiency-summary"' in document
    assert 'id="efficiency-metrics"' in document
    assert 'id="range-help"' in document
    assert 'id="efficiency-chart"' not in document
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert "efficiency-default-marker" in app
    assert "form.hidden = efficiency" in app
    assert "Complete retained history" in app
    assert 'scenario !== "efficiency"' in app
    assert 'role="tab"' in document
    assert 'aria-controls="dashboard-panel"' in document
    assert 'id="point-tooltip"' in document

    styles = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    assert ".dashboard-grid[hidden] { display: none; }" in styles


def test_dashboard_documents_dependency_and_license_status() -> None:
    """Document the intentionally dependency-free frontend."""
    notices = (FRONTEND / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")

    assert "no third-party runtime or development dependencies" in notices
