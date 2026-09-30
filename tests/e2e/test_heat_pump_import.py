"""Browser coverage of an imported snapshot served by the production process."""

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect
from pydantic import TypeAdapter

from e2e.conftest import _configuration, _free_port, _wait_for_server
from energy_optimizer.heat_pump import HeatPumpLoad
from energy_optimizer.providers.home_assistant_heat_pump import (
    HomeAssistantHeatPumpImporter,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore
from test_heat_pump import NOW, ha_configuration, ha_record

pytestmark = pytest.mark.e2e


def test_browser_reads_imported_baseline_and_stale_metadata(
    page: Page, tmp_path: Path
) -> None:
    data_directory = tmp_path / "provider-data"
    configuration = _configuration(data_directory)
    ha = ha_configuration()
    configuration["home_assistant"] = ha.model_dump(mode="json")
    configuration["home_assistant"]["token"] = "test-token"
    configuration["forecast_solar"] = None
    configuration["awattar"] = None
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=ha_record(r.url.path.split("/")[-1]))
        )
    ) as client:
        data = HomeAssistantHeatPumpImporter(ha, client).fetch(now=NOW)
    ProviderDataStore(data_directory).save(
        ProviderDataKey("heat-pump", "home-assistant", "heat_pump"),
        TypeAdapter(HeatPumpLoad),
        data,
    )
    project = Path(__file__).parents[2]
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log = tmp_path / "heat-pump-service.log"
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "energy_optimizer"],
            cwd=project,
            env={
                **os.environ,
                "ENERGY_OPTIMIZER_CONFIG": str(path),
                "ENERGY_OPTIMIZER_HOST": "127.0.0.1",
                "ENERGY_OPTIMIZER_PORT": str(port),
            },
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_for_server(process, url, log)
            page.goto(f"{url}/api/v1/heat-pump")
            expect(page.locator("body")).to_contain_text('"freshness":"stale"')
            result = json.loads(page.locator("body").inner_text())
            assert result["load_kw"] == [1, 1, 1]
            assert result["required_energy_kwh"] == 3
            query = httpx.QueryParams(
                {
                    "scenario_kind": "forecast",
                    "start_time": NOW.isoformat(),
                    "end_time": (NOW + timedelta(hours=3)).isoformat(),
                }
            )
            page.goto(f"{url}/api/v1/dashboard/data?{query}")
            expect(page.locator("body")).to_contain_text("heat_pump_forecast")
            dashboard = json.loads(page.locator("body").inner_text())
            assert dashboard["series"][0]["values"] == [1, 1, 1]
            assert dashboard["series"][0]["freshness"] == "stale"
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
