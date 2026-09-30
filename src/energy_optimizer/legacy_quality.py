"""Turn interval quality persisted by earlier versions into excluded hours.

Versions before hour exclusion kept a value for an hour they had flagged
``suspect``. The current rule is that such an hour is excluded. Persisted data
may be the only remaining copy of a long history, because Home Assistant may
have purged it, so it is converted on read instead of being discarded: valid
hours keep their values unchanged, and every hour that was flagged suspect
loses its value and becomes an excluded hour with the reason
``flagged_by_earlier_version``.
"""

from collections.abc import Container
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial
from typing import cast

from energy_optimizer.exclusions import ExclusionCause, HourExclusion, merge_exclusions
from energy_optimizer.providers.interfaces import (
    BatteryEfficiencyHistoryData,
    GridFlowData,
    HouseholdLoadData,
    IntervalQuality,
)

_HOUR = timedelta(hours=1)


def legacy_exclusion(
    hour_start: datetime, reason: str | None, entity_id: str | None
) -> HourExclusion:
    """Describe one hour that an earlier version flagged as suspect."""
    detail = ", ".join(
        part
        for part in (
            f"reason {reason}" if reason else None,
            f"entity {entity_id}" if entity_id else None,
        )
        if part
    )
    message = "The hour was flagged suspect by an earlier version" + (
        f" ({detail})." if detail else "."
    )
    return HourExclusion(
        hour_start,
        (ExclusionCause.of("flagged_by_earlier_version", message, entity_id, []),),
    )


def upgrade_legacy_quality[ModelT](data: ModelT) -> ModelT:
    """Convert suspect quality of persisted history into excluded hours."""
    if isinstance(data, HouseholdLoadData):
        return cast(ModelT, _upgrade_household_load(data))
    if isinstance(data, GridFlowData):
        return cast(ModelT, _upgrade_grid_flow(data))
    if isinstance(data, BatteryEfficiencyHistoryData):
        return cast(ModelT, _upgrade_efficiency_history(data))
    return data


def _suspect_hours(
    quality: tuple[IntervalQuality, ...], start_time: datetime, count: int
) -> dict[int, HourExclusion]:
    """Return the exclusion of every hour index that the legacy quality flags."""
    if len(quality) != count:
        raise ValueError("persisted interval quality is misaligned")
    return {
        index: legacy_exclusion(start_time + index * _HOUR, item.reason, item.entity_id)
        for index, item in enumerate(quality)
        if item.status == "suspect"
    }


def _drop(
    values: tuple[float | None, ...], indices: Container[int]
) -> tuple[float | None, ...]:
    return tuple(
        None if index in indices else value for index, value in enumerate(values)
    )


def _upgrade_household_load(data: HouseholdLoadData) -> HouseholdLoadData:
    if not data.quality:
        return data
    legacy = _suspect_hours(data.quality, data.start_time, len(data.load_kw))
    return replace(
        data,
        load_kw=_drop(data.load_kw, legacy),
        exclusions=merge_exclusions(data.exclusions, legacy.values()),
        quality=(),
    )


def _upgrade_grid_flow(data: GridFlowData) -> GridFlowData:
    if not data.quality:
        return data
    legacy = _suspect_hours(data.quality, data.start_time, len(data.import_kw))
    return replace(
        data,
        import_kw=_drop(data.import_kw, legacy),
        export_kw=_drop(data.export_kw, legacy),
        exclusions=merge_exclusions(data.exclusions, legacy.values()),
        quality=(),
    )


def _upgrade_efficiency_history(
    data: BatteryEfficiencyHistoryData,
) -> BatteryEfficiencyHistoryData:
    if not data.quality:
        return data
    count = len(data.battery_energy_in_kwh)
    legacy = _suspect_hours(data.quality, data.start_time, count)
    boundaries = legacy.keys() | {index + 1 for index in legacy}
    drop = partial(_drop, indices=legacy)
    return replace(
        data,
        battery_energy_in_kwh=drop(data.battery_energy_in_kwh),
        battery_energy_out_kwh=drop(data.battery_energy_out_kwh),
        inverter_charge_energy_in_kwh=drop(data.inverter_charge_energy_in_kwh),
        inverter_charge_energy_out_kwh=drop(data.inverter_charge_energy_out_kwh),
        inverter_discharge_energy_in_kwh=drop(data.inverter_discharge_energy_in_kwh),
        inverter_discharge_energy_out_kwh=drop(data.inverter_discharge_energy_out_kwh),
        state_of_charge_percent=_drop(data.state_of_charge_percent, boundaries),
        exclusions=merge_exclusions(data.exclusions, legacy.values()),
        quality=(),
    )
