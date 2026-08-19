"""Provider-independent data returned by external integrations."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

HOUSEHOLD_LOAD_SOURCE_ID = "household_load"
HOUSEHOLD_LOAD_MAX_VALUES = 87_672
PV_GENERATION_SOURCE_ID = "pv_generation"
GRID_FLOW_SOURCE_ID = "grid_flow"
BATTERY_SOURCE_ID = "battery"
BATTERY_EFFICIENCY_SOURCE_ID = "battery_efficiency"
BATTERY_EFFICIENCY_HISTORY_SOURCE_ID = "battery_efficiency_history"
ELECTRICITY_PRICE_SOURCE_ID = "de"


@dataclass(frozen=True)
class SourceMetadata:
    """Identify the system that supplied normalized data."""

    provider: str
    entity_id: str | None = None


@dataclass(frozen=True)
class IntervalQuality:
    """Quality assessment for one normalized hourly interval."""

    status: Literal["valid", "suspect"] = "valid"
    reason: str | None = None
    entity_id: str | None = None


@dataclass(frozen=True)
class HouseholdLoadData:
    """Normalized hourly household-load data from a provider."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    load_kw: tuple[float, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime
    quality: tuple[IntervalQuality, ...] = ()


@dataclass(frozen=True)
class PvGenerationData:
    """Normalized hourly PV-generation forecast from a provider."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    generation_kw: tuple[float, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    expires_at: datetime
    generated_at: datetime | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class GridFlowData:
    """Normalized hourly grid import and export data from a provider."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    import_kw: tuple[float, ...]
    export_kw: tuple[float, ...]
    unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime
    quality: tuple[IntervalQuality, ...] = ()


@dataclass(frozen=True)
class BatteryData:
    """Normalized current battery state and capabilities from a provider."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    state_of_charge_kwh: tuple[float, ...]
    capacity_kwh: float
    minimum_soc_kwh: float
    maximum_soc_kwh: float
    initial_soc_kwh: float
    maximum_charge_kw: float
    maximum_discharge_kw: float
    battery_efficiency: float
    unit: Literal["kWh"]
    power_unit: Literal["kW"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime


@dataclass(frozen=True)
class BatteryEfficiencyHistoryData:
    """Aligned measured energy and state-of-charge history for efficiency work."""

    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    battery_energy_in_kwh: tuple[float, ...]
    battery_energy_out_kwh: tuple[float, ...]
    inverter_charge_energy_in_kwh: tuple[float, ...]
    inverter_charge_energy_out_kwh: tuple[float, ...]
    inverter_discharge_energy_in_kwh: tuple[float, ...]
    inverter_discharge_energy_out_kwh: tuple[float, ...]
    state_of_charge_percent: tuple[float, ...]
    unit: Literal["kWh"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime


@dataclass(frozen=True)
class BatteryEfficiencyData:
    """Calculated efficiency components and their measurement diagnostics."""

    schema_version: Literal["1"]
    status: Literal["ok", "insufficient_data", "invalid"]
    inverter_charge_efficiency: float | None
    inverter_discharge_efficiency: float | None
    battery_efficiency: float | None
    round_trip_efficiency: float | None
    history_start: datetime | None
    history_end: datetime | None
    battery_throughput_kwh: float
    charge_throughput_kwh: float
    discharge_throughput_kwh: float
    complete_cycle_count: int
    unit: Literal["ratio"]
    source: SourceMetadata
    retrieved_at: datetime
    latest_observation_at: datetime
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ElectricityPriceData:
    """Normalized hourly electricity prices from a market provider."""

    schema_version: Literal["1"]
    timestamps: tuple[datetime, ...]
    interval_minutes: Literal[60]
    import_price_eur_per_kwh: tuple[float, ...]
    export_price_eur_per_kwh: tuple[float, ...]
    unit: Literal["EUR/kWh"]
    source: SourceMetadata
    retrieved_at: datetime
    expires_at: datetime


class HouseholdLoadProvider(Protocol):
    """Retrieve normalized household-load data for a requested period."""

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> HouseholdLoadData:
        """Fetch hourly household load for the requested half-open period."""

    def is_fresh(
        self,
        data: HouseholdLoadData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether data is within the configured polling age threshold."""


class PvForecastProvider(Protocol):
    """Retrieve normalized hourly PV generation forecasts."""

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> PvGenerationData:
        """Fetch a forecast for the requested half-open period."""

    def is_fresh(
        self,
        data: PvGenerationData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether the forecast is still usable."""


class GridFlowProvider(Protocol):
    """Retrieve normalized hourly grid import and export data."""

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        history_lookback_seconds: float = 0,
        *,
        now: datetime | None = None,
    ) -> GridFlowData:
        """Fetch hourly grid flow for the requested half-open period."""

    def is_fresh(
        self,
        data: GridFlowData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether data is within the configured polling age threshold."""


class BatteryProvider(Protocol):
    """Retrieve normalized current battery state and capabilities."""

    def fetch(self, *, now: datetime | None = None) -> BatteryData:
        """Fetch the current battery state and capabilities."""

    def is_fresh(
        self,
        data: BatteryData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether data is within the configured polling age threshold."""


class ElectricityPriceProvider(Protocol):
    """Retrieve normalized hourly electricity prices."""

    def fetch(
        self,
        start_time: datetime,
        end_time: datetime | None = None,
        *,
        now: datetime | None = None,
    ) -> ElectricityPriceData:
        """Fetch prices for the requested half-open period."""

    def is_fresh(
        self,
        data: ElectricityPriceData,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Report whether prices remain usable."""
