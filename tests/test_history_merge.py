"""Tests for retained hourly grid-flow and electricity-price history."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from energy_optimizer import history_merge
from energy_optimizer.history_merge import merge_grid_flow_history, merge_price_history
from energy_optimizer.providers.interfaces import (
    ElectricityPriceData,
    GridFlowData,
    IntervalQuality,
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


def grid_flow(
    start_hour: int,
    imports: tuple[float, ...],
    *,
    exports: tuple[float, ...] | None = None,
    observed_hour: int | None = None,
    quality: tuple[IntervalQuality, ...] = (),
    source: SourceMetadata = GRID_SOURCE,
) -> GridFlowData:
    observed = START + timedelta(hours=observed_hour or start_hour + len(imports))
    return GridFlowData(
        schema_version="1",
        start_time=START + timedelta(hours=start_hour),
        interval_minutes=60,
        import_kw=imports,
        export_kw=exports if exports is not None else tuple(v / 2 for v in imports),
        unit="kW",
        source=source,
        retrieved_at=observed,
        latest_observation_at=observed,
        quality=quality,
    )


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


def test_grid_flow_merge_keeps_suspect_quality_only_for_flagged_hours() -> None:
    suspect = IntervalQuality(
        status="suspect", reason="counter_reset", entity_id="sensor.grid_import"
    )
    existing = grid_flow(0, (1.0, 2.0), quality=(IntervalQuality(), suspect))

    merged = merge_grid_flow_history(existing, grid_flow(2, (3.0,)))

    assert merged.quality == (IntervalQuality(), suspect, IntervalQuality())
    assert merge_grid_flow_history(None, grid_flow(0, (1.0,))).quality == ()


@pytest.mark.parametrize(
    ("existing", "incoming", "message"),
    [
        (grid_flow(0, (1.0,)), grid_flow(2, (2.0,)), "contiguous"),
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
            grid_flow(0, (1.0, 2.0), quality=(IntervalQuality(),)),
            "quality is misaligned",
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
    original = store.save(GRID_KEY, GRID_ADAPTER, grid_flow(0, (1.0, 2.0)))

    with pytest.raises(ProviderDataStoreError, match="contiguous"):
        store.save(GRID_KEY, GRID_ADAPTER, grid_flow(5, (9.0,)))

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
