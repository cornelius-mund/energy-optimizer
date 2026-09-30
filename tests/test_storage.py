"""Tests for durable normalized provider-data storage."""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from energy_optimizer import storage as storage_module
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
    HourExclusion,
)
from energy_optimizer.providers.home_assistant_battery_efficiency import (
    merge_battery_efficiency_history,
)
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyHistoryData,
    HouseholdLoadData,
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
EFFICIENCY_KEY = ProviderDataKey(
    data_type="battery-efficiency-history",
    provider="home-assistant",
    entity_id="battery_efficiency_history",
)
EFFICIENCY_ADAPTER = TypeAdapter(BatteryEfficiencyHistoryData)
ENERGY_LEGS = (
    "battery_energy_in_kwh",
    "battery_energy_out_kwh",
    "inverter_charge_energy_in_kwh",
    "inverter_charge_energy_out_kwh",
    "inverter_discharge_energy_in_kwh",
    "inverter_discharge_energy_out_kwh",
)


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
        b'{"timestamp":"2026-01-01T00:00:00+00:00","load_kw":1.2,'
        b'"schema_version":"1","unit":"kW","source":{"provider":"home-assistant",'
        b'"entity_id":"sensor.household_load"},'
        b'"retrieved_at":"2026-01-01T00:00:00+00:00",'
        b'"latest_observation_at":"2026-01-01T01:00:00+00:00"}\n'
        b'{"timestamp":"2026-01-01T01:00:00+00:00","load_kw":1.0,'
        b'"schema_version":"1","unit":"kW","source":{"provider":"home-assistant",'
        b'"entity_id":"sensor.household_load"},'
        b'"retrieved_at":"2026-01-01T00:00:00+00:00",'
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


def test_store_logs_ndjson_load_timing_at_debug_without_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))

    with caplog.at_level(logging.DEBUG, logger="energy_optimizer"):
        loaded = store.load(KEY, ADAPTER)

    assert loaded is not None
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=persistence_load_started" in messages
    assert "event=persistence_ndjson_read_started" in messages
    assert "event=persistence_ndjson_parsed" in messages
    assert "event=persistence_load_completed" in messages
    assert "format=ndjson" in messages
    assert "status=restored" in messages
    assert "record_count=2" in messages
    assert "retained_count=2" in messages
    assert "file_size_bytes=" in messages
    assert re.search(r"duration_seconds=\d+\.\d{3}\b", messages)
    assert "load_kw" not in messages
    assert "1.0" not in messages


def test_routine_household_load_reads_stay_out_of_info_output(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with caplog.at_level(logging.INFO, logger="energy_optimizer"):
        assert store.load(KEY, ADAPTER) is None
        store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
        store.load(KEY, ADAPTER)
        store.load_household_load_range(KEY, start, start + timedelta(hours=2))

    events = [
        record.getMessage().split(" ", 1)[0]
        for record in caplog.records
        if record.levelno >= logging.INFO
    ]
    assert events == ["event=persistence_succeeded"]


def test_store_logs_invalid_primary_and_backup_recovery_with_counts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    primary.write_text("invalid\n", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="energy_optimizer"):
        recovered = store.load(KEY, ADAPTER)

    assert recovered is not None
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 2
    invalid, recovery = warnings
    assert invalid.startswith("event=persistence_ndjson_invalid")
    assert f"path={primary.name}" in invalid
    assert "file_size_bytes=8" in invalid
    assert str(tmp_path) not in invalid
    assert recovery.startswith("event=persistence_recovered")
    assert "source=backup" in recovery
    assert "record_count=2" in recovery
    assert re.search(r"duration_seconds=\d+\.\d{3}\b", recovery)


def test_store_logs_legacy_migration_with_count_and_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    primary, backup = legacy_paths(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    legacy = TypeAdapter(HouseholdLoadData).dump_json(
        household_load_data(start, [2.0, 3.0])
    )
    primary.write_bytes(legacy + b"\n")
    backup.write_bytes(legacy + b"\n")

    with caplog.at_level(logging.INFO, logger="energy_optimizer"):
        ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    migration = [
        record.getMessage()
        for record in caplog.records
        if "persistence_migration" in record.getMessage()
    ]
    assert [message.split(" ", 1)[0] for message in migration] == [
        "event=persistence_migration_started",
        "event=persistence_migration_completed",
    ]
    assert "record_count=2" in migration[1]
    assert re.search(r"duration_seconds=\d+\.\d{3}\b", migration[1])
    assert "load_kw" not in "\n".join(record.getMessage() for record in caplog.records)


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
    values: Sequence[float | None],
    retrieved_at: datetime | None = None,
    exclusions: tuple[HourExclusion, ...] = (),
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
        exclusions=exclusions,
    )


def exclusion(
    hour_start: datetime,
    reason: ExclusionReason = "counter_decrease",
    entity_id: str = "sensor.household_energy",
) -> HourExclusion:
    return HourExclusion(
        hour_start,
        (
            ExclusionCause.of(
                reason,
                f"{entity_id} is excluded in the hour starting {hour_start}",
                entity_id,
                [
                    ExcludedDataPoint(
                        hour_start,
                        state="2",
                        unit="kWh",
                        previous_timestamp=hour_start - timedelta(minutes=30),
                        previous_value=3.0,
                        value=2.0,
                        step_kwh=-1.0,
                        maximum_kwh=100.0,
                    )
                ],
            ),
        ),
    )


def legacy_record(
    hour: int,
    load_kw: float,
    quality: dict[str, object] | None,
) -> bytes:
    """Return one NDJSON record as versions before exclusion wrote it."""
    record: dict[str, object] = {
        "timestamp": (datetime(2026, 1, 1, hour, tzinfo=timezone.utc)).isoformat(),
        "load_kw": load_kw,
    }
    if quality is not None:
        record["quality"] = quality
    record |= {
        "schema_version": "1",
        "unit": "kW",
        "source": {"provider": "home-assistant", "entity_id": "household_load"},
        "retrieved_at": "2026-01-01T00:00:00+00:00",
        "latest_observation_at": "2026-01-01T03:00:00+00:00",
    }
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


SUSPECT_QUALITY: dict[str, object] = {
    "status": "suspect",
    "reason": "reset_recovery",
    "entity_id": "sensor.household_load",
}
VALID_QUALITY: dict[str, object] = {
    "status": "valid",
    "reason": None,
    "entity_id": None,
}


def write_legacy_household_ndjson(directory: Path) -> None:
    """Persist household load in which an earlier version flagged hour 1 suspect."""
    payload = (
        legacy_record(0, 1.0, VALID_QUALITY)
        + legacy_record(1, 2.0, SUSPECT_QUALITY)
        + legacy_record(2, 3.0, VALID_QUALITY)
    )
    directory.mkdir(exist_ok=True)
    primary, backup = paths(directory)
    primary.write_bytes(payload)
    backup.write_bytes(payload)


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


def test_store_round_trips_excluded_hours_after_restart(tmp_path: Path) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    excluded = exclusion(start + timedelta(hours=1))
    data = household_load_data(start, [1.0, None, 3.0], exclusions=(excluded,))

    saved = ProviderDataStore(tmp_path).save(KEY, ADAPTER, data)
    restarted = ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    assert saved == data
    assert restarted == data
    assert restarted is not None
    assert restarted.load_kw == (1.0, None, 3.0)
    assert restarted.exclusions == (excluded,)
    assert restarted.quality == ()
    primary, backup = paths(tmp_path)
    for path in (primary, backup):
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert [record["load_kw"] for record in records] == [1.0, None, 3.0]
        assert all("quality" not in record for record in records)
        assert [("exclusion" in record) for record in records] == [False, True, False]
    stored = json.loads(primary.read_text().splitlines()[1])["exclusion"]
    assert stored["hour_start"] == "2026-01-01T01:00:00Z"
    assert stored["causes"][0]["reason"] == "counter_decrease"
    assert stored["causes"][0]["entity_id"] == "sensor.household_energy"
    assert stored["causes"][0]["data_points"][0]["state"] == "2"
    assert stored["causes"][0]["data_points"][0]["step_kwh"] == -1.0


def test_store_appends_excluded_hours_and_keeps_earlier_ones_after_restart(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = exclusion(start)
    third = exclusion(start + timedelta(hours=2), "unavailable")
    store = ProviderDataStore(tmp_path)
    store.save(
        KEY, ADAPTER, household_load_data(start, [None, 2.0], exclusions=(first,))
    )

    saved = store.save(
        KEY,
        ADAPTER,
        household_load_data(
            start + timedelta(hours=2), [None, 4.0], exclusions=(third,)
        ),
    )

    restarted = ProviderDataStore(tmp_path).load(KEY, ADAPTER)
    assert saved.load_kw == (None, 2.0, None, 4.0)
    assert restarted == saved
    assert restarted is not None
    assert restarted.exclusions == (first, third)


def test_store_replaces_an_excluded_hour_with_a_valid_incoming_hour(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    excluded = exclusion(start + timedelta(hours=1))
    store.save(
        KEY, ADAPTER, household_load_data(start, [1.0, None], exclusions=(excluded,))
    )

    store.save(KEY, ADAPTER, household_load_data(start + timedelta(hours=1), [2.5]))

    restarted = ProviderDataStore(tmp_path).load(KEY, ADAPTER)
    assert restarted is not None
    assert restarted.load_kw == (1.0, 2.5)
    assert restarted.exclusions == ()


def test_store_replaces_a_valid_hour_with_an_incoming_exclusion(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    excluded = exclusion(start + timedelta(hours=1), "unavailable")

    store.save(
        KEY,
        ADAPTER,
        household_load_data(start + timedelta(hours=1), [None], exclusions=(excluded,)),
    )

    restarted = ProviderDataStore(tmp_path).load(KEY, ADAPTER)
    assert restarted is not None
    assert restarted.load_kw == (1.0, None)
    assert restarted.exclusions == (excluded,)


def test_merge_prefers_incoming_hours_completely_including_their_exclusion() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hour = [start + timedelta(hours=index) for index in range(3)]
    existing = household_load_data(
        start,
        [1.0, None, 3.0],
        exclusions=(exclusion(hour[1], "counter_decrease"),),
    )
    incoming = household_load_data(
        hour[1],
        [None, None],
        exclusions=(
            exclusion(hour[1], "unavailable"),
            exclusion(hour[2], "unavailable"),
        ),
    )

    merged = storage_module.merge_household_load_history(existing, incoming)

    assert merged.load_kw == (1.0, None, None)
    assert merged.exclusions == (
        exclusion(hour[1], "unavailable"),
        exclusion(hour[2], "unavailable"),
    )


@pytest.mark.parametrize(
    "hour_of_exclusion",
    [None, 0, 1],
    ids=[
        "value-null-without-exclusion",
        "exclusion-of-other-hour",
        "value-and-exclusion",
    ],
)
def test_store_rejects_a_household_record_whose_value_and_exclusion_disagree(
    tmp_path: Path, hour_of_exclusion: int | None
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    backup.unlink()
    first, second = (json.loads(line) for line in primary.read_text().splitlines())
    if hour_of_exclusion is None:
        # The record has no value but nothing explains its absence.
        second["load_kw"] = None
    else:
        # Hour 0's exclusion sits on hour 1's record (another hour); hour 1's
        # exclusion sits on a record that still has its value.
        second["exclusion"] = TypeAdapter(HourExclusion).dump_python(
            exclusion(start + timedelta(hours=hour_of_exclusion)), mode="json"
        )
        second["load_kw"] = None if hour_of_exclusion == 0 else 2.0
    primary.write_bytes(
        b"".join(json.dumps(record).encode() + b"\n" for record in (first, second))
    )

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        ProviderDataStore(tmp_path).load(KEY, ADAPTER)


def test_store_rejects_household_hours_without_a_value_that_lack_an_exclusion(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with pytest.raises(ProviderDataStoreError, match="match their exclusions"):
        ProviderDataStore(tmp_path).save(
            KEY, ADAPTER, household_load_data(start, [1.0, None])
        )


def test_store_converts_suspect_records_of_legacy_ndjson_into_excluded_hours(
    tmp_path: Path,
) -> None:
    write_legacy_household_ndjson(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    loaded = ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    assert loaded is not None
    assert loaded.load_kw == (1.0, None, 3.0)
    assert loaded.quality == ()
    assert [item.hour_start for item in loaded.exclusions] == [
        start + timedelta(hours=1)
    ]
    (cause,) = loaded.exclusions[0].causes
    assert cause.reason == "flagged_by_earlier_version"
    assert cause.entity_id == "sensor.household_load"
    assert cause.data_points == ()
    assert "reset_recovery" in cause.message


def test_store_appends_new_records_without_quality_to_legacy_ndjson(
    tmp_path: Path,
) -> None:
    write_legacy_household_ndjson(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()

    saved = store.save(
        KEY, ADAPTER, household_load_data(start + timedelta(hours=3), [4.0])
    )

    assert saved.load_kw == (1.0, None, 3.0, 4.0)
    assert saved.exclusions[0].causes[0].reason == "flagged_by_earlier_version"
    appended = primary.read_bytes()[len(before) :]
    assert b"quality" not in appended
    assert json.loads(appended)["load_kw"] == 4.0
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == saved


def test_store_compaction_rewrites_legacy_ndjson_in_the_current_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 3)
    write_legacy_household_ndjson(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)

    saved = store.save(
        KEY, ADAPTER, household_load_data(start + timedelta(hours=3), [4.0])
    )

    primary, backup = paths(tmp_path)
    for path in (primary, backup):
        assert b"quality" not in path.read_bytes()
    records = [json.loads(line) for line in primary.read_text().splitlines()]
    assert [record["load_kw"] for record in records] == [1.0, None, 3.0, 4.0]
    assert records[1]["exclusion"]["causes"][0]["reason"] == (
        "flagged_by_earlier_version"
    )
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == saved


def test_a_valid_incoming_hour_supersedes_a_suspect_record_of_legacy_ndjson(
    tmp_path: Path,
) -> None:
    write_legacy_household_ndjson(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)

    store.save(KEY, ADAPTER, household_load_data(start + timedelta(hours=1), [2.5]))

    restarted = ProviderDataStore(tmp_path).load(KEY, ADAPTER)
    assert restarted is not None
    assert restarted.load_kw == (1.0, 2.5, 3.0)
    assert restarted.exclusions == ()


def test_store_converts_suspect_hours_when_it_migrates_monolithic_json(
    tmp_path: Path,
) -> None:
    quality = [VALID_QUALITY, SUSPECT_QUALITY, VALID_QUALITY]
    legacy = json.dumps(
        {
            "schema_version": "1",
            "start_time": "2026-01-01T00:00:00+00:00",
            "interval_minutes": 60,
            "load_kw": [1.0, 2.0, 3.0],
            "unit": "kW",
            "source": {"provider": "home-assistant", "entity_id": "household_load"},
            "retrieved_at": "2026-01-01T00:00:00+00:00",
            "latest_observation_at": "2026-01-01T03:00:00+00:00",
            "quality": quality,
        }
    ).encode()
    primary, backup = legacy_paths(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    primary.write_bytes(legacy)
    backup.write_bytes(legacy)

    loaded = ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    assert loaded is not None
    assert loaded.load_kw == (1.0, None, 3.0)
    assert loaded.quality == ()
    assert loaded.exclusions[0].causes[0].reason == "flagged_by_earlier_version"
    ndjson_primary, ndjson_backup = paths(tmp_path)
    for path in (ndjson_primary, ndjson_backup):
        assert b"quality" not in path.read_bytes()
    records = [json.loads(line) for line in ndjson_primary.read_text().splitlines()]
    assert [record["load_kw"] for record in records] == [1.0, None, 3.0]
    assert "exclusion" in records[1]
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == loaded


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


def test_store_slices_exclusions_from_retained_start_when_range_predates_history(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    retained_start = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    excluded = exclusion(retained_start + timedelta(hours=1))
    store.save(
        KEY,
        ADAPTER,
        household_load_data(retained_start, [1.0, None], exclusions=(excluded,)),
    )

    selected = store.load_household_load_range(
        KEY,
        retained_start - timedelta(days=1),
        retained_start + timedelta(hours=2),
    )

    assert selected is not None
    assert selected.start_time == retained_start
    assert selected.load_kw == (1.0, None)
    assert selected.exclusions == (excluded,)


def test_store_slices_only_the_exclusions_of_the_requested_hours(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    excluded = exclusion(start + timedelta(hours=1))
    store.save(
        KEY,
        ADAPTER,
        household_load_data(start, [1.0, None, 3.0, 4.0], exclusions=(excluded,)),
    )

    inside = store.load_household_load_range(
        KEY, start + timedelta(hours=1), start + timedelta(hours=3)
    )
    outside = store.load_household_load_range(
        KEY, start + timedelta(hours=2), start + timedelta(hours=4)
    )

    assert inside is not None
    assert inside.load_kw == (None, 3.0)
    assert inside.exclusions == (excluded,)
    assert outside is not None
    assert outside.load_kw == (3.0, 4.0)
    assert outside.exclusions == ()


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


def test_merge_excludes_hours_between_history_and_a_later_range_as_unavailable() -> (
    None
):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    existing = household_load_data(start, [1.0, 2.0])
    incoming = household_load_data(
        start + timedelta(hours=5), [3.0, 4.0], retrieved_at=start + timedelta(days=1)
    )

    merged = storage_module.merge_household_load_history(existing, incoming)

    assert merged.start_time == start
    assert merged.load_kw == (1.0, 2.0, None, None, None, 3.0, 4.0)
    assert [item.hour_start for item in merged.exclusions] == [
        start + timedelta(hours=hour) for hour in (2, 3, 4)
    ]
    for item in merged.exclusions:
        assert [
            (cause.reason, cause.entity_id, cause.data_points, cause.data_point_count)
            for cause in item.causes
        ] == [("history_unavailable", None, (), 0)]
        assert item.causes[0].message == (
            "The provider holds no history from 2026-01-01T02:00:00+00:00 until "
            "2026-01-01T05:00:00+00:00 (3 hours), so these hours cannot be imported."
        )
    assert merged.retrieved_at == incoming.retrieved_at


@pytest.mark.parametrize(
    ("incoming_start_hour", "expected"),
    [
        (1, (1.0, 9.0, 8.0)),
        (2, (1.0, 2.0, 9.0, 8.0)),
    ],
)
def test_merge_of_an_overlapping_or_adjacent_range_creates_no_exclusion(
    incoming_start_hour: int, expected: tuple[float, ...]
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    merged = storage_module.merge_household_load_history(
        household_load_data(start, [1.0, 2.0]),
        household_load_data(start + timedelta(hours=incoming_start_hour), [9.0, 8.0]),
    )

    assert merged.load_kw == expected
    assert merged.exclusions == ()


def test_merge_still_rejects_a_range_that_leaves_a_gap_after_the_incoming_hours() -> (
    None
):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with pytest.raises(ProviderDataStoreError, match="contiguous"):
        storage_module.merge_household_load_history(
            household_load_data(start + timedelta(hours=5), [1.0]),
            household_load_data(start, [2.0]),
        )


def test_store_persists_a_gap_that_survives_restart_and_continues_incrementally(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()

    saved = store.save(
        KEY, ADAPTER, household_load_data(start + timedelta(hours=5), [3.0])
    )

    assert saved.load_kw == (1.0, 2.0, None, None, None, 3.0)
    # The gap is appended like any other hour, so the file stays contiguous.
    assert primary.read_bytes().startswith(before)
    records = [json.loads(line) for line in primary.read_text().splitlines()]
    assert [record["timestamp"] for record in records] == [
        (start + timedelta(hours=hour)).isoformat() for hour in range(6)
    ]
    assert [record["load_kw"] for record in records] == [
        1.0,
        2.0,
        None,
        None,
        None,
        3.0,
    ]
    assert [
        record.get("exclusion", {}).get("causes", [{}])[0].get("reason")
        for record in records
    ] == [None, None] + ["history_unavailable"] * 3 + [None]
    restarted = ProviderDataStore(tmp_path).load(KEY, ADAPTER)
    assert restarted == saved
    continued = ProviderDataStore(tmp_path).save(
        KEY, ADAPTER, household_load_data(start + timedelta(hours=6), [4.0])
    )
    assert continued.load_kw == (1.0, 2.0, None, None, None, 3.0, 4.0)
    assert len(continued.exclusions) == 3


def test_store_lets_a_later_submission_of_the_missing_hours_replace_the_gap(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0]))
    store.save(KEY, ADAPTER, household_load_data(start + timedelta(hours=3), [4.0]))

    filled = store.save(
        KEY, ADAPTER, household_load_data(start + timedelta(hours=1), [2.0, 3.0])
    )

    assert filled.load_kw == (1.0, 2.0, 3.0, 4.0)
    assert filled.exclusions == ()
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == filled


def test_store_lists_gap_hours_in_a_range_query_like_any_excluded_hour(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0]))
    store.save(KEY, ADAPTER, household_load_data(start + timedelta(hours=3), [2.0]))

    ranged = store.load_household_load_range(
        KEY, start + timedelta(hours=1), start + timedelta(hours=3)
    )

    assert ranged is not None
    assert ranged.load_kw == (None, None)
    assert [cause.reason for item in ranged.exclusions for cause in item.causes] == [
        "history_unavailable"
    ] * 2


def test_store_rejects_a_gap_range_from_another_source_without_changing_history(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0]))
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()
    other = replace(
        household_load_data(start + timedelta(hours=4), [2.0]),
        source=SourceMetadata(provider="home-assistant", entity_id="other"),
    )

    with pytest.raises(ProviderDataStoreError, match="source identity"):
        store.save(KEY, ADAPTER, other)

    assert primary.read_bytes() == before


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


def efficiency_path(directory: Path) -> Path:
    return directory / f"{EFFICIENCY_KEY.data_type}-{EFFICIENCY_KEY.digest()}.json"


def efficiency_history(
    start: datetime,
    hours: int,
    *,
    excluded: tuple[int, ...] = (),
) -> BatteryEfficiencyHistoryData:
    """Build a history whose excluded hours have no energy and no adjacent SoC."""
    energy = tuple(None if index in excluded else 0.5 for index in range(hours))
    boundaries = set(excluded) | {index - 1 for index in excluded}
    return BatteryEfficiencyHistoryData(
        schema_version="1",
        start_time=start,
        interval_minutes=60,
        battery_energy_in_kwh=energy,
        battery_energy_out_kwh=energy,
        inverter_charge_energy_in_kwh=energy,
        inverter_charge_energy_out_kwh=energy,
        inverter_discharge_energy_in_kwh=energy,
        inverter_discharge_energy_out_kwh=energy,
        state_of_charge_percent=tuple(
            None if index in boundaries else 20.0 + 10 * index
            for index in range(hours + 1)
        ),
        unit="kWh",
        source=SourceMetadata("home-assistant", "battery_efficiency_history"),
        retrieved_at=start,
        latest_observation_at=start + timedelta(hours=hours),
        exclusions=tuple(
            exclusion(
                start + timedelta(hours=index),
                "counter_decrease",
                "sensor.battery_in",
            )
            for index in sorted(excluded)
        ),
    )


def test_store_round_trips_excluded_efficiency_history_hours_after_restart(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    data = efficiency_history(start, 4, excluded=(2,))

    saved = ProviderDataStore(tmp_path).save(EFFICIENCY_KEY, EFFICIENCY_ADAPTER, data)
    restarted = ProviderDataStore(tmp_path).load(EFFICIENCY_KEY, EFFICIENCY_ADAPTER)

    assert saved == data
    assert restarted == data
    assert restarted is not None
    for name in ENERGY_LEGS:
        assert getattr(restarted, name) == (0.5, 0.5, None, 0.5), name
    assert restarted.state_of_charge_percent == (20.0, None, None, 50.0, 60.0)
    assert restarted.exclusions == (
        exclusion(start + timedelta(hours=2), "counter_decrease", "sensor.battery_in"),
    )
    payload = json.loads(efficiency_path(tmp_path).read_text())
    assert payload["battery_energy_in_kwh"] == [0.5, 0.5, None, 0.5]
    assert payload["state_of_charge_percent"] == [20.0, None, None, 50.0, 60.0]
    assert payload["quality"] == []
    assert payload["exclusions"][0]["hour_start"] == "2026-01-01T02:00:00Z"
    assert payload["exclusions"][0]["causes"][0]["reason"] == "counter_decrease"


def write_legacy_efficiency_history(directory: Path) -> None:
    """Persist a history in which an earlier version flagged hour 2 as suspect."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    valid = {"status": "valid", "reason": None, "entity_id": None}
    suspect = {
        "status": "suspect",
        "reason": "counter_reset",
        "entity_id": "sensor.battery_in",
    }
    payload: dict[str, object] = {
        "schema_version": "1",
        "start_time": start.isoformat(),
        "interval_minutes": 60,
        **{name: [0.5, 0.5, 0.5, 0.5] for name in ENERGY_LEGS},
        "state_of_charge_percent": [20.0, 30.0, 40.0, 50.0, 60.0],
        "unit": "kWh",
        "source": {
            "provider": "home-assistant",
            "entity_id": "battery_efficiency_history",
        },
        "retrieved_at": start.isoformat(),
        "latest_observation_at": (start + timedelta(hours=4)).isoformat(),
        "quality": [valid, valid, suspect, valid],
    }
    directory.mkdir(exist_ok=True)
    efficiency_path(directory).write_text(json.dumps(payload), encoding="utf-8")


def test_store_converts_suspect_hours_of_legacy_efficiency_history(
    tmp_path: Path,
) -> None:
    write_legacy_efficiency_history(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    loaded = ProviderDataStore(tmp_path).load(EFFICIENCY_KEY, EFFICIENCY_ADAPTER)

    assert loaded is not None
    for name in ENERGY_LEGS:
        assert getattr(loaded, name) == (0.5, 0.5, None, 0.5), name
    assert loaded.quality == ()
    # The boundary value of the excluded hour is dropped and values far from it
    # are unchanged; boundary values directly next to it are not asserted.
    assert loaded.state_of_charge_percent[0] == 20.0
    assert loaded.state_of_charge_percent[2] is None
    assert loaded.state_of_charge_percent[4] == 60.0
    assert len(loaded.state_of_charge_percent) == 5
    assert [item.hour_start for item in loaded.exclusions] == [
        start + timedelta(hours=2)
    ]
    (cause,) = loaded.exclusions[0].causes
    assert cause.reason == "flagged_by_earlier_version"
    assert cause.entity_id == "sensor.battery_in"
    assert cause.data_points == ()
    assert "counter_reset" in cause.message


def test_store_persists_converted_efficiency_history_without_quality(
    tmp_path: Path,
) -> None:
    write_legacy_efficiency_history(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = ProviderDataStore(tmp_path)
    existing = store.load(EFFICIENCY_KEY, EFFICIENCY_ADAPTER)
    incoming = efficiency_history(start + timedelta(hours=4), 1)

    merged = merge_battery_efficiency_history(existing, incoming)
    saved = store.save(EFFICIENCY_KEY, EFFICIENCY_ADAPTER, merged)

    payload = json.loads(efficiency_path(tmp_path).read_text())
    assert payload["quality"] == []
    assert payload["battery_energy_in_kwh"] == [0.5, 0.5, None, 0.5, 0.5]
    assert [item["hour_start"] for item in payload["exclusions"]] == [
        "2026-01-01T02:00:00Z"
    ]
    assert payload["exclusions"][0]["causes"][0]["reason"] == (
        "flagged_by_earlier_version"
    )
    assert ProviderDataStore(tmp_path).load(EFFICIENCY_KEY, EFFICIENCY_ADAPTER) == saved
