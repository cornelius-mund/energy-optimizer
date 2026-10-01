"""PV actuals use validated counter history and remain separate from forecasts."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from energy_optimizer.api import app
from energy_optimizer.config import Configuration, EnergyAggregateConfiguration
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.providers.home_assistant_history import (
    HomeAssistantError,
    HomeAssistantHistoryImporter,
)
from energy_optimizer.providers.home_assistant_pv import (
    HomeAssistantPvImporter,
    PvGenerationHistoryData,
)
from energy_optimizer.providers.interfaces import PvGenerationData, SourceMetadata
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)
from home_assistant_fixtures import FakeHomeAssistant, aggregate_settings

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
KEY = ProviderDataKey("pv-generation-history", "home-assistant", "pv_generation")
ADAPTER = TypeAdapter(PvGenerationHistoryData)


def configuration(directory: Path, *, shared: bool = False) -> Configuration:
    mapping = aggregate_settings(
        add=[
            {
                "entity_id": "sensor.pv",
                "unit": "kWh",
                "state_class": "total_increasing",
            }
        ]
    )
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
                "pv_generation": mapping,
                "energy_history": {"same_meter": mapping} if shared else {},
            },
            "forecast_solar": {
                "latitude": 52.52,
                "longitude": 13.41,
                "declination_degrees": 35,
                "azimuth_degrees": 0,
                "peak_power_kw": 8,
            },
            "orchestration": {
                "enabled": True,
                "sources": {
                    "pv_generation_history": {"interval_seconds": 3600},
                    **(
                        {"history.same_meter": {"interval_seconds": 3600}}
                        if shared
                        else {}
                    ),
                },
            },
        }
    )


def imported(
    config: Configuration, fake: FakeHomeAssistant, hours: int = 2
) -> PvGenerationHistoryData:
    assert config.home_assistant is not None
    plan = HomeAssistantPvImporter(config.home_assistant).plan(
        BASE, BASE + timedelta(hours=hours), now=BASE + timedelta(hours=hours)
    )
    with fake.client() as client:
        history = HomeAssistantHistoryImporter(
            config.home_assistant, client
        ).import_history(plan.needs)
        return plan.build(history)


def test_aggregate_units_and_utc_metadata(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    assert config.home_assistant is not None
    config.home_assistant.pv_generation = EnergyAggregateConfiguration.model_validate(
        aggregate_settings(
            add=[
                {
                    "entity_id": "sensor.pv",
                    "unit": "Wh",
                    "state_class": "total_increasing",
                },
                {"entity_id": "sensor.east", "unit": "kWh", "state_class": "total"},
            ]
        )
    )
    fake = FakeHomeAssistant(
        {
            "sensor.pv": [
                (BASE, "1000"),
                (BASE + timedelta(hours=1), "3000"),
                (BASE + timedelta(hours=2), "3000"),
            ],
            "sensor.east": [
                (BASE, "1"),
                (BASE + timedelta(hours=1), "2"),
                (BASE + timedelta(hours=2), "2"),
            ],
        },
        units={"sensor.pv": "Wh"},
        state_classes={"sensor.east": "total"},
    )
    data = imported(config, fake)
    assert data.generation_kw == (3, 0)
    assert data.scenario_kind == "actual" and data.unit == "kW"
    assert data.source == SourceMetadata("home-assistant", "pv_generation")
    assert data.start_time == BASE
    assert data.retrieved_at == data.latest_observation_at == BASE + timedelta(hours=2)


@pytest.mark.parametrize("value", ["unavailable", "unknown", "bad", "NaN", "-1"])
def test_invalid_counter_samples_are_excluded(tmp_path: Path, value: str) -> None:
    data = imported(
        configuration(tmp_path),
        FakeHomeAssistant(
            {
                "sensor.pv": [
                    (BASE, "0"),
                    (BASE + timedelta(hours=1), value),
                    (BASE + timedelta(hours=2), "4"),
                ]
            }
        ),
    )
    assert None in data.generation_kw
    assert data.exclusions
    assert {
        BASE + timedelta(hours=i) for i, v in enumerate(data.generation_kw) if v is None
    } == {e.hour_start for e in data.exclusions}


@pytest.mark.parametrize(
    "units,classes", [({"sensor.pv": "Wh"}, {}), ({}, {"sensor.pv": "measurement"})]
)
def test_entity_semantics_are_validated(
    tmp_path: Path, units: dict[str, str], classes: dict[str, str]
) -> None:
    data = imported(
        configuration(tmp_path),
        FakeHomeAssistant(
            {
                "sensor.pv": [
                    (BASE, "0"),
                    (BASE + timedelta(hours=1), "2"),
                    (BASE + timedelta(hours=2), "4"),
                ]
            },
            units=units,
            state_classes=classes,
        ),
    )
    assert all(value is None for value in data.generation_kw)
    assert data.exclusions


@pytest.mark.parametrize(
    "status, message",
    [
        (401, "authentication"),
        (403, "authentication"),
        (404, "not found"),
        (500, "500"),
    ],
)
def test_provider_errors_are_actionable(
    tmp_path: Path, status: int, message: str
) -> None:
    with pytest.raises(HomeAssistantError, match=message):
        imported(
            configuration(tmp_path),
            FakeHomeAssistant({}, failures={"sensor.pv": status}),
        )


def test_incremental_shared_import_persistence_and_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path, shared=True)
    fake = FakeHomeAssistant(
        {"sensor.pv": [(BASE + timedelta(hours=h), str(h * 2)) for h in range(4)]}
    )
    store = ProviderDataStore(tmp_path)
    with fake.client() as client:
        orchestrator = build_configured_orchestrator(
            config, store, home_assistant_client=client
        )
        assert orchestrator is not None
        assert all(
            run.status == "success"
            for run in orchestrator.run_due(BASE + timedelta(hours=2)).provider_runs
        )
        first_requests = list(fake.requests)
        # A source sharing the PV meter adds no second request for the same range.
        assert len(first_requests) == len(
            set((r.entity_id, r.start_time, r.end_time) for r in first_requests)
        )
        fake.requests.clear()
        assert all(
            run.status == "success"
            for run in orchestrator.run_due(BASE + timedelta(hours=3)).provider_runs
        )
        assert all(r.start_time >= BASE + timedelta(hours=2) for r in fake.requests)
        before = store.load(KEY, ADAPTER)
        assert before is not None and before.generation_kw == (2, 2, 2)
        fake.failures["sensor.pv"] = 404
        result = orchestrator.run_due(BASE + timedelta(hours=4))
        assert all(run.status == "failed" for run in result.provider_runs)
        assert store.load(KEY, ADAPTER) == before
    forecast_key = ProviderDataKey("pv-generation", "forecast.solar", "pv_generation")
    forecast = PvGenerationData(
        "1",
        BASE,
        60,
        (99, 99, 99),
        "kW",
        SourceMetadata("forecast.solar", "pv_generation"),
        BASE,
        BASE + timedelta(days=1),
    )
    store.save(forecast_key, TypeAdapter(PvGenerationData), forecast)
    monkeypatch.setattr(app.state, "configuration", config, raising=False)
    monkeypatch.setattr(app.state, "provider_data_store", store, raising=False)
    response = TestClient(app).get(
        "/api/v1/dashboard/data",
        params={
            "scenario_kind": "actual",
            "start_time": BASE.isoformat(),
            "end_time": (BASE + timedelta(hours=4)).isoformat(),
        },
    )
    assert response.status_code == 200
    pv = next(
        item
        for item in response.json()["series"]
        if item["id"] == "pv_generation_actual"
    )
    assert pv["values"] == [2, 2, 2, None]
    assert (
        pv["scenario_kind"] == "actual" and pv["source"]["provider"] == "home-assistant"
    )
    assert store.load(forecast_key, TypeAdapter(PvGenerationData)) == forecast


def test_exclusions_and_backup_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    data = imported(
        config,
        FakeHomeAssistant(
            {
                "sensor.pv": [
                    (BASE, "0"),
                    (BASE + timedelta(hours=1), "unavailable"),
                    (BASE + timedelta(hours=2), "4"),
                ]
            }
        ),
    )
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, data)
    primary = tmp_path / f"{KEY.data_type}-{KEY.digest()}.json"
    primary.write_text('{"corrupt":true}')
    assert store.load(KEY, ADAPTER) == data
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
    pv_hours = [
        item
        for item in response.json()["hours"]
        if item["source"] == "pv_generation_history"
    ]
    assert len(pv_hours) == len(data.exclusions)
    assert all(item["causes"] for item in pv_hours)
    with pytest.raises(ProviderDataStoreError):
        store.save(
            KEY, ADAPTER, replace(data, generation_kw=(float("nan"), 0), exclusions=())
        )


@pytest.mark.parametrize(
    "mapping",
    [
        {"terms": []},
        aggregate_settings(
            add=[{"entity_id": "sensor.pv", "unit": "W", "state_class": "measurement"}]
        ),
    ],
)
def test_invalid_configuration(tmp_path: Path, mapping: dict[str, Any]) -> None:
    document = configuration(tmp_path).model_dump()
    document["home_assistant"]["pv_generation"] = mapping
    with pytest.raises(ValidationError):
        Configuration.model_validate(document)


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"invalid": "response"}, "one entity series"),
        ([], "no history"),
        ([[{"state": "0", "last_updated": "invalid"}]], "timestamp"),
        ([[{"state": "0", "last_updated": "2026-01-01T00:00:00"}]], "timezone"),
    ],
)
def test_malformed_or_missing_history_is_actionable(
    tmp_path: Path, payload: Any, message: str
) -> None:
    config = configuration(tmp_path)
    assert config.home_assistant is not None
    plan = HomeAssistantPvImporter(config.home_assistant).plan(
        BASE, BASE + timedelta(hours=2)
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as client:
        history = HomeAssistantHistoryImporter(
            config.home_assistant, client
        ).import_history(plan.needs)
        with pytest.raises(HomeAssistantError, match=message):
            plan.build(history)


def test_timeout_and_instantaneous_sensor_are_actionable(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    assert config.home_assistant is not None
    plan = HomeAssistantPvImporter(config.home_assistant).plan(
        BASE, BASE + timedelta(hours=1)
    )

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test timeout", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        history = HomeAssistantHistoryImporter(
            config.home_assistant, client
        ).import_history(plan.needs)
        with pytest.raises(HomeAssistantError, match="timed out"):
            plan.build(history)
    with pytest.raises(HomeAssistantError, match="instantaneous"):
        imported(
            config,
            FakeHomeAssistant(
                {
                    "sensor.pv": [
                        (BASE, "0"),
                        (BASE + timedelta(hours=1), "2"),
                    ]
                },
                units={"sensor.pv": "W"},
            ),
            hours=1,
        )


def test_retention_gap_is_persisted_without_freezing_refresh(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    fake = FakeHomeAssistant(
        {
            "sensor.pv": [
                (BASE, "0"),
                (BASE + timedelta(hours=1), "2"),
            ]
        }
    )
    store = ProviderDataStore(tmp_path)
    with fake.client() as client:
        orchestrator = build_configured_orchestrator(
            config, store, home_assistant_client=client
        )
        assert orchestrator is not None
        orchestrator.run_due(BASE + timedelta(hours=1))
        fake.states = {
            "sensor.pv": [
                (BASE + timedelta(hours=4), "8"),
                (BASE + timedelta(hours=5), "10"),
                (BASE + timedelta(hours=6), "12"),
            ]
        }
        cycle = orchestrator.run_due(BASE + timedelta(hours=5))
        assert cycle.provider_runs[0].status == "success"
        data = store.load(KEY, ADAPTER)
        assert data is not None and data.generation_kw == (2, None, None, None, 2)
        assert len(data.exclusions) == 3
        fake.requests.clear()
        orchestrator.run_due(BASE + timedelta(hours=6))
        assert all(
            request.start_time >= BASE + timedelta(hours=5) for request in fake.requests
        )
        data = store.load(KEY, ADAPTER)
        assert data is not None and data.generation_kw == (2, None, None, None, 2, 2)
