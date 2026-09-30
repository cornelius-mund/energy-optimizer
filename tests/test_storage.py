"""Tests for durable normalized provider-data storage."""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
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
DURATION_SECONDS = re.compile(r"duration_seconds=\d+\.\d{3}\b")


@pytest.fixture
def store(tmp_path: Path) -> ProviderDataStore:
    return ProviderDataStore(tmp_path)


def at(hour: int) -> datetime:
    return START + timedelta(hours=hour)


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


def paths(directory: Path, extension: str = "ndjson") -> tuple[Path, Path]:
    primary = directory / f"{KEY.data_type}-{KEY.digest()}.{extension}"
    return primary, primary.with_name(primary.name + ".bak")


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def stored_load_kw(path: Path) -> list[float | None]:
    return [record["load_kw"] for record in records(path)]


def load_household(store: ProviderDataStore) -> HouseholdLoadData:
    loaded = store.load(KEY, ADAPTER)
    assert loaded is not None
    return loaded


def test_store_initializes_and_returns_normalized_data(
    store: ProviderDataStore, tmp_path: Path
) -> None:
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


def test_store_update_keeps_previous_valid_data_as_backup(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, normalized_data([1.2, 1.0]))

    store.save(KEY, ADAPTER, normalized_data([2.4, 2.0]))

    primary, backup = paths(tmp_path)
    assert load_household(store).load_kw == (2.4, 2.0)
    assert stored_load_kw(backup) == [1.2, 1.0]
    assert stored_load_kw(primary) == [1.2, 1.0, 2.4, 2.0]


def test_store_recovers_primary_after_restart(tmp_path: Path) -> None:
    ProviderDataStore(tmp_path).save(KEY, ADAPTER, normalized_data())

    current = load_household(ProviderDataStore(tmp_path))

    assert current.load_kw == (1.2, 1.0)


def test_store_logs_ndjson_load_timing_at_debug_without_payload(
    store: ProviderDataStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))

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
    assert DURATION_SECONDS.search(messages)
    assert "load_kw" not in messages
    assert "1.0" not in messages


def test_routine_household_load_reads_stay_out_of_info_output(
    store: ProviderDataStore, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="energy_optimizer"):
        assert store.load(KEY, ADAPTER) is None
        store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
        store.load(KEY, ADAPTER)
        store.load_household_load_range(KEY, START, at(2))

    events = [
        record.getMessage().split(" ", 1)[0]
        for record in caplog.records
        if record.levelno >= logging.INFO
    ]
    assert events == ["event=persistence_succeeded"]


def test_store_logs_invalid_primary_and_backup_recovery_with_counts(
    store: ProviderDataStore, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
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
    assert DURATION_SECONDS.search(recovery)


def test_store_logs_legacy_migration_with_count_and_duration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = ADAPTER.dump_json(household_load_data(START, [2.0, 3.0]))
    for path in paths(tmp_path, "json"):
        path.write_bytes(legacy + b"\n")

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
    assert DURATION_SECONDS.search(migration[1])
    assert "load_kw" not in "\n".join(record.getMessage() for record in caplog.records)


def test_store_recovers_invalid_primary_from_backup(store: ProviderDataStore) -> None:
    store.save(KEY, ADAPTER, normalized_data([1.2, 1.0]))
    store.save(KEY, ADAPTER, normalized_data([2.4, 2.0]))
    primary, backup = paths(store.directory)
    primary.write_text("invalid", encoding="utf-8")

    recovered = load_household(store)

    assert recovered.load_kw == (1.2, 1.0)
    assert stored_load_kw(primary) == [1.2, 1.0]
    assert stored_load_kw(backup) == [1.2, 1.0]


def test_store_rejects_unrecoverable_invalid_state(store: ProviderDataStore) -> None:
    primary, backup = paths(store.directory)
    primary.write_text("invalid", encoding="utf-8")
    backup.write_text("also-invalid", encoding="utf-8")

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        store.load(KEY, ADAPTER)


def test_invalid_write_does_not_replace_existing_data(store: ProviderDataStore) -> None:
    store.save(KEY, ADAPTER, normalized_data([1.2, 1.0]))

    with pytest.raises(ProviderDataStoreError, match="failed validation"):
        store.save(KEY, ADAPTER, {**normalized_data(), "unit": "W"})

    assert load_household(store).load_kw == (1.2, 1.0)


def test_missing_data_returns_none(store: ProviderDataStore) -> None:
    assert store.load(KEY, ADAPTER) is None


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
        "timestamp": at(hour).isoformat(),
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
    for path in paths(directory):
        path.write_bytes(payload)


def test_store_merges_hourly_history_and_incoming_values_win(
    store: ProviderDataStore,
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0, 3.0]))

    merged = store.save(
        KEY, ADAPTER, household_load_data(at(2), [30.0, 4.0], retrieved_at=at(4))
    )

    assert merged.start_time == START
    assert merged.load_kw == (1.0, 2.0, 30.0, 4.0)
    assert merged.retrieved_at == at(4)
    assert store.load(KEY, ADAPTER) == merged


def test_store_round_trips_excluded_hours_after_restart(tmp_path: Path) -> None:
    excluded = exclusion(at(1))
    data = household_load_data(START, [1.0, None, 3.0], exclusions=(excluded,))

    saved = ProviderDataStore(tmp_path).save(KEY, ADAPTER, data)
    restarted = load_household(ProviderDataStore(tmp_path))

    assert saved == data
    assert restarted == data
    assert restarted.load_kw == (1.0, None, 3.0)
    assert restarted.exclusions == (excluded,)
    assert restarted.quality == ()
    primary, backup = paths(tmp_path)
    for path in (primary, backup):
        stored = records(path)
        assert [record["load_kw"] for record in stored] == [1.0, None, 3.0]
        assert all("quality" not in record for record in stored)
        assert [("exclusion" in record) for record in stored] == [False, True, False]
    stored_exclusion = records(primary)[1]["exclusion"]
    assert stored_exclusion["hour_start"] == "2026-01-01T01:00:00Z"
    assert stored_exclusion["causes"][0]["reason"] == "counter_decrease"
    assert stored_exclusion["causes"][0]["entity_id"] == "sensor.household_energy"
    assert stored_exclusion["causes"][0]["data_points"][0]["state"] == "2"
    assert stored_exclusion["causes"][0]["data_points"][0]["step_kwh"] == -1.0


def test_store_appends_excluded_hours_and_keeps_earlier_ones_after_restart(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    first = exclusion(START)
    third = exclusion(at(2), "unavailable")
    data = household_load_data(START, [None, 2.0], exclusions=(first,))
    store.save(KEY, ADAPTER, data)

    saved = store.save(
        KEY, ADAPTER, household_load_data(at(2), [None, 4.0], exclusions=(third,))
    )

    restarted = load_household(ProviderDataStore(tmp_path))
    assert saved.load_kw == (None, 2.0, None, 4.0)
    assert restarted == saved
    assert restarted.exclusions == (first, third)


def test_store_replaces_an_excluded_hour_with_a_valid_incoming_hour(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    data = household_load_data(START, [1.0, None], exclusions=(exclusion(at(1)),))
    store.save(KEY, ADAPTER, data)

    store.save(KEY, ADAPTER, household_load_data(at(1), [2.5]))

    restarted = load_household(ProviderDataStore(tmp_path))
    assert restarted.load_kw == (1.0, 2.5)
    assert restarted.exclusions == ()


def test_store_replaces_a_valid_hour_with_an_incoming_exclusion(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    excluded = exclusion(at(1), "unavailable")

    store.save(KEY, ADAPTER, household_load_data(at(1), [None], exclusions=(excluded,)))

    restarted = load_household(ProviderDataStore(tmp_path))
    assert restarted.load_kw == (1.0, None)
    assert restarted.exclusions == (excluded,)


def test_merge_prefers_incoming_hours_completely_including_their_exclusion() -> None:
    existing = household_load_data(
        START, [1.0, None, 3.0], exclusions=(exclusion(at(1), "counter_decrease"),)
    )
    unavailable = (exclusion(at(1), "unavailable"), exclusion(at(2), "unavailable"))
    incoming = household_load_data(at(1), [None, None], exclusions=unavailable)

    merged = storage_module.merge_household_load_history(existing, incoming)

    assert merged.load_kw == (1.0, None, None)
    assert merged.exclusions == unavailable


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
    store: ProviderDataStore, tmp_path: Path, hour_of_exclusion: int | None
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    backup.unlink()
    first, second = records(primary)
    if hour_of_exclusion is None:
        # The record has no value but nothing explains its absence.
        second["load_kw"] = None
    else:
        # Hour 0's exclusion sits on hour 1's record (another hour); hour 1's
        # exclusion sits on a record that still has its value.
        second["exclusion"] = TypeAdapter(HourExclusion).dump_python(
            exclusion(at(hour_of_exclusion)), mode="json"
        )
        second["load_kw"] = None if hour_of_exclusion == 0 else 2.0
    primary.write_bytes(
        b"".join(json.dumps(record).encode() + b"\n" for record in (first, second))
    )

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        ProviderDataStore(tmp_path).load(KEY, ADAPTER)


def test_store_rejects_household_hours_without_a_value_that_lack_an_exclusion(
    store: ProviderDataStore,
) -> None:
    with pytest.raises(ProviderDataStoreError, match="match their exclusions"):
        store.save(KEY, ADAPTER, household_load_data(START, [1.0, None]))


def test_store_converts_suspect_records_of_legacy_ndjson_into_excluded_hours(
    tmp_path: Path,
) -> None:
    write_legacy_household_ndjson(tmp_path)

    loaded = load_household(ProviderDataStore(tmp_path))

    assert loaded.load_kw == (1.0, None, 3.0)
    assert loaded.quality == ()
    assert [item.hour_start for item in loaded.exclusions] == [at(1)]
    (cause,) = loaded.exclusions[0].causes
    assert cause.reason == "flagged_by_earlier_version"
    assert cause.entity_id == "sensor.household_load"
    assert cause.data_points == ()
    assert "reset_recovery" in cause.message


def test_store_appends_new_records_without_quality_to_legacy_ndjson(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    write_legacy_household_ndjson(tmp_path)
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()

    saved = store.save(KEY, ADAPTER, household_load_data(at(3), [4.0]))

    assert saved.load_kw == (1.0, None, 3.0, 4.0)
    assert saved.exclusions[0].causes[0].reason == "flagged_by_earlier_version"
    appended = primary.read_bytes()[len(before) :]
    assert b"quality" not in appended
    assert json.loads(appended)["load_kw"] == 4.0
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == saved


def test_store_compaction_rewrites_legacy_ndjson_in_the_current_format(
    store: ProviderDataStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 3)
    write_legacy_household_ndjson(tmp_path)

    saved = store.save(KEY, ADAPTER, household_load_data(at(3), [4.0]))

    primary, backup = paths(tmp_path)
    for path in (primary, backup):
        assert b"quality" not in path.read_bytes()
    stored = records(primary)
    assert [record["load_kw"] for record in stored] == [1.0, None, 3.0, 4.0]
    assert stored[1]["exclusion"]["causes"][0]["reason"] == (
        "flagged_by_earlier_version"
    )
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == saved


def test_a_valid_incoming_hour_supersedes_a_suspect_record_of_legacy_ndjson(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    write_legacy_household_ndjson(tmp_path)

    store.save(KEY, ADAPTER, household_load_data(at(1), [2.5]))

    restarted = load_household(ProviderDataStore(tmp_path))
    assert restarted.load_kw == (1.0, 2.5, 3.0)
    assert restarted.exclusions == ()


def test_store_converts_suspect_hours_when_it_migrates_monolithic_json(
    tmp_path: Path,
) -> None:
    legacy = json.dumps(
        {
            **normalized_data([1.0, 2.0, 3.0]),
            "source": {"provider": "home-assistant", "entity_id": "household_load"},
            "latest_observation_at": "2026-01-01T03:00:00+00:00",
            "quality": [VALID_QUALITY, SUSPECT_QUALITY, VALID_QUALITY],
        }
    ).encode()
    for path in paths(tmp_path, "json"):
        path.write_bytes(legacy)

    loaded = load_household(ProviderDataStore(tmp_path))

    assert loaded.load_kw == (1.0, None, 3.0)
    assert loaded.quality == ()
    assert loaded.exclusions[0].causes[0].reason == "flagged_by_earlier_version"
    ndjson_primary, ndjson_backup = paths(tmp_path)
    for path in (ndjson_primary, ndjson_backup):
        assert b"quality" not in path.read_bytes()
    stored = records(ndjson_primary)
    assert [record["load_kw"] for record in stored] == [1.0, None, 3.0]
    assert "exclusion" in stored[1]
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == loaded


def test_store_loads_only_points_in_a_half_open_range(store: ProviderDataStore) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0, 3.0, 4.0]))

    selected = store.load_household_load_range(KEY, at(1), at(3))

    assert selected is not None
    assert selected.start_time == at(1)
    assert selected.load_kw == (2.0, 3.0)


def test_store_slices_exclusions_from_retained_start_when_range_predates_history(
    store: ProviderDataStore,
) -> None:
    excluded = exclusion(at(3))
    data = household_load_data(at(2), [1.0, None], exclusions=(excluded,))
    store.save(KEY, ADAPTER, data)

    selected = store.load_household_load_range(KEY, at(2) - timedelta(days=1), at(4))

    assert selected is not None
    assert selected.start_time == at(2)
    assert selected.load_kw == (1.0, None)
    assert selected.exclusions == (excluded,)


def test_store_slices_only_the_exclusions_of_the_requested_hours(
    store: ProviderDataStore,
) -> None:
    excluded = exclusion(at(1))
    data = household_load_data(START, [1.0, None, 3.0, 4.0], exclusions=(excluded,))
    store.save(KEY, ADAPTER, data)

    inside = store.load_household_load_range(KEY, at(1), at(3))
    outside = store.load_household_load_range(KEY, at(2), at(4))

    assert inside is not None
    assert inside.load_kw == (None, 3.0)
    assert inside.exclusions == (excluded,)
    assert outside is not None
    assert outside.load_kw == (3.0, 4.0)
    assert outside.exclusions == ()


def test_store_returns_none_for_a_range_without_points(
    store: ProviderDataStore,
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))

    assert store.load_household_load_range(KEY, at(3), at(4)) is None


def test_store_appends_new_observations_without_rewriting_existing_lines(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()

    store.save(KEY, ADAPTER, household_load_data(at(2), [3.0]))

    after = primary.read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == 3


def test_merge_marks_hours_between_history_and_a_later_range_unavailable() -> None:
    existing = household_load_data(START, [1.0, 2.0])
    incoming = household_load_data(at(5), [3.0, 4.0], retrieved_at=at(24))

    merged = storage_module.merge_household_load_history(existing, incoming)

    assert merged.start_time == START
    assert merged.load_kw == (1.0, 2.0, None, None, None, 3.0, 4.0)
    assert [item.hour_start for item in merged.exclusions] == [at(2), at(3), at(4)]
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
    merged = storage_module.merge_household_load_history(
        household_load_data(START, [1.0, 2.0]),
        household_load_data(at(incoming_start_hour), [9.0, 8.0]),
    )

    assert merged.load_kw == expected
    assert merged.exclusions == ()


def test_merge_rejects_a_range_that_leaves_a_gap_after_the_incoming_hours() -> None:
    with pytest.raises(ProviderDataStoreError, match="contiguous"):
        storage_module.merge_household_load_history(
            household_load_data(at(5), [1.0]), household_load_data(START, [2.0])
        )


def test_store_persists_a_gap_that_survives_restart_and_continues_incrementally(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()

    saved = store.save(KEY, ADAPTER, household_load_data(at(5), [3.0]))

    assert saved.load_kw == (1.0, 2.0, None, None, None, 3.0)
    # The gap is appended like any other hour, so the file stays contiguous.
    assert primary.read_bytes().startswith(before)
    stored = records(primary)
    assert [record["timestamp"] for record in stored] == [
        at(hour).isoformat() for hour in range(6)
    ]
    assert [record["load_kw"] for record in stored] == [1.0, 2.0, None, None, None, 3.0]
    assert [
        record.get("exclusion", {}).get("causes", [{}])[0].get("reason")
        for record in stored
    ] == [None, None] + ["history_unavailable"] * 3 + [None]
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == saved
    continued = ProviderDataStore(tmp_path).save(
        KEY, ADAPTER, household_load_data(at(6), [4.0])
    )
    assert continued.load_kw == (1.0, 2.0, None, None, None, 3.0, 4.0)
    assert len(continued.exclusions) == 3


def test_store_lets_a_later_submission_of_the_missing_hours_replace_the_gap(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0]))
    store.save(KEY, ADAPTER, household_load_data(at(3), [4.0]))

    filled = store.save(KEY, ADAPTER, household_load_data(at(1), [2.0, 3.0]))

    assert filled.load_kw == (1.0, 2.0, 3.0, 4.0)
    assert filled.exclusions == ()
    assert ProviderDataStore(tmp_path).load(KEY, ADAPTER) == filled


def test_store_lists_gap_hours_in_a_range_query_like_any_excluded_hour(
    store: ProviderDataStore,
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0]))
    store.save(KEY, ADAPTER, household_load_data(at(3), [2.0]))

    ranged = store.load_household_load_range(KEY, at(1), at(3))

    assert ranged is not None
    assert ranged.load_kw == (None, None)
    assert [cause.reason for item in ranged.exclusions for cause in item.causes] == [
        "history_unavailable"
    ] * 2


def test_store_rejects_a_gap_range_from_another_source_without_changing_history(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0]))
    primary, _ = paths(tmp_path)
    before = primary.read_bytes()
    other = replace(
        household_load_data(at(4), [2.0]),
        source=SourceMetadata(provider="home-assistant", entity_id="other"),
    )

    with pytest.raises(ProviderDataStoreError, match="source identity"):
        store.save(KEY, ADAPTER, other)

    assert primary.read_bytes() == before


def test_store_ignores_only_an_interrupted_final_append(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    with primary.open("ab") as history_file:
        history_file.write(b'{"timestamp":"2026-01-01T02:00:00+00:00"')

    assert load_household(store).load_kw == (1.0, 2.0)


@pytest.mark.parametrize(
    "appended",
    [b"not-json", b"not-json\n"],
    ids=["final-record-without-newline", "nonfinal-record"],
)
def test_store_rejects_a_malformed_appended_ndjson_record(
    store: ProviderDataStore, tmp_path: Path, appended: bytes
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    backup.unlink()
    primary.write_bytes(primary.read_bytes() + appended)

    with pytest.raises(ProviderDataStoreError, match="invalid and cannot be recovered"):
        store.load(KEY, ADAPTER)


def test_store_rejects_mixed_household_load_sources(store: ProviderDataStore) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, backup = paths(store.directory)
    mixed = household_load_data(at(2), [3.0])
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


def test_store_rejects_source_change_before_appending(store: ProviderDataStore) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    incoming = replace(
        household_load_data(at(2), [3.0], retrieved_at=START),
        source=SourceMetadata(provider="other-provider", entity_id="household_load"),
    )

    with pytest.raises(ProviderDataStoreError, match="source identity"):
        store.save(KEY, ADAPTER, incoming)

    primary, _ = paths(store.directory)
    assert len(primary.read_text().splitlines()) == 2


def test_store_retains_only_the_newest_ten_years_of_household_load(
    store: ProviderDataStore,
) -> None:
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    store.save(KEY, ADAPTER, household_load_data(start, [1.0] * 87_672))

    merged = store.save(
        KEY,
        ADAPTER,
        household_load_data(start + timedelta(hours=87_671), [2.0, 3.0]),
    )

    assert len(merged.load_kw) == 87_672
    assert merged.start_time == start + timedelta(hours=1)
    assert merged.load_kw[-2:] == (2.0, 3.0)


def test_store_bounds_initial_oversized_household_load_save(
    store: ProviderDataStore,
) -> None:
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)

    saved = store.save(KEY, ADAPTER, household_load_data(start, [1.0] * (87_672 + 2)))

    assert len(saved.load_kw) == 87_672
    assert saved.start_time == start + timedelta(hours=2)


def test_store_compacts_superseded_records_atomically(
    store: ProviderDataStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 4)
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))

    store.save(KEY, ADAPTER, household_load_data(at(2), [3.0, 4.0, 5.0]))

    primary, backup = paths(tmp_path)
    assert len(primary.read_text().splitlines()) == 5
    assert len(backup.read_text().splitlines()) == 2
    assert load_household(store).load_kw == (1.0, 2.0, 3.0, 4.0, 5.0)


def test_store_compaction_removes_superseded_corrections(
    store: ProviderDataStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 2)
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    store.save(KEY, ADAPTER, household_load_data(at(1), [20.0]))

    primary, _ = paths(tmp_path)

    assert len(records(primary)) == 2
    assert stored_load_kw(primary) == [1.0, 20.0]


def test_store_migrates_legacy_json_primary_and_backup(tmp_path: Path) -> None:
    current = household_load_data(START, [2.0, 3.0])
    previous = household_load_data(START, [1.0, 1.5])
    primary, backup = paths(tmp_path, "json")
    primary.write_bytes(ADAPTER.dump_json(current) + b"\n")
    backup.write_bytes(ADAPTER.dump_json(previous) + b"\n")

    loaded = ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    assert loaded == current
    for path in paths(tmp_path):
        assert path.exists()
    assert not primary.exists()
    assert not backup.exists()


def test_store_migrates_valid_legacy_backup_when_primary_is_invalid(
    tmp_path: Path,
) -> None:
    previous = household_load_data(START, [1.0, 1.5])
    primary, backup = paths(tmp_path, "json")
    primary.write_text("invalid", encoding="utf-8")
    backup.write_bytes(ADAPTER.dump_json(previous) + b"\n")

    loaded = ProviderDataStore(tmp_path).load(KEY, ADAPTER)

    assert loaded == previous
    for path in paths(tmp_path):
        assert path.exists()


def test_store_recovers_invalid_ndjson_primary_from_backup(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, _ = paths(tmp_path)
    primary.write_text("invalid\n", encoding="utf-8")

    recovered = load_household(store)

    assert recovered.load_kw == (1.0, 2.0)
    assert len(primary.read_text().splitlines()) == 2


def test_store_rebuilds_backup_when_invalid_before_primary_corruption(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    backup.write_text("invalid\n", encoding="utf-8")

    store.save(KEY, ADAPTER, household_load_data(at(2), [3.0]))
    primary.write_text("invalid\n", encoding="utf-8")

    assert load_household(store).load_kw == (1.0, 2.0)


@pytest.mark.parametrize("failure_target", ["backup", "primary"])
def test_store_compaction_failure_keeps_recoverable_files(
    store: ProviderDataStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
) -> None:
    monkeypatch.setattr(storage_module, "HOUSEHOLD_LOAD_COMPACTION_THRESHOLD", 2)
    store.save(KEY, ADAPTER, household_load_data(START, [1.0, 2.0]))
    primary, backup = paths(tmp_path)
    original_atomic_write = store._atomic_write

    def fail_compaction_write(path: Path, payload: bytes) -> None:
        if path == (backup if failure_target == "backup" else primary):
            raise ProviderDataStoreError("simulated compaction failure")
        original_atomic_write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", fail_compaction_write)
    with pytest.raises(ProviderDataStoreError, match="simulated compaction failure"):
        store.save(KEY, ADAPTER, household_load_data(at(1), [20.0]))

    assert all(records(primary))
    assert all(records(backup))
    primary.write_text("invalid\n", encoding="utf-8")
    monkeypatch.undo()
    assert load_household(store).load_kw == (1.0, 2.0)


def test_non_household_provider_data_remains_json(store: ProviderDataStore) -> None:
    key = ProviderDataKey("electricity-prices", "test-provider")
    adapter = TypeAdapter(dict[str, int])

    store.save(key, adapter, {"value": 1})

    assert (store.directory / f"electricity-prices-{key.digest()}.json").exists()
    assert not (store.directory / f"electricity-prices-{key.digest()}.ndjson").exists()


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
    data = efficiency_history(START, 4, excluded=(2,))

    saved = ProviderDataStore(tmp_path).save(EFFICIENCY_KEY, EFFICIENCY_ADAPTER, data)
    restarted = ProviderDataStore(tmp_path).load(EFFICIENCY_KEY, EFFICIENCY_ADAPTER)

    assert saved == data
    assert restarted == data
    assert restarted is not None
    for name in ENERGY_LEGS:
        assert getattr(restarted, name) == (0.5, 0.5, None, 0.5), name
    assert restarted.state_of_charge_percent == (20.0, None, None, 50.0, 60.0)
    assert restarted.exclusions == (
        exclusion(at(2), "counter_decrease", "sensor.battery_in"),
    )
    payload = json.loads(efficiency_path(tmp_path).read_text())
    assert payload["battery_energy_in_kwh"] == [0.5, 0.5, None, 0.5]
    assert payload["state_of_charge_percent"] == [20.0, None, None, 50.0, 60.0]
    assert payload["quality"] == []
    assert payload["exclusions"][0]["hour_start"] == "2026-01-01T02:00:00Z"
    assert payload["exclusions"][0]["causes"][0]["reason"] == "counter_decrease"


def write_legacy_efficiency_history(directory: Path) -> None:
    """Persist a history in which an earlier version flagged hour 2 as suspect."""
    suspect = {
        "status": "suspect",
        "reason": "counter_reset",
        "entity_id": "sensor.battery_in",
    }
    payload: dict[str, object] = {
        "schema_version": "1",
        "start_time": START.isoformat(),
        "interval_minutes": 60,
        **{name: [0.5, 0.5, 0.5, 0.5] for name in ENERGY_LEGS},
        "state_of_charge_percent": [20.0, 30.0, 40.0, 50.0, 60.0],
        "unit": "kWh",
        "source": {
            "provider": "home-assistant",
            "entity_id": "battery_efficiency_history",
        },
        "retrieved_at": START.isoformat(),
        "latest_observation_at": at(4).isoformat(),
        "quality": [VALID_QUALITY, VALID_QUALITY, suspect, VALID_QUALITY],
    }
    efficiency_path(directory).write_text(json.dumps(payload), encoding="utf-8")


def test_store_converts_suspect_hours_of_legacy_efficiency_history(
    tmp_path: Path,
) -> None:
    write_legacy_efficiency_history(tmp_path)

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
    assert [item.hour_start for item in loaded.exclusions] == [at(2)]
    (cause,) = loaded.exclusions[0].causes
    assert cause.reason == "flagged_by_earlier_version"
    assert cause.entity_id == "sensor.battery_in"
    assert cause.data_points == ()
    assert "counter_reset" in cause.message


def test_store_persists_converted_efficiency_history_without_quality(
    store: ProviderDataStore, tmp_path: Path
) -> None:
    write_legacy_efficiency_history(tmp_path)
    existing = store.load(EFFICIENCY_KEY, EFFICIENCY_ADAPTER)
    incoming = efficiency_history(at(4), 1)

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
