"""Exercise the electrical contract and scheduling through the browser API docs."""

import json

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_heat_pump_contract_and_cost_aware_schedule(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/docs")
    hp = {
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00Z",
        "interval_minutes": 60,
        "load_kw": [1, 1, 1],
        "available": [True, True, False],
        "minimum_power_kw": 1,
        "maximum_power_kw": 2,
        "required_energy_kwh": 2,
        "unit": "kW",
        "energy_unit": "kWh",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "latest_observation_at": "2026-01-01T00:00:00Z",
        "source": {"provider": "home-assistant", "entity_id": "heat_pump"},
    }
    operation = page.locator("#operations-default-heat_pump_api_v1_heat_pump_post")
    operation.locator(".opblock-summary").click()
    operation.get_by_role("button", name="Try it out").click()
    operation.locator("textarea").fill(json.dumps(hp))
    operation.get_by_role("button", name="Execute", exact=True).click()
    expect(
        operation.locator(".responses-inner .response-col_status")
        .filter(has_text="200")
        .first
    ).to_be_visible()
    expect(operation.get_by_role("table").first.locator("code").first).to_contain_text(
        '"required_energy_kwh": 2'
    )
    operation.locator("textarea").fill(json.dumps(hp | {"minimum_power_kw": 3}))
    operation.get_by_role("button", name="Execute", exact=True).click()
    expect(operation.get_by_role("table").first.locator("code").first).to_contain_text(
        "minimum_power_kw must not exceed"
    )

    optimization = page.locator("#operations-default-optimize_optimize_post")
    optimization.locator(".opblock-summary").click()
    optimization.get_by_role("button", name="Try it out").click()
    optimization.locator("textarea").fill(
        json.dumps(
            {
                "start_time": hp["start_time"],
                "interval_minutes": 60,
                "load_kw": [1, 1, 1],
                "pv_generation_kw": [0, 0, 0],
                "import_price_eur_per_kwh": [0.5, 0.1, 0.01],
                "export_price_eur_per_kwh": [0, 0, 0],
                "heat_pump": hp,
            }
        )
    )
    optimization.get_by_role("button", name="Execute", exact=True).click()
    output = optimization.get_by_role("table").first.locator("code").first
    expect(output).to_contain_text('"status": "optimal"')
    result = json.loads(output.inner_text())
    assert result["heat_pump_kw"] == pytest.approx([0, 2, 0])
    assert result["objective_eur"] == pytest.approx(0.81)
