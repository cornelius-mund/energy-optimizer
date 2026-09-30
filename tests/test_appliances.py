"""General appliance controls, shared history and non-duplicating load accounting."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from energy_optimizer.api import app
from energy_optimizer.appliances import ApplianceCapabilities, account_loads
from energy_optimizer.config import Configuration
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.providers.home_assistant_energy_history import (
    EnergyHistoryData,
    HomeAssistantEnergyHistoryImporter,
)
from energy_optimizer.providers.home_assistant_history import (
    HomeAssistantError,
    HomeAssistantHistoryImporter,
)
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore
from home_assistant_fixtures import FakeHomeAssistant, aggregate_settings

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def capabilities(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Heat pump",
        "included_in_household_load": True,
        "maximum_power_kw": 3,
        "control": "discrete",
        "power_levels": [0, 0.3, 0.6, 1],
        **overrides,
    }


def counter(entity: str) -> dict[str, Any]:
    return aggregate_settings(
        add=[{"entity_id": entity, "unit": "kWh", "state_class": "total_increasing"}]
    )


def configuration(directory: Path) -> Configuration:
    return Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
            "persistence": {"directory": directory},
            "home_assistant": {
                "base_url": "http://homeassistant.test:8123",
                "token": "test-token",
                "timeout_seconds": 5,
                "household_load": counter("sensor.house"),
                "energy_history": {
                    "pv": counter("sensor.pv"),
                    "same_meter": counter("sensor.hp"),
                },
            },
            "appliances": {
                "heat_pump": capabilities(history=counter("sensor.hp")),
                "ev": capabilities(
                    name="EV",
                    included_in_household_load=False,
                    history=counter("sensor.ev"),
                    power_levels=[0, 1],
                ),
            },
            "orchestration": {
                "enabled": True,
                "sources": {
                    name: {"interval_seconds": 3600}
                    for name in (
                        "household_load",
                        "appliance.heat_pump",
                        "appliance.ev",
                        "history.pv",
                        "history.same_meter",
                    )
                },
            },
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"power_levels": [0, 1]},
        {"control": "continuous", "power_levels": None},
        {},
    ],
)
def test_supported_control_modes(overrides: dict[str, Any]) -> None:
    assert (
        ApplianceCapabilities.model_validate(capabilities(**overrides)).maximum_power_kw
        == 3
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"control": "continuous"},
        {"control": "other"},
        {"power_levels": None},
        {"power_levels": [0]},
        {"power_levels": [0, 0.3, 0.3, 1]},
        {"power_levels": [0, 0.6, 0.3, 1]},
        {"power_levels": [0.1, 1]},
        {"power_levels": [0, 0.6]},
        {"power_levels": [0, 1.1, 1]},
        {"maximum_power_kw": 0},
        {"maximum_power_kw": "NaN"},
        {"included_in_household_load": "yes"},
    ],
)
def test_invalid_control_contract(
    overrides: dict[str, Any], client: TestClient
) -> None:
    with pytest.raises(ValidationError):
        ApplianceCapabilities.model_validate(capabilities(**overrides))
    with client:
        assert (
            client.post(
                "/api/v1/appliances/validate", json=capabilities(**overrides)
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "house,loads,expected",
    [
        (5, [(True, 2), (False, 3)], (3, 8)),
        (5, [(False, 2)], (5, 7)),
        (5, [(True, None)], (None, 5)),
        (5, [(False, None)], (5, None)),
        (None, [], (None, None)),
        (1, [(True, 2)], (None, 1)),
    ],
)
def test_load_accounting(
    house: float | None,
    loads: list[tuple[bool, float | None]],
    expected: tuple[float | None, float | None],
) -> None:
    assert account_loads(house, loads) == expected


def test_shared_import_incremental_history_and_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    rates = {"sensor.house": 5, "sensor.hp": 2, "sensor.ev": 3, "sensor.pv": 4}
    fake = FakeHomeAssistant(
        {
            entity: [(BASE + timedelta(hours=h), str(rate * h)) for h in range(4)]
            for entity, rate in rates.items()
        }
    )
    store = ProviderDataStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(fake)) as http:
        orchestrator = build_configured_orchestrator(
            config, store, home_assistant_client=http
        )
        assert orchestrator is not None
        result = orchestrator.run_due(BASE + timedelta(hours=2))
        assert all(run.status == "success" for run in result.provider_runs)
        # The HP entity is shared by a generic source and an appliance source.
        assert len([r for r in fake.requests if r.entity_id == "sensor.hp"]) == len(
            [r for r in fake.requests if r.entity_id == "sensor.ev"]
        )
        fake.requests.clear()
        result = orchestrator.run_due(BASE + timedelta(hours=3))
        assert all(run.status == "success" for run in result.provider_runs)
        assert all(
            request.start_time >= BASE + timedelta(hours=2) for request in fake.requests
        )
    key = ProviderDataKey("energy-history", "home-assistant", "appliance.heat_pump")
    data = store.load(key, TypeAdapter(EnergyHistoryData))
    assert data is not None and data.power_kw == (2, 2, 2)
    monkeypatch.setattr(app.state, "configuration", config, raising=False)
    monkeypatch.setattr(app.state, "provider_data_store", store, raising=False)
    client = TestClient(app)
    assert client.get("/api/v1/appliances").json()["ev"]["power_levels"] == [0, 1]
    params = {
        "start_time": BASE.isoformat(),
        "end_time": (BASE + timedelta(hours=3)).isoformat(),
    }
    history = client.get("/api/v1/energy-history/appliance.heat_pump", params=params)
    assert history.status_code == 200 and history.json()["values"] == [2, 2, 2]
    dashboard = client.get(
        "/api/v1/dashboard/data", params=params | {"scenario_kind": "actual"}
    )
    assert dashboard.status_code == 200
    series = {item["id"]: item["values"] for item in dashboard.json()["series"]}
    assert series["household_load_actual"] == [5, 5, 5]
    assert series["appliance.heat_pump_actual"] == [2, 2, 2]
    assert series["history.pv_actual"] == [4, 4, 4]
    assert series["unmanaged_household_load_actual"] == [3, 3, 3]
    assert series["total_consumption_actual"] == [8, 8, 8]


def test_excluded_appliance_history_is_visible_and_failed_import_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    fake = FakeHomeAssistant(
        {
            entity: [
                (BASE, "0"),
                (
                    BASE + timedelta(hours=1),
                    "unavailable" if entity == "sensor.hp" else "1",
                ),
                (BASE + timedelta(hours=2), "2"),
            ]
            for entity in ("sensor.hp", "sensor.ev", "sensor.house", "sensor.pv")
        }
    )
    store = ProviderDataStore(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(fake)) as client:
        orchestrator = build_configured_orchestrator(
            config, store, home_assistant_client=client
        )
        assert orchestrator is not None
        orchestrator.run_due(BASE + timedelta(hours=2))
        key = ProviderDataKey("energy-history", "home-assistant", "appliance.heat_pump")
        before = store.load(key, TypeAdapter(EnergyHistoryData))
        assert before is not None and None in before.power_kw and before.exclusions
        fake.failures["sensor.hp"] = 404
        cycle = orchestrator.run_due(BASE + timedelta(hours=3))
        assert (
            next(
                run
                for run in cycle.provider_runs
                if run.source == "appliance.heat_pump"
            ).status
            == "failed"
        )
        assert store.load(key, TypeAdapter(EnergyHistoryData)) == before
    monkeypatch.setattr(app.state, "configuration", config, raising=False)
    monkeypatch.setattr(app.state, "provider_data_store", store, raising=False)
    response = TestClient(app).get(
        "/api/v1/dashboard/excluded-hours",
        params={
            "start_time": BASE.isoformat(),
            "end_time": (BASE + timedelta(hours=2)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert any(
        item["source"] == "appliance.heat_pump" for item in response.json()["hours"]
    )


@pytest.mark.parametrize("status", [401, 403])
def test_generic_history_authentication_failure_is_actionable(
    status: int, tmp_path: Path
) -> None:
    config = configuration(tmp_path)
    assert config.home_assistant is not None
    assert config.appliances["heat_pump"].history is not None
    plan = HomeAssistantEnergyHistoryImporter(
        config.appliances["heat_pump"].history, "appliance.heat_pump"
    ).plan(BASE, BASE + timedelta(hours=1))
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status))
    ) as client:
        history = HomeAssistantHistoryImporter(
            config.home_assistant, client
        ).import_history(plan.needs)
        with pytest.raises(HomeAssistantError, match="authentication"):
            plan.build(history)
