"""Browser verification of general appliances, history and load accounting."""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from e2e.conftest import _free_port, _wait_for_server
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.storage import ProviderDataStore
from home_assistant_fixtures import FakeHomeAssistant
from test_appliances import BASE, capabilities, configuration

pytestmark = pytest.mark.e2e


def test_appliance_controls_and_imported_actuals(page: Page, tmp_path: Path) -> None:
    config = configuration(tmp_path / "data")
    assert config.home_assistant is not None and config.orchestration is not None
    config.home_assistant.pv_generation = config.home_assistant.energy_history["pv"]
    config.orchestration.sources["pv_generation_history"] = (
        config.orchestration.sources["history.pv"]
    )
    rates = {"sensor.house": 5, "sensor.hp": 2, "sensor.ev": 3, "sensor.pv": 4}
    fake = FakeHomeAssistant(
        {
            entity: [(BASE + timedelta(hours=h), str(rate * h)) for h in range(3)]
            for entity, rate in rates.items()
        }
    )
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        orchestrator = build_configured_orchestrator(
            config, ProviderDataStore(tmp_path / "data"), home_assistant_client=client
        )
        assert orchestrator is not None
        assert all(
            run.status == "success"
            for run in orchestrator.run_due(BASE + timedelta(hours=2)).provider_runs
        )
    document = config.model_dump(mode="json")
    document["home_assistant"]["token"] = "test-token"
    document["orchestration"]["enabled"] = False
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(document))
    url = f"http://127.0.0.1:{_free_port()}"
    log = tmp_path / "service.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "energy_optimizer"],
            cwd=Path(__file__).parents[2],
            env={
                **os.environ,
                "ENERGY_OPTIMIZER_CONFIG": str(path),
                "ENERGY_OPTIMIZER_HOST": "127.0.0.1",
                "ENERGY_OPTIMIZER_PORT": url.rsplit(":", 1)[-1],
            },
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_server(process, url, log)
            page.goto(f"{url}/api/v1/appliances")
            body = json.loads(page.locator("body").inner_text())
            assert body["heat_pump"]["power_levels"] == [0, 0.3, 0.6, 1]
            assert body["heat_pump"]["included_in_household_load"] is True
            assert body["ev"]["included_in_household_load"] is False
            params = httpx.QueryParams(
                {
                    "start_time": BASE.isoformat(),
                    "end_time": (BASE + timedelta(hours=2)).isoformat(),
                    "scenario_kind": "actual",
                }
            )
            page.goto(f"{url}/api/v1/dashboard/data?{params}")
            expect(page.locator("body")).to_contain_text("appliance.heat_pump_actual")
            values = {
                item["id"]: item["values"]
                for item in json.loads(page.locator("body").inner_text())["series"]
            }
            assert values["history.pv_actual"] == [4, 4]
            assert values["pv_generation_actual"] == [4, 4]
            assert values["total_consumption_actual"] == [8, 8]
            assert values["unmanaged_household_load_actual"] == [3, 3]
            page.goto(f"{url}/dashboard/")
            expect(page.locator("#status")).not_to_contain_text("Loading")
            page.locator("#start-date").fill("2026-01-01T00:00")
            page.locator("#end-date").fill("2026-01-01T02:00")
            page.locator("#range-form button[type=submit]").click()
            legend = page.locator('[data-series-id="pv_generation_actual"].legend-item')
            expect(legend).to_contain_text("PV generation (kW)")
            points = page.locator('#power-points [aria-label^="PV generation,"]')
            expect(points).to_have_count(2)
            assert "4 kW" in (points.first.get_attribute("aria-label") or "")
            page.goto(f"{url}/docs")
            operation = page.locator(
                "#operations-default-validate_appliance_api_v1_appliances_validate_post"
            )
            operation.locator(".opblock-summary").click()
            operation.get_by_role("button", name="Try it out").click()
            operation.locator("textarea").fill(
                json.dumps(capabilities(control="continuous", power_levels=None))
            )
            operation.get_by_role("button", name="Execute", exact=True).click()
            expect(
                operation.get_by_role("table").first.locator("code").first
            ).to_contain_text('"control": "continuous"')
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
