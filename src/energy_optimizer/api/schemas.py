"""Pydantic schemas for the Energy Optimizer HTTP API."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from energy_optimizer.api.validation import (
    replace_non_finite_values,
    require_aware_timestamp,
    require_aware_timestamps,
)

MAX_HORIZON_HOURS = 87_672


class HourlyOptimizationRequest(BaseModel):
    """Validated hourly inputs accepted by the optimization boundary."""

    model_config = ConfigDict(extra="forbid")

    start_time: datetime = Field(description="Timezone-aware start of the horizon")
    interval_minutes: Literal[60] = Field(
        description="Duration of every interval; hourly requests require 60"
    )
    load_kw: list[Annotated[float, Field(ge=0, description="Household load in kW")]] = (
        Field(min_length=1, max_length=168)
    )
    pv_generation_kw: list[
        Annotated[float, Field(ge=0, description="PV generation in kW")]
    ] = Field(min_length=1, max_length=168)
    import_price_eur_per_kwh: list[
        Annotated[float, Field(description="Grid import price in EUR/kWh")]
    ] = Field(min_length=1, max_length=168)
    export_price_eur_per_kwh: list[
        Annotated[float, Field(description="Grid export price in EUR/kWh")]
    ] = Field(min_length=1, max_length=168)

    @model_validator(mode="after")
    def validate_horizon(self) -> "HourlyOptimizationRequest":
        """Require a timezone and one value for every input series per hour."""
        require_aware_timestamp(self.start_time)

        lengths = {
            "load_kw": len(self.load_kw),
            "pv_generation_kw": len(self.pv_generation_kw),
            "import_price_eur_per_kwh": len(self.import_price_eur_per_kwh),
            "export_price_eur_per_kwh": len(self.export_price_eur_per_kwh),
        }
        if len(set(lengths.values())) != 1:
            raise ValueError(f"time-series lengths must match: {lengths}")
        return self


class SourceMetadata(BaseModel):
    """Identify the system that supplied a normalized data series."""

    model_config = ConfigDict(extra="forbid")

    provider: Annotated[str, Field(min_length=1, max_length=100)]
    entity_id: Annotated[str, Field(min_length=1, max_length=255)] | None = None


class ElectricityPriceRequest(BaseModel):
    """Versioned normalized hourly electricity-price data."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(description="Version of this API contract")
    timestamps: list[datetime] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Timezone-aware hourly timestamps in ascending order",
    )
    interval_minutes: Literal[60] = Field(
        description="Duration of every price interval; hourly data requires 60"
    )
    import_price_eur_per_kwh: list[Annotated[float, Field(ge=-100, le=100)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Grid import price in EUR/kWh, one value per timestamp",
    )
    export_price_eur_per_kwh: list[Annotated[float, Field(ge=-100, le=100)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Grid export price in EUR/kWh, one value per timestamp",
    )
    unit: Literal["EUR/kWh"] = Field(description="Unit used by price fields")
    source: SourceMetadata = Field(description="Provider-independent source metadata")
    retrieved_at: datetime = Field(
        description="Time when the source data was retrieved"
    )
    expires_at: datetime = Field(description="Time after which the data is stale")

    @field_validator("timestamps", "retrieved_at", "expires_at")
    @classmethod
    def validate_aware_timestamps(
        cls, values: datetime | list[datetime]
    ) -> datetime | list[datetime]:
        """Reject naive timestamps that cannot be compared safely."""
        return require_aware_timestamps(values)

    @field_validator(
        "import_price_eur_per_kwh", "export_price_eur_per_kwh", mode="before"
    )
    @classmethod
    def validate_finite_prices(cls, values: object) -> object:
        """Make non-finite values safe for the JSON validation response."""
        return replace_non_finite_values(values)

    @model_validator(mode="after")
    def validate_price_series(self) -> "ElectricityPriceRequest":
        """Require aligned, ordered, fresh price data with explicit coverage."""
        if len(self.timestamps) != len(self.import_price_eur_per_kwh):
            raise ValueError(
                "timestamps and import_price_eur_per_kwh must have the same length"
            )
        if len(self.timestamps) != len(self.export_price_eur_per_kwh):
            raise ValueError(
                "timestamps and export_price_eur_per_kwh must have the same length"
            )
        timestamp_values = [timestamp.timestamp() for timestamp in self.timestamps]
        if timestamp_values != sorted(set(timestamp_values)):
            raise ValueError("timestamps must be unique and in ascending order")
        if any(
            later - earlier != self.interval_minutes * 60
            for earlier, later in zip(timestamp_values, timestamp_values[1:])
        ):
            raise ValueError("timestamps must be spaced by interval_minutes")
        if self.expires_at <= self.retrieved_at:
            raise ValueError("expires_at must be later than retrieved_at")
        if self.timestamps[0] < self.retrieved_at:
            raise ValueError("price coverage must not start before retrieved_at")
        if self.timestamps[-1] >= self.expires_at:
            raise ValueError("price data is stale at the end of its coverage")
        return self


class ElectricityPriceResponse(BaseModel):
    """Response returned after electricity-price data passes validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    schema_version: Literal["1"]
    timestamps: list[datetime]
    interval_minutes: Literal[60]
    import_price_eur_per_kwh: list[float]
    export_price_eur_per_kwh: list[float]
    unit: Literal["EUR/kWh"]
    source: SourceMetadata
    retrieved_at: datetime
    expires_at: datetime


class BatteryRequest(BaseModel):
    """Versioned battery state and capability data at the API boundary."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(description="Version of this API contract")
    start_time: datetime = Field(description="Timezone-aware start of the series")
    interval_minutes: Literal[60] = Field(
        description="Duration of every series interval; hourly data requires 60"
    )
    state_of_charge_kwh: list[Annotated[float, Field(ge=0, le=100000)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Battery state of charge in kWh, one value per interval",
    )
    capacity_kwh: Annotated[float, Field(gt=0, le=100000)] = Field(
        description="Usable battery capacity in kWh"
    )
    minimum_soc_kwh: Annotated[float, Field(ge=0, le=100000)] = Field(
        description="Minimum allowed state of charge in kWh"
    )
    maximum_soc_kwh: Annotated[float, Field(gt=0, le=100000)] = Field(
        description="Maximum allowed state of charge in kWh"
    )
    initial_soc_kwh: Annotated[float, Field(ge=0, le=100000)] = Field(
        description="State of charge before the first interval in kWh"
    )
    maximum_charge_kw: Annotated[float, Field(gt=0, le=100000)] = Field(
        description="Maximum charging power in kW"
    )
    maximum_discharge_kw: Annotated[float, Field(gt=0, le=100000)] = Field(
        description="Maximum discharging power in kW"
    )
    battery_efficiency: Annotated[float, Field(gt=0, le=1)] = Field(
        description="Measured battery round-trip efficiency"
    )
    unit: Literal["kWh"] = Field(description="Unit used by energy fields")
    power_unit: Literal["kW"] = Field(description="Unit used by power fields")
    source: SourceMetadata | None = Field(
        default=None,
        description="Optional source metadata for externally supplied data",
    )

    @field_validator(
        "state_of_charge_kwh",
        "capacity_kwh",
        "minimum_soc_kwh",
        "maximum_soc_kwh",
        "initial_soc_kwh",
        "maximum_charge_kw",
        "maximum_discharge_kw",
        "battery_efficiency",
        mode="before",
    )
    @classmethod
    def validate_finite_values(cls, values: object) -> object:
        """Make non-finite values safe for the JSON validation response."""
        return replace_non_finite_values(values)

    @model_validator(mode="after")
    def validate_battery_constraints(self) -> "BatteryRequest":
        """Require an unambiguous timestamp and internally consistent limits."""
        require_aware_timestamp(self.start_time)
        if self.minimum_soc_kwh > self.maximum_soc_kwh:
            raise ValueError("minimum_soc_kwh must not exceed maximum_soc_kwh")
        if self.maximum_soc_kwh > self.capacity_kwh:
            raise ValueError("maximum_soc_kwh must not exceed capacity_kwh")
        if not self.minimum_soc_kwh <= self.initial_soc_kwh <= self.maximum_soc_kwh:
            raise ValueError(
                "initial_soc_kwh must be between minimum_soc_kwh and maximum_soc_kwh"
            )
        if any(
            not self.minimum_soc_kwh <= value <= self.maximum_soc_kwh
            for value in self.state_of_charge_kwh
        ):
            raise ValueError(
                "state_of_charge_kwh values must be between minimum_soc_kwh "
                "and maximum_soc_kwh"
            )
        return self


class BatteryResponse(BaseModel):
    """Response returned after battery data passes validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    state_of_charge_kwh: list[float]
    capacity_kwh: float
    minimum_soc_kwh: float
    maximum_soc_kwh: float
    initial_soc_kwh: float
    maximum_charge_kw: float
    maximum_discharge_kw: float
    battery_efficiency: float
    unit: Literal["kWh"]
    power_unit: Literal["kW"]
    source: SourceMetadata | None = None


class HouseholdLoadRequest(BaseModel):
    """Versioned hourly household-load data at the API boundary."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(description="Version of this API contract")
    start_time: datetime = Field(description="Timezone-aware start of the series")
    interval_minutes: Literal[60] = Field(
        description="Duration of every series interval; hourly data requires 60"
    )
    load_kw: list[Annotated[float, Field(ge=0, le=1000)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Household electrical load in kW, one value per interval",
    )
    unit: Literal["kW"] = Field(description="Unit used by load_kw")
    source: SourceMetadata | None = Field(
        default=None,
        description="Optional source metadata for externally supplied data",
    )
    retrieved_at: datetime = Field(
        description="Time when the household-load data was retrieved"
    )
    latest_observation_at: datetime = Field(
        description="Time of the latest source observation in the data"
    )

    @field_validator("load_kw", mode="before")
    @classmethod
    def validate_finite_load_values(cls, values: object) -> object:
        """Make non-finite values safe for the JSON validation response."""
        return replace_non_finite_values(values)

    @model_validator(mode="after")
    def validate_start_time(self) -> "HouseholdLoadRequest":
        """Require unambiguous timestamps for the load series metadata."""
        timestamps = (self.start_time, self.retrieved_at, self.latest_observation_at)
        require_aware_timestamps(list(timestamps))
        return self


class HouseholdLoadResponse(BaseModel):
    """Response returned after household-load data passes validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    load_kw: list[float]
    unit: Literal["kW"]
    source: SourceMetadata | None = None
    retrieved_at: datetime
    latest_observation_at: datetime


class HouseholdLoadQuality(BaseModel):
    """Quality metadata for one historic household-load interval."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["valid", "suspect"]
    reason: str | None
    entity_id: str | None


class HistoricHouseholdLoadResponse(BaseModel):
    """Historic household-load actuals returned to the dashboard."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated", "stale", "suspect", "empty"]
    data_type: Literal["household_load"]
    schema_version: Literal["1"]
    start_time: datetime = Field(description="Inclusive requested range start")
    end_time: datetime = Field(description="Exclusive requested range end")
    interval_minutes: Literal[60]
    timestamps: list[datetime]
    load_kw: list[float]
    quality: list[HouseholdLoadQuality]
    unit: Literal["kW"]
    source: SourceMetadata
    coverage_start_time: datetime | None
    coverage_end_time: datetime | None
    available_start_time: datetime
    available_end_time: datetime
    retrieved_at: datetime
    latest_observation_at: datetime
    validation_status: Literal["valid", "suspect"]
    freshness: Literal["fresh", "stale", "unknown"]
    freshness_checked_at: datetime


class DashboardSeries(BaseModel):
    """One aligned, provider-independent dashboard series."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    data_type: str = Field(min_length=1)
    scenario_kind: Literal["actual", "forecast", "plan"]
    timestamps: list[datetime]
    values: list[float | None]
    unit: str = Field(min_length=1)
    source: SourceMetadata | None = None
    requested_start_time: datetime
    requested_end_time: datetime
    available_start_time: datetime | None
    available_end_time: datetime | None
    retrieved_at: datetime | None
    generated_at: datetime | None = None
    published_at: datetime | None = None
    freshness: Literal["fresh", "stale", "unknown"]
    validation_status: Literal["valid", "suspect", "invalid"]
    missing_intervals: list[datetime] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_series_alignment(self) -> "DashboardSeries":
        """Require one value, including null gaps, for every timestamp."""
        if len(self.timestamps) != len(self.values):
            raise ValueError("timestamps and values must have the same length")
        try:
            require_aware_timestamps(self.timestamps)
        except ValueError as error:
            raise ValueError("series timestamps must include a timezone") from error
        if any(
            later <= earlier
            for earlier, later in zip(self.timestamps, self.timestamps[1:])
        ):
            raise ValueError("series timestamps must be in ascending order")
        return self


class DashboardPlanSummary(BaseModel):
    """Optional plan-level diagnostics shared by dashboard consumers."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "empty", "infeasible", "unavailable"]
    objective_value: float | None = None
    diagnostics: list[str] = Field(default_factory=list)


class DashboardDataResponse(BaseModel):
    """Versioned read contract for actual, forecast, and plan dashboard data."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    status: Literal[
        "validated",
        "partial",
        "stale",
        "empty",
        "unavailable",
        "invalid",
        "infeasible",
    ]
    requested_start_time: datetime
    requested_end_time: datetime
    interval_minutes: Literal[60]
    series: list[DashboardSeries]
    diagnostics: list[str] = Field(default_factory=list)
    plan_summary: DashboardPlanSummary | None = None


class PvGenerationRequest(BaseModel):
    """Versioned hourly PV-generation data at the API boundary."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(description="Version of this API contract")
    start_time: datetime = Field(description="Timezone-aware start of the series")
    interval_minutes: Literal[60] = Field(
        description="Duration of every series interval; hourly data requires 60"
    )
    generation_kw: list[Annotated[float, Field(ge=0, le=1000)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="PV generation in kW, one value per interval",
    )
    unit: Literal["kW"] = Field(description="Unit used by generation_kw")
    source: SourceMetadata | None = Field(
        default=None,
        description="Optional source metadata for externally supplied data",
    )

    @field_validator("generation_kw", mode="before")
    @classmethod
    def validate_finite_generation_values(cls, values: object) -> object:
        """Make non-finite values safe for the JSON validation response."""
        return replace_non_finite_values(values)

    @model_validator(mode="after")
    def validate_start_time(self) -> "PvGenerationRequest":
        """Require timestamps that identify an unambiguous hourly series."""
        require_aware_timestamp(self.start_time)
        return self


class PvGenerationResponse(BaseModel):
    """Response returned after PV-generation data passes validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    generation_kw: list[float]
    unit: Literal["kW"]
    source: SourceMetadata | None = None


class GridFlowRequest(BaseModel):
    """Versioned hourly grid import and export data at the API boundary."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = Field(description="Version of this API contract")
    start_time: datetime = Field(description="Timezone-aware start of the series")
    interval_minutes: Literal[60] = Field(
        description="Duration of every series interval; hourly data requires 60"
    )
    import_kw: list[Annotated[float, Field(ge=0, le=1000)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Grid import in kW, one value per interval",
    )
    export_kw: list[Annotated[float, Field(ge=0, le=1000)]] = Field(
        min_length=1,
        max_length=MAX_HORIZON_HOURS,
        description="Grid export in kW, one value per interval",
    )
    unit: Literal["kW"] = Field(description="Unit used by import_kw and export_kw")
    source: SourceMetadata | None = Field(
        default=None,
        description="Optional source metadata for externally supplied data",
    )
    retrieved_at: datetime = Field(
        description="Time when the grid-flow data was retrieved"
    )
    latest_observation_at: datetime = Field(
        description="Time of the latest source observation in the data"
    )

    @field_validator("import_kw", "export_kw", mode="before")
    @classmethod
    def validate_finite_values(cls, values: object) -> object:
        """Make non-finite values safe for the JSON validation response."""
        return replace_non_finite_values(values)

    @model_validator(mode="after")
    def validate_series(self) -> "GridFlowRequest":
        """Require an unambiguous timestamp and aligned import/export series."""
        timestamps = (
            self.start_time,
            self.retrieved_at,
            self.latest_observation_at,
        )
        require_aware_timestamps(list(timestamps))
        if len(self.import_kw) != len(self.export_kw):
            raise ValueError(
                "import_kw and export_kw must contain the same number of values"
            )
        return self


class GridFlowResponse(BaseModel):
    """Response returned after grid-flow data passes validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    schema_version: Literal["1"]
    start_time: datetime
    interval_minutes: Literal[60]
    import_kw: list[Annotated[float, Field(ge=0, le=1000)]]
    export_kw: list[Annotated[float, Field(ge=0, le=1000)]]
    unit: Literal["kW"]
    source: SourceMetadata | None = None
    retrieved_at: datetime
    latest_observation_at: datetime


class OptimizationResponse(BaseModel):
    """Response returned after an hourly request passes API validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["validated"]
    start_time: datetime
    interval_minutes: Literal[60]
    hours: int = Field(ge=1, le=168)
