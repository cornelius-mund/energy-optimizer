"""Electrical contract, importer and MILP regression tests for the heat pump."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from energy_optimizer.api import app
from energy_optimizer.config import Configuration, HomeAssistantConfiguration
from energy_optimizer.heat_pump import HeatPumpLoad
from energy_optimizer.optimization import (
    BatteryLimits,
    OptimizationError,
    solve_schedule,
)
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.providers.home_assistant_heat_pump import (
    HomeAssistantHeatPumpImporter,
)
from energy_optimizer.providers.home_assistant_history import HomeAssistantError
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def heat_pump_payload() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "start_time": NOW.isoformat(),
        "interval_minutes": 60,
        "load_kw": [1, 1, 1],
        "available": [True, True, True],
        "minimum_power_kw": 1,
        "maximum_power_kw": 2,
        "required_energy_kwh": 3,
        "unit": "kW",
        "energy_unit": "kWh",
        "retrieved_at": NOW.isoformat(),
        "latest_observation_at": NOW.isoformat(),
    }


def ha_configuration() -> HomeAssistantConfiguration:
    return HomeAssistantConfiguration.model_validate(
        {
            "base_url": "http://homeassistant.local:8123",
            "token": "test-token",
            "timeout_seconds": 10,
            "max_data_age_seconds": 300,
            "heat_pump": {
                "power": {"entity_id": "sensor.hp_power", "unit": "W"},
                "required_energy": {"entity_id": "sensor.hp_energy", "unit": "Wh"},
                "minimum_power_kw": 1,
                "maximum_power_kw": 2,
                "available": [True, True, True],
            },
        }
    )


def ha_record(entity: str) -> dict[str, Any]:
    return {
        "entity_id": entity,
        "state": "1000" if entity.endswith("power") else "3000",
        "attributes": {
            "unit_of_measurement": "W" if entity.endswith("power") else "Wh"
        },
        "last_updated": NOW.isoformat(),
    }


@pytest.mark.parametrize(
    "override",
    [
        {"minimum_power_kw": 3},
        {"load_kw": [-1, 1, 1]},
        {"load_kw": [3, 1, 1]},
        {"available": [True]},
        {"required_energy_kwh": 7},
        {"required_energy_kwh": 0.5},
        {"start_time": "2026-01-01T00:00:00"},
        {"unit": "W"},
        {"maximum_power_kw": "NaN"},
        {"required_energy_kwh": "Infinity"},
        {"schema_version": "2"},
        {"unknown": 1},
        {"latest_observation_at": "2026-01-02T00:00:00Z"},
    ],
)
def test_heat_pump_contract_rejects_invalid_data(
    client: TestClient, override: dict[str, Any]
) -> None:
    with client:
        response = client.post("/api/v1/heat-pump", json=heat_pump_payload() | override)
    assert response.status_code == 422
    assert response.json()["detail"]


def test_heat_pump_contract_round_trip(client: TestClient) -> None:
    with client:
        response = client.post("/api/v1/heat-pump", json=heat_pump_payload())
    assert response.status_code == 200
    assert response.json()["required_energy_kwh"] == 3
    assert response.json()["freshness"] == "unknown"


def test_import_normalizes_units_and_source_times() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=ha_record(request.url.path.split("/")[-1]))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        importer = HomeAssistantHeatPumpImporter(ha_configuration(), client)
        data = importer.fetch(now=NOW)
    assert data.load_kw == [1, 1, 1]
    assert data.required_energy_kwh == 3
    assert data.source is not None and data.source.entity_id == "heat_pump"
    assert data.latest_observation_at == NOW
    assert len(requests) == 2
    assert all(r.headers["authorization"] == "Bearer test-token" for r in requests)
    assert importer.is_fresh(data, now=NOW + timedelta(seconds=300))
    assert not importer.is_fresh(data, now=NOW + timedelta(seconds=301))


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication"),
        (403, "authentication"),
        (404, "mapping"),
        (500, "HTTP 500"),
    ],
)
def test_import_http_failures(status: int, expected: str) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status))
    ) as client:
        importer = HomeAssistantHeatPumpImporter(ha_configuration(), client)
        with pytest.raises(HomeAssistantError, match=expected):
            importer.fetch(now=NOW)


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"state": "unavailable"}, "unavailable"),
        ({"state": "bad"}, "non-numeric"),
        ({"state": "NaN"}, "finite"),
        ({"state": "-1"}, "non-negative"),
        ({"state": True}, "finite"),
        ({"attributes": {}}, "unit"),
        ({"last_updated": "bad"}, "last_updated"),
        ({"last_updated": "2026-01-01T00:00:00"}, "timezone"),
        ({"entity_id": "sensor.other"}, "entity ID"),
    ],
)
def test_import_malformed_states(override: dict[str, Any], expected: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=ha_record(request.url.path.split("/")[-1]) | override
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HomeAssistantError, match=expected):
            HomeAssistantHeatPumpImporter(ha_configuration(), client).fetch(now=NOW)


def solve(**kwargs: Any) -> Any:
    return solve_schedule(
        [1, 1, 1],
        [0, 0, 0],
        [0.5, 0.1, 0.3],
        [0, 0, 0],
        maximum_import_kw=10,
        maximum_export_kw=10,
        heat_pump=HeatPumpLoad.model_validate(heat_pump_payload()),
        **kwargs,
    )


def test_schedules_exact_energy_in_cheapest_hours_and_enforces_on_limits() -> None:
    result = solve()
    assert result.status == "optimal"
    assert result.heat_pump_kw == pytest.approx([0, 2, 1])
    assert result.grid_import_kw == pytest.approx([1, 3, 2])
    assert result.objective_eur == pytest.approx(1.4)


@pytest.mark.parametrize("source", ["pv", "battery", "grid"])
def test_heat_pump_can_use_each_energy_source(source: str) -> None:
    hp = HeatPumpLoad.model_validate(
        heat_pump_payload()
        | {"available": [True, False, False], "required_energy_kwh": 2}
    )
    battery = BatteryLimits(0, 4, 4, 2, 2, 1) if source == "battery" else None
    result = solve_schedule(
        [0, 0, 0],
        [2, 0, 0] if source == "pv" else [0, 0, 0],
        [1, 1, 1],
        [0, 0, 0],
        maximum_import_kw=10,
        maximum_export_kw=10,
        heat_pump=hp,
        battery=battery,
    )
    assert result.status == "optimal"
    assert result.heat_pump_kw == pytest.approx([2, 0, 0])
    if source == "pv":
        assert result.pv_used_kw[0] == pytest.approx(2)
    elif source == "battery":
        assert result.battery_discharge_kw[0] == pytest.approx(2)
    else:
        assert result.grid_import_kw[0] == pytest.approx(2)
    for hour in range(3):
        assert result.grid_import_kw[hour] + result.pv_used_kw[
            hour
        ] + result.battery_discharge_kw[hour] == pytest.approx(
            result.heat_pump_kw[hour]
            + result.grid_export_kw[hour]
            + result.battery_charge_kw[hour]
        )


def test_reports_infeasible_operating_energy_combination() -> None:
    hp = HeatPumpLoad.model_validate(
        heat_pump_payload() | {"minimum_power_kw": 2, "required_energy_kwh": 3}
    )
    result = solve_schedule(
        [0, 0, 0],
        [0, 0, 0],
        [1, 1, 1],
        [0, 0, 0],
        maximum_import_kw=10,
        maximum_export_kw=10,
        heat_pump=hp,
    )
    assert result.status == "infeasible"
    assert result.heat_pump_kw == ()


def test_api_returns_infeasible_for_grid_limit(
    client: TestClient, valid_request: dict[str, Any]
) -> None:
    with client:
        response = client.post("/optimize", json=valid_request | {"load_kw": [11, 11]})
    assert response.status_code == 200
    assert response.json()["status"] == "infeasible"
    assert response.json()["diagnostics"]


def test_unsupported_solver_is_actionable() -> None:
    with pytest.raises(OptimizationError, match="solver.name"):
        solve(solver_name="unknown")


def test_battery_charges_then_supplies_heat_pump_with_losses() -> None:
    hp = HeatPumpLoad.model_validate(
        heat_pump_payload()
        | {
            "available": [False, True, False],
            "required_energy_kwh": 1,
        }
    )
    result = solve_schedule(
        [0, 0, 0],
        [0, 0, 0],
        [0.1, 1, 1],
        [0, 0, 0],
        maximum_import_kw=10,
        maximum_export_kw=10,
        heat_pump=hp,
        battery=BatteryLimits(0, 2, 0, 2, 2, 0.81),
    )
    assert result.status == "optimal"
    assert result.grid_import_kw == pytest.approx([1 / 0.81, 0, 0])
    assert result.battery_discharge_kw == pytest.approx([0, 1, 0])
    assert result.battery_soc_kwh == pytest.approx([1 / 0.9, 0, 0])


def test_rejects_future_observation_even_when_other_entity_is_older() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        record = ha_record(request.url.path.split("/")[-1])
        if record["entity_id"].endswith("power"):
            record["last_updated"] = (NOW + timedelta(seconds=1)).isoformat()
        return httpx.Response(200, json=record)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HomeAssistantError, match="future"):
            HomeAssistantHeatPumpImporter(ha_configuration(), client).fetch(now=NOW)


@pytest.mark.parametrize("energy", ["bad", "7000"])
def test_import_attribute_and_invalid_energy(energy: str) -> None:
    configuration = ha_configuration()
    assert configuration.heat_pump is not None
    configuration.heat_pump.required_energy.attribute = "remaining"

    def respond(request: httpx.Request) -> httpx.Response:
        record = ha_record(request.url.path.split("/")[-1])
        record["attributes"]["remaining"] = energy
        return httpx.Response(200, json=record)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HomeAssistantError):
            HomeAssistantHeatPumpImporter(configuration, client).fetch(now=NOW)


@pytest.mark.parametrize(
    "field,value", [("minimum_power_kw", 3), ("maximum_power_kw", 0), ("available", [])]
)
def test_configuration_rejects_invalid_limits(field: str, value: Any) -> None:
    raw = ha_configuration().model_dump()
    raw["heat_pump"][field] = value
    with pytest.raises(ValidationError):
        HomeAssistantConfiguration.model_validate(raw)


def test_import_missing_attribute_and_successful_attribute() -> None:
    configuration = ha_configuration()
    assert configuration.heat_pump is not None
    configuration.heat_pump.required_energy.attribute = "remaining"

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ha_record(request.url.path.split("/")[-1]))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HomeAssistantError, match="missing attribute"):
            HomeAssistantHeatPumpImporter(configuration, client).fetch(now=NOW)

    def with_attribute(request: httpx.Request) -> httpx.Response:
        record = ha_record(request.url.path.split("/")[-1])
        record["attributes"]["remaining"] = 3000
        return httpx.Response(200, json=record)

    with httpx.Client(transport=httpx.MockTransport(with_attribute)) as client:
        assert (
            HomeAssistantHeatPumpImporter(configuration, client)
            .fetch(now=NOW)
            .required_energy_kwh
            == 3
        )


def test_optimizer_horizon_alignment(
    client: TestClient, valid_request: dict[str, Any]
) -> None:
    with client:
        response = client.post(
            "/optimize", json=valid_request | {"heat_pump": heat_pump_payload()}
        )
    assert response.status_code == 422
    assert "horizon must match" in response.text


def test_solver_failure_returns_actionable_service_error(
    client: TestClient, valid_request: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OptimizationError("solver did not reach an optimum: maxTimeLimit")

    monkeypatch.setattr("energy_optimizer.api.routers.core.solve_schedule", fail)
    with client:
        response = client.post("/optimize", json=valid_request)
    assert response.status_code == 503
    assert "maxTimeLimit" in response.text


def test_poll_persist_restore_and_stale_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ha = ha_configuration()
    configuration = Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
            "home_assistant": ha,
            "persistence": {"directory": tmp_path},
            "orchestration": {
                "enabled": True,
                "sources": {"heat_pump": {"interval_seconds": 300}},
            },
        }
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ha_record(request.url.path.split("/")[-1]))

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        importer = HomeAssistantHeatPumpImporter(ha, http)
        monkeypatch.setattr(
            "energy_optimizer.orchestration.HomeAssistantHeatPumpImporter",
            lambda c: importer,
        )
        store = ProviderDataStore(tmp_path)
        orchestrator = build_configured_orchestrator(configuration, store)
        assert orchestrator is not None
        cycle = orchestrator.run_due(now=NOW)
        assert cycle.provider_runs[0].status == "success"
        data = store.load(
            ProviderDataKey("heat-pump", "home-assistant", "heat_pump"),
            TypeAdapter(HeatPumpLoad),
        )
        assert data is not None and data.required_energy_kwh == 3
        restored = build_configured_orchestrator(configuration, store)
        assert restored is not None
        assert restored.registrations[0].load is not None
        assert restored.registrations[0].load() == data
    # No lifespan here: this read uses the configuration and store just exercised.
    monkeypatch.setattr(app.state, "configuration", configuration)
    monkeypatch.setattr(app.state, "provider_data_store", store)
    response = TestClient(app).get("/api/v1/heat-pump")
    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    assert response.json()["freshness"] == "stale"
    dashboard = TestClient(app).get(
        "/api/v1/dashboard/data",
        params={
            "scenario_kind": "forecast",
            "start_time": NOW.isoformat(),
            "end_time": (NOW + timedelta(hours=3)).isoformat(),
        },
    )
    assert dashboard.status_code == 200
    hp_series = dashboard.json()["series"][0]
    assert hp_series["id"] == "heat_pump_forecast"
    assert hp_series["values"] == [1, 1, 1]
    assert hp_series["freshness"] == "stale"
