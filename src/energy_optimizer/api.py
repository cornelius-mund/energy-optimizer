"""HTTP API for the Energy Optimizer service."""

import math
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, AsyncIterator, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from energy_optimizer import __version__
from energy_optimizer.config import load_configuration

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
        """Require timestamps that identify an unambiguous hourly series."""
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
            raise ValueError("start_time must include a timezone")
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
        if self.start_time.tzinfo is None or self.start_time.utcoffset() is None:
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
    path = Path(os.environ.get("ENERGY_OPTIMIZER_CONFIG", "config.yaml"))
    application.state.configuration = load_configuration(path)
    yield


app = FastAPI(title="Energy Optimizer", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Return the service health and running version."""
    return {"status": "ok", "version": __version__}


@app.post("/optimize", response_model=OptimizationResponse)
def optimize(request: HourlyOptimizationRequest) -> OptimizationResponse:
    """Validate an hourly optimization request at the versioned API boundary."""
    return OptimizationResponse(
        status="validated",
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        hours=len(request.load_kw),
    )


@app.post("/api/v1/household-load", response_model=HouseholdLoadResponse)
def household_load(request: HouseholdLoadRequest) -> HouseholdLoadResponse:
    """Validate a versioned hourly household-load data series."""
    return HouseholdLoadResponse(
        status="validated",
        schema_version=request.schema_version,
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        load_kw=request.load_kw,
        unit=request.unit,
        source=request.source,
    )


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
def grid_flow(request: GridFlowRequest) -> GridFlowResponse:
    """Validate a versioned hourly grid import and export data series."""
    return GridFlowResponse(
        status="validated",
        schema_version=request.schema_version,
        start_time=request.start_time,
        interval_minutes=request.interval_minutes,
        import_kw=request.import_kw,
        export_kw=request.export_kw,
        unit=request.unit,
        source=request.source,
    )
