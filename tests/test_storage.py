"""Tests for durable normalized provider-data storage."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from energy_optimizer.providers.interfaces import HouseholdLoadData
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)

KEY = ProviderDataKey(
    data_type="household-load",
    provider="home-assistant",
    entity_id="sensor.household_load",
)
ADAPTER = TypeAdapter(HouseholdLoadData)


def normalized_data(load_kw: list[float] | None = None) -> dict[str, object]:
    return {
        "schema_version": "1",
        "start_time": "2026-01-01T00:00:00+00:00",
        "interval_minutes": 60,
        "load_kw": load_kw or [1.2, 1.0],
        "unit": "kW",
        "source": {
            "provider": "home-assistant",
            "entity_id": "sensor.household_load",
        },
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "latest_observation_at": "2026-01-01T01:00:00+00:00",
    }


def paths(directory: Path) -> tuple[Path, Path]:
    filename = f"{KEY.data_type}-{KEY.digest()}"
    return directory / f"{filename}.json", directory / f"{filename}.json.bak"


def test_store_initializes_and_returns_normalized_data(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)

    saved = store.save(KEY, ADAPTER, normalized_data())

    assert store.load(KEY, ADAPTER) == saved
    primary, backup = paths(tmp_path)
    expected_json = json.loads(ADAPTER.dump_json(saved))
    assert json.loads(primary.read_text()) == expected_json
    assert json.loads(backup.read_text()) == expected_json
    assert "snapshot" not in primary.read_text().lower()


def test_store_update_keeps_previous_valid_data_as_backup(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, normalized_data([1.2, 1.0]))

    store.save(KEY, ADAPTER, normalized_data([2.4, 2.0]))

    primary, backup = paths(tmp_path)
    current = store.load(KEY, ADAPTER)
    assert current is not None
    assert current.load_kw == (2.4, 2.0)
    assert ADAPTER.validate_json(backup.read_bytes()).load_kw == (1.2, 1.0)
    assert ADAPTER.validate_json(primary.read_bytes()).load_kw == (2.4, 2.0)


def test_store_recovers_primary_after_restart(tmp_path: Path) -> None:
    ProviderDataStore(tmp_path).save(KEY, ADAPTER, normalized_data())

    restarted_store = ProviderDataStore(tmp_path)

    current = restarted_store.load(KEY, ADAPTER)
    assert current is not None
    assert current.load_kw == (1.2, 1.0)


def test_store_recovers_invalid_primary_from_backup(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, normalized_data([1.2, 1.0]))
    store.save(KEY, ADAPTER, normalized_data([2.4, 2.0]))
    primary, backup = paths(tmp_path)
    primary.write_text("invalid", encoding="utf-8")

    recovered = store.load(KEY, ADAPTER)

    assert recovered is not None
    assert recovered.load_kw == (1.2, 1.0)
    assert ADAPTER.validate_json(primary.read_bytes()).load_kw == (1.2, 1.0)
    assert ADAPTER.validate_json(backup.read_bytes()).load_kw == (1.2, 1.0)


def test_store_rejects_unrecoverable_invalid_state(tmp_path: Path) -> None:
    primary, backup = paths(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    primary.write_text("invalid", encoding="utf-8")
    backup.write_text("also-invalid", encoding="utf-8")

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        ProviderDataStore(tmp_path).load(KEY, ADAPTER)


def test_invalid_write_does_not_replace_existing_data(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, normalized_data([1.2, 1.0]))

    with pytest.raises(ProviderDataStoreError, match="failed validation"):
        store.save(KEY, ADAPTER, {**normalized_data(), "unit": "W"})

    current = store.load(KEY, ADAPTER)
    assert current is not None
    assert current.load_kw == (1.2, 1.0)


def test_missing_data_returns_none(tmp_path: Path) -> None:
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) is None
