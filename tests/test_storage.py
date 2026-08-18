"""Tests for durable normalized provider-data storage."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from energy_optimizer import storage as storage_module
from energy_optimizer.providers.interfaces import (
    HouseholdLoadData,
    IntervalQuality,
    SourceMetadata,
)
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
    return directory / f"{filename}.ndjson", directory / f"{filename}.ndjson.bak"


def legacy_paths(directory: Path) -> tuple[Path, Path]:
    filename = f"{KEY.data_type}-{KEY.digest()}"
    return directory / f"{filename}.json", directory / f"{filename}.json.bak"


def test_store_initializes_and_returns_normalized_data(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)

    saved = store.save(KEY, ADAPTER, normalized_data())

    assert store.load(KEY, ADAPTER) == saved
    primary, backup = paths(tmp_path)
    expected_json = json.loads(ADAPTER.dump_json(saved))
    for path in (primary, backup):
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        assert all(json.loads(line)["unit"] == expected_json["unit"] for line in lines)
    expected_payload = (
        b'{"timestamp":"2026-01-01T00:00:00+00:00","load_kw":1.2,"quality":'
        b'{"status":"valid","reason":null,"entity_id":null},"schema_version":"1",'
        b'"unit":"kW","source":{"provider":"home-assistant","entity_id":'
        b'"sensor.household_load"},"retrieved_at":"2026-01-01T00:00:00+00:00",'
        b'"latest_observation_at":"2026-01-01T01:00:00+00:00"}\n'
        b'{"timestamp":"2026-01-01T01:00:00+00:00","load_kw":1.0,"quality":'
        b'{"status":"valid","reason":null,"entity_id":null},"schema_version":"1",'
        b'"unit":"kW","source":{"provider":"home-assistant","entity_id":'
        b'"sensor.household_load"},"retrieved_at":"2026-01-01T00:00:00+00:00",'
        b'"latest_observation_at":"2026-01-01T01:00:00+00:00"}\n'
    )
    assert primary.read_bytes() == expected_payload
    assert backup.read_bytes() == expected_payload
    assert "snapshot" not in primary.read_text().lower()


def test_store_update_keeps_previous_valid_data_as_backup(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, normalized_data([1.2, 1.0]))

    store.save(KEY, ADAPTER, normalized_data([2.4, 2.0]))

    primary, backup = paths(tmp_path)
    current = store.load(KEY, ADAPTER)
    assert current is not None
    assert current.load_kw == (2.4, 2.0)
    assert [
        json.loads(line)["load_kw"] for line in backup.read_text().splitlines()
    ] == [
        1.2,
        1.0,
    ]
    assert [
        json.loads(line)["load_kw"] for line in primary.read_text().splitlines()
    ] == [
        1.2,
        1.0,
        2.4,
        2.0,
    ]


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
    assert [
        json.loads(line)["load_kw"] for line in primary.read_text().splitlines()
    ] == [
        1.2,
        1.0,
    ]
    assert [
        json.loads(line)["load_kw"] for line in backup.read_text().splitlines()
    ] == [
        1.2,
        1.0,
    ]


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


def household_load_data(
    start_time: datetime,
    values: list[float],
    retrieved_at: datetime | None = None,
) -> HouseholdLoadData:
    retrieved = retrieved_at or start_time
    return HouseholdLoadData(
        schema_version="1",
        start_time=start_time,
        interval_minutes=60,
        load_kw=tuple(values),
        unit="kW",
        source=SourceMetadata(provider="home-assistant", entity_id="household_load"),
        retrieved_at=retrieved,
        latest_observation_at=retrieved,
    )


def test_store_merges_hourly_history_and_incoming_values_win(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0, 3.0]))

    merged = store.save(
        KEY,
        ADAPTER,
        household_load_data(
            start + timedelta(hours=2),
            [30.0, 4.0],
            retrieved_at=start + timedelta(hours=4),
        ),
    )

    assert merged.start_time == start
    assert merged.load_kw == (1.0, 2.0, 30.0, 4.0)
    assert merged.retrieved_at == start + timedelta(hours=4)
    reloaded = store.load(KEY, ADAPTER)
    assert reloaded == merged


def test_store_round_trips_interval_quality_and_legacy_records(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = household_load_data(start, [1.0, 2.0, 3.0])
    suspect = HouseholdLoadData(
        **{
            **data.__dict__,
            "quality": (
                IntervalQuality(),
                IntervalQuality(
                    status="suspect",
                    reason="reset_recovery",
                    entity_id="sensor.household_load",
                ),
                IntervalQuality(),
            ),
        }
    )

    saved = store.save(KEY, ADAPTER, suspect)

    assert store.load(KEY, ADAPTER) == saved
    assert saved.quality[1].reason == "reset_recovery"
    primary, _ = paths(tmp_path)
    assert json.loads(primary.read_text().splitlines()[1])["quality"] == {
        "status": "suspect",
        "reason": "reset_recovery",
        "entity_id": "sensor.household_load",
    }


def test_store_accepts_legacy_records_without_quality(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = household_load_data(start, [1.0, 2.0])
    store.save(KEY, ADAPTER, data)
    primary, backup = paths(tmp_path)

    for path in (primary, backup):
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        path.write_bytes(
            b"".join(
                json.dumps(
                    {key: value for key, value in record.items() if key != "quality"},
                    separators=(",", ":"),
                ).encode()
                + b"\n"
                for record in records
            )
        )

    assert store.load(KEY, ADAPTER) == data


def test_store_loads_only_points_in_a_half_open_range(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0, 3.0, 4.0]))

    selected = store.load_household_load_range(
        KEY,
        start + timedelta(hours=1),
        start + timedelta(hours=3),
    )

    assert selected is not None
    assert selected.start_time == start + timedelta(hours=1)
    assert selected.load_kw == (2.0, 3.0)


def test_store_slices_quality_from_retained_start_when_range_predates_history(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    retained_start = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    data = household_load_data(retained_start, [1.0, 2.0])
    suspect = HouseholdLoadData(
        **{
            **data.__dict__,
            "quality": (
                IntervalQuality(),
                IntervalQuality(status="suspect", reason="reset_recovery"),
            ),
        }
    )
    store.save(KEY, ADAPTER, suspect)

    selected = store.load_household_load_range(
        KEY,
        retained_start - timedelta(days=1),
        retained_start + timedelta(hours=2),
    )

    assert selected is not None
    assert selected.start_time == retained_start
    assert selected.load_kw == (1.0, 2.0)
    assert selected.quality[1].reason == "reset_recovery"


def test_store_returns_none_for_a_range_without_points(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))

    assert (
        store.load_household_load_range(
            KEY,
            start + timedelta(hours=3),
            start + timedelta(hours=4),
        )
        is None
    )


def test_store_appends_new_observations_without_rewriting_existing_lines(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()

    store.save(
        KEY,
        ADAPTER,
        household_load_data(start + timedelta(hours=2), [3.0]),
    )

    after = primary.read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == 3


def test_store_ignores_only_an_interrupted_final_append(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    with primary.open("ab") as history_file:
        history_file.write(b'{"timestamp":"2026-01-01T02:00:00+00:00"')

    loaded = store.load(KEY, ADAPTER)

    assert loaded is not None
    assert loaded.load_kw == (1.0, 2.0)


def test_store_rejects_malformed_final_record_without_a_newline(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    backup.unlink()
    primary.write_bytes(primary.read_bytes() + b"not-json")

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        store.load(KEY, ADAPTER)


def test_store_rejects_malformed_nonfinal_ndjson_record(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    backup.unlink()
    primary.write_bytes(primary.read_bytes() + b"not-json\n")

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        store.load(KEY, ADAPTER)


def test_store_rejects_mixed_household_load_sources(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    mixed = household_load_data(start + timedelta(hours=2), [3.0])
    mixed_record = json.loads(
        storage_module._encode_household_records(
            storage_module._household_load_records(mixed)
        ).decode()
    )
    mixed_record["source"]["provider"] = "other-provider"
    with primary.open("ab") as history_file:
        history_file.write(json.dumps(mixed_record).encode() + b"\n")
    backup.unlink()

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        store.load(KEY, ADAPTER)


def test_store_rejects_source_change_before_appending(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    incoming = HouseholdLoadData(
        schema_version="1",
        start_time=start + timedelta(hours=2),
        interval_minutes=60,
        load_kw=(3.0,),
        unit="kW",
        source=SourceMetadata(provider="other-provider", entity_id="household_load"),
        retrieved_at=start,
        latest_observation_at=start,
    )

    with pytest.raises(ProviderDataStoreError, match="source identity"):
        store.save(KEY, ADAPTER, incoming)

    primary, _ = paths(tmp_path)
    assert len(primary.read_text().splitlines()) == 2


def test_store_retains_only_the_newest_ten_years_of_household_load(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    values = [1.0] * 87_672
    store.save(KEY, ADAPTER, household_load_data(start, values))

    merged = store.save(
        KEY,
        ADAPTER,
        household_load_data(start + timedelta(hours=87_671), [2.0, 3.0]),
    )

    assert len(merged.load_kw) == 87_672
    assert merged.start_time == start + timedelta(hours=1)
    assert merged.load_kw[-2:] == (2.0, 3.0)


def test_store_bounds_initial_oversized_household_load_save(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)

    saved = store.save(
        KEY,
        ADAPTER,
        household_load_data(start, [1.0] * (87_672 + 2)),
    )

    assert len(saved.load_kw) == 87_672
    assert saved.start_time == start + timedelta(hours=2)


def test_store_compacts_superseded_records_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 4)
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))

    store.save(
        KEY,
        ADAPTER,
        household_load_data(start + timedelta(hours=2), [3.0, 4.0, 5.0]),
    )

    primary, backup = paths(tmp_path)
    assert len(primary.read_text().splitlines()) == 5
    assert len(backup.read_text().splitlines()) == 2
    loaded = store.load(KEY, ADAPTER)
    assert loaded is not None
    assert loaded.load_kw == (1.0, 2.0, 3.0, 4.0, 5.0)


def test_store_compaction_removes_superseded_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 2)
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    store.save(
        KEY,
        ADAPTER,
        household_load_data(start + timedelta(hours=1), [20.0]),
    )

    primary, _ = paths(tmp_path)
    records = [json.loads(line) for line in primary.read_text().splitlines()]

    assert len(records) == 2
    assert [record["load_kw"] for record in records] == [1.0, 20.0]


def test_store_migrates_legacy_json_primary_and_backup(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = household_load_data(start, [2.0, 3.0])
    previous = household_load_data(start, [1.0, 1.5])
    primary, backup = legacy_paths(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    primary.write_bytes(TypeAdapter(HouseholdLoadData).dump_json(current) + b"\n")
    backup.write_bytes(TypeAdapter(HouseholdLoadData).dump_json(previous) + b"\n")

    loaded = ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    assert loaded == current
    ndjson_primary, ndjson_backup = paths(tmp_path)
    assert ndjson_primary.exists()
    assert ndjson_backup.exists()
    assert not primary.exists()
    assert not backup.exists()


def test_store_migrates_valid_legacy_backup_when_primary_is_invalid(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    previous = household_load_data(start, [1.0, 1.5])
    primary, backup = legacy_paths(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    primary.write_text("invalid", encoding="utf-8")
    backup.write_bytes(TypeAdapter(HouseholdLoadData).dump_json(previous) + b"\n")

    loaded = ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    assert loaded == previous
    ndjson_primary, ndjson_backup = paths(tmp_path)
    assert ndjson_primary.exists()
    assert ndjson_backup.exists()


def test_store_recovers_invalid_ndjson_primary_from_backup(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    primary.write_text("invalid\n", encoding="utf-8")

    recovered = store.load(KEY, ADAPTER)

    assert recovered is not None
    assert recovered.load_kw == (1.0, 2.0)
    assert len(primary.read_text().splitlines()) == 2


def test_store_rebuilds_backup_when_invalid_before_primary_corruption(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    backup.write_text("invalid\n", encoding="utf-8")

    store.save(
        KEY,
        ADAPTER,
        household_load_data(start + timedelta(hours=2), [3.0]),
    )
    primary.write_text("invalid\n", encoding="utf-8")

    recovered = store.load(KEY, ADAPTER)

    assert recovered is not None
    assert recovered.load_kw == (1.0, 2.0)


@pytest.mark.parametrize("failure_target", ["backup", "primary"])
def test_store_compaction_failure_keeps_recoverable_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 2)
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    original_atomic_write = store._atomic_write

    def fail_compaction_write(path: Path, payload: bytes) -> None:
        if path == (backup if failure_target == "backup" else primary):
            raise ProviderDataStoreError("simulated compaction failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", fail_compaction_write)
    with pytest.raises(ProviderDataStoreError, match="simulated compaction failure"):
        store.save(
            KEY,
            ADAPTER,
            household_load_data(start + timedelta(hours=1), [20.0]),
        )

    assert all(json.loads(line) for line in primary.read_text().splitlines())
    assert all(json.loads(line) for line in backup.read_text().splitlines())
    primary.write_text("invalid\n", encoding="utf-8")
    monkeypatch.undo()
    recovered = store.load(KEY, ADAPTER)
    assert recovered is not None
    assert recovered.load_kw == (1.0, 2.0)


def test_non_household_provider_data_remains_json(tmp_path: Path) -> None:
    key = ProviderDataKey("electricity-prices", "test-provider")
    adapter = TypeAdapter(dict[str, int])

    ProviderDataStore(tmp_path).save(key, adapter, {"value": 1})

    assert (tmp_path / f"electricity-prices-{key.digest()}.json").exists()
    assert not (tmp_path / f"electricity-prices-{key.digest()}.ndjson").exists()
