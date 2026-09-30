"""Measured battery and inverter efficiency calculation."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from energy_optimizer.config import (
    HomeAssistantBatteryEfficiencyConfiguration,
    HomeAssistantBatteryEntityConfiguration,
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
from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    as_utc,
)

logger = logging.getLogger(__name__)
_HOUR = timedelta(hours=1)


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
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        now: datetime | None = None,
    ) -> HistoryPlan[BatteryEfficiencyHistoryData]:
        """Declare all configured expressions for one complete history range.

        Building reads the shared history that the caller imported for the
        declared needs; this importer makes no Home Assistant request itself.
        """
        start = self._as_utc(start_time)
        end = self._as_utc(end_time)
        if start.minute or start.second or start.microsecond:
            raise HomeAssistantError(
                "calculated battery efficiency history must start on an hour"
            )
        if end <= start or end.minute or end.second or end.microsecond:
            raise HomeAssistantError(
                "calculated battery efficiency history must end on an hour"
            )

        configuration = self.efficiency_configuration
        legs = {
            "battery": configuration.battery,
            "inverter_charge": configuration.inverter_charge,
            "inverter_discharge": configuration.inverter_discharge,
        }
        aggregates = {
            name: (
                EnergyAggregate(
                    leg.energy_in,
                    start,
                    end,
                    label=f"battery efficiency {name} input",
                ),
                EnergyAggregate(
                    leg.energy_out,
                    start,
                    end,
                    label=f"battery efficiency {name} output",
                ),
            )
            for name, leg in legs.items()
        }
        # The state-of-charge series reads one value per boundary, so the closing
        # boundary of the last hour is the requested end itself.
        needs = tuple(
            need
            for pair in aggregates.values()
            for aggregate in pair
            for need in aggregate.needs()
        ) + (HistoryNeed(configuration.state_of_charge.entity_id, "state", start, end),)

        def build(history: HomeAssistantHistory) -> BatteryEfficiencyHistoryData:
            series = {
                name: (
                    energy_in.build(history),
                    energy_out.build(history),
                )
                for name, (energy_in, energy_out) in aggregates.items()
            }
            soc = _state_of_charge_series(
                history, configuration.state_of_charge, start, end
            )
            aligned_start, aligned_end, aligned, exclusions = self._align_history(
                series, soc, end
            )
            retrieved_at = self._as_utc(now or datetime.now(timezone.utc))
            latest_observation_at = min(
                [
                    item.latest_observation_at
                    for pair in series.values()
                    for item in pair
                ]
                + [soc.latest_observation_at]
            )
            return BatteryEfficiencyHistoryData(
                schema_version="1",
                start_time=aligned_start,
                interval_minutes=60,
                battery_energy_in_kwh=aligned["battery_in"],
                battery_energy_out_kwh=aligned["battery_out"],
                inverter_charge_energy_in_kwh=aligned["charge_in"],
                inverter_charge_energy_out_kwh=aligned["charge_out"],
                inverter_discharge_energy_in_kwh=aligned["discharge_in"],
                inverter_discharge_energy_out_kwh=aligned["discharge_out"],
                state_of_charge_percent=aligned["soc"],
                unit="kWh",
                source=SourceMetadata(
                    provider="home-assistant",
                    entity_id=BATTERY_EFFICIENCY_HISTORY_SOURCE_ID,
                ),
                retrieved_at=retrieved_at,
                latest_observation_at=min(latest_observation_at, aligned_end),
                exclusions=exclusions,
            )

        return HistoryPlan(needs=needs, build=build)

    def is_fresh(
        self,
        data: BatteryEfficiencyHistoryData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Apply the configured Home Assistant freshness threshold."""
        current = self._as_utc(now or datetime.now(timezone.utc))
        max_age = self.configuration.max_data_age_seconds
        return (
            max_age is None
            or (current - self._as_utc(data.latest_observation_at)).total_seconds()
            <= max_age
        )

    def _align_history(
        self,
        series: dict[str, tuple[HomeAssistantEnergySeries, HomeAssistantEnergySeries]],
        soc: _StateOfCharge,
        requested_end: datetime,
    ) -> tuple[
        datetime,
        datetime,
        dict[str, tuple[float | None, ...]],
        tuple[HourExclusion, ...],
    ]:
        """Align all legs and the state of charge and exclude hours across them.

        An hour excluded in any energy leg or in the state of charge is excluded
        in every component, so the efficiency ratios never mix valid and invalid
        legs. A state-of-charge value next to an excluded hour is dropped too.
        """
        energy = [value for pair in series.values() for value in pair]
        starts = [item.start_time for item in energy] + [soc.start_time]
        ends = [
            item.start_time + timedelta(hours=len(item.values_kw)) for item in energy
        ]
        ends.append(soc.start_time + timedelta(hours=len(soc.values) - 1))
        start = max(starts)
        end = min(min(ends), requested_end)
        count = int((end - start).total_seconds() // 3600)
        if count <= 0:
            raise HomeAssistantError(
                "battery efficiency histories have no aligned intervals"
            )

        sources: list[HomeAssistantEnergySeries | _StateOfCharge] = [*energy, soc]
        exclusions = merge_exclusions(
            *(
                [item for item in source.exclusions if start <= item.hour_start < end]
                for source in sources
            )
        )
        excluded = {int((item.hour_start - start) / _HOUR) for item in exclusions}

        def values(item: HomeAssistantEnergySeries) -> tuple[float | None, ...]:
            offset = int((start - item.start_time).total_seconds() // 3600)
            result = item.values_kw[offset : offset + count]
            if len(result) != count:
                raise HomeAssistantError(
                    "battery efficiency energy histories are misaligned"
                )
            return tuple(
                None if index in excluded else value
                for index, value in enumerate(result)
            )

        soc_offset = int((start - soc.start_time).total_seconds() // 3600)
        soc_values = soc.values[soc_offset : soc_offset + count + 1]
        if len(soc_values) != count + 1:
            raise HomeAssistantError(
                "battery efficiency state-of-charge history is misaligned"
            )
        adjacent = _soc_indexes_of(excluded)
        return (
            start,
            start + timedelta(hours=count),
            {
                "battery_in": values(series["battery"][0]),
                "battery_out": values(series["battery"][1]),
                "charge_in": values(series["inverter_charge"][0]),
                "charge_out": values(series["inverter_charge"][1]),
                "discharge_in": values(series["inverter_discharge"][0]),
                "discharge_out": values(series["inverter_discharge"][1]),
                "soc": tuple(
                    None if index in adjacent else value
                    for index, value in enumerate(soc_values)
                ),
            },
            exclusions,
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return as_utc(
            value,
            error_factory=HomeAssistantError,
            message="Home Assistant efficiency times must include a timezone",
        )


def _state_of_charge_series(
    history: HomeAssistantHistory,
    mapping: HomeAssistantBatteryEntityConfiguration,
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
    entity_id = mapping.entity_id
    records: dict[datetime, HistorySample] = {}
    for sample in history.window(entity_id, "state", start_time, end_time):
        records[sample.timestamp] = sample
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
    last_sample: HistorySample | None = None
    next_index = 0
    for index in range(hours + 1):
        boundary = effective_start + timedelta(hours=index)
        while next_index < len(ordered) and ordered[next_index][0] <= boundary:
            last_sample = ordered[next_index][1]
            next_index += 1
        if last_sample is None:  # pragma: no cover - effective_start guarantees this
            raise HomeAssistantError(
                "Home Assistant returned no state-of-charge observation "
                f"at or before {boundary.isoformat()} for {entity_id}"
            )
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
    expected_start = existing.start_time + timedelta(
        hours=len(existing.battery_energy_in_kwh)
    )
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
    unavailable: tuple[float | None, ...] = (None,) * gap_hours
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
    merged = replace(
        incoming,
        start_time=existing.start_time,
        battery_energy_in_kwh=(
            existing.battery_energy_in_kwh
            + unavailable
            + incoming.battery_energy_in_kwh
        ),
        battery_energy_out_kwh=(
            existing.battery_energy_out_kwh
            + unavailable
            + incoming.battery_energy_out_kwh
        ),
        inverter_charge_energy_in_kwh=(
            existing.inverter_charge_energy_in_kwh
            + unavailable
            + incoming.inverter_charge_energy_in_kwh
        ),
        inverter_charge_energy_out_kwh=(
            existing.inverter_charge_energy_out_kwh
            + unavailable
            + incoming.inverter_charge_energy_out_kwh
        ),
        inverter_discharge_energy_in_kwh=(
            existing.inverter_discharge_energy_in_kwh
            + unavailable
            + incoming.inverter_discharge_energy_in_kwh
        ),
        inverter_discharge_energy_out_kwh=(
            existing.inverter_discharge_energy_out_kwh
            + unavailable
            + incoming.inverter_discharge_energy_out_kwh
        ),
        state_of_charge_percent=state_of_charge,
        exclusions=merge_exclusions(existing.exclusions, gap, incoming.exclusions),
    )
    # A full import drops the state of charge around every excluded hour. The
    # incoming series cannot know that the last persisted hour is excluded, and
    # that hour brackets the boundary value the two series share.
    seam = len(existing.battery_energy_in_kwh)
    if any(
        item.hour_start == merged.start_time + (seam - 1) * _HOUR
        for item in existing.exclusions
    ):
        merged = replace(
            merged,
            state_of_charge_percent=tuple(
                None if index == seam else value
                for index, value in enumerate(merged.state_of_charge_percent)
            ),
        )
    return bound_battery_efficiency_history(merged)


def bound_battery_efficiency_history(
    data: BatteryEfficiencyHistoryData,
    max_hours: int = HOUSEHOLD_LOAD_MAX_VALUES,
) -> BatteryEfficiencyHistoryData:
    """Trim retained battery-efficiency history to the bounded retention window."""
    count = len(data.battery_energy_in_kwh)
    if count <= max_hours:
        return data
    offset = count - max_hours
    new_start = data.start_time + timedelta(hours=offset)
    return replace(
        data,
        start_time=new_start,
        battery_energy_in_kwh=data.battery_energy_in_kwh[offset:],
        battery_energy_out_kwh=data.battery_energy_out_kwh[offset:],
        inverter_charge_energy_in_kwh=data.inverter_charge_energy_in_kwh[offset:],
        inverter_charge_energy_out_kwh=data.inverter_charge_energy_out_kwh[offset:],
        inverter_discharge_energy_in_kwh=(
            data.inverter_discharge_energy_in_kwh[offset:]
        ),
        inverter_discharge_energy_out_kwh=(
            data.inverter_discharge_energy_out_kwh[offset:]
        ),
        state_of_charge_percent=data.state_of_charge_percent[offset:],
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
    arrays = {
        "battery input": history.battery_energy_in_kwh,
        "battery output": history.battery_energy_out_kwh,
        "charge input": history.inverter_charge_energy_in_kwh,
        "charge output": history.inverter_charge_energy_out_kwh,
        "discharge input": history.inverter_discharge_energy_in_kwh,
        "discharge output": history.inverter_discharge_energy_out_kwh,
    }
    count = len(history.battery_energy_in_kwh)
    warnings: list[str] = []
    component_statuses: dict[str, EfficiencyComponentStatus] = {}
    if (
        any(len(values) != count for values in arrays.values())
        or len(history.state_of_charge_percent) != count + 1
    ):
        return _result(
            history,
            retrieved_at,
            "invalid",
            warnings=("efficiency histories are not aligned",),
            component_statuses=_all_component_statuses("invalid"),
        )
    for name, values in arrays.items():
        if any(
            value is not None and (not math.isfinite(value) or value < 0)
            for value in values
        ):
            return _result(
                history,
                retrieved_at,
                "invalid",
                warnings=(f"{name} contains negative or non-finite values",),
                component_statuses=_all_component_statuses("invalid"),
            )
    if any(
        value is not None and (not math.isfinite(value) or value < 0)
        for value in history.state_of_charge_percent
    ):
        return _result(
            history,
            retrieved_at,
            "invalid",
            warnings=("state-of-charge history contains invalid values",),
            component_statuses=_all_component_statuses("invalid"),
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
        for left, right in zip(full_indices, full_indices[1:])
        if right > left
        and all(
            value is not None for value in history.battery_energy_in_kwh[left:right]
        )
    ]
    battery_in = sum(
        _total(history.battery_energy_in_kwh[left:right]) for left, right in cycles
    )
    battery_out = sum(
        _total(history.battery_energy_out_kwh[left:right]) for left, right in cycles
    )
    charge_in = _total(history.inverter_charge_energy_in_kwh)
    charge_out = _total(history.inverter_charge_energy_out_kwh)
    discharge_in = _total(history.inverter_discharge_energy_in_kwh)
    discharge_out = _total(history.inverter_discharge_energy_out_kwh)
    invalid = False
    battery_efficiency: float | None = None
    charge_efficiency: float | None = None
    discharge_efficiency: float | None = None
    if not cycles:
        component_statuses["battery_efficiency"] = "unavailable"
        warnings.append(
            "battery efficiency is unavailable: no complete full-SoC battery cycle "
            f"is available; using default efficiency ratio {DEFAULT_EFFICIENCY_RATIO}"
        )
    elif battery_in <= 0:
        invalid = True
        component_statuses["battery_efficiency"] = "invalid"
        warnings.append(
            "battery efficiency is invalid: zero throughput denominator; using "
            f"default efficiency ratio {DEFAULT_EFFICIENCY_RATIO}"
        )
    elif battery_in < configuration.minimum_battery_throughput_kwh:
        component_statuses["battery_efficiency"] = "defaulted"
        warnings.append(
            "battery efficiency uses default efficiency ratio "
            f"{DEFAULT_EFFICIENCY_RATIO}: throughput {battery_in:.3f} kWh is below "
            "the configured minimum"
        )
    else:
        try:
            battery_efficiency = _ratio(battery_out, battery_in, "battery")
            component_statuses["battery_efficiency"] = "calculated"
        except ValueError as error:
            invalid = True
            component_statuses["battery_efficiency"] = "invalid"
            warnings.append(str(error))

    if charge_in <= 0:
        invalid = True
        component_statuses["inverter_charge_efficiency"] = "invalid"
        warnings.append(
            "inverter charge efficiency is invalid: zero throughput denominator; "
            f"using default efficiency ratio {DEFAULT_EFFICIENCY_RATIO}"
        )
    elif charge_in < configuration.minimum_inverter_charge_throughput_kwh:
        component_statuses["inverter_charge_efficiency"] = "defaulted"
        warnings.append(
            "inverter charge efficiency uses default efficiency ratio "
            f"{DEFAULT_EFFICIENCY_RATIO}: throughput {charge_in:.3f} kWh is below "
            "the configured minimum"
        )
    else:
        try:
            charge_efficiency = _ratio(charge_out, charge_in, "inverter charge")
            component_statuses["inverter_charge_efficiency"] = "calculated"
        except ValueError as error:
            invalid = True
            component_statuses["inverter_charge_efficiency"] = "invalid"
            warnings.append(str(error))

    if discharge_in <= 0:
        invalid = True
        component_statuses["inverter_discharge_efficiency"] = "invalid"
        warnings.append(
            "inverter discharge efficiency is invalid: zero throughput denominator; "
            f"using default efficiency ratio {DEFAULT_EFFICIENCY_RATIO}"
        )
    elif discharge_in < configuration.minimum_inverter_discharge_throughput_kwh:
        component_statuses["inverter_discharge_efficiency"] = "defaulted"
        warnings.append(
            "inverter discharge efficiency uses default efficiency ratio "
            f"{DEFAULT_EFFICIENCY_RATIO}: throughput {discharge_in:.3f} kWh is below "
            "the configured minimum"
        )
    else:
        try:
            discharge_efficiency = _ratio(
                discharge_out, discharge_in, "inverter discharge"
            )
            component_statuses["inverter_discharge_efficiency"] = "calculated"
        except ValueError as error:
            invalid = True
            component_statuses["inverter_discharge_efficiency"] = "invalid"
            warnings.append(str(error))

    round_trip_efficiency = (
        (
            battery_efficiency
            if battery_efficiency is not None
            else DEFAULT_EFFICIENCY_RATIO
        )
        * (
            charge_efficiency
            if charge_efficiency is not None
            else DEFAULT_EFFICIENCY_RATIO
        )
        * (
            discharge_efficiency
            if discharge_efficiency is not None
            else DEFAULT_EFFICIENCY_RATIO
        )
    )
    component_statuses["round_trip_efficiency"] = "invalid" if invalid else "calculated"
    if not invalid and any(
        status in {"defaulted", "unavailable"}
        for name, status in component_statuses.items()
        if name != "round_trip_efficiency"
    ):
        component_statuses["round_trip_efficiency"] = "calculated_with_defaults"
        warnings.append(
            "complete round-trip efficiency is calculated using one or more "
            "default component ratios"
        )
    status = (
        "invalid"
        if invalid
        else (
            "insufficient_data"
            if any(
                value is None
                for value in (
                    battery_efficiency,
                    charge_efficiency,
                    discharge_efficiency,
                )
            )
            else "ok"
        )
    )
    _check_state_of_charge_balance(history, configuration, capacity_kwh, warnings)
    return _result(
        history,
        retrieved_at,
        status,
        inverter_charge_efficiency=charge_efficiency,
        inverter_discharge_efficiency=discharge_efficiency,
        battery_efficiency=battery_efficiency,
        round_trip_efficiency=round_trip_efficiency,
        battery_throughput_kwh=battery_in,
        charge_throughput_kwh=charge_in,
        discharge_throughput_kwh=discharge_in,
        complete_cycle_count=len(cycles),
        warnings=tuple(dict.fromkeys(warnings)),
        component_statuses=component_statuses,
    )


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


def _soc_indexes_of(excluded_hours: set[int]) -> set[int]:
    """Return the state-of-charge values that bracket the excluded hours.

    Hour ``i`` lies between ``state_of_charge_percent[i]`` and
    ``state_of_charge_percent[i + 1]``, so an excluded hour drops both.
    """
    return excluded_hours | {index + 1 for index in excluded_hours}


def _total(values: tuple[float | None, ...]) -> float:
    """Sum the hours that have a value; excluded hours contribute nothing."""
    return sum(value for value in values if value is not None)


def _ratio(numerator: float, denominator: float, label: str) -> float:
    if denominator <= 0:
        raise ValueError(f"{label} efficiency has a zero throughput denominator")
    value = numerator / denominator
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} efficiency is non-finite or negative")
    return min(value, 1.0)


def _select_history(
    history: BatteryEfficiencyHistoryData,
    history_start: datetime | None,
) -> BatteryEfficiencyHistoryData:
    """Apply the configured inclusive start without introducing a rolling window."""
    if history_start is None:
        return history
    selected_start = max(
        history.start_time,
        history_start.astimezone(timezone.utc),
    )
    offset = int((selected_start - history.start_time).total_seconds() // 3600)
    count = len(history.battery_energy_in_kwh) - offset
    if count <= 0:
        return replace(
            history,
            start_time=selected_start,
            battery_energy_in_kwh=(),
            battery_energy_out_kwh=(),
            inverter_charge_energy_in_kwh=(),
            inverter_charge_energy_out_kwh=(),
            inverter_discharge_energy_in_kwh=(),
            inverter_discharge_energy_out_kwh=(),
            state_of_charge_percent=(),
            exclusions=(),
        )
    return replace(
        history,
        start_time=history.start_time + timedelta(hours=offset),
        battery_energy_in_kwh=history.battery_energy_in_kwh[offset:],
        battery_energy_out_kwh=history.battery_energy_out_kwh[offset:],
        inverter_charge_energy_in_kwh=history.inverter_charge_energy_in_kwh[offset:],
        inverter_charge_energy_out_kwh=history.inverter_charge_energy_out_kwh[offset:],
        inverter_discharge_energy_in_kwh=history.inverter_discharge_energy_in_kwh[
            offset:
        ],
        inverter_discharge_energy_out_kwh=history.inverter_discharge_energy_out_kwh[
            offset:
        ],
        state_of_charge_percent=history.state_of_charge_percent[offset:],
        exclusions=tuple(
            item
            for item in history.exclusions
            if item.hour_start >= history.start_time + timedelta(hours=offset)
        ),
    )


def _result(
    history: BatteryEfficiencyHistoryData,
    retrieved_at: datetime,
    status: str,
    *,
    inverter_charge_efficiency: float | None = None,
    inverter_discharge_efficiency: float | None = None,
    battery_efficiency: float | None = None,
    round_trip_efficiency: float | None = None,
    battery_throughput_kwh: float = 0.0,
    charge_throughput_kwh: float = 0.0,
    discharge_throughput_kwh: float = 0.0,
    complete_cycle_count: int = 0,
    warnings: tuple[str, ...] = (),
    component_statuses: dict[str, EfficiencyComponentStatus] | None = None,
) -> BatteryEfficiencyData:
    """Build a consistently shaped result for valid and unusable histories."""
    components = {
        "inverter_charge_efficiency": inverter_charge_efficiency,
        "inverter_discharge_efficiency": inverter_discharge_efficiency,
        "battery_efficiency": battery_efficiency,
        "round_trip_efficiency": round_trip_efficiency,
    }
    resolved_statuses = component_statuses or {
        name: "calculated" if value is not None else "defaulted"
        for name, value in components.items()
    }
    defaulted_components = tuple(
        name
        for name, status in resolved_statuses.items()
        if status in {"defaulted", "unavailable", "invalid"}
    )
    return BatteryEfficiencyData(
        schema_version="1",
        status=status,  # type: ignore[arg-type]
        inverter_charge_efficiency=(
            inverter_charge_efficiency
            if inverter_charge_efficiency is not None
            else DEFAULT_EFFICIENCY_RATIO
        ),
        inverter_discharge_efficiency=(
            inverter_discharge_efficiency
            if inverter_discharge_efficiency is not None
            else DEFAULT_EFFICIENCY_RATIO
        ),
        battery_efficiency=(
            battery_efficiency
            if battery_efficiency is not None
            else DEFAULT_EFFICIENCY_RATIO
        ),
        round_trip_efficiency=(
            round_trip_efficiency
            if round_trip_efficiency is not None
            else DEFAULT_EFFICIENCY_RATIO
        ),
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
        defaulted_components=defaulted_components,
        component_statuses=resolved_statuses,
    )


def _all_component_statuses(
    status: EfficiencyComponentStatus,
) -> dict[str, EfficiencyComponentStatus]:
    """Apply one validation status when no component can be calculated."""
    return {
        "battery_efficiency": status,
        "inverter_charge_efficiency": status,
        "inverter_discharge_efficiency": status,
        "round_trip_efficiency": status,
    }


__all__ = [
    "HomeAssistantBatteryEfficiencyImporter",
    "bound_battery_efficiency_history",
    "calculate_battery_efficiency",
    "merge_battery_efficiency_history",
]
