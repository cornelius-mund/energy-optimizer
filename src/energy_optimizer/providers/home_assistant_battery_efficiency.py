"""Measured battery and inverter efficiency calculation."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from typing import Any, NamedTuple

from energy_optimizer.config import (
    HomeAssistantBatteryEfficiencyConfiguration,
    HomeAssistantConfiguration,
)
from energy_optimizer.exclusions import (
    ExcludedDataPoint,
    ExclusionCause,
    ExclusionReason,
    HourExclusion,
    cap_data_points,
    history_unavailable_exclusions,
    merge_exclusions,
)
from energy_optimizer.providers.home_assistant_energy import (
    EnergyAggregate,
    HomeAssistantEnergySeries,
    gap_causes,
    overlapping_hours,
)
from energy_optimizer.providers.home_assistant_history import (
    HistoryNeed,
    HistoryPlan,
    HistorySample,
    HomeAssistantError,
    HomeAssistantHistory,
)
from energy_optimizer.providers.interfaces import (
    BATTERY_EFFICIENCY_HISTORY_SOURCE_ID,
    BATTERY_EFFICIENCY_SOURCE_ID,
    DEFAULT_EFFICIENCY_RATIO,
    HOUSEHOLD_LOAD_MAX_VALUES,
    BatteryEfficiencyData,
    BatteryEfficiencyHistoryData,
    EfficiencyComponentStatus,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import align_to_next_hour, as_utc

_HOUR = timedelta(hours=1)
# The fields of ``BatteryEfficiencyHistoryData`` that hold one energy leg, named
# as the leg is in warnings, followed by the state of charge at the hour boundaries.
_ENERGY_FIELDS = {
    "battery input": "battery_energy_in_kwh",
    "battery output": "battery_energy_out_kwh",
    "charge input": "inverter_charge_energy_in_kwh",
    "charge output": "inverter_charge_energy_out_kwh",
    "discharge input": "inverter_discharge_energy_in_kwh",
    "discharge output": "inverter_discharge_energy_out_kwh",
}
_SERIES_FIELDS = (*_ENERGY_FIELDS.values(), "state_of_charge_percent")
_COMPONENTS = (
    "battery_efficiency",
    "inverter_charge_efficiency",
    "inverter_discharge_efficiency",
    "round_trip_efficiency",
)


@dataclass(frozen=True)
class _StateOfCharge:
    """Hourly state of charge read from Home Assistant, with its excluded hours.

    ``values[i]`` is the last state recorded at or before ``start_time + i``
    hours, the opening boundary of hour ``i``; it is ``None`` when that state is
    invalid. The series holds one value more than the hours it spans, because the
    last hour also needs its closing boundary.
    """

    start_time: datetime
    values: tuple[float | None, ...]
    latest_observation_at: datetime
    exclusions: tuple[HourExclusion, ...]


class _Component(NamedTuple):
    """One efficiency component: its status, an optional warning and its ratio.

    The ratio is the default one unless the component could be calculated.
    """

    status: EfficiencyComponentStatus
    warning: str | None = None
    efficiency: float = DEFAULT_EFFICIENCY_RATIO


class HomeAssistantBatteryEfficiencyImporter:
    """Declare and build the aligned measured history the calculator needs."""

    def __init__(self, configuration: HomeAssistantConfiguration) -> None:
        if (
            configuration.battery is None
            or configuration.battery.efficiency_calculation is None
        ):
            raise HomeAssistantError(
                "Home Assistant calculated battery efficiency is not configured"
            )
        self.configuration = configuration
        self.efficiency_configuration = configuration.battery.efficiency_calculation

    def plan(
        self, start_time: datetime, end_time: datetime, *, now: datetime | None = None
    ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
        """Declare all configured expressions for one complete history range.

        Building reads the shared history that the caller imported for the
        declared needs; this importer makes no Home Assistant request itself.
        """
        start = _as_utc(start_time)
        end = _as_utc(end_time)
        if start.minute or start.second or start.microsecond:
            raise HomeAssistantError(
                "calculated battery efficiency history must start on an hour"
            )
        if end <= start or end.minute or end.second or end.microsecond:
            raise HomeAssistantError(
                "calculated battery efficiency history must end on an hour"
            )

        configuration = self.efficiency_configuration
        aggregates = [
            EnergyAggregate(
                energy, start, end, label=f"battery efficiency {name} {role}"
            )
            for name, leg in (
                ("battery", configuration.battery),
                ("inverter_charge", configuration.inverter_charge),
                ("inverter_discharge", configuration.inverter_discharge),
            )
            for role, energy in (("input", leg.energy_in), ("output", leg.energy_out))
        ]
        # The state-of-charge series reads one value per boundary, so the closing
        # boundary of the last hour is the requested end itself.
        needs = (
            *(need for aggregate in aggregates for need in aggregate.needs()),
            HistoryNeed(configuration.state_of_charge.entity_id, "state", start, end),
        )

        def build(history: HomeAssistantHistory) -> BatteryEfficiencyHistoryData:
            series = [aggregate.build(history) for aggregate in aggregates]
            soc = _state_of_charge_series(
                history, configuration.state_of_charge.entity_id, start, end
            )
            return _align_history(series, soc, end, now)

        return HistoryPlan(needs=needs, build=build)


def _as_utc(value: datetime) -> datetime:
    return as_utc(
        value,
        error_factory=HomeAssistantError,
        message="Home Assistant efficiency times must include a timezone",
    )


def _align_history(
    energy: list[HomeAssistantEnergySeries],
    soc: _StateOfCharge,
    requested_end: datetime,
    now: datetime | None,
) -> BatteryEfficiencyHistoryData:
    """Align all legs and the state of charge and exclude hours across them.

    An hour excluded in any energy leg or in the state of charge is excluded
    in every component, so the efficiency ratios never mix valid and invalid
    legs. A state-of-charge value next to an excluded hour is dropped too.
    """
    start = max([item.start_time for item in energy] + [soc.start_time])
    end = min(
        [item.start_time + timedelta(hours=len(item.values_kw)) for item in energy]
        + [soc.start_time + timedelta(hours=len(soc.values) - 1), requested_end]
    )
    count = int((end - start).total_seconds() // 3600)
    if count <= 0:
        raise HomeAssistantError(
            "battery efficiency histories have no aligned intervals"
        )

    sources: list[HomeAssistantEnergySeries | _StateOfCharge] = [*energy, soc]
    exclusions = merge_exclusions(
        item
        for source in sources
        for item in source.exclusions
        if start <= item.hour_start < end
    )
    excluded = {int((item.hour_start - start) / _HOUR) for item in exclusions}

    def values(item: HomeAssistantEnergySeries) -> tuple[float | None, ...]:
        result = item.values_from(start, count)
        if len(result) != count:
            raise HomeAssistantError(
                "battery efficiency energy histories are misaligned"
            )
        return _exclude(result, excluded)

    soc_offset = int((start - soc.start_time).total_seconds() // 3600)
    soc_values = soc.values[soc_offset : soc_offset + count + 1]
    if len(soc_values) != count + 1:
        raise HomeAssistantError(
            "battery efficiency state-of-charge history is misaligned"
        )
    # Hour ``i`` lies between the boundaries ``i`` and ``i + 1``, so an excluded
    # hour drops both.
    adjacent = excluded | {index + 1 for index in excluded}
    battery_in, battery_out, charge_in, charge_out, discharge_in, discharge_out = (
        values(item) for item in energy
    )
    retrieved_at = _as_utc(now or datetime.now(timezone.utc))
    latest_observation_at = min(
        [item.latest_observation_at for item in energy]
        + [soc.latest_observation_at, start + count * _HOUR]
    )
    return BatteryEfficiencyHistoryData(
        schema_version="1",
        start_time=start,
        interval_minutes=60,
        battery_energy_in_kwh=battery_in,
        battery_energy_out_kwh=battery_out,
        inverter_charge_energy_in_kwh=charge_in,
        inverter_charge_energy_out_kwh=charge_out,
        inverter_discharge_energy_in_kwh=discharge_in,
        inverter_discharge_energy_out_kwh=discharge_out,
        state_of_charge_percent=_exclude(soc_values, adjacent),
        unit="kWh",
        source=SourceMetadata(
            provider="home-assistant", entity_id=BATTERY_EFFICIENCY_HISTORY_SOURCE_ID
        ),
        retrieved_at=retrieved_at,
        latest_observation_at=latest_observation_at,
        exclusions=exclusions,
    )


def _state_of_charge_series(
    history: HomeAssistantHistory,
    entity_id: str,
    start_time: datetime,
    end_time: datetime,
) -> _StateOfCharge:
    """Forward-fill the state-of-charge samples of a window to hour boundaries.

    The result holds the state in force at every boundary from the first whole
    hour to ``end_time``, one value more than there are hours. A sample that is
    unavailable, not a number, or outside 0 to 100 percent is not carried forward:
    the hours in which it is in force are excluded and the values at their
    boundaries are ``None``.
    """
    records = {
        sample.timestamp: sample
        for sample in history.window(entity_id, "state", start_time, end_time)
    }
    if not records:
        raise HomeAssistantError(
            f"Home Assistant returned no usable state-of-charge history for {entity_id}"
        )
    effective_start = align_to_next_hour(min(records))
    hours = int((end_time - effective_start).total_seconds() // 3600)
    if hours <= 0:
        raise HomeAssistantError(
            "Home Assistant returned no complete state-of-charge history for "
            f"{entity_id}"
        )
    ordered = sorted(records.items())

    causes_by_hour: dict[int, list[ExclusionCause]] = {}
    gap: list[tuple[ExclusionReason, ExcludedDataPoint]] = []
    gap_start = effective_start
    last_valid_at: datetime | None = None

    def flush(valid_after_at: datetime | None, span_end: datetime) -> None:
        causes = gap_causes(entity_id, gap, last_valid_at, valid_after_at)
        for hour in overlapping_hours(effective_start, gap_start, span_end, hours):
            causes_by_hour.setdefault(hour, []).extend(causes)

    for timestamp, sample in ordered:
        if timestamp > end_time:
            break
        reason = _state_of_charge_reason(sample)
        if reason is not None:
            if not gap:
                gap_start = timestamp
            gap.append(
                (
                    reason,
                    ExcludedDataPoint(sample.recorded_at, sample.state, sample.unit),
                )
            )
            continue
        if gap:
            flush(sample.recorded_at, timestamp)
            gap = []
        last_valid_at = sample.recorded_at
    if gap:
        flush(None, end_time)

    # Home Assistant's history API only returns a row when an entity's
    # state changes, so an hour with no row does not mean the value is
    # missing; it means the value has not changed since the previous
    # observation. Carry the most recent known value forward to every hour
    # boundary instead of requiring a fresh row in every bucket. A state
    # recorded exactly on a boundary is in force there, like the counter
    # observations that close the hour before it.
    values: list[float | None] = []
    last_sample = ordered[0][1]
    next_index = 0
    for index in range(hours + 1):
        boundary = effective_start + timedelta(hours=index)
        while next_index < len(ordered) and ordered[next_index][0] <= boundary:
            last_sample = ordered[next_index][1]
            next_index += 1
        values.append(
            None if _state_of_charge_reason(last_sample) else last_sample.value
        )
    valid_times = [
        sample.timestamp for _, sample in ordered if not _state_of_charge_reason(sample)
    ]
    return _StateOfCharge(
        start_time=effective_start,
        values=tuple(values),
        latest_observation_at=max(valid_times or [max(records)]),
        exclusions=tuple(
            HourExclusion(effective_start + hour * _HOUR, cap_data_points(causes))
            for hour, causes in sorted(causes_by_hour.items())
        ),
    )


def _exclude(
    values: tuple[float | None, ...], indexes: set[int]
) -> tuple[float | None, ...]:
    """Blank the values at the given positions."""
    return tuple(
        None if index in indexes else value for index, value in enumerate(values)
    )


def _state_of_charge_reason(sample: HistorySample) -> ExclusionReason | None:
    """Return why a state-of-charge sample cannot be used, or ``None``."""
    if sample.invalid is not None:
        return sample.invalid
    if sample.value is None or not 0 <= sample.value <= 100:
        return "soc_out_of_range"
    return None


def merge_battery_efficiency_history(
    existing: BatteryEfficiencyHistoryData | None,
    incoming: BatteryEfficiencyHistoryData,
) -> BatteryEfficiencyHistoryData:
    """Extend persisted battery-efficiency history with newly fetched hours.

    ``incoming`` starts where ``existing`` ends, so ingestion only ever needs to
    request the missing hours from Home Assistant instead of re-fetching the
    complete retained history on every scheduled run. When Home Assistant no
    longer holds the hours between the two, ``incoming`` starts later and those
    hours can never be fetched: they are excluded as ``history_unavailable``.
    """
    if existing is None:
        return bound_battery_efficiency_history(incoming)
    existing_hours = len(existing.battery_energy_in_kwh)
    expected_start = existing.start_time + existing_hours * _HOUR
    gap_hours, remainder = divmod(incoming.start_time - expected_start, _HOUR)
    if gap_hours < 0 or remainder:
        raise HomeAssistantError(
            "battery efficiency history is not contiguous with the persisted history"
        )
    gap = (
        history_unavailable_exclusions(
            "battery_efficiency", expected_start, incoming.start_time
        )
        if gap_hours
        else ()
    )
    unavailable = (None,) * gap_hours
    # The incoming SoC series repeats the boundary sample already recorded as the
    # existing series' last value; keep it only once. Over a gap, the boundaries
    # of the missing hours are all unknown, including the two that touch a valid
    # hour, exactly as a full import drops the state of charge around every
    # excluded hour.
    state_of_charge = existing.state_of_charge_percent[:-1] + (
        (None,) * (gap_hours + 1) + incoming.state_of_charge_percent[1:]
        if gap_hours
        else incoming.state_of_charge_percent
    )
    # A full import drops the state of charge around every excluded hour. The
    # incoming series cannot know that the last persisted hour is excluded, and
    # that hour brackets the boundary value the two series share.
    if any(
        item.hour_start == existing.start_time + (existing_hours - 1) * _HOUR
        for item in existing.exclusions
    ):
        state_of_charge = _exclude(state_of_charge, {existing_hours})
    return bound_battery_efficiency_history(
        replace(
            incoming,
            start_time=existing.start_time,
            **{
                name: getattr(existing, name) + unavailable + getattr(incoming, name)
                for name in _ENERGY_FIELDS.values()
            },
            state_of_charge_percent=state_of_charge,
            exclusions=merge_exclusions(existing.exclusions, gap, incoming.exclusions),
        )
    )


def bound_battery_efficiency_history(
    data: BatteryEfficiencyHistoryData, max_hours: int = HOUSEHOLD_LOAD_MAX_VALUES
) -> BatteryEfficiencyHistoryData:
    """Trim retained battery-efficiency history to the bounded retention window."""
    offset = len(data.battery_energy_in_kwh) - max_hours
    if offset <= 0:
        return data
    new_start = data.start_time + offset * _HOUR
    return replace(
        data,
        start_time=new_start,
        **{name: getattr(data, name)[offset:] for name in _SERIES_FIELDS},
        exclusions=tuple(
            item for item in data.exclusions if item.hour_start >= new_start
        ),
    )


def calculate_battery_efficiency(
    history: BatteryEfficiencyHistoryData,
    configuration: HomeAssistantBatteryEfficiencyConfiguration,
    *,
    capacity_kwh: float | None = None,
    now: datetime | None = None,
) -> BatteryEfficiencyData:
    """Calculate all efficiency components from one persisted aligned history."""
    retrieved_at = as_utc(
        now or datetime.now(timezone.utc),
        error_factory=HomeAssistantError,
        message="efficiency calculation times must include a timezone",
    )
    history = _select_history(history, configuration.history_start)
    problem = _history_problem(history)
    if problem is not None:
        return _result(
            history,
            retrieved_at,
            "invalid",
            warnings=(problem,),
            component_statuses=dict.fromkeys(_COMPONENTS, "invalid"),
        )

    full_indices: list[int] = []
    was_below_full = True
    for index, value in enumerate(history.state_of_charge_percent):
        if value is None:
            continue
        if value < configuration.full_soc_threshold_percent:
            was_below_full = True
        elif was_below_full:
            full_indices.append(index)
            was_below_full = False
    # An excluded hour has no energy, so a cycle that contains one is incomplete
    # and is not used.
    cycles = [
        (left, right)
        for left, right in pairwise(full_indices)
        if None not in history.battery_energy_in_kwh[left:right]
    ]
    battery_in = sum(
        _total(history.battery_energy_in_kwh[left:right]) for left, right in cycles
    )
    battery_out = sum(
        _total(history.battery_energy_out_kwh[left:right]) for left, right in cycles
    )
    charge_in = _total(history.inverter_charge_energy_in_kwh)
    discharge_in = _total(history.inverter_discharge_energy_in_kwh)
    components = {
        "battery_efficiency": _component(
            "battery",
            battery_in,
            battery_out,
            configuration.minimum_battery_throughput_kwh,
        )
        if cycles
        else _Component(
            "unavailable",
            "battery efficiency is unavailable: no complete full-SoC battery cycle "
            f"is available; using default efficiency ratio {DEFAULT_EFFICIENCY_RATIO}",
        ),
        "inverter_charge_efficiency": _component(
            "inverter charge",
            charge_in,
            _total(history.inverter_charge_energy_out_kwh),
            configuration.minimum_inverter_charge_throughput_kwh,
        ),
        "inverter_discharge_efficiency": _component(
            "inverter discharge",
            discharge_in,
            _total(history.inverter_discharge_energy_out_kwh),
            configuration.minimum_inverter_discharge_throughput_kwh,
        ),
    }
    efficiencies = {name: part.efficiency for name, part in components.items()}
    component_statuses = {name: part.status for name, part in components.items()}
    warnings = [
        part.warning for part in components.values() if part.warning is not None
    ]
    # Without an invalid component, a component that is not calculated uses the
    # default ratio, so the round trip is calculated with defaults.
    if "invalid" in component_statuses.values():
        status = "invalid"
        component_statuses["round_trip_efficiency"] = "invalid"
    elif any(value != "calculated" for value in component_statuses.values()):
        status = "insufficient_data"
        component_statuses["round_trip_efficiency"] = "calculated_with_defaults"
        warnings.append(
            "complete round-trip efficiency is calculated using one or more "
            "default component ratios"
        )
    else:
        status = "ok"
        component_statuses["round_trip_efficiency"] = "calculated"
    _check_state_of_charge_balance(history, configuration, capacity_kwh, warnings)
    return _result(
        history,
        retrieved_at,
        status,
        **efficiencies,
        round_trip_efficiency=math.prod(efficiencies.values()),
        battery_throughput_kwh=battery_in,
        charge_throughput_kwh=charge_in,
        discharge_throughput_kwh=discharge_in,
        complete_cycle_count=len(cycles),
        warnings=warnings,
        component_statuses=component_statuses,
    )


def _history_problem(history: BatteryEfficiencyHistoryData) -> str | None:
    """Return why a persisted history cannot be calculated from, or ``None``."""
    legs = {label: getattr(history, field) for label, field in _ENERGY_FIELDS.items()}
    count = len(history.battery_energy_in_kwh)
    if (
        any(len(values) != count for values in legs.values())
        or len(history.state_of_charge_percent) != count + 1
    ):
        return "efficiency histories are not aligned"
    for label, values in legs.items():
        if _has_invalid_value(values):
            return f"{label} contains negative or non-finite values"
    if _has_invalid_value(history.state_of_charge_percent):
        return "state-of-charge history contains invalid values"
    return None


def _has_invalid_value(values: tuple[float | None, ...]) -> bool:
    return any(
        value is not None and (not math.isfinite(value) or value < 0)
        for value in values
    )


def _component(
    label: str, energy_in: float, energy_out: float, minimum_kwh: float
) -> _Component:
    """Calculate one efficiency ratio, or explain why the default applies."""
    if energy_in <= 0:
        return _Component(
            "invalid",
            f"{label} efficiency is invalid: zero throughput denominator; using "
            f"default efficiency ratio {DEFAULT_EFFICIENCY_RATIO}",
        )
    if energy_in < minimum_kwh:
        return _Component(
            "defaulted",
            f"{label} efficiency uses default efficiency ratio "
            f"{DEFAULT_EFFICIENCY_RATIO}: throughput {energy_in:.3f} kWh is below "
            "the configured minimum",
        )
    ratio = energy_out / energy_in
    if not math.isfinite(ratio) or ratio < 0:
        return _Component("invalid", f"{label} efficiency is non-finite or negative")
    return _Component("calculated", efficiency=min(ratio, 1.0))


def _check_state_of_charge_balance(
    history: BatteryEfficiencyHistoryData,
    configuration: HomeAssistantBatteryEfficiencyConfiguration,
    capacity_kwh: float | None,
    warnings: list[str],
) -> None:
    """Flag hours where the measured energy makes the SoC change impossible.

    This checks physical plausibility rather than round-trip loss: during an
    hour with only charging (or only discharging) energy measured, the change
    in stored energy can never exceed what was delivered (while charging) or
    be exceeded by what was delivered (while discharging), beyond the
    configured tolerance. Expected conversion and battery losses always keep
    the measured change within these bounds, so a violation indicates a data
    problem such as a misconfigured or drifting entity, not ordinary loss.
    """
    if capacity_kwh is None:
        warnings.append(
            "state-of-charge balance was not checked because battery capacity "
            "is unavailable"
        )
        return

    tolerance = configuration.soc_balance_tolerance_kwh
    violations = 0
    max_deviation = 0.0
    count = len(history.battery_energy_in_kwh)
    for index in range(count):
        energy_in = history.battery_energy_in_kwh[index]
        energy_out = history.battery_energy_out_kwh[index]
        soc_before = history.state_of_charge_percent[index]
        soc_after = history.state_of_charge_percent[index + 1]
        if (
            energy_in is None
            or energy_out is None
            or soc_before is None
            or soc_after is None
        ):
            continue
        soc_delta = (soc_after - soc_before) / 100 * capacity_kwh
        deviation: float | None = None
        if energy_in > 0 and energy_out == 0:
            # Stored energy can never exceed what was delivered while charging.
            deviation = soc_delta - energy_in
        elif energy_out > 0 and energy_in == 0:
            # Delivered energy can never exceed what was removed from storage.
            deviation = energy_out - (-soc_delta)
        if deviation is not None and deviation > tolerance:
            violations += 1
            max_deviation = max(max_deviation, deviation)
    if violations:
        warnings.append(
            "state-of-charge change is physically inconsistent with measured "
            f"battery energy for {violations} of {count} interval(s); maximum "
            f"deviation {max_deviation:.3f} kWh exceeds the configured tolerance"
        )


def _total(values: tuple[float | None, ...]) -> float:
    """Sum the hours that have a value; excluded hours contribute nothing."""
    return sum(value for value in values if value is not None)


def _select_history(
    history: BatteryEfficiencyHistoryData, history_start: datetime | None
) -> BatteryEfficiencyHistoryData:
    """Apply the configured inclusive start without introducing a rolling window."""
    if history_start is None:
        return history
    selected_start = max(history.start_time, history_start.astimezone(timezone.utc))
    offset = int((selected_start - history.start_time).total_seconds() // 3600)
    count = len(history.battery_energy_in_kwh) - offset
    if count <= 0:
        empty: dict[str, Any] = dict.fromkeys(_SERIES_FIELDS, ())
        return replace(history, start_time=selected_start, **empty, exclusions=())
    # Keep the last ``count`` hours: those from the selected start on.
    return bound_battery_efficiency_history(history, count)


def _result(
    history: BatteryEfficiencyHistoryData,
    retrieved_at: datetime,
    status: str,
    *,
    inverter_charge_efficiency: float = DEFAULT_EFFICIENCY_RATIO,
    inverter_discharge_efficiency: float = DEFAULT_EFFICIENCY_RATIO,
    battery_efficiency: float = DEFAULT_EFFICIENCY_RATIO,
    round_trip_efficiency: float = DEFAULT_EFFICIENCY_RATIO,
    battery_throughput_kwh: float = 0.0,
    charge_throughput_kwh: float = 0.0,
    discharge_throughput_kwh: float = 0.0,
    complete_cycle_count: int = 0,
    warnings: Iterable[str],
    component_statuses: dict[str, EfficiencyComponentStatus],
) -> BatteryEfficiencyData:
    """Build a consistently shaped result for valid and unusable histories."""
    return BatteryEfficiencyData(
        schema_version="1",
        status=status,  # type: ignore[arg-type]
        inverter_charge_efficiency=inverter_charge_efficiency,
        inverter_discharge_efficiency=inverter_discharge_efficiency,
        battery_efficiency=battery_efficiency,
        round_trip_efficiency=round_trip_efficiency,
        history_start=history.start_time,
        history_end=history.start_time
        + timedelta(hours=len(history.battery_energy_in_kwh)),
        battery_throughput_kwh=battery_throughput_kwh,
        charge_throughput_kwh=charge_throughput_kwh,
        discharge_throughput_kwh=discharge_throughput_kwh,
        complete_cycle_count=complete_cycle_count,
        unit="ratio",
        source=SourceMetadata(
            provider="home-assistant", entity_id=BATTERY_EFFICIENCY_SOURCE_ID
        ),
        retrieved_at=retrieved_at,
        latest_observation_at=history.latest_observation_at,
        warnings=tuple(dict.fromkeys(warnings)),
        defaulted_components=tuple(
            name
            for name, component_status in component_statuses.items()
            if component_status in {"defaulted", "unavailable", "invalid"}
        ),
        component_statuses=component_statuses,
    )


__all__ = [
    "HomeAssistantBatteryEfficiencyImporter",
    "bound_battery_efficiency_history",
    "calculate_battery_efficiency",
    "merge_battery_efficiency_history",
]
