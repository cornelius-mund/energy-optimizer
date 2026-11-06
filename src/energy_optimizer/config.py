"""Loading and validation of runtime configuration."""

import logging
import math
import re
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, available_timezones

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from energy_optimizer.appliances import ApplianceCapabilities
from energy_optimizer.providers.interfaces import (
    BATTERY_SOURCE_ID,
    GRID_FLOW_SOURCE_ID,
    HOUSEHOLD_LOAD_SOURCE_ID,
    PV_GENERATION_SOURCE_ID,
)

logger = logging.getLogger(__name__)


class ConfigurationError(ValueError):
    """Raised when the runtime configuration cannot be used."""


@lru_cache(maxsize=None)
def _timezone_name_error(name: str) -> str | None:
    """Explain why a time zone cannot be used for dashboard times, if it cannot.

    A name is matched exactly against the IANA names known to this system, so
    the result does not depend on whether the file system ignores case and a
    path-like value can never reach the file system. The dashboard API accepts
    only UTC-hour-aligned ranges, so the offset must be a whole number of hours
    at every hour of the current year.
    """
    if name not in available_timezones():
        return (
            f"{name!r} is not a known IANA time zone name; use the exact, "
            "case-sensitive name, for example Europe/Berlin"
        )
    zone = ZoneInfo(name)
    year = datetime.now(UTC).year
    instant = datetime(year, 1, 1, tzinfo=UTC)
    while instant.year == year:
        offset = instant.astimezone(zone).utcoffset()
        if offset is None or offset % timedelta(hours=1):
            return (
                f"{name} has a UTC offset that is not a whole number of hours "
                f"(found {offset}); only zones whose offset is always a whole "
                "number of hours are supported, for example Europe/Berlin"
            )
        instant += timedelta(hours=1)
    return None


class _StrictModel(BaseModel):
    """Base of the settings models: unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")


class GridConfiguration(_StrictModel):
    """Limits for energy exchanged with the grid, in kW."""

    maximum_import_kw: float = Field(gt=0)
    maximum_export_kw: float = Field(gt=0)


class SolverConfiguration(_StrictModel):
    name: str = Field(min_length=1)
    time_limit_seconds: float = Field(gt=0)


class HomeAssistantEnergyEntityConfiguration(_StrictModel):
    """Configuration for one Home Assistant cumulative-energy entity."""

    entity_id: str = Field(min_length=1, max_length=255)
    state_class: Literal["total", "total_increasing"]
    unit: Literal["Wh", "kWh", "MWh"]
    # The most energy this entity may report in one hour and in one counter step.
    # A larger value excludes the hour instead of importing it.
    maximum_interval_energy_kwh: float = Field(default=100, gt=0)


class HomeAssistantBatteryEntityConfiguration(_StrictModel):
    """Configuration for one instantaneous Home Assistant battery value."""

    entity_id: str = Field(min_length=1, max_length=255)
    unit: Literal["%", "Wh", "kWh", "W", "kW", "ratio"]
    attribute: str | None = Field(default=None, min_length=1, max_length=255)


class BatteryConstantConfiguration(_StrictModel):
    """A static battery value that does not require a Home Assistant entity."""

    value: float = Field(allow_inf_nan=False)
    unit: Literal["%", "Wh", "kWh", "W", "kW", "ratio"]

    @field_validator("value", mode="before")
    @classmethod
    def reject_boolean_values(cls, value: object) -> object:
        """Do not treat YAML booleans as numeric installation parameters."""
        if isinstance(value, bool):
            raise ValueError("battery constants must be numeric")
        return value


type BatteryConfigurationValue = (
    HomeAssistantBatteryEntityConfiguration | BatteryConstantConfiguration
)


class EnergyTermConfiguration(_StrictModel):
    """Cumulative-energy entities that share one sign in an aggregation."""

    operation: Literal["add", "subtract"]
    entities: list[HomeAssistantEnergyEntityConfiguration] = Field(min_length=1)


class EnergyAggregateConfiguration(_StrictModel):
    """One signed hourly energy aggregation built from cumulative counters.

    The hourly value is the sum of the energy of every ``add`` entity minus the
    sum of the energy of every ``subtract`` entity. ``part`` decides what a
    negative sum means: with ``net`` it is invalid data and excludes the hour,
    with ``positive`` it is a legitimate zero, because only the positive part of
    the sum is wanted.
    """

    part: Literal["net", "positive"] = "net"
    terms: list[EnergyTermConfiguration] = Field(min_length=1)

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Return the entity IDs of all terms in configuration order."""
        return tuple(
            entity.entity_id for term in self.terms for entity in term.entities
        )

    @model_validator(mode="after")
    def validate_terms(self) -> "EnergyAggregateConfiguration":
        """Allow one term per operation and each entity once per aggregation."""
        operations = [term.operation for term in self.terms]
        if len(operations) != len(set(operations)):
            raise ValueError(
                "an energy aggregation must not contain more than one term for "
                "the same operation"
            )
        entity_ids = self.entity_ids
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("energy aggregation entities must not contain duplicates")
        return self


class BatteryEfficiencyLegConfiguration(_StrictModel):
    """Signed cumulative-energy aggregations making up one efficiency leg."""

    energy_in: EnergyAggregateConfiguration
    energy_out: EnergyAggregateConfiguration

    @model_validator(mode="after")
    def warn_about_entity_reuse(self) -> "BatteryEfficiencyLegConfiguration":
        """Warn when one entity contributes to both sides of the leg."""
        if set(self.energy_in.entity_ids) & set(self.energy_out.entity_ids):
            logger.warning(
                "event=configuration_efficiency_entity_reuse "
                "component=configuration reason=entity_used_on_both_sides"
            )
        return self


class HomeAssistantBatteryEfficiencyConfiguration(_StrictModel):
    battery: BatteryEfficiencyLegConfiguration
    inverter_charge: BatteryEfficiencyLegConfiguration
    inverter_discharge: BatteryEfficiencyLegConfiguration
    state_of_charge: HomeAssistantBatteryEntityConfiguration
    history_start: datetime | None = None
    full_soc_threshold_percent: float = Field(default=100, gt=0, le=100)
    minimum_battery_throughput_kwh: float = Field(default=0.1, gt=0)
    minimum_inverter_charge_throughput_kwh: float = Field(default=0.1, gt=0)
    minimum_inverter_discharge_throughput_kwh: float = Field(default=0.1, gt=0)
    soc_balance_tolerance_kwh: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def validate_history_start(self) -> "HomeAssistantBatteryEfficiencyConfiguration":
        """Require an unambiguous history boundary when one is configured."""
        if self.history_start is not None and (
            self.history_start.tzinfo is None or self.history_start.utcoffset() is None
        ):
            raise ValueError(
                "battery.efficiency_calculation.history_start must include a timezone"
            )
        if self.state_of_charge.unit != "%":
            raise ValueError(
                "battery.efficiency_calculation.state_of_charge must use %"
            )
        return self


class HomeAssistantBatteryConfiguration(_StrictModel):
    """Home Assistant battery state and static capability configuration."""

    state_of_charge: HomeAssistantBatteryEntityConfiguration
    capacity: BatteryConfigurationValue
    minimum_soc: BatteryConfigurationValue
    maximum_soc: BatteryConfigurationValue
    maximum_charge: BatteryConfigurationValue
    maximum_discharge: BatteryConfigurationValue
    battery_efficiency: BatteryConfigurationValue | None = None
    efficiency_calculation: HomeAssistantBatteryEfficiencyConfiguration | None = None
    throughput_cost_eur_per_kwh: float = Field(
        default=0,
        ge=0,
        le=10,
        description="Wear cost of each kWh the battery delivers, in EUR",
    )
    end_of_horizon: Literal["restore_initial", "value_stored_energy"] = Field(
        default="restore_initial",
        description="restore_initial ends every plan at least as full as it began; "
        "value_stored_energy lets it end emptier when the energy is worth less "
        "than using it now",
    )
    end_of_horizon_value_eur_per_kwh: float | None = Field(
        default=None,
        ge=0,
        le=10,
        description="Value of a kWh left in the battery when end_of_horizon is "
        "value_stored_energy; defaults to the mean import price of the plan",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_numeric_constants(cls, values: Any) -> Any:
        """Allow static battery values as numbers in their canonical units."""
        if not isinstance(values, dict):
            return values
        defaults = {
            "capacity": "kWh",
            "minimum_soc": "%",
            "maximum_soc": "%",
            "maximum_charge": "kW",
            "maximum_discharge": "kW",
            "battery_efficiency": "ratio",
        }
        normalized = dict(values)
        for name, unit in defaults.items():
            value = normalized.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                normalized[name] = {"value": value, "unit": unit}
        return normalized

    @model_validator(mode="after")
    def validate_mapping_units(self) -> "HomeAssistantBatteryConfiguration":
        """Require units compatible with each normalized battery field."""
        for name, value in {
            "state_of_charge": self.state_of_charge,
            "minimum_soc": self.minimum_soc,
            "maximum_soc": self.maximum_soc,
        }.items():
            if value.unit not in {"%", "Wh", "kWh"}:
                raise ValueError(
                    f"battery.{name} must use %, Wh, or kWh; got {value.unit}"
                )
        if self.capacity.unit not in {"Wh", "kWh"}:
            raise ValueError("battery.capacity must use Wh or kWh")
        for name, value in {
            "maximum_charge": self.maximum_charge,
            "maximum_discharge": self.maximum_discharge,
        }.items():
            if value.unit not in {"W", "kW"}:
                raise ValueError(f"battery.{name} must use W or kW")
        efficiency = self.battery_efficiency
        if efficiency is not None and efficiency.unit not in {"%", "ratio"}:
            raise ValueError("battery.battery_efficiency must use % or ratio")
        if self.efficiency_calculation is not None and efficiency is not None:
            logger.warning(
                "event=configuration_efficiency_precedence "
                "component=configuration field=battery.battery_efficiency "
                "precedence=fixed_over_calculated"
            )
        if self.efficiency_calculation is None and efficiency is None:
            raise ValueError(
                "battery.battery_efficiency must be configured unless "
                "efficiency_calculation is configured"
            )
        self._validate_constants()
        return self

    def _validate_constants(self) -> None:
        """Validate static values that are available before provider fetch."""
        constants = {
            name: value
            for name, value in self.__dict__.items()
            if isinstance(value, BatteryConstantConfiguration)
        }
        for name, value in constants.items():
            if not math.isfinite(value.value):
                raise ValueError(f"battery.{name} must be finite")

        for name in ("capacity", "maximum_charge", "maximum_discharge"):
            constant = constants.get(name)
            if constant is not None and constant.value <= 0:
                raise ValueError(f"battery.{name} must be greater than zero")

        for name in ("minimum_soc", "maximum_soc"):
            constant = constants.get(name)
            if constant is None:
                continue
            if constant.value < 0:
                raise ValueError(f"battery.{name} must be non-negative")
            if constant.unit == "%" and constant.value > 100:
                raise ValueError(f"battery.{name} percentage must not exceed 100")

        constant = constants.get("battery_efficiency")
        if constant is not None:
            normalized = (
                constant.value / 100 if constant.unit == "%" else constant.value
            )
            if not 0 < normalized <= 1:
                raise ValueError(
                    "battery.battery_efficiency must be greater than zero and no "
                    "greater than one"
                )

        capacity = self._constant_energy(constants.get("capacity"))
        minimum_soc = self._constant_energy(constants.get("minimum_soc"), capacity)
        maximum_soc = self._constant_energy(constants.get("maximum_soc"), capacity)
        if (
            minimum_soc is not None
            and maximum_soc is not None
            and minimum_soc > maximum_soc
        ):
            raise ValueError("battery.minimum_soc must not exceed maximum_soc")
        if maximum_soc is not None and capacity is not None and maximum_soc > capacity:
            raise ValueError("battery.maximum_soc must not exceed capacity")

    @staticmethod
    def _constant_energy(
        value: BatteryConstantConfiguration | None, capacity: float | None = None
    ) -> float | None:
        if value is None:
            return None
        if value.unit == "%":
            return None if capacity is None else value.value / 100 * capacity
        return value.value / 1000 if value.unit == "Wh" else value.value


class HomeAssistantEvPlugConfiguration(_StrictModel):
    """The entity that tells whether the car is connected to the charger."""

    entity_id: str = Field(min_length=1, max_length=255)
    plugged_in_states: list[str] = Field(default_factory=lambda: ["on"], min_length=1)


class HomeAssistantEvServiceConfiguration(_StrictModel):
    """A Home Assistant service call that sets the charge power of the wall box."""

    service: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")
    entity_id: str = Field(min_length=1, max_length=255)
    data_field: str = Field(default="value", min_length=1)
    unit: Literal["A", "W", "kW"] = "A"
    phases: int = Field(default=3, ge=1, le=3)
    voltage: float = Field(default=230, gt=0)


class HomeAssistantEvControlConfiguration(_StrictModel):
    """Opt-in execution of the plan on the wall box.

    Without a plan for the running hour, or with a plan older than
    ``max_plan_age_seconds``, the charger is handed back to its own behaviour:
    enabled at the maximum power.
    """

    enabled: bool = False
    set_power: HomeAssistantEvServiceConfiguration
    enable_entity_id: str | None = Field(default=None, min_length=1, max_length=255)
    max_plan_age_seconds: float = Field(default=7200, gt=0)
    poll_interval_seconds: float = Field(default=30, gt=0)


class HomeAssistantEvConfiguration(_StrictModel):
    """The car's state of charge and plug state in Home Assistant plus its charger."""

    state_of_charge: HomeAssistantBatteryEntityConfiguration
    plugged_in: HomeAssistantEvPlugConfiguration
    capacity_kwh: float = Field(gt=0)
    minimum_charge_kw: float = Field(gt=0)
    maximum_charge_kw: float = Field(gt=0)
    charge_efficiency: float = Field(default=0.9, gt=0, le=1)
    control: HomeAssistantEvControlConfiguration | None = None

    @model_validator(mode="after")
    def validate_units_and_power(self) -> "HomeAssistantEvConfiguration":
        if self.state_of_charge.unit not in {"%", "kWh"}:
            raise ValueError("ev.state_of_charge must use % or kWh")
        if self.minimum_charge_kw > self.maximum_charge_kw:
            raise ValueError("ev.minimum_charge_kw must not exceed maximum_charge_kw")
        return self


type Weekday = Literal[
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
]
WEEKDAYS: tuple[Weekday, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class EvDayTargetConfiguration(_StrictModel):
    """The state of charge the car must hold at a local time on one weekday."""

    minimum_soc_percent: float = Field(gt=0, le=100)
    deadline: time


class EvChargingConfiguration(_StrictModel):
    """What the owner wants from the car: a state of charge at a local time."""

    strategy: Literal["deadline", "immediate", "surplus", "minimum_plus_target"] = (
        "deadline"
    )
    minimum_soc_percent: float | None = Field(
        default=None,
        gt=0,
        le=100,
        description=(
            "State of charge to reach by the deadline; with `immediate` and "
            "`surplus` the car charges at most to it (default 100)"
        ),
    )
    deadline: time | None = Field(
        default=None,
        description="Local time of day by which the car must hold the state of charge",
    )
    weekly: dict[Weekday, EvDayTargetConfiguration] = Field(
        default_factory=dict,
        description=(
            "Deadline and minimum state of charge per weekday; they replace the "
            "general `deadline` and `minimum_soc_percent` on the days they name"
        ),
    )
    target_soc_percent: float | None = Field(
        default=None,
        gt=0,
        le=100,
        description=(
            "`minimum_plus_target`: the car is charged above the minimum up to this "
            "state of charge while energy costs less than `value_eur_per_kwh`"
        ),
    )
    value_eur_per_kwh: float | None = Field(
        default=None,
        ge=0,
        description="`minimum_plus_target`: value of a stored kWh above the minimum",
    )

    @model_validator(mode="after")
    def validate_deadline(self) -> "EvChargingConfiguration":
        has_general_target = (
            self.deadline is not None and self.minimum_soc_percent is not None
        )
        if (self.deadline is None) != (self.minimum_soc_percent is None) and (
            self.strategy in ("deadline", "minimum_plus_target")
        ):
            raise ValueError(
                "ev_charging.deadline and minimum_soc_percent must be set together"
            )
        if (
            self.strategy in ("deadline", "minimum_plus_target")
            and not has_general_target
            and not self.weekly
        ):
            raise ValueError(
                "ev_charging.deadline and minimum_soc_percent or a weekly schedule "
                f"are required for the {self.strategy} strategy"
            )
        if self.strategy == "minimum_plus_target":
            self._validate_soft_target()
        return self

    def _validate_soft_target(self) -> None:
        if self.target_soc_percent is None or self.value_eur_per_kwh is None:
            raise ValueError(
                "ev_charging.target_soc_percent and value_eur_per_kwh are required "
                "for the minimum_plus_target strategy"
            )
        highest_minimum = max(
            [self.minimum_soc_percent or 0.0]
            + [day.minimum_soc_percent for day in self.weekly.values()]
        )
        if self.target_soc_percent < highest_minimum:
            raise ValueError(
                "ev_charging.target_soc_percent must not be below any minimum"
            )

    def day_targets(self) -> dict[int, tuple[float, time]]:
        """Minimum state of charge and deadline per weekday (Monday is 0)."""
        general = (
            (self.minimum_soc_percent, self.deadline)
            if self.minimum_soc_percent is not None and self.deadline is not None
            else None
        )
        targets: dict[int, tuple[float, time]] = {}
        for index, name in enumerate(WEEKDAYS):
            entry = self.weekly.get(name)
            if entry is not None:
                targets[index] = (entry.minimum_soc_percent, entry.deadline)
            elif general is not None:
                targets[index] = general
        return targets

    @property
    def limit_soc_percent(self) -> float:
        """The state of charge the car is charged up to, as far as it is a limit."""
        if self.strategy == "minimum_plus_target":
            assert self.target_soc_percent is not None
            return self.target_soc_percent
        return 100.0 if self.minimum_soc_percent is None else self.minimum_soc_percent


class HomeAssistantConfiguration(_StrictModel):
    """Connection and energy mappings for Home Assistant."""

    base_url: AnyHttpUrl
    token: SecretStr
    household_load: EnergyAggregateConfiguration | None = None
    pv_generation: EnergyAggregateConfiguration | None = None
    grid_import: EnergyAggregateConfiguration | None = None
    grid_export: EnergyAggregateConfiguration | None = None
    battery: HomeAssistantBatteryConfiguration | None = None
    ev: HomeAssistantEvConfiguration | None = None
    energy_history: dict[str, EnergyAggregateConfiguration] = Field(
        default_factory=dict
    )
    timeout_seconds: float = Field(gt=0, le=120)
    max_data_age_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_grid_and_battery_mappings(self) -> "HomeAssistantConfiguration":
        """Require grid import and export together and check battery mappings."""
        if (self.grid_import is None) != (self.grid_export is None):
            raise ValueError("grid_import and grid_export must be configured together")

        if self.battery is not None:
            mapping_keys = [
                (value.entity_id, value.attribute)
                for value in (
                    self.battery.state_of_charge,
                    self.battery.capacity,
                    self.battery.minimum_soc,
                    self.battery.maximum_soc,
                    self.battery.maximum_charge,
                    self.battery.maximum_discharge,
                    self.battery.battery_efficiency,
                )
                if isinstance(value, HomeAssistantBatteryEntityConfiguration)
            ]
            if len(mapping_keys) != len(set(mapping_keys)):
                raise ValueError(
                    "Home Assistant battery mappings must not reuse the same "
                    "entity and attribute"
                )
        return self

    @property
    def household_load_source_id(self) -> str:
        """Return the single persistence identity for the aggregate dataset."""
        return HOUSEHOLD_LOAD_SOURCE_ID

    @property
    def grid_flow_source_id(self) -> str:
        """Return the single persistence identity for grid-flow data."""
        return GRID_FLOW_SOURCE_ID

    @property
    def battery_source_id(self) -> str:
        """Return the stable persistence identity for battery data."""
        return BATTERY_SOURCE_ID


class ApplianceConfiguration(ApplianceCapabilities):
    """An installation-specific appliance with optional measured energy history."""

    history: EnergyAggregateConfiguration | None = None


class ForecastSolarConfiguration(_StrictModel):
    """Free public Forecast.Solar installation and request settings."""

    model_config = ConfigDict(validate_default=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    declination_degrees: float = Field(ge=0, le=90)
    azimuth_degrees: float = Field(ge=-180, le=180)
    peak_power_kw: float = Field(gt=0)
    base_url: AnyHttpUrl = AnyHttpUrl("https://api.forecast.solar")
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    max_data_age_seconds: float | None = Field(default=7200, gt=0)

    @property
    def pv_generation_source_id(self) -> str:
        """Return the stable identity for this installation's forecast."""
        return PV_GENERATION_SOURCE_ID


class AwattarConfiguration(_StrictModel):
    """German aWATTar market-data request and freshness settings."""

    model_config = ConfigDict(validate_default=True)

    base_url: AnyHttpUrl = AnyHttpUrl("https://api.awattar.de/v1/marketdata")
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    max_data_age_seconds: float | None = Field(default=7200, gt=0)

    @property
    def electricity_price_source_id(self) -> str:
        """Return the German market-zone persistence identity."""
        return "de"


class PersistenceConfiguration(_StrictModel):
    """Filesystem location for normalized provider data."""

    directory: Path = Field(
        default=Path("/var/lib/energy-optimizer/provider-data"),
        description="Directory containing persisted normalized provider data",
    )


class DataSourceScheduleConfiguration(_StrictModel):
    """Polling and provider-history settings for one data source."""

    enabled: bool = True
    interval_seconds: float = Field(gt=0)
    history_lookback_seconds: float = Field(default=0, ge=0)


class OptimizationTriggerConfiguration(_StrictModel):
    """Settings controlling automatic plan-generation triggers."""

    enabled: bool = False
    required_sources: list[str] = Field(default_factory=list)

    @field_validator("required_sources")
    @classmethod
    def validate_required_sources(cls, values: list[str]) -> list[str]:
        """Reject empty and duplicate source names in the plan input set."""
        if any(not source.strip() for source in values):
            raise ValueError("required_sources must contain non-empty names")
        if len(values) != len(set(values)):
            raise ValueError("required_sources must not contain duplicates")
        return values


class OrchestrationConfiguration(_StrictModel):
    """Runtime settings for scheduled provider retrieval and plan triggers."""

    enabled: bool = False
    startup_fetch: bool = True
    sources: dict[str, DataSourceScheduleConfiguration] = Field(default_factory=dict)
    optimization: OptimizationTriggerConfiguration = Field(
        default_factory=OptimizationTriggerConfiguration
    )

    @model_validator(mode="after")
    def validate_optimization_sources(self) -> "OrchestrationConfiguration":
        """Require plan inputs to be configured when automatic planning is enabled."""
        if any(not source.strip() for source in self.sources):
            raise ValueError("orchestration source names must not be empty")
        if self.optimization.enabled and not self.optimization.required_sources:
            raise ValueError(
                "optimization.required_sources must not be empty when optimization "
                "triggers are enabled"
            )
        missing = set(self.optimization.required_sources) - set(self.sources)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(
                f"optimization.required_sources are not configured as sources: {names}"
            )
        disabled = {
            source
            for source in self.optimization.required_sources
            if not self.sources[source].enabled
        }
        if disabled:
            names = ", ".join(sorted(disabled))
            raise ValueError(f"optimization.required_sources must be enabled: {names}")
        return self


class GridFeeBandConfiguration(_StrictModel):
    """A grid fee that applies from ``start_hour`` up to ``end_hour`` local time."""

    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    fee_ct_per_kwh: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> "GridFeeBandConfiguration":
        if self.end_hour <= self.start_hour:
            raise ValueError("a grid fee band must end after it starts")
        return self


class TariffConfiguration(_StrictModel):
    """Turns the net exchange price into the price on the electricity bill.

    The gross import price is ``(market price + grid fee of the local hour + fixed
    components) * (1 + vat_rate)``. All amounts are net, in ct/kWh.
    """

    grid_fee_bands: list[GridFeeBandConfiguration] = Field(min_length=1)
    supplier_surcharge_ct_per_kwh: float = Field(default=0, ge=0)
    levies_ct_per_kwh: float = Field(default=0, ge=0)
    concession_fee_ct_per_kwh: float = Field(default=0, ge=0)
    electricity_tax_ct_per_kwh: float = Field(default=0, ge=0)
    vat_rate: float = Field(default=0, ge=0, lt=1)

    @model_validator(mode="after")
    def validate_bands_cover_the_day(self) -> "TariffConfiguration":
        covered = [0] * 24
        for band in self.grid_fee_bands:
            for hour in range(band.start_hour, band.end_hour):
                covered[hour] += 1
        if any(count != 1 for count in covered):
            raise ValueError(
                "grid_fee_bands must cover every hour of the day exactly once"
            )
        return self

    def grid_fee_ct_per_kwh(self, local_hour: int) -> float:
        return next(
            band.fee_ct_per_kwh
            for band in self.grid_fee_bands
            if band.start_hour <= local_hour < band.end_hour
        )

    def fixed_components_ct_per_kwh(self) -> float:
        return (
            self.supplier_surcharge_ct_per_kwh
            + self.levies_ct_per_kwh
            + self.concession_fee_ct_per_kwh
            + self.electricity_tax_ct_per_kwh
        )

    def import_price_eur_per_kwh(
        self, market_price_eur_per_kwh: float, local_hour: int
    ) -> float:
        net_ct = (
            market_price_eur_per_kwh * 100
            + self.grid_fee_ct_per_kwh(local_hour)
            + self.fixed_components_ct_per_kwh()
        )
        return net_ct * (1 + self.vat_rate) / 100


class LoadForecastConfiguration(_StrictModel):
    """How the household load of the planning horizon is forecast."""

    method: Literal["auto", "seven_day_mean", "weekday_profile"] = Field(
        default="auto",
        description=(
            "`auto` backtests every method on the last days of history and uses the "
            "one with the smallest mean absolute error"
        ),
    )
    backtest_days: int = Field(default=7, ge=1, le=28)


class Configuration(_StrictModel):
    """Validated settings needed to start the service."""

    time_resolution_minutes: int = Field(gt=0)
    timezone: str = Field(
        default="UTC",
        description="IANA time zone in which the dashboard shows times",
    )
    grid: GridConfiguration
    solver: SolverConfiguration
    home_assistant: HomeAssistantConfiguration | None = None
    appliances: dict[str, ApplianceConfiguration] = Field(default_factory=dict)
    forecast_solar: ForecastSolarConfiguration | None = None
    awattar: AwattarConfiguration | None = None
    tariff: TariffConfiguration | None = None
    load_forecast: LoadForecastConfiguration = Field(
        default_factory=LoadForecastConfiguration
    )
    ev_charging: EvChargingConfiguration | None = None
    persistence: PersistenceConfiguration | None = None
    orchestration: OrchestrationConfiguration | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Accept only known zones whose offset is always whole hours."""
        error = _timezone_name_error(value)
        if error is not None:
            raise ValueError(error)
        return value

    @model_validator(mode="after")
    def validate_orchestration_persistence(self) -> "Configuration":
        """Ensure scheduled collection has the durable store it promises to use."""
        if (
            self.orchestration is not None
            and self.orchestration.enabled
            and self.persistence is None
        ):
            raise ValueError("persistence is required when orchestration is enabled")
        histories = self.home_assistant.energy_history if self.home_assistant else {}

        for key in (*self.appliances, *histories):
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None:
                raise ValueError(
                    "appliance and energy_history IDs must use "
                    "lowercase letters, digits and underscores"
                )
        if self.home_assistant is None and any(
            appliance.history is not None for appliance in self.appliances.values()
        ):
            raise ValueError(
                "home_assistant connection is required for appliance history"
            )
        return self

    def configured_energy_histories(self) -> dict[str, EnergyAggregateConfiguration]:
        """Return stable identities of all generic and appliance history sources."""
        histories = self.home_assistant.energy_history if self.home_assistant else {}
        return {
            **{f"history.{name}": mapping for name, mapping in histories.items()},
            **{
                f"appliance.{name}": item.history
                for name, item in self.appliances.items()
                if item.history is not None
            },
        }

    def is_configured_household_load_source(
        self, provider: str, entity_id: str | None
    ) -> bool:
        return (
            self.home_assistant is not None
            and self.home_assistant.household_load is not None
            and provider == "home-assistant"
            and entity_id == self.home_assistant.household_load_source_id
        )

    def is_configured_grid_flow_source(
        self, provider: str, entity_id: str | None
    ) -> bool:
        return (
            self.home_assistant is not None
            and self.home_assistant.grid_import is not None
            and self.home_assistant.grid_export is not None
            and provider == "home-assistant"
            and entity_id == self.home_assistant.grid_flow_source_id
        )


def _log_load_failure(path: Path, error_type: str) -> None:
    logger.error(
        "event=configuration_load_failed component=configuration operation=load "
        "path=%s error_type=%s",
        path,
        error_type,
    )


def load_configuration(path: Path) -> Configuration:
    """Load and validate a YAML configuration file.

    A ``ConfigurationError`` includes the path and the relevant validation
    details so startup failures can be diagnosed without a traceback.
    """
    logger.debug(
        "event=configuration_load_started component=configuration operation=load "
        "path=%s",
        path,
    )
    if not path.is_file():
        _log_load_failure(path, "ConfigurationError")
        raise ConfigurationError(f"Configuration file not found: {path}")

    try:
        with path.open(encoding="utf-8") as configuration_file:
            document: Any = yaml.safe_load(configuration_file)
    except yaml.YAMLError as error:
        _log_load_failure(path, "YAMLError")
        raise ConfigurationError(
            f"Invalid YAML in configuration file {path}: {error}"
        ) from error
    except OSError as error:
        _log_load_failure(path, "OSError")
        raise ConfigurationError(
            f"Could not read configuration file {path}: {error}"
        ) from error

    if not isinstance(document, dict):
        _log_load_failure(path, "ConfigurationError")
        raise ConfigurationError(
            f"Configuration file {path} must contain a YAML mapping at the top level"
        )

    try:
        configuration = Configuration.model_validate(document)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        _log_load_failure(path, "ValidationError")
        raise ConfigurationError(
            f"Invalid configuration in {path}: {details}"
        ) from error
    logger.info(
        "event=configuration_loaded component=configuration operation=load "
        "path=%s persistence_enabled=%s orchestration_enabled=%s "
        "home_assistant_enabled=%s timezone=%s",
        path,
        configuration.persistence is not None,
        configuration.orchestration is not None and configuration.orchestration.enabled,
        configuration.home_assistant is not None,
        configuration.timezone,
    )
    return configuration
