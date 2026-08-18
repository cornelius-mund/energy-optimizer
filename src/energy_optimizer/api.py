"""HTTP API for the Energy Optimizer service."""

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Annotated, AsyncIterator, Awaitable, Callable, Literal, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles

from energy_optimizer import __version__
from energy_optimizer.config import ConfigurationError, load_configuration
from energy_optimizer.logging_config import configure_logging
from energy_optimizer.orchestration import build_configured_orchestrator
from energy_optimizer.providers.interfaces import (
    ElectricityPriceData,
    GridFlowData,
    HouseholdLoadData,
    IntervalQuality,
    PvGenerationData,
)
from energy_optimizer.providers.interfaces import (
    SourceMetadata as ProviderSourceMetadata,
)
from energy_optimizer.storage import (
    ProviderDataKey,
    ProviderDataStore,
    ProviderDataStoreError,
)

MAX_HORIZON_HOURS = 87_672
logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    """Return a safe request ID for logs and the response header."""
    candidate = request.headers.get("X-Request-ID", "")
    if (
        candidate
        and len(candidate) <= 64
        and all(character.isalnum() or character in "-_." for character in candidate)
    ):
        return candidate
    return uuid4().hex


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
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
            raise ValueError("start_time must include a timezone")

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
        candidates = values if isinstance(values, list) else [values]
        if any(
            value.tzinfo is None or value.utcoffset() is None for value in candidates
        ):
            raise ValueError("timestamps must include a timezone")
        return values

    @field_validator(
        "import_price_eur_per_kwh", "export_price_eur_per_kwh", mode="before"
    )
    @classmethod
    def validate_finite_prices(cls, values: object) -> object:
        """Make non-finite values safe for the JSON validation response."""
        if isinstance(values, list):
            return [
                None if isinstance(value, float) and not math.isfinite(value) else value
                for value in values
            ]
        return values

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
    charge_efficiency: Annotated[float, Field(gt=0, le=1)] = Field(
        description="Fraction of charging energy retained by the battery"
    )
    discharge_efficiency: Annotated[float, Field(gt=0, le=1)] = Field(
        description="Fraction of battery energy delivered during discharge"
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
        "charge_efficiency",
        "discharge_efficiency",
        mode="before",
    )
    @classmethod
    def validate_finite_values(cls, values: object) -> object:
        """Make non-finite values safe for the JSON validation response."""
        if isinstance(values, list):
            return [
                None if isinstance(value, float) and not math.isfinite(value) else value
                for value in values
            ]
        if isinstance(values, float) and not math.isfinite(values):
            return None
        return values

    @model_validator(mode="after")
    def validate_battery_constraints(self) -> "BatteryRequest":
        """Require an unambiguous timestamp and internally consistent limits."""
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
            raise ValueError("start_time must include a timezone")
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
    charge_efficiency: float
    discharge_efficiency: float
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
        if isinstance(values, list):
            return [
                None if isinstance(value, float) and not math.isfinite(value) else value
                for value in values
            ]
        return values

    @model_validator(mode="after")
    def validate_start_time(self) -> "HouseholdLoadRequest":
        """Require unambiguous timestamps for the load series metadata."""
        timestamps = (self.start_time, self.retrieved_at, self.latest_observation_at)
        if any(
            timestamp.tzinfo is None or timestamp.utcoffset() is None
            for timestamp in timestamps
        ):
            raise ValueError("timestamps must include a timezone")
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
        if any(
            timestamp.tzinfo is None or timestamp.utcoffset() is None
            for timestamp in self.timestamps
        ):
            raise ValueError("series timestamps must include a timezone")
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


HOUSEHOLD_LOAD_ADAPTER = TypeAdapter(HouseholdLoadData)
GRID_FLOW_ADAPTER = TypeAdapter(GridFlowData)
PV_GENERATION_ADAPTER = TypeAdapter(PvGenerationData)
ELECTRICITY_PRICE_ADAPTER = TypeAdapter(ElectricityPriceData)


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
        if isinstance(values, list):
            return [
                None if isinstance(value, float) and not math.isfinite(value) else value
                for value in values
            ]
        return values

    @model_validator(mode="after")
    def validate_start_time(self) -> "PvGenerationRequest":
        """Require timestamps that identify an unambiguous hourly series."""
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
            raise ValueError("start_time must include a timezone")
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
        if isinstance(values, list):
            return [
                None if isinstance(value, float) and not math.isfinite(value) else value
                for value in values
            ]
        return values

    @model_validator(mode="after")
    def validate_series(self) -> "GridFlowRequest":
        """Require an unambiguous timestamp and aligned import/export series."""
        timestamps = (
            self.start_time,
            self.retrieved_at,
            self.latest_observation_at,
        )
        if any(
            timestamp.tzinfo is None or timestamp.utcoffset() is None
            for timestamp in timestamps
        ):
            raise ValueError("start_time must include a timezone")
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


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Load configuration before accepting requests."""
    try:
        log_level = configure_logging()
    except ConfigurationError:
        logging.getLogger(__name__).critical(
            "event=service_startup_failed component=logging operation=configure "
            "error_type=ConfigurationError"
        )
        raise
    path = Path(os.environ.get("ENERGY_OPTIMIZER_CONFIG", "config.yaml"))
    logger.info(
        "event=service_starting component=api operation=startup "
        "log_level=%s configuration_path=%s",
        logging.getLevelName(log_level),
        path,
    )
    service_started = False
    try:
        try:
            configuration = load_configuration(path)
            application.state.configuration = configuration
            application.state.provider_data_store = (
                ProviderDataStore(configuration.persistence.directory)
                if configuration.persistence is not None
                else None
            )
            application.state.orchestrator = build_configured_orchestrator(
                configuration,
                application.state.provider_data_store,
            )
            stop_event = asyncio.Event()
            application.state.orchestration_stop_event = stop_event
            application.state.orchestration_task = None
            if application.state.orchestrator is not None:
                application.state.orchestration_task = asyncio.create_task(
                    application.state.orchestrator.run_forever(stop_event)
                )
            logger.info(
                "event=service_started component=api operation=startup "
                "persistence_enabled=%s orchestration_enabled=%s "
                "home_assistant_enabled=%s",
                configuration.persistence is not None,
                configuration.orchestration is not None
                and configuration.orchestration.enabled,
                configuration.home_assistant is not None,
            )
            service_started = True
        except ConfigurationError as error:
            logger.critical(
                "event=service_startup_failed component=api operation=startup "
                "error_type=%s",
                error.__class__.__name__,
                exc_info=True,
            )
            raise
        except Exception as error:
            logger.critical(
                "event=service_startup_failed component=api operation=startup "
                "error_type=%s",
                error.__class__.__name__,
                exc_info=True,
            )
            raise
        yield
    finally:
        if service_started:
            logger.info("event=service_stopping component=api operation=shutdown")
            configured_stop_event = getattr(
                application.state, "orchestration_stop_event", None
            )
            task = getattr(application.state, "orchestration_task", None)
            if configured_stop_event is not None:
                configured_stop_event.set()
            if task is not None:
                try:
                    await task
                except Exception:
                    logger.critical(
                        "event=service_shutdown_failed component=orchestration "
                        "operation=stop",
                        exc_info=True,
                    )
                    raise
            logger.info("event=service_stopped component=api operation=shutdown")


app = FastAPI(title="Energy Optimizer", version=__version__, lifespan=lifespan)
DEFAULT_FRONTEND_DIRECTORY = Path(__file__).parents[2] / "frontend"


def configured_frontend_directory() -> Path:
    """Return the dashboard directory for source and installed deployments."""
    return Path(
        os.environ.get(
            "ENERGY_OPTIMIZER_FRONTEND_DIRECTORY",
            str(DEFAULT_FRONTEND_DIRECTORY),
        )
    )


FRONTEND_DIRECTORY = configured_frontend_directory()


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Log request outcomes without recording bodies or authorization headers."""
    request_id = _request_id(request)
    started = perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "event=request_failed component=api operation=request method=%s "
            "path=%s request_id=%s duration_ms=%.2f",
            request.method,
            request.url.path,
            request_id,
            (perf_counter() - started) * 1000,
        )
        raise

    duration_ms = (perf_counter() - started) * 1000
    status_code = response.status_code
    if status_code >= 500:
        level = logging.ERROR
    elif status_code >= 400:
        level = logging.WARNING
    elif request.method == "GET" and request.url.path == "/health":
        level = logging.DEBUG
    else:
        level = logging.INFO
    event = (
        "health_check_request"
        if request.method == "GET" and request.url.path == "/health"
        else "request_completed"
    )
    logger.log(
        level,
        "event=%s component=api operation=request method=%s "
        "path=%s status=%s request_id=%s duration_ms=%.2f",
        event,
        request.method,
        request.url.path,
        status_code,
        request_id,
        duration_ms,
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service health and running version."""
    return {"status": "ok", "version": __version__}


@app.get("/dashboard", include_in_schema=False)
def dashboard_redirect() -> RedirectResponse:
    """Redirect the dashboard root to its trailing-slash entry point."""
    if not FRONTEND_DIRECTORY.is_dir():
        raise HTTPException(
            status_code=503,
            detail="dashboard assets are not available in this deployment",
        )
    return RedirectResponse(url="/dashboard/", status_code=307)


@app.post("/optimize", response_model=OptimizationResponse)
def optimize(request: HourlyOptimizationRequest) -> OptimizationResponse:
    """Validate an hourly optimization request at the versioned API boundary."""
    return OptimizationResponse(
        status="validated",
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        hours=len(request.load_kw),
    )


@app.post("/api/v1/battery", response_model=BatteryResponse)
def battery(request: BatteryRequest) -> BatteryResponse:
    """Validate a versioned hourly battery state and capabilities object."""
    return BatteryResponse(
        status="validated",
        schema_version=request.schema_version,
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        state_of_charge_kwh=request.state_of_charge_kwh,
        capacity_kwh=request.capacity_kwh,
        minimum_soc_kwh=request.minimum_soc_kwh,
        maximum_soc_kwh=request.maximum_soc_kwh,
        initial_soc_kwh=request.initial_soc_kwh,
        maximum_charge_kw=request.maximum_charge_kw,
        maximum_discharge_kw=request.maximum_discharge_kw,
        charge_efficiency=request.charge_efficiency,
        discharge_efficiency=request.discharge_efficiency,
        unit=request.unit,
        power_unit=request.power_unit,
        source=request.source,
    )


@app.post("/api/v1/electricity-prices", response_model=ElectricityPriceResponse)
def electricity_prices(
    request: ElectricityPriceRequest,
) -> ElectricityPriceResponse:
    """Validate normalized hourly electricity-price data."""
    return ElectricityPriceResponse(
        status="validated",
        schema_version=request.schema_version,
        timestamps=request.timestamps,
        interval_minutes=request.interval_minutes,
        import_price_eur_per_kwh=request.import_price_eur_per_kwh,
        export_price_eur_per_kwh=request.export_price_eur_per_kwh,
        unit=request.unit,
        source=request.source,
        retrieved_at=request.retrieved_at,
        expires_at=request.expires_at,
    )


@app.post(
    "/api/v1/household-load",
    response_model=HouseholdLoadResponse,
    responses={503: {"description": "Provider data could not be persisted"}},
)
def household_load(
    request: Request,
    data: HouseholdLoadRequest,
) -> HouseholdLoadResponse:
    """Validate a versioned hourly household-load data series."""
    configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    persisted_provider_data: HouseholdLoadData | None = None
    if (
        store is not None
        and data.source is not None
        and configuration.is_configured_household_load_source(
            data.source.provider, data.source.entity_id
        )
    ):
        key = ProviderDataKey(
            data_type="household-load",
            provider=data.source.provider,
            entity_id=data.source.entity_id,
        )
        provider_data = HouseholdLoadData(
            schema_version=data.schema_version,
            start_time=data.start_time,
            interval_minutes=data.interval_minutes,
            load_kw=tuple(data.load_kw),
            unit=data.unit,
            source=ProviderSourceMetadata(
                provider=data.source.provider,
                entity_id=data.source.entity_id,
            ),
            retrieved_at=data.retrieved_at,
            latest_observation_at=data.latest_observation_at,
        )
        try:
            persisted_provider_data = store.save(
                key, HOUSEHOLD_LOAD_ADAPTER, provider_data
            )
        except ProviderDataStoreError as error:
            raise HTTPException(
                status_code=503,
                detail=f"could not persist household-load provider data: {error}",
            ) from error

    return HouseholdLoadResponse(
        status="validated",
        schema_version=data.schema_version,
        start_time=(
            persisted_provider_data.start_time
            if persisted_provider_data is not None
            else data.start_time
        ),
        interval_minutes=data.interval_minutes,
        load_kw=(
            list(persisted_provider_data.load_kw)
            if persisted_provider_data is not None
            else data.load_kw
        ),
        unit=data.unit,
        source=(
            SourceMetadata(
                provider=persisted_provider_data.source.provider,
                entity_id=persisted_provider_data.source.entity_id,
            )
            if persisted_provider_data is not None
            else data.source
        ),
        retrieved_at=(
            persisted_provider_data.retrieved_at
            if persisted_provider_data is not None
            else data.retrieved_at
        ),
        latest_observation_at=(
            persisted_provider_data.latest_observation_at
            if persisted_provider_data is not None
            else data.latest_observation_at
        ),
    )


@app.get(
    "/api/v1/household-load",
    response_model=HouseholdLoadResponse,
    responses={
        404: {"description": "No persisted provider data is available"},
        503: {"description": "Provider data persistence is unavailable"},
    },
)
def persisted_household_load(request: Request) -> HouseholdLoadResponse:
    """Return the latest persisted normalized household-load provider data."""
    configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="provider data persistence is not configured",
        )
    if configuration.home_assistant is None:
        raise HTTPException(
            status_code=503,
            detail="the Home Assistant household-load provider is not configured",
        )

    source = SourceMetadata(
        provider="home-assistant",
        entity_id=configuration.home_assistant.household_load_source_id,
    )
    key = ProviderDataKey(
        data_type="household-load",
        provider=source.provider,
        entity_id=source.entity_id,
    )
    try:
        provider_data = store.load(key, HOUSEHOLD_LOAD_ADAPTER)
    except ProviderDataStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=f"could not recover household-load provider data: {error}",
        ) from error
    if provider_data is None:
        raise HTTPException(
            status_code=404,
            detail="no persisted household-load provider data is available",
        )
    return HouseholdLoadResponse(
        status="validated",
        schema_version=provider_data.schema_version,
        start_time=provider_data.start_time,
        interval_minutes=provider_data.interval_minutes,
        load_kw=list(provider_data.load_kw),
        unit=provider_data.unit,
        source=SourceMetadata(
            provider=provider_data.source.provider,
            entity_id=provider_data.source.entity_id,
        ),
        retrieved_at=provider_data.retrieved_at,
        latest_observation_at=provider_data.latest_observation_at,
    )


@app.get(
    "/api/v1/historic/household-load",
    response_model=HistoricHouseholdLoadResponse,
    responses={
        404: {"description": "No persisted household-load data is available"},
        503: {"description": "Persisted household-load data is unavailable"},
    },
)
def historic_household_load(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
) -> HistoricHouseholdLoadResponse:
    """Return persisted household-load actuals for a requested time range."""
    if any(
        timestamp.tzinfo is None or timestamp.utcoffset() is None
        for timestamp in (start_time, end_time)
    ):
        raise HTTPException(
            status_code=422,
            detail="start_time and end_time must include a timezone",
        )
    start = start_time.astimezone(timezone.utc)
    end = end_time.astimezone(timezone.utc)
    if end <= start:
        raise HTTPException(
            status_code=422,
            detail="end_time must be later than start_time",
        )
    if end - start > timedelta(hours=MAX_HORIZON_HOURS):
        raise HTTPException(
            status_code=422,
            detail=f"requested range must not exceed {MAX_HORIZON_HOURS} hours",
        )

    configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="provider data persistence is not configured",
        )
    if configuration.home_assistant is None:
        raise HTTPException(
            status_code=503,
            detail="the Home Assistant household-load provider is not configured",
        )

    source = SourceMetadata(
        provider="home-assistant",
        entity_id=configuration.home_assistant.household_load_source_id,
    )
    key = ProviderDataKey(
        data_type="household-load",
        provider=source.provider,
        entity_id=source.entity_id,
    )
    try:
        complete_data = store.load(key, HOUSEHOLD_LOAD_ADAPTER)
        if complete_data is None:
            raise HTTPException(
                status_code=404,
                detail="no persisted household-load provider data is available",
            )
        provider_data = store.load_household_load_range(key, start, end)
    except HTTPException:
        raise
    except ProviderDataStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=f"could not recover household-load provider data: {error}",
        ) from error

    available_start = complete_data.start_time.astimezone(timezone.utc)
    available_end = available_start + timedelta(hours=len(complete_data.load_kw))
    freshness = _household_load_freshness(
        complete_data,
        configuration.home_assistant.max_data_age_seconds,
    )
    has_suspect_quality = provider_data is not None and any(
        item.status == "suspect" for item in _expanded_quality(provider_data)
    )
    response_status: Literal["validated", "stale", "suspect", "empty"] = (
        "empty"
        if provider_data is None
        else (
            "suspect"
            if has_suspect_quality
            else ("stale" if freshness == "stale" else "validated")
        )
    )
    timestamps = (
        [
            provider_data.start_time.astimezone(timezone.utc) + timedelta(hours=index)
            for index in range(len(provider_data.load_kw))
        ]
        if provider_data is not None
        else []
    )
    return HistoricHouseholdLoadResponse(
        status=response_status,
        data_type="household_load",
        schema_version=complete_data.schema_version,
        start_time=start,
        end_time=end,
        interval_minutes=complete_data.interval_minutes,
        timestamps=timestamps,
        load_kw=list(provider_data.load_kw) if provider_data is not None else [],
        quality=(
            [
                HouseholdLoadQuality(
                    status=item.status,
                    reason=item.reason,
                    entity_id=item.entity_id,
                )
                for item in _expanded_quality(provider_data)
            ]
            if provider_data is not None
            else []
        ),
        unit=complete_data.unit,
        source=SourceMetadata(
            provider=complete_data.source.provider,
            entity_id=complete_data.source.entity_id,
        ),
        coverage_start_time=(
            provider_data.start_time if provider_data is not None else None
        ),
        coverage_end_time=(
            provider_data.start_time + timedelta(hours=len(provider_data.load_kw))
            if provider_data is not None
            else None
        ),
        available_start_time=available_start,
        available_end_time=available_end,
        retrieved_at=complete_data.retrieved_at,
        latest_observation_at=complete_data.latest_observation_at,
        validation_status="suspect" if has_suspect_quality else "valid",
        freshness=freshness,
        freshness_checked_at=datetime.now(timezone.utc),
    )


def _expanded_quality(data: HouseholdLoadData) -> tuple[IntervalQuality, ...]:
    """Return one quality value for every persisted household-load interval."""
    if data.quality:
        if len(data.quality) != len(data.load_kw):
            raise HTTPException(
                status_code=503,
                detail="persisted household-load quality metadata is misaligned",
            )
        return data.quality
    return tuple(IntervalQuality() for _ in data.load_kw)


def _household_load_freshness(
    data: HouseholdLoadData,
    max_age_seconds: float | None,
) -> Literal["fresh", "stale", "unknown"]:
    """Assess polling freshness without invalidating historical actuals."""
    if max_age_seconds is None:
        return "unknown"
    age_seconds = (
        datetime.now(timezone.utc) - data.latest_observation_at.astimezone(timezone.utc)
    ).total_seconds()
    return "fresh" if age_seconds <= max_age_seconds else "stale"


def _dashboard_source(data: object) -> SourceMetadata:
    """Map normalized provider identity to the dashboard response model."""
    source = getattr(data, "source", None)
    if not isinstance(source, ProviderSourceMetadata):
        raise ProviderDataStoreError("normalized dashboard data has no source identity")
    return SourceMetadata(provider=source.provider, entity_id=source.entity_id)


def _dashboard_range(
    start_time: datetime,
    end_time: datetime,
) -> tuple[datetime, datetime]:
    """Validate and normalize one dashboard half-open hourly range."""
    if any(
        timestamp.tzinfo is None or timestamp.utcoffset() is None
        for timestamp in (start_time, end_time)
    ):
        raise HTTPException(
            status_code=422,
            detail="start_time and end_time must include a timezone",
        )
    start = start_time.astimezone(timezone.utc)
    end = end_time.astimezone(timezone.utc)
    if (
        start.minute
        or start.second
        or start.microsecond
        or end.minute
        or end.second
        or end.microsecond
    ):
        raise HTTPException(
            status_code=422,
            detail="dashboard range boundaries must be aligned to the hour",
        )
    if end <= start:
        raise HTTPException(
            status_code=422, detail="end_time must be later than start_time"
        )
    if end - start > timedelta(hours=MAX_HORIZON_HOURS):
        raise HTTPException(
            status_code=422,
            detail=f"requested range must not exceed {MAX_HORIZON_HOURS} hours",
        )
    return start, end


def _dashboard_response(
    start: datetime,
    end: datetime,
    series: list[DashboardSeries],
    diagnostics: list[str],
    *,
    plan_summary: DashboardPlanSummary | None = None,
) -> DashboardDataResponse:
    """Build the common envelope from explicit series states."""
    if not series:
        status: Literal[
            "validated",
            "partial",
            "stale",
            "empty",
            "unavailable",
            "invalid",
            "infeasible",
        ] = (
            "invalid"
            if any("invalid" in diagnostic for diagnostic in diagnostics)
            else ("unavailable" if diagnostics else "empty")
        )
    elif any(item.freshness == "stale" for item in series):
        status = "stale"
    elif any(not item.timestamps for item in series):
        status = "empty"
    elif any(
        (item.available_start_time is not None and item.available_start_time > start)
        or (item.available_end_time is not None and item.available_end_time < end)
        for item in series
    ) or any(item.missing_intervals for item in series):
        status = "partial"
    else:
        status = "validated"
    return DashboardDataResponse(
        schema_version="1",
        status=status,
        requested_start_time=start,
        requested_end_time=end,
        interval_minutes=60,
        series=series,
        diagnostics=diagnostics,
        plan_summary=plan_summary,
    )


def _household_dashboard_series(
    data: HistoricHouseholdLoadResponse,
) -> DashboardSeries:
    """Map the historic household-load response to the unified series shape."""
    return DashboardSeries(
        id="household_load_actual",
        data_type="household_load",
        scenario_kind="actual",
        timestamps=data.timestamps,
        values=cast(list[float | None], data.load_kw),
        unit=data.unit,
        source=data.source,
        requested_start_time=data.start_time,
        requested_end_time=data.end_time,
        available_start_time=data.available_start_time,
        available_end_time=data.available_end_time,
        retrieved_at=data.retrieved_at,
        freshness=data.freshness,
        validation_status=data.validation_status,
        missing_intervals=[
            data.start_time + timedelta(hours=index)
            for index in range(
                int((data.end_time - data.start_time).total_seconds() // 3600)
            )
            if data.start_time + timedelta(hours=index) not in data.timestamps
        ],
    )


def _pv_dashboard_series(
    data: PvGenerationData,
    start: datetime,
    end: datetime,
    freshness: Literal["fresh", "stale", "unknown"],
) -> DashboardSeries:
    """Map a persisted PV forecast to the common series shape."""
    timestamps = [
        data.start_time + timedelta(hours=index)
        for index in range(len(data.generation_kw))
    ]
    values_by_timestamp = dict(zip(timestamps, data.generation_kw))
    requested_timestamps = [
        start + timedelta(hours=index)
        for index in range(int((end - start).total_seconds() // 3600))
    ]
    selected = [
        (timestamp, values_by_timestamp.get(timestamp))
        for timestamp in requested_timestamps
    ]
    available_end = data.start_time + timedelta(hours=len(data.generation_kw))
    return DashboardSeries(
        id="pv_generation_forecast",
        data_type="pv_generation",
        scenario_kind="forecast",
        timestamps=[timestamp for timestamp, _ in selected],
        values=[value for _, value in selected],
        unit=data.unit,
        source=_dashboard_source(data),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.start_time,
        available_end_time=available_end,
        retrieved_at=data.retrieved_at,
        generated_at=data.generated_at,
        published_at=data.published_at,
        freshness=freshness,
        validation_status="valid",
        missing_intervals=[timestamp for timestamp, value in selected if value is None],
    )


def _price_dashboard_series(
    data: ElectricityPriceData,
    start: datetime,
    end: datetime,
    direction: Literal["import", "export"],
    freshness: Literal["fresh", "stale", "unknown"],
) -> DashboardSeries:
    """Map one normalized price direction to a forecast series."""
    values = (
        data.import_price_eur_per_kwh
        if direction == "import"
        else data.export_price_eur_per_kwh
    )
    values_by_timestamp = dict(zip(data.timestamps, values))
    requested_timestamps = [
        start + timedelta(hours=index)
        for index in range(int((end - start).total_seconds() // 3600))
    ]
    selected = [
        (timestamp, values_by_timestamp.get(timestamp))
        for timestamp in requested_timestamps
    ]
    return DashboardSeries(
        id=f"{direction}_price_forecast",
        data_type=f"{direction}_price",
        scenario_kind="forecast",
        timestamps=[timestamp for timestamp, _ in selected],
        values=[value for _, value in selected],
        unit=data.unit,
        source=_dashboard_source(data),
        requested_start_time=start,
        requested_end_time=end,
        available_start_time=data.timestamps[0],
        available_end_time=data.timestamps[-1] + timedelta(hours=1),
        retrieved_at=data.retrieved_at,
        freshness=freshness,
        validation_status="valid",
        missing_intervals=[timestamp for timestamp, value in selected if value is None],
    )


def _forecast_dashboard_data(
    request: Request,
    start: datetime,
    end: datetime,
) -> DashboardDataResponse:
    """Load the latest coherent persisted forecast series."""
    configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        return _dashboard_response(
            start, end, [], ["forecast data persistence is not configured"]
        )

    series: list[DashboardSeries] = []
    diagnostics: list[str] = []
    if configuration.forecast_solar is not None:
        key = ProviderDataKey(
            data_type="pv-generation",
            provider="forecast.solar",
            entity_id=configuration.forecast_solar.pv_generation_source_id,
        )
        try:
            data = store.load(key, PV_GENERATION_ADAPTER)
        except ProviderDataStoreError:
            data = None
            diagnostics.append("PV forecast data is invalid and could not be recovered")
        if data is None:
            diagnostics.append("PV forecast data is unavailable")
        else:
            from energy_optimizer.providers.forecast_solar import ForecastSolarImporter

            importer = ForecastSolarImporter(configuration.forecast_solar)
            freshness: Literal["fresh", "stale", "unknown"] = (
                "fresh" if importer.is_fresh(data) else "stale"
            )
            series.append(_pv_dashboard_series(data, start, end, freshness))

    if configuration.awattar is not None:
        key = ProviderDataKey(
            data_type="electricity-prices",
            provider="awattar.de",
            entity_id=configuration.awattar.electricity_price_source_id,
        )
        try:
            data = store.load(key, ELECTRICITY_PRICE_ADAPTER)
        except ProviderDataStoreError:
            data = None
            diagnostics.append(
                "electricity-price forecast data is invalid and could not be recovered"
            )
        if data is None:
            diagnostics.append("electricity-price forecast data is unavailable")
        else:
            from energy_optimizer.providers.awattar import AwattarImporter

            price_importer = AwattarImporter(configuration.awattar)
            price_freshness: Literal["fresh", "stale", "unknown"] = (
                "fresh" if price_importer.is_fresh(data) else "stale"
            )
            series.extend(
                (
                    _price_dashboard_series(
                        data, start, end, "import", price_freshness
                    ),
                    _price_dashboard_series(
                        data, start, end, "export", price_freshness
                    ),
                )
            )
    return _dashboard_response(start, end, series, diagnostics)


@app.get(
    "/api/v1/dashboard/data",
    response_model=DashboardDataResponse,
    responses={503: {"description": "Dashboard data persistence is unavailable"}},
)
def dashboard_data(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
    scenario_kind: Literal["actual", "forecast", "plan"] = Query("actual"),
) -> DashboardDataResponse:
    """Return the versioned dashboard contract without mixing scenarios."""
    start, end = _dashboard_range(start_time, end_time)
    if scenario_kind == "forecast":
        return _forecast_dashboard_data(request, start, end)
    if scenario_kind == "plan":
        return _dashboard_response(
            start,
            end,
            [],
            ["optimization plan data is unavailable"],
            plan_summary=DashboardPlanSummary(status="unavailable"),
        )
    if request.app.state.provider_data_store is None:
        return _dashboard_response(
            start,
            end,
            [],
            ["household-load data persistence is not configured"],
        )
    try:
        actuals = historic_household_load(request, start, end)
    except HTTPException as error:
        if error.status_code in {404, 503}:
            return _dashboard_response(start, end, [], [str(error.detail)])
        raise
    return _dashboard_response(
        start,
        end,
        [_household_dashboard_series(actuals)],
        (
            ["no household-load points are available in the requested range"]
            if actuals.status == "empty"
            else []
        ),
    )


@app.get(
    "/api/v1/forecast",
    response_model=DashboardDataResponse,
    include_in_schema=False,
)
def forecast_data(
    request: Request,
    start_time: datetime = Query(description="Inclusive timezone-aware range start"),
    end_time: datetime = Query(description="Exclusive timezone-aware range end"),
) -> DashboardDataResponse:
    """Return forecast data through the dashboard forecast read path."""
    start, end = _dashboard_range(start_time, end_time)
    return _forecast_dashboard_data(request, start, end)


@app.post("/api/v1/pv-generation", response_model=PvGenerationResponse)
def pv_generation(request: PvGenerationRequest) -> PvGenerationResponse:
    """Validate a versioned hourly PV-generation data series."""
    return PvGenerationResponse(
        status="validated",
        schema_version=request.schema_version,
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        generation_kw=request.generation_kw,
        unit=request.unit,
        source=request.source,
    )


@app.post("/api/v1/grid-flow", response_model=GridFlowResponse)
def grid_flow(request: Request, data: GridFlowRequest) -> GridFlowResponse:
    """Validate a versioned hourly grid import and export data series."""
    application_configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    persisted_provider_data: GridFlowData | None = None
    if (
        store is not None
        and data.source is not None
        and application_configuration.is_configured_grid_flow_source(
            data.source.provider, data.source.entity_id
        )
    ):
        key = ProviderDataKey(
            data_type="grid-flow",
            provider=data.source.provider,
            entity_id=data.source.entity_id,
        )
        provider_data = GridFlowData(
            schema_version=data.schema_version,
            start_time=data.start_time,
            interval_minutes=data.interval_minutes,
            import_kw=tuple(data.import_kw),
            export_kw=tuple(data.export_kw),
            unit=data.unit,
            source=ProviderSourceMetadata(
                provider=data.source.provider,
                entity_id=data.source.entity_id,
            ),
            retrieved_at=data.retrieved_at,
            latest_observation_at=data.latest_observation_at,
        )
        try:
            persisted_provider_data = store.save(key, GRID_FLOW_ADAPTER, provider_data)
        except ProviderDataStoreError as error:
            raise HTTPException(
                status_code=503,
                detail=f"could not persist grid-flow provider data: {error}",
            ) from error
    return GridFlowResponse(
        status="validated",
        schema_version=data.schema_version,
        start_time=(
            persisted_provider_data.start_time
            if persisted_provider_data is not None
            else data.start_time
        ),
        interval_minutes=data.interval_minutes,
        import_kw=(
            list(persisted_provider_data.import_kw)
            if persisted_provider_data is not None
            else data.import_kw
        ),
        export_kw=(
            list(persisted_provider_data.export_kw)
            if persisted_provider_data is not None
            else data.export_kw
        ),
        unit=data.unit,
        source=(
            SourceMetadata(
                provider=persisted_provider_data.source.provider,
                entity_id=persisted_provider_data.source.entity_id,
            )
            if persisted_provider_data is not None
            else data.source
        ),
        retrieved_at=(
            persisted_provider_data.retrieved_at
            if persisted_provider_data is not None
            else data.retrieved_at
        ),
        latest_observation_at=(
            persisted_provider_data.latest_observation_at
            if persisted_provider_data is not None
            else data.latest_observation_at
        ),
    )


@app.get(
    "/api/v1/grid-flow",
    response_model=GridFlowResponse,
    responses={
        404: {"description": "No persisted provider data is available"},
        503: {"description": "Provider data persistence is unavailable"},
    },
)
def persisted_grid_flow(request: Request) -> GridFlowResponse:
    """Return the latest persisted normalized grid-flow provider data."""
    application_configuration = request.app.state.configuration
    store = request.app.state.provider_data_store
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="provider data persistence is not configured",
        )
    if application_configuration.home_assistant is None:
        raise HTTPException(
            status_code=503,
            detail="the Home Assistant grid-flow provider is not configured",
        )

    source = SourceMetadata(
        provider="home-assistant",
        entity_id=application_configuration.home_assistant.grid_flow_source_id,
    )
    key = ProviderDataKey(
        data_type="grid-flow",
        provider=source.provider,
        entity_id=source.entity_id,
    )
    try:
        provider_data = store.load(key, GRID_FLOW_ADAPTER)
    except ProviderDataStoreError as error:
        raise HTTPException(
            status_code=503,
            detail=f"could not recover grid-flow provider data: {error}",
        ) from error
    if provider_data is None:
        raise HTTPException(
            status_code=404,
            detail="no persisted grid-flow provider data is available",
        )
    return GridFlowResponse(
        status="validated",
        schema_version=provider_data.schema_version,
        start_time=provider_data.start_time,
        interval_minutes=provider_data.interval_minutes,
        import_kw=list(provider_data.import_kw),
        export_kw=list(provider_data.export_kw),
        unit=provider_data.unit,
        source=SourceMetadata(
            provider=provider_data.source.provider,
            entity_id=provider_data.source.entity_id,
        ),
        retrieved_at=provider_data.retrieved_at,
        latest_observation_at=provider_data.latest_observation_at,
    )


if FRONTEND_DIRECTORY.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=FRONTEND_DIRECTORY, html=True),
        name="dashboard",
    )
