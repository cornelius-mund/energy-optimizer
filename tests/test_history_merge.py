"""Tests for retained hourly grid-flow and electricity-price history."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from energy_optimizer import history_merge
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
    HourExclusion,
)
from energy_optimizer.history_merge import (
    grid_flow_points,
    merge_grid_flow_history,
    merge_price_history,
)
from energy_optimizer.providers.interfaces import (
    ElectricityPriceData,
    GridFlowData,
    SourceMetadata,
)
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
GRID_SOURCE = SourceMetadata(provider="home-assistant", entity_id="grid_flow")
PRICE_SOURCE = SourceMetadata(provider="awattar.de", entity_id="de")
GRID_KEY = ProviderDataKey("grid-flow", "home-assistant", "grid_flow")
GRID_ADAPTER = TypeAdapter(GridFlowData)


def exclusion(
    hour: int,
    reason: ExclusionReason = "counter_decrease",
    entity_id: str = "sensor.grid_import",
) -> HourExclusion:
    hour_start = START + timedelta(hours=hour)
    return HourExclusion(
        hour_start,
        (
            ExclusionCause.of(
                reason,
                f"{entity_id} is excluded in hour {hour}",
                entity_id,
                [ExcludedDataPoint(hour_start, state="2", unit="kWh")],
            ),
        ),
    )


def grid_flow(
    start_hour: int,
    imports: tuple[float | None, ...],
    *,
    exports: tuple[float | None, ...] | None = None,
    observed_hour: int | None = None,
    exclusions: tuple[HourExclusion, ...] = (),
    source: SourceMetadata = GRID_SOURCE,
) -> GridFlowData:
    observed = START + timedelta(hours=observed_hour or start_hour + len(imports))
    return GridFlowData(
        schema_version="1",
        start_time=START + timedelta(hours=start_hour),
        interval_minutes=60,
        import_kw=imports,
        export_kw=(
            exports
            if exports is not None
            else tuple(None if v is None else v / 2 for v in imports)
        ),
        unit="kW",
        source=source,
        retrieved_at=observed,
        latest_observation_at=observed,
        exclusions=exclusions,
    )


def legacy_grid_flow_payload(
    import_kw: list[float], export_kw: list[float], quality: list[dict[str, object]]
) -> bytes:
    """Return a persisted grid-flow file as versions before exclusion wrote it."""
    return json.dumps(
        {
            "schema_version": "1",
            "start_time": START.isoformat(),
            "interval_minutes": 60,
            "import_kw": import_kw,
            "export_kw": export_kw,
            "unit": "kW",
            "source": {"provider": "home-assistant", "entity_id": "grid_flow"},
            "retrieved_at": START.isoformat(),
            "latest_observation_at": (START + timedelta(hours=3)).isoformat(),
            "quality": quality,
        },
        indent=2,
    ).encode()


def prices(
    start_hour: int,
    imports: tuple[float, ...],
    *,
    retrieved_hour: int = 0,
    source: SourceMetadata = PRICE_SOURCE,
) -> ElectricityPriceData:
    return ElectricityPriceData(
        schema_version="1",
        timestamps=tuple(
            START + timedelta(hours=start_hour + index) for index in range(len(imports))
        ),
        interval_minutes=60,
        import_price_eur_per_kwh=imports,
        export_price_eur_per_kwh=tuple(value - 0.1 for value in imports),
        unit="EUR/kWh",
        source=source,
        retrieved_at=START + timedelta(hours=retrieved_hour),
        expires_at=START + timedelta(hours=retrieved_hour + 48),
    )


def test_grid_flow_merge_extends_history_and_keeps_both_channels() -> None:
    merged = merge_grid_flow_history(
        grid_flow(0, (1.0, 2.0)), grid_flow(2, (3.0,), exports=(0.7,))
    )

    assert merged.start_time == START
    assert merged.import_kw == (1.0, 2.0, 3.0)
    assert merged.export_kw == (0.5, 1.0, 0.7)
    assert merged.latest_observation_at == START + timedelta(hours=3)


def test_grid_flow_merge_excludes_a_gap_before_the_fetched_range_as_unavailable() -> (
    None
):
    existing = grid_flow(0, (1.0, 2.0))
    incoming = grid_flow(5, (3.0, 4.0))

    merged = merge_grid_flow_history(existing, incoming)

    assert merged.start_time == START
    assert merged.import_kw == (1.0, 2.0, None, None, None, 3.0, 4.0)
    assert merged.export_kw == (0.5, 1.0, None, None, None, 1.5, 2.0)
    assert [item.hour_start for item in merged.exclusions] == [
        START + timedelta(hours=hour) for hour in (2, 3, 4)
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
    assert merged.latest_observation_at == incoming.latest_observation_at
    assert grid_flow_points(merged)[START] == (1.0, 0.5, None)
    assert grid_flow_points(merged)[START + timedelta(hours=6)] == (4.0, 2.0, None)


def test_grid_flow_merge_excludes_a_single_missing_hour() -> None:
    merged = merge_grid_flow_history(grid_flow(0, (1.0,)), grid_flow(2, (2.0,)))

    assert merged.import_kw == (1.0, None, 2.0)
    assert [item.hour_start for item in merged.exclusions] == [
        START + timedelta(hours=1)
    ]
    assert merged.exclusions[0].causes[0].message == (
        "The provider holds no history from 2026-01-01T01:00:00+00:00 until "
        "2026-01-01T02:00:00+00:00 (1 hour), so this hour cannot be imported."
    )


def test_grid_flow_merge_keeps_earlier_exclusions_next_to_a_gap() -> None:
    existing = grid_flow(0, (1.0, None), exclusions=(exclusion(1),))

    merged = merge_grid_flow_history(existing, grid_flow(3, (3.0,)))

    assert merged.import_kw == (1.0, None, None, 3.0)
    assert [
        (item.hour_start, [cause.reason for cause in item.causes])
        for item in merged.exclusions
    ] == [
        (START + timedelta(hours=1), ["counter_decrease"]),
        (START + timedelta(hours=2), ["history_unavailable"]),
    ]


def test_grid_flow_merge_leaves_a_range_that_follows_directly_without_a_gap() -> None:
    merged = merge_grid_flow_history(grid_flow(0, (1.0, 2.0)), grid_flow(2, (3.0,)))

    assert merged.import_kw == (1.0, 2.0, 3.0)
    assert merged.exclusions == ()


def test_grid_flow_merge_lets_a_later_submission_fill_the_gap() -> None:
    gapped = merge_grid_flow_history(grid_flow(0, (1.0,)), grid_flow(3, (4.0,)))

    filled = merge_grid_flow_history(gapped, grid_flow(1, (2.0, 3.0)))

    assert filled.import_kw == (1.0, 2.0, 3.0, 4.0)
    assert filled.exclusions == ()


def test_grid_flow_merge_bounds_a_gap_with_the_retention_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_merge, "HISTORY_RETENTION_HOURS", 4)

    merged = merge_grid_flow_history(grid_flow(0, (1.0, 2.0)), grid_flow(5, (3.0,)))

    assert merged.start_time == START + timedelta(hours=2)
    assert merged.import_kw == (None, None, None, 3.0)
    assert len(merged.exclusions) == 3


def test_store_persists_a_grid_flow_gap_and_continues_incrementally(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(GRID_KEY, GRID_ADAPTER, grid_flow(0, (1.0, 2.0)))

    saved = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(6, (3.0,)))
    restarted = ProviderDataStore(tmp_path).load(GRID_KEY, GRID_ADAPTER)
    continued = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(7, (4.0,)))

    assert saved.import_kw == (1.0, 2.0, None, None, None, None, 3.0)
    assert restarted == saved
    assert continued.start_time == START
    assert continued.import_kw == (1.0, 2.0, None, None, None, None, 3.0, 4.0)
    assert len(continued.exclusions) == 4


def test_grid_flow_merge_prefers_incoming_values_for_overlapping_hours() -> None:
    merged = merge_grid_flow_history(
        grid_flow(0, (1.0, 2.0, 3.0)), grid_flow(1, (9.0, 8.0), exports=(4.0, 5.0))
    )

    assert merged.import_kw == (1.0, 9.0, 8.0)
    assert merged.export_kw == (0.5, 4.0, 5.0)


def test_grid_flow_merge_never_moves_observation_metadata_backwards() -> None:
    newest = grid_flow(0, (1.0, 2.0, 3.0))
    correction = grid_flow(0, (1.5,), observed_hour=1)

    merged = merge_grid_flow_history(newest, correction)

    assert merged.import_kw == (1.5, 2.0, 3.0)
    assert merged.latest_observation_at == newest.latest_observation_at
    assert merged.retrieved_at == newest.retrieved_at


def test_grid_flow_merge_bounds_history_to_the_retention_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_merge, "HISTORY_RETENTION_HOURS", 3)

    merged = merge_grid_flow_history(
        grid_flow(0, (1.0, 2.0, 3.0)), grid_flow(3, (4.0,))
    )

    assert merged.start_time == START + timedelta(hours=1)
    assert merged.import_kw == (2.0, 3.0, 4.0)


def test_grid_flow_merge_keeps_an_excluded_hour_in_place_without_values() -> None:
    existing = grid_flow(0, (1.0, None), exclusions=(exclusion(1),))

    merged = merge_grid_flow_history(existing, grid_flow(2, (3.0,)))

    assert merged.import_kw == (1.0, None, 3.0)
    assert merged.export_kw == (0.5, None, 1.5)
    assert merged.exclusions == (exclusion(1),)
    assert merged.quality == ()
    assert merge_grid_flow_history(None, grid_flow(0, (1.0,))).exclusions == ()


def test_grid_flow_merge_lets_a_valid_incoming_hour_replace_an_exclusion() -> None:
    existing = grid_flow(0, (1.0, None, 3.0), exclusions=(exclusion(1),))

    merged = merge_grid_flow_history(existing, grid_flow(1, (9.0,), exports=(4.0,)))

    assert merged.import_kw == (1.0, 9.0, 3.0)
    assert merged.export_kw == (0.5, 4.0, 1.5)
    assert merged.exclusions == ()


def test_grid_flow_merge_lets_an_incoming_exclusion_replace_a_valid_hour() -> None:
    existing = grid_flow(0, (1.0, 2.0, 3.0))
    incoming = grid_flow(1, (None,), exclusions=(exclusion(1, "unavailable"),))

    merged = merge_grid_flow_history(existing, incoming)

    assert merged.import_kw == (1.0, None, 3.0)
    assert merged.export_kw == (0.5, None, 1.5)
    assert merged.exclusions == (exclusion(1, "unavailable"),)


def test_grid_flow_merge_replaces_the_causes_of_an_excluded_hour() -> None:
    existing = grid_flow(0, (None, 2.0), exclusions=(exclusion(0, "counter_decrease"),))
    incoming = grid_flow(0, (None,), exclusions=(exclusion(0, "unavailable"),))

    merged = merge_grid_flow_history(existing, incoming)

    assert merged.import_kw == (None, 2.0)
    assert merged.exclusions == (exclusion(0, "unavailable"),)


def test_grid_flow_merge_bounds_exclusions_with_the_retention_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_merge, "HISTORY_RETENTION_HOURS", 2)
    existing = grid_flow(0, (None, None), exclusions=(exclusion(0), exclusion(1)))

    merged = merge_grid_flow_history(existing, grid_flow(2, (3.0,)))

    assert merged.start_time == START + timedelta(hours=1)
    assert merged.import_kw == (None, 3.0)
    assert merged.exclusions == (exclusion(1),)


def test_grid_flow_points_index_excluded_hours_with_their_exclusion() -> None:
    data = grid_flow(0, (1.0, None), exclusions=(exclusion(1),))

    assert grid_flow_points(data) == {
        START: (1.0, 0.5, None),
        START + timedelta(hours=1): (None, None, exclusion(1)),
    }


@pytest.mark.parametrize(
    ("existing", "incoming", "message"),
    [
        (grid_flow(3, (1.0,)), grid_flow(0, (2.0,)), "contiguous"),
        (
            grid_flow(0, (1.0,)),
            grid_flow(1, (2.0,), source=SourceMetadata("other", "grid_flow")),
            "source identity",
        ),
        (None, grid_flow(0, (1.0, 2.0), exports=(1.0,)), "same non-zero number"),
        (None, grid_flow(0, (-1.0,), exports=(0.0,)), "non-negative"),
        (None, grid_flow(0, (float("nan"),), exports=(0.0,)), "finite"),
        (
            None,
            GridFlowData(
                "1",
                START + timedelta(minutes=30),
                60,
                (1.0,),
                (0.0,),
                "kW",
                GRID_SOURCE,
                START,
                START,
            ),
            "aligned to the hour",
        ),
        (
            None,
            grid_flow(0, (None,), exports=(0.5,), exclusions=(exclusion(0),)),
            "excluded together",
        ),
        (
            None,
            grid_flow(0, (0.5,), exports=(None,), exclusions=(exclusion(0),)),
            "excluded together",
        ),
        (
            None,
            grid_flow(0, (None,), exports=(None,)),
            "hours without values must match their exclusions",
        ),
        (
            None,
            grid_flow(0, (1.0,), exclusions=(exclusion(0),)),
            "hours without values must match their exclusions",
        ),
        (
            None,
            grid_flow(0, (None,), exclusions=(exclusion(0), exclusion(0))),
            "two exclusions for one hour",
        ),
        (
            None,
            replace(grid_flow(0, (1.0,)), interval_minutes=cast(Any, 30)),
            "hourly kW values",
        ),
    ],
)
def test_grid_flow_merge_rejects_data_that_would_corrupt_history(
    existing: GridFlowData | None, incoming: GridFlowData, message: str
) -> None:
    with pytest.raises(ProviderDataStoreError, match=message):
        merge_grid_flow_history(existing, incoming)


def test_price_merge_lets_the_newest_retrieval_win_and_keeps_gaps() -> None:
    older = prices(0, (0.30, 0.31), retrieved_hour=0)
    newer = prices(3, (0.40, 0.41), retrieved_hour=3)
    revised = prices(1, (0.99,), retrieved_hour=4)

    merged = merge_price_history(merge_price_history(older, newer), revised)

    assert merged.timestamps == tuple(START + timedelta(hours=h) for h in (0, 1, 3, 4))
    assert merged.import_price_eur_per_kwh == (0.30, 0.99, 0.40, 0.41)
    assert merged.export_price_eur_per_kwh == pytest.approx((0.20, 0.89, 0.30, 0.31))
    assert merged.retrieved_at == START + timedelta(hours=4)
    assert merged.expires_at == START + timedelta(hours=52)


def test_price_merge_bounds_history_to_the_retention_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_merge, "HISTORY_RETENTION_HOURS", 2)

    merged = merge_price_history(prices(0, (0.1, 0.2)), prices(2, (0.3,)))

    assert merged.import_price_eur_per_kwh == (0.2, 0.3)


@pytest.mark.parametrize(
    ("existing", "incoming", "message"),
    [
        (
            prices(0, (0.1,)),
            prices(1, (0.2,), source=SourceMetadata("other", "de")),
            "source identity",
        ),
        (
            None,
            ElectricityPriceData(
                "1",
                (START,),
                60,
                (0.1,),
                (),
                "EUR/kWh",
                PRICE_SOURCE,
                START,
                START,
            ),
            "aligned",
        ),
        (
            None,
            ElectricityPriceData(
                "1",
                (START + timedelta(minutes=30),),
                60,
                (0.1,),
                (0.1,),
                "EUR/kWh",
                PRICE_SOURCE,
                START,
                START,
            ),
            "aligned to the hour",
        ),
        (
            None,
            ElectricityPriceData(
                "1",
                (START,),
                60,
                (float("inf"),),
                (0.1,),
                "EUR/kWh",
                PRICE_SOURCE,
                START,
                START,
            ),
            "finite",
        ),
        (
            None,
            replace(prices(0, (0.1,)), unit=cast(Any, "EUR/MWh")),
            "hourly EUR/kWh values",
        ),
    ],
)
def test_price_merge_rejects_data_that_would_corrupt_history(
    existing: ElectricityPriceData | None, incoming: ElectricityPriceData, message: str
) -> None:
    with pytest.raises(ProviderDataStoreError, match=message):
        merge_price_history(existing, incoming)


def test_store_merges_grid_flow_saves_into_one_retained_history(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)

    store.save(GRID_KEY, GRID_ADAPTER, grid_flow(0, (1.0, 2.0)))
    saved = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(2, (3.0,)))
    restarted = ProviderDataStore(tmp_path).load(GRID_KEY, GRID_ADAPTER)

    assert saved.import_kw == (1.0, 2.0, 3.0)
    assert restarted == saved


def test_store_keeps_existing_grid_flow_history_when_a_merge_is_rejected(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    original = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(3, (1.0, 2.0)))

    with pytest.raises(ProviderDataStoreError, match="contiguous"):
        store.save(GRID_KEY, GRID_ADAPTER, grid_flow(0, (9.0,)))

    assert store.load(GRID_KEY, GRID_ADAPTER) == original


def test_store_recovers_grid_flow_history_from_its_backup(tmp_path: Path) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(GRID_KEY, GRID_ADAPTER, grid_flow(0, (1.0, 2.0)))
    saved = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(2, (3.0,)))
    primary = tmp_path / f"grid-flow-{GRID_KEY.digest()}.json"
    backup = primary.with_name(primary.name + ".bak")
    backup.write_bytes(primary.read_bytes())
    primary.write_text("{corrupt", encoding="utf-8")

    assert store.load(GRID_KEY, GRID_ADAPTER) == saved
    assert store.save(GRID_KEY, GRID_ADAPTER, grid_flow(3, (4.0,))).import_kw == (
        1.0,
        2.0,
        3.0,
        4.0,
    )


def test_store_refuses_to_merge_into_unrecoverable_grid_flow_history(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(GRID_KEY, GRID_ADAPTER, grid_flow(0, (1.0,)))
    for path in tmp_path.iterdir():
        path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(ProviderDataStoreError, match="cannot be recovered"):
        store.save(GRID_KEY, GRID_ADAPTER, grid_flow(1, (2.0,)))


def test_store_round_trips_excluded_grid_flow_hours_after_restart(
    tmp_path: Path,
) -> None:
    store = ProviderDataStore(tmp_path)
    store.save(
        GRID_KEY, GRID_ADAPTER, grid_flow(0, (1.0, None), exclusions=(exclusion(1),))
    )

    saved = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(2, (3.0,)))
    restarted = ProviderDataStore(tmp_path).load(GRID_KEY, GRID_ADAPTER)

    assert saved.import_kw == (1.0, None, 3.0)
    assert restarted == saved
    assert restarted is not None
    assert restarted.import_kw == (1.0, None, 3.0)
    assert restarted.export_kw == (0.5, None, 1.5)
    assert restarted.exclusions == (exclusion(1),)
    payload = json.loads(grid_primary(tmp_path).read_text())
    assert payload["import_kw"] == [1.0, None, 3.0]
    assert payload["export_kw"] == [0.5, None, 1.5]
    assert payload["exclusions"][0]["hour_start"] == "2026-01-01T01:00:00Z"
    assert payload["exclusions"][0]["causes"][0]["reason"] == "counter_decrease"


def grid_primary(directory: Path) -> Path:
    return directory / f"grid-flow-{GRID_KEY.digest()}.json"


def write_legacy_grid_flow(directory: Path) -> None:
    """Persist a history in which an earlier version flagged hour 1 as suspect."""
    quality: list[dict[str, object]] = [
        {"status": "valid", "reason": None, "entity_id": None},
        {
            "status": "suspect",
            "reason": "counter_reset",
            "entity_id": "sensor.grid_import",
        },
        {"status": "valid", "reason": None, "entity_id": None},
    ]
    directory.mkdir(exist_ok=True)
    payload = legacy_grid_flow_payload([1.0, 2.0, 3.0], [0.5, 1.0, 1.5], quality)
    grid_primary(directory).write_bytes(payload)


def test_store_converts_suspect_hours_of_legacy_grid_flow_history(
    tmp_path: Path,
) -> None:
    write_legacy_grid_flow(tmp_path)

    loaded = ProviderDataStore(tmp_path).load(GRID_KEY, GRID_ADAPTER)

    assert loaded is not None
    assert loaded.import_kw == (1.0, None, 3.0)
    assert loaded.export_kw == (0.5, None, 1.5)
    assert loaded.quality == ()
    assert [item.hour_start for item in loaded.exclusions] == [
        START + timedelta(hours=1)
    ]
    (cause,) = loaded.exclusions[0].causes
    assert cause.reason == "flagged_by_earlier_version"
    assert cause.entity_id == "sensor.grid_import"
    assert cause.data_points == ()
    assert "counter_reset" in cause.message


def test_store_persists_converted_grid_flow_history_without_quality(
    tmp_path: Path,
) -> None:
    write_legacy_grid_flow(tmp_path)
    store = ProviderDataStore(tmp_path)

    saved = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(3, (4.0,)))

    assert saved.import_kw == (1.0, None, 3.0, 4.0)
    assert saved.export_kw == (0.5, None, 1.5, 2.0)
    payload = json.loads(grid_primary(tmp_path).read_text())
    assert payload["quality"] == []
    assert payload["import_kw"] == [1.0, None, 3.0, 4.0]
    assert [item["hour_start"] for item in payload["exclusions"]] == [
        "2026-01-01T01:00:00Z"
    ]
    assert payload["exclusions"][0]["causes"][0]["reason"] == (
        "flagged_by_earlier_version"
    )
    assert ProviderDataStore(tmp_path).load(GRID_KEY, GRID_ADAPTER) == saved


def test_store_converts_a_legacy_grid_flow_backup_when_it_recovers_from_it(
    tmp_path: Path,
) -> None:
    write_legacy_grid_flow(tmp_path)
    store = ProviderDataStore(tmp_path)
    store.save(GRID_KEY, GRID_ADAPTER, grid_flow(3, (4.0,)))
    grid_primary(tmp_path).write_text("{corrupt", encoding="utf-8")

    recovered = store.load(GRID_KEY, GRID_ADAPTER)

    assert recovered is not None
    assert recovered.import_kw == (1.0, None, 3.0)
    assert recovered.exclusions[0].causes[0].reason == "flagged_by_earlier_version"
