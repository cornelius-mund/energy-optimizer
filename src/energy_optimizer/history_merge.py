"""Pure merge and retention rules for retained hourly provider history."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from energy_optimizer.exclusions import HourExclusion
from energy_optimizer.household_load_records import as_utc
from energy_optimizer.providers.interfaces import (
    HOUSEHOLD_LOAD_MAX_VALUES,
    ElectricityPriceData,
    GridFlowData,
)
from energy_optimizer.storage_errors import ProviderDataStoreError

# All retained histories share the household-load ten-year limit.
HISTORY_RETENTION_HOURS = HOUSEHOLD_LOAD_MAX_VALUES
_HOUR = timedelta(hours=1)


def _require_hourly_start(value: datetime, label: str) -> datetime:
    """Return one UTC timestamp aligned to the hour."""
    timestamp = as_utc(value)
    if timestamp.minute or timestamp.second or timestamp.microsecond:
        raise ProviderDataStoreError(f"{label} timestamps must be aligned to the hour")
    return timestamp


def grid_flow_points(
    data: GridFlowData,
) -> dict[datetime, tuple[float | None, float | None, HourExclusion | None]]:
    """Validate and index one normalized hourly grid-flow series.

    Import and export are excluded together: an hour has both values or neither,
    and an hour without values has exactly one exclusion.
    """
    start = _require_hourly_start(data.start_time, "grid-flow")
    if data.interval_minutes != 60 or data.unit != "kW":
        raise ProviderDataStoreError("grid-flow data must use hourly kW values")
    as_utc(data.retrieved_at)
    as_utc(data.latest_observation_at)
    if not data.import_kw or len(data.import_kw) != len(data.export_kw):
        raise ProviderDataStoreError(
            "grid-flow import and export must contain the same non-zero number "
            "of values"
        )
    exclusions: dict[datetime, HourExclusion] = {}
    for item in data.exclusions:
        timestamp = as_utc(item.hour_start)
        if timestamp in exclusions:
            raise ProviderDataStoreError(
                "grid-flow history has two exclusions for one hour"
            )
        exclusions[timestamp] = item
    points: dict[datetime, tuple[float | None, float | None, HourExclusion | None]] = {}
    for index, (imported, exported) in enumerate(zip(data.import_kw, data.export_kw)):
        timestamp = start + index * _HOUR
        if imported is None or exported is None:
            if imported is not None or exported is not None:
                raise ProviderDataStoreError(
                    "grid-flow import and export must be excluded together"
                )
            points[timestamp] = (None, None, exclusions.get(timestamp))
            continue
        if not (
            math.isfinite(imported)
            and math.isfinite(exported)
            and imported >= 0
            and exported >= 0
        ):
            raise ProviderDataStoreError(
                "grid-flow values must be finite and non-negative"
            )
        points[timestamp] = (float(imported), float(exported), None)
    excluded = {timestamp for timestamp, point in points.items() if point[0] is None}
    if excluded != set(exclusions):
        raise ProviderDataStoreError(
            "grid-flow hours without values must match their exclusions"
        )
    return points


def merge_grid_flow_history(
    existing: GridFlowData | None,
    incoming: GridFlowData,
) -> GridFlowData:
    """Merge hourly grid-flow data, preferring incoming values, within retention.

    The retained history must stay contiguous so that consumers can rely on one
    start time and equally spaced values; a gap is rejected instead of hidden.
    An excluded hour occupies its place without values.
    """
    points = {}
    if existing is not None:
        if existing.source != incoming.source:
            raise ProviderDataStoreError(
                "grid-flow history source identity does not match incoming data"
            )
        points.update(grid_flow_points(existing))
    points.update(grid_flow_points(incoming))
    ordered = sorted(points.items())[-HISTORY_RETENTION_HOURS:]
    timestamps = [timestamp for timestamp, _ in ordered]
    if any(
        later - earlier != _HOUR for earlier, later in zip(timestamps, timestamps[1:])
    ):
        raise ProviderDataStoreError(
            "grid-flow history must contain contiguous hourly timestamps"
        )
    previous = [existing] if existing is not None else []
    return GridFlowData(
        schema_version=incoming.schema_version,
        start_time=timestamps[0],
        interval_minutes=60,
        import_kw=tuple(point[0] for _, point in ordered),
        export_kw=tuple(point[1] for _, point in ordered),
        unit=incoming.unit,
        source=incoming.source,
        retrieved_at=max(as_utc(item.retrieved_at) for item in [*previous, incoming]),
        latest_observation_at=max(
            as_utc(item.latest_observation_at) for item in [*previous, incoming]
        ),
        exclusions=tuple(point[2] for _, point in ordered if point[2] is not None),
    )


def price_points(data: ElectricityPriceData) -> dict[datetime, tuple[float, float]]:
    """Validate and index one normalized hourly price series."""
    count = len(data.timestamps)
    if (
        count == 0
        or len(data.import_price_eur_per_kwh) != count
        or len(data.export_price_eur_per_kwh) != count
    ):
        raise ProviderDataStoreError(
            "electricity-price history requires aligned, non-empty import and "
            "export prices"
        )
    if data.interval_minutes != 60 or data.unit != "EUR/kWh":
        raise ProviderDataStoreError(
            "electricity-price data must use hourly EUR/kWh values"
        )
    as_utc(data.retrieved_at)
    as_utc(data.expires_at)
    points: dict[datetime, tuple[float, float]] = {}
    for timestamp, imported, exported in zip(
        data.timestamps,
        data.import_price_eur_per_kwh,
        data.export_price_eur_per_kwh,
    ):
        if not (math.isfinite(imported) and math.isfinite(exported)):
            raise ProviderDataStoreError("electricity prices must be finite")
        points[_require_hourly_start(timestamp, "electricity-price")] = (
            float(imported),
            float(exported),
        )
    return points


def merge_price_history(
    existing: ElectricityPriceData | None,
    incoming: ElectricityPriceData,
) -> ElectricityPriceData:
    """Merge hourly market prices, preferring the newest retrieval per hour.

    Unlike consumption, published prices may legitimately skip hours, so the
    retained timestamps are explicit and gaps stay visible to readers.
    """
    points = {}
    if existing is not None:
        if existing.source != incoming.source:
            raise ProviderDataStoreError(
                "electricity-price history source identity does not match incoming data"
            )
        points.update(price_points(existing))
    points.update(price_points(incoming))
    ordered = sorted(points.items())[-HISTORY_RETENTION_HOURS:]
    previous = [existing] if existing is not None else []
    return ElectricityPriceData(
        schema_version=incoming.schema_version,
        timestamps=tuple(timestamp for timestamp, _ in ordered),
        interval_minutes=60,
        import_price_eur_per_kwh=tuple(point[0] for _, point in ordered),
        export_price_eur_per_kwh=tuple(point[1] for _, point in ordered),
        unit=incoming.unit,
        source=incoming.source,
        retrieved_at=max(as_utc(item.retrieved_at) for item in [*previous, incoming]),
        expires_at=max(as_utc(item.expires_at) for item in [*previous, incoming]),
    )
