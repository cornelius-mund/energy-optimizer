"""The configured EV history source against a fake InfluxDB behind real HTTP."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import TypeAdapter

from energy_optimizer.config import Configuration
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.providers.interfaces import EvHistoryData
from energy_optimizer.storage import ProviderDataKey, ProviderDataStore
from fake_world import (
    EV_PLUG_ENTITY,
    EV_SOC_ENTITY,
    HOME_ASSISTANT_TOKEN,
    ev_history_soc_percent,
    ev_plugged_in_share,
    running_fake_world,
)

NOW = datetime(2026, 11, 8, 20, 30, tzinfo=UTC)
HISTORY_DAYS = 3
HISTORY_KEY = ProviderDataKey("ev-history", "influxdb", "ev")
ADAPTER = TypeAdapter(EvHistoryData)


def _configuration(world_url: str, data_directory: Path) -> Configuration:
    return Configuration.model_validate(
        {
            "time_resolution_minutes": 60,
            "timezone": "UTC",
            "grid": {"maximum_import_kw": 10, "maximum_export_kw": 10},
            "solver": {"name": "highs", "time_limit_seconds": 60},
            "persistence": {"directory": str(data_directory)},
            "influxdb": {"base_url": world_url, "history_days": HISTORY_DAYS},
            "home_assistant": {
                "base_url": world_url,
                "token": HOME_ASSISTANT_TOKEN,
                "timeout_seconds": 5,
                "ev": {
                    "state_of_charge": {"entity_id": EV_SOC_ENTITY, "unit": "%"},
                    "plugged_in": {"entity_id": EV_PLUG_ENTITY},
                    "capacity_kwh": 60,
                    "maximum_charge_kw": 11,
                },
            },
            "orchestration": {
                "enabled": True,
                "sources": {"ev_history": {"interval_seconds": 3600}},
            },
        }
    )


def test_the_ev_history_is_imported_persisted_and_refreshed(tmp_path: Path) -> None:
    with running_fake_world() as (world_url, world):
        configuration = _configuration(world_url, tmp_path / "provider-data")
        store = ProviderDataStore(tmp_path / "provider-data")
        orchestrator = build_configured_orchestrator(configuration, store)
        assert orchestrator is not None

        orchestrator.run_due(NOW)
        bootstrap_queries = len(world.influx_queries)
        orchestrator.run_due(NOW + timedelta(hours=1))

    history = store.load(HISTORY_KEY, ADAPTER)
    assert history is not None
    end = NOW.replace(minute=0) + timedelta(hours=1)
    assert history.start_time == end - timedelta(days=HISTORY_DAYS)
    assert len(history.state_of_charge_percent) == HISTORY_DAYS * 24
    for index, (soc, plugged) in enumerate(
        zip(history.state_of_charge_percent, history.plugged_in, strict=True)
    ):
        hour = (history.start_time + timedelta(hours=index)).hour
        assert soc == ev_history_soc_percent(hour)
        assert plugged is (ev_plugged_in_share(hour) >= 0.5)
    assert bootstrap_queries == 2
    refresh_queries = world.influx_queries[bootstrap_queries:]
    assert len(refresh_queries) == 2
    assert all("autogen" in statement for statement in refresh_queries)
