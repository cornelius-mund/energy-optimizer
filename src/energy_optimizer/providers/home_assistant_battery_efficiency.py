"""Measured battery and inverter efficiency calculation."""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from energy_optimizer.config import (
    HomeAssistantBatteryEfficiencyConfiguration,
    HomeAssistantConfiguration,
)
from energy_optimizer.providers.home_assistant_energy import (
    HOME_ASSISTANT_HISTORY_CHUNK,
    HomeAssistantEnergyAggregator,
    HomeAssistantEnergySeries,
    HomeAssistantError,
)
from energy_optimizer.providers.http import JsonHttpClient
from energy_optimizer.providers.interfaces import (
    BATTERY_EFFICIENCY_HISTORY_SOURCE_ID,
    BATTERY_EFFICIENCY_SOURCE_ID,
    DEFAULT_EFFICIENCY_RATIO,
    HOUSEHOLD_LOAD_MAX_VALUES,
    BatteryEfficiencyData,
    BatteryEfficiencyHistoryData,
    EfficiencyComponentStatus,
    IntervalQuality,
    SourceMetadata,
)
from energy_optimizer.providers.normalization import (
    align_to_next_hour,
    as_utc,
    parse_aware_timestamp,
)

logger = logging.getLogger(__name__)


class HomeAssistantBatteryEfficiencyImporter:
    """Retrieve the aligned measured history needed by the calculator."""

    def __init__(
        self,
        configuration: HomeAssistantConfiguration,
        client: httpx.Client | None = None,
    ) -> None:
        if (
            configuration.battery is None
            or configuration.battery.efficiency_calculation is None
        ):
            raise HomeAssistantError(
                "Home Assistant calculated battery efficiency is not configured"
            )
        self.configuration = configuration
        self.efficiency_configuration = configuration.battery.efficiency_calculation
        self._aggregator = HomeAssistantEnergyAggregator(configuration, client)
        self._http = JsonHttpClient(client)

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime,
        *,
        now: datetime | None = None,
    ) -> BatteryEfficiencyHistoryData:
        """Fetch all configured expressions for one complete history range."""
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
        series: dict[
            str, tuple[HomeAssistantEnergySeries, HomeAssistantEnergySeries]
        ] = {}
        for name, leg in legs.items():
            series[name] = (
                self._aggregator.aggregate(
                    leg.energy_in,
                    start,
                    end,
                    label=f"battery efficiency {name} input",
                    allow_negative=True,
                ),
                self._aggregator.aggregate(
                    leg.energy_out,
                    start,
                    end,
                    label=f"battery efficiency {name} output",
                    allow_negative=True,
                ),
            )
        soc = self._fetch_state_of_charge(
            configuration.state_of_charge,
            start,
            end + timedelta(hours=1),
        )
        aligned_start, aligned_end, aligned, quality = self._align_history(
            series, soc, end
        )
        retrieved_at = self._as_utc(now or datetime.now(timezone.utc))
        latest_observation_at = min(
            [item.latest_observation_at for pair in series.values() for item in pair]
            + [soc[2]]
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
            quality=quality,
        )

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

    def _fetch_state_of_charge(
        self,
        mapping: Any,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[datetime, tuple[float, ...], datetime]:
        records: dict[datetime, float] = {}
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + HOME_ASSISTANT_HISTORY_CHUNK, end_time)
            url = self._history_url(chunk_start, chunk_end, mapping.entity_id)
            payload = self._http.get_home_assistant_json(
                url,
                token=self.configuration.token.get_secret_value(),
                timeout_seconds=self.configuration.timeout_seconds,
                error_factory=HomeAssistantError,
                not_found_message=(
                    f"Home Assistant state-of-charge history was not found for "
                    f"{mapping.entity_id}"
                ),
                status_message=lambda status: (
                    f"Home Assistant returned HTTP {status} while retrieving state-of-"
                    "charge history"
                ),
                timeout_message=(
                    "Home Assistant request timed out while retrieving "
                    "state-of-charge history"
                ),
                transport_message=(
                    "Home Assistant request failed while retrieving "
                    "state-of-charge history"
                ),
                malformed_message=(
                    "Home Assistant returned malformed state-of-charge history"
                ),
                log_event="home_assistant_history_request",
                component="home_assistant",
                operation="history_request",
                success_log_level=logging.DEBUG,
                error_log_level=logging.DEBUG,
            )
            if not isinstance(payload, list):
                raise HomeAssistantError(
                    f"Home Assistant state-of-charge history for {mapping.entity_id} "
                    "must contain one entity series"
                )
            if not payload or (len(payload) == 1 and payload[0] == []):
                chunk_start = chunk_end
                continue
            if len(payload) != 1 or not isinstance(payload[0], list):
                raise HomeAssistantError(
                    f"Home Assistant state-of-charge history for {mapping.entity_id} "
                    "must contain one entity series"
                )
            for record in payload[0]:
                if not isinstance(record, dict):
                    raise HomeAssistantError(
                        "state-of-charge history contains an invalid record"
                    )
                timestamp = parse_aware_timestamp(
                    record.get("last_updated", record.get("last_changed")),
                    error_factory=HomeAssistantError,
                    missing_message="state-of-charge history has a missing timestamp",
                    invalid_message=lambda raw: (
                        f"invalid state-of-charge timestamp: {raw!r}"
                    ),
                    naive_message="state-of-charge timestamps must include a timezone",
                )
                raw_state = record.get("state")
                if isinstance(raw_state, str) and raw_state.strip().lower() in {
                    "unknown",
                    "unavailable",
                }:
                    continue
                try:
                    value = float(raw_state)  # type: ignore[arg-type]
                except (TypeError, ValueError) as error:
                    raise HomeAssistantError(
                        f"state-of-charge entity {mapping.entity_id} returned a "
                        "non-numeric value"
                    ) from error
                if mapping.unit in {"Wh", "kWh"}:
                    unit = (
                        record.get("attributes", {}).get("unit_of_measurement")
                        if isinstance(record.get("attributes"), dict)
                        else None
                    )
                    if unit not in {"Wh", "kWh"}:
                        raise HomeAssistantError(
                            f"state-of-charge entity {mapping.entity_id} has an "
                            "incompatible unit"
                        )
                    if unit == "Wh":
                        value /= 1000
                if not math.isfinite(value) or value < 0:
                    raise HomeAssistantError(
                        f"state-of-charge entity {mapping.entity_id} returned an "
                        "invalid value"
                    )
                records[timestamp] = value
            chunk_start = chunk_end
        if not records:
            raise HomeAssistantError(
                "Home Assistant returned no usable state-of-charge history for "
                f"{mapping.entity_id}"
            )
        effective_start = align_to_next_hour(min(records))
        hours = int((end_time - effective_start).total_seconds() // 3600)
        if hours <= 0:
            raise HomeAssistantError(
                "Home Assistant returned no complete state-of-charge history for "
                f"{mapping.entity_id}"
            )
        # Home Assistant's history API only returns a row when an entity's
        # state changes, so an hour with no row does not mean the value is
        # missing; it means the value has not changed since the previous
        # observation. Carry the most recent known value forward for each
        # hour instead of requiring a fresh row in every bucket.
        sorted_records = sorted(records.items())
        values: list[float] = []
        last_value: float | None = None
        next_index = 0
        for index in range(hours):
            boundary = effective_start + timedelta(hours=index + 1)
            while (
                next_index < len(sorted_records)
                and sorted_records[next_index][0] < boundary
            ):
                last_value = sorted_records[next_index][1]
                next_index += 1
            if last_value is None:  # pragma: no cover - effective_start guarantees this
                raise HomeAssistantError(
                    "Home Assistant returned no state-of-charge observation "
                    f"before {boundary.isoformat()} for {mapping.entity_id}"
                )
            values.append(last_value)
        latest = max(records)
        return effective_start, tuple(values), latest

    def _align_history(
        self,
        series: dict[str, tuple[HomeAssistantEnergySeries, HomeAssistantEnergySeries]],
        soc: tuple[datetime, tuple[float, ...], datetime],
        requested_end: datetime,
    ) -> tuple[
        datetime,
        datetime,
        dict[str, tuple[float, ...]],
        tuple[IntervalQuality, ...],
    ]:
        starts = [item.start_time for pair in series.values() for item in pair] + [
            soc[0]
        ]
        ends = [
            item.start_time + timedelta(hours=len(item.values_kw))
            for pair in series.values()
            for item in pair
        ]
        ends.append(soc[0] + timedelta(hours=len(soc[1]) - 1))
        start = max(starts)
        end = min(min(ends), requested_end)
        count = int((end - start).total_seconds() // 3600)
        if count <= 0:
            raise HomeAssistantError(
                "battery efficiency histories have no aligned intervals"
            )

        def values(item: HomeAssistantEnergySeries) -> tuple[float, ...]:
            offset = int((start - item.start_time).total_seconds() // 3600)
            result = item.values_kw[offset : offset + count]
            if len(result) != count:
                raise HomeAssistantError(
                    "battery efficiency energy histories are misaligned"
                )
            return result

        # A reset, spike, or recovery transition on any leg already contributes
        # zero energy for its hour (see home_assistant_energy.py); it must not
        # block persistence of the surrounding, unaffected hours. Instead, the
        # suspect status is carried onto the calculated result so orchestration
        # can flag the run without losing incremental progress.
        quality = [IntervalQuality()] * count
        for item in [value for pair in series.values() for value in pair]:
            offset = int((start - item.start_time).total_seconds() // 3600)
            for index, interval in enumerate(item.quality[offset : offset + count]):
                if interval.status == "suspect":
                    quality[index] = interval

        soc_offset = int((start - soc[0]).total_seconds() // 3600)
        soc_values = soc[1][soc_offset : soc_offset + count + 1]
        if len(soc_values) != count + 1:
            raise HomeAssistantError(
                "battery efficiency state-of-charge history is misaligned"
            )
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
                "soc": tuple(soc_values),
            },
            tuple(quality),
        )

    def _history_url(
        self, start_time: datetime, end_time: datetime, entity_id: str
    ) -> str:
        base_url = str(self.configuration.base_url).rstrip("/")
        return (
            f"{base_url}/api/history/period/{quote(start_time.isoformat(), safe='')}"
            f"?end_time={quote(end_time.isoformat(), safe='')}"
            f"&filter_entity_id={quote(entity_id, safe='')}"
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return as_utc(
            value,
            error_factory=HomeAssistantError,
            message="Home Assistant efficiency times must include a timezone",
        )


def _full_quality(data: BatteryEfficiencyHistoryData) -> tuple[IntervalQuality, ...]:
    """Return one quality entry per hour, defaulting missing entries to valid.

    Test fixtures and older data may omit ``quality`` entirely (the field
    defaults to an empty tuple); treat that as "no known anomalies" so
    concatenation stays aligned with the energy arrays.
    """
    count = len(data.battery_energy_in_kwh)
    if len(data.quality) == count:
        return data.quality
    return (IntervalQuality(),) * count


def merge_battery_efficiency_history(
    existing: BatteryEfficiencyHistoryData | None,
    incoming: BatteryEfficiencyHistoryData,
) -> BatteryEfficiencyHistoryData:
    """Extend persisted battery-efficiency history with newly fetched hours.

    ``incoming`` must start exactly where ``existing`` ends, so ingestion only
    ever needs to request the missing hours from Home Assistant instead of
    re-fetching the complete retained history on every scheduled run.
    """
    if existing is None:
        return bound_battery_efficiency_history(incoming)
    expected_start = existing.start_time + timedelta(
        hours=len(existing.battery_energy_in_kwh)
    )
    if incoming.start_time != expected_start:
        raise HomeAssistantError(
            "battery efficiency history is not contiguous with the persisted history"
        )
    merged = replace(
        incoming,
        start_time=existing.start_time,
        battery_energy_in_kwh=(
            existing.battery_energy_in_kwh + incoming.battery_energy_in_kwh
        ),
        battery_energy_out_kwh=(
            existing.battery_energy_out_kwh + incoming.battery_energy_out_kwh
        ),
        inverter_charge_energy_in_kwh=(
            existing.inverter_charge_energy_in_kwh
            + incoming.inverter_charge_energy_in_kwh
        ),
        inverter_charge_energy_out_kwh=(
            existing.inverter_charge_energy_out_kwh
            + incoming.inverter_charge_energy_out_kwh
        ),
        inverter_discharge_energy_in_kwh=(
            existing.inverter_discharge_energy_in_kwh
            + incoming.inverter_discharge_energy_in_kwh
        ),
        inverter_discharge_energy_out_kwh=(
            existing.inverter_discharge_energy_out_kwh
            + incoming.inverter_discharge_energy_out_kwh
        ),
        # The incoming SoC series repeats the boundary sample already recorded
        # as the existing series' last value; keep it only once.
        state_of_charge_percent=(
            existing.state_of_charge_percent[:-1] + incoming.state_of_charge_percent
        ),
        quality=_full_quality(existing) + _full_quality(incoming),
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
    quality = _full_quality(data)
    return replace(
        data,
        start_time=data.start_time + timedelta(hours=offset),
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
        quality=quality[offset:],
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
        if any(not math.isfinite(value) or value < 0 for value in values):
            return _result(
                history,
                retrieved_at,
                "invalid",
                warnings=(f"{name} contains negative or non-finite values",),
                component_statuses=_all_component_statuses("invalid"),
            )
    if any(
        not math.isfinite(value) or value < 0
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
        if value < configuration.full_soc_threshold_percent:
            was_below_full = True
        elif was_below_full:
            full_indices.append(index)
            was_below_full = False
    cycles = [
        (left, right)
        for left, right in zip(full_indices, full_indices[1:])
        if right > left
    ]
    battery_in = sum(
        sum(history.battery_energy_in_kwh[left:right]) for left, right in cycles
    )
    battery_out = sum(
        sum(history.battery_energy_out_kwh[left:right]) for left, right in cycles
    )
    charge_in = sum(history.inverter_charge_energy_in_kwh)
    charge_out = sum(history.inverter_charge_energy_out_kwh)
    discharge_in = sum(history.inverter_discharge_energy_in_kwh)
    discharge_out = sum(history.inverter_discharge_energy_out_kwh)
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
        soc_delta = (
            (
                history.state_of_charge_percent[index + 1]
                - history.state_of_charge_percent[index]
            )
            / 100
            * capacity_kwh
        )
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
            quality=(),
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
        quality=_full_quality(history)[offset:],
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
        quality=history.quality,
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
